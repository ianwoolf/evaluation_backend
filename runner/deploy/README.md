# Evaluation runner：Kubernetes 部署和运行契约

这里单独说明 runner。源代码仍位于 ../../src/evaluation_backend/（相对本文目录），
不是独立服务或独立仓库。镜像、collector、proxy 与后端共用：
仓库根 Dockerfile.worker → ianwoolf/evaluation-worker:20261025-env-v1。

**不要创建常驻 runner Deployment。** evaluation-api 每次 Trigger 创建一个 batch/v1 Job；
Job 定义由 src/evaluation_backend/k8s_job.py 与 runs.py 生成。
完整部署先执行 [backend deploy/README.md](../../deploy/README.md)。

## 部署前准备

1. 在 evaluation 命名空间部署 API、ES、runtime ConfigMap、Secret、worker ServiceAccount 和 RBAC。
   权威清单为 ../../deploy/kubernetes/services.yaml（相对于本文目录）。
2. 构建镜像并导入所有可能调度到的节点。API Deployment 的 image 和 EVAL_WORKER_IMAGE 使用同一 tag。
3. 私有 registry：当前动态 Job 无 imagePullSecrets 字段，可给 evaluation-worker ServiceAccount
   配置 imagePullSecrets；API Deployment 的拉取凭据单独配置。Never 仅用于已有节点缓存的本地部署。
4. 允许 worker 访问 evaluation-api:8001、ES:9200（collector）、DNS；
   glm-dev 还需要经 proxy 访问模型供应商 HTTPS。none 不需要模型外网。
5. scorer 插件必须预装在 worker 镜像中；dataset metadata 只引用插件名，不下载或执行上传代码。

## Job 中的进程

- proxy_entrypoint：启动/监督 collector、可选 LLM proxy、worker，并随 worker 结束停止子进程。
- collector：127.0.0.1:18091，写入 ES；接受内部反馈与 OTLP/HTTP JSON traces。
- LLM proxy：127.0.0.1:18090，modelId 不是 none 时启动；按 ID 向 API 解析模型配置。
- worker：取发布 dataset 或运行级上传内容 → 调用模型（或直通 input）→ scorer → 写入结果。

没有独立 collector Service，不需要另外部署 upstream OTel Collector。Pod stdout 不会自动成为
OTel Logs；当前采集的是 traces。模型 none 时不启动 proxy、不创建模型 span，modelLatencyMs=0。

## 参数来源

| 参数 | 来源 / 用途 |
|---|---|
| EVAL_MODEL_ID | 所选、固定版本 Environment.modelId：glm-dev / none |
| EVAL_GUARDRAIL_ID | 同一 Environment.guardrailId，传给 runner，不表示自动创建/调用外部 Guardrail 服务 |
| EVAL_ENVIRONMENT_SNAPSHOT | 完整配置 + Environment ID/版本；启动日志原样记录配置 |
| EVAL_TARGET_SNAPSHOT | 服务端可信 target ID/name/provider/version |
| DATASET_ID、EVAL_DATASET_VERSION | target Config 固定的 dataset/schema 版本 |
| EVAL_RUN_INPUT_ID | API 管理的运行输入 ID；runner 不挂上传 PVC |
| SCORER_IDS / EVAL_SCORER_MODE | Config 的 scorer 列表 / installed 或 mock |
| EVAL_PROJECT_ID / EXPERIMENT_ID | 项目隔离和一次运行身份 |
| EVAL_STORAGE_TOKEN | API 签发的运行级能力 token，不手工复用 |
| EVAL_CONTENT_POLICY | 运行创建时固定的 input/response/reference retain/omit |
| EVAL_MODEL_RESOLVER_TOKEN | evaluation-runner Secret，供 proxy 解析模型 |
| EVAL_ES_* | collector 的存储配置，不传给 worker 子进程 |

Environment CPU(m) / Memory(Mi) 写入 Pod requests 与 limits：1000m=1 CPU core、1024Mi=1Gi。
配置快照也包含 timeoutSeconds、runner、network、路径、commands 和 intercept 开关；
**日志表示收到的配置，不表示每个配置已经被 K8s 强制执行**。当前 API 路径：
并发由 runtime 的 EVAL_CONCURRENCY（默认 1）决定，Job activeDeadlineSeconds=600，
backoffLimit=0。没有根据 network/路径字段自动生成 NetworkPolicy 或 sandbox policy。
API 创建的 Job 不设完成 TTL；CLI build_job 的裸模板为 24h。不要用裸模板替代 API Trigger，
它缺少运行上下文、临时输入及签名 token。

## 验证与日志

在 UI 保存 Environment，再在 target Config 选择该版本，Trigger。
```bash
kubectl -n evaluation get jobs,pods
kubectl -n evaluation logs job/替换为本次evaluation-id
kubectl -n evaluation describe pod 替换为本次pod名称
```

期望看到：
```json
{"event":"evaluation.environment","evaluation_id":"exp-...","environment":{"id":"env-isolated","version":1,"modelId":"none","guardrailId":"guardrail-privacy","cpuMillicores":500,"memoryMiB":1024}}
```
实际包含所有已保存 Environment 字段。另有 evaluation.target 事件和最终 COMPLETED/FAILED 结果。
不会打印完整进程环境变量。打分器仍需遵守 input/output/reference 合同；
none 把 input 作为 output 传入，不会合成 has_issue 等预测值。

ErrImagePull：检查节点镜像/tag/架构/registry；Pending：资源不足；启动失败：ConfigMap/Secret/ES/API
不可达；失败运行检查 Pod 日志中的 error_type。readOnlyRootFilesystem=true，只可写 /tmp、
/home/worker 的 emptyDir；工作进程无 Kubernetes ServiceAccount token。

升级只影响新 Job：新镜像 tag → 导入节点 → 同步 API image / EVAL_WORKER_IMAGE → rollout →
新触发测试。不要删除已有 Job 来“刷新”镜像，否则可能丢失 UI 运行历史。
