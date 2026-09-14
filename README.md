# Buy or Wait? — AffordAI

An AI-powered financial decision agent for the HackerRank Orchestrate challenge (September 2026). For each of 250 payment requests it reconstructs the user's finances, forecasts 90 days ahead, and recommends the safest way to pay — full now, installments, partial, wait, or don't.

**Deterministic by design:** every scored number in `output.csv` is computed by plain Python — the final run makes zero model calls, costs zero tokens, and is exactly reproducible. Untrusted free text is handled by narrow, typed extractors only.

---

## Quick start

```bash
python code/main.py
```

Reads `dataset/`, writes `output.csv` (250 rows) at the repository root, and validates it against the full output contract. No third-party packages required (Python 3.10+ standard library only).

---

## How it works

```
dataset/*.csv + media/images/*.png
        │
        ▼
data.py        typed loaders, indexes, dated FX graph (BFS via USD hub)
        │
normalize.py   cash-flow rules: drop cancelled/failed/unrealized,
        │      reserve pending debits, confirm scheduled income,
        │      fill the 16 blank amounts from receipt evidence
        ▼
recurrence.py  recurring-series detection (weekly/biweekly/monthly),
        │      conservative 90-day projection, salary anchor-repeat,
        │      message facts applied via typed hook
        ▼
simulate.py    end-of-day balance walk; max_safe_today in CLOSED FORM:
        │      p ≤ B0 − min_balance + min_d S(d)
        ▼
decision.py    plan search + sample-derived ranking:
        │      installments > partial > full > changes > wait > not_recommended
        ▼
validate.py    full contract check (bounds, enums, plan rules, deadlines)
        │
        ▼
output.csv     250 rows, validation: OK
```

### Highlights

- **Image evidence:** the 16 events with blank amounts were recovered from their receipt images (payslip net pay, balance-due lines, tax-inclusive totals) and checked in as derived data with per-event provenance (`code/evidence/blank_amounts.json`).
- **Recurrence with confirmation policy:** scheduled income counts only when the dataset's own "confirmed" marker, attached evidence, or an established payroll history corroborates it; a scheduled "Next confirmed salary" anchors an ongoing monthly stream; "Final employer payroll" ends one.
- **Linked events are distinct cash flows:** expense+refund pairs, purchase+sale, cancelled→settled retries are each counted exactly once via status rules — never deduped away.
- **Closed-form safety:** the largest safe payment today is exact algebra over the cumulative flow curve, not a binary search.
- **Sample-derived conventions:** the 25 solved examples pinned the ranking order, number formatting (`17229139.2` vs `620.40`), and column semantics (e.g., partial's `earliest_date_for_full_payment` = second leg date).

## Repository layout

```
code/
  main.py          entry point: python code/main.py
  data.py          loaders, models, FX conversion
  normalize.py     Stage 2 cash-flow normalization
  recurrence.py    Stage 3 series detection + forecast
  evidence.py      Stage 4 (regex) payroll-message facts
  simulate.py      Stage 5 simulator + safety checks
  decision.py      Stage 6 plan search + ranking + explanations
  validate.py      Stage 7 output contract validator
  run_full.py      Stage 9 full 250-request run
  evidence/        checked-in image-derived amounts
  evaluation/      usage_report.md (token accounting)
  devtools/        tuning/diagnostic scripts used during development
dataset/           challenge inputs (unchanged)
output.csv         generated predictions (250 rows)
log.txt            development session log (per AGENTS.md)
work.md            detailed architecture & work log
```

## Accuracy

Tuned on the 25 solved sample requests: **16/25** exact `affordability_status` and **16/25** exact `recommended_payment_method` matches, with all `not_affordable` cases correct. Residual gaps trace to the reference forecast's exact recurring-amount statistic; our projection uses medians (conservative) — see `work.md` for the full tuning methodology.

## Token usage

The scored run is fully deterministic: **0 model calls, 0 tokens, USD 0.00**. See [`code/evaluation/usage_report.md`](./code/evaluation/usage_report.md).

---

Read [`problem_statement.md`](./problem_statement.md) for the challenge spec, [`work.md`](./work.md) for the step-by-step build log and architecture rationale, and [`log.txt`](./log.txt) for the session timeline.
