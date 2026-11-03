# Runner Git scorer fixture

Repository: git@github.com:ianwoolf/evaluation_backend.git
Branch: codex/git-scorer-ssh-fixture
Entry: scripts/git-scorer-fixtures/scan_classification.py

Resolve and pin the full commit SHA when configuring a scorer. Reads one JSON object from stdin with label (safe/unsafe) and output.result.outcome. Returns expected, predicted, outcome (TP/FP/TN/FN), and reason. Uses only the Python standard library. No keys or credentials belong in this directory.
