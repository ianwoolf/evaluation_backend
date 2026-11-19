# Runner Git scorer fixture

Repository: git@github.com:ianwoolf/evaluation_backend.git
Branch: codex/git-scorer-ssh-fixture
Entry: scripts/git-scorer-fixtures/scan_classification.py

Resolve and pin the full commit SHA when configuring an evaluator. The runner sends one JSON object on stdin with `caseid`, normalized `label` (safe/unsafe), and `target_response` (`body`, `http_code`, `client_timeout`, `error`, `elapsed_ms`). The script returns `state_type` (`ok`, `rate-limit`, `timeout`, `error`), the raw `guardrail_outcome`, and a `reason`. Successful responses also return `expected`, `predicted`, and the TP/FP/TN/FN `outcome`. Uses only the Python standard library. No keys or credentials belong in this directory.
