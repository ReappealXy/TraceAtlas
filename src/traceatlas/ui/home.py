"""Start page for the three approved layouts.

Every layout exposes the same three real entries (create, open, continue the
last project) and the same real recent-project list. Only composition and
visual language differ; the canvases are decorative and never intercept clicks.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .theme import LAYOUT_LABELS, LAYOUTS, Tokens, tokens
from .widgets import NetworkCanvas, RecentRow


def _button(text: str, name: str) -> QPushButton:
    button = QPushButton(text)
    button.setObjectName(name)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setAutoDefault(False)
    return button


def _scrollable(host: QWidget, margins: tuple[int, int, int, int], spacing: int) -> QVBoxLayout:
    """Page content inside a scroll area so a short window keeps every entry reachable."""
    outer = QVBoxLayout(host)
    outer.setContentsMargins(0, 0, 0, 0)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    inner = QWidget()
    inner_layout = QVBoxLayout(inner)
    inner_layout.setContentsMargins(*margins)
    inner_layout.setSpacing(spacing)
    scroll.setWidget(inner)
    outer.addWidget(scroll)
    return inner_layout


def _label(text: str, name: str = "muted", *, wrap: bool = False) -> QLabel:
    label = QLabel(text)
    label.setObjectName(name)
    label.setWordWrap(wrap)
    return label


class _HomeView(QWidget):
    """Shared plumbing: entries, recent list, filter and state propagation."""

    newRequested = Signal()
    openRequested = Signal()
    continueRequested = Signal()
    pathRequested = Signal(str)
    importRequested = Signal()
    resumeRequested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("homeRoot")
        self._projects: list[dict] = []
        self._layout = "folio"
        self._scheme: Tokens = tokens("folio", "light")
        self._motion = True
        self._theme_label = "默认"
        self._has_project = False
        self._current_name = ""
        self.new_button = _button("＋  新建项目", "entryLarge")
        self.open_button = _button("打开已有项目  →", "entryWide")
        self.continue_button = _button("继续上次项目  →", "entryWide")
        self.resume_button = _button("返回当前工作区  →", "entryWide")
        self.import_button = _button("导入 Excel…", "entryWide")
        self.new_button.clicked.connect(self.newRequested.emit)
        self.open_button.clicked.connect(self.openRequested.emit)
        self.continue_button.clicked.connect(self.continueRequested.emit)
        self.resume_button.clicked.connect(self.resumeRequested.emit)
        self.import_button.clicked.connect(self.importRequested.emit)
        self.resume_button.hide()
        self.import_button.hide()
        self.search = QLineEdit()
        self.search.setObjectName("searchLine")
        self.search.setPlaceholderText("按项目名称或位置查找")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        self.list_host = QWidget()
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(0)
        self.list_layout.addStretch(1)
        self.empty_label = _label("", wrap=True)
        self.empty_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)

    # -- state ---------------------------------------------------------
    def set_projects(self, projects: list[dict]) -> None:
        self._projects = list(projects)
        self._apply_filter()

    def set_context(self, *, has_project: bool, writable: bool, current_name: str = "") -> None:
        self._has_project = has_project
        self._current_name = current_name
        self.import_button.setVisible(has_project)
        self.import_button.setEnabled(writable)
        self.resume_button.setVisible(has_project)
        self._update_entries()
        self._render_context()

    def apply_theme(self, layout: str, scheme: Tokens, theme_label: str) -> None:
        self._layout = layout
        self._scheme = scheme
        self._theme_label = theme_label
        self._render_theme()

    def set_motion(self, enabled: bool) -> None:
        self._motion = enabled
        self._render_motion()

    # -- hooks ---------------------------------------------------------
    def _render_context(self) -> None:
        pass

    def _render_theme(self) -> None:
        pass

    def _render_motion(self) -> None:
        pass

    # -- internals -----------------------------------------------------
    def _update_entries(self) -> None:
        has_recent = bool(self._projects)
        self.continue_button.setEnabled(has_recent)
        self.continue_button.setToolTip(
            f"打开最近使用的项目：{self._projects[0]['name']}\n{self._projects[0]['path']}"
            if has_recent
            else "还没有最近项目；新建或打开一个项目后即可继续。"
        )
        self.resume_button.setText("返回当前项目  →")

    def _visible_projects(self) -> list[dict]:
        needle = self.search.text().strip().lower()
        if not needle:
            return self._projects
        return [
            item
            for item in self._projects
            if needle in item["name"].lower() or needle in item["path"].lower()
        ]

    def _apply_filter(self, *_args) -> None:
        while self.list_layout.count() > 1:
            widget = self.list_layout.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        visible = self._visible_projects()
        for index, project in enumerate(visible, start=1):
            row = RecentRow(index, project["path"])
            row.set_project(index, project["name"], project.get("meta", ""))
            row.clicked.connect(lambda _=False, path=project["path"]: self.pathRequested.emit(path))
            self.list_layout.insertWidget(self.list_layout.count() - 1, row)
        if visible:
            self.empty_label.hide()
        else:
            self.empty_label.show()
            self.empty_label.setText(
                "没有匹配的项目。换一个关键词，或新建、打开一个项目。"
                if self._projects
                else "还没有最近项目。新建或打开一个项目后，它会出现在这里。"
            )
        self._update_entries()


class FolioHome(_HomeView):
    """Two-page work book: cover entries on the left, project register right."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.new_button.setObjectName("entryLarge")
        self.open_button.setObjectName("entryWide")
        self.continue_button.setObjectName("entryWide")
        self.resume_button.setObjectName("quiet")
        self.import_button.setObjectName("quiet")
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        cover = QWidget()
        cover.setObjectName("folioCover")
        cover_layout = _scrollable(cover, (34, 28, 34, 24), 11)
        head = QHBoxLayout()
        head.addWidget(_label("项目工作册", "overLine"))
        head.addStretch(1)
        head.addWidget(_label("TraceAtlas / 本地", "overLine"))
        cover_layout.addLayout(head)
        rule = QFrame()
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setObjectName("folioRule")
        cover_layout.addWidget(rule)
        cover_layout.addSpacing(12)
        cover_layout.addWidget(_label("每一份工作，\n都有据可循。", "heroTitle"))
        cover_layout.addWidget(
            _label("把流水、原始来源和核查结果，收在一份可以继续的项目里。", "muted", wrap=True)
        )
        cover_layout.addSpacing(14)
        for widget in (
            self.new_button,
            self.open_button,
            self.continue_button,
            self.import_button,
            self.resume_button,
        ):
            cover_layout.addWidget(widget)
        cover_layout.addStretch(1)
        register = QGridLayout()
        register.setHorizontalSpacing(12)
        register.setVerticalSpacing(5)
        self.register_values: dict[str, QLabel] = {}
        rows = [
            ("当前项目", "尚未打开"),
            ("工作方式", "本地项目"),
            ("资料与结果", "随项目保存"),
            ("当前布局", "文件册"),
            ("当前主题", "默认"),
        ]
        for row_index, (key, value) in enumerate(rows):
            register.addWidget(_label(key, "overLine"), row_index, 0)
            value_label = _label(value, "muted")
            value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            register.addWidget(value_label, row_index, 1)
            self.register_values[key] = value_label
        cover_layout.addLayout(register)
        root.addWidget(cover, 5)

        index_page = QWidget()
        index_page.setObjectName("folioIndex")
        index_layout = QVBoxLayout(index_page)
        index_layout.setContentsMargins(30, 28, 30, 22)
        index_layout.setSpacing(9)
        title_row = QHBoxLayout()
        title_row.addWidget(_label("最近的工作", "pageTitle"))
        title_row.addStretch(1)
        self.count_label = _label("", "overLine")
        title_row.addWidget(self.count_label)
        index_layout.addLayout(title_row)
        index_layout.addWidget(self.search)
        index_layout.addWidget(self.list_host, 1)
        index_layout.addWidget(self.empty_label)
        footer = QHBoxLayout()
        footer.addWidget(_label("本地工作册 · 资料与结果随项目保存", "overLine"))
        footer.addStretch(1)
        footer.addWidget(_label("带序号的为真实最近项目", "overLine"))
        index_layout.addLayout(footer)
        root.addWidget(index_page, 6)

    def set_projects(self, projects: list[dict]) -> None:
        self.count_label.setText(f"{len(projects)} 个最近项目" if projects else "尚无项目")
        super().set_projects(projects)

    def _render_context(self) -> None:
        self.register_values["当前项目"].setText(self._current_name or "尚未打开")
        self.register_values["工作方式"].setText("当前项目已打开" if self._has_project else "本地项目")

    def _render_theme(self) -> None:
        self.register_values["当前布局"].setText(LAYOUT_LABELS[self._layout])
        self.register_values["当前主题"].setText(self._theme_label)


