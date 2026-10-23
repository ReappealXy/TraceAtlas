"""Settings dialog: layout, theme and motion, applied live and persisted."""

from __future__ import annotations

from PySide6.QtCore import QSettings, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from .theme import (
    LAYOUT_HINTS,
    LAYOUT_LABELS,
    LAYOUTS,
    THEME_LABELS,
    THEMES,
    normalize_layout,
    normalize_theme,
    resolve_mode,
    system_reduces_motion,
)


class SettingsDialog(QDialog):
    layoutChanged = Signal(str)
    themeChanged = Signal(str)
    motionChanged = Signal(bool)

    def __init__(self, layout: str, theme: str, motion_paused: bool, settings: QSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置与外观")
        self.resize(540, 520)
        self.settings = settings
        self._layout = normalize_layout(layout)
        self._theme = normalize_theme(theme)
        self._motion = bool(motion_paused)
        layout_box = QVBoxLayout(self)
        layout_box.setContentsMargins(20, 18, 20, 16)
        layout_box.setSpacing(10)
        heading = QLabel("工作界面与主题")
        heading.setObjectName("pageTitle")
        layout_box.addWidget(heading)
        note = QLabel(
            "布局改变界面组织方式，主题只改变颜色；两者都不改变数据、任务或结果。"
            "选择会立即应用，并保存到下一次启动。"
        )
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout_box.addWidget(note)

        layout_group = self._group(layout_box, "工作界面布局")
        self.layout_buttons = QButtonGroup(self)
        self.layout_buttons.setExclusive(True)
        for value in LAYOUTS:
            button = QRadioButton(LAYOUT_LABELS[value])
            button.setChecked(value == self._layout)
            button.toggled.connect(lambda checked, key=value: self._choose_layout(key) if checked else None)
            self.layout_buttons.addButton(button)
            layout_group.addWidget(button)
            hint = QLabel(LAYOUT_HINTS[value])
            hint.setObjectName("muted")
            hint.setWordWrap(True)
            layout_group.addWidget(hint)

        theme_group = self._group(layout_box, "主题")
        self.theme_buttons = QButtonGroup(self)
        self.theme_buttons.setExclusive(True)
        for value in THEMES:
            button = QRadioButton(THEME_LABELS[value])
            button.setChecked(value == self._theme)
            button.toggled.connect(lambda checked, key=value: self._choose_theme(key) if checked else None)
            self.theme_buttons.addButton(button)
            theme_group.addWidget(button)
        self.system_note = QLabel()
        self.system_note.setObjectName("muted")
        self.system_note.setWordWrap(True)
        theme_group.addWidget(self.system_note)

        motion_group = self._group(layout_box, "界面动效")
        self.motion_box = QCheckBox("暂停界面动效")
        self.motion_box.setChecked(self._motion)
        self.motion_box.toggled.connect(self._choose_motion)
        motion_group.addWidget(self.motion_box)
        self.motion_note = QLabel()
        self.motion_note.setObjectName("muted")
        self.motion_note.setWordWrap(True)
        motion_group.addWidget(self.motion_note)

        layout_box.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close = buttons.button(QDialogButtonBox.StandardButton.Close)
        close.setText("关闭")
        close.setDefault(True)
        buttons.rejected.connect(self.accept)
        layout_box.addWidget(buttons)
        self._update_notes()

    @staticmethod
    def _group(parent_layout: QVBoxLayout, title: str) -> QVBoxLayout:
        holder = QWidget()
        holder.setObjectName("card")
        box = QVBoxLayout(holder)
        box.setContentsMargins(14, 12, 14, 12)
        box.setSpacing(5)
        caption = QLabel(title)
        caption.setObjectName("sectionTitle")
        box.addWidget(caption)
        parent_layout.addWidget(holder)
        return box

    def _choose_layout(self, value: str) -> None:
        if value == self._layout:
            return
        self._layout = value
        self.settings.setValue("ui/layout", value)
        self.layoutChanged.emit(value)

    def _choose_theme(self, value: str) -> None:
        if value == self._theme:
            return
        self._theme = value
        self.settings.setValue("ui/theme", value)
        self._update_notes()
        self.themeChanged.emit(value)

    def _choose_motion(self, paused: bool) -> None:
        self._motion = bool(paused)
        self.settings.setValue("ui/motion_paused", self._motion)
        self._update_notes()
        self.motionChanged.emit(self._motion)

    def _update_notes(self) -> None:
        mode = resolve_mode(self._theme)
        self.system_note.setText(
            "当前系统外观："
            + ("深色" if mode == "dark" else "浅色")
            + "。默认跟随系统，Windows 外观变化时界面会立即更新。"
        )
        if system_reduces_motion():
            self.motion_note.setText("系统已开启“减少动态效果”，界面动效已自动暂停；取消勾选也不会立即恢复。")
        else:
            self.motion_note.setText("关联画布的连线流动与节点光效会暂停；数据、表格与任务不受影响。")
