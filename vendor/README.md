# `vendor/` —— 为什么这里有一个第三方 wheel

本仓库的**浏览器侧**（`webapp/`）用到了 AgentScope App 服务的几个注入点，它们在**上游
发布版里不存在**，只有本地改动版有：

| 符号 | 从哪导入 | 作用 |
| --- | --- | --- |
| `ToolScope` | `agentscope.app` | 一次调用的可信身份三元组 `(tenant_id, customer_id, agent_id)` |
| `ToolExposurePolicy` | `agentscope.app` | 工具暴露策略（只放行白名单里的业务工具） |
| `tool_scope_resolver` | `create_app(...)` 参数 | 会话 → `ToolScope` 的解析钩子，解析不出来就拒绝 |
| `scoped_extra_agent_tools` | `create_app(...)` 参数 | 按 `ToolScope` 装配业务工具的钩子 |
| `scoped_context_agent_tools` | `create_app(...)` 参数 | 同上，供写工具（带审计关联）使用 |

后三个是 `create_app` 的**参数**，不是包级符号——`hasattr(agentscope.app, ...)` 是 `False`，
要用 `inspect.signature(create_app).parameters` 去查。`webapp/service.py` 的导入点：

```python
from agentscope.app import ToolExposurePolicy, ToolScope, create_app
from agentscope.app.message_bus import RedisMessageBus
from agentscope.app.storage import ...
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.agent import ContextConfig, ReActConfig
from agentscope.credential import DeepSeekCredential
```

（`RedisMessageBus` 在 `agentscope.app.message_bus`，**不在** `agentscope.app` 顶层。）

所以 `pip install agentscope` 装出来的版本**跑不起来** `webapp/`。为了仓库自包含，
这里放了一份可直接安装的构建产物。

## 里面是什么

| 文件 | 说明 |
| --- | --- |
| `agentscope-2.0.8-py3-none-any.whl` | 装了本地改动的 wheel，即 `.venv` 里那一份 |
| `agentscope-local-mods.patch` | 本地改动的完整 diff（`src/` 部分，为了可审计） |
| `agentscope-LICENSE` | 上游许可证（Apache-2.0） |

## 来源

- 上游：`agentscope` 仓库，HEAD `033a3613`（`v2.0.8-5-g033a3613`），分支 `main`。
- 本地改动：`src/` 下 39 个文件，合计 +4825/−213（见 `agentscope-local-mods.patch`）。
  这些改动加的是上表那几个注入点，以及为支撑它们而调整的 storage / message_bus /
  router / toolkit 部分。

## 装

```bash
.venv/bin/pip install vendor/agentscope-2.0.8-py3-none-any.whl
```

## 重建（有上游 checkout 的时候）

```bash
cd ~/Documents/git_project/agentscope          # 带本地改动的 checkout
python -m pip install build
python -m build --wheel --outdir /tmp/as-wheel
cp /tmp/as-wheel/agentscope-*.whl  <this repo>/vendor/
git diff -- src/ > <this repo>/vendor/agentscope-local-mods.patch
```

> **注意**：`pyproject.toml` 里的 `agentscope` 是**可选**依赖（`[project.optional-dependencies]`
> 的 `agentscope` 分组）。机器客户端接口 `/v1/*` 在 `APPOINTMENT_AGENT_RUNTIME=deterministic`
> 下**不需要**它——这正是"装不上也能跑"的设计。只有 `webapp/` 和
> `APPOINTMENT_AGENT_RUNTIME=agentscope` 才需要。

## 什么时候可以删掉这个目录

等这几个注入点合进上游发布版，或者本项目改成在构建期带补丁安装依赖。
在那之前删了它，`webapp/` 就没法从零装起来。
