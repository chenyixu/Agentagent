# 浏览器界面（`frontend/`）

AgentScope Web UI 的**工作树副本**，为让本仓库自包含而搬进来。它不是一个独立项目，
是 `webapp/` 的配套前端：浏览器在这里点，请求打到 `webapp.service`，最终落到
`appointment` 领域层。

> 本 README 由本仓库改写（上游那份是 Vite 模板样板，对本场景没有信息量）。

## 来源与本地改动

- 上游：AgentScope 仓库 `examples/web_ui/frontend`（Upstream HEAD `033a361`）。
- 搬的是**工作树**而不是上游提交：这里含 21 个文件的本地改动（+774/−79），
  它们正是让前端支持**多租户后端**的部分——`X-User-ID` 身份头、`auth/config` 探测、
  按用户隔离的会话选择。用上游干净版本会连不上本服务。
- `@agentscope-ai/agentscope` 依赖为 `^0.0.15`，锁到 `0.0.15`，与上游一致。
  该依赖**不是** `workspace:*`，所以本目录能独立 `pnpm install`。

搬进来后只做了两处**非源码**改动：

| 文件 | 改了什么 | 为什么 |
| --- | --- | --- |
| `pnpm-workspace.yaml` | 新增构建脚本白名单（`allowBuilds` 策略） | 没有它，`pnpm install` 会因供应链闸门报 `ERR_PNPM_IGNORED_BUILDS` |
| `pnpm-lock.yaml` | 首次安装生成 | 锁定依赖版本 |

`package.json` 与上游逐字一致（改它会让下次同步上游变麻烦，所以宁可加一个
`pnpm-workspace.yaml`）。

## 跑

```bash
pnpm install       # 首次
pnpm dev           # http://localhost:5173
pnpm build         # 产物在 dist/
pnpm lint
```

后端要先起着（`webapp.service`，默认 8010）。打开页面后在 setup 页填：

- **服务器地址**：`http://127.0.0.1:8010`
- **用户名**：`customer-1`

这两项存在 localStorage 的 `server_url` / `username`，所以刷新不会掉。

## 两个容易踩的点

- **`vite.config.ts` 里的 `/api` → `localhost:3000` 是没用到的。** 前端真正用的是
  localStorage 里的 `server_url`（见 `src/api/client.ts` 的 `getBaseUrl`）。
  8010 而非 3000 是因为本机 3000 被 Grafana 占着。
- **控制台会有几条红色错误**（`/knowledge_bases/` 503、`/handoff/...` 404、
  `/refund-proposals/...` 404）。都是 order_demo 的功能面板，本服务有意不提供，
  聊天不受影响。

## 同步上游

要重新拉上游改动时，**不要**直接覆盖——会丢掉那 21 个文件的本地改动。
做法是 `git diff` 出上游的改动再挑进来，本目录的 `pnpm-workspace.yaml` 与
`pnpm-lock.yaml` 保留不动。
