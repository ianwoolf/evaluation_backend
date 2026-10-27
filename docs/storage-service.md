# Evaluation storage service

Status: v2 consolidated ES layout; real ES integration and API compatibility tests included.

Browser → authenticated Control proxy → EvaluationStorageService → Storage adapter.
Worker → local collector → storage adapter. The collector loads ES credentials;
the supervisor strips them from worker/model-proxy child environments.

`storage/__init__.py` defines the vendor-neutral port: get, put (create-only or
optimistic revision), project-scoped search. The service owns dataset validation,
immutable published versions, and revision conflict handling. SDK objects, vendor
dataset IDs and vendor URLs must not appear in the UI or experiment contracts.

`storage/elastic.py` implements this port via Elasticsearch HTTP. Configure the API
with EVAL_STORAGE_BACKEND=elasticsearch, EVAL_ES_URL, optional EVAL_ES_API_KEY,
EVAL_ES_CA_FILE, EVAL_ES_VERIFY_SSL and EVAL_ES_PREFIX. TLS verification defaults to disabled. Indices are
initialized on first storage request. Physical indices are consolidated; see below.

Worker settings: EVAL_PLATFORM=storage, EVAL_STORAGE_API_URL, EVAL_STORAGE_TOKEN
(a run-scoped capability token), EVAL_PROJECT_ID and EVAL_DATASET_VERSION.
The backend must provision these before switching deployed workers away from the
old platform. Existing Langfuse data is not migrated automatically.

## Adding LangSmith

Register a factory with register_storage("langsmith", factory) before service
resolution, then select EVAL_STORAGE_BACKEND=langsmith. Keep authentication and SDK
imports inside that adapter. Map project IDs to vendor namespaces server-side.
An adapter must preserve immutable versions, project isolation, create semantics
and revision conflicts; where native APIs cannot do so, use a durable metadata
store rather than silently weakening the contract. No frontend or runner change
should be needed. Registration does not imply LangSmith is implemented today.

## HTTP contract

All endpoints below use /api/v1/evaluations, bearer authorization and X-Project-ID.

- GET /datasets, GET /datasets/{id}, POST /datasets/{id}
- GET /datasets/{id}/versions/{version}
- POST/GET /experiments/{id}
- POST/GET /experiments/{id}/samples
- POST /traces; GET /traces?evaluation_id=...
- GET /scores?evaluation_id=...

Lists accept offset and limit (maximum 100 per page, 10,000 window). Dataset saves
return revision; send it on subsequent saves. Published versions may only be
appended. Sample ingestion uses deterministic keys; a sample is written after its
trace. Scores are embedded in the sample. This provides retry idempotence, not a cross-index transaction.

## V2 physical layout

Only three indices receive new application writes:

| Default index | Documents |
| --- | --- |
| evaluation-records-v2 | dataset/environment/target-config versions and runs |
| evaluation-samples-v2 | one sample per run, including all scorer results |
| evaluation-traces-v1 | existing trace format, unchanged |

Records have flat project_id, record_type, id and type-specific fields. Types are
dataset, environment, target-config, run. New records and samples have no payload
envelope. Resource content lives in source-only spec; other unmapped fields use
dynamic:false. Scores use nested mappings for correct scorer/value filtering.

Resources have immutable version documents (state:published, version, spec) and
one mutable head (state:head, version_keys, spec). Dataset head spec is the draft;
environment head spec is empty; target-config head spec is current configuration.
Content-derived version IDs make staging retryable. The optimistic head update
publishes the committed version references. Failed head updates may leave
unreferenced version documents; these do not appear through the resource APIs.
Dataset rows remain inside each version spec (existing 8 MB / 10,000 row limit).

Run context and summary share one document. Resource references, name, job_name,
status, total_samples, completed_samples, failed_samples, execution_config,
score_summary and timestamps are top-level. Atomic field merges preserve context
when results arrive and preserve created_at. context_ready/summary_ready support
create-only operations. Context-only runs read as running. Older migrated runs
may lack metadata or summaries not persisted in v1; Kubernetes history still works.

Samples store run_id, sample_id, input/reference/output, status, snake-case
latency/cost fields, trace_id and scores. Scores have scorer_id, scorer_version,
value, passed, reason and status. There are no independent score writes. Samples
reference traces; the adapter reconstructs trace steps for existing HTTP clients.
New sample traces contain timing/steps rather than duplicated inputs/outputs/scores.
The /scores compatibility endpoint derives scores from samples. Terminal summaries
aggregate sample scores into the run. Samples remain the authoritative detail.

## Shared ConfigMap

deploy/kubernetes/services.yaml defines evaluation-storage:

```yaml
EVAL_STORAGE_BACKEND: elasticsearch
EVAL_ES_URL: http://evaluation-es:9200
EVAL_ES_VERIFY_SSL: 'false'
EVAL_ES_RECORDS_INDEX: evaluation-records-v2
EVAL_ES_SAMPLES_INDEX: evaluation-samples-v2
EVAL_ES_TRACES_INDEX: evaluation-traces-v1
```

API and generated runner Jobs load it through envFrom.configMapRef. The optional
evaluation-credentials Secret supplies EVAL_ES_API_KEY / EVAL_ES_PASSWORD. Credentials must
not go in the ConfigMap. EVAL_ES_VERIFY_SSL defaults to false (skip certificate
verification). Set true to verify with system CAs or a mounted EVAL_ES_CA_FILE.
CA_FILE is ignored while verification is disabled. Standalone processes use the same environment variables. EVAL_ES_PREFIX
supplies default names when explicit index variables are absent.

Restart API after ConfigMap changes; new Jobs use the new values, existing Pods
retain their environment. Finish active Jobs before switching indices. Changing
index names does not migrate data automatically.

## Migration and rollback

Finish Jobs and temporarily stop API writes. With the new package and the same
ES configuration as API, run:

```bash
python -m evaluation_backend.storage.migrate_v2          # count only
python -m evaluation_backend.storage.migrate_v2 --apply  # copy and validate
```

Source indices are EVAL_ES_PREFIX-<collection>-v1. Migration scans datasets,
environments, target-configs, run-contexts, experiments and samples. Scores already
embedded in v1 samples are copied; the redundant standalone score index is not.
Existing v2 records are never overwritten. Score identities/values are checked
after copying. The command is restartable and never deletes legacy indices.
Keep the existing trace index name during migration because samples reference it.

After switching the API and runner image/config, verify old reports and a fresh run.
Old indices remain visible in _cat/indices as backups. Rolling back to the old image
restores the old layout, but excludes runs created only in v2. No automatic cleanup.

The Storage port remains vendor-neutral. Logical resources/runs/samples are distinct
even when their ES documents share an index. Future LangSmith adapters may use a
different layout; LangSmith remains unimplemented.
