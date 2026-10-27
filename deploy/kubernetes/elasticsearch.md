# Local Elasticsearch

## 可选：启用用户名/密码认证

客户端已支持两种互斥方式：`EVAL_ES_API_KEY`，或者同时设置
`EVAL_ES_USERNAME` / `EVAL_ES_PASSWORD`。URL 仍为不含凭据的 `EVAL_ES_URL`。
HTTPS 证书验证由 `EVAL_ES_VERIFY_SSL` 控制，默认 `false`（跳过验证）；设为 `true`
时校验证书，可通过 `EVAL_ES_CA_FILE` 指定挂载的 CA 文件，不指定则使用系统 CA。
设为 `false` 时忽略 CA 文件。该配置放在 `evaluation-storage` ConfigMap，后端和
runner collector 共用；不影响模型调用和 OTel 的 TLS 配置。
后端和 runner collector 从统一的 `evaluation-credentials` Secret 注入密码/API key；
用户名是 `evaluation-storage` ConfigMap 的普通配置。
worker 和模型代理子进程不会收到 ES 凭据。

下面在后端工程根目录、WSL/bash 中执行。密码交互输入，不写入命令历史或配置仓库：

```bash
export EVAL_ES_USERNAME=elastic
read -rsp 'ES password: ' EVAL_ES_PASSWORD; printf '\n'
export EVAL_ES_PASSWORD
unset EVAL_ES_API_KEY
.venv/bin/python deploy/kubernetes/render_es_secret.py \
  | kubectl --context docker-desktop -n evaluation patch secret evaluation-credentials --type merge --patch-file /dev/stdin
kubectl --context docker-desktop -n evaluation patch configmap evaluation-storage \
  --type merge -p '{"data":{"EVAL_ES_USERNAME":"elastic"}}'
```

先用 render_secrets.py 创建统一 Secret。这里生成 merge patch，保留其他内部/模型凭据。
渲染器从当前进程环境读取密码。输出包含凭据，只能直接管道给 kubectl，
不要保存到受版本控制的文件或日志。输出中的空 `EVAL_ES_API_KEY` 会覆盖旧 API key。

新环境先部署基础 `elasticsearch.yaml`，然后应用以下认证补丁；已有本机集群直接 patch，
避免用基础清单替换现有 ES 镜像、PVC 配置：

```bash
kubectl --context docker-desktop -n evaluation patch statefulset evaluation-es \
  --type strategic --patch-file deploy/kubernetes/elasticsearch-auth.patch.yaml
kubectl --context docker-desktop -n evaluation rollout status statefulset/evaluation-es --timeout=300s
kubectl --context docker-desktop -n evaluation rollout restart deployment/evaluation-api
```

补丁设置 `xpack.security.enabled=true`，从 Secret 的 `EVAL_ES_PASSWORD` 键注入 ES
容器的 `ELASTIC_PASSWORD`，并把 startup/readiness 探针改为携带凭据的 curl。
本示例以 `elastic` 用户初始化单节点开发环境，HTTP TLS 显式关闭；跨机器或生产部署
应配置 HTTPS，多节点还需要 transport TLS，并为应用配置独立用户或 API key。

**后端/runner 必须使用包含上述 Basic Auth 支持的新镜像。** 若当前节点仍是旧版本，
先构建并导入镜像，再更新 API 的镜像和 `EVAL_WORKER_IMAGE`；配置 Secret 本身不会升级代码。

`ELASTIC_PASSWORD` 用于初始化/bootstrap，不是已有用户的密码轮换机制。
复用数据卷且已设置过密码时，改 Secret 不会重置已有密码；需要交互式执行：

```bash
kubectl --context docker-desktop -n evaluation exec -it evaluation-es-0 -- \
  bin/elasticsearch-reset-password -u elastic -i
```

输入与 Secret 相同的密码后，探针和客户端才能正常认证；不要为此删除 PVC。
启用认证的已有集群若仍使用旧 bootstrap 密码，也可通过该命令明确设置用户密码。
相关行为见 [Elastic 最小安全配置](https://www.elastic.co/docs/deploy-manage/security/set-up-minimal-security)。

验证未携带凭据返回 401，携带凭据成功：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9200/
# curl 交互询问密码，避免把密码放到进程参数中。
curl --user elastic http://127.0.0.1:9200/
unset EVAL_ES_PASSWORD
```

基础清单仍保持无认证开发模式；是否开启认证由上述补丁决定。再次应用基础清单时
注意它会覆盖 security 和探针设置，需重新应用认证补丁。

## 基础开发清单

`elasticsearch.yaml` defines a single-node ES 8.19.23 service at
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

The manifest uses `docker.io/library/elasticsearch:8.19.23` with IfNotPresent.
Online nodes pull it from Docker Hub; offline nodes must import it into containerd first. Keep the ES version compatible with existing
storage. App image imports do not restore a lost PVC; back up ES snapshots before
rebuilding the cluster. See README.md for complete ordering and dependencies.
