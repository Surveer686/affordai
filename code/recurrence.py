"""Stage 3 - Recurrence detection + the 90-day forecast flow series.

From settled history we detect recurring series per user and project them over
the 90-day forecast window starting at request_date.

Detection (only when history supports it, per spec):
  * rows grouped per (user, direction, category) over settled, cash rows;
  * split into clusters wherever the gap between consecutive rows > 60 days;
  * a cluster qualifies when it has >= 3 rows and its last row falls within
    45 days before request_date (an ended subscription is not forecast);
  * cadence from the cluster's median gap: weekly (5-9), biweekly (10-22),
    monthly (23-40); anything else is not projected.

Projection policy (conservative):
  * expenses  -> amount = MAX of the cluster (worst observed value);
  * income    -> amount = MIN of the cluster;
  * monthly   -> fixed day-of-month (mode), clamped to month length;
  * weekly / biweekly -> every 7 / 14 days anchored on the last observed row.

Dedup vs explicit rows: a projected occurrence is dropped when the normalized
CashView already has a same-sign flow within +/-5 days of it in the same
category (explicit scheduled rows win - they may carry amended amounts).

Stage 4 evidence facts hook: `apply_facts()` mutates the forecast with typed
message facts (salary amount changes, confirmed one-off income, cancellations,
delays). Nothing else may inject or remove flows.
"""
from __future__ import annotations

import calendar
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import median
from typing import Dict, List, Optional, Tuple

from data import Dataset
from normalize import CashView

HORIZON_DAYS = 90
MIN_CLUSTER_ROWS = 3
LAST_ROW_WITHIN_DAYS = 45
MAX_CLUSTER_GAP_DAYS = 60

CADENCE_WEEKLY, CADENCE_BIWEEKLY, CADENCE_MONTHLY = "weekly", "biweekly", "monthly"


def _classify_cadence(median_gap: float) -> Optional[str]:
    if 5 <= median_gap <= 9:
        return CADENCE_WEEKLY
    if 10 <= median_gap <= 22:
        return CADENCE_BIWEEKLY
    if 23 <= median_gap <= 40:
        return CADENCE_MONTHLY
    return None


@dataclass
class RecurringSeries:
    key: str                    # e.g. "debit:rent:monthly"
    user_id: str
    direction: str              # debit|credit
    category: str
    cadence: str
    per_occurrence: float       # home currency, conservative amount
    monthly_equivalent: float   # per_occurrence * occurrences per 30 days
    dom: Optional[int]          # day of month for monthly cadence
    flexibility: str            # mode of flexibility flags ("" -> fixed)
    min_allowed: Optional[float]  # floor for reduce_to (expense series)
    last_observed: date

    @property
    def is_expense(self) -> bool:
        return self.direction == "debit"

    @property
    def adjustable(self) -> bool:
        return self.is_expense and self.flexibility in (
            "reducible", "stoppable", "reducible_or_stoppable")


@dataclass
class ForecastFlow:
    day: date
    amount: float               # signed; + credit, - debit (home currency)
    source: str                 # "event:<id>" | "series:<key>" | "fact:<n>"
    category: str = ""
    adjustable_series: Optional[str] = None  # series key if from an adjustable series


@dataclass
class UserForecast:
    user_id: str
    request_date: date
    horizon_end: date
    opening_balance: float
    reserved: float
    flows: List[ForecastFlow] = field(default_factory=list)
    series: List[RecurringSeries] = field(default_factory=list)

    def flows_between(self, start: date, end: date) -> List[ForecastFlow]:
        return [f for f in self.flows if start <= f.day <= end]


# --------------------------------------------------------------------------
# Clustering
# --------------------------------------------------------------------------

def _clusters(rows: List) -> List[List]:
    """Split sorted rows into clusters wherever the gap exceeds 60 days."""
    out: List[List] = []
    cur: List = [rows[0]]
    for r in rows[1:]:
        if (r.event_date - cur[-1].event_date).days <= MAX_CLUSTER_GAP_DAYS:
            cur.append(r)
        else:
            out.append(cur)
            cur = [r]
    out.append(cur)
    return out


def _mode_day(rows: List) -> int:
    return Counter(r.event_date.day for r in rows).most_common(1)[0][0]


def _mode_flexibility(rows: List) -> str:
    flags = [r.flexibility for r in rows if r.flexibility]
    if not flags:
        return "fixed"
    return Counter(flags).most_common(1)[0][0]


