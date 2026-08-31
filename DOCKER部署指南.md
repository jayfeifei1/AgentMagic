# EchoMind Docker 部署指南

本文档以仓库根目录 `D:\PycharmProjects\EchoMind8.30` 为执行位置，部署以下服务：

| 服务 | 容器名 | 宿主机端口 | 用途 |
| --- | --- | --- | --- |
| 前端 | `echomind-frontend` | 5174 | Vue Web 界面 |
| 后端 | `echomind-app` | 8000 | FastAPI / Agent 服务 |
| Nginx | `echomind-nginx` | 80 | 后端反向代理 |
| Redis | `echomind-redis` | 6379 | 工作记忆、会话缓存 |
| MySQL | `echomind-mysql` | 3306 | Trace 与后续业务数据 |
| ChromaDB | `echomind-chromadb` | 8001 | 向量检索数据 |
| Prometheus | `echomind-prometheus` | 9090 | 指标监控 |

## 前置条件

- 已安装并启动 Docker Desktop。
- 已安装 Node.js（仅首次构建或修改前端时需要）。
- 后端配置文件为 `EchoMind8.30/.env`；请先按实际模型供应商配置 API Key。配置变更后重启后端容器即可生效。

## 启动前端

首次部署或修改前端代码后，先构建前端静态资源：

```powershell
cd D:\PycharmProjects\EchoMind8.30\EchoMindFrontend8.30
npm install
npm run build
```

然后在前端目录启动前端容器：

```powershell
cd D:\PycharmProjects\EchoMind8.30\EchoMindFrontend8.30
docker compose up -d --build
```

> 前端 Dockerfile 会将 `dist` 拷入 Nginx 镜像。仅重新启动 Docker 容器不会编译 Vue 源码，因此改动前端代码后需再次执行 `npm run build`。

## 启动后端及依赖服务

后端、Redis、MySQL、ChromaDB、Prometheus 与 Nginx 由同一份 Compose 配置启动：

```powershell
cd D:\PycharmProjects\EchoMind8.30\EchoMind8.30
docker compose up -d --build
```

启动完成后可访问：

- 前端：http://localhost:5174
- 后端健康检查：http://localhost:8000/health
- Nginx 健康检查：http://localhost/health
- Prometheus：http://localhost:9090
- ChromaDB：http://localhost:8001

查看全部容器状态：

```powershell
docker ps --filter "name=echomind"
```

容器状态显示为 `healthy` 即表示对应健康检查通过；前端没有额外健康检查，浏览器能打开 `http://localhost:5174` 即可。

## MySQL 可视化连接

可使用 DBeaver、Navicat 或 DataGrip。连接参数如下：

| 配置项 | 值 |
| --- | --- |
| 主机 | `127.0.0.1` |
| 端口 | `3306` |
| 数据库 | `echomind` |
| 推荐用户名 | `echomind_app` |
| 推荐密码 | `echomind` |
| Root 用户名 | `root` |
| Root 密码 | `echomind` |
| 字符集 | `utf8mb4` |
| 时区 | `UTC+8`（中国本地时间） |

当前 MySQL 容器已创建数据库 `echomind`。后端启动时会自动创建以下 Agent 可观测性表：

- `request_traces`：一次请求的路由、状态与总耗时；
- `llm_call_traces`：每轮模型调用的 Agent、模型、状态与耗时；
- `tool_call_traces`：工具入参、结果摘要、状态与耗时。

可在 DBeaver 中执行下面的查询，按请求时间查看完整链路：

```sql
SELECT * FROM request_traces ORDER BY created_at DESC;
SELECT * FROM llm_call_traces ORDER BY created_at DESC;
SELECT * FROM tool_call_traces ORDER BY created_at DESC;
```

也可以通过 `GET /trace/tool/{request_id}` 查询指定请求的完整 Trace；接口优先读取 MySQL，服务重启后历史记录仍可查询。列表接口 `GET /trace/tools` 支持 `agent_type`、`tool_name`、`start_time`、`end_time` 参数，例如：

```text
http://localhost:8000/trace/tools?agent_type=technical&tool_name=lookup_error_code
```

命令行快速验证：

```powershell
docker compose -f .\EchoMind8.30\docker-compose.yml exec mysql mysql -uechomind_app -pechomind echomind
```

## Redis 可视化连接

可使用 RedisInsight、Another Redis Desktop Manager 或 DataGrip 的 Redis 数据源。

| 配置项 | 值 |
| --- | --- |
| 主机 | `127.0.0.1` |
| 端口 | `6379` |
| 用户名 | `default`（有的客户端可留空） |
| 密码 | `echomind123` |
| 数据库编号 | `0` |
| TLS/SSL | 关闭 |

Redis 使用 AOF 持久化，工作记忆和会话缓存会保存在 Docker 卷中。命令行快速验证：

```powershell
docker compose -f .\EchoMind8.30\docker-compose.yml exec redis redis-cli -a echomind123 ping
```

返回 `PONG` 表示连接正常。查看数据库 0 的键：

```powershell
docker compose -f .\EchoMind8.30\docker-compose.yml exec redis redis-cli -a echomind123 --scan
```

## 常用运维命令

查看后端日志：

```powershell
docker compose -f .\EchoMind8.30\docker-compose.yml logs -f echomind
```

查看前端日志：

```powershell
docker compose -f .\EchoMindFrontend8.30\docker-compose.yml logs -f echomind-frontend
```

重启后端（例如修改 `.env` 后）：

```powershell
docker compose -f .\EchoMind8.30\docker-compose.yml up -d --force-recreate echomind
```

停止服务但保留数据库、缓存和向量数据：

```powershell
docker compose -f .\EchoMindFrontend8.30\docker-compose.yml down
docker compose -f .\EchoMind8.30\docker-compose.yml down
```

不要在日常停止时使用 `down -v`，它会删除 Docker 数据卷，其中包含 MySQL、Redis、ChromaDB 和 Prometheus 的持久化数据。

## 数据位置与重置提示

- MySQL：Docker 卷 `echomind830_mysql-data`
- Redis：Docker 卷 `echomind830_redis-data`
- ChromaDB：Docker 卷 `echomind830_chromadb-data`，以及后端目录 `EchoMind8.30/data/chroma`
- Prometheus：Docker 卷 `echomind830_prometheus-data`

若需要重置某项数据，请先确认目标卷名称与数据是否可丢弃，再停止并删除对应服务和卷；不要直接执行全局清理命令。
