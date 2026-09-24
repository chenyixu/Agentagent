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
| `webapp.service` + `frontend/` | 8010 / 5173 | 浏览器里的人 | AgentScope 对话只读；独立预约页支持查看方案、显式确认和续查结果 |

两个入口都不是"另一套实现"：`webapp/` 的工具最终调用的是同一个
`appointment.tools.registry.invoke_tool`——白名单、参数校验、权限判定、领域规则只有
一条代码路径。模型可见的 AgentScope 工具仍然只读；`/appointment` 页面把写入请求
直接送到带服务端身份映射的预约 API，由领域事务复核凭据、权限与幂等键。模型不能触发
预约写入，只有用户查看方案后点击确认才会提交。

## 布局

```
src/appointment/          领域层（业务事实的唯一来源）
  api/                    FastAPI 装配、路由、SSE、错误映射、身份依赖
  domain/                 预约语义：可用时段、报价、状态机、事件、知识
  orchestrator/           受约束的编排：轮次预算、工具调用上限、任务恢复
  agent/                  执行适配器：deterministic（基线） | agentscope（SDK 嵌入）
  tools/                  工具白名单与处理器——所有工具调用的唯一入口
  worker/                 异步投递：Outbox、认领、投递账本、回执、对账
  knowledge/              关键词检索（向量路留待 pgvector）
  db/                     SQLAlchemy 模型与会话
  seed.py                 演示种子数据

webapp/                   浏览器侧适配层（把 AgentScope App 服务接到领域层）
  identity.py             登录名 ↔ 业务主键映射
  authorization.py        登录名 → ToolScope / ToolScope → 身份，失败即 403
  tools.py                AgentScope 只读业务工具的装配（身份由闭包捕获）
  booking_api.py          浏览器身份映射与预约 API 挂载
  prompt.py               系统提示词（含能力边界）
  service.py              应用装配、Redis 播种、启动横幅

frontend/                 浏览器界面（AgentScope web_ui 的工作树副本，本地改动已含）

vendor/                   agentscope 本地改动版 wheel + 改动补丁（见 vendor/README.md）

tests/                    回归测试，按不变量 / 恢复 / 工具边界 / 契约分组

scripts/                  dev_reset.py（种子+清会话）、dev_receipt.py、webui_smoke.mjs
docs/screenshots/         浏览器验收截图
```

设计文档（中文）在根目录：`智能预约Agent系统设计.md`、`数据契约与库表设计.md`、
`设计评审与改进清单.md` 等；`人工测试指引.md` 是"用真实进程把整条链路跑一遍"的操作手册。

## 起服务

前置：PostgreSQL 在 **5433**（必须是 PostgreSQL——核心不变量依赖 `btree_gist`
区间排他约束，启动时会校验，缺了宁可启动失败）、Python 虚拟环境就绪。

机器客户端接口**不需要** AgentScope：`APPOINTMENT_AGENT_RUNTIME=deterministic`
下它就是一套普通的 FastAPI 服务。只有浏览器侧需要装 AgentScope（且必须是带本地
改动的版本，见 `vendor/README.md`）：

```bash
cp .env.example .env          # 按需改
.venv/bin/python scripts/dev_reset.py     # 灌种子并打印可复制的身份头
.venv/bin/python -m uvicorn appointment.api.app:create_app --factory --port 8000
curl -s http://127.0.0.1:8000/healthz     # 期望 status=ok
```

浏览器侧（三个进程，端口 8010 而非 3000——本机 3000 被 Grafana 占着）：

```bash
.venv/bin/pip install 'vendor/agentscope-2.0.8-py3-none-any.whl[service]'   # 首次，含浏览器 App 依赖
.venv/bin/python scripts/dev_reset.py
DEEPSEEK_API_KEY=sk-xxx APPOINTMENT_AGENT_RUNTIME=agentscope \
APPOINTMENT_MODEL_BACKEND=deepseek \
APPOINTMENT_MODEL_NAME=deepseek-flash \
APPOINTMENT_AGENT_PROMPT_VERSION=reception-v3 \
.venv/bin/python -m webapp.service  # 预约 API 与浏览器 Agent 调用真实 DeepSeek
cd frontend && pnpm install && pnpm dev
```

打开 http://localhost:5173 ，服务器地址填 `http://127.0.0.1:8010`，用户名填
`customer-1`。细节见 `人工测试指引.md` 第 7 节与 `webapp/README.md`。
进入左侧「智能预约」可提交自然语言需求、查看待确认方案并显式确认；刷新后页面先查
原操作，再从任务快照恢复待确认凭据，不会自动提交。