class TerminalHome(_HomeView):
    """Instrument-console launch page: session control left, local facts right."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.new_button.setObjectName("entryNew")
        self.open_button.setObjectName("segment")
        self.continue_button.setObjectName("quiet")
        self.resume_button.setObjectName("quiet")
        self.import_button.setObjectName("quiet")
        self.new_button.setText("＋ 新建项目")
        self.open_button.setText("打开项目")
        self.continue_button.setText("继续上次会话 →")
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        launch = QWidget()
        launch.setObjectName("terminalPanel")
        layout = _scrollable(launch, (28, 24, 28, 20), 10)
        title_row = QHBoxLayout()
        title_column = QVBoxLayout()
        title_column.setSpacing(2)
        title_column.addWidget(_label("TRACEATLAS", "pageTitle"))
        title_column.addWidget(_label("DATA INVESTIGATION TERMINAL · 本地会话", "overLine"))
        title_row.addLayout(title_column)
        title_row.addStretch(1)
        title_row.addWidget(_label("SESSION / 001", "nodeChip"))
        layout.addLayout(title_row)
        rule = QFrame()
        rule.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(rule)
        metrics = QHBoxLayout()
        metrics.setSpacing(28)
        self.metric_projects = self._metric(metrics, "最近项目", "00")
        self.metric_theme = self._metric(metrics, "当前主题", "默认")
        self.metric_layout = self._metric(metrics, "当前布局", "金融终端")
        metrics.addStretch(1)
        layout.addLayout(metrics)
        layout.addSpacing(4)
        layout.addWidget(_label("调查会话控制台", "sectionTitle"))
        layout.addWidget(_label("选择工作项目，恢复上次的检查位置。", "muted"))
        prompt_row = QHBoxLayout()
        prompt_row.setSpacing(8)
        prompt_row.addWidget(_label("atlas >", "entryKey"))
        prompt_row.addWidget(self.search, 1)
        prompt_row.addWidget(_label("Ctrl K", "overLine"))
        layout.addLayout(prompt_row)
        session_head = QHBoxLayout()
        session_head.addWidget(_label("最近会话", "overLine"))
        session_head.addStretch(1)
        self.count_label = _label("", "overLine")
        session_head.addWidget(self.count_label)
        layout.addLayout(session_head)
        layout.addWidget(self.list_host)
        layout.addWidget(self.empty_label)
        layout.addStretch(1)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        for widget in (self.new_button, self.open_button, self.continue_button):
            actions.addWidget(widget)
        actions.addStretch(1)
        self.resume_button.setText("返回当前会话 →")
        actions.addWidget(self.resume_button)
        actions.addWidget(self.import_button)
        layout.addLayout(actions)
        root.addWidget(launch, 1)

        aside = QWidget()
        aside.setObjectName("terminalAside")
        aside_layout = _scrollable(aside, (20, 24, 20, 18), 9)
        aside_layout.addWidget(_label("会话信息", "overLine"))
        self.aside_fields: dict[str, QLabel] = {}
        for key, value in [
            ("当前项目", "尚未打开"),
            ("本地工作空间", "未被占用的项目目录"),
            ("当前布局", "金融终端"),
            ("当前主题", "默认"),
            ("界面动效", "运行中"),
        ]:
            block = QVBoxLayout()
            block.setSpacing(1)
            block.addWidget(_label(key, "overLine"))
            value_label = _label(value, "muted", wrap=True)
            block.addWidget(value_label)
            aside_layout.addLayout(block)
            self.aside_fields[key] = value_label
        aside_layout.addStretch(1)
        aside_layout.addWidget(_label("快捷键", "overLine"))
        aside_layout.addWidget(_label("Ctrl+N 新建 · Ctrl+O 打开 · Ctrl+, 设置", "muted", wrap=True))
        aside_layout.addWidget(
            _label("资料保存在本机项目文件夹，界面不连接在线服务。", "muted", wrap=True)
        )
        aside.setFixedWidth(240)
        root.addWidget(aside, 0)

    def _metric(self, layout: QHBoxLayout, label: str, value: str) -> QLabel:
        block = QVBoxLayout()
        block.setSpacing(0)
        block.addWidget(_label(label, "metricLabel"))
        value_label = _label(value, "metricValue")
        block.addWidget(value_label)
        layout.addLayout(block)
        return value_label

    def set_projects(self, projects: list[dict]) -> None:
        self.count_label.setText(f"{len(projects)} projects")
        self.metric_projects.setText(f"{len(projects):02d}")
        super().set_projects(projects)

    def _render_context(self) -> None:
        self.aside_fields["当前项目"].setText(self._current_name or "尚未打开")
        self.aside_fields["本地工作空间"].setText(
            "当前项目已打开" if self._has_project else "未打开项目，本地可用"
        )

    def _render_theme(self) -> None:
        self.aside_fields["当前布局"].setText(LAYOUT_LABELS[self._layout])
        self.aside_fields["当前主题"].setText(self._theme_label)
        self.metric_layout.setText(LAYOUT_LABELS[self._layout])
        self.metric_theme.setText(self._theme_label)

    def _render_motion(self) -> None:
        self.aside_fields["界面动效"].setText("运行中" if self._motion else "已暂停")


class NetworkHome(_HomeView):
    """Spatial intro with real entries; the node canvas carries no behaviour."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.new_button.setObjectName("entryLarge")
        self.open_button.setObjectName("entryWide")
        self.continue_button.setObjectName("entryWide")
        self.resume_button.setObjectName("quiet")
        self.import_button.setObjectName("quiet")
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        intro = QWidget()
        intro.setObjectName("networkIntro")
        layout = _scrollable(intro, (24, 24, 24, 18), 9)
        layout.addWidget(_label("资料相连，线索可循", "overLine"))
        layout.addWidget(_label("让每一条线索，\n找到它的来处。", "heroTitle"))
        layout.addWidget(_label("项目、资料与核查结果，在同一片工作空间展开。", "muted", wrap=True))
        layout.addSpacing(8)
        for widget in (
            self.new_button,
            self.open_button,
            self.continue_button,
            self.import_button,
            self.resume_button,
        ):
            layout.addWidget(widget)
        self.last_label = _label("", "overLine", wrap=True)
        layout.addWidget(self.last_label)
        layout.addStretch(1)
        stats = QHBoxLayout()
        stats.setSpacing(26)
        self.stat_projects = _label("00", "metricValue")
        self.stat_storage = _label("本机", "metricValue")
        for value, caption in ((self.stat_projects, "最近项目"), (self.stat_storage, "本地空间")):
            block = QVBoxLayout()
            block.setSpacing(0)
            block.addWidget(value)
            block.addWidget(_label(caption, "metricLabel"))
            stats.addLayout(block)
        stats.addStretch(1)
        layout.addLayout(stats)
        layout.addWidget(
            _label(
                "节点与连线仅为视觉样式，不能点击进入项目；请使用上面的入口开始工作。",
                "overLine",
                wrap=True,
            )
        )
        intro.setFixedWidth(296)
        root.addWidget(intro, 0)

        canvas_host = QWidget()
        canvas_host.setObjectName("networkCanvasHost")
        canvas_layout = QVBoxLayout(canvas_host)
        canvas_layout.setContentsMargins(14, 14, 14, 8)
        canvas_layout.setSpacing(4)
        self.canvas = NetworkCanvas(density=7)
        canvas_layout.addWidget(self.canvas, 1)
        footer = QHBoxLayout()
        footer.addWidget(_label("WORKSPACE / LOCAL", "overLine"))
        footer.addStretch(1)
        footer.addWidget(_label("连线表示视觉归属，不代表资金关系", "overLine"))
        canvas_layout.addLayout(footer)
        self.current_label = _label("", "overLine")
        canvas_layout.addWidget(self.current_label)
        root.addWidget(canvas_host, 1)

    def set_projects(self, projects: list[dict]) -> None:
        self.stat_projects.setText(f"{len(projects):02d}")
        self.last_label.setText(f"上次项目：{projects[0]['name']}" if projects else "还没有最近项目")
        super().set_projects(projects)

    def _render_context(self) -> None:
        self.current_label.setText(
            f"当前项目：{self._current_name}" if self._current_name else "当前未打开项目 · 使用左侧入口开始"
        )

    def _render_theme(self) -> None:
        self.canvas.set_tokens(self._scheme)

    def _render_motion(self) -> None:
        self.canvas.set_motion(self._motion)


