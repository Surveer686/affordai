"""Stage 1: Dataset loading, indexing, and dated FX conversion.

All CSVs from `dataset/` are loaded into typed dataclasses and indexed by the
join keys used across the challenge:
  - user_id       -> profile, events, messages, images
  - request_id    -> requests, payment options, messages, images
  - related_event_id -> messages / images that describe a financial event

FX: `exchange_rates.csv` supplies fixed, dated rates (mostly monthly, on the
15th). Rates are provided in a few directions (USD -> EUR/IDR/INR/ZAR,
EUR -> USD/ZAR). Any other pair is resolved by walking the currency graph
(e.g. EUR -> USD -> IDR). Each edge uses the rate row selected for the same
target date, so a conversion is always consistent with one rate date.

Rate-date selection policy (switchable; validated against sample_requests.csv):
  - "as_of"   : latest rate_date <= the event's settlement date
  - "nearest" : rate_date closest to the settlement date
"""

from __future__ import annotations

import csv
import os
from bisect import bisect_right
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

@dataclass
class Profile:
    user_id: str
    home_currency: str
    current_available_balance: float
    minimum_balance_to_keep: float
    financial_priorities: List[str]
    protected_categories: List[str]
    categories_willing_to_reduce: List[str]
    categories_willing_to_stop: List[str]
    payment_methods_will_consider: List[str]
    max_installment_months: Optional[int]  # None => user will not consider installments

    def accepts(self, method: str) -> bool:
        return method in self.payment_methods_will_consider


@dataclass
class Event:
    event_id: str
    user_id: str
    event_type: str  # expense|subscription|income|debt_payment|investment_purchase|refund|investment_valuation|investment_sale
    description: str
    category: str
    direction: str  # debit|credit|non_cash
    amount: Optional[float]  # None when blank in the CSV -> must come from image evidence
    currency: str
    event_date: date
    settlement_date: Optional[date]  # blank in CSV for 10 unrealized valuations
    status: str  # settled|pending|scheduled|cancelled|failed|unrealized
    linked_event_id: str  # "" when absent
    flexibility: str  # fixed|reducible|stoppable|reducible_or_stoppable ("" for income/non-flex rows)
    minimum_allowed_amount: Optional[float]  # floor for reduce_to on reducible events

    @property
    def is_flexible(self) -> bool:
        return self.flexibility in ("reducible", "stoppable", "reducible_or_stoppable")

    def can_stop(self) -> bool:
        return self.flexibility in ("stoppable", "reducible_or_stoppable")

    def can_reduce(self) -> bool:
        return self.flexibility in ("reducible", "reducible_or_stoppable")


@dataclass
class Request:
    request_id: str
    user_id: str
    request_date: date
    request_type: str
    requested_amount: float
    desired_completion_date: date
    allows_partial_payment: bool
    request_text: str


@dataclass
class PaymentOption:
    payment_option_id: str
    request_id: str
    payment_method: str  # full_payment|installments
    payment_amount: float  # per-payment amount
    number_of_payments: int
    first_payment_date: date
    payment_frequency_days: Optional[int]
    financing_fee: float
    total_payable_amount: float

    def schedule(self) -> List[Tuple[date, float]]:
        """Concrete payment schedule. The last payment absorbs any rounding
        residue so the payments sum exactly to total_payable_amount."""
        n = self.number_of_payments
        if n <= 0:
            return []
        first = self.first_payment_date
        freq = self.payment_frequency_days or 0
        pays: List[Tuple[date, float]] = []
        for k in range(n - 1):
            pays.append((date.fromordinal(first.toordinal() + freq * k), self.payment_amount))
        last_amount = round(self.total_payable_amount - self.payment_amount * (n - 1), 2)
        pays.append((date.fromordinal(first.toordinal() + freq * (n - 1)), last_amount))
        return pays


@dataclass
class Message:
    message_id: str
    user_id: str
    request_id: str  # "" when user-level
    related_event_id: str  # populated only when the message directly describes one event row
    sent_at: datetime
    source_type: str  # employer|bank|service_provider|merchant|financial_service
    text: str


@dataclass
class ImageRef:
    image_id: str
    user_id: str
    request_id: str
    related_event_id: str
    path: str


# --------------------------------------------------------------------------
# FX
# --------------------------------------------------------------------------

