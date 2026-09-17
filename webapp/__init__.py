"""浏览器侧的预约服务适配层。

把 AgentScope 的 Web UI 接到 ``appointment`` 领域层上：身份映射
(:mod:`webapp.identity`)、授权判定 (:mod:`webapp.authorization`)、只读工具装配
(:mod:`webapp.tools`)、提示词 (:mod:`webapp.prompt`)、应用装配
(:mod:`webapp.service`)。

这一层不含业务规则：业务规则全在 ``appointment`` 包里，这里只做适配。
"""

from __future__ import annotations

__all__ = ["authorization", "identity", "prompt", "service", "tools"]
