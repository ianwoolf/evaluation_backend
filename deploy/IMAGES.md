# Docker Hub 镜像清单

本次上传的是当前本机运行镜像，全部为 **linux/arm64**，不是多架构 manifest。
x86/amd64 机器请用对应源码重新构建 amd64 镜像（另用 tag），不要直接假定这些 tag 支持 amd64。

| 组件 | 镜像 |
|---|---|
| UI | ianwoolf/evaluation-ui:20261025-none-v1 |
| Control API | ianwoolf/evaluation-control-api:20261025-config-v1 |
| evaluation-api / runner | ianwoolf/evaluation-worker:20261025-env-v1 |
| Elasticsearch | ianwoolf/evaluation-elasticsearch:8.18.1 |
| PostgreSQL | ianwoolf/evaluation-postgres:17 |

清单使用 IfNotPresent，联网节点可直接拉取；Docker Hub 仓库如设为私有，需配置 imagePullSecrets。
镜像不包含运行中的 Secret、数据库卷或历史评测数据。当前本机部署未因发布而重新滚动。

