# 本地 Kubernetes：评测后端与完整依赖清单

新电脑从 [deploy/README.md](../README.md) 开始；本页保留当前本机部署说明。

当前运行链路使用 ES，不依赖 Langfuse，也不需要部署 OpenShell Gateway。
UI 仓库的对应目录是 `web/deploy/kubernetes`。本目录命令在后端仓库根执行。

## 必需组件

| 组件 | K8s 资源 | 用途 / 数据 |
|---|---|---|
| evaluation-ui | Deployment、Service :8080（UI 仓库） | Nginx SPA、同源 API 代理 |
| evaluation-control-api | Deployment、Service :8080（UI 仓库） | 登录、项目权限、评测 API 代理 |
| PostgreSQL | 当前复用宿主机 Docker ianwoolf/evaluation-postgres:17，55432→5432 | Control 用户、项目、权限；需持久卷及已执行 Prisma migrations |
| evaluation-api | Deployment、Service :8001 | 资源目录、上传校验、创建/查询 Job、存储 API、模型解析 |
| evaluation-es | StatefulSet、Service :9200、5Gi PVC | Dataset、Environment 历史版本、target Config、run context、样本、score、trace |
| evaluation-uploads | 1Gi PVC，仅 API 挂载 | 临时上传的标准化数据与元数据；终态/过期自动清理 |
| evaluation-runtime | ConfigMap | 集群内地址、ES storage 模式、worker 运行参数 |
| evaluation-models | Secret | models.json、resolver-token |
| evaluation-trigger | Secret | Control → evaluation-api 的 token |
| evaluation-runner | Secret | 模型解析 token；不含模型供应商 token |
| evaluation-api / evaluation-worker | ServiceAccount；API 配 Role/RoleBinding | API 仅在 evaluation 内创建/读取 Job、读取 Pod/log；worker 不挂 API token |
| evaluation-worker | 每次评测动态创建 Job/Pod | worker、LLM proxy、Python collector 是同一容器的子进程，无独立 Deployment |

API 使用单副本 + Recreate，避免本地 RWO 上传卷并行挂载。Worker 的 CPU/内存从已保存
Environment 版本读取，requests=limits；Job deadline 600s，API 创建的 Job 无完成 TTL、保留到人工清理，失败不自动重试。
Langfuse Compose 可为历史数据保留，但不属于当前部署依赖。`otel-collector-config.yaml`
是未启用的外部 Collector 示例，不是 K8s 清单，不要 `kubectl apply -f` 整个目录。
当前 Python collector 只接收 OTLP HTTP JSON traces，无 OTel Logs/stdout 采集。

## 部署顺序

1. Docker Desktop Kubernetes Ready，context `docker-desktop`，默认 StorageClass 可动态创建 PVC。
2. 构建镜像并导入节点。重建集群会清空节点缓存，`docker images` 有镜像不代表节点有。
3. 创建 namespace；准备 Secret；部署 ES、上传 PVC、API。
4. 部署 UI 仓库的 Control API/Nginx。已有 PostgreSQL 不重复初始化，先完成必要迁移。
5. 通过 API 导入 Dataset、创建 Environment、保存每个 target Config，再运行评测。

```bash
docker build -f Dockerfile.worker -t ianwoolf/evaluation-worker:20261025-env-v1 .
# ES 使用与现有磁盘一致的版本；本地无镜像时才从官方仓库拉取。
docker pull ianwoolf/evaluation-elasticsearch:8.18.1
bash deploy/kubernetes/import-local-image.sh ianwoolf/evaluation-worker:20261025-env-v1 ianwoolf/evaluation-elasticsearch:8.18.1
kubectl --context docker-desktop create namespace evaluation --dry-run=client -o yaml | kubectl --context docker-desktop apply -f -
PYTHONPATH=src python3 deploy/kubernetes/render_secrets.py --config .env | kubectl --context docker-desktop apply -f -
kubectl --context docker-desktop apply -f deploy/kubernetes/elasticsearch.yaml
kubectl --context docker-desktop apply -f deploy/kubernetes/uploads.yaml
kubectl --context docker-desktop apply -f deploy/kubernetes/services.yaml
kubectl --context docker-desktop -n evaluation rollout status statefulset/evaluation-es --timeout=180s
kubectl --context docker-desktop -n evaluation rollout status deployment/evaluation-api --timeout=120s
```

