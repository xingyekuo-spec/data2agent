"""v0.3 M2-T11: 并发读者、双 publisher、stale previous 与 Console 读事务。

真实独立 SQLite 连接 + 事务;不得只 mock 状态方法。
"""

from __future__ import annotations

import threading
import time
from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.connect.dataset_publish import (
    PublishedSnapshotError,
    _recover_stale_building,
    build_dataset,
    publish_dataset,
    published_read_tx,
    resolve_published_snapshot,
)
from data2agent.connect.increment import incremental_sync, watermarks_from_pack
from data2agent.connect.landing import LandingStore
from data2agent.connect.sync import whitelist_from_pack
from data2agent.console import observability as obs
from data2agent.console.app import create_app
from data2agent.mcp_server.core import QueryService
from data2agent.metamodel.dataset_publish_contract import make_build_table
from data2agent.metamodel.loader import load_pack
from data2agent.metamodel.schema import ObjectTemplate, Property, TemplatePack
from data2agent.metamodel.versioning import DatasetVersionRecord, ObjectVersionRecord
from data2agent.showroom.seed import build, write_db

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"
TOKEN = "conc-secret"


@pytest.fixture(scope="module")
def pack():
    return load_pack(ROOT / "templates")


def _sync_landing(tmp_path: Path, pack) -> LandingStore:
    src = tmp_path / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    landing = LandingStore(tmp_path / "landing.sqlite")
    adapter = SqliteReadOnlyAdapter(str(src), whitelist_from_pack(pack, SOURCE))
    incremental_sync(adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))
    return landing


def _stage(landing, pack):
    result = build_dataset(landing, pack, SOURCE, auto_publish=False)
    assert result.outcome == "ok" and result.ready
    return result


def _assert_snap_coherent(store: LandingStore, snap) -> None:
    """对象元数据与物理表均属于同一 dataset_version。"""
    rows = store.list_object_versions(snap.dataset_version)
    assert {o.object for o in rows} == set(snap.objects)
    assert all(o.status == "published" for o in rows)
    for name, entry in snap.objects.items():
        meta = next(o for o in rows if o.object == name)
        assert meta.object_version == entry.object_version
        assert meta.build_table == entry.physical_table
        (n,) = store.con.execute(
            f'SELECT COUNT(*) FROM "{entry.physical_table}"'
        ).fetchone()
        assert n == entry.row_count == meta.row_count


def _seed_minimal_customer(
    store: LandingStore,
    *,
    source: str,
    version: str,
    rows: list[tuple[str, str]],
    binding_hash: str = "sha256:" + "ab" * 32,
) -> str:
    mini = TemplatePack(
        version="0.1.0",
        objects=[
            ObjectTemplate(
                object="Customer",
                display_name="客户",
                domain="销售",
                source_of_truth="t",
                keys=["customer_code"],
                properties=[
                    Property(name="customer_code", type="string"),
                    Property(name="name", type="string"),
                    Property(name="contact", type="string", sensitive=True),
                ],
                bindings=[],
            )
        ],
        metrics=[],
    )
    table = make_build_table(
        source, "Customer", f"{abs(hash(source + version)):012x}"[:12],
    )
    store.con.execute(
        f'CREATE TABLE "{table}" ('
        "customer_code TEXT PRIMARY KEY, name TEXT, contact TEXT)"
    )
    for code, name in rows:
        store.con.execute(
            f'INSERT INTO "{table}" (customer_code, name, contact) VALUES (?, ?, ?)',
            (code, name, f"{code}@ex.com"),
        )
    store.insert_dataset_version(
        DatasetVersionRecord(
            dataset_version=version,
            source=source,
            template_version=mini.version,
            status="published",
            built_at="2026-07-21T10:00:00",
            published_at="2026-07-21T10:05:00",
            object_manifest='["Customer"]',
            template_snapshot=mini.model_dump_json(),
        )
    )
    store.insert_object_version(
        ObjectVersionRecord(
            dataset_version=version,
            object="Customer",
            object_version=f"{version}-Customer",
            binding_hash=binding_hash,
            row_count=len(rows),
            build_table=table,
            status="published",
            built_at="2026-07-21T10:00:00",
            published_at="2026-07-21T10:05:00",
        )
    )
    return table


