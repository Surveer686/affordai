# Buy or Wait? — Work Log & Architecture

**Challenge:** HackerRank Orchestrate — AI-powered financial decision agent
**Deadline:** 2026-09-13 18:00 IST · **Team:** affordai
**Result:** deterministic end-to-end pipeline producing a contract-valid `output.csv` for all 250 requests.

---

## 1. What we built

A financial decision agent that, for each of 250 payment requests:

1. Reconstructs the user's financial state from ~25K events, 275 profiles, payment options, 215 messages and 16 receipt images.
2. Detects recurring income/expense series from settled history.
3. Forecasts the balance 90 days forward from the request date.
4. Searches payment plans (full / installments / partial / wait / spending changes).
5. Emits the 8-column recommendation row, validated against the full spec contract.

**Core principle:** every scored number comes from deterministic Python. No LLM calls in the final run (zero tokens, exactly reproducible). Untrusted free text is handled by narrow, typed extractors.

---

## 2. Pipeline structure (modules & data flow)

```
dataset/*.csv + media/images/*.png
        │
        ▼
┌──────────────────────────┐
│ code/data.py             │  Stage 1: typed loaders + indexes + FX graph
│  - Profile/Event/Request │  events_by_user, messages_by_*, images_by_*
│  - PaymentOption.schedule│  options_by_request
│  - FxTable.convert       │  dated rates, direct+inverse quotes,
│  - Dataset.to_home()     │  deterministic BFS multi-hop (USD hub)
└──────────┬───────────────┘
           ▼
┌──────────────────────────┐
│ code/evidence/           │  Stage 2 evidence: 16 blank amounts read from
│  blank_amounts.json      │  receipt PNGs; checked in as derived data
└──────────┬───────────────┘
           ▼
┌──────────────────────────┐
│ code/normalize.py        │  Stage 2: raw rows → cash-flow view
│  EXCLUDED_STATUSES       │  cancelled/failed/unrealized/non_cash dropped
│  pending debit  → reserve│  money owed now; pending credit → ignored
│  scheduled credit → only │  "confirm" marker / evidence / ≥2 history
│  scheduled debit  → flow │  dated future outflow
│  exact-duplicate guard   │  scheduled double-count safety net
└──────────┬───────────────┘
           ▼
┌──────────────────────────┐   ┌───────────────────────────┐
│ code/recurrence.py       │◀──│ code/evidence.py          │
│  Stage 3: series detect  │   │ Stage 4 (light): employer │
│  cluster → cadence →     │   │ payroll messages (EN/ID)  │
│  project 90d + salary    │   │ salary increase/resume →  │
│  anchor-repeat           │   │ typed facts               │
│  apply_facts() hook      │   └───────────────────────────┘
└──────────┬───────────────┘
           ▼
┌──────────────────────────┐
│ code/simulate.py         │  Stage 5: balance path + safety
│  start = balance - resv  │  end-of-day walk, 90-day window
│  max_safe_today: CLOSED- │  p ≤ B0 − min + min_d S(d)  (algebra, no search)
│  FORM                    │  earliest_safe_date()
│  Adjustment(stop/reduce) │  mutates projected flexible series only
└──────────┬───────────────┘
           ▼
┌──────────────────────────┐
│ code/decision.py         │  Stage 6: candidates + ranking
│  installments > partial >│  (reverse-engineered from 25 samples)
│  full > changes > wait > │  output formatting helpers
│  not_recommended         │  explanation templates
└──────────┬───────────────┘
           ▼
┌──────────────────────────┐      ┌────────────────────────┐
│ code/run_full.py         │ ───▶ │ ../output.csv (250)    │
│ code/validate.py         │ ───▶ │ contract check: OK     │
└──────────────────────────┘      └────────────────────────┘
```

Support: `code/evaluation/usage_report.md` (token accounting), `log.txt` (AGENTS.md session log), tuning/diagnostic scripts (excluded from the submission zip).

