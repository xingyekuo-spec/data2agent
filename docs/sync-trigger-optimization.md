# 中间机同步机制优化方案

## Context

用户触发同步后，前端一直显示"触发中"看不到进度。根因分析：

1. `POST /api/actions/trigger` 是**同步阻塞调用**，在 HTTP 请求中跑完整个 ETL 链路（拉数→推送→apply），浏览器一直等响应
2. 如果 `sink.type=http` 且平台 ingest 不可达，每批数据重试 3 次 × 30 秒，表一多就是分钟级累积等待
3. 响应中不包含 `run_id`，前端无法跟踪具体运行
4. `incremental_sync` 底层**已经在** `d2a_sync_run` / `d2a_run_step` 表中记录了完整进度，但这些数据从未通过 API 暴露

## 优化策略

**最小改动、复用现有基础设施**：

- 保留 `incremental_sync` 的逐表/逐步记录逻辑，但让它能接收已分配的 `run_id`
- 不改 HTMX 轮询机制（status 页面已有 5 秒轮询模式）
- 参考 metadata 扫描已经有的 `ThreadPoolExecutor` 后台任务模式
- 手动触发与常驻 connector 调度必须共用同一把 **source 级跨进程锁**
- 每个成功处理的批次都持久化累计行数与进度时间，使页面能显示真实进度而非只显示“运行中”
- 只改四个区域：同步编排/锁 → API → 运行查询 → 前端轮询

---

## 改动清单

### 1. `data2agent/middle_admin/app.py` — 异步化触发 + 新增运行查询端点

**A. 把 `/actions/trigger` 改为后台执行，立即返回 `run_id`**

参照已有的 metadata 扫描器模式（`_SCAN_STORE.submit(...)`），将同步提交到后台执行；但不能只增加
`ThreadPoolExecutor(max_workers=1)`。中间机常驻 connector（NSSM / APScheduler）与管理页属于不同
进程，必须先取得与调度器共用的 source 级跨进程锁。

新增 `data2agent/connect/sync_lock.py`，提供 `SourceSyncLock`：

- 锁文件固定为 `<landing 父目录>/locks/sync-<source>.lock`，source 使用安全文件名编码；
- Windows 使用 `msvcrt.locking`，Linux/macOS 使用 `fcntl.flock`，均采用**非阻塞独占锁**；
- 锁句柄在同步完成前始终由执行线程持有；进程崩溃时由操作系统释放，避免数据库租约遗留死锁；
- 调度器和手动触发均通过同一个 `run_sync_cycle` 获取锁，禁止各自实现一套锁；
- 锁获取失败时不得排队：查询同 source 最新 `status='running'` 的 run，返回
  `reason='already_running'` 与 `run_id`（若尚未创建 run 则返回 `run_id: null`）。

触发路由使用如下生命周期：

```python
@api.post("/actions/trigger")
def trigger_action(body: TriggerBody) -> dict:
    # 1. 与 run_sync_cycle 完全相同的预检：窗口外 / tables 为空立即返回，绝不创建 run。
    preflight = check_sync_preflight(name, scfg)
    if not preflight.ok:
        return preflight.response

    # 2. 取得跨进程 source 锁；已在运行时返回已有 run，而不是排队或重复执行。
    lock = SourceSyncLock.try_acquire(cfg.landing, name)
    if lock is None:
        return already_running_response(name, cfg.landing)

    landing = LandingStore(cfg.landing)
    try:
        # 3. 锁已取得后才创建 run，前端可立即查询。
        run_id = landing.start_run(name, "sync")
        try:
            future = _TRIGGER_EXECUTOR.submit(
                _run_sync_worker, run_id, name, scfg, cfg.landing, cfg.templates, lock)
        except Exception as exc:
            landing.finish_running_run(run_id, status="failed", detail=brief_error(exc))
            lock.release()
            raise
    finally:
        landing.con.close()
    return {"action": "sync", "source": name, "run_id": run_id,
            "executed": True, "status": "started"}

def _run_sync_worker(run_id, name, scfg, landing_path, templates, lock):
    """后台线程唯一持有锁；无论任何异常均关闭预创建的 run 并释放锁。"""
    try:
        run_sync_cycle(name, scfg, landing_path, templates,
                       run_id=run_id, acquired_lock=lock)
    except Exception as exc:
        failed_run_store = LandingStore(landing_path)
        try:
            failed_run_store.finish_running_run(
                run_id, status="failed", detail=brief_error(exc))
        finally:
            failed_run_store.con.close()
        log.exception("manual sync failed source=%s run=%s", name, run_id)
    finally:
        lock.release()
```

`finish_running_run()` 必须以 `WHERE id = ? AND status = 'running'` 更新，避免
`incremental_sync` 已写入 `ok / paused / failed` 后被外层异常处理覆盖。所有错误详情使用现有脱敏
规则并限制长度；不得把 DSN、Token 或原始 SQL 写到运行详情。

**B. 新增 `GET /api/runs` — 运行历史列表**

