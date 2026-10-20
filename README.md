# Evaluation Backend

独立的评测后端工程。当前位于 AgentEval 下，但拥有独立 Git 仓库；外层通过 `.gitignore` 忽略整个目录，不使用 submodule，不导入外层工程代码。可以整体移动到其他目录后继续开发。

## 当前已实现

- FastAPI 入口、`GET /health`、OpenAPI `/docs`。
- 平台无关 VersionRef / ResultEnvelope / ResultSink 契约骨架。
- 本地 JSONL 开发 sink 和最小测试。

尚未实现业务 CRUD、持久化、OpenShell 执行、Langfuse/LangSmith 发送、认证和授权。健康接口仅表示进程存活。此工程暂不替换现有 Web 模拟工作台。

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


## 当前业务执行入口：OpenShell

业务评测已增加 OpenShell 启动器和环境变量驱动 worker。Dataset 直接从 Langfuse 读取，评分器配置来自 Dataset metadata、代码来自预装 library；使用 SDK experiment 执行目标、评分并反馈平台。平台通过 DatasetSource / ScorerLibrary / ExperimentReporter 隔离，LangSmith 适配器后续实现。

```bash
.venv/bin/python -m pip install -e '.[dev,langfuse]'
# 配置 DATASET_ID / EVALUATE_ID / SCORER_IDS / target / Langfuse / OpenShell
.venv/bin/python -m evaluation_backend.openshell_runner
```

完整变量、metadata 格式和限制见 [OpenShell worker](docs/openshell-worker.md)。旧 docker/kubectl runner 是底层验证工具；业务路径不绕过 OpenShell。OpenShell CLI/Gateway 和真实 Langfuse/Guardrail 凭证尚未在本机配置，真实端到端联调尚未完成。当前结果回传由 Langfuse SDK 处理，可靠 outbox 和业务数据库尚未接入。


### Container LLM proxy

See [LLM proxy](docs/llm-proxy.md) for model resolution, OpenShell/Docker startup and OTEL metrics.
The backend now exposes an authenticated internal model resolution endpoint in addition to health.
Local provider credentials are stored in ignored `data/models.json`, referenced by `.env`.


### Kubernetes deployment

See [local Kubernetes deployment and evaluation Jobs](deploy/kubernetes/README.md).
Includes API service manifests, secret rendering and an explicit direct-Job trigger CLI;
the guide separately describes the OpenShell Kubernetes sandbox path and its prerequisites.
