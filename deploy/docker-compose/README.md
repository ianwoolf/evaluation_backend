# Docker Compose：评测 API、ES 和动态 runner

本目录只用于 **Docker Engine + Docker Compose v2**，不需要 Kubernetes、kubectl、
PostgreSQL、Control API 或 OpenShell Gateway。K8s 部署单独保留在 `../kubernetes`。
API 将评测启动为独立 Docker 容器，内部仍包含 worker、collector 和可选模型代理。

## 配置与凭据

| 内容 | Docker Compose | Kubernetes |
| --- | --- | --- |
| API 参数、ES 地址/索引/证书开关 | config/backend.env（configs） | ConfigMap |
| 模型地址/模型名/token_env 引用 | config/models.json（configs） | evaluation-model-registry ConfigMap |
| target/dataset/environment 绑定 | config/bindings.json（configs） | evaluation-resource-bindings ConfigMap |
| UI 参数、Nginx 上游 | UI 仓库 deploy/docker-compose/config（configs） | UI ConfigMap / 镜像配置 |
| 内部签名、模型解析 token、模型 token、ES 密码/API key、OTel 认证头 | .secrets 文件（secrets） | evaluation-credentials Secret |

`models.json` 不放 token，例如：

```json
{"glm-dev":{"address":"https://model.example/v1","model":"glm","token_env":"EVAL_MODEL_TOKEN_ABCDEF123456"}}
```

对应 `.secrets/model_tokens` 只放凭据映射：

```json
{"EVAL_MODEL_TOKEN_ABCDEF123456":"YOUR_PROVIDER_TOKEN"}
```

默认模型注册表为空，示例使用 `modelId=none` + mock 评分，不需要真实模型 token。
生产配置可从旧私有模型注册表通过 `init_secrets.py` 的 EVAL_MODEL_REGISTRY 环境变量导入，
元数据写入 configs，token 拆入 Secret 文件。已有内部 token 和 ES 密码不会被初始化脚本轮换。

## 第一次启动（仅需 Docker，无需宿主机 Python/Node）

在本目录执行 Bash 命令；Windows 使用 WSL。先复制非敏感端口/镜像配置：

```bash
cp .env.example .env
# Linux/WSL 必须设置为实际 socket 组，API 以非 root 用户访问 Docker。
export DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)
printf '\nDOCKER_GID=%s\n' "$DOCKER_GID" >> .env
docker compose build evaluation-api

# 在应用镜像中执行初始化，不要求宿主机安装 Python。
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$PWD:/deployment" --entrypoint python \
  evaluation-worker:deploy-v2 /deployment/init_secrets.py

docker compose up -d --no-build --wait
```

若使用自己指定的密码，可在初始化前交互设置 `EVAL_ES_PASSWORD`，给上面的 docker run
增加 `-e EVAL_ES_PASSWORD`（不把值放在命令参数中）。默认生成随机密码，保存在
`.secrets/es_password`。ES 默认启用密码认证，单节点 HTTP；证书验证开关默认 false。
更改已有 Secret 文件不等于更改 ES 已持久化的用户密码，轮换需走 ES 密码接口/工具。

API 默认 http://127.0.0.1:8001，ES 默认 http://127.0.0.1:9200。
前端工作台由 UI 仓库的 `web/deploy/docker-compose` 独立启动，加入同一个网络。

```bash
docker compose exec -T evaluation-api python - < seed_demo.py
docker compose ps
```

seed_demo.py 只创建缺失的合成数据集、none 执行环境和 mock target 配置，不覆盖已有资源。

## runner 与持久化

- API 是唯一挂载 `/var/run/docker.sock` 的服务，拥有启动容器的宿主机 Docker 权限。
- runner 不挂 Docker socket，也不接收全局签名 token 或模型供应商 token。
  只挂所需的解析/存储凭据文件，并使用 run-bound token 读取本次输入。
- API 将解析后 Secret 的文件挂载源传给 Docker Engine；只支持本机 Engine socket，
  不支持 API 机器和远端 Docker daemon 使用不同文件系统的部署。
- runner CPU、内存、只读根文件系统、临时目录和 deadline 与原评测配置对应。
  API 的定时清理循环和状态查询负责执行 Docker deadline；API 停机时不能执行该检查。
- ES 数据、上传数据分别保存到 es-data / uploads named volumes；完成的 runner 容器保留供历史/日志查询。
- configs 挂载普通文件，secrets 通过 `_FILE` 环境变量加载。不要把 `.secrets` 提交 Git、
  打进镜像或写进 configs。Compose 本地 secrets 是受文件权限保护的挂载，不是加密保管库。
- Windows NTFS 文件挂载权限可能是 0777；ES 入口先将密码复制到容器内 0600 临时文件，
  再交给官方入口，避免权限检查失败。API/runner 使用只读原始 Secret 挂载。

## 离线机器和架构

联网机器分别构建 API/runner 和 UI，准备与目标架构一致的 ES 镜像；导出：

```bash
docker save -o evaluation-images.tar evaluation-worker:deploy-v2 evaluation-ui:deploy-v2 docker.io/library/elasticsearch:8.19.23
```

离线机器 `docker load -i evaluation-images.tar`，然后按上面的初始化与
`docker compose up -d --no-build --wait` 启动。导出镜像不包含 ES 数据、configs 或 Secret；
这些配置目录和所需 Secret 文件需另行部署。AMD64/ARM64 镜像必须匹配目标主机。

导入旧私有模型注册表时，可在配置初始化命令中显式挂载源文件，例如：

```bash
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/deployment" \
  -v /private/models.json:/input/models.json:ro -e EVAL_MODEL_REGISTRY=/input/models.json \
  --entrypoint python evaluation-worker:deploy-v2 /deployment/init_secrets.py
```

只导入新的模型 token 键，不覆盖已有 token；轮换已有模型 token 请更新对应 Secret 文件。

## 日常操作

```bash
docker compose logs -f evaluation-api
docker compose logs -f evaluation-es
docker ps -a --filter label=evaluation.backend.owner=evaluation-backend
docker compose down
```

`down` 不删除 named volumes；不要用 `down -v` 清理需要保留的数据。
先停 UI 再停后端。重启 ES 或 API 影响不了 K8s 中独立部署的那套服务，两套数据也不共享。
