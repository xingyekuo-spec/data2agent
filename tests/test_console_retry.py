"""M5-T06 对象级 retry 观测测试:Run/step 记录、前置校验、熔断、fail-close。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.connect.increment import incremental_sync, watermarks_from_pack
from data2agent.connect.landing import LandingStore
from data2agent.connect.mapping_apply import MappingCircuitBreaker
from data2agent.connect.sync import whitelist_from_pack
from data2agent.console.app import create_app
from data2agent.console.contracts import RetryActionError, RetryActionResult
from data2agent.metamodel.loader import load_pack
from data2agent.showroom.seed import build, write_db

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"


@pytest.fixture()
def env(tmp_path):
    """完整环境:landing db + 模板 + config(包含 SOURCE)。"""
    src = tmp_path / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    pack = load_pack(ROOT / "templates")
    landing = LandingStore(tmp_path / "landing.sqlite")
    hook = lambda action, sql, rows, ms: landing.log_audit(SOURCE, action, sql, rows, ms)  # noqa: E731
    adapter = SqliteReadOnlyAdapter(
        str(src), whitelist_from_pack(pack, SOURCE), audit_hook=hook)
    incremental_sync(adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))
    # 不预先执行 apply_objects,让 retry 独立物化
    cfg_file = tmp_path / "connect.yaml"
    cfg_file.write_text(
        f"templates: {ROOT / 'templates'}\n"
        f"landing: {landing.db_path}\n"
        "sources:\n"
        "  digiwin_e10:\n"
        "    adapter: sqlite_readonly\n"
        f"    path: {src}\n"
        "    tables:\n"
        "      CUSTOMER:\n"
        "        mode: incremental\n"
        "        watermark: UPD\n",
        encoding="utf-8")
    return landing, cfg_file, pack


def _client(landing: LandingStore, cfg_file: Path) -> TestClient:
    from data2agent.connect.config import load_config
    cfg = load_config(cfg_file)
    return TestClient(create_app(
        landing.db_path, ROOT / "templates", cfg))


def _client_no_config(landing: LandingStore) -> TestClient:
    return TestClient(create_app(landing.db_path, ROOT / "templates"))


def _client_with_token(landing: LandingStore, cfg_file: Path) -> TestClient:
    from data2agent.connect.config import load_config
    cfg = load_config(cfg_file)
    return TestClient(create_app(
        landing.db_path, ROOT / "templates", cfg, token="t"))


# ============================================================
# 成功路径
# ============================================================

class TestRetrySuccess:
    """成功重试:返回 RetryActionResult,创建 Run + step。"""

    def test_success_returns_retry_action_result(self, env):
        landing, cfg_file, pack = env
        obj = pack.objects[0].object
        client = _client(landing, cfg_file)
        r = client.post("/api/actions/retry", json={"source": SOURCE, "object": obj})
        assert r.status_code == 200
        body = r.json()
        result = RetryActionResult.model_validate(body)
        assert result.executed is True
        assert result.object == obj
        assert result.status == "ok"
        assert isinstance(result.run_id, int) and result.run_id > 0
        assert isinstance(result.step_id, int) and result.step_id > 0
        assert result.detail_path == f"/api/runs/{result.run_id}"

    def test_retry_creates_run_with_type_apply(self, env):
        landing, cfg_file, pack = env
        obj = pack.objects[0].object
        client = _client(landing, cfg_file)
        r = client.post("/api/actions/retry", json={"source": SOURCE, "object": obj})
        assert r.status_code == 200
        run_id = r.json()["run_id"]
        run = landing.con.execute(
            "SELECT * FROM d2a_sync_run WHERE id = ?", (run_id,)).fetchone()
        assert run is not None
        assert run["run_type"] == "apply"
        assert run["status"] == "ok"
        assert run["source"] == SOURCE

    def test_retry_creates_step_with_kind_object(self, env):
        landing, cfg_file, pack = env
        obj = pack.objects[0].object
        client = _client(landing, cfg_file)
        r = client.post("/api/actions/retry", json={"source": SOURCE, "object": obj})
        assert r.status_code == 200
        step_id = r.json()["step_id"]
        step = landing.con.execute(
            "SELECT * FROM d2a_run_step WHERE id = ?", (step_id,)).fetchone()
        assert step is not None
        assert step["kind"] == "object"
        assert step["target"] == obj
        assert step["status"] == "ok"

    def test_step_has_metrics(self, env):
        landing, cfg_file, pack = env
        obj = pack.objects[0].object
        client = _client(landing, cfg_file)
        r = client.post("/api/actions/retry", json={"source": SOURCE, "object": obj})
        assert r.status_code == 200
        step_id = r.json()["step_id"]
        step = landing.con.execute(
            "SELECT rows_in, rows_out, quarantined FROM d2a_run_step WHERE id = ?",
            (step_id,)).fetchone()
        assert isinstance(step["rows_in"], int)
        assert isinstance(step["rows_out"], int)
        assert isinstance(step["quarantined"], int)


# ============================================================
# 前置校验(不创建 Run)
# ============================================================

class TestRetryPreflight:
    """前置校验失败:返回 4xx,不创建 Run。"""

    def _run_count(self, landing: LandingStore) -> int:
        (n,) = landing.con.execute(
            "SELECT COUNT(*) FROM d2a_sync_run").fetchone()
        return n

    def test_no_config_returns_409_no_run(self, env):
        landing, _cfg_file, pack = env
        obj = pack.objects[0].object
        before = self._run_count(landing)
        client = _client_no_config(landing)
        r = client.post("/api/actions/retry", json={"source": SOURCE, "object": obj})
        assert r.status_code == 409
        body = r.json()
        error = RetryActionError.model_validate(body)
        assert error.reason_code == "preflight_failed"
        assert error.executed is False
        assert error.object == obj
        assert error.status == "aborted"
        assert self._run_count(landing) == before

    def test_unknown_source_returns_404_no_run(self, env):
        landing, cfg_file, pack = env
        obj = pack.objects[0].object
        before = self._run_count(landing)
        client = _client(landing, cfg_file)
        r = client.post("/api/actions/retry",
                       json={"source": "nonexistent_source", "object": obj})
        assert r.status_code == 404
        assert self._run_count(landing) == before

    def test_no_binding_for_source_returns_409_no_run(self, env, tmp_path):
        """对象对某个 source 没有 binding → 409。"""
        landing, _cfg_file, pack = env
        obj = pack.objects[0].object
        # 创建仅含未知 source 的临时 config
        from data2agent.connect.config import ConnectConfig, SourceConfig
        cfg = ConnectConfig(
            templates=str(ROOT / "templates"),
            landing=landing.db_path,
            sources={"other_source": SourceConfig(adapter="sqlite_readonly", path=str(tmp_path / "dummy.sqlite"))})
        before = self._run_count(landing)
        client = TestClient(create_app(
            landing.db_path, ROOT / "templates", cfg))
        r = client.post("/api/actions/retry",
                       json={"source": "other_source", "object": obj})
        assert r.status_code == 409
        body = r.json()
        error = RetryActionError.model_validate(body)
        assert error.reason_code == "preflight_failed"
        assert error.executed is False
        assert self._run_count(landing) == before

    def test_disabled_binding_returns_409_no_run(self, env, monkeypatch):
        """所有 binding 被禁用 → 409。"""
        landing, cfg_file, pack = env
        obj = pack.objects[0].object
        # 修改 pack 对象的 binding 为 disabled,然后 monkeypatch
        # load_pack 使其返回修改后的 pack(因为 create_app 会重新 load)
        for b in pack.objects[0].bindings:
            b.status = "disabled"  # type: ignore[misc]

        import data2agent.console.app as app_module
        monkeypatch.setattr(app_module, "load_pack", lambda _path: pack)

        before = self._run_count(landing)
        client = _client(landing, cfg_file)
        r = client.post("/api/actions/retry", json={"source": SOURCE, "object": obj})
        assert r.status_code == 409, f"expected 409, got {r.status_code}: {r.text}"
        body = r.json()
        error = RetryActionError.model_validate(body)
        assert error.reason_code == "preflight_failed"
        assert error.executed is False
        assert self._run_count(landing) == before

    def test_unknown_object_returns_404_no_run(self, env):
        landing, cfg_file, _pack = env
        before = self._run_count(landing)
        client = _client(landing, cfg_file)
        r = client.post("/api/actions/retry",
                       json={"source": SOURCE, "object": "NonexistentObject"})
        assert r.status_code == 404
        assert self._run_count(landing) == before

    def test_missing_object_returns_422_no_run(self, env):
        landing, cfg_file, _pack = env
        before = self._run_count(landing)
        client = _client(landing, cfg_file)
        r = client.post("/api/actions/retry", json={"source": SOURCE})
        assert r.status_code == 422
        assert self._run_count(landing) == before


# ============================================================
# 熔断
# ============================================================

class TestRetryCircuitBreaker:
    """熔断:返回 409 + RetryActionError(status=aborted),关闭 run/step 为 failed/aborted。"""

    def test_breaker_returns_409_with_retry_action_error(self, env, monkeypatch):
        landing, cfg_file, pack = env
        obj = pack.objects[0].object

        # 让 apply_object 抛出 MappingCircuitBreaker
        def _raise_breaker(*args, **kwargs):
            raise MappingCircuitBreaker(
                f"{obj}: 隔离率 10/10 超过阈值",
                total=10, mapped=0, quarantined=10, batch_id="test-batch")

        monkeypatch.setattr(
            "data2agent.connect.dataset_publish.apply_object", _raise_breaker)

        client = _client(landing, cfg_file)
        r = client.post("/api/actions/retry", json={"source": SOURCE, "object": obj})
        assert r.status_code == 409
        body = r.json()
        error = RetryActionError.model_validate(body)
        assert error.reason_code == "circuit_broken"
        assert error.executed is True
        assert error.object == obj
        assert error.status == "aborted"
        assert error.total == 10
        assert error.mapped == 0
        assert error.quarantined == 10

    def test_breaker_closes_run_as_failed(self, env, monkeypatch):
        landing, cfg_file, pack = env
        obj = pack.objects[0].object

        def _raise_breaker(*args, **kwargs):
            raise MappingCircuitBreaker(
                f"{obj}: 隔离率超过阈值",
                total=10, mapped=0, quarantined=10, batch_id="test-batch")

        monkeypatch.setattr(
            "data2agent.connect.dataset_publish.apply_object", _raise_breaker)

        client = _client(landing, cfg_file)
        r = client.post("/api/actions/retry", json={"source": SOURCE, "object": obj})
        assert r.status_code == 409
        body = r.json()
        run = landing.con.execute(
            "SELECT status FROM d2a_sync_run WHERE id = ?",
            (body["run_id"],)).fetchone()
        assert run["status"] == "failed"

    def test_breaker_closes_step_as_aborted(self, env, monkeypatch):
        landing, cfg_file, pack = env
        obj = pack.objects[0].object

        def _raise_breaker(*args, **kwargs):
            raise MappingCircuitBreaker(
                f"{obj}: 隔离率超过阈值",
                total=10, mapped=0, quarantined=10, batch_id="test-batch")

        monkeypatch.setattr(
            "data2agent.connect.dataset_publish.apply_object", _raise_breaker)

        client = _client(landing, cfg_file)
        r = client.post("/api/actions/retry", json={"source": SOURCE, "object": obj})
        assert r.status_code == 409
        body = r.json()
        step = landing.con.execute(
            "SELECT status FROM d2a_run_step WHERE id = ?",
            (body["step_id"],)).fetchone()
        assert step["status"] == "aborted"


# ============================================================
# 执行异常
# ============================================================

class TestRetryExecutionFailure:
    """执行异常:返回 500 + RetryActionError(status=failed)。"""

    def test_execution_failure_returns_500_with_retry_action_error(self, env, monkeypatch):
        landing, cfg_file, pack = env
        obj = pack.objects[0].object

        def _raise_error(*args, **kwargs):
            raise RuntimeError("simulated apply failure")

        monkeypatch.setattr(
            "data2agent.connect.dataset_publish.apply_object", _raise_error)

        client = _client(landing, cfg_file)
        r = client.post("/api/actions/retry", json={"source": SOURCE, "object": obj})
        assert r.status_code == 500
        body = r.json()
        error = RetryActionError.model_validate(body)
        assert error.reason_code == "execution_failed"
        assert error.executed is True
        assert error.object == obj
        assert error.status == "failed"

    def test_execution_failure_closes_run_as_failed(self, env, monkeypatch):
        landing, cfg_file, pack = env
        obj = pack.objects[0].object

        def _raise_error(*args, **kwargs):
            raise RuntimeError("simulated apply failure")

        monkeypatch.setattr(
            "data2agent.connect.dataset_publish.apply_object", _raise_error)

        client = _client(landing, cfg_file)
        r = client.post("/api/actions/retry", json={"source": SOURCE, "object": obj})
        assert r.status_code == 500
        body = r.json()
        run = landing.con.execute(
            "SELECT status FROM d2a_sync_run WHERE id = ?",
            (body["run_id"],)).fetchone()
        assert run["status"] == "failed"

    def test_execution_failure_closes_step_as_failed(self, env, monkeypatch):
        landing, cfg_file, pack = env
        obj = pack.objects[0].object

        def _raise_error(*args, **kwargs):
            raise RuntimeError("simulated apply failure")

        monkeypatch.setattr(
            "data2agent.connect.dataset_publish.apply_object", _raise_error)

        client = _client(landing, cfg_file)
        r = client.post("/api/actions/retry", json={"source": SOURCE, "object": obj})
        assert r.status_code == 500
        body = r.json()
        step = landing.con.execute(
            "SELECT status FROM d2a_run_step WHERE id = ?",
            (body["step_id"],)).fetchone()
        assert step["status"] == "failed"

    def test_error_detail_is_safe_no_traceback(self, env, monkeypatch):
        """错误响应 detail 不含 traceback,仅为安全摘要。"""
        landing, cfg_file, pack = env
        obj = pack.objects[0].object

        def _raise_error(*args, **kwargs):
            raise RuntimeError("long traceback\n  File 'x.py', line 42\n    do_stuff()")

        monkeypatch.setattr(
            "data2agent.connect.dataset_publish.apply_object", _raise_error)

        client = _client(landing, cfg_file)
        r = client.post("/api/actions/retry", json={"source": SOURCE, "object": obj})
        assert r.status_code == 500
        body = r.json()
        error = RetryActionError.model_validate(body)
        assert "traceback" not in error.detail.lower()
        assert "File" not in error.detail
        assert error.reason_code == "execution_failed"


# ============================================================
# 观察写入失败(fail-close)
# ============================================================

class TestRetryObservationFailure:
    """step 观测写入失败时 fail-close,不返回成功。

    T07 起 step 由 build_dataset 编排;写入失败表现为数据集构建/执行失败。
    """

    def test_step_write_failure_returns_execution_failed(self, env, monkeypatch):
        landing, cfg_file, pack = env
        obj = pack.objects[0].object

        client = _client(landing, cfg_file)

        # 让 LandingStore.update_step 在成功路径抛出异常(必须 patch class,
        # 因为 create_app 内 store() 创建了新的 LandingStore 实例)
        original_update = LandingStore.update_step
        call_count = [0]

        def _failing_update(self_, step_id, **fields):
            call_count[0] += 1
            if call_count[0] == 1 and fields.get("status") == "ok":
                import sqlite3
                raise sqlite3.OperationalError("disk full")
            return original_update(self_, step_id, **fields)

        monkeypatch.setattr(LandingStore, "update_step", _failing_update)

        r = client.post("/api/actions/retry", json={"source": SOURCE, "object": obj})
        assert r.status_code == 500
        body = r.json()
        error = RetryActionError.model_validate(body)
        assert error.reason_code == "execution_failed"
        assert error.executed is True
        assert error.object == obj
        assert error.status == "failed"
        assert error.error_id is not None
        assert len(error.error_id) > 0

    def test_observation_failure_has_error_id(self, env, monkeypatch):
        landing, cfg_file, pack = env
        obj = pack.objects[0].object

        client = _client(landing, cfg_file)
        original_update = LandingStore.update_step
        call_count = [0]

        def _failing_update(self_, step_id, **fields):
            call_count[0] += 1
            if call_count[0] == 1 and fields.get("status") == "ok":
                raise RuntimeError("update failed")
            return original_update(self_, step_id, **fields)

        monkeypatch.setattr(LandingStore, "update_step", _failing_update)

        r = client.post("/api/actions/retry", json={"source": SOURCE, "object": obj})
        assert r.status_code == 500
        body = r.json()
        assert body.get("error_id")
        assert isinstance(body["error_id"], str)
        assert len(body["error_id"]) >= 8
        assert landing.get_published_dataset(SOURCE) is None


# ============================================================
# Run/step 证据完整性
# ============================================================

class TestRetryEvidence:
    """Run + step 的元数据完整性。"""

    def test_run_detail_path_points_to_correct_run(self, env):
        landing, cfg_file, pack = env
        obj = pack.objects[0].object
        client = _client(landing, cfg_file)
        r = client.post("/api/actions/retry", json={"source": SOURCE, "object": obj})
        assert r.status_code == 200
        body = r.json()
        assert body["detail_path"] == f"/api/runs/{body['run_id']}"

    def test_breaker_response_has_detail_path(self, env, monkeypatch):
        landing, cfg_file, pack = env
        obj = pack.objects[0].object

        def _raise_breaker(*args, **kwargs):
            raise MappingCircuitBreaker(
                f"{obj}: 隔离率超过阈值",
                total=10, mapped=0, quarantined=10, batch_id="test-batch")

        monkeypatch.setattr(
            "data2agent.connect.dataset_publish.apply_object", _raise_breaker)

        client = _client(landing, cfg_file)
        r = client.post("/api/actions/retry", json={"source": SOURCE, "object": obj})
        body = r.json()
        assert body["detail_path"] == f"/api/runs/{body['run_id']}"

    def test_error_response_has_detail_path(self, env, monkeypatch):
        landing, cfg_file, pack = env
        obj = pack.objects[0].object

        def _raise_error(*args, **kwargs):
            raise RuntimeError("fail")

        monkeypatch.setattr(
            "data2agent.connect.dataset_publish.apply_object", _raise_error)

        client = _client(landing, cfg_file)
        r = client.post("/api/actions/retry", json={"source": SOURCE, "object": obj})
        body = r.json()
        assert body["detail_path"] == f"/api/runs/{body['run_id']}"

    # -- Issue 5: OpenAPI schema 包含 RetryActionError --

    def test_retry_action_error_in_openapi_schema(self, env):
        """RetryActionError 必须出现在 /actions/retry 的 409/500 responses 中。"""
        landing, cfg_file, _pack = env
        client = _client(landing, cfg_file)
        r = client.get("/openapi.json")
        assert r.status_code == 200
        schema = r.json()
        paths = schema["paths"]["/api/actions/retry"]["post"]["responses"]
        # 409 和 500 应声明 RetryActionError
        for code in ("409", "500"):
            assert code in paths, f"responses 缺失 {code}"
            # FastAPI 自动将 model 转为 $ref
            content = paths[code].get("content", {}).get("application/json", {})
            ref = content.get("schema", {}).get("$ref", "")
            assert "RetryActionError" in ref, \
                f"{code} response 未声明 RetryActionError, schema={ref}"

    def test_success_second_retry_creates_new_run(self, env):
        """两次 retry 各产生独立的 run + step。"""
        landing, cfg_file, pack = env
        obj = pack.objects[0].object
        client = _client(landing, cfg_file)
        r1 = client.post("/api/actions/retry", json={"source": SOURCE, "object": obj})
        r2 = client.post("/api/actions/retry", json={"source": SOURCE, "object": obj})
        assert r1.status_code == 200
        assert r2.status_code == 200
        id1 = r1.json()["run_id"]
        id2 = r2.json()["run_id"]
        assert id1 != id2
        # 两次 step 也不同
        assert r1.json()["step_id"] != r2.json()["step_id"]

    def test_run_runtime_is_apply_not_retry(self, env):
        """run_type 必须是 apply,不是 retry。"""
        landing, cfg_file, pack = env
        obj = pack.objects[0].object
        client = _client(landing, cfg_file)
        r = client.post("/api/actions/retry", json={"source": SOURCE, "object": obj})
        run = landing.con.execute(
            "SELECT run_type FROM d2a_sync_run WHERE id = ?",
            (r.json()["run_id"],)).fetchone()
        assert run["run_type"] == "apply"

    # ---- 模板包所有对象均可正常 retry ----

    def test_retry_every_object(self, env):
        """对每个有 binding 的对象执行 retry。"""
        landing, cfg_file, pack = env
        client = _client(landing, cfg_file)
        for tpl in pack.objects:
            if not any(b.source == SOURCE and b.enabled for b in tpl.bindings):
                continue
            r = client.post("/api/actions/retry",
                           json={"source": SOURCE, "object": tpl.object})
            assert r.status_code == 200, f"retry {tpl.object} failed: {r.text}"
            body = RetryActionResult.model_validate(r.json())
            assert body.status == "ok"
            assert body.run_id > 0
