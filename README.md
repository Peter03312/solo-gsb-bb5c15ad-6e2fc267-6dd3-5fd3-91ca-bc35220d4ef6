# 文物借展封签交接核验服务

Python (FastAPI) + PostgreSQL 实现的借展交接核验服务。调用方提交展品编号及按时间排列的交接记录（地点、交出人、接收人、封签号、时间、凭证摘要），服务完成规则核验、幂等写入与哈希链审计。

## 功能特性

- **规则核验**：首站校验、人员衔接、时间严格递增、封签连续性（换签必须带授权理由）、凭证摘要格式
- **预演（dry-run）**：正式提交前可按相同规则预演一批交接，返回逐条核验结果、首个断点与链状态令牌，**不写入任何数据**
- **链状态令牌**：正式提交可携带预演签发的 `state_token`，令牌属于该展品且链状态未变化才受理；格式无效 400，跨展品或链已推进 409（须重新预演）
- **幂等提交**：`Idempotency-Key` 请求头去重，相同请求重试返回同一结果（令牌计入载荷身份）；同键不同载荷返回 409
- **事务原子性**：核验失败的批次**不写入任何交接记录**、不推进链上状态（仅落核验报告供幂等重试与审计追溯）
- **可查询审计链**：每条交接记录携带 `prev_hash` / `record_hash` 构成哈希链，提供链查询与完整性复算接口
- **逐项诊断**：输出核验状态、首个断点（first_break）及每条记录的全部不合格原因

## 快速开始

```bash
docker compose up --build
```

启动后：

- API: http://localhost:8000 （交互式文档 http://localhost:8000/docs ）
- PostgreSQL: localhost:5432 （用户/密码 `app`/`app`，库 `seals`）
- `db/init.sql` 在数据库首次启动时自动执行，建表并写入两件种子展品（`EX-0001`、`EX-0002`）

一键跑通完整演示（登记展品 → 预演 → 携令牌提交 → 幂等重试 → 409 冲突 → 旧令牌状态冲突 → 违规批次 → 链查询 → 审计校验）：

```bash
./examples/demo.sh
```

重置数据：`docker compose down -v`（清空数据卷后重新初始化）。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgresql://app:app@localhost:5432/seals` | PostgreSQL 连接串 |
| `DB_CONNECT_RETRIES` | `30` | 启动等待数据库的重试次数 |
| `DB_CONNECT_DELAY` | `1.0` | 重试间隔（秒） |
| `STATE_TOKEN_SECRET` | `dev-only-state-token-secret` | 链状态令牌的 HMAC 签名密钥，**生产环境务必覆盖** |

## 核验规则

| 规则码 | 说明 |
|---|---|
| `FIRST_STATION_LOCATION` | 展品首条链的第一站地点必须等于登记馆藏地 |
| `FIRST_STATION_CUSTODIAN` | 首站交出人必须是登记保管人 |
| `CUSTODIAN_CHAIN_BREAK` | 交出人必须等于上一环接收人（批次首环则等于当前保管人） |
| `TIME_NOT_INCREASING` | 交接时间必须严格晚于上一环（批次首环则晚于链上最近时间） |
| `SEAL_CHANGED_WITHOUT_AUTH` | 封签号与上一环（或当前封签）不一致时，必须提供换签授权理由 |
| `INVALID_VOUCHER_DIGEST` | 凭证摘要须为 `算法:十六进制`，如 `sha256:<64hex>` |

