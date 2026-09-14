"""Tune the global policy knobs (EXPENSE_STAT, INCOME_STAT, ANCHOR_WDELTA)
by exact-match count across all 25 samples."""
import csv
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import recurrence  # noqa: E402
from data import Request, load_dataset  # noqa: E402
from normalize import normalize_dataset  # noqa: E402
from recurrence import build_forecast  # noqa: E402
from simulate import max_safe_today  # noqa: E402

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

best = []
for stat_e, stat_i, wdelta in [("max", "min", 0), ("max", "min", 1),
                               ("median", "min", 0), ("median", "min", 1),
                               ("median", "median", 0), ("median", "median", 1),
                               ("max", "median", 0), ("max", "median", 1)]:
    recurrence.EXPENSE_STAT = stat_e
    recurrence.INCOME_STAT = stat_i
    recurrence.ANCHOR_WDELTA = wdelta
    exact, sum_abs_rel = 0, 0.0
    for r, truth in SAMPLES:
        prof = ds.profiles[r.user_id]
        f = build_forecast(ds, views[r.user_id], r.user_id, r.request_date)
        mine = max_safe_today(f, prof.minimum_balance_to_keep, r.requested_amount)
        if abs(mine - truth) < 0.005:
            exact += 1
        if truth > 0:
            sum_abs_rel += abs(mine - truth) / truth
    best.append((exact, sum_abs_rel, stat_e, stat_i, wdelta))
    print(f"stat_e={stat_e:6s} stat_i={stat_i:6s} wdelta={wdelta} "
          f"exact={exact:2d}/25 sum_rel_err={sum_abs_rel:.3f}")
best.sort(reverse=True)
print("best:", best[0])
