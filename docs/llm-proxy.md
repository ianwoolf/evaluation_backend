# Runner LLM interception

The container-local OpenAI Chat Completions proxy overrides every request's model using
`EVAL_MODEL_ID`. On every request it calls the authenticated backend endpoint
`GET /internal/models/{id}`, which returns `address`, `model`, `token` from the ignored
JSON file selected by `EVAL_MODEL_REGISTRY`. Set `EVAL_MODEL_RESOLVER_TOKEN` on backend
and proxy. No provider credentials are passed to the workload.

Backend: `python -m uvicorn evaluation_backend.api:app --host 0.0.0.0 --port 8001`

Worker: set `EVAL_LLM_PROXY_ENABLED=true`, `EVAL_MODEL_ID`, `EVAL_MODEL_RESOLVER_URL`,
`EVAL_MODEL_RESOLVER_TOKEN`, and the usual Langfuse credentials. OpenShell uploads these
settings; the worker starts `proxy_entrypoint`, which supervises proxy and worker processes.
For any other workload in Docker/OpenShell use:

```sh
python -m evaluation_backend.proxy_entrypoint -- python your_workload.py
```

The wrapper sets `OPENAI_BASE_URL`, `OPENAI_API_BASE`, and `OPENAI_API_KEY` to the local
proxy. SDKs/code must use these settings. Hardcoded URLs, non-OpenAI protocols, external
HTTP targets, and direct provider connections are not transparently intercepted. Enforce
restricted egress separately through the OpenShell policy/network configuration. The local
proxy is not a security boundary against arbitrary code running under the same Unix user.
Raw Docker runner tasks can invoke the wrapper with these variables in their task environment;
the existing low-level runner has not been changed to inject model configuration automatically.

Only `/v1/chat/completions` is currently supported. Both streaming and ordinary responses
are supported; upstream is always SSE to measure first token time accurately. The proxy
requests `stream_options.include_usage`. Providers must support that protocol. No retry is
performed, preventing duplicate billed calls after uncertain responses.

Langfuse's OTEL generation records input, output, request parameters, requested model,
response model, model ID, experiment ID, usage, TTFT milliseconds, and total duration.
First token means the first nonempty content, reasoning, or tool delta (not an initial role-only
chunk). Duration starts before model resolution and ends when the upstream stream completes
or is interrupted; it excludes OTEL export time. Missing usage stays missing, never fabricated.
Reasoning/cache token breakdowns are preserved as metadata. W3C traceparent is accepted to
join the runner trace. Without it, the generation is a new trace tagged by experiment metadata.
Authorization headers, resolver/provider tokens, and backend registry contents are never traced.

Local demo model `glm-dev` resolves to the user-supplied BigModel endpoint/model. Store real
secrets only in ignored `.env` / `data/models.json`. For Docker Desktop use
`http://host.docker.internal:8001` and `http://host.docker.internal:3000`; sandbox localhost
is not the host. In Kubernetes use service DNS addresses.

Smoke workload: `examples/llm_proxy_smoke.py` checks both streaming and ordinary calls,
using a deliberately wrong caller model name to verify the runner chooses the model.


## Verified locally (2026-10-20)

Built `evaluation-worker:llm-proxy` and ran `examples/llm_proxy_smoke.py` in a real Docker
container with the proxy supervisor. Both non-streaming and streaming requests returned
HTTP 200 and `OK`, despite a deliberately incorrect client model. Langfuse readback showed
both GENERATION observations with experiment_id `dev_1`, input/output/total usage 17/3/20,
TTFT approximately 2428/1695 ms and total time 2434/1697 ms respectively. Requested model
was `glm-4.7`; provider response model was `glm-5.3-flash`, preserved in metadata.
OpenShell lifecycle integration is implemented but still requires a real installed Gateway
and an egress policy; it has not been tested against a live OpenShell installation.
