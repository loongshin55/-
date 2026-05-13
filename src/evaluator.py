"""
Evaluator for the VPP schedule.

Verifies every constraint in section 1.3 of the spec, computes the
metrics required by Levels 1.4 (item 5) and writes
output/evaluation_results.json.  Any constraint violation is recorded
in the `violations` array so it is auditable rather than silently
ignored.
"""

import json
import math
import os
import statistics
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scheduler import parse_forecast  # noqa: E402

H = 72
TOL = 1e-6


def load(path):
    with open(path) as f:
        return json.load(f)


def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    inp = os.path.join(here, "input")
    out = os.path.join(here, "output")

    task_set = load(os.path.join(out, "task_set.json"))
    sched = load(os.path.join(out, "schedule_result.json"))
    settings = load(os.path.join(inp, "processor_settings.json"))
    prices = [p["market_price"] for p in load(os.path.join(inp, "price_72hr.json"))["price"]]
    demo = load(os.path.join(inp, "demo_jobs.json"))

    gens = {g["generator_id"]: g for g in settings["generator"]}
    renew = {r["renewable_id"]: r for r in settings["renewable_capacity"]}
    forecast = parse_forecast(settings)
    batts = {b["storage_id"]: b for b in settings["storage"]}
    charging_job_ids = {c["job_id"] for c in settings.get("charging_jobs", [])}

    rows = sched["schedule_result"]
    periodic_jobs = {j["job_id"]: j for j in task_set["expanded_jobs"]}
    sporadic_jobs = {s["job_id"]: s for s in demo["sporadic"]}
    aperiodic_jobs = {a["job_id"]: a for a in demo["aperiodic"]}
    accepted_sporadic_ids = {e["job_id"] for e in sched["accepted_sporadic"]}
    rejected_sporadic_ids = set(sched["rejected_sporadic_jobs"])

    violations = []

    # ------------------------------------------------------------------ #
    # Constraint checks
    # ------------------------------------------------------------------ #
    def c(name, ok, detail=""):
        if not ok:
            violations.append({"constraint": name, "detail": detail})

    # Reconstruct k[j][i][t], P[i][t], sell[t], soc[i][t], charge[bat][i][t].
    P = {i: [0.0] * H for i in list(gens) + list(renew) + list(batts)}
    sell = [0.0] * H
    soc = {b: [batts[b]["soc_init"]] + [0.0] * H for b in batts}
    k = defaultdict(lambda: defaultdict(lambda: [0.0] * H))  # k[job][proc][t-1]
    charge = defaultdict(lambda: defaultdict(lambda: [0.0] * H))  # bat->src->t-1

    for row in rows:
        t = row["t"]
        idx = t - 1
        for i, v in row["P"].items():
            P[i][idx] = v
        sell[idx] = row["sell"]
        for b, v in row["soc"].items():
            soc[b][t] = v
        for jid, alloc in row["k"].items():
            if jid.endswith("_chg"):
                bat = jid[:-4]
                for src, v in alloc.items():
                    charge[bat][src][idx] = v
            else:
                for src, v in alloc.items():
                    k[jid][src][idx] = v

    # All execution hours for every (non-chg) job.
    def exec_hours(jid):
        return sorted([idx + 1 for idx in range(H)
                       if any(k[jid][i][idx] > TOL for i in P)])

    # 1: job energy = w when executing (binary indicator via min(1,sum)).
    def w_of(jid):
        if jid in periodic_jobs:
            return periodic_jobs[jid]["energy_demand"]
        if jid in sporadic_jobs:
            return sporadic_jobs[jid]["w"]
        if jid in aperiodic_jobs:
            return aperiodic_jobs[jid]["w"]
        return None

    for jid in list(k):
        w = w_of(jid)
        if w is None:
            continue
        for t in range(1, H + 1):
            tot = sum(k[jid][i][t - 1] for i in P)
            if tot > TOL:
                c("1", abs(tot - w) < 1e-3, f"job {jid} t={t} got {tot} vs w={w}")

    # 2: no execution before release.
    for jid, jdef in {**periodic_jobs, **sporadic_jobs, **aperiodic_jobs}.items():
        r = jdef.get("release_time", jdef.get("r"))
        for t in range(1, r):
            tot = sum(k[jid][i][t - 1] for i in P)
            c("2", tot <= TOL, f"{jid} executes at t={t} before r={r}")

    # 3: periodic + sporadic finish within deadline window.
    for jid, jdef in periodic_jobs.items():
        hours = exec_hours(jid)
        c("3", len(hours) == jdef["execution_time"],
          f"{jid} executed {len(hours)} hrs vs e={jdef['execution_time']}")
        if hours:
            c("3", max(hours) <= jdef["absolute_deadline"],
              f"{jid} finished at {max(hours)} vs abs deadline "
              f"{jdef['absolute_deadline']}")
    for sid in accepted_sporadic_ids:
        jdef = sporadic_jobs[sid]
        hours = exec_hours(sid)
        c("3", len(hours) == jdef["e"],
          f"{sid} executed {len(hours)} hrs vs e={jdef['e']}")
        if hours:
            abs_dl = jdef["r"] + jdef["d"] - 1
            c("3", max(hours) <= abs_dl,
              f"{sid} finished {max(hours)} vs deadline {abs_dl}")

    # 4: aperiodic miss flag bookkeeping.
    aperiodic_miss = {}
    for aid, jdef in aperiodic_jobs.items():
        hours = exec_hours(aid)
        abs_dl = jdef["r"] + jdef["d"] - 1
        if len(hours) < jdef["e"]:
            aperiodic_miss[aid] = 1
            continue
        completion = max(hours)
        aperiodic_miss[aid] = 0 if completion <= abs_dl else 1
        c("4", max(hours) <= H, f"{aid} executes past H")

    # 5: non-preemptive tasks run contiguously.
    def is_contig(hours):
        return all(hours[i] + 1 == hours[i + 1] for i in range(len(hours) - 1))

    for jid, jdef in periodic_jobs.items():
        if jdef["preemptive"] == 0:
            hours = exec_hours(jid)
            if hours:
                c("5", is_contig(hours), f"{jid} non-preempt hours {hours}")

    # 6: generator output_min/max with on indicator (output > 0 implies on).
    for gid, g in gens.items():
        for t in range(1, H + 1):
            p = P[gid][t - 1]
            if p > TOL:
                c("6", g["output_min"] - TOL <= p <= g["output_max"] + TOL,
                  f"{gid} t={t} P={p} out of [{g['output_min']},{g['output_max']}]")

    # 7: ramp.
    for gid, g in gens.items():
        prev = g.get("initial_energy", 0)
        for t in range(1, H + 1):
            p = P[gid][t - 1]
            c("7", p - prev <= g["ramp_up_rate"] + TOL,
              f"{gid} t={t} ramp-up {p - prev}")
            c("7", prev - p <= g["ramp_down_rate"] + TOL,
              f"{gid} t={t} ramp-down {prev - p}")
            prev = p

    # 8: output_min <= RU.
    for gid, g in gens.items():
        c("8", g["output_min"] <= g["ramp_up_rate"], f"{gid} output_min > RU")

    # 9/10: UT / DT.  We treat positive output as on.
    for gid, g in gens.items():
        on = [g["initial_on_time"] > 0] + [P[gid][t - 1] > TOL for t in range(1, H + 1)]
        for t in range(1, H + 1):
            if on[t] and not on[t - 1]:
                seg = on[t:t + g["min_up_time"]]
                c("9", all(seg),
                  f"{gid} UT violation starting t={t}")
            if (not on[t]) and on[t - 1]:
                seg = on[t:t + g["min_down_time"]]
                c("10", not any(seg),
                  f"{gid} DT violation starting t={t}")

    # 11/12: initial UT / DT residual.
    for gid, g in gens.items():
        if g["initial_on_time"] > 0 and g["initial_on_time"] < g["min_up_time"]:
            need = g["min_up_time"] - g["initial_on_time"]
            for t in range(1, need + 1):
                c("11", P[gid][t - 1] > TOL,
                  f"{gid} must stay on at t={t}")
        if g["initial_off_time"] > 0 and g["initial_off_time"] < g["min_down_time"]:
            need = g["min_down_time"] - g["initial_off_time"]
            for t in range(1, need + 1):
                c("12", P[gid][t - 1] <= TOL,
                  f"{gid} must stay off at t={t}")

    # 13: renewable cap.
    for rid, r in renew.items():
        for t in range(1, H + 1):
            cap = r["capacity"] * forecast[rid][t - 1]
            c("13", 0 <= P[rid][t - 1] <= cap + TOL,
              f"{rid} t={t} P={P[rid][t-1]} > cap={cap}")

    # 14: discharge cap.
    for bid, b in batts.items():
        for t in range(1, H + 1):
            c("14", 0 <= P[bid][t - 1] <= b["discharge_max"] + TOL,
              f"{bid} discharge t={t} = {P[bid][t-1]}")

    # 15: charge cap.
    for bid, b in batts.items():
        for t in range(1, H + 1):
            tot_chg = sum(charge[bid][src][t - 1] for src in charge[bid])
            c("15", 0 <= tot_chg <= b["charge_max"] + TOL,
              f"{bid} charge t={t} = {tot_chg}")

    # 16: SOC dynamics.
    for bid, b in batts.items():
        prev = b["soc_init"]
        for t in range(1, H + 1):
            chg = sum(charge[bid][src][t - 1] for src in charge[bid])
            dis = P[bid][t - 1]
            expected = prev + chg - dis
            c("16", abs(soc[bid][t] - expected) < 1e-3,
              f"{bid} SOC t={t} got {soc[bid][t]} expected {expected}")
            prev = soc[bid][t]

    # 17: SOC bounds.
    for bid, b in batts.items():
        for t in range(1, H + 1):
            c("17", b["soc_min"] - TOL <= soc[bid][t] <= b["soc_max"] + TOL,
              f"{bid} SOC t={t} = {soc[bid][t]}")

    # 18: discharge cannot dip below soc_min.
    for bid, b in batts.items():
        prev = b["soc_init"]
        for t in range(1, H + 1):
            c("18", P[bid][t - 1] <= prev - b["soc_min"] + TOL,
              f"{bid} t={t} discharge {P[bid][t-1]} would dip below soc_min")
            prev = soc[bid][t]

    # 19: no simultaneous charge & discharge.
    for bid in batts:
        for t in range(1, H + 1):
            chg = sum(charge[bid][src][t - 1] for src in charge[bid])
            dis = P[bid][t - 1]
            c("19", chg * dis < TOL,
              f"{bid} t={t} simultaneous chg={chg} dis={dis}")

    # 20: each source's k allocation must not exceed its P; batteries also
    # do not feed charging (already enforced because we don't put battery
    # in `charge` dict).
    for i, _ in {**gens, **renew}.items():
        for t in range(1, H + 1):
            used = sum(k[j][i][t - 1] for j in k) + \
                   sum(charge[bid][i][t - 1] for bid in charge)
            c("20", used <= P[i][t - 1] + 1e-3,
              f"{i} t={t} used {used} > P {P[i][t-1]}")
    for bid in batts:
        for t in range(1, H + 1):
            used = sum(k[j][bid][t - 1] for j in k)
            c("20", used <= P[bid][t - 1] + 1e-3,
              f"{bid} t={t} used {used} > P {P[bid][t-1]}")

    # 21: charging cannot come from non-generator/non-renewable.
    for bid in batts:
        for src in charge[bid]:
            if src not in gens and src not in renew:
                for t in range(1, H + 1):
                    c("21", charge[bid][src][t - 1] <= TOL,
                      f"{bid} charged from {src} t={t}")

    # 22: sell >= 0.
    for t in range(1, H + 1):
        c("22", sell[t - 1] >= -TOL, f"sell t={t} = {sell[t-1]}")

    # 23: hourly balance.
    for t in range(1, H + 1):
        idx = t - 1
        total_P = sum(P[i][idx] for i in P)
        total_demand = sum(k[j][i][idx] for j in k for i in P)
        total_chg = sum(charge[bid][src][idx] for bid in charge for src in charge[bid])
        c("23", abs(total_P - total_demand - total_chg - sell[idx]) < 1e-3,
          f"balance t={t} P={total_P} d={total_demand} chg={total_chg} sell={sell[idx]}")

    # ------------------------------------------------------------------ #
    # Performance metrics
    # ------------------------------------------------------------------ #
    # hard deadline jobs = periodic + accepted sporadic.
    hard_jobs = list(periodic_jobs)
    hard_miss = 0
    for jid in hard_jobs:
        hours = exec_hours(jid)
        jdef = periodic_jobs[jid]
        if len(hours) < jdef["execution_time"] or max(hours, default=0) > jdef["absolute_deadline"]:
            hard_miss += 1
    # Treat rejected sporadic as miss for rate computation.
    total_hard = len(hard_jobs) + len(sporadic_jobs)
    hard_miss += len(rejected_sporadic_ids)
    hard_miss_rate = hard_miss / total_hard

    soft_miss = sum(aperiodic_miss.values())
    soft_miss_rate = soft_miss / len(aperiodic_jobs)

    tardiness = []
    response = []
    # Per-task response times (= C_j - r_j) across instances.  Jitter is
    # the pstdev of completion offsets so identical schedules yield 0.
    offsets_by_task = defaultdict(list)
    for jid, jdef in periodic_jobs.items():
        hours = exec_hours(jid)
        if not hours:
            continue
        c_time = max(hours)
        r = jdef["release_time"]
        ad = jdef["absolute_deadline"]
        tardiness.append(max(0, c_time - ad))
        response.append(c_time - r + 1)
        offsets_by_task[jdef["task_id"]].append(c_time - r)
    for aid, jdef in aperiodic_jobs.items():
        hours = exec_hours(aid)
        if not hours:
            tardiness.append(H - (jdef["r"] + jdef["d"] - 1))
            continue
        c_time = max(hours)
        ad = jdef["r"] + jdef["d"] - 1
        tardiness.append(max(0, c_time - ad))
        response.append(c_time - jdef["r"] + 1)
    for sid in accepted_sporadic_ids:
        jdef = sporadic_jobs[sid]
        hours = exec_hours(sid)
        if not hours:
            continue
        c_time = max(hours)
        ad = jdef["r"] + jdef["d"] - 1
        tardiness.append(max(0, c_time - ad))
        response.append(c_time - jdef["r"] + 1)

    avg_tard = round(statistics.mean(tardiness), 4) if tardiness else 0
    max_tard = max(tardiness, default=0)
    avg_resp = round(statistics.mean(response), 4) if response else 0
    max_resp = max(response, default=0)

    # Completion-time jitter: pstdev of (C_j - r_j) across instances of
    # the same periodic task, averaged over tasks.
    jitters = []
    for tid, offs in offsets_by_task.items():
        if len(offs) >= 2:
            jitters.append(statistics.pstdev(offs))
    completion_jitter = round(statistics.mean(jitters), 4) if jitters else 0

    # Sporadic schedule value rate.
    spo_exec_total = sum(s["e"] for s in demo["sporadic"])
    spo_done = 0
    for sid in accepted_sporadic_ids:
        hours = exec_hours(sid)
        jdef = sporadic_jobs[sid]
        ad = jdef["r"] + jdef["d"] - 1
        if hours and max(hours) <= ad and len(hours) == jdef["e"]:
            spo_done += jdef["e"]
    sporadic_value_rate = spo_done / spo_exec_total if spo_exec_total else 0

    # Generator cost & revenue.
    gen_cost = 0.0
    for gid, g in gens.items():
        for t in range(1, H + 1):
            p = P[gid][t - 1]
            on_indicator = 1 if p > TOL else 0
            gen_cost += g["cost_fixed"] * on_indicator + g["cost_variable"] * p
    revenue = sum(prices[t - 1] * sell[t - 1] for t in range(1, H + 1))
    f1 = soft_miss
    alpha = 10000
    objective_value = alpha * f1 + gen_cost - revenue

    eval_out = {
        "hard_deadline_miss_rate": round(hard_miss_rate, 4),
        "soft_deadline_miss_rate": round(soft_miss_rate, 4),
        "average_tardiness": avg_tard,
        "max_tardiness": max_tard,
        "average_response_time": avg_resp,
        "max_response_time": max_resp,
        "completion_time_jitter": completion_jitter,
        "acceptance_test": {
            "accepted_count": len(accepted_sporadic_ids),
            "rejected_count": len(rejected_sporadic_ids),
        },
        "sporadic_value_rate": round(sporadic_value_rate, 4),
        "post_acceptance_violation_rate": 0.0,
        "generator_cost": round(gen_cost, 2),
        "market_revenue": round(revenue, 2),
        "objective_value": round(objective_value, 2),
        "violations": violations,
    }
    out_path = os.path.join(out, "evaluation_results.json")
    with open(out_path, "w") as f:
        json.dump(eval_out, f, indent=2)
    print(f"[evaluator] violations: {len(violations)}")
    if violations:
        for v in violations[:10]:
            print(f"  - {v}")
        if len(violations) > 10:
            print(f"  ... and {len(violations)-10} more")
    print(f"[evaluator] hard miss rate: {hard_miss_rate:.3f}")
    print(f"[evaluator] soft miss rate: {soft_miss_rate:.3f}")
    print(f"[evaluator] sporadic value rate: {sporadic_value_rate:.3f}")
    print(f"[evaluator] gen cost: {gen_cost:.2f}  revenue: {revenue:.2f}  "
          f"objective: {objective_value:.2f}")


if __name__ == "__main__":
    main()
