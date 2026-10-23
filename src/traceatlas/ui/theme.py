"""Layout, theme and motion preferences rendered as one Qt stylesheet.

Three layouts (folio / terminal / network) change composition and typography;
three themes (default follows Windows, light, dark) change colour only.
Business widgets never read these values directly: they use semantic object
names such as #pageTitle or #entryNew and the stylesheet supplies the colour
for the active layout and theme.
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import asdict, dataclass
from string import Template

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

LAYOUTS = ("folio", "terminal", "network")
LAYOUT_LABELS = {"folio": "文件册", "terminal": "金融终端", "network": "关联画布"}
LAYOUT_HINTS = {
    "folio": "双页文件册：项目目录、工作页与来源页并排，像一份展开的档案。",
    "terminal": "金融终端：仪表区、紧凑数据行与常驻来源检视，数字使用等宽字体。",
    "network": "关联画布：空间网格与连接线作背景，节点与连线只作视觉样式。",
}
THEMES = ("system", "light", "dark")
THEME_LABELS = {"system": "默认（跟随系统）", "light": "亮", "dark": "暗"}
DEFAULT_LAYOUT = "folio"
DEFAULT_THEME = "system"

SERIF = "'Georgia', 'Songti SC', 'SimSun', serif"
SANS = "'Segoe UI', 'Microsoft YaHei UI', sans-serif"
MONO = "'Consolas', 'Cascadia Mono', 'Microsoft YaHei UI', monospace"


@dataclass(frozen=True)
class Tokens:
    """Semantic colours for one layout in one colour mode."""

    layout: str
    mode: str
    bg: str
    surface: str
    raised: str
    ink: str
    muted: str
    line: str
    accent: str
    accent_soft: str
    on_accent: str
    emphasis: str
    warning: str
    danger: str
    success: str
    alt: str
    grid: str
    serif: str
    sans: str
    mono: str
    radius: str
    card_radius: str
    density: str

    @property
    def dark(self) -> bool:
        return self.mode == "dark"

    def as_dict(self) -> dict[str, str]:
        return {key: str(value) for key, value in asdict(self).items()}


# Colours follow the approved HTML studies: folio keeps the 17-cover paper tone,
# terminal is a cold instrument panel, network is a cool spatial surface.
_COLOURS: dict[tuple[str, str], dict[str, str]] = {
    ("folio", "light"): {
        "bg": "#eeede8", "surface": "#fbfaf6", "raised": "#f1f0e9", "ink": "#30372f",
        "muted": "#606659", "line": "#d4d7cd", "accent": "#45633f", "accent_soft": "#e4eade",
        "on_accent": "#ffffff", "emphasis": "#7a4a1f", "warning": "#825115", "danger": "#9c3b2c",
        "success": "#3d6b4a", "alt": "#f7f6f0", "grid": "#e2e4da",
    },
    ("terminal", "light"): {
        "bg": "#eff1f0", "surface": "#fafcfb", "raised": "#e8edea", "ink": "#17251f",
        "muted": "#51635a", "line": "#cbd5cf", "accent": "#296b4e", "accent_soft": "#dcebe2",
        "on_accent": "#ffffff", "emphasis": "#a2620f", "warning": "#825115", "danger": "#9c3b2c",
        "success": "#2f6d52", "alt": "#f4f7f5", "grid": "#dfe6e1",
    },
    ("network", "light"): {
        "bg": "#eef1f3", "surface": "#ffffff", "raised": "#f1f4f6", "ink": "#23313b",
        "muted": "#596b77", "line": "#d5dde2", "accent": "#286b91", "accent_soft": "#e1eef6",
        "on_accent": "#ffffff", "emphasis": "#1d5c7d", "warning": "#825115", "danger": "#9c3b2c",
        "success": "#2f6d52", "alt": "#f6f9fb", "grid": "#dde6ec",
    },
    ("folio", "dark"): {
        "bg": "#1a1d19", "surface": "#22261f", "raised": "#2b3027", "ink": "#e3e9dc",
        "muted": "#abb6a2", "line": "#414a3a", "accent": "#bdd0a6", "accent_soft": "#343f2c",
        "on_accent": "#1a2316", "emphasis": "#e8c07a", "warning": "#eac38b", "danger": "#e2897c",
        "success": "#a9c9a5", "alt": "#262b23", "grid": "#333a2f",
    },
    ("terminal", "dark"): {
        "bg": "#111514", "surface": "#171d1a", "raised": "#202924", "ink": "#e1eae4",
        "muted": "#9daf9f", "line": "#344139", "accent": "#b6d5a3", "accent_soft": "#2a382c",
        "on_accent": "#152013", "emphasis": "#e9a94b", "warning": "#eac38b", "danger": "#e2897c",
        "success": "#9ecf9b", "alt": "#1c2320", "grid": "#28332c",
    },
    ("network", "dark"): {
        "bg": "#171b1e", "surface": "#20262b", "raised": "#29323a", "ink": "#e4ecf2",
        "muted": "#a4b5c2", "line": "#3e4c57", "accent": "#9ecce7", "accent_soft": "#2b4050",
        "on_accent": "#142733", "emphasis": "#a9d7ef", "warning": "#eac38b", "danger": "#e2897c",
        "success": "#9ecfb4", "alt": "#232a30", "grid": "#2c363d",
    },
}

_TYPE: dict[str, dict[str, str]] = {
    "folio": {"serif": SERIF, "sans": SANS, "mono": MONO, "radius": "2px",
              "card_radius": "0px", "density": "roomy"},
    "terminal": {"serif": MONO, "sans": MONO, "mono": MONO, "radius": "2px",
                 "card_radius": "2px", "density": "compact"},
    "network": {"serif": SANS, "sans": SANS, "mono": MONO, "radius": "8px",
                "card_radius": "10px", "density": "normal"},
}


def mono_family(scheme: Tokens) -> str:
    """First concrete family of the layout's monospace stack."""
    first = scheme.mono.split(",")[0].strip().strip("'\"")
    return first or "Consolas"


