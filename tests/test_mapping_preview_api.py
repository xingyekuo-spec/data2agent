"""M3-T05: mapping preview Console API — Bearer、审计、安全错误、只读求值、零副作用。"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.connect.dataset_publish import build_dataset
from data2agent.connect.increment import incremental_sync, watermarks_from_pack
from data2agent.connect.landing import LandingStore
from data2agent.connect.mapping_preview import PreviewError
from data2agent.connect.sync import whitelist_from_pack
from data2agent.console.app import create_app
from data2agent.console.contracts import MappingPreviewError, MappingPreviewResponse
from data2agent.metamodel.loader import load_pack
from data2agent.showroom.seed import build, write_db

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"
TOKEN = "preview-secret"
PREVIEW_URL = "/api/mappings/Customer/preview"

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
    # query token 不得旁路 Bearer-only
    query = client.post(f"{PREVIEW_URL}?token={TOKEN}", json=_body())
    _assert_preview_error(query, status=401, reason_code="unauthorized")
    row = LandingStore(env.db_path).con.execute(
        "SELECT * FROM d2a_console_access_audit ORDER BY id DESC LIMIT 1").fetchone()
    assert row["allowed"] == 0 and row["reason_code"] == "unauthorized"
    assert TOKEN not in str(dict(row))


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
    # yifei binding 存在但展厅未落地 COPMA
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


# ---- 零业务副作用 ----


def test_preview_side_effect_barrier(env, monkeypatch):
    before = _protected_snapshot(env.db_path)
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
    (audit_after,) = LandingStore(env.db_path).con.execute(
        "SELECT COUNT(*) FROM d2a_console_access_audit").fetchone()
    assert audit_after > audit_before
