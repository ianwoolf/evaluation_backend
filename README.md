# Evaluation Backend

## 部署入口：Kubernetes / Docker Compose

两种方式独立部署，配置和数据不自动共享：

- [Docker Compose](deploy/docker-compose/README.md)：仅需 Docker + Compose v2，API 通过 Docker Engine 启动 runner 容器；前端使用主工程 `web/deploy/docker-compose`。
- [Kubernetes](deploy/kubernetes/README.md)：API 创建 K8s Job；前端使用主工程 `web/deploy/kubernetes/evaluation-ui.yaml`。

普通参数、模型 JSON、资源绑定、UI JSON 放 ConfigMap（Compose 对应 configs 文件）。
K8s 活动部署统一使用 `evaluation-credentials` Secret，只包含 token、密码、API key 等；
runner 按键引用必需凭据，不注入全局签名 token 或模型供应商 token。
模型 JSON 使用 `token_env` 引用 Secret 中的键，模型 token 本身不在 JSON 配置里。

独立的评测后端工程。当前位于 AgentEval 下，但拥有独立 Git 仓库；外层通过 `.gitignore` 忽略整个目录，不使用 submodule，不导入外层工程代码。可以整体移动到其他目录后继续开发。

## 当前已实现

- FastAPI 入口、`GET /health`、OpenAPI `/docs`。
- Dataset、Environment、Target 配置及版本管理，项目隔离与乐观并发控制。
- 创建 Kubernetes 评测 Job；worker 执行样本及评分，进程内 collector 将结果写入 ES。
- 从 ES 读取运行状态、样本、评分、报告与 trace；模型调用和存储适配器分别抽象。
- 平台无关存储接口；当前实现 Elasticsearch，LangSmith 尚未实现。

当前部署走直接 Kubernetes Job，不依赖 Langfuse 或 OpenShell Gateway。仓库仍保留早期 OpenShell/Langfuse 路径，见下方历史实现说明。健康接口仅表示进程存活。

当前 UI 是独立静态评测工作台，直接访问 `/api/v1/evaluations/*`。
本机部署已移除 Control API，不再依赖 PostgreSQL 或浏览器登录；详见
[精简部署设计](docs/simplified-deployment.md)。部署清单默认 `EVAL_AUTH_MODE=none`，
固定项目为 `EVAL_DEFAULT_PROJECT_ID=individual`；未显式配置模式的独立进程仍默认 token。
runner 内部签名与模型解析凭据保留在服务端，不提供给浏览器。

## Elasticsearch：三个业务索引

以下是当前默认索引名，可通过 ConfigMap 修改。**三个索引都在使用，`evaluation-traces-v1` 虽然带 v1，也不是可删除的旧索引。**

| 索引 | 存储内容 | 文档粒度 |
| --- | --- | --- |
| `evaluation-records-v2` | Dataset、Environment、Target 配置，以及评测运行配置和汇总 | 资源的版本/当前引用，或一次评测运行 |
| `evaluation-samples-v2` | 样本输入、参考答案、输出、状态、耗时、费用及全部评分 | 某次 run 的一个样本 |
| `evaluation-traces-v1` | 样本执行步骤、模型调用与 OTLP trace 数据 | 一条 trace 记录 |

### 1. evaluation-records-v2：资源与评测运行

公共字段为 `project_id`、`record_type`、`id`；`record_type` 区分以下类型：

| record_type | 内容与主要字段 |
| --- | --- |
| `dataset` | `spec` 中保存名称、目标、字段 schema、rows、metadata 和可用评分器 `scorerIds` |
| `environment` | `spec` 中保存模型/Guardrail 配置、runner、CPU、内存、超时和执行限制 |
| `target` | `spec` 中保存 id、name、kind、provider、version、definition；state=head，复用资源索引 |
| `target-config` | `spec` 中保存 dataset/environment 的 ID 和版本、scorer IDs、评分模式、published/upload 数据来源 |
| `run` | 保存本次测评的资源引用、固定执行配置、状态、进度和最终评分汇总 |

Target 目录不再读取 `EVAL_TARGETS`，也没有运行时默认列表。UI 继续调用
`GET /api/v1/evaluations/targets`（支持 offset/limit），数据按项目从存储读取。
`GET /api/v1/evaluations/targets/{id}` 读取详情和 revision；
`POST /api/v1/evaluations/targets/{id}` 新增或更新，例如：

