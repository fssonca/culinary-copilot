# AI-authored reference fixtures — PENDING HUMAN REVIEW

Manually constructed expected outputs for the targeted extraction contract
(prompt v5 / schema v4), one per live-evaluation record. Each fixture is
run through the real `validate_response` + `merge_response` path by
`tests/test_llm_fixtures.py`.

- `source.texts` is the verbatim pinned source (`odunola/foodie`
  revision `20a451c2a8f22e9161a13346f08e0d7cdd555727`); `content_hash`
  is its SHA-256 and is integrity-checked by the test.
- `expected.response` is what a correct model interpretation should
  contain — authored by an AI assistant from the source lines, NOT model
  output, NOT human-validated.
- `expected_verdict` is the verdict the current validator must return.
- Do not treat passing fixtures as measured model performance.
