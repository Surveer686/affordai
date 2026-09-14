"""Exact trough solver for user_13 (truth minS = -1056.12, earliest = May 15).

Prints my cumulative-S profile, then tests hypothesis combos:
  stat for weekly/biweekly (max/median/mean/last), dining cadence,
  second income stream repeated monthly at dom 20 (median/mean/none),
  utilities/rent variants.
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
from recurrence import ForecastFlow, build_forecast  # noqa: E402

here = os.path.dirname(os.path.abspath(__file__))
ds = load_dataset(os.path.join(here, "..", "dataset"))
views = normalize_dataset(ds, os.path.join(here, "evidence", "blank_amounts.json"))

uid, rd = "user_13", date(2024, 3, 7)
HE = rd + timedelta(days=90)
v = views[uid]
MIN = ds.profiles[uid].minimum_balance_to_keep
B0 = v.opening_balance - v.reserved
TRUTH_MIN_S = -1056.12

f = build_forecast(ds, v, uid, rd, evidence_facts=[])
base = [(fl.day, fl.amount, fl.source) for fl in f.flows]


def minS_day(flows):
    s, w, wd = 0.0, 0.0, None
    for d, a, *_ in sorted(flows):
        s += a
        if s < w:
            w, wd = s, d
    return w, wd


def profile(flows):
    out = []
    s = 0.0
    for d, a, *_ in sorted(flows):
        s += a
        out.append((d, s))
    return out


w, wd = minS_day(base)
print(f"base minS={w:,.2f} at {wd}  target={TRUTH_MIN_S:,.2f}  delta={TRUTH_MIN_S-w:,.2f}")
print("S profile (trough window):")
for d, s in profile(base):
    if date(2024, 4, 20) <= d <= date(2024, 5, 25):
        print(f"   {d} S={s:>10,.2f}")

# settled rows for stats
rows = defaultdict(list)
for e in ds.events_by_user[uid]:
    if e.status == "settled" and e.direction == "debit" and e.event_type != "refund":
        rows[e.category].append(e)
for cat in rows:
    rows[cat].sort(key=lambda e: e.event_date)

sal2 = sorted([e.amount for e in ds.events_by_user[uid]
               if e.category == "salary" and "second" in e.description.lower()])
STAT = {"max": max, "min": min, "mean": mean, "median": median,
        "last": lambda x: x[-1]}


def rebuild(stat_w, dining_cad, second_income, doms_policy):
    flows = [(d, a) for d, a, _ in base]
    flows = [f for f in flows if not f[1].__class__ or True]
    # remove projected weekly/biweekly/dining/salary-2 flows: filter by source
    def keep(src):
        if "groceries:weekly" in src or "transport:weekly" in src:
            return False
        if "dining:biweekly" in src:
            return False
        return True
    flows = [(d, a) for d, a, s in base if keep(s)]
    # re-add weekly/biweekly with chosen stat
    for cat, cad, step in [("groceries", "weekly", 7), ("transport", "weekly", 7)]:
        amt = round(float(STAT[stat_w]([e.amount for e in rows[cat]])), 2)
        last = rows[cat][-1].event_date
        k = 0
        while True:
            occ = last + timedelta(days=step * (k + 1))
            k += 1
            if occ > HE:
                break
            if occ >= rd:
                flows.append((occ, -amt))
    if dining_cad == "biweekly":
        amt = round(float(STAT[stat_w]([e.amount for e in rows["dining"]])), 2)
        last = rows["dining"][-1].event_date
        k = 0
        while True:
            occ = last + timedelta(days=14 * (k + 1))
            k += 1
            if occ > HE:
                break
            if occ >= rd:
                flows.append((occ, -amt))
    elif dining_cad == "weekly":
        amt = round(float(STAT[stat_w]([e.amount for e in rows["dining"]])), 2)
        last = rows["dining"][-1].event_date
        k = 0
        while True:
            occ = last + timedelta(days=7 * (k + 1))
            k += 1
            if occ > HE:
                break
            if occ >= rd:
                flows.append((occ, -amt))
    if second_income:
        amt2 = round(float(median(sal2) if second_income == "median" else mean(sal2)), 2)
        for m in (3, 4, 5):
            occ = date(2024, m, 20)
            if rd <= occ <= HE:
                flows.append((occ, amt2))
    return flows


hits = []
for stat_w, dcad, second in product(
        ["max", "median", "mean", "last"],
        ["biweekly", "weekly"],
        [None, "median", "mean"]):
    fl = rebuild(stat_w, dcad, second, None)
    w, wd = minS_day(fl)
    tag = "HIT" if abs(w - TRUTH_MIN_S) < 0.005 else ""
    print(f"stat={stat_w:6s} dining={dcad:8s} second={second or 'none':6s} "
          f"minS={w:>10,.2f} at {wd} safe={max(0, B0-MIN+w):>9,.2f} {tag}")
    if tag:
        hits.append((stat_w, dcad, second))
print("hits:", hits)