```python
@api.get("/runs")
def runs(source: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    """返回最近的 sync run 列表"""
    where = "WHERE run_type = 'sync'"
    params = []
    if source:
        where += " AND source = ?"
        params.append(source)
    ...
    return {"runs": [...], "total": total, "limit": limit, "offset": offset}
```

固定列表字段：`id`、`source`、`status`、`started_at`、`finished_at`、`tables`、`rows`、
`detail`（脱敏并截断）。`limit` 限制为 1–50，按 `started_at DESC, id DESC` 排序；指定
`source` 时必须经 `_resolve_source()` 校验。列表不返回逐步明细，避免状态页轮询读取过多数据。

**C. 新增 `GET /api/runs/{run_id}` — 运行详情 + 步骤**

```python
@api.get("/runs/{run_id}")
def run_detail(run_id: int) -> dict:
    """单个 run 的详情，包含可轮询的逐表实时步骤"""
    ...
    return {"run": {...}, "steps": [...]}
```

`steps` 每项固定返回：`id`、`ordinal`、`target`、`status`、`started_at`、`finished_at`、
`rows_in`、`rows_out`、`batches`、`batch_id`、`progressed_at`、`watermark_before`、
`watermark_after`、`error`。当 `status='running'` 时，`rows_out`/`batches` 是已成功写入的
累计值，`progressed_at` 是最后一个成功批次完成时间；不能把尚未成功推送的行计入进度。

**D. 修改 `GET /api/status` 的 `build_status()` — 包含当前/最近运行摘要**

在每个 source 中增加固定大小的 `latest_run` 摘要（同列表字段），而不是把完整历史塞进每 5 秒
轮询的状态响应。前端仅在用户展开“运行历史”时调用 `GET /api/runs`；对已启动 run 则轮询
`GET /api/runs/{run_id}`。

---

### 2. `data2agent/connect/scheduler.py` — `run_sync_cycle` 支持外部 `run_id`

```python
def run_sync_cycle(name: str, scfg: SourceConfig,
                   landing_path: str, templates: str = "templates",
                   run_id: int | None = None,
                   acquired_lock: SourceSyncLock | None = None) -> SyncCycleResult:
    # 先做窗口 / 空 tables 预检；自动调度在此处自行获取锁。
    # 手动路径传入已持有锁与 run_id，避免竞争窗口和重复建 run。
    # adapter、sink、协议预检、incremental_sync、local apply 全部由外层 try/finally 覆盖。
    # 任何 pre-increment 异常均 finish_running_run(run_id, failed, ...)；finally 释放自持锁。
    ...
```

`run_sync_cycle` 不再只返回布尔值，而是返回 `SyncCycleResult`：`executed`、`reason`、`run_id`、
`status`。自动调度遇到 `already_running` 记录一条简洁日志后跳过；手动调用使用该结果返回 API。
这样窗口外、未配表、已在运行与实际启动都具有不含歧义的状态。

---

### 3. `data2agent/connect/increment.py` 与 `data2agent/connect/landing.py` — 外部 `run_id` 与逐批进度

#### A. `d2a_run_step` 增加可选进度字段

在建表 SQL 与幂等迁移中增加：

```sql
batches INTEGER,
progressed_at TEXT
```

迁移必须兼容已有现场 SQLite：通过现有 schema migration / `PRAGMA table_info` 检查后再
`ALTER TABLE ... ADD COLUMN`。旧 run 的这两个字段为 `NULL`，前端显示“历史运行未记录批次进度”，
不得把 `NULL` 误显示为 0 批或卡死。

扩展 `LandingStore.update_step()` 的白名单，允许更新 `batches` 与 `progressed_at`。另新增
`LandingStore.record_sync_batch_progress()`：在同一 SQLite 事务内更新可恢复的水位游标（如有）与
当前 step 的累计行数/批次数，最后只提交一次。不能直接串联当前会各自 `commit()` 的
`set_sync_cursor()` 与 `update_step()`；否则页面可能短暂显示无法恢复的虚假进度。

#### B. `incremental_sync` 支持外部 run 并在每个成功批次更新步骤

```python
def incremental_sync(..., run_id: int | None = None) -> SyncReport:
    # 先创建 / 接收 report，再把 ensure_protocol 放进 try 范围。
    # 这样协议预检失败也会 finish_run(..., failed)。
    report = SyncReport(source=source,
                        run_id=run_id or landing.start_run(source, "sync"))
    try:
        ensure_protocol()
        ...
    except Exception as exc:
        landing.finish_running_run(report.run_id, status="failed", detail=brief_error(exc))
        raise
```

在 `sink.write()` 成功后，通过单一持久化方法同时写入累计行数、批次数与水位游标：

