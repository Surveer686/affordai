# Token Usage and Cost Analysis

Final full-dataset run producing `output.csv` (250 requests, 2026-09-13).

## Architecture summary

The submitted system is **fully deterministic** for the scored run:

- Financial-state reconstruction, recurrence detection, the 90-day simulator,
  plan search, ranking, and explanation generation are pure Python
  (`code/data.py`, `code/normalize.py`, `code/recurrence.py`,
  `code/simulate.py`, `code/decision.py`).
- The 16 blank event amounts were recovered from the receipt images in
  `dataset/media/images/` and are checked in as derived evidence data at
  `code/evidence/blank_amounts.json` (with per-event provenance notes), so the
  run itself requires no model calls.
- Payroll-message facts (salary increases / resumes, English and Indonesian)
  are extracted by a deterministic regex module (`code/evidence.py`).

## Model usage of the final run

| Model / provider | Calls | Input tokens | Output tokens | Est. cost (USD) |
|---|---:|---:|---:|---:|
| none (deterministic pipeline) | 0 | 0 | 0 | 0.00 |
| **Total** | **0** | **0** | **0** | **0.00** |

- Average tokens per request: 0 in / 0 out
- Average cost per request: USD 0.00

No LLM APIs were invoked while producing `output.csv`: every scored number is
computed by the deterministic engine, which also makes the run exactly
reproducible and free.

## Development-time model usage (not part of the final run)

During development we used an assistant coding agent (GLM, Z.ai) via the
Codebuff/Freebuff environment for code authoring and image reading of the 16
receipts. Those tokens are conversation overhead, not part of the solution's
inference path, and are therefore excluded from the per-request accounting
above per the report's scope ("the final full-dataset run that produced
output.csv").
