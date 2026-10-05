#!/usr/bin/env bash
# 端到端调用示例：登记展品 -> 预演 -> 凭令牌提交 -> 幂等重试 -> 冲突检测 ->
# 提交违规批次（不落记录）-> 令牌失效与重新预演 -> 查询交接链 -> 哈希链审计校验。
# 用法：BASE=http://localhost:8000 ./examples/demo.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
JQ="$(command -v jq || true)"
pp() { if [ -n "$JQ" ]; then jq .; else cat; echo; fi; }
# 从响应中抽取状态令牌（有 jq 用 jq，无 jq 退化为 sed）
extract_token() {
  if [ -n "$JQ" ]; then jq -r '.state_token.token'; else
    sed -n 's/.*"token"[[:space:]]*:[[:space:]]*"\(stv1\.[^"]*\)".*/\1/p' | head -1; fi
}

EXHIBIT_ID="EX-DEMO-$(date +%s)"
KEY_OK="demo-ok-${EXHIBIT_ID}"
KEY_BAD="demo-bad-${EXHIBIT_ID}"
KEY_RH="demo-rh-${EXHIBIT_ID}"

echo "== 0) 健康检查 =="
curl -s "${BASE}/health" | pp

echo; echo "== 1) 登记展品 ${EXHIBIT_ID}（馆藏地/保管人/初始封签为首站校验基准）=="
curl -s -X POST "${BASE}/api/v1/exhibits" \
  -H 'Content-Type: application/json' \
  -d "{\"exhibit_id\":\"${EXHIBIT_ID}\",\"name\":\"宋代青瓷莲花尊\",\"home_location\":\"南京博物院库房B区\",\"custodian\":\"张伟\",\"initial_seal\":\"SEAL-NJ-0001\"}" | pp

echo; echo "== 2) 预演合规批次（只读核验：不落报告、不落记录、不推进链；返回状态令牌）=="
TOKEN="$(curl -s -X POST "${BASE}/api/v1/handover-batches/rehearse" \
  -H 'Content-Type: application/json' \
  -d @- <<JSON | extract_token
{
  "exhibit_id": "${EXHIBIT_ID}",
  "records": [
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
}
JSON
)"
[ -n "${TOKEN}" ] || { echo "未取到预演令牌，终止"; exit 1; }
echo "已取得状态令牌：${TOKEN:0:40}..."

echo; echo "== 2b) 预演未知展品 -> 404 =="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "${BASE}/api/v1/handover-batches/rehearse" \
  -H 'Content-Type: application/json' \
  -d '{"exhibit_id":"EX-NOT-EXIST","records":[{"location":"x","from_person":"a","to_person":"b","seal_no":"S","occurred_at":"2026-09-01T09:00:00+08:00","voucher_digest":"sha256:9f2c4a1b7e3d5f608192a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9"}]}'

echo; echo "== 3) 携带 Handover-Token 正式提交合规批次（链状态未变，正常受理）=="
curl -s -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_OK}" \
  -H "Handover-Token: ${TOKEN}" \
  -d @- <<JSON | pp
{
  "exhibit_id": "${EXHIBIT_ID}",
  "records": [
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
}
JSON

echo; echo "== 4) 同键同载荷（含令牌）重试：链已推进但仍返回原报告，idempotent_replay=true =="
curl -s -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_OK}" \
  -H "Handover-Token: ${TOKEN}" \
  -d @- <<JSON | pp
{
  "exhibit_id": "${EXHIBIT_ID}",
  "records": [
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
}
JSON

echo; echo "== 5) 过期令牌再提交（链已推进）-> 409，需重新预演 =="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_RH}" \
  -H "Handover-Token: ${TOKEN}" \
  -d '{"exhibit_id":"'"${EXHIBIT_ID}"'","records":[{"location":"西安博物院交接区","from_person":"陈晨","to_person":"赵磊","seal_no":"SEAL-SH-1002","occurred_at":"2026-09-05T09:00:00+08:00","voucher_digest":"sha256:aa11bb22cc33dd44ee55ff660011223344556677889900aabbccddeeff00112233"}]}'

echo; echo "== 6) 令牌格式无效 -> 400 参数错误 =="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_RH}" \
  -H 'Handover-Token: not-a-valid-token' \
  -d '{"exhibit_id":"'"${EXHIBIT_ID}"'","records":[{"location":"西安博物院交接区","from_person":"陈晨","to_person":"赵磊","seal_no":"SEAL-SH-1002","occurred_at":"2026-09-05T09:00:00+08:00","voucher_digest":"sha256:aa11bb22cc33dd44ee55ff660011223344556677889900aabbccddeeff00112233"}]}'

echo; echo "== 7) 同幂等键但载荷不同 -> 409（令牌计入载荷身份）=="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_OK}" \
  -d "{\"exhibit_id\":\"${EXHIBIT_ID}\",\"records\":[{\"location\":\"x\",\"from_person\":\"a\",\"to_person\":\"b\",\"seal_no\":\"S\",\"occurred_at\":\"2026-09-05T09:00:00+08:00\",\"voucher_digest\":\"sha256:9f2c4a1b7e3d5f608192a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9\"}]}"

echo; echo "== 8) 提交违规批次（不带令牌，人员断裂+时间倒退+无授权换签+凭证非法）-> REJECTED =="
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

echo; echo "== 9) 查询交接链（应有 3 环：409/400/REJECTED 均未写入任何记录）=="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers" | pp

echo; echo "== 10) 哈希链审计校验 =="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/audit/verify" | pp

echo; echo "== 11) 批次历史（含被驳回批次，供审计追溯；409/400 不产生报告）=="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/batches" | pp
