"""Exact policy search for user_13: reproduce safe=433.40 AND earliest=2024-05-15.

Enumerates uniform policy axes over the user's real settled rows and simulates
the balance path. Prints every combo matching either target.
"""
import os
import sys
from collections import defaultdict
from datetime import date, timedelta
from itertools import product
from statistics import mean, median

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from data import load_dataset  # noqa: E402
from normalize import normalize_dataset  # noqa: E402

here = os.path.dirname(os.path.abspath(__file__))
ds = load_dataset(os.path.join(here, "..", "dataset"))
views = normalize_dataset(ds, os.path.join(here, "evidence", "blank_amounts.json"))

uid = "user_13"
rd = date(2024, 3, 7)
v = views[uid]
MIN = ds.profiles[uid].minimum_balance_to_keep
B0 = v.opening_balance - v.reserved
TRUTH_SAFE, TRUTH_EARLIEST = 433.40, date(2024, 5, 15)
AMT, DEADLINE = 941.60, date(2024, 5, 15)

rows = sorted([e for e in ds.events_by_user[uid]
               if e.status == "settled" and e.amount and e.direction == "debit"
               and e.event_type != "refund"], key=lambda e: e.event_date)

streams = defaultdict(list)
for e in rows:
    streams[e.category].append(e)

STATS = {"max": max, "min": min, "mean": mean, "median": median,
         "last": lambda x: x[-1]}


def occurrences(cat, cl, cadence, dom_policy, stat_name, weekly_delta):
    vals = [e.amount for e in cl]
    amt = round(float(STATS[stat_name](vals)), 2)
    last = cl[-1].event_date
    out = []
    if cadence == "monthly":
        doms = sorted({e.event_date.day for e in cl})
        if dom_policy == "first":
            doms = [doms[0]]
        elif dom_policy == "last":
            doms = [doms[-1]]
        for k in range(4):
            m = rd.month - 1 + k
            y = rd.year + m // 12
            m = m % 12 + 1
            for d in doms:
                dd = min(d, [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
                         if y % 4 == 0 else
                         [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
                occ = date(y, m, dd)
                if rd <= occ <= rd + timedelta(days=90):
                    out.append((occ, -amt))
    elif cadence == "weekly":
        for k in range(1, 14):
            occ = last + timedelta(days=7 * k + weekly_delta)
            if rd <= occ <= rd + timedelta(days=90):
                out.append((occ, -amt))
    elif cadence == "biweekly":
        for k in range(1, 8):
            occ = last + timedelta(days=14 * k + weekly_delta)
            if rd <= occ <= rd + timedelta(days=90):
                out.append((occ, -amt))
    return out


def build(stat_name, dom_policy, weekly_delta, dining_cad, salary_mode,
          horizon):
    flows = []
    he = rd + timedelta(days=horizon)
    # salary: event on 2024-03-15 (+ repeat monthly if salary_mode == 'repeat')
    flows.append((date(2024, 3, 15), 1343.54))
    if salary_mode == "repeat":
        for k in (1, 2, 3):
            m = 3 + k
            flows.append((date(2024, m, 15), 1343.54))
    for cat, cl in streams.items():
        gaps = [(cl[i + 1].event_date - cl[i].event_date).days
                for i in range(len(cl) - 1)]
        mg = median(gaps)
        cad = "monthly" if 23 <= mg <= 40 else ("weekly" if 5 <= mg <= 9 else
                                                "biweekly" if 10 <= mg <= 22 else None)
        if cad is None:
            continue
        if cat == "dining" and dining_cad:
            cad = dining_cad
        flows.extend(occurrences(cat, cl, cad, dom_policy, stat_name, weekly_delta))
    return [f for f in flows if f[0] <= he]


def evaluate(flows):
    s, worst = 0.0, 0.0
    for d, a in sorted(flows):
        s += a
        worst = min(worst, s)
    safe = max(0.0, min(AMT, B0 - MIN + worst))
    # earliest for AMT
    days = sorted({d for d, _ in flows} | {rd})
    earliest = None
    for D in [d for d in days if d <= min(DEADLINE, rd + timedelta(days=90))]:
        s = 0.0
        ok = True
        for d, a in sorted(flows):
            if rd <= d < D:
                s += a
        if B0 + s < MIN - 1e-9:
            ok = False
        s = -AMT
        for d, a in sorted(flows):
            if D <= d <= rd + timedelta(days=90):
                s += a
                if B0 + s < MIN - 1e-9:
                    ok = False
                    break
        if ok:
            earliest = D
            break
    return safe, earliest


hits = []
for stat, domp, wdelta, dcad, smode, hz in product(
        ["max", "min", "mean", "median", "last"],
        ["all", "first", "last"],
        [0, 1], [None, "monthly", "weekly"],
        ["once", "repeat"], [90, 91]):
    flows = build(stat, domp, wdelta, dcad, smode, hz)
    safe, earliest = evaluate(flows)
    if abs(safe - TRUTH_SAFE) < 0.005 or earliest == TRUTH_EARLIEST:
        hits.append((abs(safe - TRUTH_SAFE) < 0.005, earliest == TRUTH_EARLIEST,
                     safe, earliest, stat, domp, wdelta, dcad, smode, hz))

print(f"combos hitting safe-target or earliest-target: {len(hits)}")
both = [h for h in hits if h[0] and h[1]]
print(f"combos hitting BOTH: {len(both)}")
for h in sorted(hits, key=lambda x: (not (x[0] and x[1]), x[2] - TRUTH_SAFE))[:20]:
    print(f"  safe={h[2]:>8,.2f} earliest={h[3]} safeOK={h[0]} earliestOK={h[1]} | "
          f"stat={h[4]} dom={h[5]} wdelta={h[6]} dining={h[7]} salary={h[8]} hz={h[9]}")
