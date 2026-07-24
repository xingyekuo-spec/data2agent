"""M3-T05/T08: mapping preview Console API — Bearer、审计、脱敏、写屏障、零副作用。"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.connect.dataset_publish import build_dataset
from data2agent.connect.increment import incremental_sync
from tests.helpers import watermarks_from_pack
from data2agent.connect.landing import LandingStore
from data2agent.connect.mapping_preview import MASKED, PreviewError
from tests.helpers import whitelist_from_pack
from data2agent.console.app import create_app
from data2agent.console.contracts import MappingPreviewError, MappingPreviewResponse
from data2agent.metamodel.loader import load_pack
from tests.fixtures.e10.seed import build, write_db

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"
TOKEN = "preview-secret"
PREVIEW_URL = "/api/mappings/Customer/preview"
DRAFT_MARKER = "T08-DRAFT-BODY-MUST-NOT-LEAK"

PROTECTED_TABLES = (
    "d2a_sync_state",
    "d2a_quarantine",
    "d2a_sync_run",
    "d2a_run_step",
    "d2a_dataset_version",
    "d2a_object_version",
)


@pytest.fixture()
def env(tmp_path):
    src = tmp_path / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    pack = load_pack(ROOT / "templates")
    landing = LandingStore(tmp_path / "landing.sqlite")
    adapter = SqliteReadOnlyAdapter(str(src), whitelist_from_pack(pack, SOURCE))
    incremental_sync(adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))
    result = build_dataset(landing, pack, SOURCE, auto_publish=True)
    assert result.published
    return landing


def _client(landing: LandingStore, token: str | None = TOKEN) -> TestClient:
    return TestClient(create_app(landing.db_path, ROOT / "templates", token=token))


def _auth() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def _body(**overrides) -> dict:
    body = {
        "source": SOURCE,
        "sample": {"limit": 20, "offset": 0},
        "draft_binding": None,
    }
    body.update(overrides)
    return body


def _plain(value):
    """Unwrap JsonValue/RootModel wrappers for assertion comparisons."""
    return getattr(value, "root", value)


def _assert_preview_error(resp, *, status: int, reason_code: str) -> MappingPreviewError:
    assert resp.status_code == status, resp.text
    err = MappingPreviewError.model_validate(resp.json())
    assert err.status == status
    assert err.reason_code == reason_code
    if status == 500:
        assert err.error_id
    else:
        assert err.error_id is None
    # 不得泄漏异常原文 / SQL / traceback
    blob = resp.text.lower()
    assert "traceback" not in blob
    assert "select " not in blob
    assert "runtimeerror" not in blob
    return err


def _table_fingerprint(con, table: str) -> str:
    cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")').fetchall()]
    schema = json.dumps(cols, ensure_ascii=False)
    if not cols:
        return hashlib.sha256(schema.encode()).hexdigest()
    col_sql = ", ".join(f'"{c}"' for c in cols)
    rows = con.execute(
        f'SELECT {col_sql} FROM "{table}" ORDER BY rowid').fetchall()
    payload = schema + "|" + json.dumps(
        [list(r) for r in rows], ensure_ascii=False, default=str, sort_keys=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _protected_snapshot(db_path: str | Path) -> dict[str, str]:
    store = LandingStore(db_path)
    names = [
        r[0] for r in store.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND (name LIKE 'raw_%' OR name LIKE 'obj_%' OR name LIKE 'objv_%' "
            "OR name IN ("
            + ",".join(f"'{t}'" for t in PROTECTED_TABLES)
            + "))"
            " ORDER BY name"
        ).fetchall()
    ]
    return {name: _table_fingerprint(store.con, name) for name in names}


def _published_pointer(db_path: str | Path) -> tuple[str | None, str | None]:
    pub = LandingStore(db_path).get_published_dataset(SOURCE)
    if pub is None:
        return None, None
    return pub.dataset_version, pub.previous_dataset_version


def _business_write_counts(db_path: str | Path) -> dict[str, int]:
    store = LandingStore(db_path)
    counts = {
        "sync_run": store.con.execute("SELECT COUNT(*) FROM d2a_sync_run").fetchone()[0],
        "run_step": store.con.execute("SELECT COUNT(*) FROM d2a_run_step").fetchone()[0],
        "quarantine": store.con.execute("SELECT COUNT(*) FROM d2a_quarantine").fetchone()[0],
        "dataset_version": store.con.execute(
            "SELECT COUNT(*) FROM d2a_dataset_version").fetchone()[0],
        "object_version": store.con.execute(
            "SELECT COUNT(*) FROM d2a_object_version").fetchone()[0],
    }
    tables = [
        r[0] for r in store.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    ]
    counts["obj_tables"] = sum(1 for n in tables if n.startswith("obj_") and not n.startswith("objv_"))
    counts["temp_persistent"] = sum(
        1 for n in tables
        if n.startswith("tmp_") or n.startswith("temp_") or n.startswith("preview_")
    )
    return counts


def _sensitive_raw_values(db_path: str | Path) -> list[str]:
    rows = LandingStore(db_path).con.execute(
        'SELECT CONTACT_EMAIL, CONTACT_PHONE, CONTACT_NAME '
        'FROM "raw_digiwin_e10__CUSTOMER"'
    ).fetchall()
    out: list[str] = []
    for email, phone, name in rows:
        for v in (email, phone, name):
            if v:
                out.append(str(v))
    return out


def _assert_no_secrets_in_blob(blob: str, secrets: list[str], *, draft_notes: str | None = None) -> None:
    lower = blob.lower()
    assert TOKEN not in blob
    assert "traceback" not in lower
    assert "select " not in lower
    assert "raw_digiwin_e10__" not in lower
    assert "runtimeerror" not in lower
    for secret in secrets:
        assert secret not in blob
    if draft_notes is not None:
        assert draft_notes not in blob
        assert DRAFT_MARKER not in blob


def _customer_draft(**overrides) -> dict:
    draft = {
        "tables": ["CUSTOMER", "CURRENCY"],
        "key_map": {"customer_code": "CUSTOMER.CUSTOMER_CODE"},
        "field_map": {
            "customer_code": "CUSTOMER.CUSTOMER_CODE",
            "name": "CUSTOMER.CUSTOMER_NAME",
            "region": "CUSTOMER.COUNTRY_REGION",
            "currency": "CURRENCY.CURRENCY_CODE (join CUSTOMER.CURRENCY_ID)",
            "payment_days": "CUSTOMER.PAYMENT_TERM_DAYS",
            "contact": "CUSTOMER.CONTACT_EMAIL",
        },
        "derived": {},
        "watermark": "CUSTOMER.LAST_MODIFIED_DATE",
        "notes": DRAFT_MARKER,
    }
    draft.update(overrides)
    return draft


# ---- 鉴权契约 ----


def test_preview_requires_configured_token(env):
    client = _client(env, token=None)
    r = client.post(PREVIEW_URL, json=_body())
    _assert_preview_error(r, status=403, reason_code="token_not_configured")
    row = LandingStore(env.db_path).con.execute(
        "SELECT * FROM d2a_console_access_audit ORDER BY id DESC LIMIT 1").fetchone()
    assert row["allowed"] == 0 and row["reason_code"] == "token_not_configured"
    assert row["subject"] == "anonymous"
    assert str(row["resource"]).startswith("mapping_preview:Customer:")
    assert TOKEN not in str(dict(row))


def test_preview_rejects_missing_and_wrong_bearer(env):
    client = _client(env)
    missing = client.post(PREVIEW_URL, json=_body())
    _assert_preview_error(missing, status=401, reason_code="unauthorized")
    wrong = client.post(
        PREVIEW_URL, json=_body(),
        headers={"Authorization": "Bearer wrong"})
    _assert_preview_error(wrong, status=401, reason_code="unauthorized")
    # 裸 token / 非 Bearer scheme 不得旁路
    bare = client.post(
        PREVIEW_URL, json=_body(),
        headers={"Authorization": TOKEN})
    _assert_preview_error(bare, status=401, reason_code="unauthorized")
    other_scheme = client.post(
        PREVIEW_URL, json=_body(),
        headers={"Authorization": f"Token {TOKEN}"})
    _assert_preview_error(other_scheme, status=401, reason_code="unauthorized")
    # query token 不得旁路 Bearer-only
    query = client.post(f"{PREVIEW_URL}?token={TOKEN}", json=_body())
    _assert_preview_error(query, status=401, reason_code="unauthorized")
    row = LandingStore(env.db_path).con.execute(
        "SELECT * FROM d2a_console_access_audit ORDER BY id DESC LIMIT 1").fetchone()
    assert row["allowed"] == 0 and row["reason_code"] == "unauthorized"
    assert TOKEN not in str(dict(row))


def test_preview_good_bearer_writes_access_audit(env):
    client = _client(env)
    r = client.post(PREVIEW_URL, json=_body(), headers=_auth())
    assert r.status_code == 200, r.text
    body = MappingPreviewResponse.model_validate(r.json())
    row = LandingStore(env.db_path).con.execute(
        "SELECT * FROM d2a_console_access_audit WHERE allowed = 1 "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert row["reason_code"] == "preview_allowed"
    assert row["subject"] == "console-admin"
    assert row["resource"] == "mapping_preview:Customer:CUSTOMER"
    assert row["returned_rows"] == body.sample.sampled_rows
    assert row["page_offset"] == body.sample.offset
    assert row["page_limit"] == body.sample.limit
    assert row["request_id"]
    audit_blob = str(dict(row))
    _assert_no_secrets_in_blob(audit_blob, _sensitive_raw_values(env.db_path))
    assert "draft_binding" not in audit_blob
    assert "field_map" not in audit_blob


# ---- 成功路径 ----


def test_current_binding_preview_200(env):
    client = _client(env)
    r = client.post(PREVIEW_URL, json=_body(), headers=_auth())
    assert r.status_code == 200, r.text
    body = MappingPreviewResponse.model_validate(r.json())
    assert body.object == "Customer"
    assert body.source == SOURCE
    assert body.mode == "current"
    assert body.current is not None
    assert body.candidate.summary.total == body.sample.sampled_rows
    assert body.sample.anchor_table == "CUSTOMER"
    assert body.diff.state == "available"
    row = LandingStore(env.db_path).con.execute(
        "SELECT * FROM d2a_console_access_audit WHERE allowed = 1 "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert row["reason_code"] == "preview_allowed"
    assert row["subject"] == "console-admin"
    assert row["resource"] == "mapping_preview:Customer:CUSTOMER"
    assert row["returned_rows"] == body.sample.sampled_rows
    assert row["page_offset"] == body.sample.offset
    assert row["page_limit"] == body.sample.limit
    assert row["request_id"]
    audit_blob = str(dict(row))
    assert TOKEN not in audit_blob
    assert "draft_binding" not in audit_blob
    assert "SELECT" not in audit_blob
    assert "field_map" not in audit_blob


def test_draft_preview_with_diff(env):
    # 草稿去掉 contact 映射,制造与 current 的可观察差异
    draft = {
        "tables": ["CUSTOMER", "CURRENCY"],
        "key_map": {"customer_code": "CUSTOMER.CUSTOMER_CODE"},
        "field_map": {
            "customer_code": "CUSTOMER.CUSTOMER_CODE",
            "name": "CUSTOMER.CUSTOMER_NAME",
            "region": "CUSTOMER.COUNTRY_REGION",
            "currency": "CURRENCY.CURRENCY_CODE (join CUSTOMER.CURRENCY_ID)",
            "payment_days": "CUSTOMER.PAYMENT_TERM_DAYS",
        },
        "derived": {},
        "watermark": "CUSTOMER.LAST_MODIFIED_DATE",
        "notes": "preview draft",
    }
    r = _client(env).post(
        PREVIEW_URL, json=_body(draft_binding=draft), headers=_auth())
    assert r.status_code == 200, r.text
    body = MappingPreviewResponse.model_validate(r.json())
    assert body.mode == "draft"
    assert body.current is not None
    assert body.diff.state == "available"
    assert body.current_binding_hash != body.candidate_binding_hash
    assert body.diff.summary.rows_changed >= 0


def test_no_current_plus_draft_unavailable_diff(env, monkeypatch):
    monkeypatch.setattr(
        "data2agent.connect.mapping_preview._current_binding",
        lambda *args, **kwargs: None,
    )
    draft = {
        "tables": ["CUSTOMER"],
        "key_map": {"customer_code": "CUSTOMER.CUSTOMER_CODE"},
        "field_map": {
            "customer_code": "CUSTOMER.CUSTOMER_CODE",
            "name": "CUSTOMER.CUSTOMER_NAME",
        },
        "derived": {},
        "watermark": None,
        "notes": "orphan draft",
    }
    r = _client(env).post(
        PREVIEW_URL, json=_body(draft_binding=draft), headers=_auth())
    assert r.status_code == 200, r.text
    body = MappingPreviewResponse.model_validate(r.json())
    assert body.mode == "draft"
    assert body.current is None
    assert body.current_binding_hash is None
    assert body.diff.state == "unavailable"
    assert body.diff.reason == "no_current_binding"


def test_empty_table_returns_200_zeros(env):
    store = LandingStore(env.db_path)
    store.con.execute('DELETE FROM "raw_digiwin_e10__CUSTOMER"')
    store.con.commit()
    r = _client(env).post(PREVIEW_URL, json=_body(), headers=_auth())
    assert r.status_code == 200, r.text
    body = MappingPreviewResponse.model_validate(r.json())
    assert body.sample.sampled_rows == 0
    assert body.candidate.summary.total == 0
    assert body.candidate.summary.mapped == 0
    assert body.candidate.summary.quarantined == 0


# ---- 错误契约 ----


def test_object_not_found_404(env):
    r = _client(env).post(
        "/api/mappings/NoSuchObject/preview", json=_body(), headers=_auth())
    _assert_preview_error(r, status=404, reason_code="object_not_found")


def test_source_not_found_404(env):
    r = _client(env).post(
        PREVIEW_URL, json=_body(source="no_such_source"), headers=_auth())
    _assert_preview_error(r, status=404, reason_code="source_not_found")


def test_sample_batch_not_found_404(env):
    r = _client(env).post(
        PREVIEW_URL,
        json=_body(sample={"limit": 10, "offset": 0, "batch_id": "missing-batch"}),
        headers=_auth(),
    )
    _assert_preview_error(r, status=404, reason_code="sample_batch_not_found")


def test_raw_table_not_found_404(env):
    r = _client(env).post(
        "/api/mappings/Customer/preview",
        json=_body(source="digiwin_yifei"),
        headers=_auth(),
    )
    # yifei binding 存在但参考库未落地 COPMA
    _assert_preview_error(r, status=404, reason_code="raw_table_not_found")


def test_current_binding_unavailable_409(env, monkeypatch):
    monkeypatch.setattr(
        "data2agent.connect.mapping_preview._current_binding",
        lambda *args, **kwargs: None,
    )
    r = _client(env).post(PREVIEW_URL, json=_body(), headers=_auth())
    _assert_preview_error(r, status=409, reason_code="current_binding_unavailable")


def test_raw_unavailable_409(env, monkeypatch):
    def boom(*args, **kwargs):
        raise PreviewError("raw_unavailable", "landing locked")

    monkeypatch.setattr(
        "data2agent.console.app.preview_mapping", boom)
    r = _client(env).post(PREVIEW_URL, json=_body(), headers=_auth())
    _assert_preview_error(r, status=409, reason_code="raw_unavailable")
    assert "landing locked" not in r.text


def test_draft_invalid_422(env):
    draft = {
        "tables": ["NOT_IN_WHITELIST"],
        "key_map": {"customer_code": "NOT_IN_WHITELIST.CODE"},
        "field_map": {"customer_code": "NOT_IN_WHITELIST.CODE"},
        "derived": {},
        "watermark": None,
        "notes": "",
    }
    r = _client(env).post(
        PREVIEW_URL, json=_body(draft_binding=draft), headers=_auth())
    _assert_preview_error(r, status=422, reason_code="draft_invalid")


def test_preview_failed_500(env, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("secret sql SELECT * FROM raw_x")

    monkeypatch.setattr(
        "data2agent.console.app.preview_mapping", boom)
    r = _client(env).post(PREVIEW_URL, json=_body(), headers=_auth())
    err = _assert_preview_error(r, status=500, reason_code="preview_failed")
    assert "secret" not in r.text
    assert "SELECT" not in r.text
    assert err.error_id
    row = LandingStore(env.db_path).con.execute(
        "SELECT * FROM d2a_console_access_audit ORDER BY id DESC LIMIT 1").fetchone()
    assert row["allowed"] == 0 and row["reason_code"] == "preview_failed"
    assert "secret" not in str(dict(row))


# ---- T08: 脱敏 / 泄漏屏障 ----


def test_sensitive_contact_masked_on_output_issues_and_diff(env):
    secrets = _sensitive_raw_values(env.db_path)
    assert secrets, "fixture must contain sensitive contact values"
    client = _client(env)

    current = client.post(PREVIEW_URL, json=_body(), headers=_auth())
    assert current.status_code == 200, current.text
    cur_body = MappingPreviewResponse.model_validate(current.json())
    for side in (cur_body.current, cur_body.candidate):
        assert side is not None
        for row in side.rows:
            if row.status == "mapped":
                assert _plain(row.output.get("contact")) == MASKED
            for issue in row.issues:
                if issue.field == "contact" and issue.source_value is not None:
                    assert _plain(issue.source_value) == MASKED
                for secret in secrets:
                    assert secret not in (issue.detail or "")
                    assert secret not in (issue.source_value or "")

    # 草稿改写 contact 表达式仍不能降敏;并制造 diff
    draft = _customer_draft(field_map={
        "customer_code": "CUSTOMER.CUSTOMER_CODE",
        "name": "CUSTOMER.CUSTOMER_NAME",
        "region": "CUSTOMER.COUNTRY_REGION",
        "currency": "CURRENCY.CURRENCY_CODE (join CUSTOMER.CURRENCY_ID)",
        "payment_days": "CUSTOMER.PAYMENT_TERM_DAYS",
        "contact": "CUSTOMER.CONTACT_PHONE",
    })
    draft_resp = client.post(
        PREVIEW_URL, json=_body(draft_binding=draft), headers=_auth())
    assert draft_resp.status_code == 200, draft_resp.text
    draft_body = MappingPreviewResponse.model_validate(draft_resp.json())
    assert draft_body.mode == "draft"
    for row in draft_body.candidate.rows:
        if row.status == "mapped":
            assert _plain(row.output.get("contact")) == MASKED
    for drow in draft_body.diff.rows:
        for field in drow.fields:
            if field.field == "contact":
                if field.before is not None:
                    assert _plain(field.before) == MASKED
                if field.after is not None:
                    assert _plain(field.after) == MASKED
    _assert_no_secrets_in_blob(
        draft_resp.text, secrets, draft_notes=DRAFT_MARKER)
    # 未分类 raw 列不得出现在输出
    assert "CONTACT_EMAIL" not in draft_resp.text
    assert "CONTACT_PHONE" not in draft_resp.text
    assert "CONTACT_NAME" not in draft_resp.text
    audit_rows = LandingStore(env.db_path).con.execute(
        "SELECT * FROM d2a_console_access_audit ORDER BY id DESC LIMIT 5").fetchall()
    for row in audit_rows:
        _assert_no_secrets_in_blob(
            str(dict(row)), secrets, draft_notes=DRAFT_MARKER)


def test_draft_cannot_cross_source_tables(env):
    # digiwin_e10 草稿引用 yifei 锚表 → 白名单拒绝
    draft = {
        "tables": ["COPMA"],
        "key_map": {"customer_code": "COPMA.CUSTOMER_CODE"},
        "field_map": {"customer_code": "COPMA.CUSTOMER_CODE"},
        "derived": {},
        "watermark": None,
        "notes": DRAFT_MARKER,
    }
    r = _client(env).post(
        PREVIEW_URL, json=_body(draft_binding=draft), headers=_auth())
    err = _assert_preview_error(r, status=422, reason_code="draft_invalid")
    assert err.detail == "草稿不合法"
    assert DRAFT_MARKER not in r.text
    assert "raw_" not in r.text.lower()
    assert "traceback" not in r.text.lower()


def test_draft_remap_sensitive_email_onto_name_is_masked(env):
    secrets = _sensitive_raw_values(env.db_path)
    assert secrets, "fixture must contain sensitive contact values"
    draft = _customer_draft(field_map={
        "customer_code": "CUSTOMER.CUSTOMER_CODE",
        "name": "CUSTOMER.CONTACT_EMAIL",
        "region": "CUSTOMER.COUNTRY_REGION",
        "currency": "CURRENCY.CURRENCY_CODE (join CUSTOMER.CURRENCY_ID)",
        "payment_days": "CUSTOMER.PAYMENT_TERM_DAYS",
        "contact": "CUSTOMER.CONTACT_EMAIL",
    })
    r = _client(env).post(
        PREVIEW_URL, json=_body(draft_binding=draft), headers=_auth())
    assert r.status_code == 200, r.text
    body = MappingPreviewResponse.model_validate(r.json())
    for row in body.candidate.rows:
        if row.status == "mapped":
            assert _plain(row.output.get("name")) == MASKED
            assert _plain(row.output.get("contact")) == MASKED
    _assert_no_secrets_in_blob(r.text, secrets, draft_notes=DRAFT_MARKER)
    assert "CONTACT_EMAIL" not in r.text


def test_draft_remap_unknown_phone_onto_name_is_masked(env):
    secrets = _sensitive_raw_values(env.db_path)
    phones = [
        str(v) for v in LandingStore(env.db_path).con.execute(
            'SELECT CONTACT_PHONE FROM "raw_digiwin_e10__CUSTOMER" '
            "WHERE CONTACT_PHONE IS NOT NULL"
        ).fetchall()
        for v in v
        if v
    ]
    assert phones, "fixture must contain CONTACT_PHONE values"
    draft = _customer_draft(field_map={
        "customer_code": "CUSTOMER.CUSTOMER_CODE",
        "name": "CUSTOMER.CONTACT_PHONE",
        "region": "CUSTOMER.COUNTRY_REGION",
        "currency": "CURRENCY.CURRENCY_CODE (join CUSTOMER.CURRENCY_ID)",
        "payment_days": "CUSTOMER.PAYMENT_TERM_DAYS",
        "contact": "CUSTOMER.CONTACT_EMAIL",
    })
    r = _client(env).post(
        PREVIEW_URL, json=_body(draft_binding=draft), headers=_auth())
    assert r.status_code == 200, r.text
    body = MappingPreviewResponse.model_validate(r.json())
    for row in body.candidate.rows:
        if row.status == "mapped":
            assert _plain(row.output.get("name")) == MASKED
    for phone in phones:
        assert phone not in r.text
    _assert_no_secrets_in_blob(r.text, secrets, draft_notes=DRAFT_MARKER)
    assert "CONTACT_PHONE" not in r.text


def test_draft_derived_on_sensitive_col_rejected_by_api(env):
    """草稿基于敏感列构造 derived 条件 → 422 draft_invalid(关闭命中数猜测通道)。"""
    draft = _customer_draft(
        field_map={
            "customer_code": "CUSTOMER.CUSTOMER_CODE",
            "name": "CUSTOMER.CUSTOMER_NAME",
            "region": "CUSTOMER.COUNTRY_REGION",
            "currency": "CURRENCY.CURRENCY_CODE (join CUSTOMER.CURRENCY_ID)",
            "payment_days": "CUSTOMER.PAYMENT_TERM_DAYS",
            "contact": "CUSTOMER.CONTACT_EMAIL",
        },
        derived={
            "name": {
                "rules": [{"when": {"CONTACT_EMAIL": "__no_match__"}, "value": "x"}],
                "default": None,
            },
        },
    )
    r = _client(env).post(
        PREVIEW_URL, json=_body(draft_binding=draft), headers=_auth())
    err = _assert_preview_error(r, status=422, reason_code="draft_invalid")
    assert err.detail == "草稿不合法"
    assert "CONTACT_EMAIL" not in r.text
    assert DRAFT_MARKER not in r.text


def test_draft_unknown_column_is_draft_invalid_by_api(env):
    draft = _customer_draft(field_map={
        "customer_code": "CUSTOMER.CUSTOMER_CODE",
        "name": "CUSTOMER.NO_SUCH_COLUMN",
        "region": "CUSTOMER.COUNTRY_REGION",
        "currency": "CURRENCY.CURRENCY_CODE (join CUSTOMER.CURRENCY_ID)",
        "payment_days": "CUSTOMER.PAYMENT_TERM_DAYS",
        "contact": "CUSTOMER.CONTACT_EMAIL",
    })
    r = _client(env).post(
        PREVIEW_URL, json=_body(draft_binding=draft), headers=_auth())
    _assert_preview_error(r, status=422, reason_code="draft_invalid")
    assert "OperationalError" not in r.text
    assert "NO_SUCH_COLUMN" not in r.text  # 安全摘要,不回传内部列细节到 detail 出口


def test_draft_join_fk_not_on_anchor_is_draft_invalid_by_api(env):
    """join 外键不在锚表 → 422,不得 500。"""
    draft = _customer_draft(field_map={
        "customer_code": "CUSTOMER.CUSTOMER_CODE",
        "name": "CUSTOMER.CUSTOMER_NAME",
        "region": "CUSTOMER.COUNTRY_REGION",
        # CURRENCY 是 join 目标,外键却错误写在 CURRENCY 上
        "currency": "CURRENCY.CURRENCY_CODE (join CURRENCY.Id)",
        "payment_days": "CUSTOMER.PAYMENT_TERM_DAYS",
        "contact": "CUSTOMER.CONTACT_EMAIL",
    })
    r = _client(env).post(
        PREVIEW_URL, json=_body(draft_binding=draft), headers=_auth())
    _assert_preview_error(r, status=422, reason_code="draft_invalid")
    assert r.status_code != 500
    assert "ValueError" not in r.text
    assert "Traceback" not in r.text


def test_draft_empty_field_map_is_draft_invalid_by_api(env):
    """空 field_map → 422,不得 500 preview_failed。"""
    draft = _customer_draft(field_map={})
    r = _client(env).post(
        PREVIEW_URL, json=_body(draft_binding=draft), headers=_auth())
    _assert_preview_error(r, status=422, reason_code="draft_invalid")
    assert r.status_code != 500
    assert "ValueError" not in r.text
    assert "Traceback" not in r.text


def test_draft_non_anchor_field_without_join_is_draft_invalid_by_api(env):
    """非锚表字段缺 join → 422,不得 500 preview_failed。"""
    draft = _customer_draft(field_map={
        "customer_code": "CUSTOMER.CUSTOMER_CODE",
        "name": "CUSTOMER.CUSTOMER_NAME",
        "region": "CUSTOMER.COUNTRY_REGION",
        "currency": "CURRENCY.CURRENCY_CODE",  # 合法表但缺 join
        "payment_days": "CUSTOMER.PAYMENT_TERM_DAYS",
        "contact": "CUSTOMER.CONTACT_EMAIL",
    })
    r = _client(env).post(
        PREVIEW_URL, json=_body(draft_binding=draft), headers=_auth())
    _assert_preview_error(r, status=422, reason_code="draft_invalid")
    assert r.status_code != 500
    assert "ValueError" not in r.text
    assert "Traceback" not in r.text

# ---- 零业务副作用 ----


def test_preview_side_effect_barrier(env, monkeypatch):
    before = _protected_snapshot(env.db_path)
    pointer_before = _published_pointer(env.db_path)
    counts_before = _business_write_counts(env.db_path)
    (audit_before,) = LandingStore(env.db_path).con.execute(
        "SELECT COUNT(*) FROM d2a_console_access_audit").fetchone()
    client = _client(env)

    # 成功
    assert client.post(PREVIEW_URL, json=_body(), headers=_auth()).status_code == 200
    # 空样本(超大 offset,不改业务表)
    empty = client.post(
        PREVIEW_URL,
        json=_body(sample={"limit": 10, "offset": 10000}),
        headers=_auth(),
    )
    assert empty.status_code == 200
    assert empty.json()["sample"]["sampled_rows"] == 0
    # 422
    draft = {
        "tables": ["NOT_IN_WHITELIST"],
        "key_map": {},
        "field_map": {},
        "derived": {},
        "notes": "",
    }
    assert client.post(
        PREVIEW_URL, json=_body(draft_binding=draft), headers=_auth(),
    ).status_code == 422
    # 500
    monkeypatch.setattr(
        "data2agent.console.app.preview_mapping",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert client.post(PREVIEW_URL, json=_body(), headers=_auth()).status_code == 500

    after = _protected_snapshot(env.db_path)
    assert before == after, "preview 不得改变受保护表内容/schema"
    assert _published_pointer(env.db_path) == pointer_before
    assert _business_write_counts(env.db_path) == counts_before
    (audit_after,) = LandingStore(env.db_path).con.execute(
        "SELECT COUNT(*) FROM d2a_console_access_audit").fetchone()
    assert audit_after > audit_before


def test_preview_creates_no_run_version_or_temp_tables(env):
    before_tables = {
        r[0] for r in LandingStore(env.db_path).con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    counts_before = _business_write_counts(env.db_path)
    pointer_before = _published_pointer(env.db_path)
    client = _client(env)
    assert client.post(PREVIEW_URL, json=_body(), headers=_auth()).status_code == 200
    assert client.post(
        PREVIEW_URL, json=_body(draft_binding=_customer_draft()), headers=_auth(),
    ).status_code == 200

    after_tables = {
        r[0] for r in LandingStore(env.db_path).con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert after_tables == before_tables
    assert _business_write_counts(env.db_path) == counts_before
    assert _published_pointer(env.db_path) == pointer_before
    assert counts_before["obj_tables"] == 0
    assert counts_before["temp_persistent"] == 0


def test_client_disconnect_write_barrier(env, monkeypatch):
    before = _protected_snapshot(env.db_path)
    pointer_before = _published_pointer(env.db_path)
    counts_before = _business_write_counts(env.db_path)

    def boom(*args, **kwargs):
        raise ClientDisconnect()

    monkeypatch.setattr("data2agent.console.app.preview_mapping", boom)
    r = _client(env).post(PREVIEW_URL, json=_body(), headers=_auth())
    # 断开视作内部失败安全出口,不得泄漏异常类名
    assert r.status_code == 500
    assert r.json()["reason_code"] == "preview_failed"
    assert "ClientDisconnect" not in r.text
    assert before == _protected_snapshot(env.db_path)
    assert _published_pointer(env.db_path) == pointer_before
    assert _business_write_counts(env.db_path) == counts_before


def test_preview_concurrent_publish_preserves_preview_write_barrier(env):
    """并发 publish 可改 published 指针/版本表;preview 自身不得额外写隔离或临时表。"""
    pack = load_pack(ROOT / "templates")
    before = _business_write_counts(env.db_path)
    baseline_quarantine = before["quarantine"]
    baseline_obj = before["obj_tables"]
    baseline_temp = before["temp_persistent"]
    baseline_runs = before["sync_run"]
    baseline_steps = before["run_step"]

    start = threading.Event()
    errors: list[BaseException] = []
    publish_versions: list[str] = []

    def publisher() -> None:
        try:
            start.wait(timeout=5)
            store = LandingStore(env.db_path)
            result = build_dataset(store, pack, SOURCE, auto_publish=True)
            assert result.outcome == "ok" and result.dataset_version
            publish_versions.append(result.dataset_version)
        except BaseException as exc:  # noqa: BLE001 — 收集线程失败
            errors.append(exc)

    thread = threading.Thread(target=publisher, daemon=True)
    thread.start()
    client = _client(env)
    start.set()
    preview_statuses: list[int] = []
    for _ in range(8):
        preview_statuses.append(
            client.post(PREVIEW_URL, json=_body(), headers=_auth()).status_code
        )
    thread.join(timeout=60)
    assert not errors, errors
    assert publish_versions, "concurrent publish should complete"
    assert all(s == 200 for s in preview_statuses)

    after = _business_write_counts(env.db_path)
    # publish 会创建 sync_run/step 与新 version;preview 不得额外制造隔离/遗留 obj_/临时表
    assert after["quarantine"] == baseline_quarantine
    assert after["obj_tables"] == baseline_obj == 0
    assert after["temp_persistent"] == baseline_temp == 0
    assert after["sync_run"] > baseline_runs
    assert after["run_step"] >= baseline_steps
    assert after["dataset_version"] == before["dataset_version"] + 1
    # preview 不得把 sync_run 推到远超单次 build 的规模(允许 build 内部辅助 run)
    assert after["sync_run"] - baseline_runs <= 3
    pub = LandingStore(env.db_path).get_published_dataset(SOURCE)
    assert pub is not None
    assert pub.dataset_version == publish_versions[-1]
    assert pub.status == "published"
    obj_rows = LandingStore(env.db_path).list_object_versions(pub.dataset_version)
    assert obj_rows
    assert all(o.status == "published" for o in obj_rows)
