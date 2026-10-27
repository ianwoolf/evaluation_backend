# Evaluation backend：新电脑 Kubernetes 部署

当前按部署平台分目录：

| 方式 | 入口 | runner |
| --- | --- | --- |
| Docker Compose | [docker-compose/README.md](docker-compose/README.md) | 独立 Docker 容器，无 Kubernetes 依赖 |
| Kubernetes | [kubernetes/README.md](kubernetes/README.md) | K8s Job |

新的 K8s 活动配置仅用 `evaluation-credentials` 保存凭据，模型和绑定 JSON 放 ConfigMap。
当前应用镜像标签为 `evaluation-worker:deploy-v2`。下文旧标签仅用于历史完整平台部署。

当前默认是单用户精简部署：`services.yaml` 使用 `EVAL_AUTH_MODE=none`，镜像为
`evaluation-worker:standalone`；主工程 UI 使用 `web/deploy/kubernetes/evaluation-ui.yaml`。
不需要 Control API 或 PostgreSQL。完整步骤优先参考主工程 `web/deploy/STANDALONE.md`。
下文保留旧全功能部署/已发布镜像说明，旧标签不包含最新精简模式，应从当前源码构建。

已发布镜像地址及架构见 [IMAGES.md](IMAGES.md)。当前上传镜像仅支持 linux/arm64。


命令均在 **evaluation-backend 仓库根目录**执行。本仓库独立于 UI 主仓库，需分别复制代码。
全套 UI/PostgreSQL 步骤见主工程 web/deploy/README.md；runner 说明见
[runner/deploy/README.md](../runner/deploy/README.md)。

当前架构：Control API → evaluation-api → K8s Job（worker + collector + 可选 LLM proxy）。
ES 存储 dataset、environment、target Config、run context、sample、score、trace；
PostgreSQL 仅供 Control API。无需部署 Langfuse、OpenShell Gateway 或独立 OTel Collector。

## 目录与部署资源

| 文件 | 内容 |
|---|---|
| kubernetes/services.yaml | Namespace、runtime ConfigMap、API Deployment/Service、API/worker ServiceAccount、Job RBAC |
| kubernetes/elasticsearch.yaml | ES 8.18.1，单节点、5Gi PVC、512Mi JVM heap、2Gi 内存上限 |
| kubernetes/uploads.yaml | API 专用 1Gi PVC，默认 StorageClass |
| kubernetes/render_secrets.py | 从私有配置生成 models / trigger / runner 三个 Secret |
| kubernetes/import-local-image.sh | Docker Desktop kind / 标准 kind 镜像导入 |
| examples/ | 无真实数据、无真实密钥的初始化示例 |
| kubernetes/otel-collector-config.yaml | 未启用的外部 Collector 示例；不是可直接 apply 的 K8s 资源 |

不要 kubectl apply -f 整个目录。API 单副本、Recreate，使用 RWO PVC + SQLite 管理临时上传；
不能直接扩成多副本。K8s API RBAC 只允许本 namespace 内创建/读取 Job 和读取 Pod/log。

## 1. 镜像和节点

```bash
docker build -f Dockerfile.worker -t ianwoolf/evaluation-worker:20261025-env-v1 .
docker pull ianwoolf/evaluation-elasticsearch:8.18.1
bash deploy/kubernetes/import-local-image.sh ianwoolf/evaluation-worker:20261025-env-v1 ianwoolf/evaluation-elasticsearch:8.18.1
```

上述导入脚本默认使用 Docker Desktop kind helper kindest/node:v1.36.1；需本地已有该镜像。
其它版本设置 KIND_NODE_IMAGE，标准 kind 设置 KIND_CLUSTER_NAME。多节点需每个节点都有镜像。
非 kind 集群用其镜像加载工具，或推到仓库后修改 services.yaml 的 API image、
EVAL_WORKER_IMAGE 与 imagePullPolicy（清单及动态 Job 均为 IfNotPresent）。
ES imagePullPolicy 也应对应调整。ARM/x86 目标架构必须匹配。
离线可用 docker save/load；新电脑无需重新拉源代码构建，但仍需要本目录清单和初始化资料。

## 2. 私有配置与 Secret

准备 Python 环境（用于渲染 Secret）：
```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
kubectl create namespace evaluation --dry-run=client -o yaml | kubectl apply -f -
```

将 examples/backend.env.example、models.json.example 复制到仓库外私有目录并填写：
- EVAL_MODEL_REGISTRY：models.json 的绝对路径。
- EVAL_MODEL_RESOLVER_TOKEN、EVAL_TRIGGER_TOKEN：各生成一个独立的随机值。
- models.json 中 glm-dev 对应 address/model/token。none 无需注册，也不会调用 resolver。
  若只使用 none，models.json 可为 {}，但本部署仍要求 resolver/trigger Secret 存在。
- glm-dev 示例地址和模型来自当前项目设置，不保证供应商账号在新电脑网络下可用。

```bash
set -o pipefail
PYTHONPATH=src python deploy/kubernetes/render_secrets.py --config /absolute/private/backend.env | kubectl apply -f -
```

开启 shell 的 set -o pipefail 后执行管道，可使渲染失败正确返回非零。Secret 输出包含凭据，
只直接传给 kubectl，勿打印、保存到源码或粘贴到文档。
evaluation-trigger 同时被 UI Control API 引用；模型供应商 token 仅放 evaluation-models，
Job 不应直接挂载 models.json。每个运行的 EVAL_STORAGE_TOKEN 由 API 签发并限定 project/run。

## 3. 部署

