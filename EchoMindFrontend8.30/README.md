# EchoMind Frontend

独立 Vue 前端项目，连接 EchoMind Python 后端。

项目目录：

```text
/Users/xiao_xiong/Desktop/code/EchoMindFrontend
```

## 功能

- 适配 Python 后端的 `/chat` 响应字段：`conv_id`、`agent_type`、`latency_ms`。
- 支持聊天调试、健康检查、监控摘要、知识库检索、知识库文档导入、文件上传。
- 支持 Docker + Nginx 部署。

## 默认后端地址

| 后端 | 默认地址 |
|------|----------|
| Python | `http://localhost:8100` |

开发模式下，Vite 会代理：

| 前端路径 | 代理到 |
|----------|--------|
| `/api/python` | `http://localhost:8100` |

Docker 模式下，Nginx 会通过 `host.docker.internal` 访问宿主机上的 Python 服务。

## 本地运行

安装依赖：

```bash
npm install
```

启动：

```bash
npm run dev
```

访问：

```text
http://localhost:5173
```

如果后端端口不是默认值，可以启动时覆盖：

```bash
VITE_PYTHON_API_URL=http://localhost:8100 \
npm run dev
```

## Docker 部署

先构建前端静态文件：

```bash
npm run build
```

再构建并启动容器：

```bash
docker compose up -d --build
```

访问：

```text
http://localhost:5174
```

停止：

```bash
docker compose down
```

## 后端启动参考

Python 后端默认：

```text
http://localhost:8100
```
