"""HTTP API：文物借展封签交接核验服务。"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Query
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from . import __version__
from .db import get_conn, wait_for_db
from .hashing import GENESIS_HASH, record_hash, request_hash
from .schemas import (
    AuditVerifyOut,
    BatchReport,
    BatchRequest,
    BatchSummary,
    ChainOut,
    ExhibitCreate,
    ExhibitOut,
    RehearsalOut,
)
from .tokens import (
    InvalidTokenError,
    TOKEN_SECRET,
    issue_token,
    parse_token,
    state_matches,
)
from .validation import ExhibitState, validate_batch


@asynccontextmanager
async def lifespan(app: FastAPI):
    wait_for_db()
    yield


app = FastAPI(
    title="文物借展封签交接核验服务",
    version=__version__,
    lifespan=lifespan,
    description="校验借展交接链的首站、人员衔接、时间递增与封签连续性；"
                "幂等写入，失败批次不落任何交接记录，保留可查询的哈希审计链。",
)

# ---------------------------------------------------------------- 工具


def _exhibit_state(row: dict) -> ExhibitState:
    return ExhibitState(
        exhibit_id=row["exhibit_id"],
        home_location=row["home_location"],
        custodian=row["custodian"],
        current_seal=row["current_seal"],
        last_time=row["last_time"],
        chain_length=row["chain_length"],
    )


def _serialize_items(result) -> list[dict]:
    return [
        {
            "seq": item.seq,
            "ok": item.ok,
            "reasons": [{"rule": r.rule, "message": r.message} for r in item.reasons],
        }
        for item in result.items
    ]


def _serialize_first_break(result) -> dict | None:
    fb = result.first_break
    if fb is None:
        return None
    seq, reason = fb
    return {"seq": seq, "rule": reason.rule, "message": reason.message}


def _build_report(batch_id: uuid.UUID, exhibit_id: str, result, record_count: int, now: datetime) -> dict:
    return {
        "batch_id": str(batch_id),
        "exhibit_id": exhibit_id,
        "status": "ACCEPTED" if result.accepted else "REJECTED",
        "record_count": record_count,
        "first_break": _serialize_first_break(result),
        "items": _serialize_items(result),
        "idempotent_replay": False,
        "created_at": now.isoformat(),
    }


def _replay_report(row: dict) -> dict:
    report = dict(row["report"])
    report["idempotent_replay"] = True
    return report


# ---------------------------------------------------------------- 健康检查


@app.get("/health", tags=["meta"])
def health():
    with get_conn() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "version": __version__}


# ---------------------------------------------------------------- 展品


@app.post("/api/v1/exhibits", response_model=ExhibitOut, status_code=201, tags=["exhibits"])
def create_exhibit(payload: ExhibitCreate):
    """登记展品（馆藏地/保管人/初始封签为首站与连续性校验的基准）。"""
    try:
        with get_conn() as conn:
            row = conn.execute(
                """
                INSERT INTO exhibits (exhibit_id, name, home_location, custodian, current_seal)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (payload.exhibit_id, payload.name, payload.home_location, payload.custodian, payload.initial_seal),
            ).fetchone()
            return row
    except UniqueViolation:
        raise HTTPException(status_code=409, detail=f"展品「{payload.exhibit_id}」已存在")


