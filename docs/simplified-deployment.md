# 评测服务简化设计与实现

已实现并部署精简 SPA、免登录评测 API、固定项目和请求体限制。
本机 Control API Deployment/Service 已移除，评测栈不再依赖 PostgreSQL。
前端构建、运行配置和部署步骤见主工程 `web/deploy/STANDALONE.md`。

## 改造前组件及依赖

改造前三个常驻应用 Deployment 并不是三个业务后端：

| 组件 | 职责 | 依赖 |
| --- | --- | --- |
| evaluation-ui | Nginx 提供编译后的前端静态文件，转发 API | Control API |
| evaluation-control-api | Node/Nitro 服务：登录、用户、项目、角色权限，代理评测请求 | PostgreSQL、Evaluation API |
| evaluation-api | FastAPI：资源管理、上传、Job 调度、报告、模型解析、存储服务 | Elasticsearch、Kubernetes API、上传 PVC |

runner 不是常驻 Deployment，每次测评创建一个 Kubernetes Job。Job 内包括 supervisor、
worker、本地 collector，以及需要模型时启动的 LLM proxy。这些进程负责执行、评分和
发送结果，移除 Control API 不影响它们的运行职责。

PostgreSQL 属于 Control 的账户/项目/权限依赖，当前评测数据的持久化不依赖它。
评测资源、运行、样本、分数和 trace 使用三个 Elasticsearch 索引；临时上传使用 PVC。

## 推荐部署

保留精简的评测 SPA，移除 Control API 和 UI 对 PostgreSQL 的依赖：

```text
浏览器
  └─ Nginx：静态 UI + 同源 /api 转发
       └─ Evaluation API /api/v1/evaluations/*
            ├─ Elasticsearch
            ├─ 临时上传 PVC
            └─ Kubernetes Job
                 └─ worker / collector / 可选模型代理
```

Nginx 只做 HTTP 转发，不执行登录或权限逻辑。前端业务请求直接进入评测 API，
不经过 Control API。相比浏览器跨端口访问 FastAPI，同源路径无需另外配置 CORS。
若选择绝对后端地址，FastAPI 需要配置明确的 UI origin，并允许 JSON、上传和幂等请求头。

## 前端改动

1. 使用独立的评测页面入口，复用现有 evaluation-workbench 组件和样式。
2. 移除 AuthProvider、AuthGuard、账户/角色切换、SSO 和登录跳转。
3. 移除远程 ProjectProvider，使用配置中的固定项目 ID，默认保留 `individual`，
   以便继续读取现有 ES 数据。
4. 页面菜单保留 Evaluation、Datasets、Environments、Scorers、Reports 等评测功能；
   不再按用户角色隐藏菜单，也不加载其他平台模块。
5. executionApi 从 `/api/v1/projects/{project}/evaluation-execution/*` 改为
   `/api/v1/evaluations/*`，不读取浏览器登录 token。
6. API base URL、默认项目等可用运行时配置文件加载，避免改地址就重新编译 UI。

这些改动必须覆盖路由根组件和页面依赖；仅修改 Nginx 上游、删除 Control Deployment
或者伪造登录接口，会遗留 auth/config、auth/me、projects 等请求。

## 后端改动

增加明确的单用户部署模式：浏览器访问评测业务 API 无需登录或角色验证。
项目 ID 由部署配置确定，默认 `individual`，作为数据命名空间保留。
当前 authorize 要求 Bearer token，不能只把浏览器调用地址改过去。

模型凭据解析与 runner 的内部调用仍需明确处理：当前全局 trigger secret 同时用于
签发 run-bound worker token，直接删除它会让 Job 创建或数据读取失败。
简化时将内部签名配置与浏览器登录拆开；不在静态前端放模型、ES 或全局服务凭据。
模型解析接口只供内部代理调用，不通过 UI 的公开 Nginx 路由暴露。

Control 代理目前还承担请求体大小限制、路径/方法过滤、上传头传递和超时。
移除后，在 FastAPI/Nginx 保留对应行为：8 MB 上传/数据集限制、上传文件名、分页、
Idempotency-Key、X-Request-ID、错误响应及日志关联。
资源版本、参数验证、Job 幂等和样本留存策略属于业务规则，继续由评测后端负责。

## 可移除及保留的部署资源

| 资源 | 简化后的处理 |
| --- | --- |
| evaluation-control-api Deployment/Service | 移除 |
| evaluation-ui-config 中的 PostgreSQL / 用户 / SSO 配置 | 精简 UI 不再加载 |
| Control 的 PostgreSQL | 本评测栈不再依赖；其他应用仍在使用时保留数据库实例和数据 |
| evaluation-ui Deployment/Service | 保留，使用精简 SPA 和新的 Nginx API 上游 |
| evaluation-api、ES、上传 PVC、runner Job | 保留 |
| 存储、模型和运行配置 | 保留，内部调用凭据与浏览器权限解耦 |

## 如果完全不需要 UI

可以只部署 Evaluation API + Elasticsearch，继续通过 Kubernetes Job 执行。
用户通过 API、脚本或 `/docs` 完成资源管理、上传、触发和读取报告。
这会失去当前表格、配置弹窗和报告详情页面，不影响后端评测能力。

另一种选择是由 FastAPI 同时提供编译后的 UI 静态文件，将常驻应用合并为一个
API Deployment；代价是前后端镜像和发布周期绑定。现有纯 Nginx UI 已较轻量，
优先保留独立静态 UI，减少改造范围。

## 验收

- 浏览器直接进入评测页，无登录跳转，不请求 auth、projects、roles 等 Control 接口。
- 网络请求直接使用评测 API，无需浏览器登录 token。
- 关闭 Control API 后，页面加载、资源配置、上传、触发和报告仍可用。
- 本评测栈不再连接 PostgreSQL；现有 ES 数据仍使用原项目命名空间。
- 实际 runner Job 完成测评，样本/评分/trace 和报告读写正常。

验收已覆盖浏览器资源页、报告列表与详情，以及无 token 上传/触发 runner 并完成测评。
旧 PostgreSQL 数据和完整平台源码保留，不再作为精简部署依赖。
