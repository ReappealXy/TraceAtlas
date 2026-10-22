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


class TableModel(QAbstractTableModel):
    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.columns: list[dict] = []
        self.rows: list[dict] = []

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
        if role == Qt.ItemDataRole.TextAlignmentRole:
            kind = str(self.columns[index.column()].get("type", "")).lower()
            if kind in ("integer", "decimal", "number", "int", "int64", "bigint") or kind.startswith(
                "decimal("
            ):
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
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


STYLE = """
QMainWindow, QDialog { background: #f5f6f7; }
QWidget { font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: 10pt; color: #243039; }
QMenuBar, QToolBar { background: #ffffff; border-bottom: 1px solid #d9dfe3; }
QToolBar { padding: 6px 8px; spacing: 8px; }
QToolButton { padding: 6px 9px; border-radius: 3px; }
QToolButton:hover { background: #e6f2ee; }
QLabel#pageTitle { font-size: 18pt; font-weight: 600; color: #183c35; }
QLabel#sectionTitle { font-size: 12pt; font-weight: 600; }
QLabel#muted { color: #596870; }
QLabel#notice { background: #fff4d6; border: 1px solid #e0ce96; padding: 9px; color: #654e13; }
QPushButton { background: #ffffff; border: 1px solid #b6c2c8; border-radius: 4px; padding: 6px 12px; }
QPushButton:hover { border-color: #237b65; background: #edf7f3; }
QPushButton:default, QPushButton#primary { color: white; background: #17654f; border-color: #17654f; }
QPushButton:disabled { color: #8a9499; background: #edf0f2; border-color: #d8dfe2; }
QLineEdit, QComboBox, QSpinBox { background: #ffffff; border: 1px solid #b8c5cb; border-radius: 3px; padding: 5px; min-height: 20px; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: #19775d; }
QTableView, QTreeWidget, QListWidget { background: #ffffff; alternate-background-color: #f5f8f9; border: 1px solid #d8dfe3; selection-background-color: #d8eee6; selection-color: #143f32; gridline-color: #e5eaed; }
QTreeWidget::item { padding: 8px 4px; }
QHeaderView::section { background: #edf1f3; color: #35434d; padding: 7px; border: 0; border-right: 1px solid #d8dfe3; border-bottom: 1px solid #d1d9de; }
QTabWidget::pane { border: 1px solid #d8dfe3; background: white; }
QTabBar::tab { background: #e9eef0; padding: 8px 18px; }
QTabBar::tab:selected { background: white; color: #17654f; }
QStatusBar { background: white; border-top: 1px solid #d8dfe3; color: #52616a; }
QProgressBar { border: 1px solid #c8d3d9; border-radius: 3px; background: #edf1f3; text-align: center; }
QProgressBar::chunk { background: #237b65; }
QDockWidget::title { padding: 7px; background: #e9eef0; font-weight: 600; }
"""
