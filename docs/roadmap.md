# 后端实施路线

本文件是待办，不表示相关服务已经实现。

- [x] 独立仓库、包结构、健康接口、结果发送抽象与本地 sink。
- [ ] 固定依赖和工具版本、建立 CI；认证、项目授权与统一错误契约。
- [ ] PostgreSQL migration、事务边界、Dataset / Target / Environment / Scorer 不可变版本。
- [ ] Evaluation 提交/幂等/History；sample/score/span 持久化；Report 与 SSE。
- [ ] Job lease/attempt/fencing；固定资源 bundle，target 与 scorer library ABI。
- [ ] OpenShell driver：启动、加载资源、执行、取消、超时、回传与清理。
- [ ] Stork model override 与 Guardrail adapter；reference 和凭证隔离。
- [ ] Transactional outbox、delivery 状态、脱敏、重试与重放。
- [ ] Langfuse sink；契约测试确保后续 LangSmith sink 无需改动 runner。
- [ ] Web API 接入，移除模拟时钟，验收运行中报告和完整端到端流程。

原则：业务结果先本地持久化，平台发送异步；发送失败不重新执行评测。API 不在请求线程中运行任务。执行器不依赖平台 SDK。平台凭证不进入 target/scorer sandbox。

- [x] Docker / Kubernetes workload runner：配置选择、幂等提交、状态、日志、取消、清理与 deadline；Docker 真实验证通过，K8s 真实集群验证待本机 API Server 恢复。
