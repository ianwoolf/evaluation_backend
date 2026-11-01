# Dataset-specific scoring

Target configuration now accepts `scorer_selection: "dataset"`. In installed mode each selected published dataset version must declare a nonempty, unique `scorerIds` list. The top-level `scorer_ids` may be empty. When real scoring is enabled, each ID must have valid versioned metadata under `metadata.evaluation.scorers`, and its trusted implementation must be installed.

The backend resolves and validates bindings at configuration save and again at run creation. The run context snapshots scorer IDs and metadata per dataset; runner input uses those snapshots even if metadata in storage is later changed. Public run dataset records include purpose and scorer IDs, excluding raw scorer configuration. The runner selects the local list separately for each dataset. Identical scorer IDs may carry different versions/parameters in separate datasets.

Legacy configurations omit the selector and retain `shared` behavior and their selected scorer subset. The UI exposes this explicitly. `scoring_mode: "mock"` still uses the legacy mock-score workflow. With `EVAL_SCORING_ENABLED=false`, dataset-local installed selections keep their IDs but the runner bypasses plugins and emits deterministic scores marked `source: "mock"`.

Published and uploaded inputs, configured subsets, project scope and request idempotence remain supported. Execution with one dataset also uses the pinned input envelope in dataset mode. No default data or existing target configurations are overwritten by deployment.
