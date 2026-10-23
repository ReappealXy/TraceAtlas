"""TraceAtlas desktop: three layouts over the same real project workflow."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QThreadPool, QTimer
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDockWidget,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLayout,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTabBar,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from traceatlas.contracts import TraceAtlasError

from .common import (
    ENUM_LABELS,
    BackgroundCall,
    copy_error_report,
    destination_snapshot,
    display,
    make_table,
    show_error,
)
from .dialogs import ExportDialog, ImportDialog, NewProjectDialog
from .home import HomePage
from .inspector import SourceInspector
from .settings_dialog import SettingsDialog
from .sources import SourcesDialog
from .theme import (
    DEFAULT_LAYOUT,
    DEFAULT_THEME,
    LAYOUT_LABELS,
    LAYOUTS,
    THEME_LABELS,
    THEMES,
    Tokens,
    apply_appearance,
    mono_family,
    motion_enabled,
    normalize_layout,
    normalize_theme,
    resolve_mode,
    tokens,
)
from .widgets import GridPanel, Sparkline

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

PAGE_TABS = ("项目概览", "标准流水", "项目内查找", "模块与能力")
NAV_WIDTHS = {"folio": 250, "terminal": 208, "network": 234}
DOCK_TITLES = {"folio": "后台任务 · 记录条", "terminal": "后台任务 · 任务流水", "network": "后台任务 · 任务面板"}
PAGE_MARGINS = {"folio": (22, 16, 22, 12), "terminal": (14, 8, 14, 8), "network": (18, 14, 18, 12)}


def repolish(widget: QWidget) -> None:
    """Re-apply the application stylesheet after an object name change."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