def tokens(layout: str, mode: str) -> Tokens:
    layout = layout if layout in LAYOUTS else DEFAULT_LAYOUT
    mode = "dark" if mode == "dark" else "light"
    return Tokens(layout=layout, mode=mode, **_COLOURS[(layout, mode)], **_TYPE[layout])


def normalize_layout(value) -> str:
    return value if value in LAYOUTS else DEFAULT_LAYOUT


def normalize_theme(value) -> str:
    return value if value in THEMES else DEFAULT_THEME


def system_colour_scheme() -> str:
    """Windows light/dark preference as reported by Qt itself."""
    app = QApplication.instance()
    hints = app.styleHints() if app else None
    scheme = getattr(hints, "colorScheme", None)
    try:
        value = scheme() if callable(scheme) else None
    except (AttributeError, RuntimeError):
        value = None
    return "dark" if value is not None and str(value).endswith("Dark") else "light"


def resolve_mode(theme: str) -> str:
    theme = normalize_theme(theme)
    if theme == "system":
        return system_colour_scheme()
    return theme


def system_reduces_motion() -> bool:
    """SPI_GETCLIENTAREAANIMATION: the Windows 'show animations' switch."""
    if sys.platform != "win32":
        return False
    try:
        enabled = ctypes.c_int(1)
        ok = ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0)
    except (AttributeError, OSError):
        return False
    return bool(ok) and not enabled.value


def motion_enabled(paused_by_user: bool) -> bool:
    return not paused_by_user and not system_reduces_motion()


def app_palette(scheme: Tokens) -> QPalette:
    """Native pieces (scrollbars, menus, tooltips) need a matching QPalette."""
    palette = QPalette()

    def colour(value: str) -> QColor:
        return QColor(value)

    roles = {
        QPalette.ColorRole.Window: scheme.bg,
        QPalette.ColorRole.WindowText: scheme.ink,
        QPalette.ColorRole.Base: scheme.surface,
        QPalette.ColorRole.AlternateBase: scheme.alt,
        QPalette.ColorRole.Text: scheme.ink,
        QPalette.ColorRole.Button: scheme.raised,
        QPalette.ColorRole.ButtonText: scheme.ink,
        QPalette.ColorRole.BrightText: scheme.danger,
        QPalette.ColorRole.Highlight: scheme.accent_soft,
        QPalette.ColorRole.HighlightedText: scheme.ink,
        QPalette.ColorRole.ToolTipBase: scheme.raised,
        QPalette.ColorRole.ToolTipText: scheme.ink,
        QPalette.ColorRole.PlaceholderText: scheme.muted,
        QPalette.ColorRole.Link: scheme.accent,
        QPalette.ColorRole.Mid: scheme.line,
        QPalette.ColorRole.Dark: scheme.line,
        QPalette.ColorRole.Shadow: scheme.line,
    }
    for role, value in roles.items():
        palette.setColor(role, colour(value))
    disabled = QPalette.ColorGroup.Disabled
    palette.setColor(disabled, QPalette.ColorRole.WindowText, colour(scheme.muted))
    palette.setColor(disabled, QPalette.ColorRole.Text, colour(scheme.muted))
    palette.setColor(disabled, QPalette.ColorRole.ButtonText, colour(scheme.muted))
    palette.setColor(disabled, QPalette.ColorRole.Highlight, colour(scheme.raised))
    palette.setColor(disabled, QPalette.ColorRole.HighlightedText, colour(scheme.muted))
    return palette


