# OpenShell + Langfuse execution

本阶段把业务执行入口改为 OpenShell。`openshell_runner` 是控制端，`worker` 是沙箱内 Python 进程。旧 `evaluation_backend.runner` 的直接 docker/kubectl 模式保留为底层载体验证工具，不再作为业务评测入口。

## 运行路径

`OpenShellRunner → 指定 Gateway → sandbox create → sandbox exec worker → Langfuse Dataset → target → installed scorer library → Langfuse experiment/score feedback`。

`EVAL_RUNNER_MODE=docker|k8s` 选择已经注册的对应 OpenShell Gateway。Gateway 的 compute driver 决定真实 sandbox 是 container 还是 Pod；不是在普通 Docker container 内嵌套启动 Docker。K8s 下 sandbox Pod 生命周期由 OpenShell 管理，不再由旧 runner 创建 batch Job。两个 Gateway 的部署/driver 需要运维配置，本模块不自动部署 Gateway，也不声称一个 env 就改变现有 Gateway 的 driver。

## 环境变量

| 变量 | 说明 |
|---|---|
| DATASET_ID | Langfuse 数据集真实 ID；适配器先解析 ID → name，再调用 SDK，不把 ID 当名字 |
| EVALUATE_ID | 本次测评标识，作为 Langfuse experiment name；兼容 EVALUATION_ID |
| SCORER_IDS | JSON 数组，例如 `["sc-risk"]`；不是逗号分隔字符串 |
| DATASET_VERSION | 可选 ISO UTC 时间戳；未设置时控制端固定启动时刻并传入沙箱 |
| EVAL_PLATFORM | 当前 langfuse；langsmith 未实现时明确报错 |
| EVAL_CONCURRENCY | 1–32，默认 1 |
| EVAL_TARGET_URL | Guardrail HTTP POST 地址，返回对象含 boolean has_issue |
| EVAL_MODEL_ID | 可选；目标请求 model 固定为此值 |
| EVAL_TARGET_TOKEN | 可选的目标服务短期 token |
| LANGFUSE_BASE_URL / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY | Langfuse 服务和凭证 |
| EVAL_RUNNER_MODE | docker / k8s |
| EVAL_OPENSHELL_DOCKER_GATEWAY | Docker compute driver Gateway 的注册名 |
| EVAL_OPENSHELL_K8S_GATEWAY | K8s Gateway 的注册名 |
| EVAL_WORKER_IMAGE | 已构建、含 worker/library 和 Langfuse extra 的沙箱镜像；建议固定 digest |
| EVAL_OPENSHELL_POLICY | 控制端存在的 OpenShell policy 文件路径，需允许目标与 Langfuse endpoint |
| EVAL_EXECUTION_TIMEOUT | exec 时限，默认 600 秒 |
| EVAL_OPENSHELL_BIN | OpenShell CLI 路径，默认 openshell |

Langfuse Dataset metadata 声明评分器配置：

```json
{"evaluation":{"scorers":{"sc-risk":{"version":"1","library":"risk-classification","threshold":0.8}}}}
```

Dataset item.input 为 `{messages:[{role,content}]}`；expected_output 为 `{has_issue: false}`，也兼容 boolean 标准答案。目标只收到 input.messages 和可选 model，不收到 reference。评分器获取 input/output/reference，返回领域 `Score(value,reason)`；SDK adapter 再映射为 Langfuse 的评价对象并写回。无效输出/评分错误是 ERROR，不返回伪造 0 分；SDK 的逐样本错误隔离不会被误判为整个任务成功。

Langfuse 没有被当作任意 Python scorer 脚本下载/执行服务。metadata 仅保存 library 名称、版本和阈值；代码预装在受控镜像，通过 `evaluation_backend.scorers` entry point 注册。内置 risk-classification 比较预测和参考 has_issue，匹配为 1，否则为 0，理由区分误报/漏报。其他 scorer 必须先安装对应 library，否则任务启动前报错。

