"""
VPP Real-Time Scheduler.

Reads:
  output/task_set.json
  input/processor_settings.json
  input/price_72hr.json
  input/demo_jobs.json

Produces:
  output/schedule_result.json
  output/acceptance_test_log.json

Pipeline
========
Phase 1 - Periodic placement (EDF inside frames).
Phase 2 - Day-ahead processor dispatch (greedy, ramp/UT/DT aware).
Phase 3 - Sporadic acceptance test (hard deadline).
Phase 4 - Aperiodic best-effort placement (soft deadline).
Phase 5 - Job-to-processor allocation post-pass (constraint 20 aware).

Supports multiple thermal units, multiple PV units, and multiple
storages.  Batteries appear in two roles:
  * as supply via discharge (P[bat][t]),
  * as demand via the explicit charging jobs listed under
    `charging_jobs` in processor_settings.json.  Constraint 21 means
    only generators and renewables may feed a charging job.
"""

import json
import os
from collections import defaultdict

H = 72  # horizon


# --------------------------------------------------------------------------- #
# I/O helpers
# --------------------------------------------------------------------------- #
def load_json(path):
    with open(path) as f:
        return json.load(f)


def write_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


def parse_forecast(settings):
    """Normalize the renewable_forecast block into {rid: [v0..v71]}."""
    raw = settings["renewable_forecast"]
    forecast = {}
    # The spec showcases this as `{"pv_1": [...]}` keyed by id, while the
    # processor settings file uses a list-of-single-key dicts whose value
    # is an array of {hour, pv_forecast} entries.  Handle both.
    if isinstance(raw, dict):
        for rid, vals in raw.items():
            if vals and isinstance(vals[0], dict):
                forecast[rid] = [v["pv_forecast"] for v in
                                 sorted(vals, key=lambda x: x["hour"])]
            else:
                forecast[rid] = list(vals)
    elif isinstance(raw, list):
        for entry in raw:
            for rid, vals in entry.items():
                forecast[rid] = [v["pv_forecast"] for v in
                                 sorted(vals, key=lambda x: x["hour"])]
    return forecast


# --------------------------------------------------------------------------- #
# Phase 1: periodic placement
# --------------------------------------------------------------------------- #
def place_periodic(jobs):
    """Return dict job_id -> sorted list of execution hours (EDF)."""
    schedule = {}
    for j in sorted(jobs, key=lambda x: (x["absolute_deadline"], x["release_time"])):
        r, e, ad = j["release_time"], j["execution_time"], j["absolute_deadline"]
        if j["preemptive"]:
            hours = list(range(r, ad + 1))[:e]
        else:
            hours = list(range(r, r + e))
        if len(hours) != e or hours[-1] > ad:
            raise RuntimeError(f"Cannot place periodic job {j['job_id']}.")
        schedule[j["job_id"]] = hours
    return schedule


