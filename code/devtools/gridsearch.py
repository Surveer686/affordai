"""Grid-search the reference forecast policy against the 25 sample safe amounts.

For each parameter combo we rebuild projected flows with a lightweight
forecaster and count EXACT matches (|mine - truth| < 0.005). The truth
decimals fingerprint the generator's exact formula.
"""
import csv
import os
import sys
from collections import defaultdict
from datetime import date, timedelta
from statistics import mean, median
from itertools import product

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from data import Request, load_dataset  # noqa: E402
from normalize import normalize_dataset  # noqa: E402

here = os.path.dirname(os.path.abspath(__file__))
ds = load_dataset(os.path.join(here, "..", "dataset"))
views = normalize_dataset(ds, os.path.join(here, "evidence", "blank_amounts.json"))

SAMPLES = []
with open(os.path.join(here, "..", "dataset", "sample_requests.csv"), encoding="utf-8") as fh:
    for row in csv.DictReader(fh):
        r = Request(row["request_id"], row["user_id"],
                    date.fromisoformat(row["request_date"]), row["request_type"],
                    float(row["requested_amount"]),
                    date.fromisoformat(row["desired_completion_date"]),
                    row["allows_partial_payment"] == "true", row["request_text"])
        ds.requests_by_id[r.request_id] = r
        SAMPLES.append((r, float(row["amount_safe_to_pay"] or 0)))


def settled_rows(uid, direction, include_noncash=False):
    out = []
    for ev in ds.events_by_user.get(uid, []):
        if ev.status != "settled" or ev.amount is None:
            continue
        if ev.direction != direction:
            continue
        if ev.event_type == "refund":
            continue
        out.append(ev)
    return out


def month_add(d, k):
    m = d.month - 1 + k
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, 1)


def project(uid, request_date, horizon_end, amount_stat, win, min_rows,
            cadence_from, dedup_days):
    """Light forecaster: flows list of (day, signed_amount, category, series_key)."""
    flows = [(d, amt, eid)
             for d, amt, eid in views[uid].future_flows
             if request_date <= d <= horizon_end]

    streams = defaultdict(list)
    for ev in settled_rows(uid, "debit"):
        streams[(ev.direction, ev.category, "")].append(ev)
    for ev in settled_rows(uid, "credit"):
        streams[(ev.direction, ev.category, (ev.description or "").lower())].append(ev)

    series_list = []
    for (direction, cat, stream), rows in streams.items():
        rows.sort(key=lambda r: r.event_date)
        # cluster
        clusters, cur = [], [rows[0]] if rows else []
        for r in rows[1:]:
            if (r.event_date - cur[-1].event_date).days <= 60:
                cur.append(r)
            else:
                clusters.append(cur)
                cur = [r]
        if cur:
            clusters.append(cur)
        for cl in clusters:
            if len(cl) < min_rows:
                continue
            if (request_date - cl[-1].event_date).days > 45:
                continue
            if direction == "credit" and "final" in (cl[-1].description or "").lower():
                continue
            gaps = [(cl[i + 1].event_date - cl[i].event_date).days
                    for i in range(len(cl) - 1)]
            mg = median(gaps)
            if cadence_from == "median":
                if 5 <= mg <= 9:
                    cad = "weekly"
                elif 10 <= mg <= 22:
                    cad = "biweekly"
                elif 23 <= mg <= 40:
                    cad = "monthly"
                else:
                    continue
            else:  # mode-dom -> monthly only
                if not (23 <= mg <= 40):
                    continue
                cad = "monthly"
            vals = [e.amount for e in cl if e.amount]
            if not vals:
                continue
            per_occ = {"max": max, "min": min, "mean": mean,
                       "median": median, "last": lambda v: v[-1],
                       "mode": lambda v: max(set(v), key=v.count)}[amount_stat](vals)
            if per_occ <= 0:
                continue
            series_list.append((direction, cat, cad, float(per_occ),
                                cl[-1].event_date,
                                sorted(set(r.event_date.day for r in cl))
                                if cad == "monthly" else None, cl))

    for direction, cat, cad, per_occ, last_row, doms, cl in series_list:
        sign = -1.0 if direction == "debit" else 1.0
        if cad == "monthly":
            # occurrences at each observed day-of-month variant
            k = 0
            while True:
                base = month_add(request_date, k)
                k += 1
                occs = [date(base.year, base.month, min(d, 28 if d > 28 else d))
                        if False else
                        date(base.year, base.month,
                             min(d, [31, 29 if base.year % 4 == 0 and (base.year % 100 != 0 or base.year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][base.month - 1]))
                        for d in (doms or [last_row.day])]
                occs = sorted(set(o for o in occs if o >= request_date))
                if not occs:
                    if base > horizon_end:
                        break
                    continue
                if occs[0] > horizon_end:
                    break
                for occ in occs:
                    if occ > horizon_end:
                        continue
                    if dedup_days and any(abs((occ - d).days) <= dedup_days
                                          and abs(a) > 0 == (sign > 0)
                                          for d, a, _ in flows
                                          if (a > 0) == (sign > 0)):
                        continue
                    flows.append((occ, sign * per_occ, f"s:{cat}"))
        else:
            step = 7 if cad == "weekly" else 14
            k = 0
            while True:
                occ = last_row + timedelta(days=step * (k + 1))
                k += 1
                if occ > horizon_end:
                    break
                if occ < request_date:
                    continue
                if dedup_days and any(abs((occ - d).days) <= dedup_days
                                      for d, a, _ in flows if (a > 0) == (sign > 0)):
                    continue
                flows.append((occ, sign * per_occ, f"s:{cat}"))
    return flows


def safe_amount(uid, request_date, horizon_end, min_bal, opening, reserved,
                requested, cap, **kw):
    flows = project(uid, request_date, horizon_end, **kw)
    b0 = opening - reserved
    s = sum(a for d, a, _ in flows if d == request_date)
    worst = s
    for d, a, _ in sorted(flows):
        if d <= request_date:
            continue
        s += a
        worst = min(worst, s)
    return round(max(0.0, min(cap, b0 - min_bal + worst)), 2)


# grid
AMOUNT_STATS = ["max", "min", "mean", "median", "last", "mode"]
WINDOWS = [90, 91, 92]
MIN_ROWS = [2, 3]
CADENCES = ["median", "mode-dom"]
DEDUPS = [0, 3, 5]

results = []
for stat, win, minr, cad, ded in product(AMOUNT_STATS, WINDOWS, MIN_ROWS, CADENCES, DEDUPS):
    exact = 0
    tot_err = 0.0
    for r, truth in SAMPLES:
        uid = r.user_id
        prof = ds.profiles[uid]
        v = views[uid]
        mine = safe_amount(uid, r.request_date,
                           r.request_date + timedelta(days=win),
                           prof.minimum_balance_to_keep, v.opening_balance,
                           v.reserved, r.requested_amount, r.requested_amount,
                           amount_stat=stat, win=win, min_rows=minr,
                           cadence_from=cad, dedup_days=ded)
        if abs(mine - truth) < 0.005:
            exact += 1
        tot_err += abs(mine - truth)
    results.append((exact, tot_err, stat, win, minr, cad, ded))

results.sort(reverse=True)
print("top 12 combos by exact matches:")
for exact, err, stat, win, minr, cad, ded in results[:12]:
    print(f"  exact={exact:2d}/25  tot_err={err:>16,.2f}  stat={stat:6s} win={win} "
          f"min_rows={minr} cadence={cad:8s} dedup={ded}")
