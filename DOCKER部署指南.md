# EchoMind Docker 部署与演示指南

本文档用于本机面试演示。仓库根目录为 `D:\PycharmProjects\EchoMind8.30`，服务分为后端栈和前端栈，必须先启动后端栈。

## 1. 架构与访问入口

```text
浏览器 http://localhost:5174
  → 前端 Nginx（echomind-frontend）
  → /api/python/*
  → 后端 Nginx（echomind-nginx，宿主机 80）
  → FastAPI（echomind-app，容器内 8000）
  → Redis / MySQL / ChromaDB / 本地 BGE Embedding
```

| 服务 | 容器名 | 宿主机端口 | 用途 |
| --- | --- | --- | --- |
| 前端 | `echomind-frontend` | 5174 | 面试演示入口 |
| 后端 API | `echomind-app` | 8100 | 仅本机调试、Swagger |
| 后端 Nginx | `echomind-nginx` | 80 | 前端实际 API 网关 |
| MySQL | `echomind-mysql` | 3306 | Trace、业务演示数据 |
| Redis | `echomind-redis` | 6379 | 工作记忆、会话缓存 |
| ChromaDB | `echomind-chromadb` | 8001 | 向量检索数据 |
| Embedding | `echomind-embedding` | 8101 | 本地 BGE 中文意图向量服务 |
| Prometheus | `echomind-prometheus` | 9090 | 指标监控 |

> 不使用宿主机 8000 端口。当前 Windows 环境保留了该端口段，后端映射到 `8100`；容器内部端口仍为 8000。

## 2. 演示前检查

1. 启动 Docker Desktop，确认状态为 Running。
2. 确认后端配置文件存在：`EchoMind8.30/.env` 与 `EchoMind8.30/mysql.env`。
3. `.env` 中应已有可用的模型 API Key；不要在演示时展示该文件或终端中的密钥。
4. 确认本机已安装 Node.js LTS：执行 `node -v` 与 `npm -v`。第一次 `npm ci` 会下载前端依赖，请不要在面试前第一次执行构建。

## 3. 启动后端及依赖服务

```powershell
cd D:\PycharmProjects\EchoMind8.30\EchoMind8.30
docker compose up -d --build
docker compose ps
```

首次构建会预下载 ChromaDB 检索模型和独立 Embedding 服务中的中文模型 `BAAI/bge-small-zh-v1.5`（约 90MB）。模型被打入 `echomind-embedding` 镜像，运行时不需要 Embedding API Key 或访问 Hugging Face；不要在面试现场首次执行构建。

等待 `echomind-app`、`echomind-mysql`、`echomind-redis`、`echomind-chromadb` 与 `echomind-nginx` 显示为 `healthy` 后，执行：

```powershell
(Invoke-WebRequest -UseBasicParsing http://localhost/health).StatusCode
(Invoke-WebRequest -UseBasicParsing http://localhost:8100/health).StatusCode
(Invoke-WebRequest -UseBasicParsing http://localhost:8101/health).StatusCode
```

三个命令都应返回 `200`。

## 4. 启动前端

```powershell
cd D:\PycharmProjects\EchoMind8.30\EchoMindFrontend8.30
docker compose up -d --build
docker compose ps
```

前端镜像部署的是 `dist` 静态文件。首次部署或修改前端源码后，先在前端目录执行：

```powershell
npm ci
npm run build
```

确认构建成功后，再执行上面的 `docker compose up -d --build`。面试前请至少完整执行一次本节流程，不要临场修改前端源码。

验证前端及其 API 代理：

```powershell
(Invoke-WebRequest -UseBasicParsing http://localhost:5174/).StatusCode
(Invoke-WebRequest -UseBasicParsing http://localhost:5174/api/python/health).StatusCode
```

两个命令都应返回 `200`。然后打开：http://localhost:5174

## 5. 面试演示流程

1. 打开 `http://localhost:5174`，确认右侧“连接配置”显示 `ok`。
2. 在右侧用户 ID 中使用 `u1001`；该字段可编辑，会作为后端订单/退款查询范围。
3. 点击“新建对话”，再发送测试问题。

建议先演示一个短链路，再演示协同链路：

| 场景 | 建议输入 | 预期重点 |
| --- | --- | --- |
| 退款进度 | `查询订单 ORD-DEMO-1002 的退款进度` | Billing Agent、MySQL 退款查询、Trace |
| 技术排障 | `登录后台提示 401，请给出排查步骤` | Technical Agent、错误码手册、故障状态 |
| Multi-Agent | `登录时提示错误，应该怎么排查？我想申请退款，订单号是 ORD-DEMO-1001` | Technical + Billing 并行、Composer 汇总、Trace |

复合请求包含多个模型调用和工具调用，通常需要几十秒。网关超时已配置为 180 秒；等待期间不要重复点击发送或刷新页面，否则可能创建重复的人工交接工单。

