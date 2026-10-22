"""Small, versioned contracts shared by storage, workers and the desktop UI."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

PROTOCOL_VERSION = 1
PROJECT_SCHEMA_VERSION = 1
TRANSACTION_SCHEMA_VERSION = 1
ACTIVE_STATES = frozenset({'queued', 'running', 'cancel_requested', 'committing'})
TERMINAL_STATES = frozenset({'succeeded', 'cancelled', 'failed', 'interrupted'})


def new_id() -> str:
    return uuid4().hex


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class TraceAtlasError(Exception):
    def __init__(self, message: str, code: str = 'ERROR', stage: str = '',
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.stage = stage
        self.retryable = retryable
        self.details = details or {}
        self.diagnostic_id = new_id()

    def to_dict(self) -> dict:
        return {'message': self.message, 'code': self.code, 'stage': self.stage,
                'retryable': self.retryable, 'details': self.details,
                'diagnostic_id': self.diagnostic_id}


def contained_path(root: Path, relative: str) -> Path:
    """Reject traversal, absolute paths and symlinks escaping a project boundary."""
    root = Path(root).resolve()
    value = Path(relative)
    if value.is_absolute() or not relative or ':' in relative or '..' in value.parts:
        raise TraceAtlasError('文件路径超出了项目目录。', 'INVALID_PATH', 'validation')
    path = (root / value).resolve()
    if not path.is_relative_to(root) or path == root:
        raise TraceAtlasError('文件路径超出了项目目录。', 'INVALID_PATH', 'validation')
    return path


@dataclass(frozen=True)
class SourceLocator:
    source_id: str
    sheet: str | None = None
    row: int | None = None
    page: int | None = None
    table: str | None = None
    record_key: str | None = None
    column: str | None = None
    region: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class RecordProvenance:
    record_id: str
    dataset_version: str
    source_refs: tuple[SourceLocator, ...]

    def to_dict(self) -> dict:
        if not self.source_refs:
            raise TraceAtlasError('记录必须至少保留一项来源。', 'MISSING_PROVENANCE')
        return asdict(self)


@dataclass(frozen=True)
class ResultEnvelope:
    result_id: str
    task_id: str
    attempt_id: str
    feature_id: str
    algorithm_version: str
    dataset_versions: tuple[str, ...]
    parameter_snapshot: dict[str, Any]
    completion: str = 'complete'
    tables: tuple[dict, ...] = ()
    graphs: tuple[dict, ...] = ()
    metrics: dict = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    provenance_ref: str | None = None
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict:
        if self.completion not in {'complete', 'partial', 'limited'}:
            raise TraceAtlasError('结果完成状态不受支持。', 'INVALID_COMPLETION')
        return asdict(self)


@dataclass(frozen=True)
class Entity:
    entity_id: str
    entity_type: str
    namespace: str
    business_key: str
    attributes: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    def to_dict(self) -> dict:
        if not all((self.entity_id, self.entity_type, self.namespace, self.business_key)):
            raise TraceAtlasError('实体必须包含稳定标识、类型、命名空间和原始业务标识。', 'INVALID_ENTITY')
        return asdict(self)


@dataclass(frozen=True)
class Relation:
    relation_id: str
    from_entity_id: str
    to_entity_id: str
    relation_type: str
    record_refs: tuple[RecordProvenance, ...]
    attributes: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    def to_dict(self) -> dict:
        if not all((self.relation_id, self.from_entity_id, self.to_entity_id, self.relation_type, self.record_refs)):
            raise TraceAtlasError('关系必须保留方向、类型和参与的事实记录。', 'INVALID_RELATION')
        for reference in self.record_refs:
            reference.to_dict()
        return asdict(self)


@dataclass(frozen=True)
class Transform:
    transform_id: str
    rule_id: str
    rule_version: str
    input_refs: tuple[str, ...]
    output_refs: tuple[str, ...]
    parameters: dict[str, Any]
    parameter_schema_version: int = 1
    schema_version: int = 1
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict:
        if not all((self.transform_id, self.rule_id, self.rule_version, self.input_refs, self.output_refs)):
            raise TraceAtlasError('变换必须固定规则版本及完整的输入输出引用。', 'INVALID_TRANSFORM')
        return asdict(self)


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    project_id: str
    attempt_id: str
    feature_id: str
    module_version: str
    input_dataset_versions: tuple[str, ...]
    parameters: dict[str, Any]
    selection: dict | list = field(default_factory=dict)
    resource_limits: dict = field(default_factory=lambda: {'memory_bytes': 4 * 1024**3, 'cancel_timeout_seconds': 10})
    parameter_schema_version: int = 1
    protocol_version: int = PROTOCOL_VERSION
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict:
        if not all((self.task_id, self.project_id, self.attempt_id, self.feature_id, self.module_version)):
            raise TraceAtlasError('任务标识或规则版本缺失。', 'INVALID_TASK_SPEC')
        if self.protocol_version != PROTOCOL_VERSION or self.parameter_schema_version != 1:
            raise TraceAtlasError('任务协议或参数格式版本不受支持。', 'TASK_SCHEMA_UNSUPPORTED')
        return asdict(self)
