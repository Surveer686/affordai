"""Stage 9 - full run: produce output.csv for all 250 requests.

Writes the root output.csv (template header preserved), then validates it.
"""
from __future__ import annotations

import csv
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from data import load_dataset  # noqa: E402
from decision import decide  # noqa: E402
from normalize import normalize_dataset  # noqa: E402
from recurrence import build_forecast  # noqa: E402
from validate import COLUMNS, validate  # noqa: E402


def run(dataset_dir: str, out_path: str) -> None:
    ds = load_dataset(dataset_dir)
    views = normalize_dataset(ds, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                               "evidence", "blank_amounts.json"))
    rows = []
    from collections import Counter
    st = Counter()
    for r in ds.requests:  # preserve requests.csv order (request_26..request_275)
        f = build_forecast(ds, views[r.user_id], r.user_id, r.request_date)
        out = decide(ds, f, r)
        rows.append([out[c] for c in COLUMNS])
        st[out["affordability_status"]] += 1
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        w.writerows(rows)
    print(f"wrote {len(rows)} rows -> {out_path}")
    print("status distribution:", dict(st))

    errs = validate(out_path, dataset_dir)
    if errs:
        print(f"VALIDATION FAILED: {len(errs)} violations")
        for e in errs[:30]:
            print("  -", e)
        sys.exit(1)
    print("validation: OK")


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    ds_dir = os.path.join(here, "..", "dataset")
    out = os.path.join(here, "..", "output.csv")
    run(ds_dir, out)
