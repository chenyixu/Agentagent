"""智能预约 Agent 系统。

分层（依赖方向单向向下，下层不得反向调用上层）：

    api / worker       提交消息、订阅事件、执行后台任务
      |
    orchestrator       任务状态机、版本裁决、等待与重建式恢复
      |
    agents / tools     受限推理与工具边界（参数、身份、归属、版本、凭据校验）
      |
    domain             预约领域服务：业务规则、幂等、资源约束、事务、审计
      |
    db                 模型、会话、事务与锁协议
      |
    core               枚举、错误码、结果契约、时钟、哈希

`ports` 定义可替换边界：AgentRuntimePort / WorkflowPort / BookingPort /
KnowledgePort / NotificationPort。业务类型不导入任何 SDK 消息类。
"""

__version__ = "0.1.0"
