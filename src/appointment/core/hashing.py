"""规范化哈希。

设计稿 §5.2：``request_hash`` 覆盖服务端规范化的动作、目标与 expected version、
方案 ID/版本/哈希和其他有效业务参数；不包含 retry 次数、trace_id、原始 token、
SDK reply_id 与文案。金额转最小单位、时间转绝对 instant、资源列表按稳定 ID 排序、
缺省值统一后，采用约定的确定序列化计算哈希。

相同键同参数返回原结果；相同键不同参数必须冲突。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Mapping
from uuid import UUID


def _normalize(value: Any) -> Any:
    """把业务值折叠成可确定序列化的形式。"""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        # 金额一律走最小货币单位整数或字符串定点，禁止浮点。
        return format(value.normalize(), "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("哈希不接受 naive datetime，需先确定时区")
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _normalize(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple, set, frozenset)):
        normalized = [_normalize(v) for v in value]
        # 集合语义：排序后再算哈希，保证顺序无关。
        if isinstance(value, (set, frozenset)):
            normalized.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
        return normalized
    return str(value)


def canonical_json(payload: Mapping[str, Any]) -> str:
    normalized = _normalize(dict(payload))
    return json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def json_safe(payload: Any) -> Any:
    """把业务载荷折叠成可直接写入 JSONB 的形式。

    与 :func:`content_hash` 共用同一套规范化规则，因此"存下来的内容"与
    "参与哈希的内容"逐字段一致。这一点是必需的：提交时要拿库里存着的方案内容
    重算哈希并与签发时的 ``content_hash`` 比对，如果存储时被驱动按自己的规则
    序列化（例如 datetime 由 asyncpg 自行编码、键序不同），重算结果就会不一致，
    表现为"方案哈希校验失败"这种极难定位的错误。
    """

    return _normalize(payload)


def content_hash(payload: Mapping[str, Any]) -> str:
    """业务内容哈希（方案内容、请求参数、payload）。"""

    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def hash_secret(raw: str) -> str:
    """不可逆哈希，用于 token_hash 等。

    注意：这不是加盐 HMAC。涉及可枚举标识（如手机号）的关联必须使用
    :func:`pseudonymize`，且 HMAC 仍是可关联的假名化信息，不能称为匿名化。
    """

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def pseudonymize(raw: str) -> str:
    """用服务端密钥做 HMAC，避免可枚举输入的裸哈希。"""

    from ..config.settings import get_settings

    key = get_settings().pseudonymization_key.get_secret_value().encode("utf-8")
    import hmac

    return hmac.new(key, raw.encode("utf-8"), hashlib.sha256).hexdigest()


def stable_resource_ids(resource_ids: list[UUID] | list[str]) -> list[str]:
    """资源列表按稳定 ID 排序，保证不同请求顺序得到同一哈希。"""

    return sorted(str(rid) for rid in resource_ids)