```json
{
  "id": "guardrail-privacy",
  "name": "Privacy & prompt injection",
  "kind": "GUARDRAIL",
  "provider": "nemo guardrail",
  "version": 2,
  "definition": "Block private-data requests and instruction override attempts."
}
```

更新需携带读取到的 `revision` 并增加 `version`；已有测评保留启动时的 target 快照。
升级时先通过上述接口导入旧 target（保持原 ID），或显式执行
`deploy/docker-compose/seed_demo.py` 初始化演示资源；不会覆盖已有资源。
Target 定义与 target-config 测评选择分别存储，不新增 ES 索引。

资源版本使用 `state: published`、`version`、`spec`；另有一条 `state: head` 文档通过
`version_keys` 指向已提交的版本。Dataset head 的 `spec` 是草稿，Environment head 的
`spec` 为空，Target 配置 head 的 `spec` 是当前配置。版本内容先写入，再通过乐观并发更新
head；失败更新可能留下未被引用的版本，业务 API 不会将它们作为已提交版本返回。
Dataset 的 rows 目前保存在各版本的 `spec` 内，没有另建 dataset-items 索引。

`run` 文档的主要字段：

| 字段 | 含义 |
| --- | --- |
| `id` / `run_id` | 同一次测评的标识；现有 HTTP API 仍使用 `evaluation_id` / `experiment_id` 命名 |
| `name`、`job_name` | 测评名称和 Kubernetes Job 名称 |
| `target_id`、`target` | 被测目标 ID 和目标快照 |
| `dataset_id`、`dataset_version` | 本次使用的数据集版本 |
| `environment_id`、`environment_version`、`execution_config` | 执行环境引用和本次实际使用的配置快照 |
| `scorer_ids`、`scoring_mode` | 打分器及 mock/installed 模式 |
| `upload_id`、`content_policy` | 临时上传引用和输入/输出/参考答案的留存策略 |
| `status`、`total_samples`、`completed_samples`、`failed_samples` | 运行状态与样本计数；failed_samples 对应执行错误计数，不是评分未通过数量 |
| `score_summary` | 各评分器的版本、已评分数量、平均分、通过数量 |
| `created_at`、`updated_at`、`finished_at` | 创建、汇总更新和结束时间 |

原 `experiments` 和 `run-contexts` 现在共享同一条 run 文档，更新结果时保留执行配置。
`context_ready`、`summary_ready`、`request_hash` 是内部幂等/写入控制字段。
部分旧运行没有曾经未持久化的名称、Job 信息或评分汇总；迁移不会伪造这些字段。

### 2. evaluation-samples-v2：样本与评分

| 字段 | 含义 |
| --- | --- |
| `project_id`、`run_id`、`sample_id` | 所属项目、运行及样本标识 |
| `id` | 运行和样本组合生成的逻辑存储键，防止跨运行重名 |
| `input`、`reference`、`output` | 输入、参考答案、实际输出，按留存策略过滤后写入 |
| `status`、`error` | PASS / FAIL / ERROR 及执行错误 |
| `latency_ms`、`model_latency_ms`、`cost_usd` | 总耗时、模型耗时及费用 |
| `scores` | 本样本的所有评分结果 |
| `trace_id`、`created_at` | 样本执行 trace 的引用和写入时间 |

每个 `scores` 元素包含 `scorer_id`、可选的 `scorer_version`、`status`、`value`、
`passed`、`reason`。评分随样本一起保存，不再单独写入 scores 索引。
ES 对 scores 使用 `nested` mapping，避免查询时把不同评分器的字段错误匹配到一起。
因此 `_cat/indices` 的 `docs.count` 包含 nested 内部文档；查看实际样本数量应使用
`GET /evaluation-samples-v2/_count`，或按 `run_id` 查询。

样本是评分明细的事实来源，run 中的 `score_summary` 是终态汇总。
样本只存 `trace_id`，不重复保存执行步骤；适配器读取 trace 后组装现有 UI 所需的响应。

### 3. evaluation-traces-v1：执行链路

此索引沿用原有格式：顶层是 `project_id`、`id`、`evaluation_id`、可选的 `sample_id`、
`created_at` 等检索字段，完整记录放在 `payload`。
`payload` 包含 trace 的 `name`、`data`，以及可用时的目标信息。

- 样本执行 trace：保存评分器/目标执行步骤及耗时；新记录不再复制整个样本和评分。
- 模型调用或 OTLP trace：保存 collector 接收到的调用、span 及属性信息。
- 某些运行级 trace 不关联具体样本，因此不一定有 `sample_id`。

