"""
main.py — application entry point.

    python main.py            start in fullscreen show mode
    python main.py --windowed start windowed
    python main.py --settings open straight into settings

Wires everything together the way the Unity scene did:
  AppConfig  <->  GameEngine (VideoHardwareController)
             <->  ShowWindow (scene canvas + VideoPlayer + wheel)
             <->  SettingsWindow (all the custom inspectors)
  EspLink (Uduino UDP + TrafficManager) <- RelayBank (DualRelayController)
                                           bound to the engine's relay state.
On exit every relay is commanded OFF before the socket closes; if the app is
killed hard, the firmware's own 1.5 s heartbeat timeout kills them anyway.
"""

from __future__ import annotations

import signal
import sys

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

from config import AppConfig, PRIORITY_CRITICAL
from rng import entropy
from esp_link import EspLink, RelayBank
from game_engine import GameEngine
from settings_window import SettingsWindow
from show_window import ShowWindow

_APP_STYLE = """
QWidget        { background-color: #14161a; color: #d7dbe2; font-size: 13px; }
QGroupBox      { border: 1px solid #2a2e36; border-radius: 8px;
                 margin-top: 12px; padding-top: 10px; font-weight: 700;
                 color: #9adfe6; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
                 background: #1d2026; border: 1px solid #2f333c;
                 border-radius: 6px; padding: 4px 6px;
                 selection-background-color: #1d939e; }
QComboBox QAbstractItemView { background: #1d2026; }
QPushButton    { background: #23262d; border: 1px solid #33373f;
                 border-radius: 7px; padding: 6px 12px; }
QPushButton:hover   { background: #2c313a; border-color: #35c5d0; }
QPushButton:pressed { background: #191c21; }
QPushButton:disabled{ color: #5c626d; }
QListWidget, QTableWidget {
                 background: #171a1f; border: 1px solid #2a2e36;
                 border-radius: 7px; }
QListWidget::item:selected { background: #1d939e; color: #ffffff; }
QHeaderView::section { background: #1d2026; border: 0;
                 border-bottom: 1px solid #2a2e36; padding: 4px; }
QTabWidget::pane { border: 1px solid #2a2e36; border-radius: 8px; }
QTabBar::tab   { background: #1a1d22; padding: 8px 16px;
                 border-top-left-radius: 8px; border-top-right-radius: 8px;
                 margin-right: 2px; }
QTabBar::tab:selected { background: #23262d; color: #9adfe6; }
QCheckBox::indicator, QRadioButton::indicator { width: 15px; height: 15px; }
QProgressBar   { background: #1d2026; border: 1px solid #2f333c;
                 border-radius: 6px; text-align: center; }
QProgressBar::chunk { background: #1d939e; border-radius: 5px; }
QScrollBar:vertical { background: #14161a; width: 11px; }
QScrollBar::handle:vertical { background: #2f333c; border-radius: 5px;
                 min-height: 24px; }
QScrollBar:horizontal { background: #14161a; height: 11px; }
QScrollBar::handle:horizontal { background: #2f333c; border-radius: 5px; }
QToolTip       { background: #23262d; color: #d7dbe2;
                 border: 1px solid #35c5d0; }
QSplitter::handle { background: #22252b; }
"""


def _dark_palette() -> QPalette:
    p = QPalette()
    p.setColor(QPalette.ColorRole.Window, QColor("#14161a"))
    p.setColor(QPalette.ColorRole.Base, QColor("#171a1f"))
    p.setColor(QPalette.ColorRole.Text, QColor("#d7dbe2"))
    p.setColor(QPalette.ColorRole.WindowText, QColor("#d7dbe2"))
    p.setColor(QPalette.ColorRole.Button, QColor("#23262d"))
    p.setColor(QPalette.ColorRole.ButtonText, QColor("#d7dbe2"))
    p.setColor(QPalette.ColorRole.Highlight, QColor("#1d939e"))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    return p


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("ESP32-S3 Show Controller")
    app.setStyle("Fusion")
    app.setPalette(_dark_palette())
    app.setStyleSheet(_APP_STYLE)

    cfg = AppConfig.load()
    entropy.set_mode(cfg.wheel.entropy)

    link = EspLink(cfg.esp)
    bank = RelayBank(link, cfg.esp)
    engine = GameEngine(cfg)
    bank.game_source = engine.relay_states           # BindToGame

    show = ShowWindow(cfg, engine, link)
    settings = SettingsWindow(cfg, engine, link, bank)

    # console mirror of every subsystem log
    for sig in (engine.log, link.log):
        sig.connect(lambda m: print(m))
        sig.connect(settings.show_log)

    def open_settings():
        # The show keeps running underneath — nothing is paused or reset.
        if show.isFullScreen() and not cfg.ui.settings_keeps_fullscreen:
            show.ensure_windowed()      # keep the title bar when leaving fullscreen
        settings.show()
        settings.raise_()
        settings.activateWindow()

    def back_to_show():
        settings.hide()
        show.enter_show_mode()

    show.request_settings.connect(open_settings)
    settings.return_to_show.connect(back_to_show)
    # live edits repaint the show immediately, mid-video
    settings.ui_changed.connect(show.apply_ui_settings)
    settings.ui_changed.connect(lambda: entropy.set_mode(cfg.wheel.entropy))

    # boot ----------------------------------------------------------------
    engine.stop_all()                                # paints READY state
    if cfg.esp.auto_connect:
        link.start()

    if "--settings" in sys.argv:
        show.showNormal()
        show.resize(1100, 680)
        open_settings()
    elif "--windowed" in sys.argv:
        cfg.ui.fullscreen_on_start = False
        show.enter_show_mode()
    else:
        show.enter_show_mode()

    # shutdown ---------------------------------------------------------------
    def cleanup():
        try:
            engine.stop_all()
            bank.all_off()
            # push the two OFF packets out immediately, bypassing rate limits
            for relay in cfg.esp.relays:
                link.request_command("SetRelay", PRIORITY_CRITICAL,
                                     relay.pin, 0)
            link.flush()
            link.stop()
        except Exception:
            pass

    app.aboutToQuit.connect(cleanup)
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    # let the interpreter notice Ctrl+C in the terminal
    keepalive = QTimer()
    keepalive.start(300)
    keepalive.timeout.connect(lambda: None)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