# ---- §10.1 并发读者:只见全旧或全新 ----


def test_concurrent_readers_see_only_all_old_or_all_new(tmp_path, pack):
    landing = _sync_landing(tmp_path, pack)
    v1 = _stage(landing, pack)
    assert publish_dataset(landing, v1.dataset_version).executed is True
    v2 = _stage(landing, pack)
    db_path = landing.db_path
    landing.con.close()

    stop = threading.Event()
    seen_versions: set[str] = set()
    errors: list[BaseException] = []
    lock = threading.Lock()

    def reader_snap() -> None:
        while not stop.is_set():
            store = LandingStore.open_readonly(db_path)
            try:
                with published_read_tx(store):
                    snap = resolve_published_snapshot(store, SOURCE)
                    _assert_snap_coherent(store, snap)
                    with lock:
                        seen_versions.add(snap.dataset_version)
            except PublishedSnapshotError as e:
                if e.reason_code == "snapshot_corrupt":
                    with lock:
                        errors.append(e)
                    break
            except Exception as e:
                with lock:
                    errors.append(e)
                break
            finally:
                store.con.close()

    def reader_mcp() -> None:
        svc = QueryService(db_path, ROOT / "templates", source=SOURCE)
        while not stop.is_set():
            try:
                cust = svc.query_objects("Customer", limit=5)
                metric = svc.query_metrics(
                    "quote_response_hours", group_by="客户", limit=5,
                )
                dv_c = cust["meta"]["dataset_version"]
                dv_m = metric["meta"]["dataset_version"]
                # 单次工具调用内一致;两次调用允许跨版本,但各自内部不得混版
                assert dv_c in (v1.dataset_version, v2.dataset_version)
                assert dv_m in (v1.dataset_version, v2.dataset_version)
                bh = metric["meta"]["binding_hashes"]
                assert bh and set(bh) >= {"Quotation", "Customer"}
                with lock:
                    seen_versions.add(dv_m)
                    seen_versions.add(dv_c)
            except Exception as e:
                msg = str(e)
                if "snapshot_corrupt" in msg:
                    with lock:
                        errors.append(e)
                    break
                if "not_published" in msg:
                    continue
                with lock:
                    errors.append(e)
                break

    threads = [
        threading.Thread(target=reader_snap, daemon=True) for _ in range(4)
    ] + [
        threading.Thread(target=reader_mcp, daemon=True) for _ in range(2)
    ]
    for t in threads:
        t.start()
    time.sleep(0.05)

    writer = LandingStore(db_path)
    pub = publish_dataset(writer, v2.dataset_version)
    assert pub.outcome == "ok" and pub.executed is True
    writer.con.close()

    time.sleep(0.15)
    stop.set()
    for t in threads:
        t.join(timeout=5)

    assert not errors, f"readers saw inconsistent state: {errors!r}"
    assert seen_versions <= {v1.dataset_version, v2.dataset_version}
    assert v2.dataset_version in seen_versions


def test_two_concurrent_publishers_one_wins(tmp_path, pack):
    landing = _sync_landing(tmp_path, pack)
    staged = _stage(landing, pack)
    version = staged.dataset_version
    db_path = landing.db_path
    landing.con.close()

    barrier = threading.Barrier(2)
    results: list = []
    lock = threading.Lock()

    def publisher() -> None:
        store = LandingStore(db_path)
        barrier.wait(timeout=5)
        try:
            result = publish_dataset(store, version)
            with lock:
                results.append(result)
        finally:
            store.con.close()

    t1 = threading.Thread(target=publisher)
    t2 = threading.Thread(target=publisher)
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert len(results) == 2
    executed = [r for r in results if r.executed]
    idle = [r for r in results if not r.executed]
    assert len(executed) == 1
    assert executed[0].outcome == "ok"
    assert all(r.outcome in ("idempotent", "conflict", "ok") for r in results)
    assert all(r.outcome in ("idempotent", "conflict") for r in idle)

    check = LandingStore(db_path)
    pubs = check.con.execute(
        "SELECT dataset_version FROM d2a_dataset_version "
        "WHERE source = ? AND status = 'published'",
        (SOURCE,),
    ).fetchall()
    assert len(pubs) == 1
    assert pubs[0][0] == version
    snap = resolve_published_snapshot(check, SOURCE)
    _assert_snap_coherent(check, snap)
    check.con.close()


