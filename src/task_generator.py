"""
Task Generator for VPP Real-Time Scheduling.

Generates a periodic task set that satisfies all of Level 1's structural
requirements (item 1-1 through 1-8 of the rubric):

  * 6 <= |Jp| <= 10
  * After expansion to a 72-hour horizon, number of periodic jobs > 30
  * 1 <= r_j <= period_j
  * 6 <= period_j <= 24, with at least 3 different period values
  * 1 <= e_j <= 4, at least two tasks with e_j == 2 and at least one with e_j >= 3
  * e_j <= deadline_j <= period_j
  * 6 <= w_j <= 18, at least two tasks with w_j >= 14
  * D_W = sum(e_j / period_j) >= 0.7
  * At least 20% of tasks have deadline_j == e_j
  * At least 2 non-preemptive tasks with e_j != 1
  * Frame size f satisfies f >= max(e_j), 72 mod f == 0,
    2f - gcd(f, period_j) <= deadline_j for every task.

The task set is deterministic (not random) so that the submission is
fully reproducible.
"""

import json
import os
from math import gcd
from functools import reduce

H = 72  # scheduling horizon (hours)

# Hand-tuned task set. Each entry follows the JSON schema described in
# Appendix F of the assignment specification.
PERIODIC_TASKS = {
    "p1": {"r": 1, "p": 6,  "e": 3, "d": 3,  "w": 12, "preempt": 1},
    "p2": {"r": 2, "p": 6,  "e": 2, "d": 6,  "w": 10, "preempt": 1},
    "p3": {"r": 1, "p": 12, "e": 3, "d": 3,  "w": 14, "preempt": 0},
    "p4": {"r": 3, "p": 12, "e": 2, "d": 12, "w": 16, "preempt": 0},
    "p5": {"r": 1, "p": 24, "e": 2, "d": 12, "w": 8,  "preempt": 1},
    "p6": {"r": 2, "p": 24, "e": 3, "d": 24, "w": 18, "preempt": 1},
}


def select_frame_size(tasks, horizon=H):
    """Pick the smallest legal frame size f that satisfies all three rules."""
    max_e = max(t["e"] for t in tasks.values())
    for f in range(max_e, horizon + 1):
        if horizon % f != 0:
            continue
        ok = True
        for t in tasks.values():
            if 2 * f - gcd(f, t["p"]) > t["d"]:
                ok = False
                break
        if ok:
            return f
    raise ValueError("No legal frame size found.")


def validate(tasks, frame):
    """Sanity-check the generated task set against every rubric clause."""
    errs = []
    n = len(tasks)
    if not (6 <= n <= 10):
        errs.append(f"|Jp|={n} not in [6,10]")

    # Expanded job count.
    total_jobs = sum((H - t["r"]) // t["p"] + 1 for t in tasks.values())
    if total_jobs <= 30:
        errs.append(f"expanded jobs={total_jobs} <= 30")

    # Parameter ranges.
    periods = {t["p"] for t in tasks.values()}
    if len(periods) < 3:
        errs.append("fewer than 3 distinct periods")
    e2 = sum(1 for t in tasks.values() if t["e"] == 2)
    e3 = sum(1 for t in tasks.values() if t["e"] >= 3)
    if e2 < 2:
        errs.append(f"e_j==2 count={e2} < 2")
    if e3 < 1:
        errs.append(f"e_j>=3 count={e3} < 1")
    big_w = sum(1 for t in tasks.values() if t["w"] >= 14)
    if big_w < 2:
        errs.append(f"w_j>=14 count={big_w} < 2")
    for k, t in tasks.items():
        if not (1 <= t["r"] <= t["p"]):
            errs.append(f"{k} r out of range")
        if not (6 <= t["p"] <= 24):
            errs.append(f"{k} period out of range")
        if not (1 <= t["e"] <= 4):
            errs.append(f"{k} e out of range")
        if not (t["e"] <= t["d"] <= t["p"]):
            errs.append(f"{k} deadline out of range")
        if not (6 <= t["w"] <= 18):
            errs.append(f"{k} w out of range")

    # Workload density.
    dw = sum(t["e"] / t["p"] for t in tasks.values())
    if dw < 0.7:
        errs.append(f"D_W={dw:.3f} < 0.7")

    # d == e ratio.
    dem = sum(1 for t in tasks.values() if t["d"] == t["e"])
    if dem / n < 0.2:
        errs.append(f"deadline==e ratio={dem/n:.2f} < 0.2")

    # Non-preemptive count.
    npc = sum(1 for t in tasks.values() if t["preempt"] == 0 and t["e"] != 1)
    if npc < 2:
        errs.append(f"non-preempt(e!=1) count={npc} < 2")

    # Frame size.
    max_e = max(t["e"] for t in tasks.values())
    if frame < max_e:
        errs.append("f < max(e)")
    if H % frame != 0:
        errs.append("H mod f != 0")
    for k, t in tasks.items():
        if 2 * frame - gcd(frame, t["p"]) > t["d"]:
            errs.append(f"{k} violates 2f-gcd(f,p) <= d")

    return errs, {"D_W": dw, "expanded_jobs": total_jobs}


def expand_jobs(tasks, horizon=H):
    """Expand each periodic task into its concrete job instances."""
    jobs = []
    for tid, t in tasks.items():
        k = 0
        while True:
            release = t["r"] + k * t["p"]
            if release > horizon:
                break
            jobs.append({
                "job_id": f"{tid}_j{k+1}",
                "task_id": tid,
                "release_time": release,
                "execution_time": t["e"],
                "relative_deadline": t["d"],
                "absolute_deadline": release + t["d"] - 1,
                "energy_demand": t["w"],
                "preemptive": t["preempt"],
            })
            k += 1
    return jobs


def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(here, "output")
    os.makedirs(out_dir, exist_ok=True)

    frame = select_frame_size(PERIODIC_TASKS)
    errs, stats = validate(PERIODIC_TASKS, frame)
    if errs:
        raise SystemExit("Task set validation failed:\n  - " + "\n  - ".join(errs))

    expanded = expand_jobs(PERIODIC_TASKS)

    task_set = {
        "periodic": PERIODIC_TASKS,
        "frame_size": frame,
        "horizon": H,
        "workload_density": round(stats["D_W"], 4),
        "expanded_job_count": stats["expanded_jobs"],
        "expanded_jobs": expanded,
    }
    out = os.path.join(out_dir, "task_set.json")
    with open(out, "w") as f:
        json.dump(task_set, f, indent=2)
    print(f"[task_generator] frame={frame}  D_W={stats['D_W']:.3f}  "
          f"|Jp|={len(PERIODIC_TASKS)}  expanded_jobs={stats['expanded_jobs']}")
    print(f"[task_generator] wrote {out}")


if __name__ == "__main__":
    main()
