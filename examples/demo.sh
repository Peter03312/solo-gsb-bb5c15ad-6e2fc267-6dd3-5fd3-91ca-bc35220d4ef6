#!/usr/bin/env bash
# 端到端调用示例：登记展品 -> 预演（不落库，签发链状态令牌）-> 携令牌正式提交 ->
# 幂等重试 -> 冲突检测 -> 旧令牌状态冲突 -> 违规批次（不落记录）->
# 查询交接链 -> 哈希链审计校验。
# 用法：BASE=http://localhost:8000 ./examples/demo.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
JQ="$(command -v jq || true)"
pp() { if [ -n "$JQ" ]; then jq .; else cat; echo; fi; }

EXHIBIT_ID="EX-DEMO-$(date +%s)"
KEY_OK="demo-ok-${EXHIBIT_ID}"
KEY_BAD="demo-bad-${EXHIBIT_ID}"
KEY_STALE="demo-stale-${EXHIBIT_ID}"

# 合规批次的 3 环交接记录（预演 / 正式提交 / 幂等重试共用同一载荷）
RECORDS=$(cat <<JSON
[
  {"location":"南京博物院库房B区","from_person":"张伟","to_person":"李静",
   "seal_no":"SEAL-NJ-0001","occurred_at":"2026-09-01T09:00:00+08:00",
   "voucher_digest":"sha256:9f2c4a1b7e3d5f608192a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9"},
  {"location":"上海博物馆交接区","from_person":"李静","to_person":"王强",
   "seal_no":"SEAL-SH-1002","seal_change_reason":"原封签运输途中磨损，双方馆长书面授权换签（批文号 SH-2026-091）",
   "occurred_at":"2026-09-03T14:30:00+08:00",
   "voucher_digest":"sha256:1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f809"},
  {"location":"上海博物馆临时展厅","from_person":"王强","to_person":"陈晨",
   "seal_no":"SEAL-SH-1002","occurred_at":"2026-09-04T10:00:00+08:00",
   "voucher_digest":"sha256:7f8e9d0c1b2a39485766a5b4c3d2e1f00918273645564738291a0b1c2d3e4f506"}
]
JSON
)

echo "== 0) 健康检查 =="
curl -s "${BASE}/health" | pp

echo; echo "== 1) 登记展品 ${EXHIBIT_ID}（馆藏地/保管人/初始封签为首站校验基准）=="
curl -s -X POST "${BASE}/api/v1/exhibits" \
  -H 'Content-Type: application/json' \
  -d "{\"exhibit_id\":\"${EXHIBIT_ID}\",\"name\":\"宋代青瓷莲花尊\",\"home_location\":\"南京博物院库房B区\",\"custodian\":\"张伟\",\"initial_seal\":\"SEAL-NJ-0001\"}" | pp

echo; echo "== 2) 预演交接批次（逐条核验 + 首个断点 + 链状态令牌；不写入任何数据）=="
PREVIEW=$(curl -s -X POST "${BASE}/api/v1/handover-batches/preview" \
  -H 'Content-Type: application/json' \
  -d "{\"exhibit_id\":\"${EXHIBIT_ID}\",\"records\":${RECORDS}}")
echo "$PREVIEW" | pp
if [ -n "$JQ" ]; then
  TOKEN=$(echo "$PREVIEW" | jq -r .state_token)
else
  TOKEN=$(echo "$PREVIEW" | sed -n 's/.*"state_token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
fi
[ -n "$TOKEN" ] || { echo "未获取到状态令牌，演示终止"; exit 1; }

echo; echo "== 3) 携带状态令牌正式提交（令牌属于本展品且链状态未变化 -> 受理）=="
curl -s -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_OK}" \
  -d "{\"exhibit_id\":\"${EXHIBIT_ID}\",\"state_token\":\"${TOKEN}\",\"records\":${RECORDS}}" | pp

echo; echo "== 4) 相同请求重试（同幂等键同载荷同令牌）-> 同一报告，idempotent_replay=true（即使链已推进）=="
curl -s -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_OK}" \
  -d "{\"exhibit_id\":\"${EXHIBIT_ID}\",\"state_token\":\"${TOKEN}\",\"records\":${RECORDS}}" | pp

echo; echo "== 5) 同幂等键但载荷不同（去掉令牌）-> 409 =="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_OK}" \
  -d "{\"exhibit_id\":\"${EXHIBIT_ID}\",\"records\":${RECORDS}}"

echo; echo "== 6) 链已推进后重用旧令牌（新幂等键）-> 409 状态冲突，须重新预演 =="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_STALE}" \
  -d "{\"exhibit_id\":\"${EXHIBIT_ID}\",\"state_token\":\"${TOKEN}\",\"records\":${RECORDS}}"

echo; echo "== 7) 提交违规批次（不带令牌；人员断裂+时间倒退+无授权换签+凭证非法）-> REJECTED，逐项原因 =="
curl -s -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_BAD}" \
  -d @- <<JSON | pp
{
  "exhibit_id": "${EXHIBIT_ID}",
  "records": [
    {"location":"上海博物馆临时展厅","from_person":"陈晨","to_person":"赵磊",
     "seal_no":"SEAL-SH-1002","occurred_at":"2026-09-05T09:00:00+08:00",
     "voucher_digest":"sha256:aa11bb22cc33dd44ee55ff660011223344556677889900aabbccddeeff00112233"},
    {"location":"西安博物院交接区","from_person":"孙倩","to_person":"周明",
     "seal_no":"SEAL-XA-2001","occurred_at":"2026-09-05T08:30:00+08:00",
     "voucher_digest":"not-a-valid-digest"}
  ]
}
JSON

echo; echo "== 8) 查询交接链（应仍只有 3 环：失败批次未写入任何记录）=="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers" | pp

echo; echo "== 9) 哈希链审计校验 =="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/audit/verify" | pp

echo; echo "== 10) 批次历史（含被驳回批次，供审计追溯）=="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/batches" | pp