## 测试与实验证据

```bash
.venv/bin/python -B -m pytest -q -p no:cacheprovider --tb=short  # 最新 192/192 通过
.venv/bin/python -B -m pytest -q -p no:cacheprovider -m invariant
.venv/bin/python -B -m pytest -q -p no:cacheprovider tests/test_webapp_bridge.py
DEEPSEEK_API_KEY=sk-xxx APPOINTMENT_EVAL_MODEL=deepseek-flash .venv/bin/python -B evals/run_agent_e2e_batch.py --runs 3  # 真实模型合成闭环重复评测
APPOINTMENT_EVAL_MODEL=deepseek-flash .venv/bin/python -B evals/run_model_smoke.py --repeats 1  # 真实 DeepSeek 单步结构冒烟
.venv/bin/python -B evals/run_agent_e2e_batch.py --case-set evals/cases/full_booking_pilot_v1.json  # 10 个合成预约场景；最近复测 10/10
.venv/bin/python -B evals/generate_synthetic_business_data.py  # 生成多门店业务夹具与 64 个场景预期
.venv/bin/python -B -m pytest -q -p no:cacheprovider tests/test_synthetic_business_data.py
.venv/bin/python -B -m pytest -q -p no:cacheprovider tests/test_synthetic_business_database.py  # 34 个可用性场景 + 并发确认/幂等回放
```

多门店合成业务数据的规模、复现方法和评测边界见[合成业务数据集说明](evals/合成业务数据集说明.md)。数据库评测直接调用预约领域服务，不是 64 项 Agent/API 端到端成绩。

GitHub Actions 中的 `Synthetic Agent evaluation` 仅支持手动触发；仓库需先配置 Actions secret `DEEPSEEK_API_KEY`，并在运行时勾选确认以消耗模型额度。工作流执行 46 个首轮场景和 18 个事务场景，任一场景失败、报错、漏跑或未评分都会使门禁失败；原始 JSONL 与摘要作为 14 天 artifact 保存。该流程只使用生成的合成数据，不代表真实门店表现。

数据库用例各自创建 `appointment_test` 下的隔离 schema，按仓库约束保留，不自动清理。
300 次工具越权评测、150 个恢复场景、真实模型重复结构化决策冒烟、同场景及 10 场景完整预约评测、HTTP 并发确认和 3,200 次合成数据库争抢的结果与局限见
[实验阶段报告](evals/reports/实验阶段报告_2026-09-23.md)；[简历项目描述](docs/简历项目描述_智能预约Agent.md)
只引用已核验的结果。模型样本和数据库争抢都不是生产预约量或正式多场景完成率。

两项可复跑的入口评测（仅允许 `appointment_test`，每次运行会新建并保留隔离 schema）：

```bash
.venv/bin/python -B evals/run_boundary_eval.py --requests 300
.venv/bin/python -B evals/run_recovery_eval.py --cases-per-group 50
```

浏览器侧端到端冒烟（用系统 Chrome，不下载 Chromium；前后端都起着时跑）：

```bash
node scripts/webui_smoke.mjs
```

## 几条关键取舍

- **默认不发起真实 LLM 调用。** 机器客户端默认 `APPOINTMENT_MODEL_BACKEND=stub` +
  `APPOINTMENT_AGENT_RUNTIME=deterministic`，回复是确定性文本，测试因此可重复。
  浏览器聊天使用 `DEEPSEEK_API_KEY` 调用 DeepSeek；浏览器预约 API 只有在启动时设置
  `APPOINTMENT_AGENT_RUNTIME=agentscope`、`APPOINTMENT_MODEL_BACKEND=deepseek` 和
  `APPOINTMENT_MODEL_NAME=deepseek-flash` 后才启用真实模型，缺少配置时仍保持确定性基线。
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

两个**仓库自带**的外部构件，都是为了自包含：

- `frontend/` 是 AgentScope `examples/web_ui/frontend` 的工作树副本，**带本地改动**——
  那 21 个文件的改动正是让前端支持多租户后端（`X-User-ID` 身份头、`auth/config` 探测、
  按用户隔离的会话选择）的部分，用上游干净版本会连不上本服务。
- `vendor/` 是 AgentScope 的本地改动版 wheel，含 `ToolScope` / `tool_scope_resolver` /
  `scoped_extra_agent_tools` 等**上游发布版没有**的注入点——`webapp/` 依赖它们，
  所以 `pip install agentscope` 装出来的版本跑不起来。详见 `vendor/README.md`。