class HomePage(QWidget):
    """Header plus the active layout's start page."""

    newProjectRequested = Signal()
    openProjectRequested = Signal()
    continueProjectRequested = Signal()
    projectRequested = Signal(str)
    importRequested = Signal()
    resumeRequested = Signal()
    settingsRequested = Signal()
    layoutRequested = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("homeRoot")
        self._layout = "folio"
        self._scheme: Tokens = tokens("folio", "light")
        self._theme_label = "默认"
        self._motion = True
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        header = QWidget()
        header.setObjectName("homeHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(20, 9, 20, 9)
        header_layout.setSpacing(8)
        brand = _label("开始", "sectionTitle")
        header_layout.addWidget(brand)
        header_layout.addWidget(_label("新建、打开或继续一个项目", "overLine"))
        header_layout.addStretch(1)
        header_layout.addWidget(_label("工作界面", "overLine"))
        self.layout_buttons: dict[str, QPushButton] = {}
        for layout in LAYOUTS:
            button = _button(LAYOUT_LABELS[layout], "segment")
            button.setCheckable(True)
            button.setToolTip(LAYOUT_LABELS[layout])
            button.clicked.connect(lambda _=False, value=layout: self.layoutRequested.emit(value))
            header_layout.addWidget(button)
            self.layout_buttons[layout] = button
        self.settings_button = _button("设置…", "segment")
        self.settings_button.setToolTip("布局、主题与动效（Ctrl+,）")
        self.settings_button.clicked.connect(self.settingsRequested.emit)
        header_layout.addWidget(self.settings_button)
        root.addWidget(header)

        self.stack = QStackedWidget()
        self.stack.setObjectName("homeStack")
        self.views: dict[str, _HomeView] = {
            "folio": FolioHome(),
            "terminal": TerminalHome(),
            "network": NetworkHome(),
        }
        for view in self.views.values():
            view.newRequested.connect(self.newProjectRequested.emit)
            view.openRequested.connect(self.openProjectRequested.emit)
            view.continueRequested.connect(self.continueProjectRequested.emit)
            view.pathRequested.connect(self.projectRequested.emit)
            view.importRequested.connect(self.importRequested.emit)
            view.resumeRequested.connect(self.resumeRequested.emit)
            self.stack.addWidget(view)
        root.addWidget(self.stack, 1)
        self.notice = QLabel("")
        self.notice.setObjectName("notice")
        self.notice.setWordWrap(True)
        self.notice.hide()
        root.addWidget(self.notice)
        self.active_view: _HomeView = self.views["folio"]
        self.set_layout("folio")

    # -- public API ----------------------------------------------------
    @property
    def layout(self) -> str:
        return self._layout

    def set_layout(self, layout: str) -> None:
        self._layout = layout if layout in self.views else "folio"
        self.active_view = self.views[self._layout]
        self.stack.setCurrentWidget(self.active_view)
        for key, button in self.layout_buttons.items():
            button.setChecked(key == self._layout)
        self.apply_theme(self._layout, self._scheme, self._theme_label)

    def apply_theme(self, layout: str, scheme: Tokens, theme_label: str) -> None:
        self._scheme = scheme
        self._theme_label = theme_label
        for view in self.views.values():
            view.apply_theme(layout, scheme, theme_label)

    def set_motion(self, enabled: bool) -> None:
        self._motion = enabled
        for view in self.views.values():
            view.set_motion(enabled)

    def set_notice(self, text: str) -> None:
        self.notice.setText(text)
        self.notice.setVisible(bool(text))

    def set_projects(self, projects: list[dict]) -> None:
        for view in self.views.values():
            view.set_projects(projects)

    def set_context(self, *, has_project: bool, writable: bool, current_name: str = "") -> None:
        for view in self.views.values():
            view.set_context(has_project=has_project, writable=writable, current_name=current_name)

    def focus_search(self) -> None:
        self.active_view.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.active_view.search.selectAll()

    def entries(self) -> tuple[QPushButton, QPushButton, QPushButton, QPushButton]:
        """New / open / continue / import buttons of the active layout."""
        view = self.active_view
        return view.new_button, view.open_button, view.continue_button, view.import_button
