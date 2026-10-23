"""Reusable custom-painted widgets: recent-project rows, decorated canvases.

Everything here is presentation only. The canvases draw no business data and
accept no clicks: they exist so the folio, terminal and network layouts can be
recognised at a glance without inventing analysis results.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QRadialGradient
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .theme import Tokens, tokens


def with_alpha(colour: str, alpha: float) -> QColor:
    value = QColor(colour)
    value.setAlphaF(max(0.0, min(1.0, alpha)))
    return value


class RecentRow(QPushButton):
    """One real recent project; focusable and activatable from the keyboard."""

    def __init__(self, index: int, path: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.path = path
        self.setObjectName("recentRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(12)
        self.index_label = QLabel(f"{index:02d}")
        self.index_label.setObjectName("entryKey")
        self.index_label.setFixedWidth(24)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.name_label = QLabel()
        self.name_label.setObjectName("sectionTitle")
        self.meta_label = QLabel()
        self.meta_label.setObjectName("muted")
        self.meta_label.setWordWrap(False)
        text.addWidget(self.name_label)
        text.addWidget(self.meta_label)
        self.go_label = QLabel("→")
        self.go_label.setObjectName("muted")
        layout.addWidget(self.index_label)
        layout.addLayout(text, 1)
        layout.addWidget(self.go_label)
        for label in (self.index_label, self.name_label, self.meta_label, self.go_label):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def set_project(self, index: int, name: str, meta: str) -> None:
        self.index_label.setText(f"{index:02d}")
        self.name_label.setText(name)
        self.meta_label.setText(meta)
        self.setToolTip(f"{name}\n{self.path}")


class NetworkCanvas(QWidget):
    """Decorative spatial canvas: grid, orbits, nodes, flowing connections.

    No project data, no click handling. Animation pauses when motion is off and
    the widget stays useful as a static illustration.
    """

    def __init__(self, parent: QWidget | None = None, *, density: int = 7):
        super().__init__(parent)
        self.setObjectName("networkCanvas")
        self.setMinimumSize(320, 260)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._phase = 0.0
        self._motion = True
        self._density = density
        self._scheme: Tokens = tokens("network", "light")
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._advance)

    def set_tokens(self, scheme: Tokens) -> None:
        self._scheme = scheme
        self.update()

    def set_motion(self, enabled: bool) -> None:
        self._motion = enabled
        if enabled and self.isVisible():
            self._timer.start()
        else:
            self._timer.stop()
            self._phase = 0.0
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._motion:
            self._timer.start()

    def hideEvent(self, event) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def sizeHint(self) -> QSize:
        return QSize(560, 420)

    def _advance(self) -> None:
        self._phase = (self._phase + 0.012) % 1.0
        self.update()

    def _nodes(self):
        # Concept map only, never project data.
        return [
            (0.18, 0.22, "主体 A", "person"),
            (0.73, 0.21, "账户 A", "account"),
            (0.86, 0.52, "账户 B", "account"),
            (0.74, 0.81, "原始资料", "document"),
            (0.27, 0.79, "标准记录", "document"),
            (0.12, 0.53, "结果引用", "document"),
            (0.47, 0.10, "主体 B", "person"),
        ][:self._density]

    def paintEvent(self, event) -> None:
        scheme = self._scheme
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(scheme.surface))
        box = QRectF(self.rect()).adjusted(12, 46, -12, -38)
        compact = self.width() < 500 or self.height() < 340
        radius = 13.0 if compact else 21.0
        centre = self._point(box, 0.48, 0.51)
        glow = QRadialGradient(centre, box.width() * 0.65)
        glow.setColorAt(0, with_alpha(scheme.accent, 0.14 if scheme.dark else 0.07))
        glow.setColorAt(1, with_alpha(scheme.accent, 0))
        painter.fillRect(self.rect(), glow)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(with_alpha(scheme.grid, 0.9))
        for x in range(12, self.width(), 24):
            for y in range(42, self.height() - 26, 24):
                painter.drawEllipse(QRectF(x, y, 1.6, 1.6))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for scale in (0.72, 1.03):
            orbit = QRectF(0, 0, box.width() * scale, box.height() * scale * 0.84)
            orbit.moveCenter(centre)
            painter.setPen(QPen(with_alpha(scheme.line, 0.65), 1, Qt.PenStyle.DotLine))
            painter.drawEllipse(orbit)

        nodes = self._nodes()
        positions = [self._point(box, x, y) for x, y, _, _ in nodes]
        colours = {"person": scheme.accent, "account": scheme.success, "document": scheme.emphasis}
        edges = [(None, i) for i in range(len(nodes))]
        edges += [(a, b) for a, b in ((0, 6), (6, 1), (1, 2), (2, 3), (4, 5))
                  if a < len(nodes) and b < len(nodes)]
        for index, (source, target) in enumerate(edges):
            start = centre if source is None else positions[source]
            end = positions[target]
            delta = end - start
            bend = 0.10 if source is None else -0.22
            middle = (start + end) / 2 + QPointF(-delta.y() * bend, delta.x() * bend)
            path = QPainterPath(start)
            path.quadTo(middle, end)
            colour = colours[nodes[target][3]]
            painter.setPen(QPen(with_alpha(colour, 0.42 if source is None else 0.26), 1.2))
            painter.drawPath(path)
            if self._motion:
                t = (self._phase + index * 0.137) % 1.0
                for tail in range(5):
                    point = path.pointAtPercent((t - tail * 0.012) % 1.0)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(with_alpha(colour, 0.85 * (1 - tail / 5)))
                    painter.drawEllipse(point, 2.5 - tail * 0.3, 2.5 - tail * 0.3)
                painter.setBrush(Qt.BrushStyle.NoBrush)

        hub = QRectF(centre.x() - 59, centre.y() - 30, 118, 60)
        painter.setBrush(QColor(scheme.surface))
        painter.setPen(QPen(QColor(scheme.accent), 1.5))
        painter.drawRoundedRect(hub, 9, 9)
        painter.setBrush(QColor(scheme.accent))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(hub.left() + 14, hub.top() + 17), 3, 3)
        font = QFont(self.font())
        font.setPointSizeF(10)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(scheme.ink))
        painter.drawText(hub.adjusted(24, 3, -4, -25), Qt.AlignmentFlag.AlignCenter, "核查对象")
        font.setPointSizeF(8)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor(scheme.muted))
        painter.drawText(hub.adjusted(3, 27, -3, -5), Qt.AlignmentFlag.AlignCenter, "关系结构示意")

        for index, (_, _, title, kind) in enumerate(nodes):
            point = positions[index]
            colour = colours[kind]
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(with_alpha(colour, 0.18), 5))
            painter.drawEllipse(point, radius + 3, radius + 3)
            painter.setBrush(QColor(scheme.surface))
            painter.setPen(QPen(with_alpha(colour, 0.85), 1.3))
            painter.drawEllipse(point, radius, radius)
            self._draw_glyph(painter, point, kind, colour, radius)
            font.setPointSizeF(8 if compact else 9)
            painter.setFont(font)
            painter.setPen(QColor(scheme.ink))
            label = QRectF(point.x() - 36, point.y() + radius + 5, 72, 20)
            painter.drawText(label, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, title)

        font.setPointSizeF(8)
        painter.setFont(font)
        painter.setPen(QColor(scheme.muted))
        painter.drawText(QRectF(12, 8, self.width() - 24, 24),
                         Qt.AlignmentFlag.AlignVCenter, "关联空间  /  视觉示意")
        legend_y = self.height() - 18
        for index, (kind, title) in enumerate((("person", "对象"), ("account", "账户"),
                                              ("document", "资料"))):
            x = 16 + index * 70
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(colours[kind]))
            painter.drawEllipse(QPointF(x, legend_y), 3, 3)
            painter.setPen(QColor(scheme.muted))
            painter.drawText(QRectF(x + 9, legend_y - 10, 50, 20),
                             Qt.AlignmentFlag.AlignVCenter, title)
        painter.end()

    @staticmethod
    def _point(box: QRectF, nx: float, ny: float):
        return QPointF(box.left() + nx * box.width(), box.top() + ny * box.height())

    @staticmethod
    def _draw_glyph(painter, point, kind, colour, radius):
        painter.save()
        painter.translate(point)
        painter.scale(radius / 21, radius / 21)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(colour), 1.5))
        if kind == "person":
            painter.drawEllipse(QRectF(-4, -10, 8, 8))
            path = QPainterPath()
            path.moveTo(-9, 10)
            path.cubicTo(-9, -3, 9, -3, 9, 10)
            painter.drawPath(path)
        elif kind == "account":
            painter.drawRoundedRect(QRectF(-11, -8, 22, 16), 3, 3)
            painter.drawLine(-10, -2, 10, -2)
            painter.drawLine(-6, 3, -1, 3)
        else:
            painter.drawRoundedRect(QRectF(-7, -10, 14, 20), 2, 2)
            painter.drawLine(-3, -3, 4, -3)
            painter.drawLine(-3, 2, 4, 2)
            painter.drawLine(-3, 6, 1, 6)
        painter.restore()


class Sparkline(QWidget):
    """Real per-page values drawn as a small signal line; never invents points."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("sparkline")
        self.setMinimumHeight(52)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._values: list[float] = []
        self._scheme: Tokens = tokens("terminal", "light")
        self._caption = "当前页金额"
        self._empty_text = "当前页没有可绘制的金额"

    def set_tokens(self, scheme: Tokens) -> None:
        self._scheme = scheme
        self.update()

    def set_caption(self, text: str) -> None:
        self._caption = text
        self.update()

    def set_empty_text(self, text: str) -> None:
        self._empty_text = text
        self.update()

    def set_values(self, values: list[float]) -> None:
        self._values = [abs(float(value)) for value in values if value is not None]
        self.update()

    def paintEvent(self, event) -> None:
        scheme = self._scheme
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        box = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        painter.fillRect(self.rect(), QColor(scheme.surface))
        painter.setPen(QPen(with_alpha(scheme.line, 0.9), 1.0))
        for ratio in (0.25, 0.5, 0.75):
            y = box.top() + box.height() * ratio
            painter.drawLine(box.left(), y, box.right(), y)

        font = QFont(self.font())
        font.setPointSizeF(7.5)
        painter.setFont(font)
        painter.setPen(QColor(scheme.muted))
        painter.drawText(box.adjusted(5, 2, -5, 0), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft, self._caption)

        if len(self._values) < 2:
            painter.drawText(
                box.adjusted(5, 14, -5, -2),
                Qt.AlignmentFlag.AlignCenter,
                self._empty_text if not self._values else "记录不足，暂不绘制",
            )
            painter.end()
            return

        peak = max(self._values) or 1.0
        step = box.width() / (len(self._values) - 1)
        path = QPainterPath()
        for index, value in enumerate(self._values):
            x = box.left() + index * step
            y = box.top() + 20 + (box.height() - 26) * (1 - value / peak)
            if index == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        painter.setPen(QPen(with_alpha(scheme.emphasis, 0.95), 1.4))
        painter.drawPath(path)
        last_x = box.left() + (len(self._values) - 1) * step
        last_y = box.top() + 20 + (box.height() - 26) * (1 - self._values[-1] / peak)
        painter.setBrush(QColor(scheme.emphasis))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRectF(last_x - 2.2, last_y - 2.2, 4.4, 4.4))
        painter.end()


class GridPanel(QWidget):
    """Content container painted with a spatial grid (network layout backdrop)."""

    def __init__(self, parent: QWidget | None = None, *, step: int = 24):
        super().__init__(parent)
        self.setObjectName("workspaceRoot")
        self._scheme: Tokens = tokens("network", "light")
        self._step = step
        self._visible = True

    def set_tokens(self, scheme: Tokens) -> None:
        self._scheme = scheme
        self.update()

    def set_grid_visible(self, visible: bool) -> None:
        self._visible = visible
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        scheme = self._scheme
        painter.fillRect(self.rect(), QColor(scheme.bg))
        if self._visible:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(with_alpha(scheme.grid, 0.9))
            x = self._step / 2
            while x < self.width():
                y = self._step / 2
                while y < self.height():
                    painter.drawEllipse(QRectF(x, y, 1.5, 1.5))
                    y += self._step
                x += self._step
        painter.end()
