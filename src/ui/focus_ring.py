"""
ui/focus_ring.py — Controller focus ring for scrollable pages.

A mouse-transparent accent outline parented to a QScrollArea's viewport
(so it is clipped with the content) that follows a target widget through
scrolling and resizing. Works on any widget regardless of its own
stylesheet, including fully self-painted ones like SettingsToggle.
"""

from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import Qt, QEvent, QRectF, QPoint
from PyQt6.QtGui import QPainter, QPen, QColor

from ui.style import COLORS


class FocusRing(QWidget):
    PAD = 3

    def __init__(self, scroll_area):
        super().__init__(scroll_area.viewport())
        self._scroll = scroll_area
        self._target = None
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()
        scroll_area.verticalScrollBar().valueChanged.connect(self._reposition)
        scroll_area.viewport().installEventFilter(self)

    def show_for(self, widget):
        if self._target is not None:
            self._target.removeEventFilter(self)
        self._target = widget
        widget.installEventFilter(self)
        self._reposition()

    def clear(self):
        if self._target is not None:
            self._target.removeEventFilter(self)
        self._target = None
        self.hide()

    def eventFilter(self, obj, ev):
        if ev.type() in (QEvent.Type.Move, QEvent.Type.Resize,
                         QEvent.Type.Show, QEvent.Type.Hide):
            self._reposition()
        return False

    def _reposition(self, *_):
        w = self._target
        if w is None or not w.isVisible():
            self.hide()
            return
        tl = w.mapTo(self.parentWidget(), QPoint(0, 0))
        pad = self.PAD
        self.setGeometry(tl.x() - pad, tl.y() - pad,
                         w.width() + 2 * pad, w.height() + 2 * pad)
        self.show()
        self.raise_()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(COLORS["accent"]), 2))
        p.drawRoundedRect(QRectF(1, 1, self.width() - 2, self.height() - 2), 8, 8)
        p.end()