def test_stale_recover_skips_after_concurrent_publish_wins(tmp_path, pack, monkeypatch):
    """recovery 先读到 building 后暂停;publish 提交后 recovery 必须跳过,不得删表。

    真实双连接: list(building) → 暂停 → publish COMMIT → recovery 继续。
    """
    landing = _sync_landing(tmp_path, pack)
    staged = _stage(landing, pack)
    version = staged.dataset_version
    tables = [
        o.build_table
        for o in landing.list_object_versions(version)
        if o.build_table
    ]
    assert len(tables) >= 1
    db_path = landing.db_path
    landing.con.close()

    pause_after_read = threading.Event()
    resume = threading.Event()
    recover_results: list[str | None] = []
    real_list = LandingStore.list_dataset_versions

    def list_then_pause(self, *args, **kwargs):
        rows, total = real_list(self, *args, **kwargs)
        if kwargs.get("status") == "building" and rows:
            pause_after_read.set()
            assert resume.wait(timeout=30), "publish did not resume recovery"
        return rows, total

    monkeypatch.setattr(LandingStore, "list_dataset_versions", list_then_pause)

    def recoverer() -> None:
        store = LandingStore(db_path)
        try:
            recover_results.append(_recover_stale_building(store, SOURCE))
        finally:
            store.con.close()

    t = threading.Thread(target=recoverer, daemon=True)
    t.start()
    assert pause_after_read.wait(timeout=10), "recovery did not reach list pause"

    publisher = LandingStore(db_path)
    pub = publish_dataset(publisher, version)
    publisher.con.close()
    assert pub.executed is True and pub.outcome == "ok"

    resume.set()
    t.join(timeout=30)
    assert recover_results == [None]

    check = LandingStore(db_path)
    published = check.get_published_dataset(SOURCE)
    assert published is not None
    assert published.dataset_version == version
    assert published.status == "published"
    objs = check.list_object_versions(version)
    assert all(o.status == "published" for o in objs)
    for table in tables:
        exists = check.con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        assert exists is not None, f"published table dropped: {table}"
    snap = resolve_published_snapshot(check, SOURCE)
    _assert_snap_coherent(check, snap)
    check.con.close()


def test_build_after_intervening_publish_rejects_stale_previous(tmp_path, pack):
    """构建冻结 previous 后若另一次发布已切换当前指针 → stale_previous。"""
    landing = _sync_landing(tmp_path, pack)
    v1 = _stage(landing, pack)
    assert publish_dataset(landing, v1.dataset_version).executed is True

    cand_a = _stage(landing, pack)
    assert cand_a.previous_dataset_version == v1.dataset_version
    db_path = landing.db_path

    # 独立连接上插入并激活另一 published(模拟构建期间另一次发布)
    other = LandingStore(db_path)
    sneaky_table = make_build_table(SOURCE, "Customer", "eeee1111ffff")
    other.con.execute(
        f'CREATE TABLE "{sneaky_table}" (customer_code TEXT PRIMARY KEY)'
    )
    other.con.execute(f'INSERT INTO "{sneaky_table}" VALUES ("X")')
    other.update_dataset_lifecycle(v1.dataset_version, status="retired")
    other.insert_dataset_version(
        DatasetVersionRecord(
            dataset_version="ds-intervening",
            source=SOURCE,
            template_version=pack.version,
            status="published",
            built_at="2026-07-21T12:00:00",
            published_at="2026-07-21T12:00:00",
            previous_dataset_version=v1.dataset_version,
            object_manifest='["Customer"]',
            template_snapshot=pack.model_dump_json(),
        )
    )
    other.insert_object_version(
        ObjectVersionRecord(
            dataset_version="ds-intervening",
            object="Customer",
            object_version="obj-intervening",
            binding_hash="sha256:" + "ef" * 32,
            row_count=1,
            build_table=sneaky_table,
            status="published",
            built_at="2026-07-21T12:00:00",
            published_at="2026-07-21T12:00:00",
        )
    )
    other.con.close()

    result = publish_dataset(landing, cand_a.dataset_version)
    assert result.outcome == "conflict"
    assert result.reason_code == "stale_previous"
    assert result.http_status == 409
    assert landing.get_published_dataset(SOURCE).dataset_version == "ds-intervening"


