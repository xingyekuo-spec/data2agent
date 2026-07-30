"""M5-T03 隔离详情 API 测试:强制 Bearer auth、raw 脱敏、访问审计、fail-close。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from data2agent.shared.store.landing import LandingStore
from data2agent.platform.console.app import create_app
from data2agent.platform.console.contracts import QuarantineDetail

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "digiwin_e10"
TOKEN = "quarantine-secret"

_NOW = datetime.now().isoformat(timespec="seconds")


def _insert_q(landing: LandingStore, source: str, object: str,
              keys_json: str | None, reason: str, raw_json: str | None = None,
              batch_id: str | None = None) -> int:
    cur = landing.con.execute(
        "INSERT INTO d2a_quarantine (source, object, keys_json, reason, "
        "raw_json, created_at, batch_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (source, object, keys_json, reason, raw_json, _NOW, batch_id))
    landing.con.commit()
    return cur.lastrowid


def _insert_resolved(landing: LandingStore, source: str) -> int:
    cur = landing.con.execute(
        "INSERT INTO d2a_quarantine (source, object, keys_json, reason, "
        "created_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?)",
        (source, "Customer", '{"customer_code":"C999"}',
         "resolved issue", _NOW, _NOW))
    landing.con.commit()
    return cur.lastrowid


@pytest.fixture()
def db_quarantine_detail(tmp_path):
    """独立的 fixture:不依赖 showroom seed,直接插入隔离记录供详情测试。"""
    landing = LandingStore(tmp_path / "landing.sqlite")

    # ---- 1:正常记录(含 raw_json,CONTACT_EMAIL 为敏感列)----
    raw1 = json.dumps({
        "CUSTOMER_CODE": "C001",
        "CUSTOMER_NAME": "测试客户",
        "COUNTRY_REGION": "CN",
        "CONTACT_EMAIL": "test@example.com",  # sensitive → ***
        "PAYMENT_TERM_DAYS": 30,
        "LAST_MODIFIED_DATE": "2026-07-10T12:00:00",
    }, ensure_ascii=False)
    _insert_q(landing, SOURCE, "Customer",
              '{"customer_code":"C001"}', "null key",
              raw_json=raw1, batch_id="batch-1")

    # ---- 2:无 raw_json ----
    _insert_q(landing, SOURCE, "Customer",
              '{"customer_code":"C002"}', "missing field",
              raw_json=None, batch_id="batch-2")

    # ---- 3:含超长字段(用于截断测试)----
    large_raw = json.dumps({
        "CUSTOMER_CODE": "C003",
        "CUSTOMER_NAME": "X" * 70_000,  # 超过 VALUE_BUDGET_BYTES (64 KiB)
    }, ensure_ascii=False)
    _insert_q(landing, SOURCE, "Customer",
              '{"customer_code":"C003"}', "large field",
              raw_json=large_raw, batch_id="batch-3")

    # ---- 4:未知 source/object(模板中不存在)----
    _insert_q(landing, "unknown_source", "UnknownObj",
              '{"ukey":"U001"}', "unknown source error",
              raw_json=json.dumps({"SECRET": "sensitive_data", "COL_A": "ok"}),
              batch_id="batch-4")

    # ---- 5:raw_json 格式错误 ----
    _insert_q(landing, SOURCE, "Customer",
              '{"customer_code":"C004"}', "bad raw json",
              raw_json="not-valid-json", batch_id="batch-5")

    # ---- 6:raw_json 是数组非对象 ----
    _insert_q(landing, SOURCE, "Customer",
              '{"customer_code":"C005"}', "raw is array",
              raw_json='["not","an","object"]', batch_id="batch-6")

    # ---- 7:已处理记录(resolved_at 非空)----
    _insert_resolved(landing, SOURCE)

    return landing


def _client(landing: LandingStore, token: str | None = TOKEN) -> TestClient:
    return TestClient(create_app(
        landing.db_path, str(ROOT / "templates"), token=token))


def _auth(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestQuarantineDetail:
    """GET /api/quarantine/{id} -- 强制 auth、raw 脱敏、审计。"""

    # ================================================================
    # 基本成功
    # ================================================================

    def test_detail_returns_valid_model(self, db_quarantine_detail):
        client = _client(db_quarantine_detail)
        r = client.get("/api/quarantine/1", headers=_auth())
        assert r.status_code == 200
        body = QuarantineDetail.model_validate(r.json())
        assert body.id == 1
        assert body.source == SOURCE
        assert body.object == "Customer"
        assert body.request_id is not None
        assert len(body.request_id) > 0
        assert body.created_at.tzinfo is not None

    def test_request_id_is_valid_uuid4(self, db_quarantine_detail):
        client = _client(db_quarantine_detail)
        body = client.get("/api/quarantine/1", headers=_auth()).json()
        rid = body["request_id"]
        assert isinstance(rid, str)
        assert len(rid) == 36  # UUID4
        assert rid.count("-") == 4

    # ================================================================
    # 强制认证
    # ================================================================

    def test_no_token_configured_returns_403(self, db_quarantine_detail):
        client = _client(db_quarantine_detail, token=None)
        r = client.get("/api/quarantine/1")
        assert r.status_code == 403
        assert "隔离详情" in r.json()["detail"]

    def test_no_token_configured_writes_deny_audit(self, db_quarantine_detail):
        client = _client(db_quarantine_detail, token=None)
        client.get("/api/quarantine/1")
        row = LandingStore(db_quarantine_detail.db_path).con.execute(
            "SELECT * FROM d2a_console_access_audit ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["allowed"] == 0
        assert row["reason_code"] == "token_not_configured"
        assert row["resource_type"] == "quarantine_raw"
        assert row["subject"] == "anonymous"
        assert row["request_id"] is not None

    def test_wrong_token_returns_401(self, db_quarantine_detail):
        client = _client(db_quarantine_detail)
        r = client.get("/api/quarantine/1",
                       headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

    def test_wrong_token_writes_deny_audit(self, db_quarantine_detail):
        client = _client(db_quarantine_detail)
        client.get("/api/quarantine/1",
                   headers={"Authorization": "Bearer wrong"})
        row = LandingStore(db_quarantine_detail.db_path).con.execute(
            "SELECT * FROM d2a_console_access_audit ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["allowed"] == 0
        assert row["reason_code"] == "unauthorized"
        assert row["resource_type"] == "quarantine_raw"

    def test_deny_audit_no_token_leak(self, db_quarantine_detail):
        """拒绝审计不泄露 token、raw 值、SQL。"""
        client = _client(db_quarantine_detail)
        client.get("/api/quarantine/1",
                   headers={"Authorization": "Bearer wrong"})
        row = LandingStore(db_quarantine_detail.db_path).con.execute(
            "SELECT * FROM d2a_console_access_audit ORDER BY id DESC LIMIT 1"
        ).fetchone()
        audit_str = str(dict(row))
        assert TOKEN not in audit_str
        assert "wrong" not in audit_str
        assert "SELECT" not in audit_str

    # ================================================================
    # 404 语义
    # ================================================================

    def test_resolved_record_returns_404(self, db_quarantine_detail):
        client = _client(db_quarantine_detail)
        resolved = LandingStore(db_quarantine_detail.db_path).con.execute(
            "SELECT id FROM d2a_quarantine WHERE resolved_at IS NOT NULL LIMIT 1"
        ).fetchone()
        assert resolved is not None
        r = client.get(f"/api/quarantine/{resolved['id']}", headers=_auth())
        assert r.status_code == 404

    def test_resolved_record_audit_reason_code(self, db_quarantine_detail):
        client = _client(db_quarantine_detail)
        resolved = LandingStore(db_quarantine_detail.db_path).con.execute(
            "SELECT id FROM d2a_quarantine WHERE resolved_at IS NOT NULL LIMIT 1"
        ).fetchone()
        client.get(f"/api/quarantine/{resolved['id']}", headers=_auth())
        row = LandingStore(db_quarantine_detail.db_path).con.execute(
            "SELECT * FROM d2a_console_access_audit ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["allowed"] == 0
        assert row["reason_code"] == "resolved"

    def test_nonexistent_id_returns_404(self, db_quarantine_detail):
        client = _client(db_quarantine_detail)
        r = client.get("/api/quarantine/99999", headers=_auth())
        assert r.status_code == 404

    def test_nonexistent_id_audit_reason_code(self, db_quarantine_detail):
        client = _client(db_quarantine_detail)
        client.get("/api/quarantine/99999", headers=_auth())
        row = LandingStore(db_quarantine_detail.db_path).con.execute(
            "SELECT * FROM d2a_console_access_audit ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["allowed"] == 0
        assert row["reason_code"] == "not_found"

    # ================================================================
    # raw 脱敏
    # ================================================================

    def test_sensitive_fields_masked_in_raw(self, db_quarantine_detail):
        """CONTACT_EMAIL 映射到敏感属性 contact → 应为 ***。"""
        client = _client(db_quarantine_detail)
        body = client.get("/api/quarantine/1", headers=_auth()).json()
        raw = body["raw"]
        assert raw is not None
        assert raw["CONTACT_EMAIL"] == "***"
        # 非敏感列保持原值
        assert raw["CUSTOMER_CODE"] == "C001"
        assert raw["CUSTOMER_NAME"] == "测试客户"
        assert raw["PAYMENT_TERM_DAYS"] == 30

    def test_raw_not_leaked_in_list_endpoint(self, db_quarantine_detail):
        """列表端点不存在 raw/raw_json(安全基准验证)。"""
        client = _client(db_quarantine_detail)
        for row in client.get("/api/quarantine").json():
            assert "raw" not in row
            assert "raw_json" not in row

    # ================================================================
    # truncations
    # ================================================================

    def test_truncations_for_large_values(self, db_quarantine_detail):
        client = _client(db_quarantine_detail)
        large = LandingStore(db_quarantine_detail.db_path).con.execute(
            "SELECT id FROM d2a_quarantine WHERE batch_id = 'batch-3' LIMIT 1"
        ).fetchone()
        assert large is not None
        body = client.get(
            f"/api/quarantine/{large['id']}", headers=_auth()).json()
        assert len(body["truncations"]) > 0
        trunc = body["truncations"][0]
        assert "CUSTOMER_NAME" in trunc["fields"]
        assert trunc["row_index"] == 0

    # ================================================================
    # 未知 source/object
    # ================================================================

    def test_unknown_object_masks_all_raw_values(self, db_quarantine_detail):
        """模板中不存在的对象 → raw 全部 mask 为 ***。"""
        client = _client(db_quarantine_detail)
        unk = LandingStore(db_quarantine_detail.db_path).con.execute(
            "SELECT id FROM d2a_quarantine "
            "WHERE source = 'unknown_source' LIMIT 1"
        ).fetchone()
        assert unk is not None
        body = client.get(
            f"/api/quarantine/{unk['id']}", headers=_auth()).json()
        assert body["raw"] is not None
        for v in body["raw"].values():
            assert v == "***", f"未知对象 raw 值应全 mask, got {v!r}"

    def test_unknown_object_masks_all_key_values(self, db_quarantine_detail):
        client = _client(db_quarantine_detail)
        unk = LandingStore(db_quarantine_detail.db_path).con.execute(
            "SELECT id FROM d2a_quarantine "
            "WHERE source = 'unknown_source' LIMIT 1"
        ).fetchone()
        body = client.get(
            f"/api/quarantine/{unk['id']}", headers=_auth()).json()
        assert body["keys"] is not None
        for v in body["keys"].values():
            assert v == "***", f"未知对象 keys 值应全 mask, got {v!r}"

    # ================================================================
    # 审计
    # ================================================================

    def test_allow_audit_written(self, db_quarantine_detail):
        client = _client(db_quarantine_detail)
        client.get("/api/quarantine/1", headers=_auth())
        row = LandingStore(db_quarantine_detail.db_path).con.execute(
            "SELECT * FROM d2a_console_access_audit "
            "WHERE resource_type = 'quarantine_raw' AND allowed = 1 "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row["subject"] == "console-admin"
        assert row["resource"] == "1"
        assert row["reason_code"] == "ok"
        assert row["returned_rows"] == 1
        assert row["request_id"] is not None
        assert TOKEN not in str(dict(row))

    def test_audit_failure_causes_fail_close(self, db_quarantine_detail, monkeypatch):
        """审计写入失败 → 500,不返回数据,不泄露内部细节。"""
        client = _client(db_quarantine_detail)

        def boom(*args, **kwargs):
            raise RuntimeError("storage-failure-detail")

        monkeypatch.setattr(LandingStore, "log_access", boom)
        r = client.get("/api/quarantine/1", headers=_auth())
        assert r.status_code == 500
        assert "隔离详情" in r.json()["detail"]
        assert "storage-failure-detail" not in r.text

    # ================================================================
    # 不良 raw_json
    # ================================================================

    def test_malformed_raw_json_yields_null_raw_with_warning(
        self, db_quarantine_detail
    ):
        client = _client(db_quarantine_detail)
        bad = LandingStore(db_quarantine_detail.db_path).con.execute(
            "SELECT id FROM d2a_quarantine WHERE batch_id = 'batch-5' LIMIT 1"
        ).fetchone()
        body = client.get(
            f"/api/quarantine/{bad['id']}", headers=_auth()).json()
        assert body["raw"] is None
        assert any(
            "raw_json" in w.lower() for w in body["warnings"])

    def test_array_raw_json_yields_null_raw_with_warning(
        self, db_quarantine_detail
    ):
        client = _client(db_quarantine_detail)
        arr = LandingStore(db_quarantine_detail.db_path).con.execute(
            "SELECT id FROM d2a_quarantine WHERE batch_id = 'batch-6' LIMIT 1"
        ).fetchone()
        body = client.get(
            f"/api/quarantine/{arr['id']}", headers=_auth()).json()
        assert body["raw"] is None
        assert any(
            "不是 JSON 对象" in w for w in body["warnings"])

    def test_null_raw_json_yields_null_raw_no_warning(self, db_quarantine_detail):
        client = _client(db_quarantine_detail)
        no_raw = LandingStore(db_quarantine_detail.db_path).con.execute(
            "SELECT id FROM d2a_quarantine WHERE batch_id = 'batch-2' LIMIT 1"
        ).fetchone()
        body = client.get(
            f"/api/quarantine/{no_raw['id']}", headers=_auth()).json()
        assert body["raw"] is None
        assert body["truncations"] == []
        assert not any(
            "raw_json" in w.lower() for w in body["warnings"])

    # ================================================================
    # 理由脱敏
    # ================================================================

    def test_reason_is_sanitized(self, db_quarantine_detail):
        client = _client(db_quarantine_detail)
        body = client.get("/api/quarantine/1", headers=_auth()).json()
        assert isinstance(body["reason"], str)
        assert len(body["reason"]) > 0
        # safe_error_summary 会压缩空白,单行返回
        assert "\n" not in body["reason"]

    # ================================================================
    # 年龄计算与时间
    # ================================================================

    def test_age_seconds_computed(self, db_quarantine_detail):
        client = _client(db_quarantine_detail)
        body = client.get("/api/quarantine/1", headers=_auth()).json()
        assert isinstance(body["age_seconds"], int)
        assert body["age_seconds"] >= 0

    def test_created_at_is_tz_aware(self, db_quarantine_detail):
        client = _client(db_quarantine_detail)
        body = client.get("/api/quarantine/1", headers=_auth()).json()
        rec = QuarantineDetail.model_validate(body)
        assert rec.created_at.tzinfo is not None

    # ================================================================
    # Issue 1: known object but no matching enabled binding → all mask
    # ================================================================

    def test_known_object_no_matching_binding_masks_all_raw(self, db_quarantine_detail):
        """已知对象但在该源没有已启用 binding → raw 全遮罩为 ***。"""
        # Customer 在 templates 中只有 digiwin_yifei 和 digiwin_e10 的 binding,
        # nonexistent 源无任何 binding
        landing = LandingStore(db_quarantine_detail.db_path)
        raw_bad = json.dumps({"CUSTOMER_CODE": "C999", "SECRET": "leak"}, ensure_ascii=False)
        rid = _insert_q(landing, "nonexistent_source", "Customer",
                        '{"customer_code":"C999"}', "no binding for this source",
                        raw_json=raw_bad)

        client = _client(db_quarantine_detail)
        body = client.get(f"/api/quarantine/{rid}", headers=_auth()).json()
        assert body["raw"] is not None
        for v in body["raw"].values():
            assert v == "***", f"无匹配 binding 源 → raw 应全 mask, got {v!r}"


# ============================================================
# Issue 2 [P1]: keys_json 解析失败不泄露原始值 (detail endpoint)
# ============================================================

class TestKeysJsonSanitizationDetail:
    """详情端点 keys_json 解析失败/非 dict 时 keys_json_out 应为 null。"""

    @pytest.fixture()
    def db_malformed(self, tmp_path):
        landing = LandingStore(tmp_path / "landing.sqlite")
        # 插入含解析失败的 keys_json 记录
        raw_malformed = json.dumps({"CUSTOMER_CODE": "C999"}, ensure_ascii=False)
        rid1 = _insert_q(landing, SOURCE, "Customer",
                        "not-valid-json-at-all@@@SECRET",
                        "bad keys json", raw_json=raw_malformed)
        rid2 = _insert_q(landing, SOURCE, "Customer",
                        '["array","not","object"]',
                        "keys is array", raw_json=raw_malformed)
        return landing, rid1, rid2

    def test_malformed_keys_json_null_in_detail(self, db_malformed):
        """解析失败的 keys_json 在 detail 中 keys_json 为 null。"""
        landing, rid, _ = db_malformed
        client = _client(landing)
        body = client.get(f"/api/quarantine/{rid}", headers=_auth()).json()
        assert body["keys_json"] is None
        assert body["keys"] is None
        assert any("json" in w.lower() for w in body.get("warnings", []))

    def test_array_keys_json_null_in_detail(self, db_malformed):
        """非 dict 的 keys_json 在 detail 中 keys_json 为 null。"""
        landing, _, rid = db_malformed
        client = _client(landing)
        body = client.get(f"/api/quarantine/{rid}", headers=_auth()).json()
        assert body["keys_json"] is None
        assert body["keys"] is None
        assert any("不是 JSON 对象" in w for w in body.get("warnings", []))

    def test_malformed_keys_json_not_leaking_raw_in_detail(self, db_malformed):
        """原始敏感字符绝不出现在 detail 响应中。"""
        landing, rid, _ = db_malformed
        client = _client(landing)
        body = client.get(f"/api/quarantine/{rid}", headers=_auth()).json()
        assert body["keys_json"] is None
        assert body["keys"] is None
        response_str = json.dumps(body, ensure_ascii=False)
        assert "not-valid-json-at-all" not in response_str
        assert "SECRET" not in response_str