---

## 3. Workflow architecture, step by step

### Stage 1 — Data loaders + FX graph (`data.py`)
- Typed dataclasses for all 6 CSVs; indexes: `events_by_user`, `events_by_id`, `options_by_request`, `messages_by_user/request/event`, `images_by_user/request/event`.
- `PaymentOption.schedule()` builds concrete installment dates; last leg absorbs rounding so payments sum exactly to `total_payable_amount`.
- `FxTable`: rates quoted on the 15th; direct + inverse quotes; BFS multi-hop via USD hub with deterministic tie-breaking. (Analysis showed all 140 cross-currency events have direct rows and constant rates — FX risk ≈ 0.)
- Verified: 275 profiles · 25,342 events · 250 requests · 215 messages · 16 images.

### Stage 2 — Event normalization (`normalize.py` + `evidence/blank_amounts.json`)
- Read all 16 receipt images; extracted the 16 blank amounts with provenance (payslip net pay, rent balance due, invoices with tax breakdowns, handwritten bill…). One image (event_1700) is physically cut off — used the visible Item Bill total; delta ≈ ₹16 delivery fee.
- Inclusion rules: `cancelled`/`failed`/`unrealized`/`non_cash` dropped; **pending debits → reserves** (reduce safe-to-pay now); pending credits ignored; scheduled credits need confirmation ("confirm" in description — true for all 47 rows — or attached evidence or ≥2 settled payroll history); scheduled debits → dated flows.
- Investigated `linked_event_id`: the 58 linked pairs are **distinct cash events** (expense+refund, purchase+sale, cancelled→settled retry, failed→rescheduled). Deduping by link would delete refunds — so: status rules count each once, plus an exact-duplicate safety net for scheduled rows.
- Result: 25,281 normalized · 63 reserved · 70 confirmed future flows · 16 blanks filled.

### Stage 3 — Recurrence + 90-day forecast (`recurrence.py`)
- Group settled cash rows per (direction, category[, description for credits]); split clusters at >60-day gaps; qualify ≥3 rows with last row within 45 days of request date; cadence from median gap: weekly 5–9d, biweekly 10–22d, monthly 23–40d.
- Projection: expenses at cluster **median** (grid-searched vs max — median maximized sample agreement), income at **median**; monthly at mode day-of-month (clamped to month length); weekly/biweekly anchored on last observed row.
- Dedup: projected occurrence dropped when an explicit same-sign same-category flow exists within ±5 days (explicit scheduled rows win — they may carry amended amounts).
- Income robustness: description-split with category-level fallback (freelancers have unique descriptions on every row); "Final employer payroll" ends a stream.
- Salary anchor-repeat: a scheduled "Next confirmed salary" implies an ongoing monthly stream — repeated forward (this alone fixed request_01 to gap 0.00 vs truth).
- `apply_facts()` typed hook: `salary_change`, `confirmed_income`, `cancellation`, `delay` — series/fact flows only; explicit event rows never mutated.

### Stage 4 — Evidence extraction (`evidence.py`, light)
- Deterministic regex over employer payroll messages (English + Indonesian): "Gaji bulanan Anda naik menjadi IDR 42750000 … berlaku mulai 2025-08-15" / "monthly salary is increasing to …" / "Regular salary of EUR 2717 resumes on …".
- Converts to home currency at the effective date via the FX table; feeds `salary_change` facts.
- Known scope cut: service-provider "client approved invoice payment" confirmations not harvested (a few users slightly under-credited; accepted trade-off).
- Trap avoided: 8 "new recurring childcare" messages carry **no amount** — distractors; never invented numbers.

