# Local Elasticsearch

`elasticsearch.yaml` defines the current single-node ES 8.18.1 service at
`http://evaluation-es.evaluation.svc.cluster.local:9200`, a StatefulSet and a 5Gi
new-volume template. The rebuilt local cluster currently reuses a 2Gi PVC named
`es-data-evaluation-es-0`; applying the template does not expand that existing
volume. Do not delete or replace it during application deployment.

The evaluation API and collector already use ES for datasets, environments,
target configurations, run contexts, samples, scores and traces. PostgreSQL is
still used by Control API for identity/project configuration. Langfuse is not a
runtime dependency and its historical data is not automatically migrated.

512Mi heap, 2Gi memory limit, ClusterIP only; security is disabled for this local
single-user deployment. Shared deployments require authentication and TLS.
Loopback access: `kubectl -n evaluation port-forward service/evaluation-es
9200:9200 --address 127.0.0.1` (write this command on one line).

Import `elasticsearch:8.18.1` into node containerd before applying the manifest
with Never image pull policy. Keep the ES version compatible with existing
storage. App image imports do not restore a lost PVC; back up ES snapshots before
rebuilding the cluster. See README.md for complete ordering and dependencies.