@app.get("/api/v1/exhibits/{exhibit_id}", response_model=ExhibitOut, tags=["exhibits"])
def get_exhibit(exhibit_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM exhibits WHERE exhibit_id = %s", (exhibit_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"展品「{exhibit_id}」不存在")
    return row


# ---------------------------------------------------------------- 交接批次（核心）


@app.post("/api/v1/handover-batches/rehearse", response_model=RehearsalOut, tags=["handovers"])
def rehearse_handover_batch(payload: BatchRequest):
    """预演一批交接：只读核验，不写入批次报告、交接记录，也不推进展品状态。

    - 接受与正式提交完全相同的展品编号与按序记录，记录格式仍按现有规则校验
      （载荷格式非法时返回 422）。
    - 返回逐条核验结果、首个断点，以及绑定展品当前链状态的 `state_token`。
    - 未知展品返回 404。
    - 正式提交时可在 `Handover-Token` 头携带令牌；链状态未变化才被受理。
    """
    with get_conn() as conn:
        exhibit = conn.execute(
            "SELECT * FROM exhibits WHERE exhibit_id = %s",
            (payload.exhibit_id,),
        ).fetchone()
    if not exhibit:
        raise HTTPException(status_code=404, detail=f"展品「{payload.exhibit_id}」不存在")

    result = validate_batch(_exhibit_state(exhibit), payload.records)
    now = datetime.now(timezone.utc)
    token = issue_token(
        TOKEN_SECRET,
        payload.exhibit_id,
        exhibit["chain_length"],
        exhibit["chain_head_hash"],
        issued_at=int(now.timestamp()),
    )
    return {
        "exhibit_id": payload.exhibit_id,
        "status": "ACCEPTED" if result.accepted else "REJECTED",
        "record_count": len(payload.records),
        "first_break": _serialize_first_break(result),
        "items": _serialize_items(result),
        "rehearsed_at": now.isoformat(),
        "state_token": {
            "token": token,
            "exhibit_id": payload.exhibit_id,
            "chain_length": exhibit["chain_length"],
            "chain_head_hash": exhibit["chain_head_hash"],
            "issued_at": now.isoformat(),
        },
    }


@app.post("/api/v1/handover-batches", response_model=BatchReport, tags=["handovers"])
def submit_handover_batch(
    payload: BatchRequest,
    idempotency_key: str = Header(min_length=1, max_length=128),
    handover_token: str | None = Header(
        default=None,
        alias="Handover-Token",
        max_length=2048,
        description="可选：预演（/rehearse）签发的展品状态令牌",
    ),
):
    """提交一批交接记录并核验。

    - 幂等：携带 `Idempotency-Key` 请求头；同键同载荷重试返回同一报告
      （`idempotent_replay=true`），同键不同载荷返回 409。
      携带 `Handover-Token` 时令牌计入载荷身份；同键成功重试即使链已推进，
      也直接返回原报告。
    - 预演令牌（可选）：令牌格式无效 -> 400；跨展品或链状态已变化 -> 409，
      冲突时不生成批次报告，调用方须重新预演。不带令牌的提交行为不变。
    - 事务：核验通过则批次报告与全部交接记录一并写入并推进链上状态；
      核验失败仅落核验报告（供幂等重试与审计），不写入任何交接记录。
    """
    # 1) 令牌先验格式/签名（纯本地校验，400 参数错误），再在展品行锁上比对状态
    parsed_token = None
    if handover_token and handover_token.strip():
        try:
            parsed_token = parse_token(TOKEN_SECRET, handover_token)
        except InvalidTokenError as exc:
            raise HTTPException(status_code=400, detail=f"Handover-Token 无效：{exc}")

    records_for_hash = [r.model_dump(mode="json") for r in payload.records]
    req_hash = request_hash(payload.exhibit_id, records_for_hash, handover_token)

    try:
        with get_conn() as conn:
            # 2) 幂等预检（同键成功重试即使链已推进/令牌已过期，也返回原报告）
            existing = conn.execute(
                "SELECT * FROM batches WHERE idempotency_key = %s", (idempotency_key,)
            ).fetchone()
            if existing:
                if existing["request_hash"] != req_hash:
                    raise HTTPException(
                        status_code=409,
                        detail="同一 Idempotency-Key 提交了不同的请求载荷",
                    )
                return _replay_report(existing)

            # 3) 锁定展品行，串行化同一展品的批次，保证链式状态一致
            exhibit = conn.execute(
                "SELECT * FROM exhibits WHERE exhibit_id = %s FOR UPDATE",
                (payload.exhibit_id,),
            ).fetchone()
            if not exhibit:
                if parsed_token is not None:
                    # 令牌签名有效但展品对不上：按跨展品状态冲突处理
                    raise HTTPException(
                        status_code=409,
                        detail=f"令牌属于展品「{parsed_token.exhibit_id}」，"
                               f"不能用于展品「{payload.exhibit_id}」，请重新预演",
                    )
                raise HTTPException(status_code=404, detail=f"展品「{payload.exhibit_id}」不存在")

            # 4) 令牌归属与链状态比对（行锁内完成，与链推进互斥）
            if parsed_token is not None:
                if parsed_token.exhibit_id != payload.exhibit_id:
                    raise HTTPException(
                        status_code=409,
                        detail=f"令牌属于展品「{parsed_token.exhibit_id}」，"
                               f"不能用于展品「{payload.exhibit_id}」，请重新预演",
                    )
                if not state_matches(
                    parsed_token, exhibit["chain_length"], exhibit["chain_head_hash"]
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="展品链状态自预演后已变化（哈希链已推进），"
                               "令牌失效，请重新预演后再提交",
                    )

            # 5) 核验
            result = validate_batch(_exhibit_state(exhibit), payload.records)
            batch_id = uuid.uuid4()
            now = datetime.now(timezone.utc)
            report = _build_report(batch_id, payload.exhibit_id, result, len(payload.records), now)

            conn.execute(
                """
                INSERT INTO batches (batch_id, idempotency_key, exhibit_id, request_hash, status, report, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (batch_id, idempotency_key, payload.exhibit_id, req_hash,
                 report["status"], Jsonb(report), now),
            )

            # 6) 通过才写交接记录并推进链上状态；失败则只有批次报告落库
            if result.accepted:
                prev_hash = exhibit["chain_head_hash"]
                prev_seal = exhibit["current_seal"]
                chain_seq = exhibit["chain_length"]
                for seq_in_batch, rec in enumerate(payload.records, start=1):
                    chain_seq += 1
                    h = record_hash(
                        prev_hash=prev_hash,
                        exhibit_id=payload.exhibit_id,
                        chain_seq=chain_seq,
                        batch_id=batch_id,
                        location=rec.location,
                        from_person=rec.from_person,
                        to_person=rec.to_person,
                        seal_no=rec.seal_no,
                        seal_change_reason=rec.seal_change_reason,
                        occurred_at=rec.occurred_at,
                        voucher_digest=rec.voucher_digest,
                    )
                    conn.execute(
                        """
                        INSERT INTO handover_records
                            (batch_id, exhibit_id, chain_seq, seq_in_batch, location,
                             from_person, to_person, seal_no, seal_changed, seal_change_reason,
                             occurred_at, voucher_digest, prev_hash, record_hash)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (batch_id, payload.exhibit_id, chain_seq, seq_in_batch, rec.location,
                         rec.from_person, rec.to_person, rec.seal_no, rec.seal_no != prev_seal,
                         rec.seal_change_reason, rec.occurred_at, rec.voucher_digest, prev_hash, h),
                    )
                    prev_hash = h
                    prev_seal = rec.seal_no

                last = payload.records[-1]
                conn.execute(
                    """
                    UPDATE exhibits
                       SET custodian = %s, current_seal = %s, last_time = %s,
                           chain_length = %s, chain_head_hash = %s
                     WHERE exhibit_id = %s
                    """,
                    (last.to_person, last.seal_no, last.occurred_at, chain_seq,
                     prev_hash, payload.exhibit_id),
                )

            return report
    except UniqueViolation:
        # 并发下同键插入：回滚后按已存在的批次返回（等价于后到的重试）
        with get_conn() as conn:
            existing = conn.execute(
                "SELECT * FROM batches WHERE idempotency_key = %s", (idempotency_key,)
            ).fetchone()
        if existing and existing["request_hash"] == req_hash:
            return _replay_report(existing)
        raise HTTPException(status_code=409, detail="同一 Idempotency-Key 提交了不同的请求载荷")


@app.get("/api/v1/handover-batches/{batch_id}", response_model=BatchReport, tags=["handovers"])
def get_handover_batch(batch_id: uuid.UUID):
    """按批次号查询核验报告。"""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM batches WHERE batch_id = %s", (batch_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"批次「{batch_id}」不存在")
    return row["report"]


# ---------------------------------------------------------------- 审计链


@app.get("/api/v1/exhibits/{exhibit_id}/handovers", response_model=ChainOut, tags=["audit"])
def list_handovers(exhibit_id: str):
    """查询展品的完整交接链（含每条记录的链式哈希）。"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT chain_seq, batch_id, seq_in_batch, location, from_person, to_person,
                   seal_no, seal_changed, seal_change_reason, occurred_at, voucher_digest,
                   prev_hash, record_hash, created_at
              FROM handover_records
             WHERE exhibit_id = %s
             ORDER BY chain_seq
            """,
            (exhibit_id,),
        ).fetchall()
    for r in rows:
        r["batch_id"] = str(r["batch_id"])
    return {"exhibit_id": exhibit_id, "chain_length": len(rows), "records": rows}