## 6. 常用地址

| 地址 | 说明 |
| --- | --- |
| http://localhost:5174 | 前端页面 |
| http://localhost/docs | 经 Nginx 访问的 Swagger |
| http://localhost:8100/docs | 直连 FastAPI Swagger（本机调试） |
| http://localhost:8101/health | 本地 BGE Embedding 服务健康检查 |
| http://localhost/health | 后端网关健康检查 |
| http://localhost:5174/api/python/health | 前端 → 后端完整代理链路健康检查 |
| http://localhost:9090 | Prometheus |

## 7. MySQL 与 Redis 可视化连接

### MySQL（DBeaver / DataGrip）

| 配置项 | 值 |
| --- | --- |
| 主机 | `127.0.0.1` |
| 端口 | `3306` |
| 数据库 | `echomind` |
| 用户名 | `echomind_app` |
| 密码 | `echomind` |
| 字符集 | `utf8mb4` |
| 时区 | `UTC+8` |

常看表：`request_traces`、`agent_execution_traces`、`llm_call_traces`、`tool_call_traces`、`orders`、`payment_transactions`、`refunds`、`service_incidents`、`handoff_tickets`。

```sql
SELECT * FROM request_traces ORDER BY created_at DESC;
SELECT * FROM agent_execution_traces ORDER BY created_at DESC;
SELECT * FROM llm_call_traces ORDER BY created_at DESC;
SELECT * FROM tool_call_traces ORDER BY created_at DESC;
```

演示数据：`u1001` 对应重复扣款订单 `ORD-DEMO-1001`、退款处理中订单 `ORD-DEMO-1002`、已完成退款订单 `ORD-DEMO-1005`；技术场景可使用 `401`、`500`、`502`。

### Redis（RedisInsight）

| 配置项 | 值 |
| --- | --- |
| 主机 | `127.0.0.1` |
| 端口 | `6379` |
| 用户名 | `default`（可留空） |
| 密码 | `echomind123` |
| 数据库编号 | `0` |
| TLS/SSL | 关闭 |

验证 Redis：

```powershell
cd D:\PycharmProjects\EchoMind8.30\EchoMind8.30
docker compose exec redis redis-cli -a echomind123 ping
```

返回 `PONG` 表示正常。

## 8. 故障排查与恢复

### 页面显示 Python 不可用

按顺序执行：

```powershell
(Invoke-WebRequest -UseBasicParsing http://localhost/health).StatusCode
(Invoke-WebRequest -UseBasicParsing http://localhost:5174/api/python/health).StatusCode
```

若任一不是 `200`，先查看日志：

```powershell
cd D:\PycharmProjects\EchoMind8.30\EchoMind8.30
docker compose logs --tail 100 echomind nginx

cd D:\PycharmProjects\EchoMind8.30\EchoMindFrontend8.30
docker compose logs --tail 100 echomind-frontend
```

然后按第 3、4 节的顺序重新执行 `docker compose up -d --build`。

### 页面显示“请求处理超时”

不要立即重复发送。复合请求可能仍在后端执行并写入 MySQL；先等待一会儿，然后通过最近的 `request_traces` 查看是否已有完成记录。当前前后端 Nginx 的 `/chat` 超时均为 180 秒；若仍超过 180 秒，应查看 `llm_call_traces` 中具体哪个 Agent 或 Composer 调用过慢。

### 日志提示 BGE 模型不可用

后端会自动回退到字符 n-gram 向量，不会中断聊天主链路，但意图相似度效果会下降。重新构建独立 Embedding 镜像以恢复本地模型缓存：

```powershell
cd D:\PycharmProjects\EchoMind8.30\EchoMind8.30
docker compose up -d --build embedding
docker compose logs --tail 100 embedding
```

访问 `http://localhost:8101/health` 返回 `{"status":"ok","model":"BAAI/bge-small-zh-v1.5","dimensions":512}` 表示加载成功。

### 电脑重启后恢复

Docker Desktop 启动完成后，按第 3 节启动后端，再按第 4 节启动前端，并执行两条健康检查。不要假设容器“Up”就代表 API 代理可用。

## 9. 停止、日志与清理

停止服务但保留 MySQL、Redis、ChromaDB 数据卷：

```powershell
cd D:\PycharmProjects\EchoMind8.30\EchoMindFrontend8.30
docker compose down

cd D:\PycharmProjects\EchoMind8.30\EchoMind8.30
docker compose down
```

不要在日常演示前使用 `docker compose down -v`，它会删除数据库、缓存和向量数据。

仅清理 `<none>` 悬挂镜像：

```powershell
docker image prune -f
```

不要使用 `docker system prune -a --volumes`，它会删除未使用镜像和数据卷，可能破坏演示数据。