# --------------------------------------------------------------------------- #
# Phase 2: dispatch state
# --------------------------------------------------------------------------- #
class Dispatch:
    """Mutable hour-by-hour dispatch state.

    The dispatch is recomputed from scratch on every call to
    `dispatch(start)`; the only thing that survives across calls is
    `self.demand`, which is the running record of how much MWh each
    job consumes in each hour (set via `commit_job`).
    """

    SELL_PRICE_THRESHOLD = 60   # sell surplus when price >= this
    CHEAP_PRICE_THRESHOLD = 35  # opportunistically charge when price < this

    def __init__(self, settings, prices, charging_jobs):
        self.settings = settings
        self.prices = prices

        self.gens = {g["generator_id"]: g for g in settings["generator"]}
        self.renew = {r["renewable_id"]: r for r in settings["renewable_capacity"]}
        self.batts = {b["storage_id"]: b for b in settings["storage"]}
        self.forecast = parse_forecast(settings)
        self.charging_jobs = {c["job_id"]: c for c in charging_jobs}

        self.processors = list(self.gens) + list(self.renew) + list(self.batts)

        self.P = {i: [0.0] * H for i in self.processors}
        # charge[bid][hour-1][src_id] -> MWh fed into bid at that hour
        self.charge_flow = {b: [defaultdict(float) for _ in range(H)]
                            for b in self.batts}
        self.sell = [0.0] * H
        self.soc = {b: [self.batts[b]["soc_init"]] + [0.0] * H for b in self.batts}
        # Commitment.
        self.on = {gid: [g["initial_on_time"] > 0] + [False] * H
                   for gid, g in self.gens.items()}
        # k[job_id][hour-1][processor_id] -> MWh
        self.k = defaultdict(lambda: [defaultdict(float) for _ in range(H)])
        # Demand book: per-job execution hours and w.
        self.job_hours = {}     # job_id -> list[int]
        self.job_w = {}         # job_id -> MWh per hour
        # `demand[t-1]` = sum of w over jobs (excluding charging) running at t.
        self.demand = [0.0] * H

    # ----- demand registration -------------------------------------------- #
    def commit_job(self, job_id, hours, w):
        self.job_hours[job_id] = list(hours)
        self.job_w[job_id] = w
        for t in hours:
            self.demand[t - 1] += w

    # ----- commitment ----------------------------------------------------- #
    def commit_thermals(self):
        """Keep all thermals on for the entire horizon.

        With initial_off_time = 99 >= DT for both units, both are free
        to start at t = 1.  Keeping them online for the whole window is
        the simplest commitment that obviously satisfies UT/DT.
        """
        for gid in self.gens:
            for t in range(1, H + 1):
                self.on[gid][t] = True

    # ----- per-hour dispatch ---------------------------------------------- #
    def dispatch(self):
        """Full recomputation across t = 1..H."""
        for i in self.processors:
            self.P[i] = [0.0] * H
        for bid in self.batts:
            self.charge_flow[bid] = [defaultdict(float) for _ in range(H)]
            self.soc[bid] = [self.batts[bid]["soc_init"]] + [0.0] * H
        self.sell = [0.0] * H

        # Sorted thermal lists: by variable cost (ascending) for filling
        # demand, by variable cost (descending) for shedding surplus.
        cheap = sorted(self.gens, key=lambda g: self.gens[g]["cost_variable"])
        expensive = list(reversed(cheap))

        prev_P = {gid: g.get("initial_energy", 0) for gid, g in self.gens.items()}

        for t in range(1, H + 1):
            idx = t - 1
            price = self.prices[idx]

            # PV: always at forecast (free).
            pv_total = 0.0
            for rid, r in self.renew.items():
                v = r["capacity"] * self.forecast[rid][idx]
                self.P[rid][idx] = v
                pv_total += v

            # Thermal min envelopes (ramp + bounds).
            ranges = {}
            for gid, g in self.gens.items():
                lo = max(g["output_min"], prev_P[gid] - g["ramp_down_rate"])
                hi = min(g["output_max"], prev_P[gid] + g["ramp_up_rate"])
                if not self.on[gid][t - 1]:  # transitioning from off to on
                    lo = g["output_min"]
                    hi = min(g["output_max"], g["ramp_up_rate"])
                if hi < lo:  # ramp limit forced upper below min - infeasible
                    hi = lo
                ranges[gid] = [lo, hi]

            # Start each at its lower bound.
            therm = {gid: r[0] for gid, r in ranges.items()}

            demand = self.demand[idx]
            supply = pv_total + sum(therm.values())

            # ---- Cover demand with extra thermal / battery discharge ---- #
            if supply < demand:
                shortfall = demand - supply
                for gid in cheap:
                    if shortfall <= 1e-9:
                        break
                    add = min(shortfall, ranges[gid][1] - therm[gid])
                    therm[gid] += add
                    supply += add
                    shortfall -= add
                # Battery discharge if still short.
                if shortfall > 1e-9:
                    for bid, b in self.batts.items():
                        if shortfall <= 1e-9:
                            break
                        avail = min(b["discharge_max"],
                                    self.soc[bid][t - 1] - b["soc_min"])
                        avail = max(0.0, avail)
                        take = min(shortfall, avail)
                        self.P[bid][idx] = take
                        shortfall -= take
                if shortfall > 1e-3:
                    raise RuntimeError(
                        f"Infeasible at t={t}: shortfall {shortfall:.3f} MWh")
            # ---- Surplus / arbitrage ----------------------------------- #
            surplus = supply - demand

            # Reduce expensive thermal if surplus exists and price is low.
            if surplus > 0 and price < self.SELL_PRICE_THRESHOLD:
                for gid in expensive:
                    if surplus <= 1e-9:
                        break
                    # cannot go below ranges[gid][0]
                    reducible = therm[gid] - ranges[gid][0]
                    cut = min(surplus, reducible)
                    therm[gid] -= cut
                    surplus -= cut

            # Charge batteries when price is cheap.
            charge_total = 0.0
            if surplus > 0 and price < self.SELL_PRICE_THRESHOLD:
                for bid, b in self.batts.items():
                    if surplus <= 1e-9:
                        break
                    room = min(b["charge_max"], b["soc_max"] - self.soc[bid][t - 1])
                    room = max(0.0, room)
                    take = min(surplus, room)
                    self.charge_flow[bid][idx]["__pending__"] = take
                    charge_total += take
                    surplus -= take

            # Whatever surplus remains is sold (only if price > 0; the
            # alternative is to push thermal lower, but we already did).
            sell_amt = max(0.0, surplus)

            # If price is high, push cheapest thermal higher to sell more.
            if price >= self.SELL_PRICE_THRESHOLD:
                for gid in cheap:
                    g = self.gens[gid]
                    extra = ranges[gid][1] - therm[gid]
                    if extra <= 0:
                        continue
                    # Only worth it if price covers variable cost.
                    if price > g["cost_variable"]:
                        therm[gid] += extra
                        sell_amt += extra

            # Commit values.
            for gid in self.gens:
                self.P[gid][idx] = therm[gid]
                prev_P[gid] = therm[gid]
            self.sell[idx] = sell_amt
            # SOC update.
            for bid in self.batts:
                chg = self.charge_flow[bid][idx].get("__pending__", 0)
                self.soc[bid][t] = self.soc[bid][t - 1] + chg - self.P[bid][idx]

    # ----- post-pass k & charge allocation -------------------------------- #
    def allocate_k(self):
        """Assign each job's MWh to specific processors per hour.

        Priority for jobs:   PV -> cheap thermal -> expensive thermal -> battery
        Priority for charge: PV -> cheap thermal -> expensive thermal  (no battery)

        Constraint 20 (battery output only supplies external loads,
        never charging) is enforced because charging never draws from
        a battery.
        """
        cheap = sorted(self.gens, key=lambda g: self.gens[g]["cost_variable"])
        order_supply_job = list(self.renew) + cheap + list(self.batts)
        order_supply_chg = list(self.renew) + cheap  # constraint 21
        remain = {i: list(self.P[i]) for i in self.processors}

        # 1. Jobs (excluding charging - they are handled separately).
        for jid, hours in self.job_hours.items():
            w = self.job_w[jid]
            for t in hours:
                idx = t - 1
                need = w
                for i in order_supply_job:
                    if need <= 1e-9:
                        break
                    avail = remain[i][idx]
                    if avail <= 1e-9:
                        continue
                    take = min(need, avail)
                    self.k[jid][idx][i] = take
                    remain[i][idx] -= take
                    need -= take
                if need > 1e-6:
                    raise RuntimeError(
                        f"Cannot allocate {need} MWh for job {jid} at t={t}")

        # 2. Charging.
        for bid in self.batts:
            for idx in range(H):
                pending = self.charge_flow[bid][idx].pop("__pending__", 0)
                if pending <= 1e-9:
                    continue
                need = pending
                for i in order_supply_chg:
                    if need <= 1e-9:
                        break
                    avail = remain[i][idx]
                    if avail <= 1e-9:
                        continue
                    take = min(need, avail)
                    self.charge_flow[bid][idx][i] = take
                    remain[i][idx] -= take
                    need -= take
                # If something couldn't be charged from gen/PV (shouldn't
                # happen with our greedy), revise SOC to keep balance.
                if need > 1e-6:
                    # Refund the un-routable portion: SOC stays lower.
                    t = idx + 1
                    self.soc[bid][t] -= need

        # 3. Reconcile sell with remaining (leftover) supply.
        for idx in range(H):
            leftover = sum(remain[i][idx] for i in self.renew) + \
                       sum(remain[i][idx] for i in self.gens)
            # Battery cannot sell either (constraint 20 implies battery
            # discharge must supply external loads, not market).  So
            # leftover battery discharge is forced to zero by allocate_k
            # never going above what's needed.  Sell is simply the
            # gen/renewable surplus.
            self.sell[idx] = leftover

    # ----- emit schedule_result.json ------------------------------------- #
    def to_schedule(self):
        result = []
        for t in range(1, H + 1):
            idx = t - 1
            row = {
                "t": t,
                "P": {i: round(self.P[i][idx], 4) for i in self.processors},
                "k": {},
                "sell": round(self.sell[idx], 4),
                "soc": {bid: round(self.soc[bid][t], 4) for bid in self.batts},
                "missed_aperiodic": [],
                "rejected_sporadic": [],
            }
            for jid, hours in self.job_hours.items():
                if t in hours:
                    alloc = {i: round(v, 4) for i, v in self.k[jid][idx].items()
                             if v > 1e-6}
                    if alloc:
                        row["k"][jid] = alloc
            for bid, c in self.charging_jobs.items():
                flow = {i: round(v, 4) for i, v in self.charge_flow[c["target_storage"]][idx].items()
                        if v > 1e-6}
                if flow:
                    row["k"][bid] = flow
            result.append(row)
        return result


