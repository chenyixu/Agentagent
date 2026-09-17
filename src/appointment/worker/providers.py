"""通知供应商端口与沙箱实现。

设计稿 §11.2 对供应商的假设必须显式写下来，不能含糊：

- **HTTP 200 与"供应商受理"都不等于送达。** 只有可信回执才算 ``DELIVERED``；
  不提供送达回执的渠道停在 ``ACCEPTED``，并明确证据范围。
- **超时是 ``UNKNOWN`` 而不是失败。** 把"可能已经发出去"当成"没发出去"是重复
  打扰客户；当成"没发"又会在真正没发时漏掉提醒。所以单独一态，交给对账。
- **幂等键由应用生成且跨重试复用**，供应商侧据此去重。若供应商既不支持幂等
  又没有查单能力，就不能宣称"绝不重复"，只能显式承担残余风险（设计稿 §11.2）。

这里的沙箱实现不是"装作成功"：它真的记录每个幂等键对应的消息 ID，重复发送返回
同一个消息 ID，并支持脚本化注入（超时、可重试失败、终态失败），这样上层账本的
每条分支都能被确定性地测到。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import random
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from uuid import UUID

SendKind = Literal["ACCEPTED", "RETRYABLE", "FINAL", "UNKNOWN", "ACCEPTED_LOST"]

#: 开发默认签名密钥。真实部署必须来自秘密管理（设计稿 §11.2），
#: 不允许放在普通 JSON 配置或日志里。
SANDBOX_SIGNING_SECRET = "dev-only-provider-signing-key"


class ProviderTimeout(Exception):
    """调用超时/网络中断：效果不明。"""


@dataclass(frozen=True, slots=True)
class SendRequest:
    """一次逻辑投递的请求内容。

    ``idempotency_key`` 与 ``payload_hash`` 一起构成"这是不是同一条消息"的判据：
    键相同但内容不同说明内容变了，那属于**更正投递**，必须在账本层新建一条，
    而不是复用旧键去改供应商侧的内容。
    """

    delivery_id: UUID
    tenant_id: UUID
    channel: str
    recipient_ref: str
    template_version: str
    idempotency_key: str
    payload_hash: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SendOutcome:
    kind: SendKind
    provider_message_id: str | None = None
    provider_request_id: str | None = None
    error_code: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.kind == "ACCEPTED"


@dataclass(frozen=True, slots=True)
class QueryOutcome:
    """查单结果。``known=False`` 表示供应商也无法判定。"""

    known: bool
    delivered: bool = False
    accepted: bool = False
    provider_message_id: str | None = None
    error_code: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class NotificationProviderPort(Protocol):
    """通知供应商能力集合。

    ``account_id`` 参与去重作用域：不同供应商/账户的消息 ID 会碰撞，
    回调映射必须带上它（数据契约 §6）。
    """

    name: str
    account_id: str
    #: 供应商是否支持查单。不支持时 ``UNKNOWN`` 只能转人工，不能自动重发。
    supports_query: bool

    async def send(self, request: SendRequest) -> SendOutcome: ...

    async def query(
        self,
        *,
        idempotency_key: str,
        provider_message_id: str | None,
    ) -> QueryOutcome: ...


def _stable_message_id(idempotency_key: str) -> str:
    digest = hashlib.sha1(idempotency_key.encode("utf-8")).hexdigest()
    return f"sbx-{digest[:16]}"


class SandboxProvider:
    """沙箱供应商。

    支持三类注入，让上层的每条分支都能被测到：

    - ``script[key] = ["UNKNOWN", "ACCEPTED"]``：按顺序消费，超时后重试成功；
    - ``failure_rate``：按固定种子决定失败，可重复；
    - ``supports_query=False``：模拟无查单能力的渠道。
    """

    def __init__(
        self,
        *,
        account_id: str = "sandbox-account",
        failure_rate: float = 0.0,
        seed: int = 0,
        supports_query: bool = True,
        signing_secret: str = SANDBOX_SIGNING_SECRET,
        query_unknown: bool = False,
    ) -> None:
        self.name = "sandbox"
        self.account_id = account_id
        self.supports_query = supports_query
        self.signing_secret = signing_secret
        #: True 时查单永远回答"不知道"，用来测对账截止后转人工的分支。
        self.query_unknown = query_unknown
        self._random = random.Random(seed)
        self.failure_rate = failure_rate
        #: 幂等键 → 该键已产生的消息 ID。供应商侧去重的证据。
        self._messages: dict[str, str] = {}
        self._scripts: dict[str, list[SendKind]] = {}
        self._ordered_scripts: list[list[SendKind]] = []
        self._calls: list[str] = []
        self._queries: list[str] = []

    # ---------------- 测试注入 ----------------
    def script(self, idempotency_key: str, kinds: list[SendKind]) -> None:
        """按键注入。适合测试已经拿到键的场景。"""

        self._scripts[idempotency_key] = list(kinds)

    def script_next(self, kinds: list[SendKind]) -> None:
        """按调用顺序注入，不看键名。

        幂等键由应用按 (租户, 事件, 渠道, 接收者, 模板) 拼出来，测试在库生成
        事件 ID 之前拿不到它；"第 n 次调用要发生什么"才是测试真正关心的，
        所以这里按顺序注入。
        """

        self._ordered_scripts.append(list(kinds))

    @property
    def calls(self) -> list[str]:
        """被真正调用过的幂等键（按顺序）。用于断言"没有重复调用"。"""

        return list(self._calls)

    @property
    def queries(self) -> list[str]:
        return list(self._queries)

    # ---------------- 端口实现 ----------------
    async def send(self, request: SendRequest) -> SendOutcome:
        key = request.idempotency_key
        self._calls.append(key)

        # 供应商侧幂等：同一个键永远返回同一个消息 ID。
        if key in self._messages:
            return SendOutcome(
                kind="ACCEPTED",
                provider_message_id=self._messages[key],
                provider_request_id=f"req-{key[:8]}",
                detail={"idempotent_replay": True},
            )

        kind: SendKind = "ACCEPTED"
        scripted = self._scripts.get(key)
        if scripted:
            kind = scripted.pop(0)
        else:
            while self._ordered_scripts:
                head = self._ordered_scripts[0]
                if not head:
                    self._ordered_scripts.pop(0)
                    continue
                kind = head.pop(0)
                break
            else:
                if self.failure_rate and self._random.random() < self.failure_rate:
                    kind = "RETRYABLE"

        if kind == "UNKNOWN":
            # 请求可能根本没到供应商：不记录消息，查单会说"没有这条"。
            raise ProviderTimeout("供应商响应超时，效果不明")
        if kind == "ACCEPTED_LOST":
            # 供应商**已经受理**，只是响应丢了。这是对账存在的理由：
            # 查单能查到这条消息，于是不必重发。
            message_id = _stable_message_id(key)
            self._messages[key] = message_id
            raise ProviderTimeout("供应商已受理但响应丢失")
        if kind == "RETRYABLE":
            return SendOutcome(kind="RETRYABLE", error_code="PROVIDER_5XX")
        if kind == "FINAL":
            return SendOutcome(kind="FINAL", error_code="INVALID_RECIPIENT")

        message_id = _stable_message_id(key)
        self._messages[key] = message_id
        return SendOutcome(
            kind="ACCEPTED",
            provider_message_id=message_id,
            provider_request_id=f"req-{key[:8]}",
        )

    async def query(
        self, *, idempotency_key: str, provider_message_id: str | None
    ) -> QueryOutcome:
        self._queries.append(idempotency_key)
        if not self.supports_query:
            return QueryOutcome(known=False, error_code="QUERY_UNSUPPORTED")
        if self.query_unknown:
            return QueryOutcome(known=False, error_code="QUERY_TIMEOUT")
        message_id = provider_message_id or self._messages.get(idempotency_key)
        if message_id is None:
            # 查不到消息：说明上一次调用确实没有到达供应商。
            return QueryOutcome(known=True, accepted=False)
        return QueryOutcome(
            known=True, accepted=True, provider_message_id=message_id
        )

    def message_id_for(self, idempotency_key: str) -> str:
        """这个幂等键会拿到什么消息 ID。

        回执测试需要在同步响应之前构造 message_id（"回调先于响应"场景），
        复制一份公式到测试里就等于把"消息 ID 怎么算"变成两处真相。
        """

        return _stable_message_id(idempotency_key)

    # ---------------- 回执构造（测试与沙箱联调用） ----------------
    def build_receipt(
        self,
        *,
        event_id: str,
        message_id: str,
        event_type: str,
        occurred_at: str,
        issued_at: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """构造一个已签名的回执 ``(body, headers)``。

        ``issued_at`` 是**请求**时间（签名覆盖它），与 ``occurred_at``（供应商事件
        发生时间）分开：前者用于防重放窗口，后者用于事件顺序归并。两者混用会导致
        "迟到的真事件"被当成重放拒绝，或者重放被当成真事件接受。
        """

        body: dict[str, Any] = {
            "event_id": event_id,
            "message_id": message_id,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "issued_at": issued_at or occurred_at,
            "account_id": self.account_id,
        }
        if extra:
            body.update(extra)
        return body, sign_receipt(
            secret=self.signing_secret,
            account_id=self.account_id,
            body=body,
        )


def receipt_signing_payload(*, account_id: str, body: dict[str, Any]) -> bytes:
    """待签名字节串。

    签名覆盖账户与规范化后的正文：只签正文会让"换个账户投递同一份正文"通过校验。
    """

    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{account_id}\n{canonical}".encode("utf-8")


def sign_receipt(
    *, secret: str, account_id: str, body: dict[str, Any]
) -> dict[str, str]:
    signature = hmac.new(
        secret.encode("utf-8"),
        receipt_signing_payload(account_id=account_id, body=body),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-Provider-Signature": signature,
        "X-Provider-Account": account_id,
    }


def verify_receipt_signature(
    *, secret: str, account_id: str, body: dict[str, Any], signature: str | None
) -> bool:
    if not signature:
        return False
    expected = sign_receipt(secret=secret, account_id=account_id, body=body)
    return hmac.compare_digest(
        expected["X-Provider-Signature"], signature
    )


def build_provider(settings) -> NotificationProviderPort | None:
    """按配置构造供应商适配器。

    ``none`` 表示本环境不接通知渠道——这时 Worker 仍然处理 Outbox 与回收，
    但**不会**为通知建逻辑投递，也不会假装发出去了。
    """

    provider = getattr(settings, "notification_provider", "none")
    if provider == "none":
        return None
    if provider == "sandbox":
        return SandboxProvider(
            failure_rate=float(
                getattr(settings, "notification_sandbox_failure_rate", 0.0)
            )
        )
    raise ValueError(f"未知的通知供应商：{provider}")


__all__ = [
    "NotificationProviderPort",
    "ProviderTimeout",
    "QueryOutcome",
    "SANDBOX_SIGNING_SECRET",
    "SandboxProvider",
    "SendOutcome",
    "SendRequest",
    "build_provider",
    "receipt_signing_payload",
    "sign_receipt",
    "verify_receipt_signature",
]