## 使用

```bash
.venv/bin/python -m pip install -e '.[dev,langfuse]'
docker build -f Dockerfile.worker -t evaluation-worker:dev .
# 显式 export 上述配置和受限凭证；不要将真实值提交到 Git。
export DATASET_ID='<langfuse-dataset-id>'
export EVALUATE_ID='guardrail-regression-001'
export SCORER_IDS='["sc-risk"]'
export EVAL_RUNNER_MODE=docker
export EVAL_OPENSHELL_DOCKER_GATEWAY=eval-docker
export EVAL_WORKER_IMAGE=evaluation-worker:dev
export EVAL_OPENSHELL_POLICY=/absolute/path/to/policy.yaml
.venv/bin/python -m evaluation_backend.openshell_runner
```

镜像 ENTRYPOINT 是 worker，但 OpenShell sandbox 的存活进程显式设为限时 sleep，再以 exec 运行一次评测。依赖 OpenShell CLI 已安装、Gateway 已配置并可访问、镜像可拉取；镜像还需兼容所锁定的 OpenShell supervisor，必须真实集成验证。本机目前没有 OpenShell CLI/Gateway、也没有提供 Langfuse/Guardrail 凭证，测试使用注入的客户端和 CLI stub，不能视为真实沙箱/平台联调通过。

## 抽离边界与当前限制

- `experiment.py` 只有平台无关 Settings / DatasetSource / ScorerLibrary / ExperimentReporter / Score；`worker.py` 不导入供应商 SDK，平台由 factory 创建。
- `platforms/langfuse.py` 负责数据集 ID/name 转换、版本读取，以及 Langfuse 原生 experiment/traces/score feedback。切换 LangSmith 时实现相同 source/reporter 协议并注册 factory，复用 target/library/worker/OpenShell。
- 当前通过 SDK 原生 experiment 生命周期发送结果。已有 ResultSink + 本地 JSONL 接口保留用于后续 outbox；本次没有实现可靠跨平台 outbox、重放或 exactly-once。不要自动重跑不确定的 target 请求。
- 评分器元数据不是 Langfuse 的独立不可变发布实体；执行时记录 metadata hash 和 scorer version，镜像应固定 digest。完整 snapshot/发布权限及 scorer bundle 分发仍待接入业务 API。
- 仅适用于可信 target adapter 与预装 scorer；它们同处 worker 进程，不提供不可信评分代码与凭证的进程级隔离。运行任意用户代码前必须补充独立沙箱。
- 凭证不出现在 CLI argv：通过本地 0600 临时 JSON 上传，worker 读取后进入进程环境。凭证文件仍属于敏感沙箱文件，需短期受限凭证及访问控制；不能声称所有沙箱代码不可见。
- 成功后删除沙箱；失败后 stop 并保留，控制台输出 sandbox 名以便诊断和人工清理；创建响应不确定时禁止盲目重试。SIGKILL/控制端宕机的自动回收仍需外部 coordinator/reaper。
- SDK 会将输入、输出、reference 和反馈发送到所配置 Langfuse 项目。部署前确认项目访问权限和数据策略。真实 model override 防绕过需要外部强制代理，本 HTTP adapter 仅显式构造 model 字段。