# --------------------------------------------------------------------------- #
# Phase 3: sporadic acceptance test
# --------------------------------------------------------------------------- #
def hour_spare_capacity(disp, t):
    """Conservative upper bound of extra MWh available at hour t."""
    idx = t - 1
    room = 0.0
    for rid, r in disp.renew.items():
        room += max(0, r["capacity"] * disp.forecast[rid][idx] - disp.P[rid][idx])
    for gid, g in disp.gens.items():
        room += max(0, g["output_max"] - disp.P[gid][idx])
    for bid, b in disp.batts.items():
        room += max(0, min(b["discharge_max"],
                           disp.soc[bid][t - 1] - b["soc_min"])
                   - disp.P[bid][idx])
    return room


def acceptance_test(disp, sporadics):
    log = []
    for s in sporadics:
        r, e, d, w = s["r"], s["e"], s["d"], s["w"]
        chosen = None
        for start in range(r, r + d - e + 1):
            block = list(range(start, start + e))
            if all(hour_spare_capacity(disp, t) >= w for t in block):
                chosen = block
                break
        if chosen:
            disp.commit_job(s["job_id"], chosen, w)
            disp.dispatch()
            log.append({"job_id": s["job_id"], "decision": "accept",
                        "scheduled_hours": chosen, "w": w,
                        "reason": "sufficient slack in window"})
        else:
            log.append({"job_id": s["job_id"], "decision": "reject",
                        "w": w, "window": [r, r + d - 1],
                        "reason": "no e consecutive hours with enough capacity"})
    return log


