# 本地 Kubernetes 部署与测评触发

当前确认：context `docker-desktop`，节点 `desktop-control-plane` Ready，Kubernetes
v1.36.1，containerd 2.3.1，默认 StorageClass `standard`。当前没有安装 OpenShell
Kubernetes Gateway/Agent Sandbox 控制器。API 与普通测评 Job 已实际部署并通过验收，详见文末记录。

## 先明确两条运行路径

Docker Desktop 提供本地 Kubernetes；Job/Pod 由 Kubernetes API 创建，不是通过
`docker run` 创建。镜像仍然用 Docker 构建，节点由 containerd 运行容器。

1. **普通 Kubernetes Job**：Job 中运行 worker + 本地 LLM 代理。能完成 Dataset、
   模型配置查询、真实模型请求、评分和 Langfuse 上报，但没有 OpenShell 沙箱边界。
2. **OpenShell Kubernetes 沙箱**：后端调用 Kubernetes 中的 OpenShell Gateway，
   Gateway 通过 Agent Sandbox 控制器创建 Sandbox、工作负载 Pod、supervisor Pod、
   网络策略和工作区。它不是把现有 Docker Gateway 的名称换成 k8s，也不是普通 Job。

若要一个 Job 代表一次触发，同时保留 OpenShell：该 Job 运行的是 **launcher**，
launcher 调用 Gateway，真正测评发生在另一组 OpenShell 管理的 Pod 中。需要分别
跟踪 launcher Job 状态和 Sandbox 状态，避免只看到 launcher 成功就认为测评完成。

## 服务与配置分工

| 组件 | 部署方式 | 配置/数据 |
|---|---|---|
| evaluation-api | Deployment + ClusterIP Service | 模型注册表 Secret；resolver token |
| 测评 worker | 每次一个 Job，或 OpenShell Sandbox Pod | Dataset ID、模型 ID、评分器 ID、experiment_id |
| LLM 本地代理 | worker 同一容器内受 supervisor 管理 | 从后端解析 address/model/token；发送 generation OTEL |
| OpenShell Gateway | 官方 Helm chart（保留沙箱路径时需要） | Kubernetes 驱动、认证、策略、工作区存储 |
| Agent Sandbox controller | 独立控制器与 CRD | Sandbox 到 Pod/Service/PVC 的协调 |
| Langfuse | 当前继续使用 Compose | Dataset、experiment、score、OTEL；暂不迁移其数据库等依赖 |

`services.yaml` 部署 API、Service、运行参数 ConfigMap 和两个无 API token 的
ServiceAccount。`render_secrets.py` 从本地忽略文件 `.env` / `data/models.json`
生成 Kubernetes Secret。服务商 token 只存在于 API 的模型注册表中；worker 的 Secret
包含 Langfuse key 和 resolver token，不包含服务商 token。

目前 API 只有 health 和模型解析接口，**没有 HTTP 创建测评的接口，也未接前端 Trigger**。
下述触发由本地 CLI 完成。API ServiceAccount 当前没有创建 Job/Pod 的权限，后续实现
经过认证的触发接口时再增加限定到 evaluation 命名空间的 Job RBAC。

## 1. 构建并提供镜像

以下命令在独立后端目录、WSL 下执行：

```sh
docker build -f Dockerfile.worker -t evaluation-worker:experiment .
```

Docker 的镜像缓存不等于 Kubernetes 节点的 containerd 缓存。当前节点报告的镜像列表
中尚未验证存在 worker 镜像。可靠的通用方式是推送到节点能访问的私有镜像仓库：

```sh
# 替换为实际仓库；这一步会向仓库发布镜像，需要使用你自己的仓库及权限。
docker tag evaluation-worker:experiment YOUR_REGISTRY/evaluation-worker:dev
docker push YOUR_REGISTRY/evaluation-worker:dev
```

然后将 `services.yaml` 中 API 的 image 改为此地址，触发 Job 时传同一 `--image`。
生产环境使用不可变 tag 或 digest。私有仓库配置 imagePullSecrets。也可按本地
Docker Desktop provisioning 类型导入节点缓存；只有节点能看到镜像时，本地 tag +
IfNotPresent 才有效，不要用宿主机 `docker images` 代替节点验证。

