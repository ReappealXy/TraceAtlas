"""Traced-source inspector shared by all three layouts.

It shows the selected row's stored fields and offers the two real source
actions (contribution records, saved import-quality report). It never invents
values: unknown fields stay blank.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QTextOption
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .common import ENUM_LABELS, display
from .theme import Tokens

FIELDS = [
    ("owner_account", "本方账户"),
    ("counterparty_account", "对方账户"),
    ("counterparty_name", "对方户名"),
    ("owner_name", "本方户名"),
    ("record_count", "记录数"),
    ("occurred_at", "交易时间"),
    ("direction", "收支"),
    ("currency", "币种"),
    ("transaction_id", "流水号"),
    ("balance", "余额"),
    ("record_id", "记录编号"),
]


class FieldValue(QTextEdit):
    """Selectable original text, wrapping even unbroken IDs in a narrow pane."""

    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet("QTextEdit { background: transparent; border: none; padding: 0; }")
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.document().setDocumentMargin(0)
        self.document().documentLayout().documentSizeChanged.connect(self._fit_height)

    def _fit_height(self, *_args):
        height = int(self.document().size().height() + 2)
        if self.height() != height:
            self.setFixedHeight(height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_height()

    def setText(self, text):
        self.setPlainText(text)
        self.setToolTip(text)
        self._fit_height()

    def text(self):
        return self.toPlainText()


class SourceInspector(QWidget):
    sourcesRequested = Signal()
    qualityRequested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("inspectorRoot")
        self.setMinimumWidth(230)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 14)
        layout.setSpacing(8)
        head = QHBoxLayout()
        title = QLabel("来源检视")
        title.setObjectName("sectionTitle")
        head.addWidget(title)
        head.addStretch(1)
        self.kind_chip = QLabel("")
        self.kind_chip.setObjectName("nodeChip")
        head.addWidget(self.kind_chip)
        layout.addLayout(head)
        self.details_scroll = QScrollArea()
        self.details_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.details_scroll.setWidgetResizable(True)
        details = QWidget()
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(0, 0, 6, 0)
        details_layout.setSpacing(8)
        self.details_scroll.setWidget(details)
        layout.addWidget(self.details_scroll, 1)
        self.object_label = QLabel("尚未选择数据")
        self.object_label.setObjectName("overLine")
        self.object_label.setWordWrap(True)
        details_layout.addWidget(self.object_label)
        self.amount_label = QLabel("—")
        self.amount_label.setObjectName("amountLarge")
        self.amount_label.setWordWrap(True)
        self.amount_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        amount_policy = self.amount_label.sizePolicy()
        amount_policy.setHeightForWidth(True)
        self.amount_label.setSizePolicy(amount_policy)
        details_layout.addWidget(self.amount_label)
        self.amount_note = QLabel("")
        self.amount_note.setObjectName("muted")
        self.amount_note.setWordWrap(True)
        details_layout.addWidget(self.amount_note)
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setObjectName("folioRule")
        details_layout.addWidget(line)
        self.fields_host = QWidget()
        self.grid = QGridLayout(self.fields_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(8)
        self.grid.setVerticalSpacing(5)
        self.grid.setColumnStretch(1, 1)
        self.value_labels: dict[str, FieldValue] = {}
        for row, (key, label) in enumerate(FIELDS):
            caption = QLabel(label)
            caption.setObjectName("overLine")
            value = FieldValue()
            value.setObjectName("muted")
            value.setMinimumWidth(0)
            value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            self.grid.addWidget(caption, row, 0, Qt.AlignmentFlag.AlignTop)
            self.grid.addWidget(value, row, 1)
            self.value_labels[key] = value
        self.fields_host.hide()
        details_layout.addWidget(self.fields_host)
        self.source_note = QLabel("")
        self.source_note.setObjectName("muted")
        self.source_note.setWordWrap(True)
        details_layout.addWidget(self.source_note)
        details_layout.addStretch(1)
        self.sources_button = QPushButton("查看选中行来源")
        self.sources_button.setObjectName("primary")
        self.sources_button.clicked.connect(self.sourcesRequested.emit)
        self.quality_button = QPushButton("导入质量报告")
        self.quality_button.clicked.connect(self.qualityRequested.emit)
        layout.addWidget(self.sources_button)
        layout.addWidget(self.quality_button)
        self.empty_hint = QLabel("选择一行后，这里显示该行的关键字段与原始行定位。")
        self.empty_hint.setObjectName("hint")
        self.empty_hint.setWordWrap(True)
        details_layout.insertWidget(0, self.empty_hint)
        self.set_object(None)
        self.set_actions(has_row=False, is_dataset=False)

    def apply_theme(self, scheme: Tokens) -> None:
        self.setProperty("mode", scheme.mode)

    def set_object(self, obj: dict | None) -> None:
        self._object = obj
        if obj is None:
            self.kind_chip.setText("未选择")
            self.object_label.setText("尚未打开数据或结果")
            self.amount_label.setText("—")
            self.amount_note.setText("")
            self.source_note.setText("")
            self.quality_button.setEnabled(False)
            self.fields_host.hide()
            return
        self.kind_chip.setText("数据版本" if obj["kind"] == "dataset" else "结果")
        self.object_label.setText(obj["name"])
        self.source_note.setText("")
        self.set_row(None)

    def set_row(self, row: dict | None) -> None:
        obj = getattr(self, "_object", None)
        self._row = row
        self.quality_button.setEnabled(bool(obj and obj["kind"] == "dataset"))
        if row is None:
            self.amount_label.setText("—")
            self.amount_note.setText("尚未选择记录" if obj else "")
            self.fields_host.hide()
            self.source_note.setText(
                "在数据表格中选择一行，即可查看它的字段与原始行定位。" if obj else ""
            )
            self.empty_hint.show()
            return
        self.fields_host.show()
        self.empty_hint.hide()
        amount = row.get("amount")
        if amount is None:
            self.amount_label.setText("—")
            self.amount_note.setText("该结果行没有金额列")
        else:
            currency = row.get("currency")
            currency_label = ENUM_LABELS.get("currency", {}).get(str(currency), display(currency))
            self.amount_label.setText(f"{display(amount)}")
            self.amount_note.setText(f"币种：{currency_label or '未知'} · 按存储精度显示")
        for key, _label in FIELDS:
            value = row.get(key)
            if value is None or value == "":
                self.value_labels[key].setText("—")
            elif key in ENUM_LABELS:
                self.value_labels[key].setText(
                    ENUM_LABELS[key].get(str(value), display(value))
                )
            else:
                self.value_labels[key].setText(display(value))
        sheet = row.get("source_sheet")
        number = row.get("source_row")
        if sheet and number is not None:
            self.source_note.setText(
                f"原始来源：工作表“{sheet}” · 第 {number} 行（项目内保存的原文件副本）。"
            )
        else:
            self.source_note.setText("结果固定引用生成它时的数据版本；来源可逐条展开。")

    def set_actions(self, *, has_row: bool, is_dataset: bool) -> None:
        self.sources_button.setEnabled(bool(has_row))
        self.quality_button.setEnabled(bool(is_dataset))
