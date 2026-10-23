"""Small Qt helpers. Business operations live in application services."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QRunnable, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QMessageBox, QTableView, QWidget

from traceatlas.contracts import TraceAtlasError


def destination_snapshot(path: Path) -> tuple[int, ...] | None:
    """Remember the target accepted in the save dialog before background work starts."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise TraceAtlasError("保存目标不是普通文件，请选择其他位置。", "INVALID_EXPORT_DESTINATION")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def copy_error_report(
    source: Path, destination: Path, project_root: Path, expected: tuple[int, ...] | None
) -> None:
    """Publish a complete report without damaging project files or an unconfirmed target."""
    project_root = project_root.resolve()
    destination = destination.resolve()
    if destination.is_relative_to(project_root) and not destination.is_relative_to(project_root / "exports"):
        raise TraceAtlasError(
            "请保存到项目的 exports 目录或项目之外，避免覆盖项目内部数据。", "INVALID_EXPORT_DESTINATION"
        )
    if not destination.parent.is_dir():
        raise TraceAtlasError("保存目录不存在，请重新选择位置。", "EXPORT_DIRECTORY")
    if destination_snapshot(destination) != expected:
        raise TraceAtlasError("目标文件已被其他程序更改，请重新选择保存位置。", "EXPORT_CONFLICT")
    temporary = None
    try:
        with source.open("rb") as reader, tempfile.NamedTemporaryFile(
            mode="wb", dir=destination.parent, prefix=f".{destination.name}.traceatlas-", suffix=".tmp", delete=False
        ) as writer:
            temporary = Path(writer.name)
            shutil.copyfileobj(reader, writer, 1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        if destination_snapshot(destination) != expected:
            raise TraceAtlasError("目标文件在复制期间被其他程序更改，请重新选择保存位置。", "EXPORT_CONFLICT")
        if expected is not None:
            os.replace(temporary, destination)
        elif os.name == "nt":
            os.rename(temporary, destination)  # Windows refuses a concurrently created target.
        else:
            os.link(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def display(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


EMPTY_INDEX = QModelIndex()
ENUM_LABELS = {
    "platform": {"bank": "银行", "wechat": "微信", "alipay": "支付宝", "unknown": "未指定"},
    "account_namespace": {
        "account": "账户",
        "bank_card": "银行卡号",
        "customer_account": "客户账号",
        "payment_account": "支付账号",
    },
    "direction": {"in": "收入", "out": "支出", "unknown": "未知"},
    "currency": {"unknown": "未知"},
    "timezone": {"unknown": "未知", "Asia/Shanghai": "北京时间"},
    "time_precision": {"day": "日期", "second": "秒", "millisecond": "毫秒", "unknown": "未知"},
}


NUMERIC_TYPES = ("integer", "decimal", "number", "int", "int64", "bigint")


def is_numeric(column: dict) -> bool:
    kind = str(column.get("type", "")).lower()
    return kind in NUMERIC_TYPES or kind.startswith("decimal(")


class TableModel(QAbstractTableModel):
    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.columns: list[dict] = []
        self.rows: list[dict] = []
        self.mono_family = ""

    def replace(self, page: dict) -> None:
        self.beginResetModel()
        self.columns = page.get("columns", [])
        self.rows = page.get("rows", [])
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = EMPTY_INDEX) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = EMPTY_INDEX) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            key = self.columns[index.column()]["key"]
            value = self.rows[index.row()].get(key)
            return ENUM_LABELS.get(key, {}).get(str(value), display(value))
        if role == Qt.ItemDataRole.TextAlignmentRole and is_numeric(self.columns[index.column()]):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if (
            role == Qt.ItemDataRole.FontRole
            and self.mono_family
            and is_numeric(self.columns[index.column()])
        ):
            font = QFont(self.mono_family)
            font.setStyleHint(QFont.StyleHint.Monospace)
            return font
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            column = self.columns[section]
            return column.get("label", column["key"])
        return section + 1


def make_table(parent: QWidget | None = None) -> tuple[QTableView, TableModel]:
    table = QTableView(parent)
    model = TableModel(table)
    table.setModel(model)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.setWordWrap(False)
    table.verticalHeader().setDefaultSectionSize(30)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    table.horizontalHeader().setDefaultSectionSize(150)
    table.setMinimumHeight(130)
    return table, model


class JobSignals(QObject):
    finished = Signal(object)
    failed = Signal(object)


class BackgroundCall(QRunnable):
    def __init__(self, function: Callable):
        super().__init__()
        self.function = function
        self.signals = JobSignals()

    def run(self):
        try:
            self.signals.finished.emit(self.function())
        except Exception as error:  # noqa: BLE001 - forward service errors across the Qt thread boundary
            self.signals.failed.emit(error)


def show_error(parent: QWidget, title: str, error: Exception | str) -> None:
    text = getattr(error, "message", None) or str(error) or type(error).__name__
    box = QMessageBox(QMessageBox.Icon.Warning, title, text, QMessageBox.StandardButton.Ok, parent)
    details = getattr(error, "details", None)
    if details:
        box.setDetailedText(display(details))
    box.exec()
