# Configurable workload runner

本阶段实现执行载体，配置选择 Docker container 或 Kubernetes batch/v1 Job。目标调用、数据加载、评分和结果回传应由任务镜像中的可信 eval worker 实现；示例只是运行时 smoke，不生成虚假评分。普通 container/Job 不代表已实现 OpenShell 隔离。

## 配置

| 环境变量 | 默认值 | 用途 |
|---|---|---|
| EVAL_RUNNER_MODE | docker | docker / k8s |
| EVAL_DOCKER_CONTEXT | 空 | Docker context；为空使用 CLI 当前 context |
| EVAL_DOCKER_NETWORK | none | 任务网络；需要模型/回传访问时显式配置 |
| EVAL_K8S_CONTEXT | 空 | kubeconfig context；为空使用当前 context |
| EVAL_K8S_NAMESPACE | default | Job 命名空间，需预先创建 |
| EVAL_RUNNER_STATE_DB | data/runner.db | 持久化任务 spec、backend 绑定及取消意图 |
| EVAL_RUNNER_CLI_TIMEOUT | 60 | 单次 CLI 调用超时秒数 |

不自动读取 .env；由进程环境注入。操作历史任务必须使用相同配置和 state DB。迁移 context 时先处理旧任务，不能只改配置后丢弃旧资源。生产应显式设置 context，避免 CLI 当前 context 改变。

```bash
export EVAL_RUNNER_MODE=docker
.venv/bin/python -m evaluation_backend.runner submit examples/runner-task.json
.venv/bin/python -m evaluation_backend.runner wait runner-example-001
.venv/bin/python -m evaluation_backend.runner status runner-example-001
.venv/bin/python -m evaluation_backend.runner logs runner-example-001
.venv/bin/python -m evaluation_backend.runner cleanup runner-example-001

# Kubernetes：预先准备 kubectl、凭证、命名空间及可拉取镜像
export EVAL_RUNNER_MODE=k8s
export EVAL_K8S_NAMESPACE=default
export EVAL_RUNNER_STATE_DB=data/k8s-runner.db
.venv/bin/python -m evaluation_backend.runner submit examples/runner-task.json
.venv/bin/python -m evaluation_backend.runner wait runner-example-001
.venv/bin/python -m evaluation_backend.runner logs runner-example-001
.venv/bin/python -m evaluation_backend.runner cleanup runner-example-001
```

同 id 同 spec 幂等；同 id 改 spec 或切 backend 拒绝，重跑使用新 id。固定 command 列表，不拼宿主 shell。镜像入口由 command 显式覆盖。manifest 限 16KiB，通过 EVALUATION_MANIFEST JSON 传入；只放资源版本、artifact refs 和非敏感参数，凭证后续通过专门引用管理，不能放 env 明文长期密钥。

状态：SUBMITTING / QUEUED / RUNNING / SUCCEEDED / FAILED / TIMED_OUT / CANCELLING / CANCELLED / LOST。SUCCEEDED 仅表示 workload 退出 0，不表示样本评分全部通过。业务结果需要独立 ingestion 判断。

submit 在 CLI 副作用前持久化 intent；提交响应未知时重新 submit 可检查同名/同指纹资源，避免重复启动。操作资源前检查 owner 和 spec 标签；不会接管同名外部资源。取消先保存 CANCELLING，再停止/删除；进程崩溃后 status 可继续取消。终态保留在 DB；cleanup 仅清理终态的自有资源，不删除历史元数据。Job 不配置自动 TTL，避免结果/日志未收集就消失。

Docker 无原生 Job deadline，必须运行 wait 或由 coordinator 定期 status，超时会 stop 并记录 TIMED_OUT；控制进程完全离线时不能保证按时停止，这是当前限制。K8s 使用 activeDeadlineSeconds；backoffLimit=0、restartPolicy=Never，避免平台自动重试有副作用的目标调用。取消使用 foreground deletion 并等待依赖 Pod 退出；超时会保留 CANCELLING 供重试。namespace/context 状态查询报错不能误判为不存在。

默认非 root、只读根文件系统、受限 tmp、CPU/内存限制和禁止提权；不挂 Docker socket、宿主路径、K8s service-account token。K8s 网络由集群 NetworkPolicy 另行落实，不声称等同 Docker 的 network=none。容器镜像必须兼容 UID 65532 及只读文件系统。依赖预装在镜像，运行时不从宿主安装。镜像建议固定 digest。

此接口仅供受信任的 coordinator/CLI 调用，没有新增无鉴权的任意镜像执行 HTTP API。SQLite 只管理本地控制进程生命周期，尚未实现多主机调度、队列、事件 ingestion、重试 attempt、Secret/PVC 或 OpenShell 专用镜像协议。LOST 明确表示外部资源被移除，不能当作成功。logs 在 cleanup 后不可获取，生产需先归档。

依据：[Docker create](https://docs.docker.com/reference/cli/docker/container/create/)；[Kubernetes Jobs](https://kubernetes.io/docs/concepts/workloads/controllers/job/)。依赖本机 docker / kubectl，不增加 Python 容器 SDK。
