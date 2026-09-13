"""Stage 5 - 90-day balance simulator + min-balance safety check.

Balance model:
  * starting balance = current_available_balance - reserved (pending debits are
    money that will leave the account; conservative upfront reservation),
  * flows applied on their day; SAFETY is checked on end-of-day balances,
  * a plan is safe iff every end-of-day balance in [request_date, horizon_end]
    stays >= minimum_balance_to_keep.

Core queries used by the decision engine (Stage 6):
  * simulate(...)                 -> full balance path + trough + ok flag
  * max_safe_today(...)           -> closed-form amount_safe_to_pay (cap applied)
  * earliest_safe_date(...)       -> first day a single full payment is safe
  * adjustments via Adjustment    -> stop/reduce projected series flows so
    Stage 6 can search spending-change plans deterministically.

Note: with fixed flows, max_safe_today is exact algebra, not a search:
  pay today p keeps path safe  <=>  p <= B0 - min_balance + min_d S(d),
where B0 = opening - reserved and S(d) = cumulative net flow from today to d.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from normalize import CashView
from recurrence import ForecastFlow, UserForecast

HORIZON_DAYS = 90


@dataclass
class Adjustment:
    series_key: str
    action: str                    # "stop" | "reduce"
    reduce_to: Optional[float] = None


@dataclass
class SimResult:
    ok: bool
    trough: float
    trough_day: Optional[date]
    end_balance: float
    start_balance: float
    balance_by_day: Dict[date, float] = field(default_factory=dict)


def adjusted_flows(fc: UserForecast, adjustments: Optional[List[Adjustment]] = None
                   ) -> List[ForecastFlow]:
    """Projected series flows with stop/reduce adjustments applied.

    Explicit event flows and fact flows are never adjusted; only flows that
    carry `adjustable_series` (i.e. projected from flexible series) change.
    """
    if not adjustments:
        return fc.flows
    stop = {a.series_key for a in adjustments if a.action == "stop"}
    reduce = {a.series_key: a.reduce_to for a in adjustments if a.action == "reduce"}
    out: List[ForecastFlow] = []
    for f in fc.flows:
        key = f.adjustable_series
        if key and key in stop:
            continue
        if key and key in reduce and reduce[key] is not None:
            sign = -1.0 if f.amount < 0 else 1.0
            new_amt = max(float(reduce[key]), 0.0)
            out.append(ForecastFlow(f.day, sign * new_amt, f.source,
                                    category=f.category, adjustable_series=key))
        else:
            out.append(f)
    return out


def simulate(fc: UserForecast, min_balance: float,
             adjustments: Optional[List[Adjustment]] = None,
             pay_today: float = 0.0,
             schedule: Optional[List[Tuple[date, float]]] = None,
             count_reserved: bool = True) -> SimResult:
    """Walk the balance path. `schedule` entries are extra debits (e.g.
    installment legs); `pay_today` is a debit on request_date."""
    flows = adjusted_flows(fc, adjustments)
    start = fc.opening_balance - (fc.reserved if count_reserved else 0.0)

    events: List[Tuple[date, float]] = [(f.day, f.amount) for f in flows]
    if pay_today:
        events.append((fc.request_date, -float(pay_today)))
    for d, amt in (schedule or []):
        events.append((d, -float(amt)))

    balance = start
    trough, trough_day = start, fc.request_date
    by_day: Dict[date, float] = {}
    day_index = 0
    events.sort(key=lambda x: x[0])

    # include every calendar day so empty days still expose the running balance
    all_days: List[date] = []
    d = fc.request_date
    while d <= fc.horizon_end:
        all_days.append(d)
        d += timedelta(days=1)

    ei = 0
    for d in all_days:
        while ei < len(events) and events[ei][0] <= d:
            balance += events[ei][1]
            ei += 1
        by_day[d] = balance
        if balance < trough:
            trough, trough_day = balance, d
    ok = trough >= min_balance - 1e-6
    return SimResult(ok=ok, trough=trough, trough_day=trough_day,
                     end_balance=balance, start_balance=start,
                     balance_by_day=by_day)


def max_safe_today(fc: UserForecast, min_balance: float, cap: float,
                   adjustments: Optional[List[Adjustment]] = None,
                   count_reserved: bool = True) -> float:
    """Largest payment on request_date that keeps the whole path >= min_balance.

    Closed form: p <= B0 - min_balance + min_d S(d), floored at 0, capped.
    """
    flows = adjusted_flows(fc, adjustments)
    b0 = fc.opening_balance - (fc.reserved if count_reserved else 0.0)

    today_flows = [f for f in flows if f.day == fc.request_date]
    later = sorted((f for f in flows if f.day > fc.request_date),
                   key=lambda f: f.day)
    s = sum(f.amount for f in today_flows)  # S(today)
    worst = s
    for f in later:
        s += f.amount
        worst = min(worst, s)
    p = b0 - min_balance + worst
    return round(max(0.0, min(float(cap), p)), 2)


def earliest_safe_date(fc: UserForecast, amount: float, min_balance: float,
                       deadline: date,
                       count_reserved: bool = True) -> Optional[date]:
    """First day in [request_date, min(deadline, horizon_end)] where a single
    payment of `amount` keeps every end-of-day balance >= min_balance."""
    amount = float(amount)
    if amount <= 0:
        return fc.request_date
    last = min(deadline, fc.horizon_end)
    flows = sorted(fc.flows, key=lambda f: f.day)
    b0 = fc.opening_balance - (fc.reserved if count_reserved else 0.0)

    # prefix balances without the payment; then try each candidate day D:
    # safe iff (a) path up to D-1 ok and (b) balance from D onward (minus
    # amount) never dips below min_balance.
    running = b0
    pre: Dict[date, float] = {}
    for f in flows:
        if f.day > last:
            break
        running += f.amount
        pre[f.day] = running

    horizon = [f for f in flows if f.day <= last]
    for d in sorted({fc.request_date} | {f.day for f in horizon}):
        if d > last:
            break
        # path up to the day before d must be safe without the payment
        ok_pre = all(v >= min_balance - 1e-6 for dd, v in pre.items() if dd < d)
        if not ok_pre:
            continue
        # walk from d with the payment applied
        bal = b0 - amount
        ok = True
        for f in horizon:
            if f.day < d:
                continue
            bal += f.amount
            if bal < min_balance - 1e-6:
                ok = False
                break
        if ok:
            return d
    return None


def safety_check(fc: UserForecast, min_balance: float,
                 adjustments: Optional[List[Adjustment]] = None,
                 pay_today: float = 0.0,
                 schedule: Optional[List[Tuple[date, float]]] = None,
                 count_reserved: bool = True) -> bool:
    return simulate(fc, min_balance, adjustments, pay_today, schedule,
                    count_reserved).ok


if __name__ == "__main__":
    import os
    from data import load_dataset
    from normalize import normalize_dataset
    from recurrence import build_forecast

    here = os.path.dirname(os.path.abspath(__file__))
    ds = load_dataset(os.path.join(here, "..", "dataset"))
    views = normalize_dataset(ds, os.path.join(here, "evidence", "blank_amounts.json"))

    import csv
    with open(os.path.join(here, "..", "dataset", "requests.csv"), encoding="utf-8") as fh:
        req_rows = list(csv.DictReader(fh))
    n_ok = n_unsafe = 0
    worst_gap = None
    for row in req_rows:
        rid = row["request_id"]
        r = ds.requests_by_id[rid]
        prof = ds.profiles[r.user_id]
        f = build_forecast(ds, views[r.user_id], r.user_id, r.request_date)
        safe = max_safe_today(f, prof.minimum_balance_to_keep, r.requested_amount)
        sim = simulate(f, prof.minimum_balance_to_keep, pay_today=safe)
        d = earliest_safe_date(f, r.requested_amount, prof.minimum_balance_to_keep,
                               r.desired_completion_date)
        if safe >= r.requested_amount - 1e-9:
            n_ok += 1
            assert sim.ok, f"{rid}: safe={safe} but sim failed"
        else:
            n_unsafe += 1
            if worst_gap is None or safe / max(r.requested_amount, 1) < worst_gap[1]:
                worst_gap = (rid, safe / max(r.requested_amount, 1))
    print(f"requests: {len(req_rows)}  full-amount-safe: {n_ok}  partial/none: {n_unsafe}")
    print(f"worst affordability ratio: {worst_gap}")

    # worked example with adjustment
    r = ds.requests_by_id["request_73"]
    prof = ds.profiles[r.user_id]
    f = build_forecast(ds, views[r.user_id], r.user_id, r.request_date)
    print(f"\nrequest_73 amount={r.requested_amount:,.2f} min={prof.minimum_balance_to_keep:,.2f}")
    print("  max_safe_today:", f"{max_safe_today(f, prof.minimum_balance_to_keep, r.requested_amount):,.2f}")
    stoppers = [Adjustment(s.key, "stop") for s in f.series if s.adjustable]
    print("  with all flexible stopped:",
          f"{max_safe_today(f, prof.minimum_balance_to_keep, r.requested_amount, stoppers):,.2f}")
    ed = earliest_safe_date(f, r.requested_amount, prof.minimum_balance_to_keep,
                            r.desired_completion_date)
    print("  earliest full-payment date:", ed, "deadline:", r.desired_completion_date)
