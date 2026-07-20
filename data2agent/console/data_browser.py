"""数据浏览内核(M4):白名单目录、稳定排序、业务键搜索、字段分类、
服务端脱敏与 JSON-safe 序列化。

安全纪律(契约先于动态 SQL):
- source/table/object 只做精确成员校验;标识符统一经 quote_ident 引用,
  值一律参数绑定;不接受 SQL、客户端列清单、任意 ORDER BY 或跨表 join;
- 资源不存在返回 404(不伪装空数组),非法参数 422;
- 对象敏感属性与 raw 已知敏感列永久服务端脱敏为 ***(v0.2 不提供 unmask);
- 无法分类的 raw 列标 unknown 并持续警告,不能默认为安全;
- BLOB/超大值具名安全转换并在 truncations 列明,不能静默裁剪。
"""

from __future__ import annotations

import base64
import math
import re
import sqlite3
from datetime import datetime
from typing import Any

from ..connect.landing import LandingStore
from ..metamodel.schema import ObjectTemplate, TemplatePack
from . import observability as obs

VALUE_BUDGET_BYTES = 64 * 1024
Q_MAX_LEN = 200
MASKED = "***"

_META_PREFIX = "_d2a_"


class BrowseError(Exception):
    """浏览层错误:status + 人话原因(不含 SQL/敏感值)。"""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def quote_ident(name: str) -> str:
    """标识符安全引用(双引号 + 加倍)。调用前必须已过目录精确校验。"""
    return '"' + name.replace('"', '""') + '"'


# ---- JSON-safe 值转换 ----


def json_safe(value: Any) -> tuple[Any, bool]:
    """(安全值, 是否截断)。

    bytes → 具名二进制描述(受限 base64 预览);非有限 float → 字符串;
    超长字符串 → 预算内预览 + truncated;datetime → ISO;其余原样。
    """
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str) and len(value.encode("utf-8", "ignore")) > VALUE_BUDGET_BYTES:
            raw = value.encode("utf-8", "ignore")[:VALUE_BUDGET_BYTES]
            return raw.decode("utf-8", "ignore") + "…[已截断]", True
        return value, False
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return str(value), False
        return value, False
    if isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
        preview = base64.b64encode(data[:64]).decode("ascii")
        return {
            "__blob__": True,
            "bytes": len(data),
            "encoding": "base64",
            "preview": preview,
            "truncated": len(data) > 64,
        }, len(data) > 64
    if isinstance(value, datetime):
        return value.isoformat(), False
    return str(value), False


# ---- 目录与白名单 ----


def sources_from_pack(pack: TemplatePack) -> list[str]:
    """模板 enabled binding 中声明过的源;用于无 config 的只读测试/展厅上下文。"""
    return sorted({
        binding.source
        for tpl in pack.objects
        for binding in tpl.bindings
        if binding.enabled
    })


def allowed_sources(pack: TemplatePack, cfg_sources: list[str]) -> list[str]:
    """可浏览源:优先当前配置源;无 config 时回退模板声明源,绝不从 DB 反推。"""
    return sorted(set(cfg_sources or sources_from_pack(pack)))


def configured_sources(cfg_sources: list[str], db: LandingStore) -> list[str]:
    """兼容旧调用:只返回显式配置源,不再把落地库 raw_* 当白名单。"""
    del db
    return sorted(set(cfg_sources))


def require_source(cfg_sources: list[str], db: LandingStore, source: str) -> None:
    if source not in configured_sources(cfg_sources, db):
        raise BrowseError(404, f"未知或不允许的数据源 '{source}'")


def raw_tables(db: LandingStore, source: str) -> list[str]:
    """该源实际存在的 raw 业务表(逻辑名;不含 SQLite 内部表)。"""
    prefix = f"raw_{source}__%"
    return sorted(
        r[0][len(f"raw_{source}__"):]
        for r in db.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ?",
            (prefix,)))


def allowed_raw_tables(pack: TemplatePack, source: str) -> list[str]:
    return sorted({
        table
        for tpl in pack.objects
        for binding in tpl.bindings
        if binding.enabled and binding.source == source
        for table in binding.tables
    })


def require_raw_table(db: LandingStore, source: str, table: str,
                      allowed_tables: list[str] | None = None) -> None:
    if allowed_tables is not None and table not in allowed_tables:
        raise BrowseError(404, f"表 '{table}' 不存在或不在 '{source}' 的 raw 目录")
    if table not in raw_tables(db, source):
        raise BrowseError(404, f"表 '{table}' 不存在或不在 '{source}' 的 raw 目录")