```python
rows += written_rows
batches += 1
# watermark 为 None 的全量快照表只更新 step；增量表同时推进可恢复游标。
landing.record_sync_batch_progress(
    step_id=current_step,
    source=source,
    table=info.name,
    watermark_col=wm_col,
    watermark=last_wm if wm_col else None,
    key_values=last_cursor_keys if wm_col else None,
    force_cursor=False,
    rows_in=rows,
    rows_out=rows,
    batches=batches,
    batch_id=table_batch_id,
    progressed_at=_now(),
)
```

异常、暂停、完成时仍沿用现有终态更新，并写入最终累计值。不得为了显示进度把每一行都写一次
SQLite；进度粒度固定为“一个成功批次一次”，与当前 `rate.batch_size` 保持一致。

---

### 4. `data2agent/middle_admin/templates/status.html` — 前端改为异步+轮询

```javascript
document.getElementById('btn-trigger').onclick = async function () {
  var el = document.getElementById('trigger-result');
  el.textContent = '触发中…';
  try {
    var r = await fetch('/api/actions/trigger', {
      method: 'POST', headers: authHeaders(),
      body: JSON.stringify({action: 'sync'})
    });
    var body = await r.json();
    if (body.status === 'started') {
      el.textContent = '同步已启动 (运行 #' + body.run_id + ')';
      watchRun(body.run_id); // 轮询 /api/runs/{run_id}，直至 ok / failed / paused
      htmx.trigger(document.getElementById('status-panel'), 'load');
    } else if (body.reason === 'already_running') {
      el.textContent = body.run_id
        ? '已有同步正在运行 (运行 #' + body.run_id + ')，正在查看进度'
        : '已有同步正在启动，请稍后刷新状态';
      if (body.run_id) watchRun(body.run_id);
    } else {
      // 窗口外 / 未配置等
      el.textContent = JSON.stringify(body);
    }
  } catch (e) {
    el.textContent = '请求失败: ' + e.message;
  }
};
```

状态页增加“当前运行 / 最近一次运行”摘要和可展开的运行历史；终态为 `ok`、`failed`、`paused`。
对 `running` run，`watchRun(run_id)` 每 2 秒请求一次详情，并显示：

- `正在同步 <target>（第 <ordinal> 张）`；
- `已处理 <rows_out> 行 / <batches> 批`；
- `最后进度时间 <progressed_at>`；
- 已完成步骤的水位及失败步骤的脱敏原因。

终态后立即停止 2 秒轮询，状态页恢复既有 5 秒摘要轮询。轮询请求必须捕获网络/JSON 错误、
在页面离开时取消定时器，且不渲染未经 `esc()` 处理的错误详情。首次尚未完成任何批次的表显示
“已启动，等待首批完成”，不能伪造 0 行完成进度。

---

## 验证方式

### 单元测试
- 新增 `test_middle_admin_runs`：验证 `/api/runs` 和 `/api/runs/{id}` 端点
- 新增 `test_async_trigger`：验证 `/actions/trigger` 立即返回 `run_id` 且不阻塞
- 新增 `test_sync_single_flight`：同 source 第二次手动触发，以及模拟常驻调度触发，均返回
  `already_running` 且不会新增第二条 run
- 新增 `test_precreated_run_lifecycle`：窗口外不建 run；executor 提交失败、adapter/sink/协议预检
  失败均将已创建 run 收敛为 `failed`，不遗留 `running`
- 新增 `test_run_api_contract`：分页边界、source 校验、列表排序、详情步骤顺序、错误脱敏
- 新增 `test_sync_step_progress`：在至少两批的同步中断点后断言运行中 step 的 `rows_out`、`batches`、
  `progressed_at` 已更新；最终 `ok / paused / failed` 的累计值与 run 汇总一致
- 新增 SQLite 迁移回归：旧版 `d2a_run_step` 数据库启动后可添加进度字段，旧 run 的空字段可被 API 正确返回

### 集成测试
1. 启动中间机管理：`python -m data2agent.middle_admin --home ...`
2. 打开状态页面 `http://127.0.0.1:8851/status`
3. 点击"立即触发同步"，确认不再一直显示"触发中"
4. 确认页面显示 `run_id` 和运行状态，状态面板自动刷新
5. 如果在窗口外，确认返回 `executed: false` 且有明确说明
6. 使用小 `batch_size` 制造多批同步，确认运行中每 2 秒可看到累计行数、批次数和最后进度时间递增

### 边界情况
- 触发时链接ERP失败 → run 记录 status=failed，前端能看到失败原因
- 平台 ingest 不可达 → 批次 status=failed，不阻塞其他表
- 窗口内/外 → 窗口外立即返回不执行
- tables_unconfigured → 立即返回，不创建 run
- connector 调度运行中手动点击 → 立即返回 `already_running` 与现有 `run_id`，不排队、不重复读 ERP
- 管理页进程在后台同步期间退出 → 操作系统释放 source 锁；下次启动可正常同步，旧 run 不得误报成功
- 首批尚未完成 → 页面显示“等待首批完成”，不把空值展示成完成 0 行
- 中途 sink 写入失败 → 页面仅显示最后一个已成功批次的累计值，当前步骤与 run 均进入 `failed`
