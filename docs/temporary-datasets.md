# Temporary datasets and run content policy

A run selects a published dataset ID/version as its schema. It can use that version's rows or provide an `upload_id` from a validated CSV/XLSX upload. Uploads never create published dataset versions.

## API flow

1. `POST /api/v1/evaluations/uploads?dataset_id=...&dataset_version=...` with the raw file body, `X-Upload-Filename` (percent-encoded UTF-8), project header and platform authorization. Returns `id`, `row_count`, schema binding and expiry. The authenticated Control API proxies this endpoint.
2. Submit the normal run request with `upload_id` and an idempotency key. The backend validates the schema binding and claims the upload for one run. A retry of the same run is idempotent.
3. The worker reads `/api/v1/evaluations/run-input/{experiment_id}` using a signed capability limited to its project/run. Upload rows do not enter Kubernetes annotations or the ES dataset catalog.
4. A background reconciler checks every 15 seconds and removes normalized content after Job success, failure or suspension. Missing Jobs have a 60-second creation grace period. Unused uploads expire after one hour by default; claimed uploads have a 24-hour fallback expiry. Terminal report reads also request cleanup. Metadata tombstones contain identifiers/counts, not rows.

CSV must be UTF-8 (BOM allowed), with one header row. XLSX must have one sheet. Both are limited to 8 MB and 10,000 samples. Headers and cell types must match the selected schema: JSON fields contain JSON text, booleans use true/false, numbers must be finite. Required fields, message structure, duplicate/unknown headers, XLSX formulas, macros, external links and archive expansion are checked. Validation errors identify row/column without quoting submitted values. The UI offers a CSV header template and prevents submission until validation succeeds.

## Storage and deployment

Apply `deploy/kubernetes/uploads.yaml` before `services.yaml`. Only evaluation-api mounts the private `evaluation-uploads` PVC at `/uploads`; workers fetch rows through the API. Original CSV/XLSX bytes are parsed in memory; only normalized JSON is persisted with mode 0600 alongside SQLite metadata. The Nginx proxy disables request buffering to disk.

This implementation uses one API replica with a persistent volume. Multi-replica production deployment needs shared storage and coordinated metadata/claims. Configure storage encryption and backup exclusions separately: unlinking temporary files is not cryptographic erasure or deletion of volume snapshots.

Settings:

- `EVAL_UPLOAD_DIR`: private content/metadata directory (default `data/uploads`).
- `EVAL_UPLOAD_TTL_SECONDS`: unused-upload lifetime (default `3600`).
- `EVAL_CONTENT_POLICY`: JSON object, default all `retain`.

## Content policy

```json
{"input":"retain","response":"retain","reference":"retain"}
```

Each field independently accepts `retain` or `omit`. The backend snapshots this policy in an immutable ES run context and injects it into the worker/collector. Changing deployment defaults affects future runs only. Clients cannot override it in the run payload; no UI setting or report source/cleanup badge is exposed.

Execution and scoring receive complete values. Filtering happens at serialization in the storage client, telemetry/collector and storage API. `response` maps to persisted `output`. When any field is omitted, free-form score reasons, trace details and exception text are also suppressed to prevent indirect copies; numerical scores, duration and permitted token counts remain. Generic OTLP attributes/events are removed in restricted mode. Custom scorer code must not independently print raw content or configure another exporter outside these managed paths.

Temporary-file deletion and result retention are separate. With the default retain policy, samples in ES still contain input/response/reference after the temporary dataset is deleted.

## Local acceptance (2026-10-24)

CSV/default-retain and XLSX/all-omit ran through the authenticated UI proxy and real Kubernetes workers. Both completed; invalid schema returned 422. The omitted canary was absent from ES samples/scores/traces and worker logs. A missing-model failure also removed its temporary content without report polling. All three uploads had deletion tombstones and zero remaining JSON files. Model calls were real; scores used the existing explicit `mock-score` development scorer.
