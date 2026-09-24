# 浏览器侧适配层（`webapp/`）

把 AgentScope 的 Web UI 接到本项目的领域层上。**这一层不含业务规则**——预约、报价、
可用时段、政策的事实全部来自 `appointment` 包。

```
浏览器 (frontend/, React + Vite)
    │  X-User-ID: customer-1        ← 开发期身份头
    ▼
AgentScope App 服务 (webapp/service.py)
    │  ① tool_scope_resolver     登录名 → ToolScope(tenant, customer)
    │  ② scoped_extra_agent_tools  ToolScope → 只读业务工具（模型只能看这些）
    ▼
webapp/tools.py  ── invoke_tool ──▶  appointment.tools.registry
                                          （白名单/校验/权限/领域规则唯一入口）
                                          │
                                          ▼
                                     业务库 (PostgreSQL)

预约页 (frontend/src/pages/appointment)
    │  X-User-ID → 服务端 IdentityRegistry → TrustedContext
    ▼
webapp/booking_api.py ── /booking/v1/* ──▶ appointment.api.routes
                                         │  显式确认 / 幂等查单 / 持久事件 SSE
                                         └─▶ 同一业务库与领域事务
```

## 三条不变式

1. **业务事实只在业务库。** Redis 里存会话、消息、事件流；业务事实全在 `appointment`。
   浏览器看到的工具和编排器用的工具是**同一条代码路径**（都走 `invoke_tool`），不会
   长成两套语义。
2. **可信身份只来自服务端。** 登录名经 `webapp/identity.py` 映射成业务 UUID，再折成
   `ToolScope`。模型可见的函数签名里**没有任何身份字段**——没有可传的地方，也就没有
   传错的可能。`tests/test_webapp_bridge.py::test_model_visible_schema_exposes_no_identity_fields`
   钉住这条。
3. **作用域必须自洽。** 框架会核对"会话记录的作用域"与"解析出的 `ToolScope`"是否相等，
   不等就拒。所以种子数据与会话的作用域都从同一份身份表取。

## 模块

| 文件 | 职责 |
| --- | --- |
| `identity.py` | 登录名 ↔ 业务主键的双向映射；确定性排序 |
| `authorization.py` | 授权判定（登录名 → `ToolScope`，`ToolScope` → 身份），失败即 403 |
| `tools.py` | 只读业务工具的装配；身份由闭包捕获 |
| `booking_api.py` | 浏览器预约 API 的路由挂载与服务端身份映射 |
| `prompt.py` | 系统提示词（含能力边界） |
| `service.py` | 应用装配、Redis 播种、启动横幅、`__main__` |

## 当前能力边界（重要）

- **模型工具只读。** AgentScope 模型仍只拿到 `get_service_quote` /
  `search_availability` / `search_knowledge`，没有任何写工具。
- **浏览器预约走独立 API。** `/appointment` 页面使用 `/booking/v1/*` 路由，服务端从
  `X-User-ID` 映射出可信身份；确认凭据不存浏览器持久化，用户必须先查看方案再点确认。
  刷新后先按原幂等键查询操作，再读取任务快照并恢复仍有效的凭据，不会自动下单。
- **凭据恢复只读。** `POST /booking/v1/tasks/{task_id}/confirmation-credential` 只会
  重发当前授权顾客、当前有效方案与占位对应的未消费凭据，并返回 `Cache-Control: no-store`；
  真正写入仍由原确认事务处理。
- **只有客户能对话。** 店长没有 `customer_id`，给不出 `ToolScope`，解析器明确拒绝。
  在浏览器里用 `manager` 登录会看到一个空白的智能体列表。
- **认证是开发期模式。** `identity_provider=None` 时框架用 `X-User-ID` 请求头当登录名。
  这**不是**生产认证；正式环境要换成 `BearerPrincipalProvider`（见 `agentscope.app._auth`）。

## 跑起来

```bash
# 1. 业务库种子（顺带清掉本演示的 AgentScope 会话键）
.venv/bin/python scripts/dev_reset.py

# 2. 浏览器侧服务（默认 http://127.0.0.1:8010）
APPOINTMENT_AGENT_RUNTIME=agentscope \
APPOINTMENT_MODEL_BACKEND=deepseek \
APPOINTMENT_MODEL_NAME=deepseek-flash \
APPOINTMENT_AGENT_PROMPT_VERSION=reception-v3 \
.venv/bin/python -m webapp.service

# 3. 前端（本仓库自带，无需外部 checkout）
cd frontend && pnpm install && pnpm dev
```

打开 http://localhost:5173 ，在 setup 页填：

- 服务器地址：`http://127.0.0.1:8010`
- 用户名：`customer-1`（或 `customer-2`）

