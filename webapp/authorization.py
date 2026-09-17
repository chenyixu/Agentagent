"""授权判定：登录名 → 业务作用域。

单独成模块的理由是它**不该依赖 I/O**。这条判定是整个浏览器链路里唯一一处
"登录名 → 业务主键"的转换，也是最该被直接测的一处；如果它长在 ``service.py`` 里，
测试就得先把整个应用装配起来（Redis、消息总线、工作区、模型凭证）才能问一句
"店长能不能对话"。把判定和装配分开，问题就只剩一个纯函数的输入输出。

最后一道防线在框架那一侧：它要求"会话记录的 (tenant, customer)"与这里返回的
``ToolScope`` 逐字相等，否则拒绝这次运行。所以这里的返回值不是建议，是约束。
"""

from __future__ import annotations

from fastapi import HTTPException, status

from agentscope.app import ToolScope

from .identity import BusinessIdentity, IdentityRegistry


def resolve_tool_scope(registry: IdentityRegistry, user_id: str) -> ToolScope:
    """已加载的身份表 + 登录名 → ``ToolScope``。拒绝时抛 403。

    两类拒绝，都不回退：

    - **未知登录名**：不在服务端身份表里。给一个默认租户比报错危险得多。
    - **已知但没有客户作用域**（如店长）：``ToolScope`` 要求 ``customer_id`` 非空，
      硬凑一个空值会让下游拿到一个语义不完整的作用域，然后把"未知"当成"全部"。

    错误信息里列出了本机可用的演示登录名。这是本地开发的有意选择：它们本来就是种子
    数据里写死的夹具，不是凭证；让人对着 403 猜该填什么没有意义。
    """

    known = "、".join(sorted(i.user_id for i in registry.all())) or "（无）"

    identity: BusinessIdentity | None = registry.find(user_id)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"未登记的登录名 {user_id!r}。本机可用：{known}。",
        )

    if identity.customer_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"{identity.user_id} 的角色是 {identity.role}，没有客户作用域，"
                "无法在这个界面里发起预约对话。"
            ),
        )

    return ToolScope(
        tenant_id=str(identity.tenant_id),
        customer_id=str(identity.customer_id),
    )


def resolve_identity_for_scope(
    registry: IdentityRegistry,
    tenant_id: str,
    customer_id: str,
) -> BusinessIdentity:
    """``ToolScope`` → 身份。这是 :func:`resolve_tool_scope` 的逆运算。

    框架只把核实过的 ``ToolScope`` 交给工具工厂，但装配业务工具还需要登录名、角色
    和门店。反查失败只可能来自"业务库重置了、Redis 里还留着旧会话"——那种情况下静默
    降级会让浏览器看到另一个客户的数据，所以这里也拒绝。
    """

    identity = registry.find_by_scope(tenant_id, customer_id)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"业务作用域 (tenant={tenant_id}, customer={customer_id}) 没有对应"
                "的登录名；业务库可能已重置，请重启本服务或清掉 Redis 里的旧会话"
            ),
        )
    return identity