def physical_raw(source: str, table: str) -> str:
    return f"raw_{source}__{table}"


def physical_object(obj: str) -> str:
    return f"obj_{obj}"


def table_exists(db: LandingStore, physical: str) -> bool:
    return db.con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (physical,)).fetchone() is not None


def _pk_columns(db: LandingStore, physical: str) -> list[str]:
    """DDL 声明的主键列(PRAGMA table_info 的 pk 位序)。"""
    rows = db.con.execute(f"PRAGMA table_info({quote_ident(physical)})").fetchall()
    return [r["name"] for r in sorted(
        (r for r in rows if r["pk"] > 0), key=lambda r: r["pk"])]


def _business_key_columns(db: LandingStore, pack: TemplatePack,
                          source: str, table: str) -> set[str]:
    """raw 业务键列:优先 binding key_map(配置映射能确定的源主键);
    无 key_map 时回退 DDL 声明主键;再无则不可搜索。"""
    keys: set[str] = set()
    prefix = f"{table}."
    for tpl in pack.objects:
        for binding in tpl.bindings:
            if not binding.enabled or binding.source != source:
                continue
            for _prop, expr in binding.key_map.items():
                if isinstance(expr, str) and expr.startswith(prefix):
                    keys.add(expr[len(prefix):].split(" ")[0])
    if keys:
        return keys
    return set(_pk_columns(db, physical_raw(source, table)))


def _all_columns(db: LandingStore, physical: str) -> list[tuple[str, str]]:
    rows = db.con.execute(f"PRAGMA table_info({quote_ident(physical)})").fetchall()
    return [(r["name"], (r["type"] or "TEXT").upper()) for r in rows]


# ---- 列分类(raw)----


def _sensitive_columns(pack: TemplatePack, source: str, table: str) -> dict[str, bool]:
    """binding key_map/field_map 反查:raw 列 → 是否对应敏感对象属性。"""
    out: dict[str, bool] = {}
    prefix = f"{table}."
    for tpl in pack.objects:
        sensitive = {p.name for p in tpl.properties if p.sensitive}
        for binding in tpl.bindings:
            if not binding.enabled or binding.source != source:
                continue
            for prop, expr in [*binding.key_map.items(), *binding.field_map.items()]:
                if not isinstance(expr, str) or not expr.startswith(prefix):
                    continue
                col = expr[len(prefix):].split(" ")[0]  # 去掉 join 等附加说明
                out[col] = out.get(col, False) or prop in sensitive
    return out


def raw_column_meta(db: LandingStore, pack: TemplatePack,
                    source: str, table: str) -> list[dict[str, Any]]:
    """raw 列元数据:role / classification / masked / searchable。

    业务键(优先 key_map,回退 DDL 主键)→ business_key;_d2a_* → metadata;
    binding 可定位 → normal/sensitive;无法定位 → unknown(持续警告)。
    """
    physical = physical_raw(source, table)
    biz_keys = _business_key_columns(db, pack, source, table)
    sensitive_map = _sensitive_columns(pack, source, table)
    cols = []
    for name, data_type in _all_columns(db, physical):
        is_sensitive = sensitive_map.get(name, False)
        if name in biz_keys:
            role = "business_key"
            classification = "sensitive" if is_sensitive else "normal"
            searchable = not is_sensitive
        elif name.startswith(_META_PREFIX):
            role, classification, searchable = "metadata", "normal", False
        else:
            role, searchable = "data", False
            if name in sensitive_map:
                classification = "sensitive" if is_sensitive else "normal"
            else:
                classification = "unknown"
        cols.append({
            "name": name,
            "data_type": data_type,
            "role": role,
            "classification": classification,
            "masked": classification == "sensitive",
            "searchable": searchable,
        })
    return cols