Secret 渲染要求本地忽略的配置文件提供 `EVAL_MODEL_REGISTRY`（models.json 路径）、
`EVAL_MODEL_RESOLVER_TOKEN`、`EVAL_TRIGGER_TOKEN`。现有集群已有正确 Secret 时无需重建。
stdout 含凭据，只管道给 kubectl，不保存到仓库、不回显。UI 引用同一个 evaluation-trigger。
`services.yaml` 的 `EVAL_RESOURCE_BINDINGS` 是允许执行的 target/dataset/environment/model
组合；换 ID 时同步修改。可选 `EVAL_TARGETS` 定义服务端 target 目录。

`import-local-image.sh` 支持 macOS/Linux/WSL 的 Docker Desktop kind 单节点。使用已有
`kindest/node:v1.36.1` 临时 privileged helper 导入隐藏节点 containerd，不推送仓库。
其他节点版本通过 `KIND_NODE_IMAGE` 指定已有镜像；标准 kind 集群可设置
`KIND_CLUSTER_NAME` 使用 `kind load docker-image`。生产环境改用可访问仓库及不可变 tag/digest。
API image 与 `EVAL_WORKER_IMAGE` 必须一致。当前清单为 `IfNotPresent`，从 ianwoolf Docker Hub 仓库拉取。

## 初始化业务数据与配置

API 请求需 Bearer token、`X-Project-ID`；浏览器经 Control API 代理，不持有后端 token。

- `POST /api/v1/evaluations/datasets/{id}` 导入 dataset 文档（包含发布版本及 schema）。
- `POST /api/v1/evaluations/environments/{id}`，body 为 `{"config":{...}}`；创建配置时
  提供 name、modelId、guardrailId、cpuMillicores、memoryMiB 等。修改需当前 revision。
- `PATCH /api/v1/evaluations/environments/{id}` 可修改 CPU/内存并追加版本。
- `POST /api/v1/evaluations/targets/{id}/config` 保存 dataset/version、environment/version、
  data_source、scoring_mode、scorer_ids；更新需 revision。不得保存上传文件内容/upload_id。
- `POST /api/v1/evaluations/targets/{id}/runs` 只需 config_revision、可选 name；upload 模式
  另传本次 upload_id。空名称为 target + 当前时间（`EVAL_NAME_TIMEZONE`，默认 Asia/Shanghai）。

详见 [Environment 与 target API](../../docs/environments-targets.md) 和
[临时数据与内容策略](../../docs/temporary-datasets.md)。不要将生产数据写进示例 ConfigMap。

## 验证、访问与恢复

```bash
kubectl --context docker-desktop -n evaluation get deploy,statefulset,pods,svc,pvc
kubectl --context docker-desktop -n evaluation port-forward service/evaluation-es 9200:9200 --address 127.0.0.1
# 另开终端；API 调试需要时才开启：
kubectl --context docker-desktop -n evaluation port-forward service/evaluation-api 18001:8001 --address 127.0.0.1
curl --fail http://127.0.0.1:18001/health
curl --fail http://127.0.0.1:9200/_cluster/health
```

`ErrImageNeverPull`：重新运行镜像导入，等待 Pod 重试；不需要删除 namespace/PVC。
更新应用使用新 tag，更新清单，再 apply；UI Pod 替换后重新启动端口转发。
重建集群不仅会丢镜像缓存，还可能丢本地 PVC；ES 数据需 snapshot/restore，PostgreSQL
需备份恢复，不能靠导入镜像恢复业务数据。已有 StatefulSet 的 claim 名称/容量不可随意修改；
当前重建集群的 `es-data-evaluation-es-0` 实际为 2Gi（StatefulSet 新卷模板为 5Gi），`evaluation-uploads` 1Gi，变更需单独迁移。

## 无模型测评

Environment 的 Model ID 可选 `none`。保存新 Environment 版本后，在 target Config 中选中
该版本并保存。runner 不启动 LLM proxy，也不构造或调用模型 target；将 dataset 的 input
对象同时传给 scorer 的 input/output 参数，reference 保持不变。打分器必须支持该输入形状。
样本 `modelLatencyMs` 和 evaluation.sample 遥测的 `modelLatencyMs` 为 0，不生成模型 span；
`latencyMs` 仍表示包含打分的样本总耗时。collector、运行级内容策略和上传清理继续生效。

## Runner Environment 日志

API 将选中且已固定版本的 Environment 配置通过 `EVAL_ENVIRONMENT_SNAPSHOT` 传给 Job，
包括 ID、version、modelId、guardrailId、CPU/内存及其它已保存字段。
同时提供 `EVAL_GUARDRAIL_ID`，供 runner 内的代码读取。
worker 启动时输出 `evaluation.environment` JSON 日志；只打印该配置，不打印进程环境变量或认证 token。
这条日志表示收到的配置，不代表其中所有 sandbox 限制已由 Kubernetes 执行。
