# Phase 4 Model 5 fixtures

Shared HTTP examples Ex-1, Ex-2 and Ex-4 from the Phase 4 Implementation Specification
section 4.7. The JSON file is data only; run it with:

```bash
python -m tools.run_model5 --base-url "$BASE_URL"
```

Environment (same names as the HTTP acceptance runner):

| Variable | Role |
| --- | --- |
| `BASE_URL` | Service under test |
| `INGEST_KEY` / `INGEST_KEY_SYS_42` | System key for `sys-42` |
| `REVIEWER_KEY` | Reviewer |
| `VIEWER_KEY` | Viewer (`analyst1` in Model 5) |
| `WEBHOOK_SINK_URL` | Optional; substituted into webhook registration bodies |

Seed mapping (Model 5 → this service): loan-scorer / resume-ranker → `sys-42`; analyst1 →
viewer-1. Values `0.61` (accuracy) and `0.34` (accuracy_drift) are kept.

These examples need Parts 1–3 real code (disposition, review start, report guard). On a part
branch they are marked integration and skipped unless `--run-integration` is set.
