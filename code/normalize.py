"""Stage 2 - Event normalization.

Turns raw financial_events rows into a normalized, cash-flow-relevant view.

Inclusion rules (from problem_statement.md + sample analysis):
  * `cancelled` / `failed`  -> excluded entirely (money never moved).
  * `pending` debits        -> INCLUDED with reserve=True (money owed / blocked
                               soon; must reduce the safe-to-pay amount).
  * `pending` credits       -> EXCLUDED (unconfirmed income; never forecast).
  * `scheduled` credits     -> INCLUDED only when "confirmed" (see below);
                               counted on their settlement date.
  * `scheduled` debits      -> INCLUDED as future outflows on their date.
  * `unrealized`            -> excluded (non-cash portfolio valuations; also
                               the only rows with blank settlement_date).
  * `non_cash` direction    -> excluded.

Confirmed income = salary/income credits whose reality is corroborated by
evidence: a matching settled income history (recurrence) or a message/image
fact (Stage 4 will feed amendments here).  In this stage we implement the
conservative default: scheduled income is included only if the user has a
settled income history at monthly cadence, or the event is referenced by a
message/image.  Everything else stays out of the forecast.

Blank amounts are filled from evidence/blank_amounts.json (image extraction).

Linked lifecycles: rows joined by linked_event_id are distinct cash events
(expense + its refund, purchase + later sale, cancelled->settled retry, failed
debit -> rescheduled debit). Status rules above already count each exactly
once; an exact-duplicate safety net guards scheduled rows that would otherwise
double-count the same future movement.

Output: NormalizedEvent list + per-user CashView (opening balance snapshot,
reserved amounts, dated future flows).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Set

from data import Dataset, Event, load_dataset

EXCLUDED_STATUSES = {"cancelled", "failed", "unrealized"}


@dataclass
class NormalizedEvent:
    event: Event
    amount_home: float                 # in the user's home currency
    settlement: Optional[date]         # day cash moves (None -> not cash-dated)
    reserve: bool = False              # True for pending debits (already blocked)
    duplicate_of: Optional[str] = None  # event_id this lifecycle duplicate maps to


@dataclass
class CashView:
    user_id: str
    home_currency: str
    opening_balance: float             # balance as of the day before the request
    reserved: float = 0.0              # pending debits not yet settled
    future_flows: List[tuple] = field(default_factory=list)  # (date, signed_home_amount, event_id)
    normalized: List[NormalizedEvent] = field(default_factory=list)


def load_blank_amounts(path: str) -> Dict[str, dict]:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def _is_confirmed_income(ev: Event, user_history: List[Event],
                         evidenced_event_ids: Set[str]) -> bool:
    """Conservative confirmation policy for future income.

    A scheduled credit counts only when corroborated by one of:
      * explicit dataset marker: description says "confirmed" (all 47 rows),
      * attached evidence (image/message referencing the event),
      * an established settled income history for this user (>=2 rows),
        i.e. an ongoing payroll pattern.
    """
    if "confirm" in (ev.description or "").lower():
        return True
    if ev.event_id in evidenced_event_ids:
        return True
    # A settled income history for this user at all => monthly salary pattern.
    settled_income = [e for e in user_history
                      if e.direction == "credit" and e.status == "settled"
                      and e.category == ev.category and e.amount is not None]
    return len(settled_income) >= 2


def normalize_dataset(ds: Dataset, blank_path: str) -> Dict[str, CashView]:
    blanks = load_blank_amounts(blank_path)
    views: Dict[str, CashView] = {}

    # Pass 0: fill blank amounts from image evidence (currency stays the row's).
    filled: Dict[str, float] = {}
    for ev in ds.events:
        if ev.amount is None and ev.event_id in blanks:
            filled[ev.event_id] = float(blanks[ev.event_id]["amount"])

    for user_id, profile in ds.profiles.items():
        home = profile.home_currency
        user_events = ds.events_by_user.get(user_id, [])
        evidenced = {ev.event_id for ev in user_events if ev.event_id in blanks}
        evidenced |= {img.related_event_id for img in ds.images_by_user.get(user_id, [])
                      if img.related_event_id}

        view = CashView(user_id=user_id, home_currency=home,
                        opening_balance=profile.current_available_balance or 0.0)

        for ev in user_events:
            if ev.status in EXCLUDED_STATUSES or ev.direction == "non_cash":
                continue
            amount = ev.amount if ev.amount is not None else filled.get(ev.event_id)
            if amount is None:
                continue  # no evidence available -> cannot count it
            amount_home = amount if ev.currency == home else (
                ds.fx.convert(amount, ev.settlement_date or date(2026, 1, 15),
                              ev.currency, home) or 0.0)

            if ev.status == "pending":
                if ev.direction != "debit":
                    continue  # pending credits are unconfirmed income
                view.normalized.append(NormalizedEvent(
                    ev, amount_home, ev.settlement_date, reserve=True))
                view.reserved += amount_home
                continue

            if ev.status == "scheduled":
                if ev.settlement_date is None:
                    continue
                if ev.direction == "credit" and not _is_confirmed_income(
                        ev, user_events, evidenced):
                    continue
                ne = NormalizedEvent(ev, amount_home, ev.settlement_date)
                view.normalized.append(ne)
                sign = 1.0 if ev.direction == "credit" else -1.0
                view.future_flows.append((ev.settlement_date, sign * amount_home, ev.event_id))
                continue

            # settled rows are history; they only inform recurrence (Stage 3)
            view.normalized.append(NormalizedEvent(ev, amount_home, ev.settlement_date))

        # Exact-duplicate safety net: identical scheduled (type, direction,
        # amount, date) rows would double-count the same future movement.
        # Linked lifecycles (expense+refund, purchase+valuation, cancelled->
        # settled retries) are distinct cash events and are NOT deduped.
        seen_exact: Set[tuple] = set()
        for ne in view.normalized:
            if ne.reserve or ne.event.status != "scheduled":
                continue
            key = (ne.event.event_type, ne.event.direction, ne.amount_home, ne.settlement)
            if key in seen_exact:
                ne.duplicate_of = "exact_duplicate"
                view.future_flows = [f for f in view.future_flows if f[2] != ne.event.event_id]
            else:
                seen_exact.add(key)

        views[user_id] = view
    return views


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    ds = load_dataset(os.path.join(here, "..", "dataset"))
    views = normalize_dataset(ds, os.path.join(here, "evidence", "blank_amounts.json"))
    n_ev = sum(len(v.normalized) for v in views.values())
    n_res = sum(1 for v in views.values() for n in v.normalized if n.reserve)
    n_dupe = sum(1 for v in views.values() for n in v.normalized if n.duplicate_of)
    n_flow = sum(len(v.future_flows) for v in views.values())
    print(f"users: {len(views)}  normalized: {n_ev}  reserved: {n_res}  "
          f"dupes: {n_dupe}  future_flows: {n_flow}")
    print("blank amounts filled:", len(load_blank_amounts(os.path.join(here, 'evidence', 'blank_amounts.json'))))
