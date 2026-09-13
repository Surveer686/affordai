"""Stage 4 (light) - evidence facts from employer payroll messages.

Deterministic regex extraction for the two message patterns that materially
change the 90-day forecast:

  1. salary increase  "Gaji bulanan Anda naik menjadi IDR 42750000. Perubahan
     ini berlaku mulai 2025-08-15." / "Your monthly salary is increasing to
     IDR 17,290,000 effective 15 July 2026."
        -> {"type": "salary_change", ...} applied on/after from_date

  2. salary resume    "Regular salary of EUR 2717 resumes on 2025-08-15."
        -> {"type": "salary_change", ...} (re-anchors an existing stream)

Amounts are converted to the user's home currency at the effective date via
the dataset FX table. All other message types (childcare announcements without
amounts, service-provider notes, one-off arrears) are intentionally NOT
interpreted here - the full LLM pass (Stage 4 complete) handles the rest.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import List, Optional

from data import Dataset

AMOUNT_RE = r"([A-Z]{3})\s*([0-9][0-9.,]*)"
DATE_RE = r"(\d{4}-\d{2}-\d{2})"


def _parse_amount(raw: str) -> float:
    return float(raw.replace(",", ""))


def _to_home(ds: Dataset, user_id: str, currency: str, amount: float,
             d: date) -> Optional[float]:
    home = ds.profiles[user_id].home_currency
    if currency == home:
        return round(amount, 2)
    conv = ds.fx.convert(amount, d, currency, home)
    return round(conv, 2) if conv is not None else None


def extract_facts(ds: Dataset, user_id: str, request_date: date) -> List[dict]:
    facts: List[dict] = []
    for m in ds.messages_by_user.get(user_id, []):
        if m.source_type != "employer" or m.sent_at.date() > request_date:
            continue
        text = m.text or ""
        low = text.lower()
        amt = re.search(AMOUNT_RE, text)
        if not amt:
            continue
        currency, amount_raw = amt.group(1), amt.group(2)
        try:
            amount = _parse_amount(amount_raw)
        except ValueError:
            continue

        # effective / resume date: explicit ISO date near the amount sentence
        dmatch = re.search(DATE_RE, text)
        eff_date = date.fromisoformat(dmatch.group(1)) if dmatch else None

        is_increase = ("naik menjadi" in low or "increasing to" in low
                       or "salary of" in low and "resumes" in low
                       or "first salary" in low)
        if is_increase and eff_date and eff_date >= request_date:
            home_amt = _to_home(ds, user_id, currency, amount, eff_date)
            if home_amt:
                facts.append({"type": "salary_change", "amount_home": home_amt,
                              "from_date": eff_date, "currency": currency,
                              "source": m.message_id})
    return facts
