"""
show_window.py — the fullscreen show screen.

Layers, bottom to top:
  QGraphicsVideoItem            the video (fit / fill / stretch)
  fading overlay                question label, physics wheel, START button
  countdown text                READY / 3-2-1 / GO! / BREAK  (centre, big)
  TimecodeStrip                 elapsed clock in a corner, on a gradient
                                "bookmark" that fades out towards the middle
  TransportBar                  auto-hiding play / restart / skip / stop /
                                mute / volume / seek controls
  status strip                  relay + ESP link state

Keys
  Space / Enter  START · RESET      P  pause / resume     R  restart clip
  N  skip video       S  stop        M  mute              F  cycle video fit
  ← / →  seek 5 s     ↑ / ↓ volume   T  toggle transport   H  help
  Esc  settings       F11 fullscreen
"""

from __future__ import annotations

import time

from PyQt6.QtCore import (QPropertyAnimation, QRectF, QSizeF, Qt, QTimer,
                          pyqtSignal)
from PyQt6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter
from PyQt6.QtMultimediaWidgets import QGraphicsVideoItem
from PyQt6.QtWidgets import (QGraphicsProxyWidget, QGraphicsScene,
                             QGraphicsView, QGraphicsWidget, QHBoxLayout,
                             QLabel, QPushButton, QSizePolicy, QSlider, QStyle,
                             QWidget)

import esp_link
from config import AppConfig, format_time
from game_engine import GameEngine
from wheel_widget import WheelWidget

_BTN_STYLE = """
QPushButton {
    background-color: #1d939e; color: #ffffff;
    border: 2px solid #35c5d0; border-radius: 12px;
    padding: 12px 30px; font-size: 20px; font-weight: 800;
    letter-spacing: 2px;
}
QPushButton:hover  { background-color: #35c5d0; color: #06282b; }
QPushButton:pressed{ background-color: #0e6b73; }
"""

_TRANSPORT_STYLE = """
QWidget#transport { background: rgba(10, 12, 16, 205); border-radius: 14px; }
QPushButton {
    background: rgba(38, 42, 50, 190); color: #dfe4ec;
    border: 1px solid rgba(80, 88, 100, 160); border-radius: 8px;
    padding: 6px 10px; min-width: 34px; font-weight: 700;
}
QPushButton:hover  { background: #35c5d0; color: #06282b; border-color: #35c5d0; }
QPushButton:pressed{ background: #0e6b73; }
QLabel { color: #aeb6c2; font-family: Consolas, monospace; }
QSlider::groove:horizontal { height: 5px; background: #2d323b; border-radius: 3px; }
QSlider::sub-page:horizontal { background: #35c5d0; border-radius: 3px; }
QSlider::handle:horizontal {
    background: #eaf3f5; width: 12px; margin: -5px 0; border-radius: 6px; }
"""

_GEAR_STYLE = """
QPushButton {
    background-color: rgba(20, 24, 30, 190); color: #9adfe6;
    border: 1px solid rgba(53, 197, 208, 140); border-radius: 9px;
    padding: 7px 14px; font-size: 13px; font-weight: 600;
}
QPushButton:hover { background-color: rgba(53, 197, 208, 220); color: #06282b; }
"""

ASPECT_MODES = ["fit", "fill", "stretch"]


def _transparent(widget: QWidget):
    widget.setAutoFillBackground(False)
    widget.setStyleSheet(widget.styleSheet() + "background: transparent;")