def object_column_meta(db: LandingStore, tpl: ObjectTemplate) -> list[dict[str, Any]]:
    """对象列元数据:keys → business_key;敏感属性 → sensitive(脱敏)。"""
    physical = physical_object(tpl.object)
    actual = {name for name, _ in _all_columns(db, physical)}
    prop_meta = {p.name: p for p in tpl.properties}
    keys = set(tpl.keys)
    cols = []
    for name, data_type in _all_columns(db, physical):
        prop = prop_meta.get(name)
        is_sensitive = prop is not None and prop.sensitive
        if name in keys:
            role = "business_key"
            classification = "sensitive" if is_sensitive else "normal"
            searchable = not is_sensitive
        elif name.startswith(_META_PREFIX):
            role, classification, searchable = "metadata", "normal", False
        else:
            role, searchable = "data", False
            classification = (
                "sensitive" if is_sensitive
                else "normal" if prop is not None else "unknown")
        if name not in actual:  # pragma: no cover - 防御
            continue
        cols.append({
            "name": name,
            "data_type": data_type,
            "role": role,
            "classification": classification,
            "masked": classification == "sensitive",
            "searchable": searchable,
        })
    return cols


# ---- raw 目录 ----


def raw_catalog(db: LandingStore, pack: TemplatePack,
                cfg_sources: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """raw 目录(items, warnings);计数失败为 null + 警告,不伪装 0。"""
    items: list[dict[str, Any]] = []
    warnings: list[str] = []
    for source in configured_sources(cfg_sources, db):
        table_whitelist = set(allowed_raw_tables(pack, source))
        for table in raw_tables(db, source):
            if table not in table_whitelist:
                continue
            physical = physical_raw(source, table)
            rows: int | None = None
            latest_batch: str | None = None
            extracted: datetime | None = None
            try:
                row = db.con.execute(
                    f'SELECT COUNT(*) AS n, MAX("_d2a_extracted_at") AS m '
                    f'FROM {quote_ident(physical)} WHERE _d2a_deleted_at IS NULL'
                ).fetchone()
                rows = row["n"]
                if row["m"]:
                    extracted = obs.aware(row["m"])
                batch_row = db.con.execute(
                    f'SELECT "_d2a_batch_id" AS b, "_d2a_extracted_at" AS e '
                    f'FROM {quote_ident(physical)} ORDER BY e DESC LIMIT 1'
                ).fetchone()
                latest_batch = batch_row["b"] if batch_row else None
            except sqlite3.Error as e:
                warnings.append(f"{source}.{table}: 计数查询失败({e})")
            cols = raw_column_meta(db, pack, source, table)
            has_unknown = any(c["classification"] == "unknown" for c in cols)
            items.append({
                "source": source,
                "table": table,
                "display_name": table,
                "rows": rows,
                "latest_batch_id": latest_batch,
                "extracted_at": extracted,
                "searchable": any(c["searchable"] for c in cols),
                "classification_warning": has_unknown,
            })
    return items, warnings


# ---- 通用分页浏览 ----


def _sort_clause(db: LandingStore, physical: str) -> tuple[str, str]:
    """稳定排序子句与说明:声明主键优先,仅框架表允许 rowid 兜底。"""
    pk = _pk_columns(db, physical)
    if pk:
        cols = ", ".join(quote_ident(c) for c in pk)
        return f" ORDER BY {cols}", "pk:" + ",".join(pk)
    return " ORDER BY rowid", "rowid"


def _search_clause(q: str, key_cols: list[str]) -> tuple[str, list[Any]]:
    """业务键搜索子句(参数化 + LIKE 通配转义)。"""
    if not q:
        return "", []
    if len(q) > Q_MAX_LEN:
        raise BrowseError(422, f"搜索值超长(上限 {Q_MAX_LEN} 字符)")
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    likes = " OR ".join(
        f"CAST({quote_ident(c)} AS TEXT) LIKE ? ESCAPE '\\'" for c in key_cols)
    return f" AND ({likes})", [f"%{escaped}%"] * len(key_cols)


# ---- 隔离 raw 脱敏(M5-T03)----


def _quarantine_sensitive_cols(pack: TemplatePack, source: str,
                               object_name: str) -> set[str] | None:
    """从模板反查:隔离对象的 raw 字典中,哪些列名对应敏感属性。

    返回 None 表示对象不在模板中(完全未知) —— 调用方应 mask 全部值。
    """
    tpl = next((o for o in pack.objects if o.object == object_name), None)
    if tpl is None:
        return None
    sensitive_props = {p.name for p in tpl.properties if p.sensitive}
    if not sensitive_props:
        return set()
    sensitive_cols: set[str] = set()
    found_binding = False
    for binding in tpl.bindings:
        if not binding.enabled or binding.source != source:
            continue
        found_binding = True
        for table in binding.tables:
            prefix = f"{table}."
            for prop, expr in [*binding.key_map.items(), *binding.field_map.items()]:
                if not isinstance(expr, str) or not expr.startswith(prefix):
                    continue
                if prop not in sensitive_props:
                    continue
                col = expr[len(prefix):].split(" ")[0]
                sensitive_cols.add(col)
    if not found_binding:
        return None  # 无可靠 binding → 调用方应全量遮罩
    return sensitive_cols


def _sanitize_object_keys(pack: TemplatePack | None, source: str,
                          object_name: str,
                          keys_dict: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """对隔离记录的 keys 字典做脱敏:敏感键值 → MASKED,其余经过 json_safe。

    pack=None 或对象不在模板中 → 全部 mask。
    对象已知但 source 无已启用 binding → 全部 mask(与 raw 脱敏一致,防止未知来源泄露业务键)。
    返回 (sanitized_keys, truncated_field_names)。
    """
    if pack is not None:
        tpl = next((o for o in pack.objects if o.object == object_name), None)
    else:
        tpl = None
    if tpl is None:
        return {k: MASKED for k in keys_dict}, []
    # 检查是否有已启用的 binding 对应此 source
    has_binding = any(
        b.enabled and b.source == source for b in tpl.bindings)
    if not has_binding:
        # 已知对象但来源无可靠 binding → 全量遮罩
        return {k: MASKED for k in keys_dict}, []
    sensitive_key_names = {p.name for p in tpl.properties
                           if p.name in tpl.keys and p.sensitive}
    out: dict[str, Any] = {}
    truncated: list[str] = []
    for key, value in keys_dict.items():
        if key in sensitive_key_names:
            out[key] = MASKED
        else:
            safe, was_truncated = json_safe(value)
            out[key] = safe
            if was_truncated:
                truncated.append(key)
    return out, truncated


_EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')
_REASON_MAX_LEN = 512


def _sanitize_quarantine_reason(reason: str | None, pack: TemplatePack | None,
                                 source: str, object_name: str) -> str:
    """对隔离原因做安全脱敏:屏蔽邮箱,按 field_map/key_map 字段名正则屏蔽
    ``FIELD=VALUE`` 模式;长度预算 512 字符。

    与 safe_error_summary 不同,本函数负责去除映射错误原因中可能泄露的
    业务敏感值(如邮箱地址、来源字段值)。调用方不应再将返回值传入
    safe_error_summary(已内置空白压缩)。

    无可靠 binding(对象不在模板或无已启用绑定,或 binding 无 field_map)
    返回固定通用摘要。
    """
    if not reason or not isinstance(reason, str) or not reason.strip():
        return ""
    # 检查是否有已启用的 binding 对应此 source+object
    enabled_binding = None
    tpl = None
    if pack is not None:
        tpl = next((o for o in pack.objects if o.object == object_name), None)
        if tpl is not None:
            for b in tpl.bindings:
                if b.enabled and b.source == source:
                    enabled_binding = b
                    break
    if enabled_binding is None or not enabled_binding.field_map:
        return "映射失败（详情请查看隔离 raw 预览）"
    # 从 field_map/key_map 表达式中提取所有字段名,用于正则屏蔽 FIELD=VALUE 模式
    field_names: set[str] = set()
    from ..mapping import parse_field_expr  # noqa: PLC0415 延迟导入避免循环
    for expr_str in [*enabled_binding.key_map.values(),
                     *enabled_binding.field_map.values()]:
        if not isinstance(expr_str, str):
            continue
        try:
            fexpr = parse_field_expr(expr_str)
            if fexpr.column:
                field_names.add(fexpr.column.upper())
        except ValueError:
            # 解析失败时,尝试从表达式字符串中提取列名作为备选
            if "." in expr_str:
                col = expr_str.split(".", 1)[1].split(" ", 1)[0].strip()
                if col:
                    field_names.add(col.upper())
    # 也添加模板声明的业务键列名
    if tpl is not None:
        for k in tpl.keys:
            field_names.add(k.upper())
    # 屏蔽: 邮箱 -> FIELD=VALUE 模式(正则,无长度下限,不遗漏单字符枚举值)
    sanitized = _EMAIL_RE.sub("[email]", reason)
    if field_names:
        names_pattern = "|".join(sorted(field_names, key=len, reverse=True))
        # 屏蔽形如 FIELD=VALUE 或 FIELD = VALUE 的模式
        sanitized = re.sub(
            rf'\b({names_pattern})\s*=\s*\S+',
            r'\1=[masked]',
            sanitized,
            flags=re.IGNORECASE,
        )
        # 同时屏蔽 dict repr 格式: 'FIELD': 'VALUE' / 'FIELD': VALUE
        sanitized = re.sub(
            rf"'({names_pattern})'\s*:\s*'?\S+'?",
            r"'\1': '[masked]'",
            sanitized,
            flags=re.IGNORECASE,
        )
    # 长度预算
    if len(sanitized) > _REASON_MAX_LEN:
        sanitized = sanitized[:_REASON_MAX_LEN] + "..."
    # 压缩空白(同 safe_error_summary)
    cleaned = " ".join(sanitized.split())
    return cleaned if cleaned else ""


def sanitize_quarantine_raw(raw_dict: dict[str, Any], pack: TemplatePack | None,
                            source: str, object_name: str,
                            ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """隔离 raw_json 脱敏:敏感列 → MASKED,其余 json_safe 并跟踪截断。

    返回 (sanitized_dict | None, truncations)。
    raw_dict 非 dict 时返回 (None, []),由调用方添加警告。
    """
    if not isinstance(raw_dict, dict) or not raw_dict:
        return None, []
    if pack is not None:
        sensitive_cols = _quarantine_sensitive_cols(pack, source, object_name)
    else:
        sensitive_cols = None  # 无模板 → 完全未知,全部 mask
    out: dict[str, Any] = {}
    truncated_fields: list[str] = []
    for key, value in raw_dict.items():
        if sensitive_cols is None:
            out[key] = MASKED
        elif key in sensitive_cols:
            out[key] = MASKED
        else:
            safe, was_truncated = json_safe(value)
            out[key] = safe
            if was_truncated:
                truncated_fields.append(key)
    truncations = [{"row_index": 0, "fields": truncated_fields}] if truncated_fields else []
    return out, truncations


def browse_table(
    db: LandingStore,
    physical: str,
    columns: list[dict[str, Any]],
    *,
    limit: int,
    offset: int,
    q: str,
    base_where: str | None = None,
    base_params: list[Any] | None = None,
) -> dict[str, Any]:
    """统一分页浏览:稳定排序、业务键搜索、脱敏、JSON-safe、截断标记。"""
    if not 1 <= limit <= 100 or offset < 0:
        raise BrowseError(422, "limit 须为 1..100,offset 须 >= 0")
    key_cols = [c["name"] for c in columns if c["searchable"]]
    masked_cols = {c["name"] for c in columns if c["masked"]}
    conditions: list[str] = []
    params: list[Any] = list(base_params or [])
    if base_where:
        conditions.append(base_where)
    if q:
        if not key_cols:
            raise BrowseError(422, "该资源没有可搜索的业务键")
        search_sql, search_params = _search_clause(q, key_cols)
        conditions.append(search_sql.removeprefix(" AND "))
        params.extend(search_params)
    where_sql = (
        " WHERE " + " AND ".join(f"({c})" for c in conditions)) if conditions else ""
    (total,) = db.con.execute(
        f"SELECT COUNT(*) FROM {quote_ident(physical)}{where_sql}", params).fetchone()
    order_sql, sort_desc = _sort_clause(db, physical)
    col_names = [c["name"] for c in columns]
    select_cols = ", ".join(quote_ident(c) for c in col_names)
    rows = db.con.execute(
        f"SELECT {select_cols} FROM {quote_ident(physical)}{where_sql}"
        f"{order_sql} LIMIT ? OFFSET ?",
        [*params, limit, offset]).fetchall()
    out_rows: list[dict[str, Any]] = []
    truncations: list[dict[str, Any]] = []
    for i, r in enumerate(rows):
        row_out: dict[str, Any] = {}
        truncated_fields: list[str] = []
        for name in col_names:
            if name in masked_cols:
                row_out[name] = MASKED
                continue
            safe, truncated = json_safe(r[name])
            row_out[name] = safe
            if truncated:
                truncated_fields.append(name)
        if truncated_fields:
            truncations.append({"row_index": offset + i, "fields": truncated_fields})
        out_rows.append(row_out)
    return {
        "rows": out_rows,
        "truncations": truncations,
        "total": total,
        "sort": sort_desc,
        "searchable": bool(key_cols),
    }