# ---- Console 读事务:单请求内不可混版 ----


def test_console_object_stats_read_txn_survives_concurrent_publish(tmp_path, pack):
    """object_stats 在 BEGIN 快照内完成 resolve+对象查询;并发 publish 不得撕快照。"""
    landing = _sync_landing(tmp_path, pack)
    v1 = _stage(landing, pack)
    assert publish_dataset(landing, v1.dataset_version).executed is True
    v2 = _stage(landing, pack)
    db_path = landing.db_path

    reader = LandingStore.open_readonly(db_path)
    with published_read_tx(reader) as store:
        snap_before = resolve_published_snapshot(store, SOURCE)
        assert snap_before.dataset_version == v1.dataset_version

        writer = LandingStore(db_path)
        assert publish_dataset(writer, v2.dataset_version).executed is True
        writer.con.close()

        snap_after = resolve_published_snapshot(store, SOURCE)
        stats = obs.object_stats(store, pack, SOURCE)
        assert snap_after.dataset_version == v1.dataset_version
        assert all(
            st.get("version") == snap_before.objects[name].object_version
            for name, st in stats.items()
            if st.get("rows") is not None and name in snap_before.objects
        )
        assert not any(st.get("error") for st in stats.values())

    reader.con.close()

    after = LandingStore.open_readonly(db_path)
    snap = resolve_published_snapshot(after, SOURCE)
    assert snap.dataset_version == v2.dataset_version
    after.con.close()


def test_console_http_objects_catalog_consistent_under_publish(tmp_path, pack):
    landing = _sync_landing(tmp_path, pack)
    v1 = _stage(landing, pack)
    assert publish_dataset(landing, v1.dataset_version).executed is True
    v2 = _stage(landing, pack)
    db_path = landing.db_path
    v1_owned = {
        o.object_version for o in landing.list_object_versions(v1.dataset_version)
    }
    v2_owned = {
        o.object_version for o in landing.list_object_versions(v2.dataset_version)
    }
    landing.con.close()

    app = create_app(db_path, ROOT / "templates", token=TOKEN)
    headers = {"Authorization": f"Bearer {TOKEN}"}

    stop = threading.Event()
    errors: list[BaseException] = []
    lock = threading.Lock()

    success_count = [0]  # 可变容器供线程累加

    def hammer() -> None:
        # TestClient 非线程安全:每线程独立客户端 + 上下文管理器。
        with TestClient(app, raise_server_exceptions=False) as client:
            while not stop.is_set():
                try:
                    resp = client.get("/api/objects", headers=headers)
                    assert resp.status_code == 200, (
                        f"期望 200,实际 {resp.status_code}: {resp.text[:200]}"
                    )
                    rows = resp.json()
                    assert isinstance(rows, list), type(rows)
                    obj_versions = {
                        r["version"] for r in rows
                        if isinstance(r, dict)
                        and r.get("rows") is not None
                        and r.get("version")
                    }
                    if obj_versions:
                        assert obj_versions <= v1_owned or obj_versions <= v2_owned
                    with lock:
                        success_count[0] += 1
                except Exception as e:
                    with lock:
                        errors.append(e)
                    break

    threads = [threading.Thread(target=hammer, daemon=True) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.05)

    writer = LandingStore(db_path)
    assert publish_dataset(writer, v2.dataset_version).executed is True
    writer.con.close()
    time.sleep(0.15)
    stop.set()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    assert success_count[0] > 0, "并发窗口内未获得任何成功响应"


