"""链状态令牌与请求指纹单元测试（纯标准库，python3 -m unittest 或 pytest 均可运行）。"""
import base64
import json
import unittest

from app.hashing import request_hash
from app.tokens import TokenError, _sign, issue_state_token, parse_state_token


def forge(body_obj: dict, sig: str) -> str:
    body = base64.urlsafe_b64encode(
        json.dumps(body_obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    return f"v1.{body}.{sig}"


class TestStateToken(unittest.TestCase):
    def test_roundtrip(self):
        token = issue_state_token("EX-1", 3, "abc123")
        claims = parse_state_token(token)
        self.assertEqual(claims["exhibit_id"], "EX-1")
        self.assertEqual(claims["chain_length"], 3)
        self.assertEqual(claims["chain_head_hash"], "abc123")

    def test_genesis_state_roundtrip(self):
        token = issue_state_token("EX-2", 0, "GENESIS")
        claims = parse_state_token(token)
        self.assertEqual(claims["chain_length"], 0)
        self.assertEqual(claims["chain_head_hash"], "GENESIS")

    def test_tampered_payload_rejected(self):
        token = issue_state_token("EX-1", 3, "abc123")
        sig = token.split(".")[2]
        forged = forge({"exhibit_id": "EX-2", "chain_length": 3, "chain_head_hash": "abc123"}, sig)
        with self.assertRaises(TokenError):
            parse_state_token(forged)

    def test_tampered_signature_rejected(self):
        token = issue_state_token("EX-1", 3, "abc123")
        parts = token.split(".")
        with self.assertRaises(TokenError):
            parse_state_token(f"{parts[0]}.{parts[1]}.AAAA")

    def test_garbage_rejected(self):
        for bad in ["", "x", "a.b", "a.b.c.d", "v2.x.y", "v1.@@.@@", "v1..", "中文令牌.测试.签名"]:
            with self.assertRaises(TokenError, msg=bad):
                parse_state_token(bad)

    def test_well_signed_but_illegal_content_rejected(self):
        # 签名合法但内容缺字段（如旧版本/伪造来源的令牌结构）
        body = base64.urlsafe_b64encode(json.dumps({"exhibit_id": "EX-1"}).encode()).rstrip(b"=").decode()
        with self.assertRaises(TokenError):
            parse_state_token(f"v1.{body}.{_sign(body)}")


class TestRequestHashTokenIdentity(unittest.TestCase):
    RECORDS = [{"location": "库房A", "seal_no": "SEAL-1"}]

    def test_no_token_matches_legacy_fingerprint(self):
        self.assertEqual(
            request_hash("EX-1", self.RECORDS),
            request_hash("EX-1", self.RECORDS, None),
        )

    def test_token_is_part_of_payload_identity(self):
        h_no_token = request_hash("EX-1", self.RECORDS)
        h_tok_a = request_hash("EX-1", self.RECORDS, "tok-a")
        h_tok_b = request_hash("EX-1", self.RECORDS, "tok-b")
        self.assertNotEqual(h_no_token, h_tok_a)
        self.assertNotEqual(h_tok_a, h_tok_b)
        # 同令牌同记录指纹稳定
        self.assertEqual(h_tok_a, request_hash("EX-1", self.RECORDS, "tok-a"))


if __name__ == "__main__":
    unittest.main()