## 2. 部署 API 与配置

```sh
kubectl --context docker-desktop create namespace evaluation --dry-run=client -o yaml | kubectl --context docker-desktop apply -f -
.venv/bin/python deploy/kubernetes/render_secrets.py --config .env | kubectl --context docker-desktop apply -f -
kubectl --context docker-desktop apply -f deploy/kubernetes/services.yaml
kubectl --context docker-desktop -n evaluation rollout status deployment/evaluation-api --timeout=120s
kubectl --context docker-desktop -n evaluation port-forward service/evaluation-api 18001:8001
```

`render_secrets.py` 的 stdout 包含秘密，只直接管道给 kubectl，不保存到 Git、终端日志
或构建产物。部署清单及 Secret/Job 生成脚本统一输出 YAML。

当前 ConfigMap 的 Langfuse 地址为本机实测 Docker Desktop host IP
`http://192.168.65.254:3000`。部署前确认 Pod 能访问该地址；换机器或网络需要更新。
集群内 API 则使用 `http://evaluation-api.evaluation.svc.cluster.local:8001`。
若 Langfuse 以后迁入集群，改用它的 ClusterIP Service DNS，不再经过宿主机。

## 3. 直接触发普通测评 Job

```sh
# 仅生成 YAML 清单，不创建资源；每次生成新的 experiment_id
.venv/bin/python -m evaluation_backend.k8s_job \
  --dataset-id cmvff7e84000dpq4o7rg21lhd --model-id glm-dev --mock > data/evaluation-job.yaml

# 先进行服务端校验，再提交同一份清单；不会重复生成 experiment_id
kubectl --context docker-desktop create --dry-run=server -f data/evaluation-job.yaml
kubectl --context docker-desktop create -f data/evaluation-job.yaml
```

也可以直接使用同一 CLI 的 `--submit --context docker-desktop`。CLI 会打印 Job 名称和
experiment_id。默认不使用 mock；上例显式 `--mock` 使用固定 1 分的测试评分器。
真实评分时使用 `--scorer-id <id>`（可重复），并确保 Dataset metadata 与安装的 library
匹配。当前示例 target 为真实 LLM 分类目标，不是远端 Guardrail HTTP 服务。

Job 的 Pod 内执行：

```text
proxy_entrypoint
  ├─ 本地 LLM proxy（127.0.0.1:18090）
  └─ worker
       ├─ Langfuse 按 dataset_id 读样本
       ├─ library 加载评分器（测试可显式 mock）
       ├─ target → 本地代理 → API 查询 model_id → 模型服务
       └─ 实验、score、OTEL → Langfuse
```

Job 和 Pod 标签、三个 ID 环境变量、全部 runner OTEL/score metadata 使用同一个
experiment_id。每次触发一个 Job，restartPolicy=Never、backoffLimit=0，防止自动重试
重复调用模型；最长 600 秒，完成后保留 24 小时再清理。worker 不挂 Kubernetes token，
不挂 Docker socket。配置错误、模型错误、评分错误都以非零退出码反馈给 Job。

```sh
kubectl --context docker-desktop -n evaluation get jobs,pods -l app=evaluation-worker
kubectl --context docker-desktop -n evaluation logs -f job/JOB_NAME
kubectl --context docker-desktop -n evaluation wait --for=condition=complete job/JOB_NAME --timeout=650s
kubectl --context docker-desktop -n evaluation describe job JOB_NAME
# 取消单次任务：
kubectl --context docker-desktop -n evaluation delete job JOB_NAME
```

Pod 是 Job 管理的执行单元；推荐创建 Job，不直接创建裸 Pod。

## 4. 保留 OpenShell：Gateway 使用 Kubernetes 驱动

这是后续生产路径，普通 Job 清单不声称提供 OpenShell 隔离。