def test_console_http_overview_atomic_under_publish(tmp_path, pack):
    """Overview 的 versions 与 object_rows 属于同一 published 快照(§10.1)。"""
    landing = _sync_landing(tmp_path, pack)
    v1 = _stage(landing, pack)
    assert publish_dataset(landing, v1.dataset_version).executed is True
    v2 = _stage(landing, pack)
    db_path = landing.db_path

    def _object_rows(version: str) -> int:
        total = 0
        for o in landing.list_object_versions(version):
            (n,) = landing.con.execute(
                f'SELECT COUNT(*) FROM "{o.build_table}"'
            ).fetchone()
            total += n
        return total

    rows_by_version = {
        v1.dataset_version: _object_rows(v1.dataset_version),
        v2.dataset_version: _object_rows(v2.dataset_version),
    }
    landing.con.close()

    app = create_app(db_path, ROOT / "templates", token=TOKEN)
    headers = {"Authorization": f"Bearer {TOKEN}"}

    stop = threading.Event()
    errors: list[BaseException] = []
    lock = threading.Lock()
    allowed = set(rows_by_version)

    def hammer() -> None:
        # TestClient 非线程安全:每线程独立客户端,避免并发串响应。
        client = TestClient(app)
        while not stop.is_set():
            try:
                body = client.get("/api/overview", headers=headers).json()
                ds = body["versions"]["dataset"]
                assert ds in allowed
                # 对象层完整 published → object 版本与 dataset 一致
                assert body["versions"]["object"] == ds
                assert body["summary"]["object_rows"] == rows_by_version[ds]
            except Exception as e:
                with lock:
                    errors.append(e)
                break

    threads = [threading.Thread(target=hammer, daemon=True) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.05)

    writer = LandingStore(db_path)
    assert publish_dataset(writer, v2.dataset_version).executed is True
    writer.con.close()
    time.sleep(0.15)
    stop.set()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors


def test_dual_source_isolation_console_and_mcp(tmp_path):
    """双 source 同名对象互不串线(Console resolve + MCP)。"""
    store = LandingStore(tmp_path / "landing.sqlite")
    table_a = _seed_minimal_customer(
        store,
        source="src_a",
        version="ds-a",
        rows=[("A1", "from-a")],
        binding_hash="sha256:" + "aa" * 32,
    )
    table_b = _seed_minimal_customer(
        store,
        source="src_b",
        version="ds-b",
        rows=[("B1", "from-b")],
        binding_hash="sha256:" + "bb" * 32,
    )
    assert table_a != table_b
    db_path = store.db_path
    store.con.close()

    svc_a = QueryService(db_path, ROOT / "templates", source="src_a")
    svc_b = QueryService(db_path, ROOT / "templates", source="src_b")
    ra = svc_a.query_objects("Customer", limit=10)
    rb = svc_b.query_objects("Customer", limit=10)
    assert {r["customer_code"] for r in ra["rows"]} == {"A1"}
    assert {r["customer_code"] for r in rb["rows"]} == {"B1"}
    assert ra["meta"]["dataset_version"] == "ds-a"
    assert rb["meta"]["dataset_version"] == "ds-b"

    db = LandingStore.open_readonly(db_path)
    with published_read_tx(db) as s:
        sa = resolve_published_snapshot(s, "src_a")
        sb = resolve_published_snapshot(s, "src_b")
    assert sa.dataset_version == "ds-a"
    assert sb.dataset_version == "ds-b"
    assert sa.objects["Customer"].physical_table == table_a
    assert sb.objects["Customer"].physical_table == table_b
    db.con.close()