# --------------------------------------------------------------------------- #
# Phase 4: aperiodic placement (soft deadline)
# --------------------------------------------------------------------------- #
def place_aperiodic(disp, aperiodics):
    misses = {}
    for a in sorted(aperiodics, key=lambda x: x["r"]):
        r, e, d, w = a["r"], a["e"], a["d"], a["w"]
        soft_dl = r + d - 1
        chosen, t = [], r
        while t <= H and len(chosen) < e:
            if hour_spare_capacity(disp, t) >= w:
                chosen.append(t)
            t += 1
        if len(chosen) < e:
            misses[a["job_id"]] = {"reason": "no capacity"}
            continue
        disp.commit_job(a["job_id"], chosen, w)
        disp.dispatch()
        completion = chosen[-1]
        if completion > soft_dl:
            misses[a["job_id"]] = {"completion_time": completion,
                                   "soft_deadline": soft_dl,
                                   "tardiness": completion - soft_dl}
    return misses


# --------------------------------------------------------------------------- #
# Annotate per-hour rows with missed_aperiodic / rejected_sporadic
# --------------------------------------------------------------------------- #
def annotate(rows, aperiodic_misses, acceptance_log):
    for jid, info in aperiodic_misses.items():
        t = info.get("completion_time", H)
        rows[t - 1]["missed_aperiodic"].append(jid)
    for entry in acceptance_log:
        if entry["decision"] == "reject":
            # Tag the rejection at the release hour (window[0]).
            t = entry["window"][0]
            rows[t - 1]["rejected_sporadic"].append(entry["job_id"])


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    inp = os.path.join(here, "input")
    out = os.path.join(here, "output")

    task_set = load_json(os.path.join(out, "task_set.json"))
    settings = load_json(os.path.join(inp, "processor_settings.json"))
    price_data = load_json(os.path.join(inp, "price_72hr.json"))
    demo = load_json(os.path.join(inp, "demo_jobs.json"))

    prices = [p["market_price"] for p in price_data["price"]]
    charging_jobs = settings.get("charging_jobs", [])

    # Phase 1.
    periodic_schedule = place_periodic(task_set["expanded_jobs"])

    disp = Dispatch(settings, prices, charging_jobs)
    disp.commit_thermals()
    for j in task_set["expanded_jobs"]:
        disp.commit_job(j["job_id"], periodic_schedule[j["job_id"]],
                        j["energy_demand"])

    # Phase 2.
    disp.dispatch()

    # Phase 3.
    acceptance_log = acceptance_test(disp, demo["sporadic"])

    # Phase 4.
    aperiodic_misses = place_aperiodic(disp, demo["aperiodic"])

    # Phase 5.
    disp.allocate_k()

    rows = disp.to_schedule()
    annotate(rows, aperiodic_misses, acceptance_log)

    accepted = [e for e in acceptance_log if e["decision"] == "accept"]
    rejected = [e["job_id"] for e in acceptance_log if e["decision"] == "reject"]

    summary = {
        "schedule_result": rows,
        "periodic_schedule": periodic_schedule,
        "accepted_sporadic": accepted,
        "rejected_sporadic_jobs": rejected,
        "aperiodic_misses": aperiodic_misses,
    }
    write_json(os.path.join(out, "schedule_result.json"), summary)
    write_json(os.path.join(out, "acceptance_test_log.json"),
               {"log": acceptance_log})

    print(f"[scheduler] periodic jobs: {len(periodic_schedule)}")
    print(f"[scheduler] sporadic accepted: {len(accepted)} / "
          f"{len(demo['sporadic'])}")
    print(f"[scheduler] aperiodic misses: {len(aperiodic_misses)} / "
          f"{len(demo['aperiodic'])}")


if __name__ == "__main__":
    main()