启动前在 `.env` 或进程环境中配置 `DEEPSEEK_API_KEY`。上述设置让浏览器聊天和预约 API 都使用真实 DeepSeek；密钥以 `SecretStr` 载入，不进入配置导出或 Redis，预约确认仍由服务端专用接口与显式用户操作执行。启动横幅会列出本机可登录的演示身份；也可以 `curl http://127.0.0.1:8010/demo/identities`。

### 端口为什么不是 3000

本机的 3000 被 observability 那套 Grafana 占着。setup 页接受任意地址，所以默认用
8010（`PORT` 可覆盖）。注意 `frontend/vite.config.ts` 里的 `/api` 代理指向
`localhost:3000` 是**没用到的**——前端真正用的是 localStorage 里的 `server_url`。

## 依赖：必须是带本地改动的 AgentScope

这一层用到的注入点——`ToolScope`、`tool_scope_resolver`、`scoped_extra_agent_tools`、
`ToolExposurePolicy`——**上游发布版没有**。`pip install agentscope` 装出来的版本会
在装配时 AttributeError。仓库自带了构建好的 wheel：

```bash
.venv/bin/pip install vendor/agentscope-2.0.8-py3-none-any.whl
```

来源与重建方式见 `vendor/README.md`。机器客户端接口（`/v1/*`）在
`APPOINTMENT_AGENT_RUNTIME=deterministic` 下**不需要**它。

## `frontend/` 的来源（vendored）

`frontend/` 是 AgentScope 仓库 `examples/web_ui/frontend` 的**工作树副本**，为让本仓库
自包含而搬进来。关键点：搬的是**带本地改动的版本**，不是上游 HEAD。

那些改动（+774/−79，21 个文件）正是让前端支持"多租户后端"的部分——`X-User-ID`
身份头、`auth/config` 探测、按用户隔离的会话选择等。用上游干净版本会连不上本服务。

搬进来后只做了两处非源码改动：

- `pnpm-workspace.yaml`：pnpm 的构建脚本白名单（`allowBuilds` 策略），
  否则 `pnpm install` 会被供应链闸门拦下。`package.json` 与上游逐字一致。
- `pnpm-lock.yaml`：锁定的 `@agentscope-ai/agentscope` 与上游同为 `0.0.15`。

上游前端有 `workspace:*` 依赖的话本仓库就跑不起来；实测是**没有**，
所以 `frontend/` 可以独立 `pnpm install`。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PORT` | `8010` | 服务端口 |
| `HOST` | `127.0.0.1` | 绑定地址 |
| `DEEPSEEK_API_KEY` | — | 缺省时不播种会话（界面会明确报缺密钥） |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | |
| `APPOINTMENT_WEB_MODEL` | `deepseek-flash` | 浏览器聊天会话使用的 DeepSeek 模型名 |
| `APPOINTMENT_AGENT_RUNTIME` | `deterministic` | 预约 API 的运行时；真实模型需设为 `agentscope` |
| `APPOINTMENT_MODEL_BACKEND` | `stub` | 预约 API 的模型后端；真实 DeepSeek 需设为 `deepseek` |
| `APPOINTMENT_MODEL_NAME` | `qwen-plus` | 预约 API 使用的模型；DeepSeek 建议设为 `deepseek-flash` |
| `APPOINTMENT_AGENT_PROMPT_VERSION` | `reception-v3` | 预约 API 的结构化决策提示词版本 |
| `APPOINTMENT_REDIS_HOST` / `_PORT` / `_DB` / `_PASSWORD` | `127.0.0.1` / `16379` / `0` / — | |
| `APPOINTMENT_WEB_WORKSPACE_DIR` | `.local/workspaces` | 工作区根目录 |

## 已知的非致命噪音

浏览器控制台会出现几条错误，都是 order_demo 的功能面板，本服务**有意不提供**：

- `GET /knowledge_bases/` → 503（未配 `knowledge_base_manager`，知识库面板关闭）
- `GET /handoff/...` → 404（转人工是 order_demo 的功能）
- `GET /refund-proposals/session/...` → 404（退款提案同上）

聊天本身不受影响；知识检索走的是 `search_knowledge` 业务工具，不依赖知识库面板。

## 测试

```bash
.venv/bin/python -m pytest tests/test_webapp_bridge.py -q
.venv/bin/python -m pytest tests/test_browser_booking_api.py -q
```

锁的是边界而不是"能跑通"：身份映射、模型可见 schema 里没有身份字段、只读工具确实
只读、预约上下文拒绝未知身份/店长身份，确认仍需单独凭据和显式按钮。

浏览器侧的端到端冒烟（用系统 Chrome，不需要下载 Chromium）：

```bash
node scripts/webui_smoke.mjs        # 前后端都起着的时候跑
```

它走完"填 setup → 提问 → 等真实模型回答"，断言：工具卡片只调用**业务工具**、
回答里有真实报价与可用时段、并且写明"只读/未占位"边界。截图落在
`docs/screenshots/`。
