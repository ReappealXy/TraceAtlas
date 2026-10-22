"""Paged contribution records and original worksheet rows."""

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QVBoxLayout,
)

from traceatlas.query import QueryService

from .common import BackgroundCall, make_table, show_error


class SourcesDialog(QDialog):
    def __init__(self, project, object_id, row_id, parent=None):
        super().__init__(parent)
        self.setWindowTitle("查看来源 · 贡献记录与原始行")
        self.resize(1120, 720)
        self.project = project
        self.query = QueryService(project)
        self.object_id, self.row_id = object_id, row_id
        self.offset, self.total = 0, 0
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self._jobs = set()
        self._closed = False
        self._source_generation = 0
        layout = QVBoxLayout(self)
        self.summary = QLabel("正在查询参与这条结果的记录…")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.contributions, self.contributions_model = make_table()
        self.contributions.setObjectName("sourceContributions")
        self.contributions.selectionModel().selectionChanged.connect(self.load_original)
        splitter.addWidget(self.contributions)
        self.original, self.original_model = make_table()
        self.original.setObjectName("originalRows")
        splitter.addWidget(self.original)
        splitter.setSizes([260, 270])
        layout.addWidget(splitter, 1)
        self.original_note = QLabel("在上表选择一条记录，即可查看项目中保存的原文件对应行。")
        self.original_note.setWordWrap(True)
        self.original_note.setObjectName("muted")
        layout.addWidget(self.original_note)
        navigation = QHBoxLayout()
        self.previous = QPushButton("上一页")
        self.next = QPushButton("下一页")
        self.previous.clicked.connect(lambda: self.change_page(-1))
        self.next.clicked.connect(lambda: self.change_page(1))
        self.page_label = QLabel()
        navigation.addWidget(self.previous)
        navigation.addWidget(self.next)
        navigation.addWidget(self.page_label)
        navigation.addStretch()
        self.repair_button = QPushButton("重新定位缺失原文件…")
        self.repair_button.setEnabled(False)
        self.repair_button.clicked.connect(self.repair_source)
        navigation.addWidget(self.repair_button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        navigation.addWidget(buttons)
        layout.addLayout(navigation)
        self.load()

    def run(self, function, ready):
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
            self.original_note.setText(f"来源暂时无法读取：{error}")
            if getattr(error, "code", "") in ("ARTIFACT_MISSING", "SOURCE_MISSING"):
                self.repair_button.setEnabled(not self.project.readonly)
            show_error(self, "无法读取来源", error)

        job.signals.finished.connect(done)
        job.signals.failed.connect(failed)
        self.pool.start(job)

    def load(self):
        self.previous.setEnabled(False)
        self.next.setEnabled(False)

        def ready(page):
            self.total = page["total"]
            self.contributions_model.replace(page)
            self.summary.setText(
                f"此条记录 / 结果共有 {self.total:,} 条贡献记录。来源固定引用生成它时的数据版本。"
            )
            self.page_label.setText(
                f"第 {self.offset // 200 + 1} 页 · 共 {max(1, (self.total + 199) // 200)} 页"
            )
            self.previous.setEnabled(self.offset > 0)
            self.next.setEnabled(self.offset + 200 < self.total)
            if page["rows"]:
                self.contributions.selectRow(0)

        self.run(
            lambda: self.query.sources(self.object_id, self.row_id, offset=self.offset, limit=200), ready
        )

    def change_page(self, delta):
        self.offset = max(0, self.offset + delta * 200)
        self.load()

    def load_original(self, *_):
        selected = self.contributions.selectionModel().selectedRows()
        if not selected:
            return
        row = self.contributions_model.rows[selected[0].row()]
        source_id, sheet, number = row.get("source_id"), row.get("source_sheet"), row.get("source_row")
        if not source_id or number is None:
            return
        source = self.project.get_object(source_id)
        self.repair_button.setEnabled(
            not self.project.readonly and source.get("state") in ("source_missing", "damaged", "corrupt")
        )
        self._source_generation += 1
        generation = self._source_generation
        self.original_note.setText(f"正在读取“{sheet}”第 {number} 行…")

        def ready(page):
            if generation != self._source_generation:
                return
            self.original_model.replace(page)
            for i, record in enumerate(page["rows"]):
                if record.get("row_number") == number:
                    self.original.selectRow(i)
                    self.original.scrollTo(self.original_model.index(i, 0))
                    break
            self.original_note.setText(
                f"原文件副本 · 工作表“{sheet}” · 第 {number} 行（已选中）；账号与金额按原始单元格内容显示。"
            )

        self.run(lambda: self.query.source_preview(source_id, sheet, number, limit=15), ready)

    def repair_source(self):
        selected = self.contributions.selectionModel().selectedRows()
        if not selected:
            return
        source_id = self.contributions_model.rows[selected[0].row()]["source_id"]
        filename, _ = QFileDialog.getOpenFileName(
            self, "选择原始文件（内容必须与原副本一致）", "", "Excel 工作簿 (*.xlsx)"
        )
        if not filename:
            return
        self.repair_button.setEnabled(False)
        self.run(lambda: self.project.repair_source(source_id, filename), lambda _: self.load_original())

    def reject(self):
        self._closed = True
        super().reject()
