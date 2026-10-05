"""展品链状态令牌：预演签发、正式提交核验（纯标准库，可独立单测）。

令牌绑定「展品 + 链状态快照」（chain_length 与 chain_head_hash），
用 HMAC-SHA256 签名防伪造。格式：v1.<base64url(payload)>.<base64url(sig)>。

正式提交携带令牌时：
  - 格式/签名非法            -> 400 参数错误
  - 令牌属于其他展品          -> 409 状态冲突
  - 链状态已推进（快照不符）   -> 409 状态冲突，调用方须重新预演
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os

# 签名密钥：生产环境务必通过环境变量覆盖
STATE_TOKEN_SECRET = os.getenv("STATE_TOKEN_SECRET", "dev-only-state-token-secret")

VERSION = "v1"


class TokenError(ValueError):
    """令牌格式非法或签名不匹配。"""


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _sign(body: str) -> str:
    mac = hmac.new(STATE_TOKEN_SECRET.encode("utf-8"), f"{VERSION}.{body}".encode("utf-8"), hashlib.sha256)
    return _b64e(mac.digest())


def issue_state_token(exhibit_id: str, chain_length: int, chain_head_hash: str) -> str:
    """按展品当前链状态签发令牌（预演响应的一部分）。"""
    payload = json.dumps(
        {"exhibit_id": exhibit_id, "chain_length": chain_length, "chain_head_hash": chain_head_hash},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    body = _b64e(payload)
    return f"{VERSION}.{body}.{_sign(body)}"


def parse_state_token(token: str) -> dict:
    """解析并验签，返回 {exhibit_id, chain_length, chain_head_hash}。

    任何格式/签名问题统一抛 TokenError（调用方映射为 400）。
    """
    try:
        version, body, sig = token.split(".")
        if version != VERSION:
            raise TokenError(f"令牌版本「{version}」不受支持")
        if not hmac.compare_digest(_sign(body), sig):
            raise TokenError("令牌签名不匹配")
        payload = json.loads(_b64d(body))
        if not (
            isinstance(payload, dict)
            and isinstance(payload.get("exhibit_id"), str)
            and isinstance(payload.get("chain_length"), int)
            and isinstance(payload.get("chain_head_hash"), str)
        ):
            raise TokenError("令牌内容非法")
        return payload
    except TokenError:
        raise
    except Exception as exc:  # noqa: BLE001 - 结构异常统一归为格式非法
        raise TokenError("令牌格式非法") from exc