逐项判定均以「提交的前一条记录」为参照（首条以展品当前链上状态为参照），因此链条中途断裂后，后续各项仍能给出独立可定位的原因。批次任一记录不合格即整体 `REJECTED`。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/api/v1/exhibits` | 登记展品（馆藏地/保管人/初始封签） |
| GET | `/api/v1/exhibits/{id}` | 查询展品当前链上状态 |
| POST | `/api/v1/handover-batches/preview` | **预演交接批次**（相同规则核验 + 签发链状态令牌，不落任何数据） |
| POST | `/api/v1/handover-batches` | **提交交接批次并核验**（需 `Idempotency-Key` 头，可选 `state_token`） |
| GET | `/api/v1/handover-batches/{batch_id}` | 查询批次核验报告 |
| GET | `/api/v1/exhibits/{id}/handovers` | 查询完整交接链（含哈希） |
| GET | `/api/v1/exhibits/{id}/audit/verify` | 重放复算哈希链，校验完整性 |
| GET | `/api/v1/exhibits/{id}/batches` | 批次历史（含被驳回批次，可按 `status` 过滤） |

## 调用示例

### 1. 登记展品

```bash
curl -X POST http://localhost:8000/api/v1/exhibits \
  -H 'Content-Type: application/json' \
  -d '{"exhibit_id":"EX-1001","name":"宋代青瓷莲花尊","home_location":"南京博物院库房B区","custodian":"张伟","initial_seal":"SEAL-NJ-0001"}'
```

### 2. 预演交接批次（可选，不落库）

正式提交前，可用相同载荷预演：按相同规则逐条核验，返回核验结果、首个断点与链状态令牌，**不写入批次报告、交接记录或展品状态**。预演未知展品返回 404，记录格式校验规则与正式提交一致。

```bash
curl -X POST http://localhost:8000/api/v1/handover-batches/preview \
  -H 'Content-Type: application/json' \
  -d @examples/accept.json
```

响应（无需幂等键；`state_token` 绑定「该展品 + 当前链状态」）：

```json
{
  "exhibit_id": "EX-0001",
  "status": "ACCEPTED",
  "record_count": 3,
  "first_break": null,
  "items": [
    {"seq": 1, "ok": true, "reasons": []},
    {"seq": 2, "ok": true, "reasons": []},
    {"seq": 3, "ok": true, "reasons": []}
  ],
  "state_token": "v1.eyJjaGFpbl9oZWFkX2hhc2giOiJHRU5FU0lTIiwiY2hhaW5fbGVuZ3RoIjowLCJleGhpYml0X2lkIjoiRVgtMDAwMSJ9.k0vQ…",
  "created_at": "2026-10-05T08:00:00+00:00"
}
```

### 3. 提交交接批次（合规，含一次授权换签）

```bash
curl -X POST http://localhost:8000/api/v1/handover-batches \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: 7b8f9c2e-2026-0001' \
  -d @examples/accept.json
```

正式提交可在请求体中携带预演返回的 `state_token`（可选，不带令牌的提交继续可用）：

```json
{"exhibit_id": "EX-0001", "state_token": "v1.eyJ…", "records": [ … ]}
```

令牌校验规则（失败时**不生成批次报告**，调用方须重新预演）：

| 情形 | 状态码 | 说明 |
|---|---|---|
| 令牌格式/签名非法 | 400 | 参数错误 |
| 令牌属于其他展品 | 409 | 状态冲突 |
| 链状态已推进（期间有其他批次写入） | 409 | 状态冲突，须重新预演获取新令牌 |
| 令牌属于本展品且链状态未变化 | — | 按现有规则正常核验处理 |

响应（`status=ACCEPTED`，批次与交接记录已同事务写入）：

```json
{
  "batch_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "exhibit_id": "EX-0001",
  "status": "ACCEPTED",
  "record_count": 3,
  "first_break": null,
  "items": [
    {"seq": 1, "ok": true, "reasons": []},
    {"seq": 2, "ok": true, "reasons": []},
    {"seq": 3, "ok": true, "reasons": []}
  ],
  "idempotent_replay": false,
  "created_at": "2026-10-05T08:00:00+00:00"
}
```

### 4. 幂等重试与冲突

- **相同请求重试**（同 `Idempotency-Key`、同载荷）：返回与首次完全一致的报告，`idempotent_replay=true`，不重复写入；**即使链已推进也返回原报告**（重放不再校验令牌）。
- **同键不同载荷**：返回 `409`，说明幂等键被复用。`state_token` 计入载荷身份——同键同记录但令牌不同（或一带一不带）同样视为不同载荷。

### 5. 提交违规批次

```bash
curl -X POST http://localhost:8000/api/v1/handover-batches \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: 7b8f9c2e-2026-0002' \
  -d @examples/reject.json