def _floors(rows: List) -> Optional[float]:
    vals = [r.minimum_allowed_amount for r in rows if r.minimum_allowed_amount]
    return float(median(vals)) if vals else None


# --------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------

def _month_add(d: date, k: int) -> date:
    m = d.month - 1 + k
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, 1)


def _occurrences_monthly(dom: int, request_date: date, horizon_end: date):
    k = 0
    while True:
        base = _month_add(request_date, k)
        day = min(dom, calendar.monthrange(base.year, base.month)[1])
        occ = date(base.year, base.month, day)
        if occ > horizon_end:
            return
        k += 1
        if occ >= request_date:
            yield occ


def _occurrences_interval(anchor: date, step_days: int, request_date: date,
                          horizon_end: date):
    k = 0
    while True:
        occ = anchor + timedelta(days=step_days * k)
        if occ > horizon_end:
            return
        k += 1
        if occ >= request_date:
            yield occ


def detect_series(ds: Dataset, user_id: str, request_date: date) -> List[RecurringSeries]:
    rows_by_key: Dict[Tuple[str, str], List] = defaultdict(list)
    for ev in ds.events_by_user.get(user_id, []):
        if ev.status != "settled" or ev.amount is None:
            continue
        if ev.direction not in ("debit", "credit") or ev.event_type == "refund":
            continue
        rows_by_key[(ev.direction, ev.category)].append(ev)

    series: List[RecurringSeries] = []
    for (direction, category), rows in rows_by_key.items():
        rows.sort(key=lambda r: r.event_date)
        for cl in _clusters(rows):
            if len(cl) < MIN_CLUSTER_ROWS:
                continue
            if (request_date - cl[-1].event_date).days > LAST_ROW_WITHIN_DAYS:
                continue  # series ended before the request -> do not forecast
            gaps = [(cl[i + 1].event_date - cl[i].event_date).days
                    for i in range(len(cl) - 1)]
            cadence = _classify_cadence(median(gaps))
            if cadence is None:
                continue
            amounts = [e.amount for e in cl if e.amount]
            if not amounts:
                continue
            per_occ = max(amounts) if direction == "debit" else min(amounts)
            if per_occ <= 0:
                continue
            per30 = {CADENCE_WEEKLY: 30 / 7, CADENCE_BIWEEKLY: 30 / 14,
                     CADENCE_MONTHLY: 1.0}[cadence]
            series.append(RecurringSeries(
                key=f"{direction}:{category}:{cadence}",
                user_id=user_id, direction=direction, category=category,
                cadence=cadence, per_occurrence=round(per_occ, 2),
                monthly_equivalent=round(per_occ * per30, 2),
                dom=_mode_day(cl) if cadence == CADENCE_MONTHLY else None,
                flexibility=_mode_flexibility(cl),
                min_allowed=_floors(cl),
                last_observed=cl[-1].event_date))
    return series


def build_forecast(ds: Dataset, view: CashView, user_id: str,
                   request_date: date, evidence_facts: Optional[List[dict]] = None
                   ) -> UserForecast:
    horizon_end = request_date + timedelta(days=HORIZON_DAYS)
    fc = UserForecast(user_id=user_id, request_date=request_date,
                      horizon_end=horizon_end,
                      opening_balance=view.opening_balance, reserved=view.reserved)

    # 1) explicit event flows from Stage 2 (already signed, home currency)
    for d, amt, eid in view.future_flows:
        if request_date <= d <= horizon_end:
            ev = ds.events_by_id[eid]
            fc.flows.append(ForecastFlow(d, amt, f"event:{eid}", category=ev.category))

    # 2) projected recurring series (deduped against explicit flows)
    fc.series = detect_series(ds, user_id, request_date)
    for s in fc.series:
        sign = -1.0 if s.is_expense else 1.0
        if s.cadence == CADENCE_MONTHLY:
            occ_iter = _occurrences_monthly(s.dom, request_date, horizon_end)
        else:
            step = 7 if s.cadence == CADENCE_WEEKLY else 14
            occ_iter = _occurrences_interval(s.last_observed, step,
                                             request_date, horizon_end)
        explicit_same_cat = [(f.day, f.amount) for f in fc.flows
                             if f.category == s.category and (f.amount > 0) == (sign > 0)]
        for occ in occ_iter:
            if any(abs((occ - d).days) <= 5 for d, _ in explicit_same_cat):
                continue  # explicit row already covers this occurrence
            fc.flows.append(ForecastFlow(
                occ, sign * s.per_occurrence, f"series:{s.key}",
                category=s.category,
                adjustable_series=s.key if s.adjustable else None))

    # 3) evidence facts from Stage 4 (typed; see apply_facts)
    if evidence_facts:
        apply_facts(fc, evidence_facts)

    fc.flows.sort(key=lambda f: f.day)
    return fc


