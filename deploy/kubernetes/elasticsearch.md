# Local Elasticsearch

Apply `elasticsearch.yaml` to context docker-desktop. Service address:
`http://evaluation-es.evaluation.svc.cluster.local:9200`.

Single-node development instance, 512 MiB heap, 2 GiB memory limit, persistent
10 GiB PVC. No host port is published. Security is disabled for local development;
use TLS and API-key authentication for shared or production clusters.

The data PVC is retained when the StatefulSet is removed. Do not delete it as
part of application cleanup. No existing UI/API Deployment should be deleted:
Nginx → Control API → Evaluation API is still the active request path.

The current running Evaluation API has not yet been migrated to this storage.
Set EVAL_ES_URL to the service address when deploying the new backend image.
Langfuse currently runs in Docker Compose, outside Kubernetes; it is not removed
by this manifest, and historical data has not been migrated.
