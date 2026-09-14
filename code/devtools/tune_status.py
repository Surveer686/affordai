"""Score STATUS + METHOD agreement (not just exact amounts) per knob combo."""
import csv
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import recurrence  # noqa: E402
from data import Request, load_dataset  # noqa: E402
from decision import decide  # noqa: E402
from normalize import normalize_dataset  # noqa: E402
from recurrence import build_forecast  # noqa: E402

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
        SAMPLES.append((r, row))

for stat_e, stat_i, wdelta in [("max", "min", 0), ("max", "min", 1),
                               ("median", "min", 0), ("median", "min", 1),
                               ("median", "median", 0), ("max", "median", 0)]:
    recurrence.EXPENSE_STAT = stat_e
    recurrence.INCOME_STAT = stat_i
    recurrence.ANCHOR_WDELTA = wdelta
    st_ok = m_ok = 0
    for r, row in SAMPLES:
        f = build_forecast(ds, views[r.user_id], r.user_id, r.request_date)
        out = decide(ds, f, r)
        if out["affordability_status"] == row["affordability_status"]:
            st_ok += 1
        if out["recommended_payment_method"] == row["recommended_payment_method"]:
            m_ok += 1
    print(f"stat_e={stat_e:6s} stat_i={stat_i:6s} wd={wdelta} "
          f"status={st_ok:2d}/25 method={m_ok:2d}/25")
