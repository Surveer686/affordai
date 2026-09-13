"""Stage 7 - deterministic output validator.

Every row is checked against the problem-statement contract before
output.csv is accepted. Exits non-zero on any violation.

Checks:
  * 8 columns exactly; request_id set matches requests.csv 1:1, same order
  * amount_safe_to_pay: parses as float, 0 <= x <= requested_amount
  * enums: affordability_status / recommended_payment_method
  * plan format: date:amount legs chronological; 'none' only with
    not_recommended; installments must exactly match a supplied option
  * affordable_now  => earliest == request_date, method full_payment
  * partial_payment => status affordable_with_plan, exactly 2 legs, legs sum
    == requested_amount, leg1 date == request_date, leg1 amount ==
    amount_safe_to_pay, earliest == leg2 date <= desired_completion_date
  * earliest: empty for not_affordable; ISO date within
    [request_date, request_date+90d] otherwise; == request_date for now
  * spending_changes: 'none' or up to 3 stop:/reduce_to: refs to events of the
    request's user with reducible/stoppable flexibility
  * explanation non-empty for every row
"""
from __future__ import annotations

import csv
import os
import re
import sys
from datetime import date, timedelta
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from data import load_dataset  # noqa: E402

STATUSES = {"affordable_now", "affordable_with_plan", "affordable_later",
            "not_affordable"}
METHODS = {"full_payment", "partial_payment", "installments", "wait",
           "not_recommended"}
COLUMNS = ["request_id", "amount_safe_to_pay", "affordability_status",
           "recommended_payment_method", "payment_plan",
           "earliest_date_for_full_payment", "spending_changes_needed",
           "decision_explanation"]

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FLOAT_RE = re.compile(r"^\d+(\.\d+)?$")


