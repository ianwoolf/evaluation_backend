# Evaluation storage service

Status: implementation in progress; not yet deployed or validated end to end.

Browser → authenticated Control proxy → EvaluationStorageService → Storage adapter.
Worker → StorageClient HTTP → same service. Workers never receive ES credentials.

`storage/__init__.py` defines the vendor-neutral port: get, put (create-only or
optimistic revision), project-scoped search. The service owns dataset validation,
immutable published versions, and revision conflict handling. SDK objects, vendor
dataset IDs and vendor URLs must not appear in the UI or experiment contracts.

`storage/elastic.py` implements this port via Elasticsearch HTTP. Configure the API
with EVAL_STORAGE_BACKEND=elasticsearch, EVAL_ES_URL, optional EVAL_ES_API_KEY,
EVAL_ES_CA_FILE and EVAL_ES_PREFIX. TLS verification stays enabled. Indices are
initialized on first storage request. Dataset, experiment, sample, trace and score
collections are separate. Arbitrary payloads stay in _source without dynamic field
mapping. Project and evaluation IDs are indexed for filtering.

Worker settings: EVAL_PLATFORM=storage, EVAL_STORAGE_API_URL, EVAL_STORAGE_TOKEN
(currently the server trigger credential), EVAL_PROJECT_ID and EVAL_DATASET_VERSION.
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
trace and scores. This provides retry idempotence, not a cross-index transaction.

Outstanding integration: deployment variables/secrets, existing-data migration,
frontend live report retrieval, legacy tests and real ES integration verification.
