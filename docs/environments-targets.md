# Environments, targets and telemetry

Environments live in project-scoped ES documents (`evaluation-environments-v1`).
The browser loads them from the API; it no longer substitutes demo environments.
Each save appends an immutable version. The existing `env-isolated` configuration
must be imported once using POST; GET does not silently seed or overwrite data.

All endpoints below are under `/api/v1/evaluations` and require platform bearer
authorization plus `X-Project-ID`. Browser clients use the authenticated Control
proxy `/api/v1/projects/{projectId}/evaluation-execution` instead. Worker capabilities
cannot edit or list these configurations.

- `GET /environments`: paginated list (`offset`, `limit`).
- `GET /environments/{id}`: document including `revision` and `versions`.
- `GET /environments/{id}/versions/{version}`: immutable version.
- `POST /environments/{id}`: `{ "config": {...}, "revision": "..." }`.
  Omit revision only for a new document. Version number and timestamp are assigned
  by the server. Existing versions cannot be rewritten.
- `PATCH /environments/{id}`: resource-only update, e.g.
  `{ "revision": "3:1", "cpuMillicores": 750, "memoryMiB": 768 }`.
  Supply either or both resources. A stale revision returns 409; reload and retry.

CPU is an integer 1–32000 millicpu (`1000m = 1 CPU core`). Memory is an integer
128–65536 Mi (`1024Mi = 1Gi`). Both Pod requests and limits use the stored values.
New run requests pin `environment_id` and `environment_version`; resource values
come from that saved version. Legacy `resources` input is accepted only when it
matches the database. Existing run snapshots remain unchanged after an edit.
Other sandbox settings remain previews; trusted server resource bindings still
control which model/target/dataset/environment combinations can execute.

`GET /targets` supplies the server-owned Guardrail catalog. The current target is
`guardrail-privacy`, provider `nemo guardrail`. Provider values are `f5` and
`nemo guardrail`; the UI shows a Provider column and filter. Operators may replace
the catalog using JSON array `EVAL_TARGETS` with validated id, name, kind,
provider, version and definition fields. This does not install provider adapters.

At launch the backend fixes the target snapshot in the run context and passes
`EVAL_TARGET_SNAPSHOT` to the runner. Startup prints one structured stdout line:

```json
{"event":"evaluation.target","evaluation_id":"exp-...","target":{"id":"guardrail-privacy","name":"Privacy & prompt injection","kind":"GUARDRAIL","provider":"nemo guardrail","version":2}}
```

The worker also writes an `evaluation.target` trace. The API/collector enriches
LLM, sample and OTLP JSON trace records with trusted top-level `target` metadata
(ES `_source.payload.target`). The metadata survives input omission policies;
dataset values are never copied into it. Target definitions are excluded from
logs and telemetry. Future catalog changes do not alter already launched runs.

The current collector supports **OTLP HTTP JSON traces only** (`/v1/traces`).
There is no `/v1/logs`, OTel Logs pipeline, stdout scraper, protobuf/gRPC endpoint
or metrics receiver. The startup line stays in Kubernetes stdout; its target
metadata is separately available in ES traces, not transported as an OTel log.


## Reusable target Config and Trigger

`GET/POST /targets/{id}/config` reads/saves a project-scoped target Config in ES
`evaluation-target-configs-v1`. Save fields: dataset_id, dataset_version,
environment_id, environment_version, data_source (published/upload), scoring_mode,
scorer_ids, and revision when updating. The backend verifies bindings and schema
versions and derives model_id. Config never stores upload_id or sample content.

`POST /targets/{id}/runs` takes config_revision, optional name and (upload mode
only) a freshly validated upload_id, plus Idempotency-Key. It resolves all other
fields from saved Config. Missing/stale Config blocks triggering. Blank or
whitespace-only names become `<target name> · YYYY-MM-DD HH:mm:ss` using
EVAL_NAME_TIMEZONE (default Asia/Shanghai). A retry returns the same Job and name.

UI: each target has Config and Trigger actions. Config persists settings without
launching a Job. Trigger only shows optional name, plus upload when required.
