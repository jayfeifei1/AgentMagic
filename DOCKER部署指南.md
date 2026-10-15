# EchoMind Docker 部署指南

适用于 Windows 重启后的本地完整启动。项目由宿主机 MinerU Relay、后端 Docker 服务和前端 Docker 服务组成，必须按以下顺序启动。

## 1. 启动前准备

1. 打开 Docker Desktop，确认状态为 Running。
2. 确认后端配置文件 `backend/.env` 与 `backend/mysql.env` 已存在；不要在终端或截图中展示密钥。

## 2. 启动 MinerU Relay

Relay 运行在 Windows 宿主机，负责下载 MinerU 的解析 Markdown，避免 Docker 访问 MinerU CDN 时出现 TLS EOF。

```powershell
cd D:\PycharmProjects\EchoMind8.30\backend
powershell -ExecutionPolicy Bypass -File .\tools\start_mineru_relay.ps1
```

预期输出包含：

```text
MinerU Relay is already running: http://127.0.0.1:18789/health
```

或：

```text
MinerU Relay started (PID: ...): http://127.0.0.1:18789/health
```

## 3. 启动后端服务

```powershell
cd D:\PycharmProjects\EchoMind8.30\backend
docker compose up -d --build
docker compose ps
```

等待 `echomind-app`、`echomind-mysql`、`echomind-redis`、`echomind-chromadb`、`echomind-embedding` 和 `echomind-nginx` 显示为 `healthy` 或 `running`。

验证后端和 Relay：

```powershell
(Invoke-WebRequest -UseBasicParsing http://localhost/health).StatusCode
(Invoke-WebRequest -UseBasicParsing http://localhost:8100/health).StatusCode
(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:18789/health).StatusCode
```

三个命令都应返回 `200`。

## 4. 启动前端服务

```powershell
cd D:\PycharmProjects\EchoMind8.30\frontend
docker compose up -d --build
docker compose ps
```

验证前端与完整 API 代理链路：

```powershell
(Invoke-WebRequest -UseBasicParsing http://localhost:5174/).StatusCode
(Invoke-WebRequest -UseBasicParsing http://localhost:5174/api/python/health).StatusCode
```

两个命令都应返回 `200`。页面入口：<http://localhost:5174>。

> 修改前端源码后，先在前端目录执行 `npm ci` 和 `npm run build`，再执行本节 Docker 命令。

## 5. 日常重启

仅重启 Docker 服务时，Relay 无需重启：

```powershell
cd D:\PycharmProjects\EchoMind8.30\backend
docker compose restart

cd D:\PycharmProjects\EchoMind8.30\frontend
docker compose restart
```

Windows 重启后，从第 2 节重新执行即可。

## 6. 查看日志

```powershell
cd D:\PycharmProjects\EchoMind8.30\backend
docker compose logs --tail 100 echomind nginx

cd D:\PycharmProjects\EchoMind8.30\frontend
docker compose logs --tail 100 echomind-frontend
```

## 7. 停止服务

停止 Docker 服务但保留 MySQL、Redis 和 ChromaDB 数据：

```powershell
cd D:\PycharmProjects\EchoMind8.30\frontend
docker compose down

cd D:\PycharmProjects\EchoMind8.30\backend
docker compose down
```

MinerU Relay 可以继续运行；需要停止时，先查看其 PID：

```powershell
Get-NetTCPConnection -State Listen -LocalPort 18789
```

确认该进程为 Python Relay 后，再执行：

```powershell
Stop-Process -Id <PID>
```

不要使用 `docker compose down -v`，否则会删除本地数据库、缓存和向量数据。

## 根目录一键部署

在仓库根目录执行 `docker compose up -d --build --wait --wait-timeout 600`，统一启动 `backend/` 与 `frontend/`。前端静态文件在 Docker 内构建，无需宿主机预先运行 npm。

## 目录改名后的部署

后端目录为 `backend/`，前端目录为 `frontend/`；仓库根目录不变，仍在根目录执行上述 `docker compose` 命令。后端和前端的 Compose 项目名固定为改名前的 `echomind830`、`echomindfrontend830`，以沿用已有镜像、网络和数据卷。环境配置与本地数据随目录一起移动。

拉取此次更新前，已有部署需把旧后端目录内未纳入 Git 的 `.env`、`mysql.env`、`data/`、`logs/` 以及前端 `.env`（如有）迁移至对应新目录；不要覆盖新版本已跟踪的文件。运行中的容器需要在迁移后执行 `docker compose up -d --build --force-recreate --wait --wait-timeout 600`，以更新宿主机挂载路径；不要删除持久化数据卷。