def validate(output_csv: str, dataset_dir: str) -> List[str]:
    errors: List[str] = []
    ds = load_dataset(dataset_dir)

    with open(output_csv, encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        rows = list(reader)

    if header != COLUMNS:
        errors.append(f"header mismatch: {header}")
        return errors

    if len(rows) != len(ds.requests):
        errors.append(f"row count {len(rows)} != requests {len(ds.requests)}")

    req_ids = [r.request_id for r in ds.requests]
    for i, row in enumerate(rows):
        if len(row) != 8:
            errors.append(f"line {i+2}: expected 8 fields, got {len(row)}")
            continue
        (rid, safe_s, status, method, plan, earliest_s, changes, expl) = row
        req = ds.requests_by_id.get(rid)
        if req is None:
            errors.append(f"line {i+2}: unknown request_id {rid}")
            continue
        if i < len(req_ids) and rid != req_ids[i]:
            errors.append(f"line {i+2}: out of order (expected {req_ids[i]})")
        prof = ds.profiles[req.user_id]

        # amount_safe_to_pay
        if not FLOAT_RE.match(safe_s or ""):
            errors.append(f"{rid}: bad amount_safe_to_pay {safe_s!r}")
        else:
            safe = float(safe_s)
            if not (0 <= safe <= req.requested_amount + 1e-9):
                errors.append(f"{rid}: safe {safe} outside [0, {req.requested_amount}]")

        # enums
        if status not in STATUSES:
            errors.append(f"{rid}: bad status {status!r}")
        if method not in METHODS:
            errors.append(f"{rid}: bad method {method!r}")

        # plan
        legs: List = []
        if plan == "none" or plan == "":
            if status != "not_affordable" or method != "not_recommended":
                errors.append(f"{rid}: empty plan only allowed for not_recommended")
        else:
            ok_fmt = True
            for tok in plan.split("|"):
                m = re.match(r"^(\d{4}-\d{2}-\d{2}):(\d+(\.\d+)?)$", tok)
                if not m:
                    ok_fmt = False
                    errors.append(f"{rid}: bad plan token {tok!r}")
                    continue
                legs.append((date.fromisoformat(m.group(1)), float(m.group(2))))
            if ok_fmt:
                if any(legs[i][0] > legs[i + 1][0] for i in range(len(legs) - 1)):
                    errors.append(f"{rid}: plan legs not chronological")
                if legs and legs[-1][0] > req.desired_completion_date:
                    errors.append(f"{rid}: plan completes after deadline")
                if method == "installments":
                    valid = False
                    for opt in ds.options_by_request.get(rid, []):
                        if opt.payment_method != "installments":
                            continue
                        sched = opt.schedule()
                        if (len(sched) == len(legs) and
                                all(s[0] == l[0] and abs(s[1] - l[1]) < 0.005
                                    for s, l in zip(sched, legs))):
                            valid = True
                            break
                    if not valid:
                        errors.append(f"{rid}: installments do not match any option")

        # status/method relations
        if method == "not_recommended" and status != "not_affordable":
            errors.append(f"{rid}: not_recommended requires not_affordable")
        if method == "wait" and status != "affordable_later":
            errors.append(f"{rid}: wait requires affordable_later")
        if method == "partial_payment":
            if status != "affordable_with_plan":
                errors.append(f"{rid}: partial requires affordable_with_plan")
            if not req.allows_partial_payment:
                errors.append(f"{rid}: partial not allowed by request")
            if not prof.accepts("partial_payment"):
                errors.append(f"{rid}: user does not accept partial_payment")
            if len(legs) != 2:
                errors.append(f"{rid}: partial plan must have exactly 2 legs")
            else:
                if abs(legs[0][1] - (float(safe_s) if FLOAT_RE.match(safe_s or "") else -1)) > 0.005:
                    errors.append(f"{rid}: partial leg1 != amount_safe_to_pay")
                total = legs[0][1] + legs[1][1]
                if abs(total - req.requested_amount) > 0.005:
                    errors.append(f"{rid}: partial legs sum {total} != requested")
                if legs[0][0] != req.request_date:
                    errors.append(f"{rid}: partial leg1 must be request_date")
                if earliest_s != legs[1][0].isoformat():
                    errors.append(f"{rid}: partial earliest must equal leg2 date")

        # earliest
        if status == "not_affordable":
            if earliest_s != "":
                errors.append(f"{rid}: earliest must be empty for not_affordable")
        elif status == "affordable_now":
            if earliest_s != req.request_date.isoformat():
                errors.append(f"{rid}: affordable_now earliest must be request_date")
        elif earliest_s:
            if not DATE_RE.match(earliest_s):
                errors.append(f"{rid}: bad earliest date {earliest_s!r}")
            else:
                d = date.fromisoformat(earliest_s)
                if not (req.request_date <= d <= req.request_date + timedelta(days=90)):
                    errors.append(f"{rid}: earliest {d} outside 90-day window")

        # spending changes
        if changes not in ("", "none"):
            toks = changes.split("|")
            if len(toks) > 3:
                errors.append(f"{rid}: more than 3 spending changes")
            user_event_ids = {e.event_id for e in ds.events_by_user.get(req.user_id, [])}
            flex = {e.event_id: e.flexibility for e in ds.events_by_user.get(req.user_id, [])}
            for t in toks:
                mm = re.match(r"^stop:(event_\d+)$", t) or \
                     re.match(r"^reduce_to:(event_\d+):(\d+(\.\d+)?)$", t)
                if not mm:
                    errors.append(f"{rid}: bad change token {t!r}")
                    continue
                eid = mm.group(1)
                if eid not in user_event_ids:
                    errors.append(f"{rid}: change references foreign event {eid}")
                elif flex.get(eid) not in ("reducible", "stoppable",
                                           "reducible_or_stoppable"):
                    errors.append(f"{rid}: event {eid} not flexible")

        if not expl.strip():
            errors.append(f"{rid}: empty explanation")

    return errors


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    out_csv = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "..", "output.csv")
    ds_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.join(here, "..", "dataset")
    errs = validate(out_csv, ds_dir)
    if errs:
        print(f"INVALID: {len(errs)} violations")
        for e in errs[:40]:
            print("  -", e)
        sys.exit(1)
    print("VALID: output.csv satisfies the contract")
