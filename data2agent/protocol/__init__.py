"""跨机契约层:中间端与平台端之间唯一允许共享的"接口"代码。

约束(由架构分层规则强制):

- 本包不得 import ``data2agent`` 中的任何端目录(middle / platform)或其他兄弟模块;
- 除 pydantic / 标准库外不得引入第三方依赖(中间端与平台端部署环境不同);
- 协议变更须同步更新 ``deploy/ingest_protocol_compat.json`` 兼容门禁。
"""

from .ingest import (
    INGEST_PROTOCOL_VERSION,
    LEGACY_HEALTH_INGEST_PROTOCOL_VERSION,
    SUPPORTED_INGEST_PROTOCOL_VERSIONS,
    BatchBody,
    TableAbortBody,
    TableBeginBody,
    TableCompleteBody,
    health_protocol_fields,
    is_supported_protocol,
)

__all__ = [
    "INGEST_PROTOCOL_VERSION",
    "LEGACY_HEALTH_INGEST_PROTOCOL_VERSION",
    "SUPPORTED_INGEST_PROTOCOL_VERSIONS",
    "BatchBody",
    "TableAbortBody",
    "TableBeginBody",
    "TableCompleteBody",
    "health_protocol_fields",
    "is_supported_protocol",
]