首先核实 CNI 对 ingress/egress NetworkPolicy 的实际执行能力。当前集群仅看到 kindnet；
尚未证明 NetworkPolicy 生效，不应直接假设满足 OpenShell 的隔离前提。先按集群支持方式
配置支持策略的网络组件并做拒绝/允许验证，不在此自动替换 CNI。

之后按 OpenShell 0.1.4 官方安装文档安装受支持版本的 Agent Sandbox CRD/controller，
再安装同版本 Gateway chart：

```sh
# 先完成官方文档中的 Agent Sandbox controller/CRD 安装与 NetworkPolicy 前提。
helm upgrade --install openshell oci://ghcr.io/nvidia/openshell/helm-chart \
  --version 0.1.4 --namespace openshell --create-namespace
```

需要为 sandbox namespace、工作区 PVC/StorageClass、worker 镜像仓库和 Gateway 认证
制定 values 文件；先 helm template 审阅，再安装。配置 Kubernetes Gateway 中的默认
worker 镜像、超时和网络策略，沙箱使用集群 Service DNS 访问 API 和 Langfuse。
不要把现在 WSL Docker Gateway 的配置原样当作 Kubernetes Gateway 配置。

注册该 Gateway 为单独的 CLI 别名，保留认证文件，然后设置 launcher：

```dotenv
EVAL_RUNNER_MODE=k8s
EVAL_OPENSHELL_K8S_GATEWAY=<Kubernetes Gateway 的已认证注册名>
EVAL_WORKER_IMAGE=<节点可拉取的 worker 镜像>
EVAL_OPENSHELL_POLICY=<适用于集群 Service DNS 的策略文件>
EVAL_MODEL_RESOLVER_URL=http://evaluation-api.evaluation.svc.cluster.local:8001
```

现有 `openshell_runner` 可按显式 Gateway 注册名启动、上传环境、执行、清理 Sandbox。
Kubernetes Gateway 和 CNI 尚未在本机安装/验证；需要单独做一次真实测评验收。
若 launcher 放进 Job，镜像还需 OpenShell CLI，挂载 Gateway 客户端认证 Secret，
并把 experiment_id 在创建 launcher Job 前生成且一路传给 Sandbox。当前
`OpenShellRunner.run()` 会新建 ID；launcher Job 场景需先扩展为接收可信父任务 ID，
避免 Job 与 Sandbox 出现两个 ID。这部分不是当前直接 Job 工具已实现的功能。

## 5. 已接入的 Trigger 接口

- `GET /api/v1/evaluations/config`：返回服务端允许的 target/dataset/version/environment/model 绑定和执行模式。
- `POST /api/v1/evaluations/runs`：冻结配置快照，生成 experiment_id，使用 Kubernetes API 创建 Job。
- `GET /api/v1/evaluations/runs`、`GET /api/v1/evaluations/runs/{job}`：读取真实状态和 worker 摘要。
- 所有接口要求独立 `EVAL_TRIGGER_TOKEN` Bearer 认证；运行接口要求 `X-Project-ID`。
- POST 要求 `Idempotency-Key`。同项目同键同配置返回原任务；改变配置返回 409。
- Job annotation 保存项目及配置快照，刷新前端/API 重启后可以恢复；当前不自动删除这些 Job。
- 前端通过 `/api/v1/projects/{projectId}/evaluation-execution/{path}` 服务端代理调用。
  代理校验现有登录及项目成员身份，私有 Trigger token 不下发浏览器。
- 前端 server 环境变量：`EVAL_EXECUTION_API_URL=http://127.0.0.1:18001`、`EVAL_TRIGGER_TOKEN`。
  本机已保存在忽略的 `data/trigger.env`；修改环境变量后重启前端。
- Kubernetes API ServiceAccount 只有 evaluation namespace 中 Job create/get/list 和 Pod/log get/list；
  worker 不挂载 API token，不挂载任何容器运行时 socket。
- 当前服务端绑定在 Deployment 的 `EVAL_RESOURCE_BINDINGS` JSON 环境变量中：UI `ds-guardrail` v1
  映射到 Langfuse ID `cmvff7e84000dpq4o7rg21lhd`，版本固定为 as-of 时间戳。
  UI 新发布版本不会自动替换该版本；运营者需显式增加绑定。