_QSS = Template("""
* { outline: 0; }
QWidget {
    color: $ink;
    font-family: $sans;
    font-size: 10pt;
    selection-background-color: $accent_soft;
    selection-color: $ink;
}
QMainWindow, QDialog, QMessageBox { background: $bg; }
QWidget#homeRoot, QWidget#workspaceRoot, QWidget#inspectorRoot { background: $bg; }
QWidget#pageSurface, QWidget#card, QFrame#card, QWidget#folioPage, QWidget#terminalPanel,
QWidget#networkPanel, QWidget#instrumentPanel {
    background: $surface;
    border: 1px solid $line;
    border-radius: $card_radius;
}
QLabel { background: transparent; }
QWidget#homeHeader { background: $surface; border-bottom: 1px solid $line; }
QWidget#homeStack { background: $bg; }
QWidget#folioCover { background: $surface; border: 1px solid $line; border-right: 0; }
QWidget#folioIndex { background: $surface; border: 1px solid $line; }
QFrame#folioRule { border: 0; border-top: 1px solid $line; max-height: 1px; }
QWidget#terminalPanel { background: $surface; border: 1px solid $line; border-right: 0; }
QWidget#terminalAside { background: $raised; border: 1px solid $line; border-left: 0; }
QWidget#networkIntro { background: $surface; border: 1px solid $line; border-right: 0; }
QWidget#networkCanvasHost { background: $surface; border: 1px solid $line; }
QScrollArea { border: 0; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollArea > QWidget#qt_scrollarea_viewport { background: transparent; }
QLineEdit#searchLine {
    background: $surface; border: 1px solid $line; border-radius: $radius;
    padding: 6px 9px; color: $ink;
}
QLineEdit#searchLine:focus { border: 2px solid $accent; padding: 5px 8px; }
QWidget#instrumentPanel QLabel#metricValue { font-size: 13pt; }
QWidget#inspectorRoot QPushButton#quiet { text-align: left; }
QLabel#pageTitle { font-family: $serif; font-size: 19pt; font-weight: 600; color: $ink; }
QLabel#heroTitle { font-family: $serif; font-size: 22pt; font-weight: 500; color: $ink; }
QLabel#sectionTitle { font-size: 11pt; font-weight: 600; color: $ink; }
QLabel#overLine, QLabel#eyeline { color: $muted; font-size: 9pt; }
QLabel#muted, QLabel#hint { color: $muted; }
QLabel#notice { background: $accent_soft; border: 1px solid $line; border-left: 3px solid $warning;
    padding: 8px 10px; color: $ink; }
QLabel#warningNote { color: $warning; }
QLabel#dangerNote { color: $danger; }
QLabel#successNote { color: $success; }
QLabel#metricValue { font-family: $mono; font-size: 16pt; color: $emphasis; }
QLabel#metricLabel { color: $muted; font-size: 9pt; }
QLabel#amountLarge { font-family: $mono; font-size: 19pt; color: $emphasis; }
QLabel#nodeChip { border: 1px solid $line; border-radius: 10px; padding: 2px 9px; color: $muted;
    background: $raised; font-size: 9pt; }
QLabel#statusDot { color: $accent; }

QMenuBar { background: $surface; color: $ink; border-bottom: 1px solid $line; padding: 2px 4px; }
QMenuBar::item { background: transparent; padding: 5px 10px; border-radius: 2px; }
QMenuBar::item:selected { background: $accent_soft; color: $ink; }
QMenuBar::item:pressed { background: $accent_soft; }
QMenu { background: $surface; border: 1px solid $line; padding: 4px; color: $ink; }
QMenu::item { padding: 6px 24px 6px 20px; border-radius: 2px; }
QMenu::item:selected { background: $accent_soft; color: $ink; }
QMenu::item:disabled { color: $muted; }
QMenu::separator { height: 1px; background: $line; margin: 5px 8px; }

QToolBar { background: $surface; border: 0; border-bottom: 1px solid $line; padding: 6px 10px; spacing: 6px; }
QToolBar QToolButton, QToolButton {
    background: transparent; border: 1px solid transparent; border-radius: $radius;
    padding: 5px 10px; color: $ink;
}
QToolButton:hover { background: $accent_soft; border-color: $line; }
QToolButton:pressed { background: $accent_soft; }
QToolButton:checked { background: $accent_soft; border-color: $accent; }
QToolButton:focus { border-color: $accent; }
QToolButton:disabled { color: $muted; background: transparent; border-color: transparent; }

QStatusBar { background: $surface; border-top: 1px solid $line; color: $muted; }
QStatusBar QLabel { color: $muted; padding: 0 4px; }
QStatusBar::item { border: 0; }

QPushButton {
    background: $surface; border: 1px solid $line; border-radius: $radius;
    padding: 6px 13px; color: $ink; min-height: 20px;
}
QPushButton:hover { background: $raised; border-color: $accent; }
QPushButton:pressed { background: $accent_soft; }
QPushButton:focus { border: 2px solid $accent; padding: 5px 12px; }
QPushButton:default { border-color: $accent; }
QPushButton:disabled { color: $muted; background: $raised; border-color: $line; }
QPushButton#primary, QPushButton#entryNew {
    background: $accent; color: $on_accent; border-color: $accent; font-weight: 600;
}
QPushButton#primary:hover, QPushButton#entryNew:hover { background: $accent; border-color: $ink; }
QPushButton#primary:disabled, QPushButton#entryNew:disabled { background: $raised; color: $muted; border-color: $line; }
QPushButton#entryLarge {
    background: $accent; color: $on_accent; border: 1px solid $accent; border-radius: $card_radius;
    text-align: left; padding: 12px 16px; min-height: 26px; font-size: 11pt; font-weight: 600;
}
QPushButton#entryLarge:hover { border-color: $ink; }
QPushButton#entryLarge:disabled { background: $raised; color: $muted; }
QPushButton#entryWide {
    text-align: left; padding: 12px 16px; border-radius: $card_radius; background: $surface; font-size: 10.5pt;
}
QPushButton#entryWide:hover { background: $raised; }
QPushButton#entryWide:disabled { color: $muted; background: $raised; }
QPushButton#segment {
    background: transparent; border: 1px solid $line; border-radius: $radius; padding: 4px 10px; color: $muted;
}
QPushButton#segment:hover { background: $raised; color: $ink; }
QPushButton#segment:checked { background: $accent_soft; border-color: $accent; color: $ink; font-weight: 600; }
QPushButton#recentRow {
    text-align: left; background: transparent; border: 0; border-bottom: 1px solid $line;
    border-radius: 0; padding: 10px 6px; min-height: 34px;
}
QPushButton#recentRow:hover { background: $raised; }
QPushButton#recentRow:focus { border: 1px solid $accent; }
QPushButton#recentRow QLabel { background: transparent; }
QPushButton#quiet { border-color: transparent; background: transparent; color: $accent; }
QPushButton#quiet:hover { background: $raised; }

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: $surface; border: 1px solid $line; border-radius: $radius;
    padding: 5px 7px; min-height: 20px; color: $ink;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border: 2px solid $accent; padding: 4px 6px; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { color: $muted; background: $raised; }
QComboBox::drop-down { border: 0; width: 18px; }
QComboBox QAbstractItemView {
    background: $surface; border: 1px solid $line; color: $ink; selection-background-color: $accent_soft;
    selection-color: $ink; outline: 0;
}
QLineEdit#promptLine {
    border: 0; border-bottom: 1px solid $accent; border-radius: 0; background: transparent;
    font-family: $mono; font-size: 12pt; padding: 6px 2px;
}
QLineEdit#promptLine:focus { border-bottom: 2px solid $accent; padding-bottom: 5px; }

QTableView, QTreeView, QTreeWidget, QListWidget, QListView {
    background: $surface; alternate-background-color: $alt; border: 1px solid $line;
    gridline-color: $line; selection-background-color: $accent_soft; selection-color: $ink;
}
QTableView::item, QTreeWidget::item, QListView::item { padding: 3px 4px; border: 0; }
QTableView::item:selected, QTreeWidget::item:selected, QListView::item:selected {
    background: $accent_soft; color: $ink;
}
QTableView::item:focus { border: 1px solid $accent; }
QTableView:focus, QTreeWidget:focus { border-color: $accent; }
QTableView:disabled, QTreeWidget:disabled { color: $muted; background: $raised; }
QHeaderView::section {
    background: $raised; color: $muted; padding: 6px 7px; border: 0;
    border-right: 1px solid $line; border-bottom: 1px solid $line;
}
QTableCornerButton::section { background: $raised; border: 0; }
QTreeWidget::branch { background: transparent; }

QTabWidget::pane { border: 1px solid $line; background: $surface; border-radius: $card_radius; top: -1px; }
QTabBar { qproperty-drawBase: 0; background: transparent; }
QTabBar::tab {
    background: transparent; color: $muted; border: 0; border-bottom: 2px solid transparent;
    padding: 8px 14px; margin-right: 2px;
}
QTabBar::tab:hover { color: $ink; }
QTabBar::tab:selected { color: $ink; border-bottom: 2px solid $accent; font-weight: 600; }
QTabBar#boxedTab::tab {
    background: $raised; border: 1px solid $line; border-bottom: 0; border-top-left-radius: $radius;
    border-top-right-radius: $radius; padding: 7px 14px;
}
QTabBar#boxedTab::tab:selected { background: $surface; border-color: $accent; }

QDockWidget { color: $ink; }
QDockWidget::title { background: $raised; padding: 7px 10px; border-top: 1px solid $line; color: $muted; }
QDockWidget > QWidget { background: $surface; }
QGroupBox { border: 1px solid $line; border-radius: $card_radius; margin-top: 9px; padding: 10px 8px 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; color: $muted; }
QCheckBox { color: $ink; spacing: 7px; }
QCheckBox:disabled { color: $muted; }
QCheckBox:focus { color: $ink; }
QRadioButton { color: $ink; spacing: 7px; }
QRadioButton:disabled { color: $muted; }

QSplitter::handle { background: $line; }
QSplitter::handle:horizontal { width: 1px; }
QSplitter::handle:vertical { height: 1px; }
QSplitter::handle:hover { background: $accent; }

QProgressBar { border: 1px solid $line; border-radius: 2px; background: $raised; text-align: center; color: $ink; }
QProgressBar::chunk { background: $accent; }

QScrollBar:vertical { background: transparent; width: 11px; margin: 0; }
QScrollBar::handle:vertical { background: $line; min-height: 26px; border-radius: 5px; }
QScrollBar::handle:vertical:hover { background: $accent; }
QScrollBar:horizontal { background: transparent; height: 11px; margin: 0; }
QScrollBar::handle:horizontal { background: $line; min-width: 26px; border-radius: 5px; }
QScrollBar::handle:horizontal:hover { background: $accent; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

QToolTip { background: $raised; color: $ink; border: 1px solid $line; padding: 5px; }

QWidget#navRoot { background: $surface; border-right: 1px solid $line; }
QWidget#navRoot QLabel#pageTitle { font-size: 15pt; }
QTreeWidget#navigation { background: transparent; border: 0; }
QTreeWidget#navigation::item { padding: 6px 4px; border-radius: $radius; }
QTreeWidget#navigation::item:selected { background: $accent_soft; color: $ink; }
QWidget#inspectorRoot { background: $surface; border-left: 1px solid $line; }
QWidget#inspectorRoot QLabel#sectionTitle { font-size: 10.5pt; }
QWidget#instrumentPanel { border-radius: 0; border-left: 0; border-right: 0; border-top: 0; }
QLabel#logLine { font-family: $mono; color: $muted; font-size: 9pt; }
QWidget#taskDockBody { background: $surface; }
QLabel#entryKey { color: $muted; font-family: $mono; font-size: 9pt; }
""")


def stylesheet(layout: str, mode: str) -> str:
    scheme = tokens(layout, mode)
    return _QSS.safe_substitute(scheme.as_dict())


def apply_appearance(layout: str, mode: str, app: QApplication | None = None) -> Tokens:
    """Apply one layout + mode to the whole application (windows, dialogs, menus)."""
    application = app or QApplication.instance()
    scheme = tokens(layout, mode)
    if application is not None:
        application.setPalette(app_palette(scheme))
        application.setStyleSheet(stylesheet(layout, mode))
    return scheme
