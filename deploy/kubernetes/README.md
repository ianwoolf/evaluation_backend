# 本地 Kubernetes：评测后端与完整依赖清单

**本目录仅用于 Kubernetes。只有 Docker 的机器请使用 [Docker Compose](../docker-compose/README.md)。**

新电脑从 [deploy/README.md](../README.md) 开始；本页保留当前本机部署说明。

当前运行链路使用 ES，不依赖 Langfuse，也不需要部署 OpenShell Gateway。
UI 仓库的对应目录是 `web/deploy/kubernetes`。本目录命令在后端仓库根执行。

## 必需组件

| 组件 | K8s 资源 | 用途 / 数据 |
|---|---|---|
| evaluation-ui | Deployment、Service :8080（UI 仓库 evaluation-ui.yaml） | 独立 Nginx SPA，直接转发评测 API，无登录 |
| evaluation-api | Deployment、Service :8001 | 资源目录、上传校验、创建/查询 Job、存储 API、模型解析 |
| evaluation-es | StatefulSet、Service :9200、5Gi PVC | Dataset、Environment 历史版本、target Config、run context、样本、score、trace |
| evaluation-uploads | 1Gi PVC，仅 API 挂载 | 临时上传的标准化数据与元数据；终态/过期自动清理 |
| evaluation-runtime | ConfigMap | 集群内地址、worker 运行参数 |
| evaluation-storage | ConfigMap | 后端与 runner 共用的 ES 地址、records/samples/traces 索引名 |
| evaluation-api-config | ConfigMap | API 普通参数、项目、执行方式、镜像、配置文件路径 |
| evaluation-model-registry | ConfigMap | models.json，仅地址、模型名和 token_env 引用 |
| evaluation-ui-runtime | ConfigMap（UI 仓库） | UI JSON 参数，无凭据 |
| evaluation-credentials | Secret | 内部签名/解析 token、模型 token、可选 ES 密码/API key、OTel 认证头 |
| evaluation-api / evaluation-worker | ServiceAccount；API 配 Role/RoleBinding | API 仅在 evaluation 内创建/读取 Job、读取 Pod/log；worker 不挂 API token |
| evaluation-worker | 每次评测动态创建 Job/Pod | worker、LLM proxy、Python collector 是同一容器的子进程，无独立 Deployment |

API 使用单副本 + Recreate，避免本地 RWO 上传卷并行挂载。Worker 的 CPU/内存从已保存
Environment 版本读取，requests=limits；Job deadline 600s，API 创建的 Job 无完成 TTL、保留到人工清理，失败不自动重试。
Langfuse Compose 可为历史数据保留，但不属于当前部署依赖。`otel-collector-config.yaml`
是未启用的外部 Collector 示例，不是 K8s 清单，不要 `kubectl apply -f` 整个目录。
当前 Python collector 只接收 OTLP HTTP JSON traces，无 OTel Logs/stdout 采集。

API 和新 runner 使用统一 Secret，模型 JSON、绑定 JSON 和 UI JSON/Nginx 配置均为 ConfigMap。
旧 evaluation-models / evaluation-trigger / evaluation-runner 已由新 Secret 取代。
旧完整平台 evaluation-ui-config 不再被精简评测服务引用，不属于当前部署依赖。

## 部署顺序

当前清单使用本地镜像 `evaluation-worker:configured-datasets-20261030`，需先执行
`docker build -f Dockerfile.worker -t evaluation-worker:configured-datasets-20261030 .` 并导入 K8s 节点。
此标签尚未发布到 Docker Hub。已有数据升级前，按照
[storage-service.md](../../docs/storage-service.md) 停止写入、迁移，再启动新版本。
修改 `evaluation-storage` 后需要重启 API；新建 runner Job 自动加载新配置。

1. Docker Desktop Kubernetes Ready，context `docker-desktop`，默认 StorageClass 可动态创建 PVC。
2. 构建镜像并导入节点。重建集群会清空节点缓存，`docker images` 有镜像不代表节点有。
3. 创建 namespace；准备 Secret；部署 ES、上传 PVC、API。
4. 部署 UI 仓库的 Control API/Nginx。已有 PostgreSQL 不重复初始化，先完成必要迁移。
5. 通过 API 导入 Dataset、创建 Environment、保存每个 target Config，再运行评测。

```bash
docker build -f Dockerfile.worker -t evaluation-worker:configured-datasets-20261030 .
# ES 使用与现有磁盘一致的版本；本地无镜像时才从官方仓库拉取。
docker pull docker.io/library/elasticsearch:8.19.23
bash deploy/kubernetes/import-local-image.sh evaluation-worker:configured-datasets-20261030 docker.io/library/elasticsearch:8.19.23
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
stdout 含凭据，只管道给 kubectl，不保存到仓库、不回显。脚本同时生成模型 ConfigMap，
以及统一的 evaluation-credentials Secret；UI 无需 Secret。
测评配置保存在 ES 的 target-configs 记录中；保存和执行时校验 dataset/environment 的指定版本及打分器。
模型取自 environment，支持 `none`。不再需要 bindings.json 或绑定 ConfigMap。
target 目录保存在 ES 的 evaluation-records-v2（record_type=target），通过 targets API 管理，不再读取 EVAL_TARGETS。
从旧版本升级时同时更新 API/UI 镜像和 services.yaml。API 滚动更新完成后，
旧 evaluation-resource-bindings ConfigMap 可删除；已有 ES target 配置无需迁移。

`import-local-image.sh` 支持 macOS/Linux/WSL 的 Docker Desktop kind 单节点。使用已有
`kindest/node:v1.36.1` 临时 privileged helper 导入隐藏节点 containerd，不推送仓库。
其他节点版本通过 `KIND_NODE_IMAGE` 指定已有镜像；标准 kind 集群可设置
`KIND_CLUSTER_NAME` 使用 `kind load docker-image`。生产环境改用可访问仓库及不可变 tag/digest。
API image 与 `EVAL_WORKER_IMAGE` 必须一致。当前应用镜像为本地 configured-datasets-20261030 标签，需自行构建/导入或推送到自己的仓库。

## 初始化业务数据与配置

当前清单使用免登录单用户模式，浏览器直接访问评测 API；未提供项目头时使用部署默认项目。
若显式改为 token 模式，调用者才需配置服务凭据。模型解析仍为内部验证接口。

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
