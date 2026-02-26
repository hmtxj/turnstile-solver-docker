# Turnstile Solver Docker

Cloudflare Turnstile 解题微服务节点，一键部署到 Zeabur / Docker。

## 部署到 Zeabur
1. Fork 或导入本仓库到 Zeabur
2. Zeabur 自动识别 Dockerfile 并构建
3. 部署完成后获得域名 `https://xxx.zeabur.app`

## 本地运行
```bash
docker build -t turnstile-solver .
docker run -p 5000:5000 turnstile-solver
```

## API
- `GET /` — 健康检查
- `GET /turnstile?url=<url>&sitekey=<key>` — 创建解题任务
- `GET /result?task_id=<id>` — 获取结果