class InstrumentPanel(QWidget):
    """Terminal layout: real counters and a real current-page amount signal."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("instrumentPanel")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(10)
        self.values: dict[str, QLabel] = {}
        for key, caption in (
            ("object", "当前对象"),
            ("records", "记录"),
            ("page", "当前页"),
            ("filters", "筛选"),
            ("tasks", "后台任务"),
        ):
            block = QVBoxLayout()
            block.setSpacing(0)
            block.addWidget(self._caption(caption))
            value = QLabel("—")
            value.setObjectName("metricValue")
            if key == "object":
                value.setMinimumWidth(0)
                value.setMaximumWidth(140)
                value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            block.addWidget(value)
            layout.addLayout(block, 1 if key == "object" else 0)
            self.values[key] = value
        signal = QVBoxLayout()
        signal.setSpacing(0)
        signal.addWidget(self._caption("当前页金额分布"))
        self.sparkline = Sparkline()
        self.sparkline.setMinimumWidth(60)
        self.sparkline.setMaximumWidth(260)
        self.sparkline.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.sparkline.setFixedHeight(54)
        signal.addWidget(self.sparkline)
        layout.addLayout(signal, 2)

    @staticmethod
    def _caption(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("metricLabel")
        return label

    def set_metrics(self, values: dict[str, str]) -> None:
        for key, value in values.items():
            if key in self.values:
                self.values[key].setText(value)
                self.values[key].setToolTip(value)

    def set_amounts(self, values: list[float], *, empty_text: str = "当前页没有可绘制的金额") -> None:
        self.sparkline.set_empty_text(empty_text)
        self.sparkline.set_values(values)

    def apply_theme(self, scheme: Tokens) -> None:
        self.sparkline.set_tokens(scheme)

    def clear(self) -> None:
        for value in self.values.values():
            value.setText("—")
        self.sparkline.set_values([])


class MainWindow(QMainWindow):
    def __init__(self, project=None):
        super().__init__()
        self.setWindowTitle("TraceAtlas")
        screen = self.screen().availableGeometry()
        self.resize(min(1400, screen.width() - 40), min(900, screen.height() - 60))
        self.setMinimumSize(1000, 680)
        self.settings = QSettings("TraceAtlas", "TraceAtlas")
        self.layout_id = normalize_layout(self.settings.value("ui/layout", DEFAULT_LAYOUT))
        self.theme_setting = normalize_theme(self.settings.value("ui/theme", DEFAULT_THEME))
        self.motion_paused = _as_bool(self.settings.value("ui/motion_paused", False))
        self._scheme: Tokens = tokens(self.layout_id, resolve_mode(self.theme_setting))
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
        self._syncing_tabs = False
        self._finished_ids = set()
        self._latest_task = None
        self._notice_text = ""
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(2)
        self._create_actions()
        self._create_shell()
        self._create_tasks()
        self._apply_layout(self.layout_id, persist=False)
        self._refresh_actions()
        self._mode_label = QLabel("")
        self._mode_label.setObjectName("muted")
        self.statusBar().addPermanentWidget(self._mode_label)
        self.statusBar().showMessage("本机项目 · 完成的操作自动保存")
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.poll_tasks)
        self.timer.start()
        if project is not None:
            self.set_project(project)
        self._watch_system_theme()
        geometry = self.settings.value("window/geometry")
        if geometry:
            self.restoreGeometry(geometry)

    # -- construction ---------------------------------------------------
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
        self.settings_action = self._action("设置…", self.open_settings, "Ctrl+,")
        self.search_action = self._action("查找最近项目", self.focus_home_search, "Ctrl+K")
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

        view_menu = self.menuBar().addMenu("视图")
        self.view_menu = view_menu
        self.layout_menu = view_menu.addMenu("工作界面布局")
        self.layout_group = QActionGroup(self)
        self.layout_group.setExclusive(True)
        self.layout_actions: dict[str, QAction] = {}
        for layout in LAYOUTS:
            action = QAction(f"{LAYOUT_LABELS[layout]}", self, checkable=True)
            action.setChecked(layout == self.layout_id)
            action.triggered.connect(lambda _=False, value=layout: self.set_layout(value))
            self.layout_group.addAction(action)
            self.layout_menu.addAction(action)
            self.layout_actions[layout] = action
        self.theme_menu = view_menu.addMenu("主题")
        self.theme_group = QActionGroup(self)
        self.theme_group.setExclusive(True)
        self.theme_actions: dict[str, QAction] = {}
        for theme in THEMES:
            action = QAction(THEME_LABELS[theme], self, checkable=True)
            action.setChecked(theme == self.theme_setting)
            action.triggered.connect(lambda _=False, value=theme: self.set_theme(value))
            self.theme_group.addAction(action)
            self.theme_menu.addAction(action)
            self.theme_actions[theme] = action
        self.motion_action = QAction("暂停界面动效", self, checkable=True)
        self.motion_action.setChecked(self.motion_paused)
        self.motion_action.triggered.connect(self.set_motion_paused)
        view_menu.addAction(self.motion_action)
        view_menu.addSeparator()
        view_menu.addAction(self.search_action)
        view_menu.addAction(self.settings_action)

        help_menu = self.menuBar().addMenu("帮助")
        help_menu.addAction(self._action("关于 TraceAtlas", self.about))

        toolbar = QToolBar("常用操作", self)
        toolbar.setMovable(False)
        for action in (self.new_action, self.open_action, self.save_action):
            toolbar.addAction(action)
        toolbar.addSeparator()
        for action in (self.import_action, self.count_action, self.export_action):
            toolbar.addAction(action)
        toolbar.addSeparator()
        toolbar.addAction(self.settings_action)
        self.addToolBar(toolbar)

    def _create_shell(self):
        self.nav_root = QWidget()
        self.nav_root.setObjectName("navRoot")
        nav_layout = QVBoxLayout(self.nav_root)
        nav_layout.setContentsMargins(16, 16, 12, 12)
        nav_layout.setSpacing(6)
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
        self.navigation.setMinimumWidth(180)
        self.navigation.itemSelectionChanged.connect(self._navigate)
        nav_layout.addWidget(self.navigation, 1)
        self.nav_settings_button = QPushButton("设置与外观…")
        self.nav_settings_button.setObjectName("quiet")
        self.nav_settings_button.clicked.connect(self.open_settings)
        nav_layout.addWidget(self.nav_settings_button)

        self.workspace = QStackedWidget()
        self.workspace.setObjectName("workspace")
        self.home = HomePage()
        self.welcome_page = self.home
        self.workspace.addWidget(self.home)
        self.data_page = self._data_page()
        self.workspace.addWidget(self.data_page)
        self.index_page = self._index_page()
        self.workspace.addWidget(self.index_page)
        self.modules_page = self._modules_page()
        self.workspace.addWidget(self.modules_page)

        center = QWidget()
        self.center = center
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(0)
        self.instrument = InstrumentPanel()
        center_layout.addWidget(self.instrument)
        self.notice_label = QLabel()
        self.notice_label.setObjectName("notice")
        self.notice_label.setWordWrap(True)
        self.notice_label.hide()
        center_layout.addWidget(self.notice_label)
        self.page_tabs = QTabBar()
        self.page_tabs.setObjectName("pageTabs")
        self.page_tabs.setExpanding(False)
        for title in PAGE_TABS:
            self.page_tabs.addTab(title)
        self.page_tabs.setCurrentIndex(0)
        self.page_tabs.currentChanged.connect(self._tab_selected)
        self.workspace.currentChanged.connect(self._sync_page_tabs)
        self.workspace.currentChanged.connect(self._update_inspector_visibility)
        center_layout.addWidget(self.page_tabs)
        self.workspace_backdrop = GridPanel()
        backdrop_layout = QVBoxLayout(self.workspace_backdrop)
        backdrop_layout.setContentsMargins(0, 0, 0, 0)
        backdrop_layout.addWidget(self.workspace)
        center_layout.addWidget(self.workspace_backdrop, 1)
        self.log_line = QLabel("")
        self.log_line.setObjectName("logLine")
        self.log_line.setContentsMargins(16, 4, 16, 6)
        center_layout.addWidget(self.log_line)

        self.inspector = SourceInspector()
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setObjectName("shellSplitter")
        self.splitter.addWidget(self.nav_root)
        self.splitter.addWidget(center)
        self.splitter.addWidget(self.inspector)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setSizes([NAV_WIDTHS[self.layout_id], 900, 300])
        self.setCentralWidget(self.splitter)

        self.home.newProjectRequested.connect(self.new_project)
        self.home.openProjectRequested.connect(self.choose_project)
        self.home.continueProjectRequested.connect(self.continue_project)
        self.home.projectRequested.connect(self.open_project)
        self.home.importRequested.connect(self.import_data)
        self.home.resumeRequested.connect(self.return_to_workspace)
        self.home.settingsRequested.connect(self.open_settings)
        self.home.layoutRequested.connect(self.set_layout)
        self.inspector.sourcesRequested.connect(self.show_sources)
        self.inspector.qualityRequested.connect(self.show_quality)
        self._populate_navigation()
        self._refresh_home()

    def _data_page(self):
        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        page = QWidget()
        page.setObjectName("pageSurface")
        layout = QVBoxLayout(page)
        self.data_layout = layout
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        page.setMinimumWidth(580)
        layout.setContentsMargins(*PAGE_MARGINS[self.layout_id])
        layout.setSpacing(7)
        header = QHBoxLayout()
        title_column = QVBoxLayout()
        title_column.setSpacing(2)
        self.data_object_name = QLabel("数据")
        self.data_object_name.setObjectName("pageTitle")
        self.data_object_name.setWordWrap(True)
        title_column.addWidget(self.data_object_name)
        self.data_summary = QLabel()
        self.data_summary.setWordWrap(True)
        self.data_summary.setObjectName("muted")
        title_column.addWidget(self.data_summary)
        header.addLayout(title_column, 1)
        self.data_import_button = QPushButton("导入数据")
        self.data_import_button.clicked.connect(self.import_data)
        self.data_source_button = QPushButton("查看来源")
        self.data_source_button.clicked.connect(self.show_sources)
        self.data_export_button = QPushButton("导出…")
        self.data_export_button.clicked.connect(self.export_data)
        for button in (self.data_import_button, self.data_source_button, self.data_export_button):
            header.addWidget(button)
        layout.addLayout(header)
        filter_row = QHBoxLayout()
        filter_row.setSpacing(6)
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
        filter_row.addStretch(1)
        layout.addLayout(filter_row)
        layout.addWidget(self.time_filter_note)
        self.filter_summary = QLabel("显示全部记录 · 点击列标题排序")
        self.filter_summary.setWordWrap(True)
        self.filter_summary.setObjectName("muted")
        layout.addWidget(self.filter_summary)
        self.table, self.table_model = make_table()
        self.table.setObjectName("dataTable")
        self.table.setMinimumHeight(180)
        self.table.horizontalHeader().setSectionsClickable(True)
        self.table.horizontalHeader().sectionClicked.connect(self.sort_column)
        self.table.doubleClicked.connect(self.show_sources)
        self.table.selectionModel().selectionChanged.connect(self._on_row_selection)
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
        scroll.setWidget(page)
        return scroll

    def _index_page(self):
        page = QWidget()
        page.setObjectName("pageSurface")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(*PAGE_MARGINS[self.layout_id])
        title = QLabel("项目内查找")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        note = QLabel("在当前项目已登记的本方 / 对方账户中查找。索引保留数据版本和原始行定位。")
        note.setWordWrap(True)
        note.setObjectName("muted")
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
        page.setObjectName("pageSurface")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(*PAGE_MARGINS[self.layout_id])
        title = QLabel("模块与能力")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        note = QLabel("当前已安装的能力及输入要求。已保存的结果保留生成它时的算法版本。")
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(note)
        self.modules_table, self.modules_model = make_table()
        layout.addWidget(self.modules_table, 1)
        return page

    def _create_tasks(self):
        dock = QDockWidget(DOCK_TITLES[self.layout_id], self)
        dock.setObjectName("taskDock")
        dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable)
        widget = QWidget()
        widget.setObjectName("taskDockBody")
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 6, 8, 8)
        self.tasks_table = QTreeWidget()
        self.tasks_table.setObjectName("taskList")
        self.tasks_table.setHeaderLabels(["任务", "状态", "阶段", "进度", "说明"])
        self.tasks_table.setRootIsDecorated(False)
        self.tasks_table.setMinimumHeight(92)
        self.tasks_table.setMaximumHeight(132)
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
        for button in (self.cancel_task_button, self.retry_task_button):
            row.addWidget(button)
        self.task_details_button = QPushButton("详情")
        self.task_details_button.clicked.connect(self.task_details)
        row.addWidget(self.task_details_button)
        layout.addLayout(row)
        dock.setWidget(widget)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        self.resizeDocks([dock], [148], Qt.Orientation.Vertical)
        self.task_dock = dock
        self.view_menu_action = dock.toggleViewAction()
        self.view_menu_action.setText("显示后台任务区")
        self.view_menu.addAction(self.view_menu_action)

    # -- appearance -----------------------------------------------------
    def _watch_system_theme(self):
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        hints = app.styleHints() if app is not None else None
        signal = getattr(hints, "colorSchemeChanged", None)
        if signal is None:
            return
        try:
            signal.connect(self._system_scheme_changed)
        except (RuntimeError, TypeError):
            pass

    def _system_scheme_changed(self, *_args):
        if self.theme_setting == "system":
            self._apply_appearance()

    def _apply_appearance(self):
        mode = resolve_mode(self.theme_setting)
        self._scheme = apply_appearance(self.layout_id, mode)
        label = THEME_LABELS[self.theme_setting]
        self.home.apply_theme(self.layout_id, self._scheme, label)
        self.home.set_motion(motion_enabled(self.motion_paused))
        self.inspector.apply_theme(self._scheme)
        self.instrument.apply_theme(self._scheme)
        self.workspace_backdrop.set_tokens(self._scheme)
        family = mono_family(self._scheme)
        self.table_model.mono_family = family
        self.index_model.mono_family = family
        self._sync_appearance_actions()
        self._update_mode_label()

    def _update_mode_label(self):
        if getattr(self, "_mode_label", None) is None:
            return
        paused = " · 动效暂停" if self.motion_paused else ""
        self._mode_label.setText(
            f"{LAYOUT_LABELS[self.layout_id]} · {THEME_LABELS[self.theme_setting]}{paused}"
        )

    def _sync_appearance_actions(self):
        for layout, action in getattr(self, "layout_actions", {}).items():
            action.setChecked(layout == self.layout_id)
        for theme, action in getattr(self, "theme_actions", {}).items():
            action.setChecked(theme == self.theme_setting)
        if hasattr(self, "motion_action"):
            self.motion_action.setChecked(self.motion_paused)

    def set_layout(self, layout: str):
        self._apply_layout(layout)

    def _apply_layout(self, layout: str, *, persist: bool = True):
        layout = normalize_layout(layout)
        self.layout_id = layout
        self.instrument.setVisible(layout == "terminal")
        self.log_line.setVisible(layout == "terminal")
        self.workspace_backdrop.set_grid_visible(layout == "network")
        target = "boxedTab" if layout == "folio" else "pageTabs"
        if self.page_tabs.objectName() != target:
            self.page_tabs.setObjectName(target)
            repolish(self.page_tabs)
        self.page_tabs.setDrawBase(layout == "folio")
        self.nav_root.setMinimumWidth(0)
        self.navigation.setMinimumWidth(NAV_WIDTHS[layout] - 40)
        self.task_dock.setWindowTitle(DOCK_TITLES[layout])
        for page in (self.index_page, self.modules_page):
            page.layout().setContentsMargins(*PAGE_MARGINS[layout])
        self.data_layout.setContentsMargins(*PAGE_MARGINS[layout])
        self.home.set_layout(layout)
        self.splitter.setSizes([NAV_WIDTHS[layout], 900, 300])
        self._update_inspector_visibility()
        self._apply_appearance()
        self._update_metrics()
        if persist:
            self.settings.setValue("ui/layout", layout)
            self.statusBar().showMessage(f"工作界面已切换为{LAYOUT_LABELS[layout]}", 4000)

    def set_theme(self, theme: str):
        self.theme_setting = normalize_theme(theme)
        self.settings.setValue("ui/theme", self.theme_setting)
        self._apply_appearance()
        self.statusBar().showMessage(f"主题：{THEME_LABELS[self.theme_setting]}", 4000)

    def set_motion_paused(self, paused: bool):
        self.motion_paused = bool(paused)
        self.settings.setValue("ui/motion_paused", self.motion_paused)
        # Motion does not change colours, so avoid a full stylesheet re-polish.
        self.home.set_motion(motion_enabled(self.motion_paused))
        self.motion_action.setChecked(self.motion_paused)
        self._update_mode_label()
        self.statusBar().showMessage("界面动效已暂停" if self.motion_paused else "界面动效已恢复", 4000)

    def open_settings(self):
        dialog = SettingsDialog(
            self.layout_id, self.theme_setting, self.motion_paused, self.settings, self
        )
        dialog.layoutChanged.connect(self.set_layout)
        dialog.themeChanged.connect(self.set_theme)
        dialog.motionChanged.connect(self.set_motion_paused)
        self._dialogs.append(dialog)
        dialog.exec()

    def _update_inspector_visibility(self):
        on_home = self.workspace.currentWidget() is self.home
        self.inspector.setVisible(not on_home)
        self.inspector.set_object(self.current_object)
        self.inspector.set_row(self._current_row() if self.current_object else None)

    # -- recent projects and home ---------------------------------------
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

    def recent_projects(self) -> list[dict]:
        entries = []
        for path in self._recent():
            folder = Path(path)
            entries.append(
                {
                    "path": str(folder),
                    "name": folder.name or str(folder),
                    "meta": str(folder.parent) + ("" if folder.is_dir() else " · 位置已找不到"),
                }
            )
        return entries

    def _refresh_home(self):
        self.home.set_projects(self.recent_projects())
        self.home.set_context(
            has_project=self.project is not None,
            writable=self.project is not None and not self.project.readonly,
            current_name=self.project.name if self.project is not None else "",
        )

    def _set_notice(self, text: str):
        self._notice_text = text
        self.notice_label.setText(text)
        self.notice_label.setVisible(bool(text))
        self.home.set_notice(text)

    def focus_home_search(self):
        if self.workspace.currentWidget() is not self.home:
            self.workspace.setCurrentWidget(self.home)
            self._sync_page_tabs()
        self.home.focus_search()

    def continue_project(self):
        projects = self.recent_projects()
        if not projects:
            self.statusBar().showMessage("还没有最近项目，请先新建或打开一个项目。", 6000)
            return
        target = projects[0]
        if not Path(target["path"]).is_dir():
            show_error(
                self,
                "上次项目不在原位置",
                f"找不到：{target['path']}\n项目可能被移动或删除。可以用“打开项目”重新选择它的文件夹。",
            )
            return
        self.open_project(target["path"])

    def return_to_workspace(self):
        if self.project is None:
            return
        if self.current_object is not None:
            self.workspace.setCurrentWidget(self.data_page)
        else:
            self.workspace.setCurrentWidget(self.home)
        self._sync_page_tabs()

    def _refresh_recent_menu(self):
        self.recent_menu.clear()
        paths = self._recent()
        self.recent_menu.setEnabled(bool(paths))
        for path in paths:
            action = self.recent_menu.addAction(str(path))
            action.triggered.connect(lambda checked=False, value=path: self.open_project(value))

    # -- project lifecycle ----------------------------------------------
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
        report = getattr(project, "recovery_report", [])
        notice = "\n".join(
            (["项目以只读方式打开，可以查看已提交的数据和结果，不能运行新任务。"] if project.readonly else [])
            + [str(line) for line in report]
        )
        self._set_notice(notice)
        paths = [str(project.path)] + [value for value in self._recent() if value != str(project.path)]
        self.settings.setValue("projects/recent", paths[:10])
        self._refresh_recent_menu()
        self._populate_navigation()
        self._load_modules()
        self._refresh_actions()
        self.workspace.setCurrentWidget(self.home)
        self._sync_page_tabs()
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
            overview.setToolTip(
                0,
                f"本项目有 {datasets.childCount()} 个数据版本、{results.childCount()} 份结果。"
                "原文件和已提交结果在项目中独立保存。",
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
            self.workspace.setCurrentWidget(self.home)
        self._sync_page_tabs()

    def _tab_selected(self, index: int):
        if self._syncing_tabs or index < 0:
            return
        if index == 0:
            self.workspace.setCurrentWidget(self.home)
        elif index == 1:
            self.workspace.setCurrentWidget(
                self.data_page if self.current_object is not None else self.home
            )
        elif index == 2:
            self.workspace.setCurrentWidget(self.index_page)
        else:
            self._load_modules()
            self.workspace.setCurrentWidget(self.modules_page)
        self._sync_page_tabs()

    def _sync_page_tabs(self, *_args):
        self._syncing_tabs = True
        try:
            current = self.workspace.currentWidget()
            mapping = {self.home: 0, self.data_page: 1, self.index_page: 2, self.modules_page: 3}
            self.page_tabs.setCurrentIndex(mapping.get(current, 0))
            label = "标准流水" if self.current_object is None else self.current_object["name"]
            self.page_tabs.setTabText(1, label if len(label) <= 18 else label[:17] + "…")
            self.page_tabs.setTabToolTip(1, label)
        finally:
            self._syncing_tabs = False

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
        self.data_source_button.setEnabled(has_row)
        self.data_export_button.setEnabled(
            writable and self.current_object is not None and self._query_ready
        )
        self.data_import_button.setEnabled(writable)
        self.inspector.set_actions(has_row=has_row, is_dataset=bool(
            self.current_object and self.current_object["kind"] == "dataset"
        ))
        self._refresh_home()
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
        self._set_notice("")
        self.inspector.set_object(None)
        self.instrument.clear()
        self.log_line.setText("")
        self.workspace.setCurrentWidget(self.home)
        self._sync_page_tabs()
        self._populate_navigation()
        self._refresh_actions()
        self._refresh_home()

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
        self.data_object_name.setText(obj["name"])
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
        self.inspector.set_object(obj)
        self.workspace.setCurrentWidget(self.data_page)
        self._sync_page_tabs()
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
            self._on_row_selection()
            self._update_metrics()
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

    def _current_row(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows or not self.table_model.rows:
            return None
        index = rows[0].row()
        return self.table_model.rows[index] if index < len(self.table_model.rows) else None

    def _on_row_selection(self, *_args):
        self.inspector.set_row(self._current_row())
        self._refresh_actions()

    def _update_metrics(self):
        if not hasattr(self, "instrument"):
            return
        obj = self.current_object
        rows = len(self.table_model.rows)
        pages = max(1, (self.total + self.page_size - 1) // self.page_size) if self.total else 0
        page_number = (self.offset // self.page_size + 1) if self.total else 0
        running = 0
        if self.manager is not None:
            try:
                running = sum(
                    1 for task in self.manager.list_tasks() if task.get("state") in ACTIVE
                )
            except Exception:  # noqa: BLE001 - counters never block the window
                running = 0
        self.instrument.set_metrics(
            {
                "object": (obj["name"] if obj else "—"),
                "records": (f"{self.total:,}" if self.total else "0") if obj else "—",
                "page": f"{page_number}/{pages}" if pages else "—",
                "filters": f"{len(self.filters)} 条" if obj else "—",
                "tasks": f"{running} 运行中",
            }
        )
        amounts = []
        for row in self.table_model.rows[:200]:
            value = row.get("amount")
            if value is None:
                continue
            try:
                amounts.append(float(str(value)))
            except (TypeError, ValueError):
                continue
        self.instrument.set_amounts(
            amounts,
            empty_text="尚未打开数据" if obj is None else "当前页没有可绘制的金额",
        )
        self._update_log_line(rows)

    @staticmethod
    def _task_summary(task: dict) -> str:
        state = task.get("state", task.get("status", ""))
        feature = task.get("feature_id", "")
        summary = task.get("summary") or {}
        if state == "succeeded":
            if feature == "xlsx.import":
                return f"已导入 {summary.get('row_count', 0):,} 条，错误行 {summary.get('rejected_count', 0):,} 条"
            if feature == "validation.account_count":
                return (
                    f"{summary.get('contributing_count', 0):,} 条记录 → "
                    f"{summary.get('row_count', 0):,} 个账户"
                )
            if feature == "result.export":
                return f"已导出到 {task.get('parameters', {}).get('destination', '')}"
            return "项目内索引已保存"
        error = task.get("error") or {}
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        return str(task.get("message", "") or "")

    def _update_log_line(self, rows: int | None = None):
        if not hasattr(self, "log_line"):
            return
        task = self._latest_task
        if task:
            state = task.get("state", task.get("status", ""))
            stage = task.get("stage", "") or ""
            detail = self._task_summary(task)
            text = (
                f"{FEATURES.get(task.get('feature_id', ''), task.get('feature_id', ''))}"
                f" · {STATES.get(state, state)}"
                + (f" · {STAGES.get(stage, stage)}" if stage and state not in ("succeeded",) else "")
                + (f" · {detail}" if detail else "")
            )
        elif self.current_object is not None:
            text = (
                f"{'数据' if self.current_object['kind'] == 'dataset' else '结果'}"
                f" · {self.total:,} 条 · 当前页 {rows if rows is not None else len(self.table_model.rows)} 行"
                " · 查询与来源在同一条记录链上"
            )
        else:
            text = "本地会话就绪 · 选择项目与数据后开始核查"
        self.log_line.setText(text)

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
            self.inspector.set_object(None)
            self._populate_navigation()
            self.workspace.setCurrentWidget(self.home)
            self._sync_page_tabs()
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
        self._latest_task = tasks[0] if tasks else None
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
            self._update_metrics()
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
            "TraceAtlas 0.1.0\n\n本机资金数据分析工作区。\n本版本提供项目保存、Excel 导入、账户计数、来源追溯、查询导出、后台任务和项目内索引。\n"
            "文件册、金融终端、关联画布三种工作界面与默认/亮/暗主题只改变界面组织与颜色，不改变数据与结果。\n\n规模能力以实际测试结果为准。",
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


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