# ==========================================================================
class TimecodeStrip(QWidget):
    """The clock overlay.

    Text sits hard against its corner on a horizontal gradient that starts at
    the configured opacity beside the text and reaches fully transparent at the
    far end — the "bookmark" look, so nothing boxes in the picture.
    """

    def __init__(self, cfg: AppConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.elapsed = 0.0
        self.total = 0.0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.hide()

    def set_time(self, elapsed: float, total: float):
        self.elapsed, self.total = elapsed, total
        visible = self.cfg.ui.timecode_enabled and total > 0.0
        self.setVisible(visible)
        if visible:
            self.update()

    def text(self) -> str:
        u = self.cfg.ui
        if u.timecode_show_remaining and self.total > 0:
            head = "-" + format_time(max(0.0, self.total - self.elapsed))
        else:
            head = format_time(self.elapsed)
        if u.timecode_show_total and self.total > 0:
            return f"{head} / {format_time(self.total)}"
        return head

    def right_aligned(self) -> bool:
        return self.cfg.ui.timecode_corner.endswith("right")

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        w, h = self.width(), self.height()
        peak = max(0.0, min(1.0, self.cfg.ui.timecode_opacity))
        right = self.right_aligned()

        grad = QLinearGradient(0, 0, w, 0)
        solid = QColor(0, 0, 0, int(255 * peak))
        clear = QColor(0, 0, 0, 0)
        if right:                       # opaque at the right, fading left
            grad.setColorAt(0.0, clear)
            grad.setColorAt(0.55, QColor(0, 0, 0, int(255 * peak * 0.55)))
            grad.setColorAt(1.0, solid)
        else:
            grad.setColorAt(0.0, solid)
            grad.setColorAt(0.45, QColor(0, 0, 0, int(255 * peak * 0.55)))
            grad.setColorAt(1.0, clear)
        p.fillRect(self.rect(), QBrush(grad))

        # thin accent rule along the solid edge, fading with the strip
        line = QLinearGradient(0, 0, w, 0)
        accent = QColor("#35c5d0")
        a1 = QColor(accent); a1.setAlpha(int(210 * peak))
        a0 = QColor(accent); a0.setAlpha(0)
        line.setColorAt(0.0, a0 if right else a1)
        line.setColorAt(1.0, a1 if right else a0)
        p.fillRect(QRectF(0, h - 2.0, w, 1.6), QBrush(line))

        f = QFont("Consolas")
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setPointSizeF(max(9.0, 15.0 * max(0.4, self.cfg.ui.timecode_scale)))
        f.setBold(True)
        p.setFont(f)
        pad = 16
        rect = QRectF(pad, 0, w - 2 * pad, h)
        flag = (Qt.AlignmentFlag.AlignRight if right
                else Qt.AlignmentFlag.AlignLeft) | Qt.AlignmentFlag.AlignVCenter
        p.setPen(QColor(0, 0, 0, 190))
        p.drawText(rect.adjusted(1, 1, 1, 1), flag, self.text())
        p.setPen(QColor("#f4f8fb"))
        p.drawText(rect, flag, self.text())


# ==========================================================================
class TransportBar(QWidget):
    """Auto-hiding control bar: everything you can do to a running video."""

    def __init__(self, cfg: AppConfig, engine: GameEngine, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.engine = engine
        self.setObjectName("transport")
        self.setStyleSheet(_TRANSPORT_STYLE)
        self._seeking = False

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 9, 14, 9)
        row.setSpacing(9)
        st = self.style()

        def button(pixmap, tip, slot, text=""):
            b = QPushButton(text)
            if pixmap is not None:
                b.setIcon(st.standardIcon(pixmap))
            b.setToolTip(tip)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(slot)
            row.addWidget(b)
            return b

        self.btn_play = button(QStyle.StandardPixmap.SP_MediaPause,
                               "Pause / resume  (P)", engine.toggle_pause)
        button(QStyle.StandardPixmap.SP_BrowserReload,
               "Restart this clip from its IN point  (R)", engine.restart_video)
        button(QStyle.StandardPixmap.SP_MediaSeekBackward,
               "Back 5 s  (←)", lambda: engine.seek_by(-5.0))
        button(QStyle.StandardPixmap.SP_MediaSeekForward,
               "Forward 5 s  (→)", lambda: engine.seek_by(5.0))
        button(QStyle.StandardPixmap.SP_MediaSkipForward,
               "Skip to the end of this clip  (N)", engine.skip_to_end)
        button(QStyle.StandardPixmap.SP_MediaStop,
               "Stop everything and reset  (S)", engine.stop_all)

        self.position = QSlider(Qt.Orientation.Horizontal)
        self.position.setRange(0, 1000)
        self.position.setToolTip("Scrub the running clip")
        self.position.sliderPressed.connect(self._seek_start)
        self.position.sliderReleased.connect(self._seek_end)
        row.addWidget(self.position, 1)

        self.clock = QLabel("00:00 / 00:00")
        row.addWidget(self.clock)

        self.btn_mute = button(QStyle.StandardPixmap.SP_MediaVolume,
                               "Mute / unmute  (M)", engine.toggle_mute)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setFixedWidth(110)
        self.volume.setValue(int(engine.volume * 100))
        self.volume.setToolTip("Volume  (↑ / ↓)")
        self.volume.valueChanged.connect(lambda v: engine.set_volume(v / 100.0))
        row.addWidget(self.volume)

        self._total = 0.0
        engine.volume_changed.connect(self._on_volume)
        engine.playing_changed.connect(self._on_playing)
        self._on_volume(engine.volume, engine.is_muted)

    # ---- seek -----------------------------------------------------------
    def _seek_start(self):
        self._seeking = True

    def _seek_end(self):
        self._seeking = False
        if self._total > 0:
            v = self.engine.current_video
            base = v.start_time if v else 0.0
            self.engine.seek_to(base + self._total * self.position.value() / 1000.0)

    def set_time(self, elapsed: float, total: float):
        self._total = total
        self.clock.setText(f"{format_time(elapsed)} / {format_time(total)}")
        if total > 0 and not self._seeking:
            self.position.setValue(int(1000 * min(1.0, elapsed / total)))
        elif total <= 0:
            self.position.setValue(0)

    def _on_playing(self, playing: bool):
        st = self.style()
        self.btn_play.setIcon(st.standardIcon(
            QStyle.StandardPixmap.SP_MediaPause if playing
            else QStyle.StandardPixmap.SP_MediaPlay))

    def _on_volume(self, volume: float, muted: bool):
        st = self.style()
        self.btn_mute.setIcon(st.standardIcon(
            QStyle.StandardPixmap.SP_MediaVolumeMuted if muted or volume <= 0
            else QStyle.StandardPixmap.SP_MediaVolume))
        if not self.volume.isSliderDown():
            self.volume.blockSignals(True)
            self.volume.setValue(int(volume * 100))
            self.volume.blockSignals(False)


# ==========================================================================
class ShowWindow(QGraphicsView):
    request_settings = pyqtSignal()

    def __init__(self, cfg: AppConfig, engine: GameEngine,
                 link: esp_link.EspLink, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.engine = engine
        self.link = link

        self.setWindowTitle("ESP32-S3 Show Controller")
        self.setFrameStyle(0)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

        self._scene = QGraphicsScene(self)
        self._scene.setBackgroundBrush(QBrush(QColor(0, 0, 0)))
        self.setScene(self._scene)

        # ---- video ----------------------------------------------------------
        self.video_item = QGraphicsVideoItem()
        self.video_item.setZValue(0)
        self.video_item.setAspectRatioMode(Qt.AspectRatioMode.IgnoreAspectRatio)
        self._scene.addItem(self.video_item)
        self.video_item.nativeSizeChanged.connect(lambda _s: self._layout())
        engine.attach_video_output(self.video_item)

        # ---- fading overlay --------------------------------------------------
        self.overlay = QGraphicsWidget()
        self.overlay.setZValue(1)
        self._scene.addItem(self.overlay)

        self.question_label = QLabel("PRESS START")
        self.question_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.question_label.setWordWrap(True)
        self.question_label.setStyleSheet("color: #f2f5f9; font-weight: 900;")
        _transparent(self.question_label)
        self._question_proxy = QGraphicsProxyWidget(self.overlay)
        self._question_proxy.setWidget(self.question_label)

        self.wheel = WheelWidget(cfg.wheel)
        _transparent(self.wheel)
        self._wheel_proxy = QGraphicsProxyWidget(self.overlay)
        self._wheel_proxy.setWidget(self.wheel)

        self.start_button = QPushButton(cfg.ui.start_button_text)
        self.start_button.setStyleSheet(_BTN_STYLE)
        self.start_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_button.clicked.connect(engine.handle_button_press)
        self._button_proxy = QGraphicsProxyWidget(self.overlay)
        self._button_proxy.setWidget(self.start_button)

        # ---- countdown (centre, never fades) ---------------------------------
        self.countdown_label = QLabel("READY")
        self.countdown_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.countdown_label.setStyleSheet(
            "color: #ffffff; font-weight: 900;"
            "background: rgba(0, 0, 0, 120); border-radius: 14px;"
            "padding: 4px 22px;")
        self._countdown_proxy = QGraphicsProxyWidget()
        self._countdown_proxy.setWidget(self.countdown_label)
        self._countdown_proxy.setZValue(2)
        self._scene.addItem(self._countdown_proxy)

        # ---- status strip ------------------------------------------------------
        self.status_widget = QWidget()
        self.status_widget.setStyleSheet(
            "background: rgba(8, 10, 14, 215); color: #c8cdd6;")
        srow = QHBoxLayout(self.status_widget)
        srow.setContentsMargins(14, 3, 14, 3)
        self.status_left = QLabel("...")
        self.status_left.setTextFormat(Qt.TextFormat.RichText)
        # Ignored width: the show text gives way instead of shoving the link
        # indicator off the right-hand edge.
        self.status_left.setSizePolicy(QSizePolicy.Policy.Ignored,
                                       QSizePolicy.Policy.Preferred)
        self.status_left.setMinimumWidth(0)
        self.status_right = QLabel("")
        self.status_right.setTextFormat(Qt.TextFormat.RichText)
        self.status_right.setMinimumWidth(210)
        self.status_right.setAlignment(Qt.AlignmentFlag.AlignRight
                                       | Qt.AlignmentFlag.AlignVCenter)
        self.status_right.setSizePolicy(QSizePolicy.Policy.Fixed,
                                        QSizePolicy.Policy.Preferred)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSize(10)
        self.status_left.setFont(mono)
        self.status_right.setFont(mono)
        _transparent(self.status_left)
        _transparent(self.status_right)
        srow.addWidget(self.status_left, 1)
        srow.addSpacing(14)
        srow.addWidget(self.status_right, 0)
        self._status_proxy = QGraphicsProxyWidget()
        self._status_proxy.setWidget(self.status_widget)
        self._status_proxy.setZValue(3)
        self._scene.addItem(self._status_proxy)

        # ---- overlays that are plain children of the view ----------------------
        self.timecode = TimecodeStrip(cfg, self)
        self.transport = TransportBar(cfg, engine, self)
        self.transport.hide()
        self._video_active = False      # transport only exists during a clip

        self.gear = QPushButton("\u2699  SETTINGS", self)
        self.gear.setStyleSheet(_GEAR_STYLE)
        self.gear.setCursor(Qt.CursorShape.PointingHandCursor)
        self.gear.clicked.connect(self.request_settings.emit)
        self.gear.hide()

        self.help_label = QLabel(self)
        self.help_label.setStyleSheet(
            "background: rgba(8,10,14,235); color:#d7dbe2; border:1px solid #35c5d0;"
            "border-radius:12px; padding:16px 22px; font-size:13px;")
        self.help_label.setText(
            "<b style='color:#9adfe6'>SHOW CONTROLS</b><br><br>"
            "<b>Space / Enter</b>  start &middot; reset<br>"
            "<b>P</b>  pause / resume &nbsp;&nbsp; <b>R</b>  restart clip<br>"
            "<b>N</b>  skip video &nbsp;&nbsp; <b>S</b>  stop everything<br>"
            "<b>M</b>  mute &nbsp;&nbsp; <b>&uarr; / &darr;</b>  volume<br>"
            "<b>&larr; / &rarr;</b>  seek 5 s &nbsp;&nbsp; <b>F</b>  video fit<br>"
            "<b>T</b>  transport bar &nbsp;&nbsp; <b>H</b>  this help<br>"
            "<b>Esc</b>  settings &nbsp;&nbsp; <b>F11</b>  fullscreen")
        self.help_label.hide()

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._hide_chrome)

        self._corner_clicks = []
        self._fade_anim = None

        # ---- wiring -------------------------------------------------------------
        engine.wheel_rebuild.connect(self.wheel.set_pieces)
        engine.spin_requested.connect(self.wheel.spin)
        engine.wheel_reset.connect(self.wheel.reset_spin_state)
        self.wheel.spin_ended.connect(engine.on_wheel_result)

        engine.question_text.connect(self.question_label.setText)
        engine.countdown_text.connect(self._set_countdown)
        engine.start_button_text.connect(self.start_button.setText)
        engine.fade_wheel.connect(self.set_overlay_visible)
        engine.status_updated.connect(self._on_status)
        engine.timecode_updated.connect(self._on_timecode)
        engine.playing_changed.connect(self._on_playing)
        engine.state_changed.connect(self._on_engine_state)
        link.state_changed.connect(lambda _s: self._refresh_status_right())
        link.board_identity.connect(lambda _n: self._refresh_status_right())

        self._refresh_status_right()

    # ------------------------------------------------------------ fades
    def set_overlay_visible(self, show: bool):
        dur_ms = max(0, int(self.cfg.ui.fade_duration * 1000))
        if self._fade_anim is not None:
            self._fade_anim.stop()
        if show:
            self.overlay.setVisible(True)
        anim = QPropertyAnimation(self.overlay, b"opacity", self)
        anim.setDuration(dur_ms)
        anim.setStartValue(self.overlay.opacity())
        anim.setEndValue(1.0 if show else 0.0)
        if not show:
            anim.finished.connect(lambda: self.overlay.setVisible(False))
        anim.start()
        self._fade_anim = anim

    # ------------------------------------------------------------ overlays
    def _on_timecode(self, elapsed: float, total: float):
        self.timecode.set_time(elapsed, total)
        self.transport.set_time(elapsed, total)
        self._layout_timecode()

    def _on_playing(self, playing: bool):
        """Paused still counts as an active clip — you need the controls then."""
        if playing:
            self._video_active = True
            self._reveal_chrome()

    def _on_engine_state(self, state: str):
        active = state in ("Playing", "Transitioning")
        if active == self._video_active:
            return
        self._video_active = active
        if not active:
            self.transport.hide()          # never sits over the START button
        elif self.cfg.ui.transport_bar:
            self._reveal_chrome()

    def _on_status(self, d: dict):
        e = self.cfg.esp
        u = self.cfg.ui
        states = d.get("relays", {})

        parts = [f"Video: {d.get('video', '...')}"]
        for relay in e.relays:
            on = bool(states.get(relay.id, False))
            colour = "#43d17c" if on else "#ff5c5c"
            text = relay.on_label if on else relay.off_label
            parts.append(f"{relay.name}: "
                         f"<span style='color:{colour}'>{text}</span>")
        parts.append(f"{u.break_header}: {d.get('break_label', '...')}")
        parts.append(f"{u.loop_header}: {d.get('loops_done', 0)}"
                     f" / {d.get('loops_total', 1)}")
        self.status_left.setText("| " + " | ".join(parts) + " |")

    def _refresh_status_right(self):
        st = self.link.state
        name = self.link.identity or "ESP32"
        self.status_right.setText(
            f"<span style='color:{esp_link.state_color(st)}'>"
            f"\u25cf {esp_link.state_label(st)}</span>"
            f" <span style='color:#7f8694'>{name}</span>")

    def _set_countdown(self, text: str):
        self.countdown_label.setText(text)
        self._countdown_proxy.setVisible(bool(text))
        self._layout_countdown()

    # ------------------------------------------------------------ settings hooks
    def apply_ui_settings(self):
        """Called live when the settings window changes something."""
        self.start_button.setText(
            self.cfg.ui.stop_button_text if self.engine.state != "Idle"
            else self.cfg.ui.start_button_text)
        self.wheel.set_settings(self.cfg.wheel)
        self._layout()
        self.timecode.update()

    def cycle_aspect(self):
        idx = (ASPECT_MODES.index(self.cfg.ui.show_aspect)
               if self.cfg.ui.show_aspect in ASPECT_MODES else 0)
        self.cfg.ui.show_aspect = ASPECT_MODES[(idx + 1) % len(ASPECT_MODES)]
        self.engine.log.emit(f"Video fit: {self.cfg.ui.show_aspect.upper()}")
        self._layout()

    # ------------------------------------------------------------ layout
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout()

    def showEvent(self, event):
        super().showEvent(event)
        self._layout()

    def _layout(self):
        w = max(320, self.viewport().width())
        h = max(240, self.viewport().height())
        self._scene.setSceneRect(0, 0, w, h)

        nat = self.video_item.nativeSize()
        mode = self.cfg.ui.show_aspect
        if nat.isValid() and nat.width() > 0 and nat.height() > 0 and mode != "stretch":
            ar = nat.width() / nat.height()
            fit_h = w / ar
            if mode == "fill":                      # cover, overflow is clipped
                if fit_h < h:
                    vw, vh = h * ar, float(h)
                else:
                    vw, vh = float(w), fit_h
            else:                                   # fit / letterbox
                if fit_h > h:
                    vw, vh = h * ar, float(h)
                else:
                    vw, vh = float(w), fit_h
            self.video_item.setSize(QSizeF(vw, vh))
            self.video_item.setPos((w - vw) / 2, (h - vh) / 2)
        else:
            self.video_item.setSize(QSizeF(w, h))
            self.video_item.setPos(0, 0)

        self.overlay.setGeometry(QRectF(0, 0, w, h))

        qf = QFont("Arial Black")
        qf.setPointSizeF(max(16.0, min(46.0, h * 0.038)))
        qf.setBold(True)
        self.question_label.setFont(qf)
        self._question_proxy.setGeometry(QRectF(w * 0.04, h * 0.025,
                                                w * 0.92, h * 0.115))

        side = min(w * 0.86, h * 0.60)
        self._wheel_proxy.setGeometry(QRectF((w - side) / 2,
                                             h * 0.47 - side / 2, side, side))

        bw, bh = min(380.0, w * 0.5), 62.0
        self._button_proxy.setGeometry(QRectF((w - bw) / 2, h * 0.892, bw, bh))

        self._layout_countdown()
        self._layout_timecode()

        self.status_widget.setFixedWidth(w)
        self._status_proxy.setGeometry(QRectF(0, h - 30, w, 30))
        self._status_proxy.setVisible(self.cfg.ui.show_status_bar)

        bar_w = min(920, int(w * 0.94))
        self.transport.setFixedWidth(bar_w)
        self.transport.adjustSize()
        bottom = h - (34 if self.cfg.ui.show_status_bar else 8)
        self.transport.move((w - bar_w) // 2,
                            bottom - self.transport.height() - 6)

        self.gear.adjustSize()
        self.gear.move(w - self.gear.width() - 16,
                       h - self.gear.height() - (42 if self.cfg.ui.show_status_bar else 14))

        self.help_label.adjustSize()
        self.help_label.move((w - self.help_label.width()) // 2,
                             (h - self.help_label.height()) // 2)

    def _layout_countdown(self):
        w = max(320, self.viewport().width())
        h = max(240, self.viewport().height())
        cf = QFont("Arial Black")
        cf.setPointSizeF(max(15.0, min(40.0, h * 0.030)))
        cf.setBold(True)
        self.countdown_label.setFont(cf)
        self.countdown_label.adjustSize()
        self._countdown_proxy.setPos((w - self.countdown_label.width()) / 2,
                                     h * 0.815 - self.countdown_label.height() / 2)

    def _layout_timecode(self):
        w = max(320, self.viewport().width())
        h = max(240, self.viewport().height())
        scale = max(0.4, self.cfg.ui.timecode_scale)
        strip_h = int(38 * scale)
        strip_w = int(min(w * 0.55, max(240, 330 * scale)))
        corner = self.cfg.ui.timecode_corner
        x = w - strip_w if corner.endswith("right") else 0
        y = int(12 * scale) if corner.startswith("top") else h - strip_h - 40
        self.timecode.setGeometry(x, y, strip_w, strip_h)
        self.timecode.raise_()

    # ------------------------------------------------------------ input
    def keyPressEvent(self, event):
        key = event.key()
        eng = self.engine
        if key in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            eng.handle_button_press()
        elif key == Qt.Key.Key_Escape:
            self.request_settings.emit()
        elif key == Qt.Key.Key_F11:
            if self.isFullScreen():
                self.ensure_windowed()
            else:
                self.showFullScreen()
        elif key == Qt.Key.Key_P:
            eng.toggle_pause()
        elif key == Qt.Key.Key_N:
            eng.skip_to_end()
        elif key == Qt.Key.Key_S:
            eng.stop_all()
        elif key == Qt.Key.Key_R:
            eng.restart_video()
        elif key == Qt.Key.Key_M:
            eng.toggle_mute()
        elif key == Qt.Key.Key_F:
            self.cycle_aspect()
        elif key == Qt.Key.Key_Left:
            eng.seek_by(-5.0)
        elif key == Qt.Key.Key_Right:
            eng.seek_by(5.0)
        elif key == Qt.Key.Key_Up:
            eng.set_volume(min(1.0, eng.volume + 0.05))
        elif key == Qt.Key.Key_Down:
            eng.set_volume(max(0.0, eng.volume - 0.05))
        elif key == Qt.Key.Key_T:
            self.cfg.ui.transport_bar = not self.cfg.ui.transport_bar
            self._reveal_chrome()
        elif key == Qt.Key.Key_H:
            self.help_label.setVisible(not self.help_label.isVisible())
            self.help_label.raise_()
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event):
        pos = event.position()
        if pos.x() < 90 and pos.y() < 90:
            now = time.monotonic()
            self._corner_clicks = [t for t in self._corner_clicks
                                   if now - t < 2.0] + [now]
            if len(self._corner_clicks) >= 5:
                self._corner_clicks.clear()
                self.request_settings.emit()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        self._reveal_chrome()
        super().mouseMoveEvent(event)

    def _reveal_chrome(self):
        self.unsetCursor()
        self.gear.show()
        self.gear.raise_()
        # Video controls are a playback tool, not part of the idle screen.
        if self.cfg.ui.transport_bar and self._video_active:
            self.transport.show()
            self.transport.raise_()
        else:
            self.transport.hide()
        self._hide_timer.start(max(800, int(self.cfg.ui.transport_autohide * 1000)))

    def _hide_chrome(self):
        self.gear.hide()
        self.transport.hide()
        if self.isFullScreen():
            self.setCursor(Qt.CursorShape.BlankCursor)

    # ------------------------------------------------------------ mode
    def ensure_windowed(self):
        """Give the window its title bar back.

        Coming out of fullscreen on Windows can leave the frame off, which
        strands the show with no way to move, minimise or close it. Re-asserting
        the plain Window flag and clearing the window state fixes that; the flag
        change hides the widget, so it has to be shown again afterwards.
        """
        if self.isFullScreen():
            self.showNormal()
        flags = self.windowFlags()
        wanted = (Qt.WindowType.Window
                  | Qt.WindowType.WindowMinMaxButtonsHint
                  | Qt.WindowType.WindowCloseButtonHint
                  | Qt.WindowType.WindowTitleHint
                  | Qt.WindowType.WindowSystemMenuHint)
        if flags != wanted:
            self.setWindowFlags(wanted)
        self.setWindowState(Qt.WindowState.WindowNoState)
        self.unsetCursor()
        self.show()

    def enter_show_mode(self):
        if self.cfg.ui.fullscreen_on_start:
            self.showFullScreen()
        else:
            self.ensure_windowed()
            if self.width() < 640 or self.height() < 400:
                self.resize(1280, 760)
        self.raise_()
        self.activateWindow()
        self.setFocus()
        self._layout()
