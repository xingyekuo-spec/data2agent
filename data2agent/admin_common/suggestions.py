"""统一错误文案:detail(发生了什么) + suggestion(建议操作)。"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


def with_suggestion(detail: str, suggestion: str) -> dict[str, str]:
    """HTTPException / JSON 错误体的标准形状。"""
    return {"detail": detail, "suggestion": suggestion}


def http_error(status: int, detail: str, suggestion: str, **extra: Any) -> HTTPException:
    body: dict[str, Any] = with_suggestion(detail, suggestion)
    body.update(extra)
    return HTTPException(status, body)


def field_error(field: str, message: str, suggestion: str) -> dict[str, str]:
    """`{ok:false, errors:[...]}` 中的单项。"""
    return {"field": field, "message": message, "suggestion": suggestion}


def format_operator_message(detail: str, suggestion: str | None) -> str:
    """日志或纯文本场景:把建议拼进同一行。"""
    detail = (detail or "").strip()
    suggestion = (suggestion or "").strip()
    if not suggestion:
        return detail
    if not detail:
        return f"建议：{suggestion}"
    return f"{detail} 建议：{suggestion}"


_CHECK_SUGGESTIONS: dict[str, str] = {
    "key_missing": "填写存在的业务键/主键列后重试",
    "key_not_unique": "更换唯一键组合，或先清洗源表重复/空值",
    "key_check_failed": "核对表权限与键列，查看日志后重试",
    "watermark_missing": "选择存在的水位列（通常为更新时间类字段）",
    "watermark_invalid": "更换合适的水位列，或确认字段类型适合增量抽取",
    "timeout": "增大校验超时，或在业务低峰重试",
    "permission_denied": "为只读账号授予该表 SELECT 权限后重试",
    "connection_failed": "先在「配置」页测试数据库连接后再校验",
}


def suggestion_for_check(code: str | None) -> str | None:
    if not code or code == "ready":
        return None
    return _CHECK_SUGGESTIONS.get(code, "根据错误详情修正后重试")


_CONN_SUGGESTIONS: dict[str, str] = {
    "auth": "核对 secrets.env 中的只读账号密码，确认账号允许从本机登录",
    "timeout": "检查中间机到 ERP 数据库的网络与防火墙，必要时增大超时",
    "missing_dsn": "在 config/secrets.env 写入对应 DSN 环境变量后重启管理进程",
    "unsupported": "连接测试仅支持 mssql_readonly；请确认 sources.*.adapter 配置",
}


def suggestion_for_connection(error: str | None) -> str:
    if not error:
        return "查看管理界面日志中的脱敏错误后重试"
    return _CONN_SUGGESTIONS.get(
        error, "核对数据库连通与凭据，查看管理界面日志后重试")
