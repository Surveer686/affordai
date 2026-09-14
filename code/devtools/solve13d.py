"""Broad exact search for user_13 flow policy: target minS = -1056.12.

Knobs: per-cadence amount statistic (all-history or last-3), monthly smalls
in/out, dining cadence, utilities statistic. Checks exact trough and earliest.
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

uid, rd = "user_13", date(2024, 3, 7)
HE = rd + timedelta(days=90)
v = views[uid]
MIN = ds.profiles[uid].minimum_balance_to_keep
B0 = v.opening_balance - v.reserved
TRUTH_MIN_S = -1056.12
AMT, DEADLINE = 941.60, date(2024, 5, 15)

rows = defaultdict(list)
for e in ds.events_by_user[uid]:
    if e.status == "settled" and e.direction == "debit" and e.event_type != "refund":
        rows[e.category].append(e)
for c in rows:
    rows[c].sort(key=lambda e: e.event_date)

STAT = {"max": max, "min": min, "mean": mean, "median": median,
        "last": lambda x: x[-1], "last3mean": lambda x: mean(x[-3:]),
        "last3med": lambda x: median(x[-3:])}


def project(cat, stat, cadence, wdelta=0):
    vals = [e.amount for e in rows[cat]]
    amt = round(float(STAT[stat](vals)), 2)
    last = rows[cat][-1].event_date
    out = []
    if cadence == "monthly":
        doms = sorted({e.event_date.day for e in rows[cat]})
        for k in range(4):
            m = rd.month - 1 + k
            y = rd.year + m // 12
            m = m % 12 + 1
            for d in doms:
                import calendar
                dd = min(d, calendar.monthrange(y, m)[1])
                occ = date(y, m, dd)
                if rd <= occ <= HE:
                    out.append((occ, -amt))
    else:
        step = 7 if cadence == "weekly" else 14
        k = 0
        while True:
            occ = last + timedelta(days=step * (k + 1) + wdelta)
            k += 1
            if occ > HE:
                break
            if occ >= rd:
                out.append((occ, -amt))
    return out


SALARY1 = 1343.54


def build(stat_var, stat_fix, dining_cad, smalls, wdelta):
    flows = [(date(2024, 3, 15), SALARY1)]
    for k in (1, 2, 3):
        flows.append((date(2024, 3 + k, 15), SALARY1))
    for cat in ("groceries", "transport"):
        flows += project(cat, stat_var, "weekly", wdelta)
    flows += project("dining", stat_var, dining_cad, wdelta)
    for cat in ("rent", "utilities", "gym", "music_subscription",
                "delivery_membership", "entertainment"):
        if cat in ("gym", "music_subscription", "delivery_membership",
                   "entertainment") and not smalls:
            continue
        flows += project(cat, stat_fix, "monthly")
    return flows


def minS_day(flows):
    s, w, wd = 0.0, 0.0, None
    for d, a in sorted(flows):
        s += a
        if s < w:
            w, wd = s, d
    return w, wd


def earliest(flows):
    for D in sorted({d for d, _ in flows} | {rd}):
        if D > min(DEADLINE, HE):
            break
        s = 0.0
        for d, a in sorted(flows):
            if rd <= d < D:
                s += a
        if B0 + s < MIN - 1e-9:
            continue
        s = -AMT
        ok = True
        for d, a in sorted(flows):
            if D <= d <= HE:
                s += a
                if B0 + s < MIN - 1e-9:
                    ok = False
                    break
        if ok:
            return D
    return None


hits = []
for stat_var, stat_fix, dcad, smalls, wdelta in product(
        list(STAT.keys()), list(STAT.keys()),
        ["biweekly", "weekly"], [True, False], [0, 1]):
    flows = build(stat_var, stat_fix, dcad, smalls, wdelta)
    w, wd = minS_day(flows)
    if abs(w - TRUTH_MIN_S) < 0.005:
        e = earliest(flows)
        hits.append((stat_var, stat_fix, dcad, smalls, wdelta, w, e))
        print(f"HIT stat_var={stat_var} stat_fix={stat_fix} dining={dcad} "
              f"smalls={smalls} wdelta={wdelta} minS={w:,.2f} earliest={e}")
if not hits:
    print("no hits in this space")
    # show closest few
    best = []
    for stat_var, stat_fix, dcad, smalls, wdelta in product(
            list(STAT.keys()), list(STAT.keys()),
            ["biweekly", "weekly"], [True, False], [0, 1]):
        flows = build(stat_var, stat_fix, dcad, smalls, wdelta)
        w, wd = minS_day(flows)
        best.append((abs(w - TRUTH_MIN_S), stat_var, stat_fix, dcad, smalls, wdelta, w))
    best.sort()
    for d, sv, sf, dc, sm, wd_, w in best[:10]:
        print(f"  close: |d|={d:>9,.2f} stat_var={sv:9s} stat_fix={sf:9s} "
              f"dining={dc:8s} smalls={sm} wdelta={wd_} minS={w:>10,.2f}")