确认 kubectl current-context 是目标集群、默认 StorageClass 可用。没有默认类时修改 ES
volumeClaimTemplates 和 uploads.yaml 为实际类名；不要沿用旧电脑的 standard。
```bash
kubectl apply -f deploy/kubernetes/elasticsearch.yaml
kubectl apply -f deploy/kubernetes/uploads.yaml
kubectl -n evaluation rollout status statefulset/evaluation-es --timeout=180s
kubectl apply -f deploy/kubernetes/services.yaml
kubectl -n evaluation rollout status deployment/evaluation-api --timeout=180s
kubectl -n evaluation get pods,svc,pvc
```

services.yaml 中：
- EVAL_RESOURCE_BINDINGS 允许 guardrail-privacy / ds-guardrail v1 / env-isolated / glm-dev。
  none 由所选 Environment 版本决定，可在同一 binding 下使用；修改业务 ID 时同步修改 binding。
- 默认 target provider 为 nemo guardrail；自定义 target 用 EVAL_TARGETS JSON。
- EVAL_CONTENT_POLICY 默认保留 input/response/reference；可分别改成 omit，对新运行生效。
- 集群服务名默认 evaluation-api:8001 和 evaluation-es:9200，跨命名空间地址固定为 evaluation。
- CPU/Memory 从 Environment 版本读取：1000m=1 core，1024Mi=1Gi，requests=limits。
- 临时 CSV/XLSX 上传由 API 验证 schema；终态/过期清理，不挂载到 worker。
- 单节点 ES 为开发配置，不开放宿主机端口；有网络共享需求应另配认证/TLS及访问控制。

## 4. 初始化 dataset / Environment / target Config

这些业务数据在 ES 中；部署 YAML 不会自动生成。examples 提供两个合成样本，
兼容手动上传字段 messages（JSON）、has_issue（boolean reference）、category（metadata）。

单独终端转发：
```bash
kubectl -n evaluation port-forward service/evaluation-api 18001:8001 --address 127.0.0.1
```

以下命令在新安装、空的 individual 项目中执行。该项目需与 UI 登录后的项目一致；
可从浏览器路由确认项目 ID。勿在已有同名 dataset 上重放初始化 POST：可能覆盖已有文档。
```bash
read -r -s -p 'Backend trigger token: ' EVAL_TRIGGER_TOKEN
export EVAL_TRIGGER_TOKEN
export EVAL_PROJECT=individual
curl --fail-with-body -sS -H "Authorization: Bearer $EVAL_TRIGGER_TOKEN" -H "X-Project-ID: $EVAL_PROJECT" -H 'Content-Type: application/json' --data-binary @deploy/examples/dataset.json http://127.0.0.1:18001/api/v1/evaluations/datasets/ds-guardrail
curl --fail-with-body -sS -H "Authorization: Bearer $EVAL_TRIGGER_TOKEN" -H "X-Project-ID: $EVAL_PROJECT" -H 'Content-Type: application/json' --data-binary @deploy/examples/environment.json http://127.0.0.1:18001/api/v1/evaluations/environments/env-isolated
curl --fail-with-body -sS -H "Authorization: Bearer $EVAL_TRIGGER_TOKEN" -H "X-Project-ID: $EVAL_PROJECT" -H 'Content-Type: application/json' --data-binary @deploy/examples/target-config.json http://127.0.0.1:18001/api/v1/evaluations/targets/guardrail-privacy/config
unset EVAL_TRIGGER_TOKEN
```

Environment 示例使用 none，target Config 使用 mock-score，先验证基础链路，不花费模型调用。
mock-score 固定返回 1；切换 glm-dev 时保存 Environment 新版本，并在 target Config 选择该版本。
已保存配置固定版本，不会自动跟随 Environment 最新版本。使用真实 scorer 时需校验它的 input/
output/reference 合同及 dataset metadata。内置 sc-risk 要求 output.has_issue，不能直接对原始
messages 的 none 输出使用。已存在的环境和 Config 更新要带 GET 返回的 revision。

UI 中 Trigger 名称可留空，后端使用 target+当前时间；upload 模式每次必须上传新的 CSV/XLSX，
Config 只存模式与 schema 版本，不存文件。详细 API 见
[环境和 target](../docs/environments-targets.md)、[临时数据](../docs/temporary-datasets.md)。

## 5. 验收、日志、迁移

```bash
curl --fail http://127.0.0.1:18001/health
kubectl -n evaluation get jobs,pods
kubectl -n evaluation logs job/替换为本次evaluation-id
```

runner 日志应包含 evaluation.environment（包括 guardrailId）和 evaluation.target，
最终 status=COMPLETED。none 模式模型耗时为 0，无模型 span；collector 仍记录 sample/score/trace。
OTel 当前只支持 HTTP JSON traces，不自动采集 Pod stdout；用 kubectl logs 查看启动日志。

更新 API image 和 EVAL_WORKER_IMAGE 要同步，新 tag 导入所有节点后 rollout。运行中的 Job
不会自动换镜像。API 创建的 Job 保留到人工清理（不设 TTL），Job deadline 固定 600s、
backoffLimit=0；不要把 Environment.timeoutSeconds 误认为已控制这个 deadline。
裸 build_job CLI 模板的 TTL 为 24h，与 API 路径不同。

迁移必须分别处理 ES snapshot、PostgreSQL、Secrets；不要删除 PVC 来解决镜像错误。
新 PVC ES=5Gi、uploads=1Gi；旧机实际 ES PVC 可能为 2Gi，新模板不会自动扩容旧卷。
ES 数据恢复不等于恢复 K8s Job 历史。现有临时上传运行应结束后再搬迁。

此资料的验证范围：清单结构、脚本语法、初始化示例与后端测试；未在第二台真实机器执行部署。

手动上传样例：见 [CSV / Excel 与生成脚本](examples/manual-upload/README.md)，12 条合成数据，不含真实用户数据。
