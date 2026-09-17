# 智能预约 Agent 系统

门店预约场景的 Agentic Workflow 实现。核心思路是**把"不确定的对话"和"确定的预约语义"分开**：
模型只负责理解与表达，预约这件事由一套确定性领域核心负责，不变量（不超卖、不重复效果、
不越权）由数据库约束和状态机钉死，而不是靠提示词祈祷。

本仓库**自包含**：领域层、HTTP 接口、后台 Worker、浏览器侧的 App 服务与前端的全部源码
都在这里，不需要外部 checkout。

## 两个入口，一套领域层

| 入口 | 端口 | 面向谁 | 能力 |
| --- | --- | --- | --- |
| `appointment.api.app:create_app` | 8000 | 机器客户端（`/v1/*`，curl） | **完整链路**：提需求 → 选时段 → 确认下单 → 事件流 |
| `webapp.service` + `frontend/` | 8010 / 5173 | 浏览器里的人 | **只读**：报价、可用时段、政策问答 |

两个入口都不是"另一套实现"：`webapp/` 的工具最终调用的是同一个
`appointment.tools.registry.invoke_tool`——白名单、参数校验、权限判定、领域规则只有
一条代码路径。浏览器侧之所以只读，是因为写工具（占位、确认）需要幂等键与审计关联，
还没接；提示词要求模型**如实说明**这一点，不许说"已为您约上"。

## 布局

```
src/appointment/          领域层（业务事实的唯一来源）
  api/                    FastAPI 装配、路由、SSE、错误映射、身份依赖
  domain/                 预约语义：可用时段、报价、状态机、事件、知识
  orchestrator/           受约束的编排：轮次预算、工具调用上限、咨询委派
  agent/                  执行适配器：deterministic（基线） | agentscope（SDK 嵌入）
  tools/                  工具白名单与处理器——所有工具调用的唯一入口
  worker/                 异步投递：Outbox、认领、投递账本、回执、对账
  knowledge/              关键词检索（向量路留待 pgvector）
  db/                     SQLAlchemy 模型与会话
  seed.py                 演示种子数据

webapp/                   浏览器侧适配层（把 AgentScope App 服务接到领域层）
  identity.py             登录名 ↔ 业务主键映射
  authorization.py        登录名 → ToolScope / ToolScope → 身份，失败即 403
  tools.py                只读业务工具的装配（身份由闭包捕获）
  prompt.py               系统提示词（含能力边界）
  service.py              应用装配、Redis 播种、启动横幅

frontend/                 浏览器界面（AgentScope web_ui 的工作树副本，本地改动已含）

tests/                    135 个测试，按不变量 / 恢复 / 工具边界 / 契约分组

scripts/                  dev_reset.py（种子+清会话）、dev_receipt.py、webui_smoke.mjs
docs/screenshots/         浏览器验收截图
```

设计文档（中文）在根目录：`智能预约Agent系统设计.md`、`数据契约与库表设计.md`、
`设计评审与改进清单.md` 等；`人工测试指引.md` 是"用真实进程把整条链路跑一遍"的操作手册。

## 起服务

前置：PostgreSQL 在 **5433**（必须是 PostgreSQL——核心不变量依赖 `btree_gist`
区间排他约束，启动时会校验，缺了宁可启动失败）、Python 虚拟环境就绪。

```bash
cp .env.example .env          # 按需改
.venv/bin/python scripts/dev_reset.py     # 灌种子并打印可复制的身份头
.venv/bin/python -m uvicorn appointment.api.app:create_app --factory --port 8000
curl -s http://127.0.0.1:8000/healthz     # 期望 status=ok
```

浏览器侧（三个进程，端口 8010 而非 3000——本机 3000 被 Grafana 占着）：

```bash
.venv/bin/python scripts/dev_reset.py
DEEPSEEK_API_KEY=sk-xxx .venv/bin/python -m webapp.service
cd frontend && pnpm install && pnpm dev
```

打开 http://localhost:5173 ，服务器地址填 `http://127.0.0.1:8010`，用户名填
`customer-1`。细节见 `人工测试指引.md` 第 7 节与 `webapp/README.md`。

## 测试

```bash
.venv/bin/python -m pytest -q                          # 135 个
.venv/bin/python -m pytest -q -m invariant             # 只看不变量
.venv/bin/python -m pytest tests/test_webapp_bridge.py -q
```

浏览器侧端到端冒烟（用系统 Chrome，不下载 Chromium；前后端都起着时跑）：

```bash
node scripts/webui_smoke.mjs
```

## 几条关键取舍

- **默认不发起真实 LLM 调用。** 机器客户端默认 `APPOINTMENT_MODEL_BACKEND=stub` +
  `APPOINTMENT_AGENT_RUNTIME=deterministic`，回复是确定性文本，测试因此可重复。
  浏览器侧相反，它用 `DEEPSEEK_API_KEY` 走真实模型。
- **身份不能由请求体声明。** 请求里带 `tenant_id` 一律 400；身份只从请求头/令牌解析。
  换个租户的身份头去读别人的任务返回 `NOT_FOUND` 而不是 403——不泄露存在性。
- **回调入口是唯一不带租户身份的路由。** 身份来自 `(provider, account_id)` 路径 + 签名
  + 时间窗口，`payload` 自报的 tenant 一律忽略。认不出的回执照样入库**隔离**而非丢弃。
- **异步投递不假装成功。** 预约成功只写 Outbox；`ACCEPTED` 只代表供应商受理，走到
  `DELIVERED` 需要回调。没配供应商时停在 `PENDING` 并明确"未发送"。
- **浏览器侧认证是开发期模式。** `identity_provider=None` 时框架用 `X-User-ID` 请求头当
  登录名，这**不是**生产认证，正式环境要换成 `BearerPrincipalProvider`。
- **没有 Alembic 迁移。** 库表由 `create_all` 建立（开发与测试够用），生产迁移路径待补。

## 环境

Python ≥ 3.11（`pyproject.toml`），Node ≥ 18（前端，需 pnpm）。
`frontend/` 是 AgentScope `examples/web_ui/frontend` 的工作树副本，**带本地改动**——
那 21 个文件的改动正是让前端支持多租户后端（`X-User-ID` 身份头、`auth/config` 探测、
按用户隔离的会话选择）的部分，用上游干净版本会连不上本服务。
