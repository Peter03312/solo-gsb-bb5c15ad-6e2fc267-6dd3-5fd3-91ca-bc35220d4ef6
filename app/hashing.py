"""规范化序列化与哈希链计算（纯标准库，便于单测与审计复算）。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

# 每条展品审计链的创世前驱哈希
GENESIS_HASH = "GENESIS"


def canonical_json(obj) -> bytes:
    """确定性 JSON 序列化：键排序、无空白、UTF-8。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def norm_ts(dt: datetime) -> str:
    """时间戳规范化：统一换算到 UTC、微秒精度、Z 后缀。

    写入与审计复算必须使用同一规范化，否则同一时刻的不同表示
    （+00:00 / Z / 不同精度）会得到不同哈希。
    """
    if dt.tzinfo is None:
        raise ValueError("时间必须携带时区")
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def request_hash(exhibit_id: str, records: list[dict]) -> str:
    """请求体指纹：用于幂等键下检测「同键不同载荷」。"""
    return sha256_hex(canonical_json({"exhibit_id": exhibit_id, "records": records}))


def record_hash(
    *,
    prev_hash: str,
    exhibit_id: str,
    chain_seq: int,
    batch_id,
    location: str,
    from_person: str,
    to_person: str,
    seal_no: str,
    seal_change_reason: str | None,
    occurred_at: datetime,
    voucher_digest: str,
) -> str:
    """单条交接记录的链式哈希：record_hash = sha256(prev_hash ‖ 业务字段)。

    任何字段被篡改、记录被删除或顺序被调换，都会使后续哈希校验失败。
    """
    payload = {
        "prev_hash": prev_hash,
        "exhibit_id": exhibit_id,
        "chain_seq": chain_seq,
        "batch_id": str(batch_id),
        "location": location,
        "from_person": from_person,
        "to_person": to_person,
        "seal_no": seal_no,
        "seal_change_reason": seal_change_reason or "",
        "occurred_at": norm_ts(occurred_at),
        "voucher_digest": voucher_digest,
    }
    return sha256_hex(canonical_json(payload))