# --------------------------------------------------------------------------
# Stage 4 hook: typed evidence facts
# --------------------------------------------------------------------------

def apply_facts(fc: UserForecast, facts: List[dict]) -> None:
    """Apply typed message-derived facts.

    Supported fact dicts:
      {"type": "salary_change", "amount": X, "currency"?: C, "from_date"?: D}
          -> changes the salary series amount on/after from_date.
      {"type": "confirmed_income", "amount": X, "currency"?: C, "day": D}
          -> injects a one-off confirmed credit on D.
      {"type": "cancellation", "match_day"?: D, "category"?: C}
          -> removes a projected flow (never an explicit event row).
      {"type": "delay", "match_day": D, "new_day": N}
          -> moves a projected flow to N.
    Currency conversion uses the dataset FX table via a closure set by
    set_fx_context(); without it, amounts are assumed already home currency.
    """
    for i, fact in enumerate(facts):
        ftype = fact.get("type")
        amt = fact.get("amount_home")
        if ftype == "salary_change" and amt:
            from_d = fact.get("from_date") or fc.request_date
            for f in fc.flows:
                if f.category == "salary" and f.amount > 0 and f.day >= from_d:
                    f.amount = amt
                    f.source += f"|fact:{i}"
        elif ftype == "confirmed_income" and amt:
            day = fact.get("day")
            if day and fc.request_date <= day <= fc.horizon_end:
                fc.flows.append(ForecastFlow(
                    day, amt, f"fact:{i}", category=fact.get("category", "salary")))
        elif ftype == "cancellation":
            md, cat = fact.get("match_day"), fact.get("category")
            fc.flows = [f for f in fc.flows
                        if not (f.source.startswith(("series:", "fact:"))
                                and (md is None or f.day == md)
                                and (cat is None or f.category == cat))]
        elif ftype == "delay":
            md, nd = fact.get("match_day"), fact.get("new_day")
            if md and nd:
                for f in fc.flows:
                    if f.day == md and f.source.startswith(("series:", "fact:")):
                        f.day = nd
        fc.flows.sort(key=lambda f: f.day)


def set_fx_context(ds: Dataset, user_id: str) -> None:
    """No-op placeholder for Stage 4 wiring (kept for interface stability)."""
    return None


if __name__ == "__main__":
    import os
    from data import load_dataset
    from normalize import normalize_dataset

    here = os.path.dirname(os.path.abspath(__file__))
    ds = load_dataset(os.path.join(here, "..", "dataset"))
    views = normalize_dataset(ds, os.path.join(here, "evidence", "blank_amounts.json"))

    reqs = sorted(ds.requests, key=lambda r: r.request_date)
    print(f"requests: {len(reqs)}")
    n_series, n_proj = [], []
    for r in reqs:
        f = build_forecast(ds, views[r.user_id], r.user_id, r.request_date)
        n_series.append(len(f.series))
        n_proj.append(sum(1 for fl in f.flows if fl.source.startswith("series:")))
    from statistics import mean
    print(f"avg series/user: {mean(n_series):.1f}  avg projected flows: {mean(n_proj):.1f} "
          f" max: {max(n_series)}/{max(n_proj)}")

    demo = reqs[0]
    f = build_forecast(ds, views[demo.user_id], demo.user_id, demo.request_date)
    print(f"\n=== {demo.request_id} {demo.user_id} on {demo.request_date} "
          f"(home {views[demo.user_id].home_currency})")
    print(f"opening {f.opening_balance:,.2f}  reserved {f.reserved:,.2f}")
    for s in f.series:
        print(f"  series {s.key:34s} per_occ {s.per_occurrence:>12,.2f} "
              f"/30d {s.monthly_equivalent:>12,.2f} flex {s.flexibility}")
    net = sum(fl.amount for fl in f.flows)
    print(f"  flows: {len(f.flows)}  net over 90d: {net:,.2f} "
          f" end balance ~ {f.opening_balance - f.reserved + net:,.2f}")