- 当前实际执行器为 Kubernetes Job 内 LLM 风险分类，model_id=`glm-dev`，真实调用模型。
  已安装评分器仅 `mock-score`，分数固定 1；界面明确标记 mock，不提供虚构准确率/成本。
  原 OpenShell UI 中的网络/命令限制尚未由此 Job 实现，界面也明确标注。
- 报告每 3 秒轮询，区分排队/运行/完成/失败。样本数在 worker 返回最终摘要后更新；
  明细、分数与实际响应模型通过 Langfuse 链接查看。取消运行/中间样本流/真正 Guardrail library 仍待实现。

参考：
https://docs.nvidia.com/openshell/v0.1.4/kubernetes/setup
https://docs.nvidia.com/openshell/latest/how-it-works/sandboxes/runtimes
https://docs.docker.com/desktop/use-desktop/kubernetes/
https://kubernetes.io/docs/concepts/workloads/controllers/job/


## 本机实际部署结果（2026-10-20）

- Namespace：`evaluation`
- API：`deployment/evaluation-api`，1/1 Ready
- Service：`evaluation-api:8001`（ClusterIP）
- 镜像：`docker.io/ianwoolf/evaluation-worker:k8s-20261020-r3`
- Digest：`sha256:ba1b089e0a85796664e3a218db44a133f3a3cddc9118dbc8667af1d9fe73bd9e`
- 本机临时访问：`http://localhost:18001/docs`（依赖 port-forward 进程）
- Langfuse 继续运行在本机 Compose，模型解析由 K8s 内 API Service 提供。

```sh
# 本机访问入口断开后，重新运行：
kubectl --context docker-desktop -n evaluation port-forward service/evaluation-api 18001:8001

# 再次触发测评，每次生成新 Job 和 experiment_id：
.venv/bin/python -m evaluation_backend.k8s_job \
  --dataset-id cmvff7e84000dpq4o7rg21lhd --model-id glm-dev --mock \
  --image docker.io/ianwoolf/evaluation-worker:k8s-20261020-r3 --submit
```

验收 Job：`eval-60d9773df9f7412186ec`，Complete 1/1。
Runner experiment_id：`exp-b07260d875fa400c8aab4ad2444ec19d`。
Langfuse experiment ID：`ee54aa242d2c5dd1`。
读取当前 Dataset，6 次真实 glm-dev 调用、6 条 mock score、30 条 OTEL 记录均已回读验证。
mock 分数固定为 1，不代表模型质量。节点通过 Docker Hub 拉取镜像，未挂载 Docker 或
containerd socket，未部署本地 Registry。普通 Job 不包含 OpenShell 隔离，OpenShell
Kubernetes Gateway/controller/CNI 验收仍属后续工作。前端 Trigger 已接入上述真实 Job 接口。

首次验收发现模型返回 Markdown JSON 代码块，已在 Target 中兼容完整代码块并保留
布尔值校验。失败 Job 日志已存入忽略文件 `data/k8s-first-attempt.log`，该失败 Job 已清理。
最终回读摘要：`data/k8s-verification.json`（忽略文件）。

## GLM 模型名称与实际路由

`data/models.json` 的 `glm-dev` 映射中 model=`glm-4.7`，address 为标准 API `/api/paas/v4/`。
代理发给上游的 JSON model 字段确实是 `glm-4.7`。Langfuse generation.model 保存请求模型，
metadata.response_model 保存供应商返回的实际模型；目前回包为 `glm-5.3-flash`。
智谱官方 Coding Plan 概览明确说明 GLM-4.7 会自动切换至 GLM-5.3-Flash，符合目前现象。
这不能单独证明此 API Key 的套餐类型；若必须固定真实 4.7，需要供应商提供仍保留 4.7 的部署/账户路由，
仅修改大小写无法覆盖供应商自动映射。没有擅自改成新版模型或修改 endpoint。

官方文档：https://docs.bigmodel.cn/cn/coding-plan/overview
历史 4.7 模型文档：https://docs.bigmodel.cn/cn/guide/models/text/glm-4.7