参考：[OpenShell CLI](https://github.com/NVIDIA/OpenShell/blob/main/deploy/man/openshell.1.md)、[Langfuse experiments](https://langfuse.com/docs/evaluation/experiments/experiments-via-sdk)。SDK 可选依赖锁定 4.18.0，并用实际安装签名做契约检查。

## Local runner configuration

The OpenShell launcher and worker read `.env` from the working directory. Select another
file with `--config PATH` or `EVAL_CONFIG_FILE`. Existing environment values take precedence.
Credentials: `LANGFUSE_BASE_URL`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`.
Experiment and score metadata include `experiment_id`, using `EVALUATE_ID`.
Inside a Docker sandbox, localhost is the sandbox itself; use a reachable host address
such as `http://host.docker.internal:3000` for Docker Desktop.

Explicit score delivery test (from the backend directory):

```sh
.venv/bin/python examples/langfuse_score_smoke.py --config .env --dataset-id cmvff7e84000dpq4o7rg21lhd --experiment-id dev_1
```

This reads the real dataset and uses synthetic always-safe predictions, marked `test_only`.
Each invocation writes a new batch of traces and scores with metadata `experiment_id: dev_1`,
and verifies the scores through the read API. It does not start OpenShell, call a real target,
or change Dataset/scorer definitions.

### Experiment identity and live-model integration test

`OpenShellRunner.run()` generates a fresh `exp-<uuid>` before each launch and uploads
`EXPERIMENT_ID`, `EVALUATE_ID`, `EVALUATION_ID` with the same value. Child processes
preserve it. The local Docker proxy supervisor generates an identity if none was supplied.
Runner telemetry uses a dedicated OTEL provider with experiment_id on the resource and
all spans (including Langfuse SDK experiment/item/evaluator spans), plus Langfuse metadata
and tags. LLM requests propagate W3C traceparent to link generations to dataset items.

The explicit integration modes `EVAL_TARGET_MODE=llm` and `EVAL_SCORER_MODE=mock`
read real Langfuse dataset items, classify their conversations through the local proxy and
`glm-dev`, load `mock-score` from a test-only library, and publish real score feedback.
The mock scorer returns 1.0 for every sample; this is not a quality measurement.
It does not change Dataset metadata or install a real scorer.

Run the OpenShell test from the backend directory (local ignored test config):

```sh
.venv/bin/python -m evaluation_backend.openshell_runner --config data/openshell-test.env
```

Requires the backend model resolver on port 8001, local Langfuse, installed OpenShell
Gateway `openshell`, image `evaluation-worker:experiment`, and the supplied example policy.


### Integration result, 2026-10-20

OpenShell 0.1.4 CLI and local Docker-driver Gateway were installed and connected.
Actual CLI testing fixed the sandbox name limit (19 characters) and separated sandbox
creation from upload, because `create --upload` conflicts with a custom command.

OpenShell provisioning remains blocked on this machine's Docker Desktop Engine 27.1.1:
`BoundaryConfigWriteFailed: ... getent unable to find entry "10001:10001" in passwd database`.
It occurs in the official driver's archive upload with `copyUIDGID=true`, before the worker
starts, including with explicit non-root UID/GID policy. No OpenShell run is claimed successful.
A Docker engine/runtime combination compatible with this OpenShell driver is required;
upgrading Docker Desktop and retrying is the next environment check, not a verified fix.

The same worker image was separately validated in a real Docker container, using real
Langfuse dataset `cmvff7e84000dpq4o7rg21lhd`, backend model ID `glm-dev`, real provider
requests, and mock scorer `mock-score` returning 1.0. Run completed with 6 items and zero
errors. Experiment label: `exp-b15094dc1ab844bb949147dbf9bc1463`.
Langfuse experiment ID: `50ad5f6116137c82`. Readback verified 6 scores, 6 generations,
6 evaluator observations and 18 other spans, all labelled with the runner experiment ID.
The generation usage, TTFT, duration and response model were present. The mock scores are
not model quality measurements. An earlier Docker diagnostic batch also sent test data
before exposing an SDK result-URL field mismatch; that mismatch has been fixed.

### Confirmed Docker root cause (minimal reproduction)

A direct PUT `/v1.46/containers/{id}/archive?path=/tmp&copyUIDGID=true` into a disposable
container configured with User `10001:10001`, uploading only a plain text file, reproduces
`getent unable to find entry "10001:10001" in passwd database` on Engine 27.1.1.
The identical upload with `copyUIDGID=false` returns HTTP 200. Probe containers were removed.

Moby v27.1.1 `daemon/archive_tarcopyoptions_unix.go` calls
`idtools.LookupUser(container.Config.User)` on the entire user:group string.
Moby v28.0.0 replaces this with `getUIDGID`, splits on `:`, and resolves numeric UID and GID
independently. This establishes a specific engine defect rather than a worker permission issue.
Use an Engine version containing that fix (confirmed in v28.0.0 source), then rerun the minimal
probe and OpenShell integration. End-to-end OpenShell success after upgrade is still unverified.
Changing `copyUIDGID` in OpenShell is not applied here: it changes ownership semantics and
must be validated for both the workload bundle and supervisor bundle before being used.

Sources:
https://github.com/moby/moby/blob/v27.1.1/daemon/archive_tarcopyoptions_unix.go
https://github.com/moby/moby/blob/v28.0.0/daemon/archive_tarcopyoptions_unix.go

### Retry after Docker upgrade (2026-10-20)

Engine upgraded to 29.9.1. The archive UID:GID error is resolved; real OpenShell creation
now reaches supervisor startup. Capturing Docker exit events before automatic cleanup
revealed `seccomp notification probe: notification launcher disappeared` (workload exit 1).

The machine has `.wslconfig` networkingMode=mirrored. WSL PID 1 reports Seccomp=2 and
Seccomp_filters=1. A minimal non-root Docker probe successfully sets no_new_privileges,
but seccomp(SET_MODE_FILTER, NEW_LISTENER) fails with errno 16 EBUSY, reproducing the
specific notification-listener conflict independently from OpenShell and evaluation code.
The WSL kernel is 6.18.54.1; merely updating the kernel is therefore insufficient here.

OpenShell upstream issue #3842 documents the mirrored-mode inherited listener conflict
and successful NAT-mode workaround. Changing global WSL networking and restarting WSL
requires user approval because it affects all WSL processes and current Docker services.
No security enforcement has been disabled. Failed test sandboxes have been removed.
https://github.com/NVIDIA/OpenShell/issues/3842

### OpenShell end-to-end verified after NAT switch (2026-10-20)

This supersedes the earlier provisioning-blocked results above. With user approval,
`.wslconfig` was backed up and networkingMode changed from mirrored to nat, followed by
Docker Desktop / WSL restart. PID 1 now has no seccomp filter; NEW_LISTENER probe succeeds.

Additional compatibility fixes required:
- Gateway `/root/.config/openshell/gateway.toml`: `[openshell.drivers.docker]`
  `grpc_endpoint = "https://host.docker.internal:17670"` for supervisor-to-WSL access.
- Worker image WORKDIR is `/home/worker`, owned by the non-root workload user.
- Local services use Docker Desktop host IP `192.168.65.254` with explicit `/32`
  allowed_ips entries for ports 3000 and 8001 in the example policy. Current OpenShell
  reserved-alias DNS rejects host.docker.internal without a trusted host gateway; the
  explicit IP policy avoids this restriction without disabling network enforcement.
  Re-resolve this address when moving machines or changing Docker Desktop networking.
- Proxy still uses trust_env=False to avoid inherited proxy routing, but now explicitly
  loads SSL_CERT_FILE / REQUESTS_CA_BUNDLE into a verified SSLContext. This trusts the
  runtime's injected CA without disabling TLS verification.

Actual OpenShell launch read 6 Langfuse dataset items, made 6 successful real glm-dev calls,
loaded the explicit mock-score library, and sent 6 mock scores to Langfuse. Worker completed
with zero errors and the sandbox was automatically deleted. The mock scores are all 1.0
and are not a quality assessment.

Runner experiment_id: exp-468a9fb028e44ba891b2aee864b87ef6
Langfuse experiment ID: 672b6279181519c3
Readback: 30 labelled observations (6 GENERATION, 6 EVALUATOR, 18 SPAN), 6 scores, all
6 generations containing token usage, TTFT and duration. All 56 backend tests pass.
Local readback artifact: ignored data/openshell-verification.json.