class FxTable:
    """Dated FX rates with graph traversal for pairs not directly quoted."""

    def __init__(self, rows: List[Tuple[date, str, str, float]]):
        # (from, to) -> sorted list of (rate_date, rate)
        self._series: Dict[Tuple[str, str], List[Tuple[date, float]]] = {}
        self._currencies: set[str] = set()
        for d, fc, tc, rate in rows:
            self._series.setdefault((fc, tc), []).append((d, rate))
            self._currencies.add(fc)
            self._currencies.add(tc)
        for key in self._series:
            self._series[key].sort(key=lambda x: x[0])

    @classmethod
    def load(cls, path: str) -> "FxTable":
        rows: List[Tuple[date, str, str, float]] = []
        with open(path, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                rows.append((date.fromisoformat(r["rate_date"]), r["from_currency"], r["to_currency"], float(r["rate"])))
        return cls(rows)

    def _edge_rate(self, from_c: str, to_c: str, d: date, policy: str) -> Optional[float]:
        """Direct quoted rate for (from_c -> to_c) at date d, else the inverse."""
        direct = self._series.get((from_c, to_c))
        inverse = self._series.get((to_c, from_c))
        if direct:
            return self._pick(direct, d, policy)
        if inverse:
            inv = self._pick(inverse, d, policy)
            if inv:
                return 1.0 / inv
        return None

    @staticmethod
    def _pick(series: List[Tuple[date, float]], d: date, policy: str) -> Optional[float]:
        dates = [x[0] for x in series]
        if policy == "as_of":
            idx = bisect_right(dates, d)
            return series[idx - 1][1] if idx > 0 else (series[0][1] if series else None)
        # nearest by absolute day distance; ties -> earlier date
        best = min(series, key=lambda x: (abs((x[0] - d).days), x[0]))
        return best[1]

    def convert(self, amount: float, d: date, from_c: str, to_c: str, policy: str = "as_of") -> Optional[float]:
        """Convert amount on date d from from_c to to_c via BFS over the graph."""
        if from_c == to_c:
            return amount
        if from_c not in self._currencies or to_c not in self._currencies:
            return None
        # BFS; all paths through this small hub graph should agree
        visited = {from_c}
        q: deque[Tuple[str, float]] = deque([(from_c, 1.0)])
        while q:
            cur, acc = q.popleft()
            for nxt in sorted(self._currencies):
                if nxt in visited:
                    continue
                r = self._edge_rate(cur, nxt, d, policy)
                if r is None:
                    continue
                acc2 = acc * r
                if nxt == to_c:
                    return amount * acc2
                visited.add(nxt)
                q.append((nxt, acc2))
        return None


# --------------------------------------------------------------------------
# Dataset container
# --------------------------------------------------------------------------

@dataclass
class Dataset:
    profiles: Dict[str, Profile]
    events: List[Event]
    events_by_id: Dict[str, Event]
    events_by_user: Dict[str, List[Event]]
    requests: List[Request]
    requests_by_id: Dict[str, Request]
    options_by_request: Dict[str, List[PaymentOption]]
    messages: List[Message]
    messages_by_user: Dict[str, List[Message]]
    messages_by_request: Dict[str, List[Message]]
    messages_by_event: Dict[str, List[Message]]
    images_by_id: Dict[str, ImageRef]
    images_by_user: Dict[str, List[ImageRef]]
    images_by_request: Dict[str, List[ImageRef]]
    images_by_event: Dict[str, List[ImageRef]]
    fx: FxTable
    dataset_dir: str

    # -- helpers ------------------------------------------------------------

    def to_home(self, amount: float, d: date, from_currency: str, user_id: str,
                policy: str = "as_of") -> Optional[float]:
        prof = self.profiles[user_id]
        return self.fx.convert(amount, d, from_currency, prof.home_currency, policy)


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

def _split_list(value: str) -> List[str]:
    value = (value or "").strip()
    if not value:
        return []
    return [p.strip() for p in value.split("|") if p.strip()]


def _parse_opt_int(value: str) -> Optional[int]:
    value = (value or "").strip()
    return int(value) if value else None


def _parse_amount(value: str) -> Optional[float]:
    value = (value or "").strip()
    return float(value) if value else None


def _parse_date(value: str) -> date:
    return date.fromisoformat((value or "").strip())


def _parse_opt_date(value: str) -> Optional[date]:
    value = (value or "").strip()
    return date.fromisoformat(value) if value else None


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat((value or "").strip().replace("Z", "+00:00"))


# --------------------------------------------------------------------------
# Loaders
# --------------------------------------------------------------------------

def load_profiles(path: str) -> Dict[str, Profile]:
    profiles: Dict[str, Profile] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            p = Profile(
                user_id=r["user_id"],
                home_currency=r["home_currency"],
                current_available_balance=float(r["current_available_balance"]),
                minimum_balance_to_keep=float(r["minimum_balance_to_keep"]),
                financial_priorities=_split_list(r["financial_priorities"]),
                protected_categories=_split_list(r["expense_categories_to_protect"]),
                categories_willing_to_reduce=_split_list(r["expense_categories_user_is_willing_to_reduce"]),
                categories_willing_to_stop=_split_list(r["expense_categories_user_is_willing_to_stop"]),
                payment_methods_will_consider=_split_list(r["payment_methods_user_will_consider"]),
                max_installment_months=_parse_opt_int(r["max_installment_months"]),
            )
            profiles[p.user_id] = p
    return profiles


def load_events(path: str) -> List[Event]:
    events: List[Event] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            events.append(Event(
                event_id=r["event_id"],
                user_id=r["user_id"],
                event_type=r["event_type"],
                description=r["description"],
                category=r["category"],
                direction=r["direction"],
                amount=_parse_amount(r["amount"]),
                currency=r["currency"],
                event_date=_parse_date(r["event_date"]),
                settlement_date=_parse_opt_date(r["settlement_date"]),
                status=r["status"],
                linked_event_id=(r.get("linked_event_id") or "").strip(),
                flexibility=(r.get("flexibility") or "").strip(),
                minimum_allowed_amount=_parse_amount(r.get("minimum_allowed_amount", "")),
            ))
    return events


def load_requests(path: str) -> List[Request]:
    requests: List[Request] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            requests.append(Request(
                request_id=r["request_id"],
                user_id=r["user_id"],
                request_date=_parse_date(r["request_date"]),
                request_type=r["request_type"],
                requested_amount=float(r["requested_amount"]),
                desired_completion_date=_parse_date(r["desired_completion_date"]),
                allows_partial_payment=(r["allows_partial_payment"].strip().lower() == "true"),
                request_text=r["request_text"],
            ))
    return requests


def load_payment_options(path: str) -> Dict[str, List[PaymentOption]]:
    by_request: Dict[str, List[PaymentOption]] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            opt = PaymentOption(
                payment_option_id=r["payment_option_id"],
                request_id=r["request_id"],
                payment_method=r["payment_method"],
                payment_amount=float(r["payment_amount"]),
                number_of_payments=int(r["number_of_payments"]),
                first_payment_date=_parse_date(r["first_payment_date"]),
                payment_frequency_days=_parse_opt_int(r["payment_frequency_days"]),
                financing_fee=float(r["financing_fee"] or 0),
                total_payable_amount=float(r["total_payable_amount"]),
            )
            by_request.setdefault(opt.request_id, []).append(opt)
    for opts in by_request.values():
        opts.sort(key=lambda o: o.payment_option_id)
    return by_request


def load_messages(path: str) -> List[Message]:
    messages: List[Message] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            messages.append(Message(
                message_id=r["message_id"],
                user_id=r["user_id"],
                request_id=(r.get("request_id") or "").strip(),
                related_event_id=(r.get("related_event_id") or "").strip(),
                sent_at=_parse_datetime(r["sent_at"]),
                source_type=r["source_type"],
                text=r["message_text"],
            ))
    return messages


def load_images(path: str, media_dir: str) -> List[ImageRef]:
    images: List[ImageRef] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            images.append(ImageRef(
                image_id=r["image_id"],
                user_id=r["user_id"],
                request_id=(r.get("request_id") or "").strip(),
                related_event_id=(r.get("related_event_id") or "").strip(),
                path=os.path.join(media_dir, f"{r['image_id']}.png"),
            ))
    return images


def load_dataset(dataset_dir: str) -> Dataset:
    profiles = load_profiles(os.path.join(dataset_dir, "financial_profiles.csv"))
    events = load_events(os.path.join(dataset_dir, "financial_events.csv"))
    requests = load_requests(os.path.join(dataset_dir, "requests.csv"))
    options_by_request = load_payment_options(os.path.join(dataset_dir, "request_payment_options.csv"))
    messages = load_messages(os.path.join(dataset_dir, "messages.csv"))
    images = load_images(os.path.join(dataset_dir, "images.csv"),
                         os.path.join(dataset_dir, "media", "images"))
    fx = FxTable.load(os.path.join(dataset_dir, "exchange_rates.csv"))

    events_by_user: Dict[str, List[Event]] = {}
    events_by_id: Dict[str, Event] = {}
    for e in events:
        events_by_user.setdefault(e.user_id, []).append(e)
        events_by_id[e.event_id] = e

    messages_by_user: Dict[str, List[Message]] = {}
    messages_by_request: Dict[str, List[Message]] = {}
    messages_by_event: Dict[str, List[Message]] = {}
    for m in messages:
        messages_by_user.setdefault(m.user_id, []).append(m)
        if m.request_id:
            messages_by_request.setdefault(m.request_id, []).append(m)
        if m.related_event_id:
            messages_by_event.setdefault(m.related_event_id, []).append(m)

    images_by_id: Dict[str, ImageRef] = {}
    images_by_user: Dict[str, List[ImageRef]] = {}
    images_by_request: Dict[str, List[ImageRef]] = {}
    images_by_event: Dict[str, List[ImageRef]] = {}
    for img in images:
        images_by_id[img.image_id] = img
        images_by_user.setdefault(img.user_id, []).append(img)
        if img.request_id:
            images_by_request.setdefault(img.request_id, []).append(img)
        if img.related_event_id:
            images_by_event.setdefault(img.related_event_id, []).append(img)

    return Dataset(
        profiles=profiles,
        events=events,
        events_by_id=events_by_id,
        events_by_user=events_by_user,
        requests=requests,
        requests_by_id={r.request_id: r for r in requests},
        options_by_request=options_by_request,
        messages=messages,
        messages_by_user=messages_by_user,
        messages_by_request=messages_by_request,
        messages_by_event=messages_by_event,
        images_by_id=images_by_id,
        images_by_user=images_by_user,
        images_by_request=images_by_request,
        images_by_event=images_by_event,
        fx=fx,
        dataset_dir=dataset_dir,
    )


# --------------------------------------------------------------------------
# Smoke test
# --------------------------------------------------------------------------

if __name__ == "__main__":
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ds = load_dataset(os.path.join(root, "dataset"))
    print(f"profiles={len(ds.profiles)} events={len(ds.events)} requests={len(ds.requests)}")
    print(f"option-requests={len(ds.options_by_request)} messages={len(ds.messages)} images={len(ds.images_by_id)}")
    rate_dates = [r["rate_date"] for r in csv.DictReader(open(os.path.join(root, "dataset", "exchange_rates.csv")))]
    print(f"fx rows={len(rate_dates)} range={min(rate_dates)}..{max(rate_dates)}")

    # FX sanity: EUR -> IDR and USD -> ZAR via graph, on a known rate date
    eur_idr = ds.fx.convert(1000.0, date(2025, 3, 15), "EUR", "IDR")
    usd_zar = ds.fx.convert(100.0, date(2025, 3, 15), "USD", "ZAR")
    inr_inr = ds.fx.convert(5.0, date(2025, 3, 15), "INR", "INR")
    print(f"EUR1000->IDR@2025-03-15 = {eur_idr} (expect 1000*(1/0.92)*15833.33={1000*(1/0.92)*15833.33:.2f})")
    print(f"USD100->ZAR@2025-03-15  = {usd_zar} (expect 100*0.92*20={100*0.92*20:.2f})")
    print(f"INR->INR = {inr_inr}")

    # as-of policy: settlement 2024-06-04 should use the 2024-05-15 row
    v = ds.fx.convert(1.0, date(2024, 6, 4), "USD", "INR", policy="as_of")
    print(f"USD->INR@2024-06-04 as_of = {v} (expect 83.33)")
    v2 = ds.fx.convert(1.0, date(2024, 6, 4), "USD", "INR", policy="nearest")
    print(f"USD->INR@2024-06-04 nearest = {v2} (expect 83.33)")