样本的 `trace_id` 对应 trace 的逻辑 `id`，同时必须匹配 `project_id`；
trace 的 `evaluation_id` 对应样本/运行的 `run_id`。ES `_id` 是适配器生成的项目隔离哈希，
不应直接当作业务 trace ID 使用。删除 traces 索引会丢失历史执行链路，剩余样本和评分
不能完整重建原始 trace。

### 配置与读取关系

后端 Deployment 和新建 runner Job 共用 `evaluation-storage` ConfigMap：

```yaml
EVAL_STORAGE_BACKEND: elasticsearch
EVAL_ES_URL: http://evaluation-es:9200
EVAL_ES_VERIFY_SSL: 'false'
EVAL_ES_RECORDS_INDEX: evaluation-records-v2
EVAL_ES_SAMPLES_INDEX: evaluation-samples-v2
EVAL_ES_TRACES_INDEX: evaluation-traces-v1
```

ES API key 和密码放在 `evaluation-credentials` Secret 中，不放 ConfigMap。
用户名 `EVAL_ES_USERNAME` 放在 `evaluation-storage` ConfigMap，密码使用 Secret 的 `EVAL_ES_PASSWORD`，
与 API key 互斥。ES 开启认证、密码环境变量和认证探针的完整步骤见
[ES 认证部署](deploy/kubernetes/elasticsearch.md#可选启用用户名密码认证)。
`EVAL_ES_VERIFY_SSL` 默认 `false`，HTTPS 连接跳过服务端证书验证；设为 `true`
启用验证。启用时可用 `EVAL_ES_CA_FILE` 指向挂载的 CA 文件，不指定则使用系统 CA。
验证关闭时忽略 CA 文件配置。该开关仅影响 ES 客户端，不影响模型或 OTel 连接。
修改 ConfigMap 后重启 API；已运行的 Pod 不会自动更新环境变量，新 Job 会加载新值。

资源页面读取 records 中对应类型的资源；报告读取 run 和按 `run_id` 分页的 samples，
需要执行步骤时再读取 traces。三个索引都通过 `project_id` 隔离项目。
ES 物理布局封装在存储适配器内部，业务接口不直接依赖索引名。

完整存储契约、v1 迁移与回退说明见 [存储服务设计](docs/storage-service.md)，
部署配置见 [Kubernetes 部署](deploy/kubernetes/README.md)。删除索引前应先做 ES 快照；
曾用于迁移的旧索引副本不等于完整的快照备份。

## 运行日志与 OpenTelemetry

后端、runner supervisor、worker、collector、模型代理统一输出 JSON 日志，每行一条。
记录请求/配置保存、Job 创建与幂等重试、数据加载、评分器加载、目标与评分器执行、
样本及汇总持久化、ES/反馈 HTTP 请求、上传管理、进程启动/退出/超时清理等关键节点。
失败日志包含异常类型、调用栈位置和可用的 HTTP 状态码，不包含异常原文、请求体、
模型输入输出、认证信息或进程环境。健康探针不逐次记录，避免刷屏。

公共字段为 `timestamp`、`level`、`service`、`event`，按场景附带 `request_id`、
`project_id`、`run_id`、`sample_id`、`scorer_id`、`job_name`、`duration_ms` 等。
HTTP 请求支持 `X-Request-ID`，可解析传入的 W3C `traceparent`；存在 OTel span 上下文
时附带 `trace_id` / `span_id`。Worker 原有的最终结果 JSON 继续保留，供 Job 状态读取。

- **未配置 OTel endpoint：**直接输出 stdout，可用 `kubectl logs` 查看。
- **配置 endpoint：**同时写 stdout 和 OpenTelemetry，使用 SDK 批量导出 OTLP HTTP/protobuf 日志。
  出口不可用时 stdout 仍有完整的应用事件，遥测失败不改变测评结果。
- `EVAL_LOG_LEVEL` 默认 INFO。可设置 `OTEL_LOGS_EXPORTER=none` 或 `OTEL_SDK_DISABLED=true`
  禁用 OTel；当前日志出口不支持 gRPC。

API 和新建 runner Job 加载 `evaluation-observability` ConfigMap。例如：

```yaml
EVAL_LOG_LEVEL: INFO
OTEL_EXPORTER_OTLP_LOGS_ENDPOINT: http://otel-collector:4318/v1/logs
OTEL_EXPORTER_OTLP_LOGS_PROTOCOL: http/protobuf
```

也支持 `OTEL_EXPORTER_OTLP_ENDPOINT`（基础地址，SDK 自动追加 `/v1/logs`）。认证头通过
统一的 `evaluation-credentials` Secret 配置
`OTEL_EXPORTER_OTLP_LOGS_HEADERS`；不要把 token 写入 ConfigMap 或 endpoint URL。
TLS 证书等使用 SDK 的标准 OTLP 配置。日志导出超时为 5 秒，进程正常退出时 flush。
ConfigMap 修改后需要重启 API，已启动的 runner 不会自动更新环境变量。

这里的 endpoint 必须是支持 **OTLP Logs HTTP/protobuf** 的接收端。
现有 runner 内置 Python collector 处理业务评分和 OTLP JSON traces，尚不支持 `/v1/logs`；
不要将日志 endpoint 指向它。未配置日志接收端时不需要新增日志索引，也不会把运行日志
混入三个评测业务索引。SDK 接入方式参考 [OpenTelemetry Python 日志文档](https://opentelemetry.io/docs/languages/python/instrumentation/#logs)。

```bash
kubectl -n evaluation logs deployment/evaluation-api
kubectl -n evaluation logs job/<evaluation-job-name>
```

## 开发启动（WSL / Linux）

Python 3.10+，在本工程根目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m uvicorn evaluation_backend.api:app --host 127.0.0.1 --port 8001
```

访问 `http://127.0.0.1:8001/health` 和 `http://127.0.0.1:8001/docs`。使用 8001 避免与旧 Python API 的 8000 及 Web 的 18082 冲突。

## 目录

```text
src/evaluation_backend/
  api.py              HTTP 入口
  contracts.py        内部结果事件与版本引用
  publishing/base.py  平台无关发送接口
  publishing/local.py 本地开发输出
tests/                健康接口与发送契约测试
docs/architecture.md  模块、数据、接口与执行设计
docs/roadmap.md       后续实施顺序
```

## Git

本工程尚未配置远程仓库。外层项目不会提交本目录；在本目录执行 `git status` / `git add` / `git commit` 管理后端代码。初始文件保留未提交，便于审阅后建立首次提交。


## Runner：Docker / Kubernetes

已实现可配置 workload runner（内部 Python API + CLI），支持 Docker container / K8s Job 的提交、幂等、状态、日志、取消、超时处理及清理。详见 [runner 设计与用法](docs/runner.md)。这不等于已接入业务评测或 OpenShell。

```bash
export EVAL_RUNNER_MODE=docker  # 或 k8s
.venv/bin/python -m evaluation_backend.runner submit examples/runner-task.json
.venv/bin/python -m evaluation_backend.runner wait runner-example-001
.venv/bin/python -m evaluation_backend.runner logs runner-example-001
.venv/bin/python -m evaluation_backend.runner cleanup runner-example-001
```

`wait` / 定期 `status` 是 Docker 超时执行器，必须持续运行；K8s 使用 Job 原生 deadline。示例只验证 workload 运行，不执行真实 Guardrail 或评分。任务状态库是本地 SQLite；不与未来的业务数据库混用。


## 历史实现：OpenShell / Langfuse（非当前部署链路）

业务评测已增加 OpenShell 启动器和环境变量驱动 worker。Dataset 直接从 Langfuse 读取，评分器配置来自 Dataset metadata、代码来自预装 library；使用 SDK experiment 执行目标、评分并反馈平台。平台通过 DatasetSource / ScorerLibrary / ExperimentReporter 隔离，LangSmith 适配器后续实现。

```bash
.venv/bin/python -m pip install -e '.[dev,langfuse]'
# 配置 DATASET_ID / EVALUATE_ID / SCORER_IDS / target / Langfuse / OpenShell
.venv/bin/python -m evaluation_backend.openshell_runner
```

该历史路径的变量、metadata 格式和限制见 [OpenShell worker](docs/openshell-worker.md)。其中由 Langfuse SDK 回传结果的方式不适用于当前 ES 部署；当前链路以本文的存储说明和 Kubernetes 部署文档为准。


### Container LLM proxy

See [LLM proxy](docs/llm-proxy.md) for model resolution, OpenShell/Docker startup and OTEL metrics.
The backend now exposes an authenticated internal model resolution endpoint in addition to health.
Local provider credentials are stored in ignored `data/models.json`, referenced by `.env`.


### Kubernetes deployment

See [local Kubernetes deployment and evaluation Jobs](deploy/kubernetes/README.md).
Includes API service manifests, secret rendering and an explicit direct-Job trigger CLI;
the guide separately describes the OpenShell Kubernetes sandbox path and its prerequisites.