```

响应（`status=REJECTED`，给出首个断点与逐项原因；**未写入任何交接记录**）：

```json
{
  "status": "REJECTED",
  "first_break": {"seq": 2, "rule": "CUSTODIAN_CHAIN_BREAK", "message": "交出人「孙倩」与上一站接收人「赵磊」不一致，人员衔接断裂"},
  "items": [
    {"seq": 1, "ok": true, "reasons": []},
    {"seq": 2, "ok": false, "reasons": [
      {"rule": "CUSTODIAN_CHAIN_BREAK", "message": "交出人「孙倩」与上一站接收人「赵磊」不一致，人员衔接断裂"},
      {"rule": "TIME_NOT_INCREASING", "message": "交接时间 ... 未严格晚于上一环节 ..."},
      {"rule": "SEAL_CHANGED_WITHOUT_AUTH", "message": "封签号由「SEAL-BJ-0002」变更为「SEAL-XA-2001」，但未提供换签授权理由"},
      {"rule": "INVALID_VOUCHER_DIGEST", "message": "凭证摘要格式非法，应为「算法:十六进制摘要」，如 sha256:<64位hex>"}
    ]}
  ]
}
```

### 6. 查询审计链并校验完整性

```bash
curl http://localhost:8000/api/v1/exhibits/EX-0001/handovers
curl http://localhost:8000/api/v1/exhibits/EX-0001/audit/verify
```

`audit/verify` 逐条复算 `record_hash = sha256(prev_hash ‖ 规范化业务字段)` 并比对链头，任何篡改、删除、乱序都会定位到首个失效环：

```json
{"exhibit_id":"EX-0001","chain_length":3,"valid":true,"first_invalid_seq":null,"detail":"哈希链完整，与展品当前状态一致"}
```

## 设计说明

- **幂等**：`batches.idempotency_key` 唯一约束 + `request_hash`（请求体规范化 JSON 的 SHA-256，`state_token` 计入载荷身份；不带令牌时指纹与旧版一致）。同键同载荷直接返回已存报告；同键不同载荷 409；并发同键插入由唯一约束兜底，后到者按重试返回。
- **预演与状态令牌**：预演端点只读核验并签发 `v1.<base64url(payload)>.<base64url(HMAC-SHA256)>` 令牌，载荷为 `{exhibit_id, chain_length, chain_head_hash}` 链状态快照。正式提交在幂等预检之后、落库之前校验令牌：签名/格式非法 400，跨展品 409，快照与当前链状态不符 409——校验失败事务回滚，不生成批次报告。由于令牌校验位于幂等预检之后，同键成功重试即使链已推进也返回原报告。
- **原子性**：核验、批次报告、交接记录、展品链上状态推进全部在单事务内完成；`REJECTED` 批次仅落 `batches` 报告行（用于幂等重试返回同一结果与审计追溯），`handover_records` 与 `exhibits` 状态零变更。
- **并发一致性**：处理批次时对展品行 `SELECT ... FOR UPDATE`，串行化同一展品的链推进，防止并发批次造成链序号/链头错乱；令牌快照的比较也在锁内进行，与链推进串行化。
- **哈希链**：首环 `prev_hash = GENESIS`，之后每环指向前环 `record_hash`；时间统一规范化为 UTC 微秒精度 `Z` 格式后参与哈希，保证写入与复算一致。

## 本地开发与测试

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest tests/                 # 规则引擎与状态令牌单元测试（无数据库依赖）
uvicorn app.main:app --reload # 需本地 PostgreSQL 并执行 db/init.sql
```

## 目录结构

```
app/
  main.py        # FastAPI 路由与事务编排（含预演端点与令牌校验）
  validation.py  # 规则引擎（纯标准库，可独立单测）
  hashing.py     # 规范化序列化、请求指纹、哈希链
  tokens.py      # 链状态令牌（HMAC 签发/验签，纯标准库）
  schemas.py     # 请求/响应模型
  db.py          # 连接与启动等待
db/init.sql      # 建表 + 种子展品（Compose 首启自动执行）
examples/        # accept/reject 示例载荷与端到端 demo.sh（含预演演示）
tests/           # 规则引擎与状态令牌单元测试
Dockerfile  docker-compose.yml  requirements*.txt
```
