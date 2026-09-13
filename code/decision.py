"""Stage 6 - Decision engine: produces the 8 output columns per request.

Conventions reverse-engineered from the 25 solved samples:

Candidate plans (feasible = every day >= min_balance AND completion <= deadline):
  installments    a seller option whose exact schedule is safe and completes
                  by the deadline  ->  affordable_with_plan
  full            single leg (request_date, requested_amount)
                       - safe with no changes        -> affordable_now
                       - safe after stop/reduce      -> affordable_with_plan
  partial         (today, safe_amount) + (earliest-safe day, remainder);
                  only when allows_partial_payment AND the remainder completes
                  STRICTLY before the deadline (req_04 vs req_19 evidence)
  wait            single leg on the first day the full amount is safe
                  (samples: affordable_later)

Ranking: installments (min total_payable) > full(no changes) is folded into
affordable_now only when NO installment option is feasible; then full+changes >
partial > wait > not_affordable.

Columns:
  amount_safe_to_pay   max safe today WITHOUT changes, capped at requested
  earliest_date        first safe single-full-payment date in the horizon
                       (empty for not_affordable; = request_date for now)
  spending_changes     "stop:<event_id>" / "reduce_to:<event_id>:<amount>",
                       "|"-joined, stops before reduces; only projected series
                       from categories the profile is willing to adjust.
Formatting: safe amount strips trailing zeros; plan/change amounts keep 2dp
unless integral ("620.40" but "13110000").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional, Tuple

from data import Dataset, PaymentOption, Request
from normalize import CashView
from recurrence import UserForecast
from simulate import Adjustment, earliest_safe_date, max_safe_today, simulate

EPS = 1e-6


# --------------------------------------------------------------------------
# formatting helpers (reverse-engineered from output.csv samples)
# --------------------------------------------------------------------------

def fmt_safe(x: float) -> str:
    """amount_safe_to_pay style: strip all trailing zeros ('17229139.2')."""
    s = f"{float(x):.2f}"
    s = s.rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def fmt_leg(x: float) -> str:
    """plan/change amounts: 2dp, but integral values stay bare ('620.40', '13110000')."""
    s = f"{float(x):.2f}"
    return s[:-3] if s.endswith(".00") else s


def fmt_money_text(x: float) -> str:
    """Explanation text amounts: thousands separators, 2dp kept unless integral
    ('15,952,906.67', '996.60', '25,256')."""
    s = f"{float(x):,.2f}"
    return s[:-3] if s.endswith(".00") else s


@dataclass
class Candidate:
    method: str                      # full_payment|installments|partial_payment|wait|not_recommended
    status: str
    legs: List[Tuple[date, float]] = field(default_factory=list)
    adjustments: List[Adjustment] = field(default_factory=list)
    earliest: Optional[date] = None
    option: Optional[PaymentOption] = None
    note: str = ""

    @property
    def rank(self) -> Tuple[int, float, str]:
        order = {"installments": 0, "full_payment": 1, "partial_payment": 2,
                 "wait": 3, "not_recommended": 4}
        total = sum(a for _, a in self.legs)
        return (order[self.method], total if self.method != "installments" else 0.0,
                self.note)


# --------------------------------------------------------------------------
# candidate builders
# --------------------------------------------------------------------------

def _installment_candidates(ds: Dataset, fc: UserForecast, min_balance: float,
                            r: Request) -> List[Candidate]:
    out = []
    for opt in ds.options_by_request.get(r.request_id, []):
        if opt.payment_method != "installments":
            continue
        legs = opt.schedule()
        if not legs or legs[-1][0] > r.desired_completion_date:
            continue  # does not complete by the deadline
        if legs[-1][0] > fc.horizon_end:
            continue
        if not simulate(fc, min_balance, schedule=legs).ok:
            continue
        out.append(Candidate("installments", "affordable_with_plan",
                             legs=legs, option=opt,
                             note=f"{opt.payment_option_id}"))
    return out


def _profile_allows(ds: Dataset, fc: UserForecast, adj: Adjustment) -> bool:
    prof = ds.profiles[fc.user_id]
    series = next((s for s in fc.series if s.key == adj.series_key), None)
    if series is None or not series.adjustable:
        return False
    if adj.action == "stop":
        return series.can_stop_category and series.category in prof.categories_willing_to_stop
    return series.category in prof.categories_willing_to_reduce


def _needed_reduce_to(fc: UserForecast, min_balance: float, key: str,
                      requested: float) -> Optional[float]:
    """Smallest per-occurrence value (>= floor) that unlocks the full payment."""
    series = next((s for s in fc.series if s.key == key), None)
    if series is None or not series.min_allowed:
        return None
    lo, hi = float(series.min_allowed), float(series.per_occurrence)
    best = None
    # clean candidate values first (generator-plausible), then binary refine
    cands = [lo, round(hi * 0.75, 2), round(hi * 0.5, 2), round(hi * 0.25, 2)]
    for v in sorted(set(c for c in cands if lo <= c < hi)):
        if simulate(fc, min_balance, adjustments=[Adjustment(key, "reduce", v)],
                    pay_today=requested).ok:
            return v
    a, b = lo, hi
    for _ in range(24):
        mid = round((a + b) / 2, 2)
        if simulate(fc, min_balance, adjustments=[Adjustment(key, "reduce", mid)],
                    pay_today=requested).ok:
            best = mid
            b = mid
        else:
            a = mid
    return best


def _full_with_changes(ds: Dataset, fc: UserForecast, min_balance: float,
                       requested: float) -> Optional[Candidate]:
    if not simulate(fc, min_balance, pay_today=requested).ok:
        return None
    return Candidate("full_payment", "affordable_now",
                     legs=[(fc.request_date, requested)])


def _search_changes(ds: Dataset, fc: UserForecast, min_balance: float,
                    requested: float) -> Optional[Candidate]:
    if simulate(fc, min_balance, pay_today=requested).ok:
        return None  # changes not needed (handled elsewhere)
    prof = ds.profiles[fc.user_id]
    adjustable = [s for s in fc.series if s.adjustable
                  and ((s.can_stop_category and s.category in prof.categories_willing_to_stop)
                       or (s.category in prof.categories_willing_to_reduce
                           and ("reducible" in s.flexibility)))]
    if not adjustable:
        return None

    def ok(adjs):
        return simulate(fc, min_balance, adjustments=adjs, pay_today=requested).ok

    # 1 change: stops first (cheapest impact), then reduces
    stops = sorted([s for s in adjustable
                    if s.can_stop_category and s.category in prof.categories_willing_to_stop],
                   key=lambda s: (s.monthly_equivalent, s.key))
    for s in stops:
        a = [Adjustment(s.key, "stop")]
        if ok(a):
            return Candidate("full_payment", "affordable_with_plan",
                             legs=[(fc.request_date, requested)], adjustments=a)
    reduces = sorted([s for s in adjustable if "reducible" in s.flexibility
                      and s.category in prof.categories_willing_to_reduce],
                     key=lambda s: (s.monthly_equivalent, s.key))
    for s in reduces:
        v = _needed_reduce_to(fc, min_balance, s.key, requested)
        if v is not None:
            a = [Adjustment(s.key, "reduce", v)]
            if ok(a):
                return Candidate("full_payment", "affordable_with_plan",
                                 legs=[(fc.request_date, requested)], adjustments=a)
    # 2 changes: stop+stop, stop+reduce
    for i, s1 in enumerate(stops):
        for j, s2 in enumerate(stops):
            if j <= i:
                continue
            a = [Adjustment(s1.key, "stop"), Adjustment(s2.key, "stop")]
            if ok(a):
                return Candidate("full_payment", "affordable_with_plan",
                                 legs=[(fc.request_date, requested)], adjustments=a)
    for s1 in stops:
        for s2 in reduces:
            v = _needed_reduce_to(fc, min_balance, s2.key, requested)
            if v is None:
                continue
            a = [Adjustment(s1.key, "stop"), Adjustment(s2.key, "reduce", v)]
            if ok(a):
                return Candidate("full_payment", "affordable_with_plan",
                                 legs=[(fc.request_date, requested)], adjustments=a)
    return None


def _partial_candidate(ds: Dataset, fc: UserForecast, min_balance: float,
                       r: Request, safe_today: float) -> Optional[Candidate]:
    if not r.allows_partial_payment or safe_today <= 0:
        return None
    remaining = round(r.requested_amount - safe_today, 2)
    if remaining <= 0:
        return None
    if not simulate(fc, min_balance, pay_today=safe_today).ok:
        return None
    d2 = earliest_safe_date(fc, remaining, min_balance, r.desired_completion_date)
    if d2 is None or d2 >= r.desired_completion_date:
        return None  # req_04 evidence: remainder must complete strictly before deadline
    return Candidate("partial_payment", "affordable_with_plan",
                     legs=[(fc.request_date, safe_today), (d2, remaining)])


def _wait_candidate(fc: UserForecast, min_balance: float, r: Request,
                    earliest: Optional[date]) -> Optional[Candidate]:
    if earliest is None:
        return None
    leg = (earliest, r.requested_amount)
    if not simulate(fc, min_balance, schedule=[leg]).ok:
        return None
    if leg[0] > fc.horizon_end:
        return None
    return Candidate("wait", "affordable_later", legs=[leg])


# --------------------------------------------------------------------------
# main entry
# --------------------------------------------------------------------------

def decide(ds: Dataset, fc: UserForecast, r: Request) -> dict:
    prof = ds.profiles[r.user_id]
    min_balance = prof.minimum_balance_to_keep
    cur = prof.home_currency

    safe = max_safe_today(fc, min_balance, r.requested_amount)
    earliest = earliest_safe_date(fc, r.requested_amount, min_balance,
                                  fc.horizon_end)

    # Ranking (reverse-engineered from the 25 samples):
    #   installments (feasible by deadline) > partial (allowed+feasible) >
    #   full now > full with changes > wait > not_recommended.
    # req_12: installments chosen even though full was safe today.
    # req_19 (partial chosen): under the reference forecast no installment
    # option was feasible there; installments-first matches the majority.
    candidates = _installment_candidates(ds, fc, min_balance, r)
    full_now = _full_with_changes(ds, fc, min_balance, r.requested_amount) \
        if prof.accepts("full_payment") else None
    partial = _partial_candidate(ds, fc, min_balance, r, safe) \
        if prof.accepts("partial_payment") else None
    if candidates:
        best = min(candidates, key=lambda c: (c.option.total_payable_amount,
                                              c.legs[-1][0], c.note))
        chosen, kind = best, "installments"
    elif partial is not None:
        chosen, kind = partial, "partial"
    elif full_now is not None:
        chosen, kind = full_now, "full_now"
    else:
        with_chg = _search_changes(ds, fc, min_balance, r.requested_amount)
        if with_chg is not None:
            chosen, kind = with_chg, "full_changes"
        else:
            wait = _wait_candidate(fc, min_balance, r, earliest)
            if wait is not None:
                chosen, kind = wait, "wait"
            else:
                chosen, kind = Candidate("not_recommended", "not_affordable",
                                         note="none"), "none"

    # ---- columns --------------------------------------------------------
    plan_str = "|".join(f"{d.isoformat()}:{fmt_leg(a)}" for d, a in chosen.legs) \
        if chosen.legs else "none"

    if kind == "full_now":
        earliest_col = r.request_date
    elif kind == "partial":
        earliest_col = chosen.legs[1][0]  # spec: remainder paid on earliest_date
    elif kind == "none":
        earliest_col = None
    else:
        earliest_col = earliest

    changes: List[str] = []
    if chosen.adjustments:
        series_by_key = {s.key: s for s in fc.series}
        stops = [a for a in chosen.adjustments if a.action == "stop"]
        reduces = [a for a in chosen.adjustments if a.action == "reduce"]
        for a in stops:
            changes.append(f"stop:{series_by_key[a.series_key].repr_event_id}")
        for a in reduces:
            changes.append(
                f"reduce_to:{series_by_key[a.series_key].repr_event_id}:{fmt_leg(a.reduce_to)}")

    # ---- explanation ----------------------------------------------------
    min_txt = fmt_money_text(min_balance)
    if kind == "full_now":
        expl = (f"Pay {cur} {fmt_money_text(r.requested_amount)} today. "
                f"This leaves at least {cur} {min_txt} available over the next 90 days.")
    elif kind == "installments":
        n = chosen.option.number_of_payments
        amt = chosen.option.payment_amount
        expl = (f"Use {n} installments of {cur} {fmt_money_text(amt)}, starting "
                f"{chosen.legs[0][0].isoformat()}. This leaves at least {cur} {min_txt} available.")
    elif kind == "full_changes":
        series_by_key = {s.key: s for s in fc.series}
        parts = []
        for a in chosen.adjustments:
            s = series_by_key[a.series_key]
            if a.action == "stop":
                parts.append(f"Stop the {s.label}")
            else:
                parts.append(f"Reduce the {s.label} to {cur} {fmt_money_text(a.reduce_to)}")
        expl = (" and ".join(parts) +
                f", then pay {cur} {fmt_money_text(r.requested_amount)} today. "
                f"This leaves at least {cur} {min_txt} available.")
    elif kind == "partial":
        a1, a2 = chosen.legs[0][1], chosen.legs[1][1]
        expl = (f"Pay {cur} {fmt_money_text(a1)} today and the remaining "
                f"{cur} {fmt_money_text(a2)} on {chosen.legs[1][0].isoformat()}. "
                f"This completes the full request and keeps the {cur} {min_txt} minimum protected.")
    elif kind == "wait":
        expl = (f"Pay {cur} {fmt_money_text(r.requested_amount)} in full on "
                f"{chosen.legs[0][0].isoformat()}. Paying earlier would take the balance "
                f"below the {cur} {min_txt} minimum.")
    else:
        if safe > 0 and r.allows_partial_payment:
            expl = (f"Do not proceed with the {cur} {fmt_money_text(r.requested_amount)} request. "
                    f"Although {cur} {fmt_money_text(safe)} is available today, the full amount "
                    f"cannot be completed safely within 90 days.")
        else:
            expl = (f"Do not make this payment by {r.desired_completion_date.isoformat()}. "
                    f"None of the available options keeps the {cur} {min_txt} minimum protected.")

    return {
        "request_id": r.request_id,
        "amount_safe_to_pay": fmt_safe(safe),
        "affordability_status": chosen.status,
        "recommended_payment_method": chosen.method,
        "payment_plan": plan_str,
        "earliest_date_for_full_payment": earliest_col.isoformat() if earliest_col else "",
        "spending_changes_needed": "|".join(changes) if changes else "none",
        "decision_explanation": expl,
    }


if __name__ == "__main__":
    import os
    from data import load_dataset
    from normalize import normalize_dataset
    from recurrence import build_forecast

    here = os.path.dirname(os.path.abspath(__file__))
    ds = load_dataset(os.path.join(here, "..", "dataset"))
    views = normalize_dataset(ds, os.path.join(here, "evidence", "blank_amounts.json"))

    import csv
    from data import Request
    sample = {}
    with open(os.path.join(here, "..", "dataset", "sample_requests.csv"), encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            r = Request(
                request_id=row["request_id"], user_id=row["user_id"],
                request_date=date.fromisoformat(row["request_date"]),
                request_type=row["request_type"],
                requested_amount=float(row["requested_amount"]),
                desired_completion_date=date.fromisoformat(row["desired_completion_date"]),
                allows_partial_payment=row["allows_partial_payment"] == "true",
                request_text=row["request_text"])
            ds.requests_by_id[r.request_id] = r  # samples are absent from requests.csv
            sample[row["request_id"]] = row

    from collections import Counter
    st = Counter()
    for rid, truth in sorted(sample.items()):
        r = ds.requests_by_id[rid]
        f = build_forecast(ds, views[r.user_id], r.user_id, r.request_date)
        out = decide(ds, f, r)
        st[truth["affordability_status"] + " -> " + out["affordability_status"]] += 1
        same_method = truth["recommended_payment_method"] == out["recommended_payment_method"]
        print(f"{rid}: truth={truth['affordability_status']:22s} got={out['affordability_status']:22s} "
              f"method={'OK ' if same_method else 'DIFF'} "
              f"truth={truth['recommended_payment_method']:16s} got={out['recommended_payment_method']}")
    print("\nstatus transitions:", dict(st))
