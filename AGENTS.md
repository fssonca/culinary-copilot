# Agent guidance

## Project
- Python, FastAPI, PostgreSQL, and uv. Application code lives in `src/culinary_copilot/`; dataset research tools live in `scripts/datasets/`.
- Keep application LLM workflows separate from the recipe-ingestion pipeline and its enablement settings.
- Read `README.md` and the relevant runbook: `docs/recipe-ingestion.md` or `docs/hybrid-ingestion.md`.

## Working conventions
- Implement the requested scope; preserve unrelated working-tree changes. Do not reset, clean, or stash them.
- Production modules must not import from `scripts/`. Prefer existing helpers over duplicate implementations.
- Keep secrets in `.env`; document configuration in `.env.example`. Never commit credentials, local datasets, model responses, or database backups.
- Commit, push, deploy, make paid calls, or mutate the application database only when authorized by the task. Existing authorization does not require repeated confirmation.

## Data integrity
- Identify recipes by `(dataset_id, source_id)`; preserve exact IDs, source evidence, provenance, and duplicate aliases.
- Unknown quantities, units, dietary compatibility, and nutrition remain unknown. Do not invent values or promote recipe capabilities without evidence.
- Never weaken validation merely to pass tests. Diagnose whether the fixture or implementation violates the intended contract.
- Keep applied SQL migrations byte-for-byte unchanged; add a new ordered migration for schema changes.
- Preserve recovery artifacts. Before an authorized application migration, verify the target and a recoverable backup.

## Verification
- Run `make check` (Ruff, formatting, mypy for `src/` and `scripts/`, tests and evals).
- Default tests must not require model credentials or live provider calls. Database write tests must use explicitly identified disposable databases, never the application database.
- Report checks actually executed, skipped checks, and remaining failures. Distinguish mocked results from live evaluation and disposable test writes from application writes.
- Update relevant documentation when behavior or commands change; label historical findings rather than presenting them as current status.
