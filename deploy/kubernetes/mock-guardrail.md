# Local mock Guardrail

Independent deployment: `evaluation-mock-guardrail`, namespace `evaluation`.
Image: `evaluation-mock-guardrail:scan-20261102`.

Scan endpoint inside Kubernetes:
`http://evaluation-mock-guardrail.evaluation.svc.cluster.local:8002/runtime/v1/endpoints/mock-guardrail/backend/v1/scans`

Authentication: Bearer token from Secret `evaluation-mock-guardrail`, key `token`.
The generated token is kept in Kubernetes only. Do not put it into Git, datasets or report snapshots.
ConfigMap `evaluation-mock-guardrail` owns the non-secret endpoint ID.

Build from backend root:
```bash
docker build -f deploy/Dockerfile.mock-guardrail -t evaluation-mock-guardrail:scan-20261102 .
bash deploy/kubernetes/import-local-image.sh evaluation-mock-guardrail:scan-20261102
kubectl --context docker-desktop apply -f deploy/kubernetes/mock-guardrail.yaml
```
The manifest requires the Secret to exist; deployments preserve it.

Optional local access:
```bash
kubectl --context docker-desktop -n evaluation port-forward service/evaluation-mock-guardrail 18083:8002
```
Health: GET /health. Readiness: GET /ready (requires token configured).
Interactive API schema: /docs. Scan requests require Authorization.

Input fixture behavior:
- Ordinary input: cleared.
- `[mock:unsafe]`: flagged; with flagOnly=false: blocked.
- `[mock:error]`: HTTP 503.
- `[mock:timeout]`: immediate HTTP 504, not a delayed network timeout.
- verbose=true includes one scannerResults entry.

This is a synthetic Scan adapter, not a risk detector. It never receives dataset labels.
`redacted` responses are not synthesized by this fixture yet. The example scorer accepts them.

Verified on 2026-11-02: readiness and all six scan/auth/error cases through Service DNS.
The Guardrail evaluation runner/proxy integration is still in progress; deploying this service alone does not activate the new Trigger flow.

## Integration status (2026-11-02)

UI/API/runner remain on `update-score-20261102`. The standalone mock is on `scan-20261102`.
No Guardrail-specific target/dataset/scorer/environment bindings have been created yet.
The example scorer exists at `examples/guardrail-scorer/scorer.py`, but pinned Git artifacts and isolated execution are pending.
The new dataset editor, Trigger and aspect report UI are not deployed. Model `none` is required by the planned direct-Guardrail flow, which is not connected yet.
Local port 18083 is optional and is not a persistent forward established by this deployment.


Request logs (2026-11-04): the mock writes structured JSON to stdout for each non-probe request. Events are guardrail.request.started, guardrail.request.completed and guardrail.request.failed. Fields include request_id, method, path, status_code, duration_ms, scan endpoint_id, input_chars, scan_direction, flag_only and successful outcome. Valid incoming traceparent adds trace_id and parent_span_id. X-Request-ID is returned on successes and failures; successful scan response id matches it. Authorization, token and raw request/response content are never logged. /health and /ready (including query strings) are filtered. Uvicorn access logging remains disabled to avoid duplicate/probe logs.

```bash
kubectl -n evaluation logs -f deployment/evaluation-mock-guardrail --since=10m
```

Current local image: evaluation-mock-guardrail:request-logs-20261104. No API/UI change is needed for these logs.
