"""User confirmations for import, export and project creation."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .common import BackgroundCall, make_table, show_error

FIELDS = [
    ("owner_account", "本方账户 *", ("本方账户", "账号", "卡号", "账户")),
    ("amount", "交易金额 *", ("交易金额", "金额", "发生额")),
    ("counterparty_account", "对方账户", ("对方账户", "对手账号", "对手账户")),
    ("owner_name", "本方户名", ("本方户名", "户名", "本方名称")),
    ("counterparty_name", "对方户名", ("对方户名", "对手户名", "对手名称")),
    ("occurred_at", "交易时间 / 日期", ("交易时间", "交易日期", "时间", "日期")),
    ("transaction_id", "流水号", ("流水号", "交易流水号", "交易编号")),
    ("balance", "余额", ("余额", "账户余额", "交易余额")),
    ("direction", "收支方向", ("收支方向", "方向", "借贷标志")),
    ("currency", "币种", ("币种", "货币")),
    ("status", "交易状态", ("交易状态", "状态")),
    ("transaction_type", "交易类型", ("交易类型", "业务类型")),
    ("channel", "交易渠道", ("交易渠道", "渠道")),
    ("ip", "IP 地址", ("IP", "IP地址", "IP 地址")),
    ("device_id", "设备编号", ("设备编号", "设备ID")),
    ("location", "地点", ("地点", "交易地点")),
]


class NewProjectDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("创建项目")
        self.resize(590, 230)
        layout = QVBoxLayout(self)
        label = QLabel("项目会保存原文件、数据和结果。之后可以移动整个项目文件夹。")
        label.setWordWrap(True)
        layout.addWidget(label)
        form = QFormLayout()
        self.name = QLineEdit()
        self.name.setObjectName("projectName")
        self.name.setPlaceholderText("例如：审计项目 01")
        form.addRow("项目名称", self.name)
        folder_row = QHBoxLayout()
        self.folder = QLineEdit(str(Path.home() / "Documents"))
        self.folder.setObjectName("projectFolder")
        browse = QPushButton("选择…")
        browse.clicked.connect(self._browse)
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)
        form.addRow("保存到", folder_row)
        layout.addLayout(form)
        self.location = QLabel()
        self.location.setObjectName("muted")
        self.location.setWordWrap(True)
        layout.addWidget(self.location)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("创建项目")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.name.textChanged.connect(self._show_location)
        self.folder.textChanged.connect(self._show_location)
        self._show_location()

    def _browse(self):
        selected = QFileDialog.getExistingDirectory(self, "选择项目保存位置", self.folder.text())
        if selected:
            self.folder.setText(selected)

    def _show_location(self):
        self.location.setText(f"完整位置：{self.project_path}")

    @property
    def project_path(self) -> Path:
        return Path(self.folder.text()) / self.name.text().strip()

    def accept(self):
        name = self.name.text().strip()
        if not name or name in (".", "..") or any(c in name for c in '<>:"/\\|?*'):
            show_error(
                self, "项目名称需要调整", "请输入项目名称，不要使用路径分隔符或 Windows 文件名保留字符。"
            )
            return
        if not self.folder.text().strip():
            show_error(self, "尚未选择位置", "请选择项目保存位置。")
            return
        super().accept()


class ImportDialog(QDialog):
    """Preview, explicitly map, validate, then submit one worksheet."""

    def __init__(self, parent=None, path: str | None = None, existing_hashes=None):
        super().__init__(parent)
        self.setWindowTitle("导入 Excel · 确认字段和数据质量")
        screen = self.screen().availableGeometry()
        self.resize(min(1140, screen.width() - 60), min(790, screen.height() - 80))
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self._jobs: set[BackgroundCall] = set()
        self._closed = False
        self._cancelled = threading.Event()
        self.existing_hashes = set(existing_hashes or [])
        self._preview_generation = 0
        self._validated_parameters = None
        self.report: dict | None = None
        self.preview = None
        layout = QVBoxLayout(self)
        file_row = QHBoxLayout()
        self.file = QLineEdit()
        self.file.setObjectName("importFile")
        self.file.setReadOnly(True)
        self.file.setPlaceholderText("选择一个 .xlsx 文件")
        self.browse = QPushButton("选择 Excel…")
        self.browse.clicked.connect(self._browse)
        file_row.addWidget(self.file, 1)
        file_row.addWidget(self.browse)
        layout.addLayout(file_row)
        scope = QHBoxLayout()
        self.sheet = QComboBox()
        self.sheet.setObjectName("importSheet")
        self.sheet.setMinimumWidth(160)
        self.header = QSpinBox()
        self.header.setRange(1, 1_048_576)
        self.header.setValue(1)
        self.start = QSpinBox()
        self.start.setRange(1, 1_048_576)
        self.start.setValue(2)
        self.end = QSpinBox()
        self.end.setRange(0, 1_048_576)
        self.end.setSpecialValueText("文件末尾")
        self.refresh = QPushButton("更新预览")
        self.refresh.clicked.connect(self.load_preview)
        for label, widget in [
            ("工作表", self.sheet),
            ("表头行", self.header),
            ("数据起始行", self.start),
            ("结束行", self.end),
        ]:
            scope.addWidget(QLabel(label))
            scope.addWidget(widget)
        scope.addWidget(self.refresh)
        layout.addLayout(scope)
        self.status = QLabel("选择文件后确认字段。带 * 的字段必须映射，其余原始列也会保留。")
        self.status.setWordWrap(True)
        self.status.setObjectName("muted")
        layout.addWidget(self.status)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        mapping_widget = QWidget()
        mapping_layout = QVBoxLayout(mapping_widget)
        mapping_layout.setContentsMargins(0, 0, 10, 0)
        mapping_group = QGroupBox("字段对应关系")
        mapping_form = QFormLayout(mapping_group)
        self.mapping: dict[str, QComboBox] = {}
        for key, title, _ in FIELDS:
            combo = QComboBox()
            combo.setObjectName(f"mapping_{key}")
            combo.addItem("不映射", None)
            combo.currentIndexChanged.connect(self.invalidate_report)
            self.mapping[key] = combo
            mapping_form.addRow(title, combo)
        mapping_layout.addWidget(mapping_group)
        properties = QGroupBox("数据归属和缺失值")
        form = QFormLayout(properties)
        self.platform = QComboBox()
        for label, value in [
            ("银行", "bank"),
            ("微信", "wechat"),
            ("支付宝", "alipay"),
            ("其他 / 未指定", "unknown"),
        ]:
            self.platform.addItem(label, value)
        self.namespace = QComboBox()
        for label, value in [
            ("账户", "account"),
            ("银行卡号", "bank_card"),
            ("客户账号", "customer_account"),
            ("支付账号", "payment_account"),
        ]:
            self.namespace.addItem(label, value)
        self.currency = QComboBox()
        for label, value in [
            ("保留未知", "unknown"),
            ("人民币 CNY", "CNY"),
            ("美元 USD", "USD"),
            ("港币 HKD", "HKD"),
            ("欧元 EUR", "EUR"),
        ]:
            self.currency.addItem(label, value)
        self.timezone = QComboBox()
        self.timezone.addItem("保留未知", "unknown")
        self.timezone.addItem("北京时间（UTC+8）", "Asia/Shanghai")
        form.addRow("所属平台", self.platform)
        form.addRow("账户类别", self.namespace)
        form.addRow("缺失币种", self.currency)
        form.addRow("缺失时区", self.timezone)
        mapping_layout.addWidget(properties)
        mapping_layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(300)
        scroll.setWidget(mapping_widget)
        splitter.addWidget(scroll)
        tabs = QTabWidget()
        self.preview_table, self.preview_model = make_table()
        self.preview_table.setObjectName("importPreview")
        tabs.addTab(self.preview_table, "原始数据预览")
        report_widget = QWidget()
        report_layout = QVBoxLayout(report_widget)
        self.report_label = QLabel("尚未检查数据质量。预览最多显示 20 行，检查会扫描所选范围的全部记录。")
        self.report_label.setWordWrap(True)
        report_layout.addWidget(self.report_label)
        self.report_table, self.report_model = make_table()
        self.report_table.setObjectName("qualityReport")
        report_layout.addWidget(self.report_table)
        tabs.addTab(report_widget, "数据质量报告")
        self.tabs = tabs
        splitter.addWidget(tabs)
        splitter.setSizes([320, 800])
        layout.addWidget(splitter, 1)
        self.partial = QCheckBox("遇到错误时，只导入合格行，并保留错误清单（需要明确选择）")
        self.partial.setObjectName("allowPartial")
        self.partial.toggled.connect(self._update_accept)
        partial_row = QHBoxLayout()
        partial_row.addWidget(self.partial)
        partial_row.addStretch(1)
        layout.addLayout(partial_row)
        footer = QHBoxLayout()
        self.validate_button = QPushButton("检查全部数据")
        self.validate_button.setObjectName("validateImport")
        self.validate_button.clicked.connect(self.validate)
        self.validate_button.setEnabled(False)
        footer.addWidget(self.validate_button)
        footer.addStretch()
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.accept_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.accept_button.setText("确认导入")
        self.accept_button.setObjectName("confirmImport")
        self.accept_button.setEnabled(False)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        footer.addWidget(self.buttons)
        layout.addLayout(footer)
        for widget in (self.sheet, self.platform, self.namespace, self.currency, self.timezone):
            widget.currentIndexChanged.connect(self.invalidate_report)
        for widget in (self.header, self.start, self.end):
            widget.valueChanged.connect(self.invalidate_report)
        if path:
            self.load_file(path)

    def _run(self, function, ready):
        job = BackgroundCall(function)
        self._jobs.add(job)

        def done(value):
            self._jobs.discard(job)
            if not self._closed:
                ready(value)

        def failed(error):
            self._jobs.discard(job)
            if self._closed:
                return
            self._set_busy(False)
            self.status.setText(f"无法完成：{error}")
            show_error(self, "导入检查失败", error)

        job.signals.finished.connect(done)
        job.signals.failed.connect(failed)
        self.pool.start(job)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 Excel 文件", "", "Excel 工作簿 (*.xlsx)")
        if path:
            self.load_file(path)

    def load_file(self, path: str):
        self.file.setText(str(Path(path).resolve()))
        self.sheet.clear()
        self.preview = None
        self.load_preview()

    def _set_busy(self, busy: bool):
        self.browse.setEnabled(not busy)
        self.refresh.setEnabled(not busy and bool(self.file.text()))
        self.validate_button.setEnabled(not busy and self.preview is not None)
        for widget in (
            self.sheet,
            self.header,
            self.start,
            self.end,
            self.platform,
            self.namespace,
            self.currency,
            self.timezone,
            *self.mapping.values(),
        ):
            widget.setEnabled(not busy)
        self.accept_button.setEnabled(False if busy else self._can_accept())

    def load_preview(self):
        if not self.file.text():
            return
        from traceatlas.importer import inspect_workbook

        self.invalidate_report()
        self._preview_generation += 1
        generation = self._preview_generation
        path, sheet, header = self.file.text(), self.sheet.currentText() or None, self.header.value()
        self._set_busy(True)
        self.status.setText("正在读取工作表预览…")

        def ready(preview):
            if generation != self._preview_generation:
                return
            self.preview = preview
            self.sheet.blockSignals(True)
            self.sheet.clear()
            self.sheet.addItems(preview["sheets"])
            self.sheet.setCurrentText(preview["sheet"])
            self.sheet.blockSignals(False)
            headers = preview["headers"]
            for key, _, aliases in FIELDS:
                combo = self.mapping[key]
                previous = combo.currentData()
                combo.blockSignals(True)
                combo.clear()
                combo.addItem("不映射", None)
                for i, value in enumerate(headers, 1):
                    combo.addItem(f"{i} · {value or '空表头'}", i)
                matches = [i for i, value in enumerate(headers, 1) if str(value).strip() in (*aliases, key)]
                if previous and previous <= len(headers):
                    combo.setCurrentIndex(previous)
                elif len(matches) == 1:
                    combo.setCurrentIndex(matches[0])
                combo.blockSignals(False)
            rows = [
                dict(row_number=number, **{f"c{i}": value for i, value in enumerate(row)})
                for number, row in zip(preview["row_numbers"], preview["rows"])
            ]
            self.preview_model.replace(
                {
                    "columns": [{"key": "row_number", "label": "原始行号"}]
                    + [
                        {"key": f"c{i}", "label": str(value) or f"第{i + 1}列"}
                        for i, value in enumerate(headers)
                    ],
                    "rows": rows,
                }
            )
            if self.start.value() <= header:
                self.start.setValue(header + 1)
            warning = "；".join(preview.get("warnings", []))
            self.status.setText(
                f"工作表“{preview['sheet']}” · 约 {preview.get('estimated_rows', 0):,} 行。请核对字段、表头与数据范围。"
                + ("\n" + warning if warning else "")
            )
            self._preview_parameters = (path, preview["sheet"], header)
            self._set_busy(False)

        self._run(lambda: inspect_workbook(path, sheet=sheet, header_row=header, limit=20), ready)

    def parameters(self) -> dict:
        return {
            "path": self.file.text(),
            "sheet": self.sheet.currentText(),
            "header_row": self.header.value(),
            "start_row": self.start.value(),
            "end_row": self.end.value() or None,
            "mapping": {
                key: combo.currentData()
                for key, combo in self.mapping.items()
                if combo.currentData() is not None
            },
            "platform": self.platform.currentData(),
            "account_namespace": self.namespace.currentData(),
            "currency": self.currency.currentData(),
            "timezone": self.timezone.currentData(),
            "allow_partial": self.partial.isChecked(),
        }

    def invalidate_report(self, *_):
        self._validated_parameters = None
        self.report = None
        self.accept_button.setEnabled(False)
        self.report_label.setText("字段或范围有变化，请重新检查全部数据。")

    def _can_accept(self):
        if not self.report or not self._validated_parameters:
            return False
        current = self.parameters()
        current.pop("allow_partial")
        return (
            current == self._validated_parameters
            and self.report.get("valid_count", 0) > 0
            and (self.report.get("rejected_count", 0) == 0 or self.partial.isChecked())
        )

    def _update_accept(self, *_):
        self.accept_button.setEnabled(self._can_accept())

    def validate(self):
        from traceatlas.importer import validate_import

        parameters = self.parameters()
        if getattr(self, "_preview_parameters", None) != (
            parameters["path"],
            parameters["sheet"],
            parameters["header_row"],
        ):
            show_error(
                self,
                "请先更新预览",
                "工作表或表头行已经变化，请点击“更新预览”，核对新的字段对应关系后再检查。",
            )
            return
        if not all(key in parameters["mapping"] for key in ("owner_account", "amount")):
            show_error(self, "还需要映射字段", "请选择“本方账户”和“交易金额”分别对应哪一列。")
            return
        if parameters["start_row"] <= parameters["header_row"]:
            show_error(self, "数据范围需要调整", "数据起始行必须在表头行之后。")
            return
        if parameters["end_row"] and parameters["end_row"] < parameters["start_row"]:
            show_error(self, "数据范围需要调整", "结束行不能早于数据起始行。")
            return
        self._set_busy(True)
        self.status.setText("正在检查所选范围的全部数据，完成后会显示合格行和错误行。")
        self.tabs.setCurrentIndex(1)

        def ready(report):
            self.report = report
            self._validated_parameters = dict(parameters)
            self._validated_parameters.pop("allow_partial")
            text = f"已检查 {report['input_count']:,} 行 · 合格 {report['valid_count']:,} 行 · 错误 {report['rejected_count']:,} 行"
            if report.get("blank_count"):
                text += f" · 跳过空行 {report['blank_count']:,} 行"
            if report.get("errors_truncated") or report.get("warnings_truncated"):
                text += "。下表仅展示前 200 条错误 / 提醒，提交后保存完整错误清单。"
            self.report_label.setText(text)
            self.status.setText(
                "检查完成。导入将保留原文件、原始行号和未映射列。"
                if not report["rejected_count"]
                else "发现错误。默认阻止导入；如需保留合格行，请明确勾选下方选项。"
            )
            if report.get("content_hash") in self.existing_hashes:
                self.status.setText(
                    self.status.text() + "\n此文件内容已导入过；再次导入会产生独立批次，记录不会自动去重。"
                )
            issues = [dict(severity="错误", **item) for item in report.get("errors", [])] + [
                dict(severity="提醒", **item) for item in report.get("warnings", [])
            ]
            labels = {key: label.replace(" *", "") for key, label, _ in FIELDS}
            issues = [
                {**item, "field": labels.get(item.get("field"), item.get("field", ""))} for item in issues
            ]
            self.report_model.replace(
                {
                    "columns": [
                        {"key": "severity", "label": "级别"},
                        {"key": "row", "label": "原始行号"},
                        {"key": "field", "label": "字段"},
                        {"key": "message", "label": "原因"},
                    ],
                    "rows": issues,
                }
            )
            for index, width in enumerate((55, 80, 100)):
                self.report_table.setColumnWidth(index, width)
            self.report_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
            self.report_table.setWordWrap(True)
            self.report_table.resizeRowsToContents()
            self._set_busy(False)

        def work():
            from traceatlas.project import file_hash

            result = validate_import(parameters, cancelled=self._cancelled.is_set)
            result["content_hash"] = file_hash(Path(parameters["path"]))
            return result

        self._run(work, ready)

    def accept(self):
        if not self._can_accept():
            return
        if self.report.get("content_hash") in self.existing_hashes:
            answer = QMessageBox.question(
                self,
                "确认重复导入",
                "相同内容的文件已在本项目导入。再次导入会形成独立批次，并保留所有重复记录。\n\n仍然导入？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._closed = True
        super().accept()

    def confirmed_parameters(self):
        parameters = self.parameters()
        parameters["expected_source_hash"] = self.report["content_hash"]
        return parameters

    def reject(self):
        self._closed = True
        self._cancelled.set()
        if self._jobs:
            # Keep QObject signals alive until the bounded scan exits.
            self.hide()
        super().reject()


class ExportDialog(QDialog):
    def __init__(self, name: str, filtered_count: int, full_count: int, directory: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("导出数据")
        self.resize(640, 370)
        self.filtered_count, self.full_count = filtered_count, full_count
        self.overwrite = False
        layout = QVBoxLayout(self)
        heading = QLabel(name)
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        form = QFormLayout()
        self.scope = QComboBox()
        self.scope.addItem(f"当前筛选范围（{filtered_count:,} 条）", "filtered")
        self.scope.addItem(f"完整数据（{full_count:,} 条）", "all")
        self.format = QComboBox()
        self.format.addItem("Excel 工作簿（.xlsx）", "xlsx")
        self.format.addItem("CSV 文本（.csv）", "csv")
        form.addRow("导出范围", self.scope)
        form.addRow("文件格式", self.format)
        path_row = QHBoxLayout()
        self.destination = QLineEdit(str(directory / f"{self._safe_name(name)}.xlsx"))
        self.destination.setObjectName("exportDestination")
        browse = QPushButton("另存为…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(self.destination, 1)
        path_row.addWidget(browse)
        form.addRow("保存位置", path_row)
        layout.addLayout(form)
        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setObjectName("muted")
        layout.addWidget(self.details)
        note = QLabel(
            "导出附带同名元数据清单，记录输入版本、筛选条件和算法版本。导出在后台运行，完成前不发布半成品。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("开始导出")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.format.currentIndexChanged.connect(self._format_changed)
        self.scope.currentIndexChanged.connect(self._details)
        self._details()

    @staticmethod
    def _safe_name(name):
        return "".join("_" if character in '<>:"/\\|?*' else character for character in name)

    def _details(self):
        if self.format.currentData() == "csv":
            self.details.setText(
                "CSV：UTF-8 编码（带 BOM）、逗号分隔。金额和日期按精确文本写出；用 Excel 打开时，应将账号列按文本导入，防止 Excel 自动改写长账号。"
            )
        else:
            count = self.filtered_count if self.scope.currentData() == "filtered" else self.full_count
            text = "Excel：账号、流水号及精确金额按文本写出，保留前导零和小数精度。"
            if count > 1_048_575:
                text += " 本次超过单表容量，将自动分到多个工作表，全部记录都会导出。"
            self.details.setText(text)

    def _format_changed(self):
        if self.destination.text():
            self.destination.setText(
                str(Path(self.destination.text()).with_suffix("." + self.format.currentData()))
            )
        self._details()

    def _browse(self):
        kind = self.format.currentData()
        path, _ = QFileDialog.getSaveFileName(
            self,
            "选择导出位置",
            self.destination.text(),
            f"{kind.upper()} 文件 (*.{kind})",
            options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if path:
            self.destination.setText(path)

    def accept(self):
        if not self.destination.text().strip():
            show_error(self, "尚未选择位置", "请选择导出文件的保存位置。")
            return
        destination = Path(self.destination.text()).expanduser().resolve()
        if destination.suffix.lower() != "." + self.format.currentData():
            destination = destination.with_suffix("." + self.format.currentData())
        self.destination.setText(str(destination))
        metadata = destination.with_suffix(destination.suffix + ".metadata.json")
        if destination.exists() or metadata.exists():
            answer = QMessageBox.question(
                self,
                "确认覆盖文件",
                f"目标文件或随附清单已经存在：\n{destination}\n\n是否在导出成功后替换它们？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.overwrite = True
        super().accept()

    def parameters(self):
        return {
            "scope": self.scope.currentData(),
            "format": self.format.currentData(),
            "destination": self.destination.text(),
            "overwrite": self.overwrite,
        }