@app.get("/api/v1/exhibits/{exhibit_id}/audit/verify", response_model=AuditVerifyOut, tags=["audit"])
def verify_audit_chain(exhibit_id: str):
    """重放哈希链：逐条复算 record_hash 并校验 prev_hash 衔接与链头一致性。"""
    with get_conn() as conn:
        exhibit = conn.execute(
            "SELECT * FROM exhibits WHERE exhibit_id = %s", (exhibit_id,)
        ).fetchone()
        if not exhibit:
            raise HTTPException(status_code=404, detail=f"展品「{exhibit_id}」不存在")
        rows = conn.execute(
            "SELECT * FROM handover_records WHERE exhibit_id = %s ORDER BY chain_seq",
            (exhibit_id,),
        ).fetchall()

    prev = GENESIS_HASH
    for row in rows:
        expected = record_hash(
            prev_hash=prev,
            exhibit_id=row["exhibit_id"],
            chain_seq=row["chain_seq"],
            batch_id=row["batch_id"],
            location=row["location"],
            from_person=row["from_person"],
            to_person=row["to_person"],
            seal_no=row["seal_no"],
            seal_change_reason=row["seal_change_reason"],
            occurred_at=row["occurred_at"],
            voucher_digest=row["voucher_digest"],
        )
        if row["prev_hash"] != prev or row["record_hash"] != expected:
            return {
                "exhibit_id": exhibit_id,
                "chain_length": len(rows),
                "valid": False,
                "first_invalid_seq": row["chain_seq"],
                "detail": f"第 {row['chain_seq']} 环哈希校验失败：记录被篡改、删除或顺序被调换",
            }
        prev = row["record_hash"]

    if prev != exhibit["chain_head_hash"] or len(rows) != exhibit["chain_length"]:
        return {
            "exhibit_id": exhibit_id,
            "chain_length": len(rows),
            "valid": False,
            "first_invalid_seq": None,
            "detail": "链尾哈希或链长与展品当前状态不一致",
        }
    return {
        "exhibit_id": exhibit_id,
        "chain_length": len(rows),
        "valid": True,
        "first_invalid_seq": None,
        "detail": "哈希链完整，与展品当前状态一致",
    }


@app.get("/api/v1/exhibits/{exhibit_id}/batches", response_model=list[BatchSummary], tags=["audit"])
def list_batches(
    exhibit_id: str,
    status: str | None = Query(default=None, pattern="^(ACCEPTED|REJECTED)$"),
):
    """查询展品的批次历史（含被驳回批次，供审计追溯）。"""
    sql = """
        SELECT batch_id, status, report, created_at
          FROM batches
         WHERE exhibit_id = %s
    """
    params: list = [exhibit_id]
    if status:
        sql += " AND status = %s"
        params.append(status)
    sql += " ORDER BY created_at, batch_id"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {
            "batch_id": str(r["batch_id"]),
            "status": r["status"],
            "record_count": r["report"]["record_count"],
            "first_break": r["report"]["first_break"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
