"""请求/响应模型（Pydantic v2）。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class HandoverIn(BaseModel):
    """单条交接记录（按时间排列提交，序号由提交顺序决定）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    location: str = Field(min_length=1, max_length=200, description="交接地点")
    from_person: str = Field(min_length=1, max_length=100, description="交出人")
    to_person: str = Field(min_length=1, max_length=100, description="接收人")
    seal_no: str = Field(min_length=1, max_length=100, description="封签号")
    occurred_at: datetime = Field(description="交接时间（必须带时区）")
    voucher_digest: str = Field(min_length=1, max_length=200, description="凭证摘要，如 sha256:<64hex>")
    seal_change_reason: Optional[str] = Field(default=None, max_length=500, description="换签授权理由（换签时必填）")

    @field_validator("occurred_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("occurred_at 必须携带时区，如 2026-09-01T09:00:00+08:00 或 2026-09-01T01:00:00Z")
        return v


class BatchRequest(BaseModel):
    exhibit_id: str = Field(min_length=1, max_length=64, description="展品编号")
    records: list[HandoverIn] = Field(min_length=1, max_length=1000, description="按时间排列的交接记录")
    state_token: Optional[str] = Field(
        default=None, max_length=1024,
        description="预演签发的链状态令牌（可选）；携带时须属于本展品且链状态未变化",
    )


class ReasonOut(BaseModel):
    rule: str
    message: str


class ItemOut(BaseModel):
    seq: int
    ok: bool
    reasons: list[ReasonOut]


class FirstBreakOut(BaseModel):
    seq: int
    rule: str
    message: str


class BatchReport(BaseModel):
    """核验报告（POST 提交与 GET 查询共用）。"""

    batch_id: str
    exhibit_id: str
    status: Literal["ACCEPTED", "REJECTED"]
    record_count: int
    first_break: Optional[FirstBreakOut]
    items: list[ItemOut]
    idempotent_replay: bool
    created_at: datetime


class PreviewReport(BaseModel):
    """预演核验结果（不落库，无批次号；附链状态令牌供正式提交携带）。"""

    exhibit_id: str
    status: Literal["ACCEPTED", "REJECTED"]
    record_count: int
    first_break: Optional[FirstBreakOut]
    items: list[ItemOut]
    state_token: str
    created_at: datetime


class ExhibitCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    exhibit_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    home_location: str = Field(min_length=1, max_length=200, description="登记馆藏地（首站校验基准）")
    custodian: str = Field(min_length=1, max_length=100, description="登记保管人")
    initial_seal: str = Field(min_length=1, max_length=100, description="初始封签号")


class ExhibitOut(BaseModel):
    exhibit_id: str
    name: str
    home_location: str
    custodian: str
    current_seal: str
    last_time: Optional[datetime]
    chain_length: int
    chain_head_hash: str
    created_at: datetime


class HandoverRecordOut(BaseModel):
    chain_seq: int
    batch_id: str
    seq_in_batch: int
    location: str
    from_person: str
    to_person: str
    seal_no: str
    seal_changed: bool
    seal_change_reason: Optional[str]
    occurred_at: datetime
    voucher_digest: str
    prev_hash: str
    record_hash: str
    created_at: datetime


class ChainOut(BaseModel):
    exhibit_id: str
    chain_length: int
    records: list[HandoverRecordOut]


class AuditVerifyOut(BaseModel):
    exhibit_id: str
    chain_length: int
    valid: bool
    first_invalid_seq: Optional[int]
    detail: str


class BatchSummary(BaseModel):
    batch_id: str
    status: str
    record_count: int
    first_break: Optional[FirstBreakOut]
    created_at: datetime
