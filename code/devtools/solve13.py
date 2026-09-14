"""Targeted solver: which flow-set variant reproduces user_13's truth exactly?

Truth (request_13): amount_safe_to_pay = 433.40, earliest full payment = 2024-05-15,
plan = wait 2024-05-15:941.60. B0=2789.52, min=1300 -> truth min S(d) = -1056.12.
"""
import os
import sys
from collections import defaultdict
from datetime import date, timedelta
from statistics import median

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from data import load_dataset  # noqa: E402
from normalize import normalize_dataset  # noqa: E402

here = os.path.dirname(os.path.abspath(__file__))
ds = load_dataset(os.path.join(here, "..", "dataset"))
views = normalize_dataset(ds, os.path.join(here, "evidence", "blank_amounts.json"))

uid = "user_13"
request_date = date(2024, 3, 7)
H = 90
horizon_end = request_date + timedelta(days=H)
v = views[uid]
prof = ds.profiles[uid]
B0 = v.opening_balance - v.reserved
MIN = prof.minimum_balance_to_keep
TRUTH_SAFE = 433.40
TRUTH_MIN_S = TRUTH_SAFE - (B0 - MIN)
print(f"B0={B0:,.2f} min={MIN:,.2f} -> truth min S = {TRUTH_MIN_S:,.2f}")

# my flow list (from the engine)
from recurrence import build_forecast
f = build_forecast(ds, v, uid, request_date)
base_flows = [(fl.day, fl.amount, fl.source, fl.category) for fl in f.flows]


def minS(flows):
    s = 0.0
    worst = 0.0
    for d, a, *_ in sorted(flows):
        if d < request_date:
            continue
        s += a
        worst = min(worst, s)
    return worst


def earliest_with(flows, amount):
    # try each day: pay, check path
    days = sorted({d for d, *_ in flows if request_date <= d <= min(date(2024, 5, 15), horizon_end)})
    for D in days:
        pre_ok = True
        s = 0.0
        for d, a, *_ in sorted(flows):
            if d < request_date or d >= D:
                continue
            s += a
            if B0 + s < MIN - 1e-9:
                pre_ok = False
                break
        if not pre_ok:
            continue
        s = -amount
        ok = True
        for d, a, *_ in sorted(flows):
            if d < D or d > horizon_end:
                continue
            s += a
            if B0 + s < MIN - 1e-9:
                ok = False
                break
        if ok:
            return D
    return None


print(f"\nmy min S = {minS(base_flows):,.2f}   my safe = {max(0, B0 - MIN + minS(base_flows)):,.2f}")

# variant knobs
import itertools
cats = sorted({c for *_, c in base_flows})
print("\ncategories:", cats)

# try: drop each single category; drop pairs; also drop weekly/biweekly classes
series_keys = sorted({s.split(":")[1] if s.startswith("series") else "EVENT" for _, _, s, _ in base_flows})
print("series cats:", series_keys)

best = []
for r in range(0, 4):
    for combo in itertools.combinations(series_keys, r):
        flows = [(d, a, s, c) for d, a, s, c in base_flows
                 if not (s.startswith("series") and s.split(":")[1] in combo)]
        # also variant: drop the 'EVENT' salary (no) - skip
        ms = minS(flows)
        safe = max(0.0, B0 - MIN + ms)
        ed = earliest_with(flows, 941.60)
        score = (abs(safe - TRUTH_SAFE) < 0.005) + (ed == date(2024, 5, 15))
        if score:
            best.append((score, safe, ms, ed, combo))
for score, safe, ms, ed, combo in sorted(best, reverse=True)[:10]:
    print(f"score={score} drop={combo} safe={safe:,.2f} minS={ms:,.2f} earliest={ed}")
if not best:
    print("no single/pair/triple category drop reproduces truth")
