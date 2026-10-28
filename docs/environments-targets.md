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
Other sandbox settings remain previews. Runs validate the server-owned target catalog,
stored dataset/environment versions and scorer compatibility without a static bindings whitelist.
The pinned environment selects the model, including `none` to skip model invocation.

`GET /targets` reads the project-scoped target catalog from storage (ES
`evaluation-records-v2`, `record_type=target`, `state=head`). No EVAL_TARGETS or
hardcoded runtime fallback is used. An empty project returns an empty list.
`GET /targets/{id}` returns one target and its revision. `POST /targets/{id}`
creates/updates id, name, kind (GUARDRAIL), provider (f5 / nemo guardrail), version
and definition. Updates require the current revision and an increased version.
The catalog is distinct from target-configs (dataset/environment/scorer selection).
Creating catalog metadata does not install provider adapters.

For upgrades, import the old target definitions through POST before triggering runs;
keep the same target IDs so existing target-configs remain usable. The explicit
`deploy/docker-compose/seed_demo.py` initializer creates the demo target if missing
and does not overwrite existing targets. It can also run inside the K8s API container.
Existing run snapshots/history are not changed by catalog updates.

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
`evaluation-records-v2` (target-configs records). Save fields: dataset_id, dataset_version,
environment_id, environment_version, data_source (published/upload), scoring_mode,
scorer_ids, and revision when updating. The backend verifies resource existence and schema
versions and derives model_id. Config never stores upload_id or sample content.

`POST /targets/{id}/runs` takes config_revision, optional name and (upload mode
only) a freshly validated upload_id, plus Idempotency-Key. It resolves all other
fields from saved Config. Missing/stale Config blocks triggering. Blank or
whitespace-only names become `<target name> · YYYY-MM-DD HH:mm:ss` using
EVAL_NAME_TIMEZONE (default Asia/Shanghai). A retry returns the same Job and name.

UI: each target has Config and Trigger actions. Config persists settings without
launching a Job. Trigger only shows optional name, plus upload when required.
