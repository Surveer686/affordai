"""Differential diagnostics: my safe vs truth safe for the 25 samples.

For each sample print:
  * truth safe / my safe / requested
  * headroom gap (truth - mine) - positive means the reference model counts
    MORE available money than we do (we over-subtract or under-add),
    negative means we are too generous.
  * my flow aggregates by source/class for eyeballing.
"""
import csv
import os
import sys
from collections import defaultdict
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from data import Request, load_dataset  # noqa: E402
from normalize import normalize_dataset  # noqa: E402
from recurrence import build_forecast  # noqa: E402
from simulate import max_safe_today  # noqa: E402

here = os.path.dirname(os.path.abspath(__file__))
ds = load_dataset(os.path.join(here, "..", "dataset"))
views = normalize_dataset(ds, os.path.join(here, "evidence", "blank_amounts.json"))

with open(os.path.join(here, "..", "dataset", "sample_requests.csv"), encoding="utf-8") as fh:
    for row in csv.DictReader(fh):
        rid = row["request_id"]
        r = Request(rid, row["user_id"], date.fromisoformat(row["request_date"]),
                    row["request_type"], float(row["requested_amount"]),
                    date.fromisoformat(row["desired_completion_date"]),
                    row["allows_partial_payment"] == "true", row["request_text"])
        ds.requests_by_id[rid] = r
        uid = r.user_id
        prof = ds.profiles[uid]
        v = views[uid]
        f = build_forecast(ds, v, uid, r.request_date)
        truth = float(row["amount_safe_to_pay"] or 0)
        mine = max_safe_today(f, prof.minimum_balance_to_keep, r.requested_amount)
        gap = truth - mine
        flag = "OK " if abs(gap) < 0.01 else ("OVER+" if gap > 0 else "UNDER-")
        print(f"{rid} {uid} {v.home_currency} {flag} truth={truth:>13,.2f} "
              f"mine={mine:>13,.2f} gap={gap:>13,.2f} amt={r.requested_amount:,.0f}")
        if flag != "OK ":
            agg = defaultdict(float)
            for fl in f.flows:
                cls = fl.source.split(":")[0]
                agg[f"{cls}:{fl.category}"] += fl.amount
            for k, amt in sorted(agg.items(), key=lambda kv: kv[1]):
                print(f"      {k:34s} {amt:>14,.2f}")
            if v.reserved:
                print(f"      {'RESERVED (pending debits)':34s} {-v.reserved:>14,.2f}")
