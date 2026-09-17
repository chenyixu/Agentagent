"""开发用：向本机 API 投递一条**已签名**的供应商回执。

回调是全系统唯一不带租户身份的写入口，人工测试时必须能自己造一条合法回执，
否则投递账本永远停在 ACCEPTED，验证不到"回执驱动状态收敛"这条链路。

用法::

    .venv/bin/python scripts/dev_receipt.py --message-id sbx-xxxx --status DELIVERED
    .venv/bin/python scripts/dev_receipt.py --message-id sbx-xxxx --status DELIVERED \
        --issued-at-offset 0        # 时间窗口内（默认）
    .venv/bin/python scripts/dev_receipt.py --message-id sbx-xxxx --status DELIVERED \
        --issued-at-offset -900     # 故意超出重放窗口，用于验证隔离

也可以只打印签名而不发送：``--dry-run``。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

from appointment.worker.providers import sign_receipt  # noqa: E402
from appointment.worker.receipts import sandbox_account  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="投递一条签名的沙箱回执")
    parser.add_argument("--message-id", required=True, help="provider_message_id（投递尝试里的那个）")
    parser.add_argument(
        "--status",
        default="delivered",
        help="供应商事件类型：delivered / accepted / bounced / complained",
    )
    parser.add_argument("--event-id", default=None, help="供应商事件 ID，默认按消息 ID 生成")
    parser.add_argument(
        "--issued-at-offset",
        type=int,
        default=0,
        help="issued_at 相对现在的秒偏移（负数表示更早，用于验证重放窗口）",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--provider", default="sandbox")
    parser.add_argument("--account-id", default="sandbox-account")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    account = sandbox_account(account_id=args.account_id)
    now = datetime.now(timezone.utc)
    event_type = args.status.lower()
    body = {
        "event_id": args.event_id or f"evt-{args.message_id}-{event_type}",
        # 归并只看 event_type（delivered/accepted/bounced/complained），
        # 不看自报的 status —— 自报字段不能推进我方状态。
        "event_type": event_type,
        "message_id": args.message_id,
        "status": event_type,
        "issued_at": (now + timedelta(seconds=args.issued_at_offset)).isoformat(),
        "occurred_at": now.isoformat(),
    }
    headers = sign_receipt(
        secret=account.signing_secret, account_id=account.account_id, body=body
    )
    url = f"{args.base_url}/v1/provider-receipts/{args.provider}/{args.account_id}"

    print(f"POST {url}")
    print(f"headers: {headers}")
    print(f"body: {json.dumps(body, ensure_ascii=False)}")
    if args.dry_run:
        return 0

    # 本地探测必须绕过 HTTP 代理。
    with httpx.Client(timeout=15.0, trust_env=False) as client:
        response = client.post(url, json=body, headers=headers)
    print(f"HTTP {response.status_code}")
    try:
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    except Exception:  # pragma: no cover - 非 JSON 响应
        print(response.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
