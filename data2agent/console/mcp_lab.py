"""MCP Lab 控制台辅助:安全错误映射与建议卡入口(说档)。"""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi.responses import JSONResponse

from .contracts import McpLabError, McpLabReasonCode

# 错误正文不得包含路径/Token/traceback;用短摘要替换具体异常文本中的敏感片段。
_PATH_RE = re.compile(r"(?:/Users|/home|/var|/tmp|[A-Za-z]:\\)[^\s\"']+")
_TOKEN_RE = re.compile(r"(?i)(bearer\s+)\S+")


def _safe_detail(message: str, *, fallback: str) -> str:
    text = (message or "").strip() or fallback
    text = _PATH_RE.sub("<path>", text)
    text = _TOKEN_RE.sub(r"\1***", text)
    if "Traceback" in text or "traceback" in text:
        return fallback
    return text[:300]


def classify_mcp_error(exc: BaseException) -> tuple[int, McpLabReasonCode, str, bool]:
    """将 QueryService/网关异常映射为 (status, reason_code, detail, retryable)。"""
    msg = str(exc)
    if "尚未物化" in msg:
        return 409, "not_materialized", "对象层尚未物化,请先完成 sync/apply", False
    if "未知对象" in msg or "未知指标" in msg:
        return 404, "unknown_target", _safe_detail(msg, fallback="未知查询目标"), False
    if "无法溯源" in msg or "不是本会话的查询" in msg:
        return 409, "query_expired", "query ID 已失效或不在当前 Console 进程内", False
    if "档位上限" in msg or "超出本网关档位" in msg:
        return 403, "tier_forbidden", "动作档位超出当前部署上限", False
    if any(k in msg for k in (
        "取值须为", "未知筛选", "未知排序", "conclusion", "evidence",
        "不能为空", "支持的 group_by", "未声明动作", "filters 须为",
        "参数无效", "unexpected keyword", "got an unexpected",
    )):
        return 422, "invalid_params", _safe_detail(msg, fallback="查询或建议卡参数无效"), False
    if isinstance(exc, TypeError):
        return 422, "invalid_params", "查询或建议卡参数类型无效", False
    if "rate" in msg.lower() and "limit" in msg.lower():
        return 429, "rate_limited", "MCP 调用过于频繁,请稍后重试", True
    return 500, "execution_failed", "MCP Lab 执行失败", False


def mcp_lab_error_response(
    exc: BaseException, *, tool: str | None,
) -> JSONResponse:
    status, reason, detail, retryable = classify_mcp_error(exc)
    error_id = uuid.uuid4().hex[:12] if reason == "execution_failed" else None
    if reason == "execution_failed":
        detail = f"{detail}(error_id={error_id})"
    body = McpLabError(
        detail=detail,
        reason_code=reason,
        tool=tool,
        retryable=retryable,
        error_id=error_id,
    )
    return JSONResponse(status_code=status, content=body.model_dump())


def proposal_response_from_service(card: dict[str, Any]) -> dict[str, Any]:
    """规范化 propose_action 返回,保证 at 为带时区可解析值。"""
    from datetime import datetime

    at = card.get("at")
    if isinstance(at, str):
        try:
            parsed = datetime.fromisoformat(at)
            if parsed.tzinfo is None:
                parsed = parsed.astimezone()
            card = {**card, "at": parsed}
        except ValueError:
            card = {**card, "at": datetime.now().astimezone()}
    for ev in card.get("evidence") or []:
        q = ev.get("query") or {}
        q_at = q.get("at")
        if isinstance(q_at, str):
            try:
                parsed = datetime.fromisoformat(q_at)
                if parsed.tzinfo is None:
                    parsed = parsed.astimezone()
                q["at"] = parsed
                ev["query"] = q
            except ValueError:
                pass
    return card
