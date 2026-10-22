"""TraceAtlas desktop: projects, real data, background tasks and traceable results."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QThreadPool, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from traceatlas.contracts import TraceAtlasError

from .common import (
    ENUM_LABELS,
    STYLE,
    BackgroundCall,
    copy_error_report,
    destination_snapshot,
    display,
    make_table,
    show_error,
)
from .dialogs import ExportDialog, ImportDialog, NewProjectDialog
from .sources import SourcesDialog

STATES = {
    "queued": "排队中",
    "running": "运行中",
    "committing": "正在保存",
    "cancel_requested": "正在取消",
    "succeeded": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
    "interrupted": "已中断",
}
FEATURES = {
    "xlsx.import": "导入 Excel",
    "validation.account_count": "按账户计数",
    "result.export": "导出数据",
    "index.rebuild": "重建项目索引",
}
STAGES = {
    "copying_source": "保存原文件",
    "validating": "检查字段与数据",
    "importing": "导入记录",
    "verifying": "校验产物",
    "counting": "按账户计数",
    "aggregating": "汇总账户",
    "lineage": "保存来源关系",
    "exporting": "写出导出文件",
    "indexing": "建立账户索引",
    "committing": "安全保存",
    "validation": "校验输入",
    "export": "导出",
    "starting": "启动工作进程",
}
ACTIVE = {"queued", "running", "committing", "cancel_requested"}


class MainWindow(QMainWindow):
    def __init__(self, project=None):
        super().__init__()
        self.setWindowTitle("TraceAtlas")
        screen = self.screen().availableGeometry()
        self.resize(min(1360, screen.width() - 40), min(880, screen.height() - 60))
        self.setMinimumSize(1000, 680)
        self.setStyleSheet(STYLE)
        self.settings = QSettings("TraceAtlas", "TraceAtlas")
        self.project = None
        self.manager = None
        self.query = None
        self.current_object = None
        self.filters: list[dict] = []
        self.sort = None
        self.descending = False
        self.offset = 0
        self.total = 0
        self._query_ready = False
        self.page_size = 200
        self._generation = 0
        self._jobs = set()
        self._dialogs = []
        self._task_snapshot = None
        self._deferred = None
        self._closing = False
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(2)
        self._create_actions()
        self._create_shell()
        self._create_tasks()
        self.statusBar().showMessage("本机项目 · 完成的操作自动保存")
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.poll_tasks)
        self.timer.start()
        if project is not None:
            self.set_project(project)
        else:
            self._refresh_actions()
        geometry = self.settings.value("window/geometry")
        if geometry:
            self.restoreGeometry(geometry)

    def _action(self, name, slot, shortcut=None):
        action = QAction(name, self)
        action.triggered.connect(slot)
        if shortcut:
            action.setShortcut(shortcut)
        return action

    def _create_actions(self):
        self.new_action = self._action("新建项目…", self.new_project, QKeySequence.StandardKey.New)
        self.open_action = self._action("打开项目…", self.choose_project, QKeySequence.StandardKey.Open)
        self.save_action = self._action("保存项目", self.save_project, QKeySequence.StandardKey.Save)
        self.close_action = self._action("关闭项目", self.close_project)
        self.import_action = self._action("导入 Excel…", self.import_data, "Ctrl+I")
        self.count_action = self._action("按账户计数", self.account_count)
        self.export_action = self._action("导出…", self.export_data, "Ctrl+E")
        self.source_action = self._action("查看来源", self.show_sources)
        self.quality_action = self._action("查看导入质量报告", self.show_quality)
        self.delete_action = self._action("删除当前结果…", self.delete_result)
        self.cleanup_action = self._action("清理中断任务的临时文件…", self.cleanup)
        self.rebuild_action = self._action("重建项目内索引", self.rebuild_index)
        self.quit_action = self._action("退出", self.close, QKeySequence.StandardKey.Quit)
        project_menu = self.menuBar().addMenu("项目")
        for action in (self.new_action, self.open_action):
            project_menu.addAction(action)
        self.recent_menu = project_menu.addMenu("最近项目")
        self._refresh_recent_menu()
        project_menu.addSeparator()
        for action in (self.save_action, self.close_action, self.cleanup_action):
            project_menu.addAction(action)
        project_menu.addSeparator()
        project_menu.addAction(self.quit_action)
        data_menu = self.menuBar().addMenu("数据")
        for action in (
            self.import_action,
            self.count_action,
            self.source_action,
            self.quality_action,
            self.export_action,
            self.delete_action,
            self.rebuild_action,
        ):
            data_menu.addAction(action)
        help_menu = self.menuBar().addMenu("帮助")
        help_menu.addAction(self._action("关于 TraceAtlas", self.about))
        toolbar = QToolBar("常用操作", self)
        toolbar.setMovable(False)
        for action in (self.new_action, self.open_action, self.save_action):
            toolbar.addAction(action)
        toolbar.addSeparator()
        for action in (self.import_action, self.count_action, self.export_action):
            toolbar.addAction(action)
        self.addToolBar(toolbar)

    def _create_shell(self):
        splitter = QSplitter(Qt.Orientation.Horizontal)
        navigation = QWidget()
        nav_layout = QVBoxLayout(navigation)
        nav_layout.setContentsMargins(16, 18, 12, 12)
        brand = QLabel("TraceAtlas")
        brand.setObjectName("pageTitle")
        nav_layout.addWidget(brand)
        self.project_label = QLabel("尚未打开项目")
        self.project_label.setWordWrap(True)
        self.project_label.setObjectName("sectionTitle")
        nav_layout.addWidget(self.project_label)
        self.project_path_label = QLabel("数据保存在你的电脑中")
        self.project_path_label.setWordWrap(True)
        self.project_path_label.setObjectName("muted")
        nav_layout.addWidget(self.project_path_label)
        self.navigation = QTreeWidget()
        self.navigation.setObjectName("navigation")
        self.navigation.setHeaderHidden(True)
        self.navigation.setMinimumWidth(230)
        self.navigation.itemSelectionChanged.connect(self._navigate)
        nav_layout.addWidget(self.navigation, 1)
        splitter.addWidget(navigation)
        self.workspace = QStackedWidget()
        self.workspace.setObjectName("workspace")
        self.welcome_page = self._welcome()
        self.workspace.addWidget(self.welcome_page)
        self.data_page = self._data_page()
        self.workspace.addWidget(self.data_page)
        self.index_page = self._index_page()
        self.workspace.addWidget(self.index_page)
        self.modules_page = self._modules_page()
        self.workspace.addWidget(self.modules_page)
        splitter.addWidget(self.workspace)
        splitter.setSizes([270, 1090])
        self.setCentralWidget(splitter)
        self._populate_navigation()

    def _welcome(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(38, 36, 38, 28)
        title = QLabel("从一个项目开始")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        self.welcome_text = QLabel(
            "把原始文件、数据版本和分析结果放在同一个项目里。\n每个结果都能回到参与计算的原始记录。"
        )
        self.welcome_text.setWordWrap(True)
        layout.addWidget(self.welcome_text)
        layout.addSpacing(18)
        actions = QHBoxLayout()
        self.welcome_new = QPushButton("新建项目")
        self.welcome_new.setObjectName("primary")
        self.welcome_new.clicked.connect(self.new_project)
        self.welcome_open = QPushButton("打开已有项目")
        self.welcome_open.clicked.connect(self.choose_project)
        self.welcome_import = QPushButton("导入第一份 Excel")
        self.welcome_import.setObjectName("primary")
        self.welcome_import.clicked.connect(self.import_data)
        actions.addWidget(self.welcome_new)
        actions.addWidget(self.welcome_open)
        actions.addWidget(self.welcome_import)
        actions.addStretch()
        layout.addLayout(actions)
        layout.addSpacing(30)
        self.overview = QLabel("当前可用：Excel 导入、按账户计数、来源追溯、查询导出和项目内索引。")
        self.overview.setWordWrap(True)
        self.overview.setObjectName("muted")
        layout.addWidget(self.overview)
        self.recovery_notice = QLabel()
        self.recovery_notice.setWordWrap(True)
        self.recovery_notice.setObjectName("notice")
        self.recovery_notice.hide()
        layout.addWidget(self.recovery_notice)
        layout.addStretch()
        return page

    def _data_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(22, 20, 22, 16)
        self.data_title = QLabel("数据")
        self.data_title.setObjectName("pageTitle")
        layout.addWidget(self.data_title)
        self.data_summary = QLabel()
        self.data_summary.setWordWrap(True)
        self.data_summary.setObjectName("muted")
        layout.addWidget(self.data_summary)
        filter_row = QHBoxLayout()
        self.filter_column = QComboBox()
        self.filter_column.setObjectName("filterColumn")
        self.filter_column.setMinimumWidth(130)
        self.filter_operator = QComboBox()
        for label, value in [
            ("包含", "contains"),
            ("等于", "eq"),
            ("大于等于", "gte"),
            ("小于等于", "lte"),
            ("大于", "gt"),
            ("小于", "lt"),
            ("为空", "is_null"),
        ]:
            self.filter_operator.addItem(label, value)
        self.filter_value = QLineEdit()
        self.filter_value.setObjectName("filterValue")
        self.filter_value.setPlaceholderText("输入筛选值；日期示例 2026-10-22")
        self.time_filter_note = QLabel(
            "整天筛选按原日期。精确时间只比较同类：带时区按实际时刻，未知时区按原钟表时间；"
            "只有日期的记录不参加精确比较，分钟值按分钟起点参与且保留原精度。"
            "无法识别的时区不参加精确比较。时间区间用“大于等于起点”与“小于终点”。"
            "排序按已知时区、未知时区、仅日期分组。"
        )
        self.time_filter_note.setWordWrap(True)
        self.time_filter_note.setObjectName("muted")
        self.time_filter_note.hide()
        self.filter_column.currentIndexChanged.connect(
            lambda: self.time_filter_note.setVisible(self.filter_column.currentData() == "occurred_at")
        )
        self.filter_value.returnPressed.connect(self.add_filter)
        self.filter_operator.currentIndexChanged.connect(
            lambda: self.filter_value.setEnabled(self.filter_operator.currentData() != "is_null")
        )
        self.filter_button = QPushButton("添加筛选")
        self.filter_button.clicked.connect(self.add_filter)
        self.clear_filters_button = QPushButton("清除筛选")
        self.clear_filters_button.clicked.connect(self.clear_filters)
        for widget in (
            self.filter_column,
            self.filter_operator,
            self.filter_value,
            self.filter_button,
            self.clear_filters_button,
        ):
            filter_row.addWidget(widget)
        layout.addLayout(filter_row)
        layout.addWidget(self.time_filter_note)
        self.filter_summary = QLabel("显示全部记录 · 点击列标题排序")
        self.filter_summary.setWordWrap(True)
        self.filter_summary.setObjectName("muted")
        layout.addWidget(self.filter_summary)
        self.table, self.table_model = make_table()
        self.table.setObjectName("dataTable")
        self.table.horizontalHeader().setSectionsClickable(True)
        self.table.horizontalHeader().sectionClicked.connect(self.sort_column)
        self.table.doubleClicked.connect(self.show_sources)
        self.table.selectionModel().selectionChanged.connect(self._refresh_actions)
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.previous_page = QPushButton("上一页")
        self.previous_page.clicked.connect(lambda: self.change_page(-1))
        self.next_page = QPushButton("下一页")
        self.next_page.clicked.connect(lambda: self.change_page(1))
        self.page_label = QLabel()
        row.addWidget(self.previous_page)
        row.addWidget(self.next_page)
        row.addWidget(self.page_label)
        row.addStretch()
        source = QPushButton("查看选中行来源")
        source.clicked.connect(self.show_sources)
        source.setObjectName("showSources")
        self.source_button = source
        row.addWidget(source)
        export = QPushButton("导出…")
        export.clicked.connect(self.export_data)
        row.addWidget(export)
        layout.addLayout(row)
        return page

    def _index_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(22, 20, 22, 16)
        title = QLabel("项目内查找")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        note = QLabel("在当前项目已登记的本方 / 对方账户中查找。索引保留数据版本和原始行定位。")
        note.setWordWrap(True)
        layout.addWidget(note)
        row = QHBoxLayout()
        self.index_input = QLineEdit()
        self.index_input.setObjectName("indexQuery")
        self.index_input.setPlaceholderText("输入账户或账号片段")
        self.index_input.returnPressed.connect(self.search_index)
        search = QPushButton("查找")
        search.clicked.connect(self.search_index)
        rebuild = QPushButton("重建索引")
        rebuild.clicked.connect(self.rebuild_index)
        self.index_rebuild_button = rebuild
        row.addWidget(self.index_input, 1)
        row.addWidget(search)
        row.addWidget(rebuild)
        layout.addLayout(row)
        self.index_notice = QLabel("首次使用或数据版本改变后，需要重建索引。")
        self.index_notice.setWordWrap(True)
        self.index_notice.setObjectName("muted")
        layout.addWidget(self.index_notice)
        self.index_table, self.index_model = make_table()
        self.index_table.doubleClicked.connect(self.open_index_source)
        layout.addWidget(self.index_table, 1)
        self.index_offset, self.index_total = 0, 0
        controls = QHBoxLayout()
        self.index_previous = QPushButton("上一页")
        self.index_next = QPushButton("下一页")
        self.index_previous.clicked.connect(lambda: self.search_index(offset=max(0, self.index_offset - 200)))
        self.index_next.clicked.connect(lambda: self.search_index(offset=self.index_offset + 200))
        self.index_previous.setEnabled(False)
        self.index_next.setEnabled(False)
        controls.addWidget(self.index_previous)
        controls.addWidget(self.index_next)
        controls.addStretch()
        note = QLabel("双击一行查看原始来源")
        note.setObjectName("muted")
        controls.addWidget(note)
        layout.addLayout(controls)
        return page

    def _modules_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(22, 20, 22, 16)
        title = QLabel("模块与能力")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        note = QLabel("当前已安装的能力及输入要求。已保存的结果保留生成它时的算法版本。")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.modules_table, self.modules_model = make_table()
        layout.addWidget(self.modules_table, 1)
        return page

    def _create_tasks(self):
        dock = QDockWidget("后台任务", self)
        dock.setObjectName("taskDock")
        dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable)
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 6, 8, 8)
        self.tasks_table = QTreeWidget()
        self.tasks_table.setObjectName("taskList")
        self.tasks_table.setHeaderLabels(["任务", "状态", "阶段", "进度", "说明"])
        self.tasks_table.setRootIsDecorated(False)
        self.tasks_table.setMinimumHeight(100)
        self.tasks_table.setMaximumHeight(210)
        self.tasks_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tasks_table.itemDoubleClicked.connect(self.task_details)
        self.tasks_table.itemSelectionChanged.connect(self._refresh_task_actions)
        for index, width in enumerate([180, 100, 170, 130, 500]):
            self.tasks_table.setColumnWidth(index, width)
        layout.addWidget(self.tasks_table)
        row = QHBoxLayout()
        self.task_note = QLabel("任务在后台执行，切换页面不会取消任务。")
        self.task_note.setObjectName("muted")
        row.addWidget(self.task_note, 1)
        self.cancel_task_button = QPushButton("取消选中任务")
        self.cancel_task_button.clicked.connect(self.cancel_task)
        self.retry_task_button = QPushButton("重试")
        self.retry_task_button.clicked.connect(self.retry_task)
        self.task_details_button = QPushButton("详情")
        self.task_details_button.clicked.connect(self.task_details)
        for button in (self.cancel_task_button, self.retry_task_button, self.task_details_button):
            row.addWidget(button)
        layout.addLayout(row)
        dock.setWidget(widget)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        self.menuBar().addMenu("视图").addAction(dock.toggleViewAction())
        self.task_dock = dock

    def _run(self, function, ready, *, title="操作失败", error_handler=None):
        job = BackgroundCall(function)
        self._jobs.add(job)

        def done(value):
            self._jobs.discard(job)
            ready(value)

        def failed(error):
            self._jobs.discard(job)
            if error_handler:
                error_handler(error)
            else:
                self.statusBar().showMessage(str(error))
                show_error(self, title, error)

        job.signals.finished.connect(done)
        job.signals.failed.connect(failed)
        self.pool.start(job)

    def _recent(self):
        stored = self.settings.value("projects/recent", [])
        return stored if isinstance(stored, list) else [stored] if stored else []

    def _refresh_recent_menu(self):
        self.recent_menu.clear()
        paths = self._recent()
        self.recent_menu.setEnabled(bool(paths))
        for path in paths:
            action = self.recent_menu.addAction(str(path))
            action.triggered.connect(lambda checked=False, value=path: self.open_project(value))

    def set_project(self, project):
        from traceatlas.query import QueryService
        from traceatlas.tasks import TaskManager

        if self.project is not None and self.project is not project:
            if self.manager and self.manager.has_active():
                raise RuntimeError("当前项目仍有任务，请等待或取消后再切换。")
            self._release_project()
        self._generation += 1
        self.project = project
        self.query = QueryService(project)
        self.manager = None if project.readonly else TaskManager(project)
        self.current_object = None
        self._task_snapshot = None
        suffix = "（只读）" if project.readonly else ""
        self.setWindowTitle(f"{project.name}{suffix} — TraceAtlas")
        self.project_label.setText(project.name + suffix)
        self.project_path_label.setText(str(project.path))
        self.welcome_text.setText(
            "导入 Excel 后，可以按账户统计记录数、查看来源并导出。\n已完成的操作会自动保存，关闭后可继续查看。"
        )
        report = getattr(project, "recovery_report", [])
        self.recovery_notice.setVisible(bool(report) or project.readonly)
        self.recovery_notice.setText(
            "\n".join(
                (["项目以只读方式打开，可以查看已提交的数据和结果。"] if project.readonly else [])
                + [str(line) for line in report]
            )
        )
        paths = [str(project.path)] + [value for value in self._recent() if value != str(project.path)]
        self.settings.setValue("projects/recent", paths[:10])
        self._refresh_recent_menu()
        self._populate_navigation()
        self._load_modules()
        self._refresh_actions()
        self.workspace.setCurrentWidget(self.welcome_page)
        session = project.load_session()
        selected = session.get("selected_object")
        if selected:
            try:
                obj = project.get_object(selected)
                if obj.get("state") != "deleted":
                    self.show_object(selected, restore=session)
            except (KeyError, ValueError, TraceAtlasError):
                pass
        self.poll_tasks()

    def _populate_navigation(self):
        self.navigation.blockSignals(True)
        self.navigation.clear()
        overview = QTreeWidgetItem(["项目概览"])
        overview.setData(0, Qt.ItemDataRole.UserRole, ("overview", None))
        self.navigation.addTopLevelItem(overview)
        datasets = QTreeWidgetItem(["数据集"])
        datasets.setData(0, Qt.ItemDataRole.UserRole, ("overview", None))
        results = QTreeWidgetItem(["结果"])
        results.setData(0, Qt.ItemDataRole.UserRole, ("overview", None))
        self.navigation.addTopLevelItems([datasets, results])
        if self.project:
            objects = self.project.objects()
            for obj in objects:
                if obj["kind"] not in ("dataset", "result"):
                    continue
                suffix = (
                    " · 已损坏" if obj.get("state") in ("corrupt", "damaged", "corrupted", "missing") else ""
                )
                item = QTreeWidgetItem([obj["name"] + suffix])
                item.setData(0, Qt.ItemDataRole.UserRole, ("object", obj["id"]))
                item.setToolTip(0, f"{obj['name']}\n{obj['id']}")
                (datasets if obj["kind"] == "dataset" else results).addChild(item)
            datasets.setText(0, f"数据集  {datasets.childCount()}")
            results.setText(0, f"结果  {results.childCount()}")
            self.overview.setText(
                f"本项目有 {datasets.childCount()} 个数据版本、{results.childCount()} 份结果。原文件和已提交结果在项目中独立保存。"
            )
        for label, kind in [("项目内查找", "index"), ("模块与能力", "modules")]:
            item = QTreeWidgetItem([label])
            item.setData(0, Qt.ItemDataRole.UserRole, (kind, None))
            self.navigation.addTopLevelItem(item)
        self.navigation.expandAll()
        self.navigation.blockSignals(False)

    def _navigate(self):
        selected = self.navigation.selectedItems()
        if not selected:
            return
        kind, identifier = selected[0].data(0, Qt.ItemDataRole.UserRole)
        if kind == "object":
            self.show_object(identifier)
        elif kind == "index" and self.project:
            self.workspace.setCurrentWidget(self.index_page)
        elif kind == "modules":
            self._load_modules()
            self.workspace.setCurrentWidget(self.modules_page)
        else:
            self.workspace.setCurrentWidget(self.welcome_page)

    def _refresh_actions(self, *_):
        writable = self.project is not None and not self.project.readonly
        has_dataset = bool(self.project and self.project.objects("dataset"))
        for action in (self.save_action, self.import_action, self.cleanup_action):
            action.setEnabled(writable)
        self.count_action.setEnabled(writable and has_dataset)
        self.rebuild_action.setEnabled(writable and has_dataset)
        self.index_rebuild_button.setEnabled(writable and has_dataset)
        self.close_action.setEnabled(self.project is not None)
        self.export_action.setEnabled(writable and self.current_object is not None and self._query_ready)
        self.delete_action.setEnabled(
            writable and self.current_object is not None and self.current_object["kind"] == "result"
        )
        self.quality_action.setEnabled(
            self.current_object is not None and self.current_object["kind"] == "dataset"
        )
        has_row = self.current_object is not None and bool(self.table.selectionModel().selectedRows())
        self.source_action.setEnabled(has_row)
        self.source_button.setEnabled(has_row)
        self.welcome_import.setVisible(self.project is not None)
        self.welcome_import.setEnabled(writable)
        self.welcome_new.setVisible(self.project is None)
        self.welcome_open.setVisible(self.project is None)
        self._refresh_task_actions()

    def _load_modules(self):
        from traceatlas.registry import ModuleRegistry

        registry = ModuleRegistry()
        rows = []
        kinds = {obj["kind"] for obj in self.project.objects()} if self.project else set()
        modules = {item["module_id"]: item.get("name", item["module_id"]) for item in registry.list_modules()}
        for feature in registry.available_features(input_kinds=kinds):
            if hasattr(feature, "to_dict"):
                feature = feature.to_dict()
            elif not isinstance(feature, dict):
                feature = vars(feature)
            feature_id = feature.get("feature_id", feature.get("id", ""))
            availability = (
                feature.get("reason")
                or feature.get("unavailable_reason")
                or ("可用" if feature.get("available", True) else "缺少所需输入")
            )
            if not self.project:
                availability = "请先新建或打开项目。"
            elif self.project.readonly:
                availability = "只读项目；可查看已保存数据，不能运行新任务。"
            rows.append(
                {
                    "name": feature.get("name", FEATURES.get(feature_id, feature_id)),
                    "module": modules.get(feature.get("module_id"), feature.get("module_id", "")),
                    "version": feature.get("module_version", feature.get("version", "")),
                    "availability": availability,
                    "feature": feature_id,
                }
            )
        self.modules_model.replace(
            {
                "columns": [
                    {"key": "name", "label": "功能"},
                    {"key": "module", "label": "模块"},
                    {"key": "version", "label": "算法版本"},
                    {"key": "availability", "label": "输入要求 / 状态"},
                    {"key": "feature", "label": "功能标识"},
                ],
                "rows": rows,
            }
        )
        self.modules_table.setColumnWidth(3, 330)

    def new_project(self):
        dialog = NewProjectDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        path, name = dialog.project_path, dialog.name.text().strip()

        def create():
            from traceatlas.project import Project

            self.statusBar().showMessage("正在创建项目…")
            self._run(lambda: Project.create(path, name), self.set_project, title="项目创建失败")

        self._prepare_switch(create)

    def choose_project(self):
        path = QFileDialog.getExistingDirectory(self, "选择包含 project.json 的项目文件夹")
        if path:
            self.open_project(path)

    def open_project(self, path):
        from traceatlas.project import Project

        if Path(path).is_file():
            path = Path(path).parent
        if self.project and Path(path).resolve() == self.project.path:
            self.statusBar().showMessage("这个项目已经打开。", 5000)
            return

        def do_open(readonly=False, migrate=False):
            self.statusBar().showMessage("正在检查并打开项目…")

            def failed(error):
                code = getattr(error, "code", "")
                if code == "PROJECT_LOCKED":
                    answer = QMessageBox.question(
                        self,
                        "项目正在被使用",
                        "另一个程序正在修改此项目。是否以只读方式查看已保存的数据和结果？",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.Yes,
                    )
                    if answer == QMessageBox.StandardButton.Yes:
                        do_open(readonly=True)
                elif code == "MIGRATION_REQUIRED":
                    answer = QMessageBox.question(
                        self,
                        "项目需要升级",
                        "这个项目使用较旧的格式。升级前会保留可恢复备份，并校验数据和来源关联。\n\n是否备份并升级后打开？",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    if answer == QMessageBox.StandardButton.Yes:
                        do_open(migrate=True)
                else:
                    show_error(self, "无法打开项目", error)

            self._run(
                lambda: Project.open(path, readonly=readonly, migrate=migrate),
                self.set_project,
                error_handler=failed,
            )

        self._prepare_switch(do_open)

    def _session(self):
        return {
            "selected_object": self.current_object["id"] if self.current_object else None,
            "filters": self.filters,
            "sort": self.sort,
            "descending": self.descending,
            "offset": self.offset,
        }

    def save_project(self):
        if not self.project or self.project.readonly:
            return
        try:
            self.project.save_session(self._session())
            self.statusBar().showMessage("项目已保存 · 已完成的数据和结果会自动保存", 5000)
        except Exception as error:  # noqa: BLE001 - user-facing service boundary
            show_error(self, "保存失败", error)

    def _release_project(self):
        if self.project is None:
            return
        self.save_project()
        if self.manager:
            self.manager.shutdown(cancel=False)
        self.project.close()
        self.project = self.manager = self.query = self.current_object = None
        self._generation += 1
        self.tasks_table.clear()
        self.table_model.replace({})
        self._task_snapshot = None
        self._finished_ids = set()
        self._query_ready = False
        self.project_label.setText("尚未打开项目")
        self.project_path_label.setText("数据保存在你的电脑中")
        self.setWindowTitle("TraceAtlas")
        self.workspace.setCurrentWidget(self.welcome_page)
        self._populate_navigation()
        self._refresh_actions()

    def _prepare_switch(self, callback):
        if self._deferred:
            self.statusBar().showMessage("正在等待当前操作安全结束，请稍候。")
            return
        active = self.manager and self.manager.has_active()
        if active:
            box = QMessageBox(self)
            box.setWindowTitle("项目仍有后台任务")
            box.setText("切换或关闭项目之前，需要让当前任务安全结束。")
            wait_button = box.addButton("等待任务完成后继续", QMessageBox.ButtonRole.AcceptRole)
            cancel_button = box.addButton("取消任务后继续", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton("留在当前项目", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() not in (wait_button, cancel_button):
                return
            if box.clickedButton() == cancel_button:
                for task in self.manager.list_tasks():
                    if task.get("state", task.get("status")) in ACTIVE:
                        self.manager.cancel(task.get("task_id", task.get("id")))
        if active or self._has_pending_queries():
            self._deferred = callback
            self.statusBar().showMessage("正在等待后台操作安全结束…")
        else:
            callback()

    def close_project(self):
        self._prepare_switch(self._release_project)

    def _has_pending_queries(self):
        return bool(self._jobs or any(getattr(dialog, "_jobs", ()) for dialog in self._dialogs))

    def import_data(self):
        if not self.project or self.project.readonly:
            return
        hashes = {obj["data"].get("content_hash") for obj in self.project.objects("source")}
        dialog = ImportDialog(self, existing_hashes=hashes)
        self._dialogs.append(dialog)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.submit_task("xlsx.import", dialog.confirmed_parameters())

    def submit_task(self, feature_id, parameters, input_ids=None):
        if self.manager is None:
            show_error(self, "项目不可修改", "请打开可写项目后重试。")
            return None
        try:
            task_id = self.manager.submit(feature_id, parameters, input_ids=input_ids or [])
            self.task_dock.show()
            self.statusBar().showMessage(f"已提交：{FEATURES.get(feature_id, feature_id)}")
            self.poll_tasks()
            return task_id
        except Exception as error:  # noqa: BLE001 - user-facing service boundary
            show_error(self, "任务未能开始", error)
            return None

    def account_count(self):
        if not self.project:
            return
        datasets = [
            obj
            for obj in self.project.objects("dataset")
            if obj.get("state") not in ("deleted", "corrupt", "damaged", "corrupted", "missing")
        ]
        if not datasets:
            show_error(self, "还没有可用数据", "请先导入一份 Excel 数据。")
            return
        current = next(
            (
                index
                for index, obj in enumerate(datasets)
                if self.current_object and obj["id"] == self.current_object["id"]
            ),
            0,
        )
        labels = [
            f"{obj['name']}  ·  {obj['data'].get('row_count', 0):,} 条  ·  {obj['id'][:8]}"
            for obj in datasets
        ]
        selected, ok = QInputDialog.getItem(
            self,
            "按账户统计输入记录数",
            "选择数据版本。重复记录保留计数，不代表去重后的经济交易数。",
            labels,
            current,
            False,
        )
        if ok:
            obj = datasets[labels.index(selected)]
            self.submit_task("validation.account_count", {}, [obj["id"]])

    def show_object(self, object_id, restore=None):
        if self.project is None:
            return
        try:
            obj = self.project.get_object(object_id)
        except Exception as error:  # noqa: BLE001 - user-facing service boundary
            show_error(self, "无法打开数据", error)
            return
        self.current_object = obj
        self.filters = (restore or {}).get("filters", [])
        self.sort = (restore or {}).get("sort") or (
            "source_row" if obj["kind"] == "dataset" else "owner_account"
        )
        self.descending = (restore or {}).get("descending", False)
        self.offset = (restore or {}).get("offset", 0)
        self.total = 0
        self.table_model.replace({})
        self.data_title.setText(obj["name"])
        data = obj["data"]
        summary = f"{'数据版本' if obj['kind'] == 'dataset' else '结果'} · {data.get('row_count', 0):,} 条"
        if obj["kind"] == "dataset":
            summary += f" · 原工作表：{data.get('sheet', '—')}"
            if data.get("rejected_count"):
                summary += f" · 已排除错误行 {data['rejected_count']:,} 条（清单已保存）"
        else:
            completion = {"complete": "完整", "partial": "部分完成", "limited": "有范围限制"}.get(
                data.get("completion"), "—"
            )
            summary += f" · 算法版本：{data.get('algorithm_version', '—')} · 完成情况：{completion}"
            if data.get("warnings"):
                summary += "\n" + "；".join(str(value) for value in data["warnings"])
        self.data_summary.setText(summary)
        self.workspace.setCurrentWidget(self.data_page)
        self.load_page()

    def load_page(self):
        if not self.current_object or self.query is None:
            return
        self._generation += 1
        generation = self._generation
        service, object_id = self.query, self.current_object["id"]
        self._query_ready = False
        self._refresh_actions()
        filters, sort, descending, offset = list(self.filters), self.sort, self.descending, self.offset
        self.previous_page.setEnabled(False)
        self.next_page.setEnabled(False)
        self.page_label.setText("正在查询…")

        def ready(page):
            if generation != self._generation:
                return
            self.total = page["total"]
            self._query_ready = True
            priority = [
                "owner_account",
                "record_count",
                "amount",
                "currency",
                "occurred_at",
                "counterparty_account",
                "owner_name",
                "counterparty_name",
                "direction",
                "balance",
                "transaction_id",
                "source_row",
                "source_sheet",
                "platform",
                "account_namespace",
            ]
            page["columns"] = sorted(
                page["columns"],
                key=lambda column: (
                    priority.index(column["key"]) if column["key"] in priority else len(priority)
                ),
            )
            self.table_model.replace(page)
            self.filter_column.blockSignals(True)
            current = self.filter_column.currentData()
            self.filter_column.clear()
            for column in page["columns"]:
                self.filter_column.addItem(column.get("label", column["key"]), column["key"])
            selected = self.filter_column.findData(current or "owner_account")
            if selected >= 0:
                self.filter_column.setCurrentIndex(selected)
            self.filter_column.blockSignals(False)
            hidden = {
                "record_id",
                "result_row_id",
                "source_id",
                "raw_cells",
                "quality_flags",
                "amount_raw",
                "balance_raw",
                "direction_raw",
            }
            for i, column in enumerate(page["columns"]):
                self.table.setColumnHidden(i, column["key"] in hidden)
                if column["key"] in ("owner_account", "counterparty_account", "amount"):
                    self.table.setColumnWidth(i, 200)
            pages = max(1, (self.total + self.page_size - 1) // self.page_size)
            self.page_label.setText(
                f"第 {self.offset // self.page_size + 1} / {pages} 页 · 共 {self.total:,} 条"
                if self.total
                else "无符合条件记录 · 0 条"
            )
            self.previous_page.setEnabled(self.offset > 0)
            self.next_page.setEnabled(self.offset + self.page_size < self.total)
            labels = {col["key"]: col.get("label", col["key"]) for col in page["columns"]}
            operations = {
                "contains": "包含",
                "eq": "等于",
                "gte": "≥",
                "lte": "≤",
                "gt": ">",
                "lt": "<",
                "is_null": "为空",
            }
            conditions = [
                f"{labels.get(item['column'], item['column'])} {operations[item['op']]} {item.get('value') or ''}"
                for item in self.filters
            ]
            self.filter_summary.setText(
                " 且 ".join(conditions) if conditions else "显示全部记录 · 点击列标题排序"
            )
            self.time_filter_note.setVisible(
                "time_comparison" in page or self.filter_column.currentData() == "occurred_at"
            )
            if self.sort:
                index = next(
                    (i for i, column in enumerate(page["columns"]) if column["key"] == self.sort), -1
                )
                self.table.horizontalHeader().setSortIndicatorShown(True)
                self.table.horizontalHeader().setSortIndicator(
                    index, Qt.SortOrder.DescendingOrder if self.descending else Qt.SortOrder.AscendingOrder
                )
            if page["rows"]:
                self.table.selectRow(0)
            self._refresh_actions()
            if self.project and not self.project.readonly:
                self.project.save_session(self._session())
            self.statusBar().showMessage("查询完成", 2000)

        def failed(error):
            if generation == self._generation:
                self.page_label.setText("查询未完成")
                show_error(self, "无法查询数据", error)

        self._run(
            lambda: service.page(
                object_id,
                filters=filters,
                sort=sort,
                descending=descending,
                offset=offset,
                limit=self.page_size,
            ),
            ready,
            error_handler=failed,
        )

    def add_filter(self):
        column = self.filter_column.currentData()
        if not column:
            return
        operator = self.filter_operator.currentData()
        value = self.filter_value.text()
        if operator != "is_null" and not value:
            return
        translations = {label: key for key, label in ENUM_LABELS.get(column, {}).items()}
        value = translations.get(value, value)
        self.filters.append(
            {"column": column, "op": operator, "value": None if operator == "is_null" else value}
        )
        self.offset = 0
        self.load_page()

    def clear_filters(self):
        self.filters = []
        self.offset = 0
        self.filter_value.clear()
        self.load_page()

    def sort_column(self, section):
        if section >= len(self.table_model.columns):
            return
        key = self.table_model.columns[section]["key"]
        self.descending = not self.descending if self.sort == key else False
        self.sort = key
        self.offset = 0
        self.load_page()

    def change_page(self, delta):
        self.offset = max(0, self.offset + delta * self.page_size)
        self.load_page()

    def show_sources(self, *_):
        if self.current_object is None:
            return
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        row = self.table_model.rows[rows[0].row()]
        row_id = row.get("result_row_id") or row.get("record_id")
        if not row_id:
            return
        dialog = SourcesDialog(self.project, self.current_object["id"], row_id, self)
        self._dialogs.append(dialog)
        dialog.exec()

    def export_data(self):
        if not self.current_object or not self.project or self.project.readonly or not self._query_ready:
            return
        obj = self.current_object
        dialog = ExportDialog(
            obj["name"], self.total, obj["data"].get("row_count", 0), self.project.path / "exports", self
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            parameters = {
                **dialog.parameters(),
                "object_id": obj["id"],
                "filters": list(self.filters),
                "sort": self.sort,
                "descending": self.descending,
            }
            self.submit_task("result.export", parameters, [obj["id"]])

    def show_quality(self):
        if not self.current_object or self.current_object["kind"] != "dataset":
            return
        from traceatlas.contracts import contained_path

        relative = self.current_object["data"].get("quality_path")
        if not relative:
            return
        project = self.project
        path = contained_path(project.path, relative)

        def ready(report):
            dialog = QDialog(self)
            dialog.setWindowTitle("已保存的导入质量报告")
            dialog.resize(900, 550)
            layout = QVBoxLayout(dialog)
            label = QLabel(
                f"共检查 {report.get('input_count', 0):,} 行 · 合格 {report.get('valid_count', 0):,} 行 · 错误 {report.get('rejected_count', 0):,} 行 · 空行 {report.get('blank_count', 0):,} 行"
            )
            label.setWordWrap(True)
            layout.addWidget(label)
            note = QLabel(
                "下表展示已保存的错误和提醒样本（每类最多 200 条）。完整错误记录保存在项目内，可复制到指定位置。"
            )
            note.setWordWrap(True)
            layout.addWidget(note)
            table, model = make_table()
            from .dialogs import FIELDS

            labels = {key: label.replace(" *", "") for key, label, _ in FIELDS}
            issues = [dict(severity="错误", **item) for item in report.get("errors", [])] + [
                dict(severity="提醒", **item) for item in report.get("warnings", [])
            ]
            rows = [
                {**item, "field": labels.get(item.get("field"), item.get("field", ""))} for item in issues
            ]
            model.replace(
                {
                    "columns": [
                        {"key": "severity", "label": "级别"},
                        {"key": "row", "label": "原始行号"},
                        {"key": "field", "label": "字段"},
                        {"key": "message", "label": "原因"},
                    ],
                    "rows": rows,
                }
            )
            table.setWordWrap(True)
            table.setColumnWidth(0, 60)
            table.setColumnWidth(1, 80)
            table.setColumnWidth(2, 110)
            table.horizontalHeader().setStretchLastSection(True)
            table.resizeRowsToContents()
            layout.addWidget(table, 1)
            controls = QHBoxLayout()
            save = QPushButton("复制完整错误清单…")
            relative_errors = self.current_object["data"].get("errors_path")
            save.setEnabled(bool(relative_errors))

            def copy_errors():
                destination, _ = QFileDialog.getSaveFileName(
                    dialog, "保存错误记录（含原始单元格）", "导入错误清单.jsonl", "逐行记录文件 (*.jsonl)"
                )
                if destination:
                    source = contained_path(project.path, relative_errors)
                    if Path(destination).resolve() == source:
                        return
                    destination = Path(destination).resolve()
                    try:
                        expected = destination_snapshot(destination)
                    except (OSError, TraceAtlasError) as error:
                        show_error(dialog, "错误清单复制失败", error)
                        return
                    self._run(
                        lambda: copy_error_report(source, destination, project.path, expected),
                        lambda _: self.statusBar().showMessage("完整错误清单已复制。", 5000),
                        title="错误清单复制失败",
                    )

            save.clicked.connect(copy_errors)
            controls.addWidget(save)
            controls.addStretch()
            close = QPushButton("关闭")
            close.clicked.connect(dialog.accept)
            controls.addWidget(close)
            layout.addLayout(controls)
            dialog.exec()

        self._run(lambda: json.loads(path.read_text(encoding="utf-8")), ready, title="无法读取质量报告")

    def search_index(self, checked=False, *, offset=0):
        if self.query is None:
            return
        query = self.index_input.text()
        self.index_offset = offset
        self.index_notice.setText("正在查询项目内索引…")
        service = self.query
        self._generation += 1
        generation = self._generation

        def ready(page):
            if generation != self._generation:
                return
            self.index_model.replace(page)
            self.index_total = page["total"]
            self.index_notice.setText(
                page.get("warning") or f"找到 {page['total']:,} 条匹配 · 第 {offset // 200 + 1} 页"
            )
            self.index_previous.setEnabled(offset > 0)
            self.index_next.setEnabled(offset + 200 < page["total"])

        self._run(
            lambda: service.search_index(query, offset=offset, limit=200), ready, title="索引查询未完成"
        )

    def rebuild_index(self):
        if self.project:
            identifiers = [
                obj["id"]
                for obj in self.project.objects("dataset")
                if obj.get("state") not in ("deleted", "corrupt", "damaged", "missing", "corrupted")
            ]
            if identifiers:
                self.submit_task("index.rebuild", {}, identifiers)

    def open_index_source(self, index):
        if not index.isValid():
            return
        row = self.index_model.rows[index.row()]
        dialog = SourcesDialog(self.project, row["dataset_version"], row["record_id"], self)
        self._dialogs.append(dialog)
        dialog.exec()

    def delete_result(self):
        if not self.current_object or self.current_object["kind"] != "result":
            return
        answer = QMessageBox.question(
            self,
            "删除结果",
            f"删除“{self.current_object['name']}”？\n\n原文件和输入数据会保留。存在下游引用的结果会阻止删除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.project.delete_object(self.current_object["id"])
            self.current_object = None
            self._populate_navigation()
            self.workspace.setCurrentWidget(self.welcome_page)
            self._refresh_actions()
            self.save_project()
        except Exception as error:  # noqa: BLE001 - user-facing service boundary
            show_error(self, "无法删除结果", error)

    def cleanup(self):
        if not self.project:
            return
        answer = QMessageBox.question(
            self,
            "清理临时文件",
            "仅清理应用已确认可以移除的中断任务临时文件。已提交数据、结果、原文件和升级备份都会保留。\n\n开始清理？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            project = self.project
            self._run(
                lambda: project.cleanup_staging(),
                lambda result: self.statusBar().showMessage(f"临时文件清理完成：{display(result)}", 10000),
                title="清理未完成",
            )

    def poll_tasks(self):
        if self.manager:
            try:
                self.manager.poll()
                tasks = self.manager.list_tasks()
            except Exception as error:  # noqa: BLE001 - report scheduler errors without stopping the Qt loop
                self.task_note.setText(f"任务状态暂时不可用：{error}")
                return
        elif self.project:
            tasks = self.project.list_tasks()
        else:
            tasks = []
        snapshot = json.dumps(tasks, ensure_ascii=False, default=str, sort_keys=True)
        if snapshot != self._task_snapshot:
            previous_finished = getattr(self, "_finished_ids", set())
            selected = self._selected_task()
            selected_id = selected.get("task_id", selected.get("id")) if selected else None
            scroll_position = self.tasks_table.verticalScrollBar().value()
            self.tasks_table.clear()
            finished = set()
            for task in tasks:
                task_id = task.get("task_id", task.get("id"))
                state = task.get("state", task.get("status", ""))
                progress = task.get("progress") or {}
                completed, total = (
                    task.get("completed", progress.get("completed")),
                    task.get("total", progress.get("total")),
                )
                if total:
                    label = f"{completed or 0:,} / {total:,}"
                elif completed:
                    label = f"已处理 {completed:,}"
                else:
                    label = "—"
                stage = task.get("stage", progress.get("stage", "")) or ""
                message = task.get("message", progress.get("message", "")) or ""
                if state == "succeeded":
                    stage, label = "已保存", "完成"
                    summary = task.get("summary") or {}
                    if task["feature_id"] == "xlsx.import":
                        message = f"已导入 {summary.get('row_count', 0):,} 条；错误行 {summary.get('rejected_count', 0):,} 条"
                    elif task["feature_id"] == "validation.account_count":
                        message = f"{summary.get('contributing_count', 0):,} 条记录 → {summary.get('row_count', 0):,} 个账户"
                    elif task["feature_id"] == "result.export":
                        message = f"导出完成：{task.get('parameters', {}).get('destination', '')}"
                    else:
                        message = "项目内索引已保存"
                elif state in ("failed", "interrupted"):
                    message = (task.get("error") or {}).get("message") or message
                item = QTreeWidgetItem(
                    [
                        FEATURES.get(task["feature_id"], task["feature_id"]),
                        STATES.get(state, state),
                        STAGES.get(stage, stage),
                        label,
                        message,
                    ]
                )
                item.setData(0, Qt.ItemDataRole.UserRole, task)
                self.tasks_table.addTopLevelItem(item)
                if task_id == selected_id:
                    self.tasks_table.setCurrentItem(item)
                if state == "succeeded":
                    finished.add(task_id)
            self._finished_ids = finished
            self.tasks_table.verticalScrollBar().setValue(scroll_position)
            self._task_snapshot = snapshot
            if finished != previous_finished:
                self._populate_navigation()
                self._load_modules()
                self._refresh_actions()
                self.statusBar().showMessage("后台任务已完成，数据和结果已保存。", 7000)
            self._refresh_task_actions()
        if (
            self._deferred
            and not self._has_pending_queries()
            and not (self.manager and self.manager.has_active())
        ):
            callback = self._deferred
            self._deferred = None
            callback()

    def _selected_task(self):
        selected = self.tasks_table.selectedItems()
        return selected[0].data(0, Qt.ItemDataRole.UserRole) if selected else None

    def _refresh_task_actions(self):
        if not hasattr(self, "tasks_table"):
            return
        task = self._selected_task()
        state = task.get("state", task.get("status")) if task else None
        self.cancel_task_button.setEnabled(
            self.manager is not None and state in ("queued", "running", "cancel_requested")
        )
        self.retry_task_button.setEnabled(
            self.manager is not None and state in ("failed", "cancelled", "interrupted")
        )
        self.task_details_button.setEnabled(task is not None)

    def cancel_task(self):
        task = self._selected_task()
        if task and self.manager:
            try:
                self.manager.cancel(task.get("task_id", task.get("id")))
                self.poll_tasks()
            except Exception as error:  # noqa: BLE001 - user-facing service boundary
                show_error(self, "无法取消任务", error)

    def retry_task(self):
        task = self._selected_task()
        if task and self.manager:
            try:
                self.manager.retry(task.get("task_id", task.get("id")))
                self.poll_tasks()
            except Exception as error:  # noqa: BLE001 - user-facing service boundary
                show_error(self, "无法重试任务", error)

    def task_details(self, *_):
        task = self._selected_task()
        if not task:
            return
        error = task.get("error") or {}
        message = error.get("message") if isinstance(error, dict) else str(error)
        state = task.get("state", task.get("status", ""))
        box = QMessageBox(self)
        box.setWindowTitle("任务详情")
        box.setText(f"{FEATURES.get(task['feature_id'], task['feature_id'])} · {STATES.get(state, state)}")
        info = message or task.get("message") or "任务参数和诊断记录见详细信息。"
        if task.get("summary"):
            info += "\n" + display(task["summary"])
        box.setInformativeText(info)
        box.setDetailedText(json.dumps(task, ensure_ascii=False, indent=2, default=str))
        box.exec()

    def about(self):
        QMessageBox.about(
            self,
            "关于 TraceAtlas",
            "TraceAtlas 0.1.0\n\n本机资金数据分析工作区。\n本版本提供项目保存、Excel 导入、账户计数、来源追溯、查询导出、后台任务和项目内索引。\n\n规模能力以实际测试结果为准。",
        )

    def closeEvent(self, event: QCloseEvent):
        if self._closing:
            event.accept()
            return
        if self._has_pending_queries() or (self.manager and self.manager.has_active()):
            event.ignore()

            def finish():
                self._release_project()
                self.settings.setValue("window/geometry", self.saveGeometry())
                self._closing = True
                self.close()

            self._prepare_switch(finish)
            return
        self._release_project()
        self.settings.setValue("window/geometry", self.saveGeometry())
        event.accept()