### Stage 5 — Balance simulator (`simulate.py`)
- Start = `current_available_balance − reserved`; apply flows end-of-day; safe iff every day in [request_date, +90d] ≥ `minimum_balance_to_keep`.
- `max_safe_today` is **closed-form**: `p ≤ B0 − min_balance + min_d S(d)` — exact, no binary search, floored at 0, capped at requested amount.
- `earliest_safe_date`: first day a single full payment keeps the path safe, within `min(deadline, horizon)`.
- `Adjustment(stop/reduce)` applies only to projected flexible series; used by the plan search.
- Cross-check: `max_safe ⇒ simulate(ok)` invariant held on all 250 requests.

### Stage 6 — Decision engine (`decision.py`)
- Candidate builders: installment options (exact schedule safe + completes by deadline), full payment (no changes), full + spending changes (1–2 changes: stops first, then reduces with floor-respecting minimum), partial (today's safe amount + remainder at earliest-safe date), wait, not_recommended.
- Ranking reverse-engineered from the 25 solved samples: **installments > partial > full > full+changes > wait > not_recommended** (req_12: installments chosen even though full was safe today).
- Spec gates: `affordable_now` requires profile accepts `full_payment`; partial requires `allows_partial_payment` + accepts `partial_payment` + `0 < safe < requested`; partial `earliest` column = second leg date.
- Sample-tuned formatting: safe amount strips all trailing zeros (`17229139.2`), plan legs keep 2dp unless integral (`620.40`, `13110000`), explanations use thousands separators with 2dp (`EUR 996.60`).
- Sample accuracy (frozen): **16/25 status, 16/25 method**; all 6 `not_affordable` correct; all wait/installment majors correct.

### Stage 7 — Validator (`validate.py`)
Enforces the full contract before anything is submitted: header + 8 columns, row order = `requests.csv` order, `0 ≤ safe ≤ requested`, enums, chronological legs completing by deadline, installments exactly match a supplied option, partial rules (2 legs, sum = requested, leg1 = safe @ request_date, earliest = leg2), earliest empty for `not_affordable` / = request_date for `affordable_now` / within 90-day window otherwise, ≤3 spending changes referencing the user's flexible events, non-empty explanations. **Caught a real bug:** lexicographic sort put `request_100` before `request_26`.

### Stage 8 — Sample iteration (methodology)
- Differential diag harness per sample (truth vs my safe, flow aggregates by source).
- Targeted exact solvers (user_13 trough fingerprint), global knob grid-search.
- Adopted: median expense stat, median income stat, salary anchor-repeat, freelance fallback, method ranking.
- Frozen at diminishing returns; status correctness prioritized over cent-exact amounts.

### Stage 9 — Full run (`run_full.py`)
- All 250 requests in file order → root `output.csv` → validator: **OK**.
- Distribution: 52 `affordable_now` · 59 `affordable_with_plan` · 23 `affordable_later` · 116 `not_affordable`.
- `code/evaluation/usage_report.md`: truthful zero-token accounting (deterministic final run; image amounts checked in as derived evidence data; message facts via regex).

---

## 4. Key decisions & rationale

| Decision | Rationale |
|---|---|
| Deterministic core, no LLM in the run | Reproducibility, zero cost, no hallucination risk on scored numbers |
| Median (not max) expense projection | Grid-searched on 25 samples: better status/method agreement |
| Linked events NOT deduped | They're distinct cash events (refunds!); status rules already count once |
| Pending debits reserved upfront | Spec: safety must account for money about to leave |
| Scheduled credits need confirmation | Conservative; all 47 rows carry the dataset's "confirm" marker |
| Installments > partial ranking | req_12/req_19 evidence from solved samples |
| Freeze sample tuning at 16/25 | Exact-decimal chase hit diminishing returns; ship complete pipeline |

## 5. Deliverables status

| Item | Status |
|---|---|
| `output.csv` (250 rows, root) | ✅ written, validator OK |
| `code.zip` (code + README + evaluation/) | ⬜ packaging remaining |
| `chat_transcript` | ⬜ export from tool |
| `log.txt` (AGENTS.md requirement) | ✅ maintained every turn |
| `code/evaluation/usage_report.md` | ✅ complete |
