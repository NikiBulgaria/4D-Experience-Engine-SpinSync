"""
esp_link.py — the WiFi/UDP bridge to the ESP32-S3.

Ports three Unity pieces into one object:
  TrafficManager.cs      -> priority queue + SetRelay smart-merge + send pacing
  EspHeartbeat.cs        -> "Heartbeat" every 0.5 s, identity handshake
  DualRelayController.cs -> RelayBank below (bind-to-game + test override timer)

=== WHY THE OLD BUILD SAT ON "SEARCHING" ===========================
Custom_Esp32_S3.cpp sends its replies like this:

    UDP_Receiver.beginPacket(remote, port);   // port == 4222

`port` is the *listening* port, so every reply is addressed to
<PC ip>:4222 — NOT to the ephemeral source port our packet came from.
A socket bound to port 0 therefore never hears a single byte back, no matter
how healthy the link is. We now bind to the same port we send to (4222) with
SO_REUSEADDR, so the handshake completes.

Two more details from that firmware worth knowing:

  * `if (remote == IPAddress(0,0,0,0)) remote = UDP_Receiver.remoteIP();`
    The board latches the FIRST address that talks to it after boot and never
    updates it. If the PC's IP changed, or another device spoke first, replies
    go to the wrong machine — power-cycle the ESP to clear it.

  * The TX buffer flushes when it ends in "\r\n", and Uduino only dispatches a
    command once the line terminates. That is why `terminator` now defaults to
    CRLF: without it commands are buffered forever and the relays never move.
====================================================================
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Callable, List, Optional, Tuple

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from config import (EspSettings, PRIORITY_CRITICAL, PRIORITY_HIGH,
                    PRIORITY_LOW)

STATE_OFFLINE = "offline"            # socket closed, not trying
STATE_CONNECTING = "connecting"      # trying for the first time, no reply yet
STATE_RECONNECTING = "reconnecting"  # was connected, lost it, still retrying
STATE_CONNECTED = "connected"        # board is answering
STATE_TRUSTED = "trusted"            # sending without verification

# colour, short label, and what it actually means
STATE_INFO = {
    STATE_OFFLINE: ("#8a8f99", "OFFLINE",
                    "Not connected and not trying. Press CONNECT."),
    STATE_CONNECTING: ("#ffb04a", "CONNECTING",
                       "Sending and waiting for the board's first reply\u2026"),
    STATE_RECONNECTING: ("#ff7a3d", "RECONNECTING",
                         "Reply lost \u2014 still sending commands and retrying."),
    STATE_CONNECTED: ("#43d17c", "CONNECTED", "The board is answering."),
    STATE_TRUSTED: ("#35c5d0", "SENDING",
                    "Verification is switched off \u2014 commands go out "
                    "unchecked."),
}

ACTIVE_STATES = (STATE_CONNECTING, STATE_RECONNECTING, STATE_CONNECTED,
                 STATE_TRUSTED)


def state_color(state: str) -> str:
    return STATE_INFO.get(state, STATE_INFO[STATE_OFFLINE])[0]


def state_label(state: str) -> str:
    return STATE_INFO.get(state, STATE_INFO[STATE_OFFLINE])[1]


def state_hint(state: str) -> str:
    return STATE_INFO.get(state, STATE_INFO[STATE_OFFLINE])[2]


def button_label(state: str) -> str:
    """What the primary button should say in each state."""
    if state == STATE_OFFLINE:
        return "CONNECT"
    if state == STATE_CONNECTING:
        return "CANCEL"
    return "DISCONNECT"

_PROBE_SEARCH = 1.0              # identity probe interval while searching

# Uduino.cpp sets term='\r' and drops every non-printable char, so a command is
# dispatched by the CARRIAGE RETURN alone. "\n" or "" can never execute anything,
# which is why they are not candidates.
_TERMINATOR_CANDIDATES = ["\r\n", "\r"]

PROBE_IDENTITY = "identity"      # allocates on the board, use sparingly
PROBE_PING = "Ping"              # answers from a static string, zero heap


class _QueuedCommand:
    __slots__ = ("name", "args", "priority", "stamp")

    def __init__(self, name: str, args: tuple, priority: int):
        self.name = name
        self.args = args
        self.priority = priority
        self.stamp = time.monotonic()


class EspLink(QObject):
    """One UDP socket, Uduino wire format, with the reply path fixed."""

    state_changed = pyqtSignal(str)
    on_ready = pyqtSignal()                  # EspHeartbeat.OnReady equivalent
    packet_sent = pyqtSignal(str, int)       # text, priority
    packet_received = pyqtSignal(str, str)   # text, from_ip
    board_identity = pyqtSignal(str)         # name from "uduinoIdentity X"
    board_found = pyqtSignal(str, str)       # ip, name (broadcast scan)
    stats_changed = pyqtSignal(dict)
    log = pyqtSignal(str)

    def __init__(self, cfg: EspSettings, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._sock: Optional[socket.socket] = None
        self._enabled = False
        self._state = STATE_OFFLINE
        self._identity_name = ""
        self._bound_port = 0

        self._queue: List[_QueuedCommand] = []
        self._inbox: List[Tuple[str, str]] = []
        self._inbox_lock = threading.Lock()
        self._rx_thread: Optional[threading.Thread] = None

        self._last_send = 0.0
        self._last_probe = 0.0
        self._last_reply = 0.0
        self._probe_sent_at = 0.0
        self.rtt_ms = -1.0
        self.tx_count = 0
        self.rx_count = 0
        self.probes_sent = 0
        self.probes_answered = 0
        self._probe_open = False
        self._last_command_at = 0.0
        self._last_socket_refresh = 0.0
        self._probe_cmd = PROBE_IDENTITY
        self._ping_misses = 0
        self._rx_accum = ""

        # protocol auto-detection
        self._detecting = False
        self._detect_index = 0
        self._detect_started = 0.0

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(10)
        self._tick_timer.timeout.connect(self._tick)

        self._hb_timer = QTimer(self)
        self._hb_timer.timeout.connect(self._heartbeat)

    # ------------------------------------------------------------- properties
    @property
    def state(self) -> str:
        return self._state

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def identity(self) -> str:
        return self._identity_name

    @property
    def bound_port(self) -> int:
        return self._bound_port

    @property
    def seconds_since_reply(self) -> float:
        if self._last_reply <= 0:
            return -1.0
        return time.monotonic() - self._last_reply

    # ------------------------------------------------------------- lifecycle
    def _make_socket(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass

        # bind to the port the firmware replies to
        bind_port = self.cfg.local_port if self.cfg.bind_local_port else 0
        try:
            s.bind(("", bind_port))
        except OSError as ex:
            self.log.emit(f"Port {bind_port} is busy ({ex}). Falling back to a "
                          f"random port — replies will NOT be heard, so the "
                          f"status cannot leave CONNECTING even though relay "
                          f"commands still go out.")
            s.bind(("", 0))
        self._bound_port = s.getsockname()[1]

        if hasattr(socket, "SIO_UDP_CONNRESET"):       # Windows only
            try:
                s.ioctl(socket.SIO_UDP_CONNRESET, False)
            except OSError:
                pass
        s.settimeout(0.2)
        return s

    def _refresh_socket(self):
        """Rebuild the socket during a long reconnect: covers the case where
        the adapter changed, the PC slept, or Windows dropped the binding."""
        old, self._sock = self._sock, None
        if old is not None:
            try:
                old.close()
            except OSError:
                pass
        try:
            self._sock = self._make_socket()
        except OSError as ex:
            self.log.emit(f"Could not reopen the socket: {ex}")
            return
        self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
        self._rx_thread.start()
        self.log.emit(f"Socket refreshed on :{self._bound_port}")

    def start(self):
        if self._enabled:
            return
        try:
            self._sock = self._make_socket()
        except OSError as ex:
            self.log.emit(f"Could not open UDP socket: {ex}")
            return

        self._enabled = True
        self._last_reply = 0.0
        self._last_probe = 0.0
        self.rtt_ms = -1.0
        self.tx_count = 0
        self.rx_count = 0
        self.probes_sent = 0
        self.probes_answered = 0
        self._probe_open = False
        self._probe_cmd = PROBE_IDENTITY
        self._ping_misses = 0
        self._rx_accum = ""
        self._last_socket_refresh = time.monotonic()
        self._set_state(STATE_TRUSTED if not self.cfg.identity_probe
                        else STATE_CONNECTING)

        self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
        self._rx_thread.start()
        self._tick_timer.start()
        self.apply_settings(self.cfg)

        self.log.emit(f"UDP up: local :{self._bound_port} -> "
                      f"{self.cfg.ip}:{self.cfg.port}  "
                      f"(terminator {self._term_name()})")
        if self.cfg.auto_detect_protocol:
            self.begin_auto_detect(quiet=True)

    def stop(self):
        if self._enabled and self._sock is not None:
            try:
                self._send_raw("disconnected")
            except OSError:
                pass
        self._enabled = False
        self._detecting = False
        self._tick_timer.stop()
        self._hb_timer.stop()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        self._queue.clear()
        self._identity_name = ""
        self._bound_port = 0
        self._set_state(STATE_OFFLINE)

    def apply_settings(self, cfg: EspSettings):
        self.cfg = cfg
        interval = max(50, int(cfg.heartbeat_rate * 1000))
        self._hb_timer.setInterval(interval)
        if self._enabled and not self._hb_timer.isActive():
            self._hb_timer.start()

    def _term_name(self) -> str:
        return {"\r\n": "CRLF", "\n": "LF", "": "none"}.get(self.cfg.terminator,
                                                            repr(self.cfg.terminator))

    # ------------------------------------------------------------- TrafficManager
    def request_command(self, name: str, priority: int, *args):
        """Queue a command. SetRelay merges per pin so a burst of toggles
        collapses to the latest value (TrafficManager smart-merge)."""
        if name == "SetRelay" and args:
            pin = args[0]
            for cmd in self._queue:
                if cmd.name == "SetRelay" and cmd.args and cmd.args[0] == pin:
                    cmd.args = tuple(args)
                    cmd.priority = max(cmd.priority, priority)
                    self._sort_queue()
                    return
        self._queue.append(_QueuedCommand(name, tuple(args), priority))
        self._sort_queue()

    def send_now(self, text: str):
        """Fire a raw line immediately (console / diagnostics)."""
        if self._send_raw(text):
            self.packet_sent.emit(text, PRIORITY_CRITICAL)

    def _sort_queue(self):
        self._queue.sort(key=lambda c: (-c.priority, c.stamp))

    def _heartbeat(self):
        if self._enabled:
            self.request_command("Heartbeat", PRIORITY_CRITICAL)

    def _tick(self):
        if not self._enabled:
            return
        self._drain_inbox()
        now = time.monotonic()

        if self._detecting:
            self._detect_step(now)

        # pacing (TrafficManager.SendDelay)
        if self._queue and now - self._last_send >= max(0.005, self.cfg.send_delay):
            cmd = self._queue.pop(0)
            text = cmd.name
            if cmd.args:
                text += self.cfg.arg_delimiter + self.cfg.arg_delimiter.join(
                    str(a) for a in cmd.args)
            if self._send_raw(text):
                self.packet_sent.emit(text, cmd.priority)
            self._last_send = now
            self._last_command_at = now

        # ---- identity handshake ------------------------------------------
        # Probing costs the board a whole loop iteration, and it can only read
        # one packet per loop, so never probe while real commands are queued —
        # relay traffic matters more than a status dot.
        if self.cfg.identity_probe and not self._detecting:
            interval = (max(0.5, self.cfg.probe_interval)
                        if self._state == STATE_CONNECTED else _PROBE_SEARCH)
            quiet = (now - self._last_command_at) > 0.12
            if now - self._last_probe >= interval and not self._queue and quiet:
                if self._probe_open:                 # previous one went unanswered
                    if self._probe_cmd == PROBE_PING:
                        self._ping_misses += 1
                        if self._ping_misses >= 3:
                            # board has no Ping command (stock firmware)
                            self._probe_cmd = PROBE_IDENTITY
                            self._ping_misses = 0
                            self.log.emit("No answer to Ping — falling back to "
                                          "the identity handshake.")
                self._last_probe = now
                self._probe_sent_at = now
                self.probes_sent += 1
                self._probe_open = True
                self._send_raw(self._probe_cmd)

        # ---- watchdog ------------------------------------------------------
        if not self.cfg.identity_probe:
            if self._state != STATE_TRUSTED:
                self._set_state(STATE_TRUSTED)    # verification switched off
            return

        if self._state == STATE_TRUSTED:          # verification switched back on
            self._set_state(STATE_CONNECTING)

        age = self.seconds_since_reply
        tolerance = max(4.0, self.cfg.link_tolerance)
        if not self.cfg.sticky_link:
            tolerance = max(2.0, self.cfg.reply_timeout)

        if self._state == STATE_CONNECTED and age >= 0 and age > tolerance:
            self._set_state(STATE_RECONNECTING)
            self.log.emit(f"No reply for {age:.0f}s — retrying. Relay commands "
                          f"are still going out.")

        # a long reconnect gets a fresh socket now and then
        if (self._state == STATE_RECONNECTING
                and now - self._last_socket_refresh > 20.0):
            self._last_socket_refresh = now
            self._refresh_socket()

    def flush(self, max_packets: int = 8):
        """Send queued packets right now (shutdown path — ignores SendDelay)."""
        sent = 0
        while self._queue and sent < max_packets and self._enabled:
            cmd = self._queue.pop(0)
            text = cmd.name
            if cmd.args:
                text += self.cfg.arg_delimiter + self.cfg.arg_delimiter.join(
                    str(a) for a in cmd.args)
            if self._send_raw(text):
                self.packet_sent.emit(text, cmd.priority)
            sent += 1
        self._last_send = time.monotonic()

    # ------------------------------------------------------------- socket I/O
    def _send_raw(self, text: str, addr: Optional[Tuple[str, int]] = None,
                  terminator: Optional[str] = None) -> bool:
        sock = self._sock
        if sock is None:
            return False
        term = self.cfg.terminator if terminator is None else terminator
        payload = (text + term).encode("utf-8", errors="replace")
        try:
            sock.sendto(payload, addr or (self.cfg.ip, self.cfg.port))
            self.tx_count += 1
            return True
        except OSError as ex:
            self.log.emit(f"Send failed: {ex}")
            return False

    def _rx_loop(self):
        while self._enabled:
            sock = self._sock
            if sock is None:
                return
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                continue
            text = data.decode("utf-8", errors="replace").strip()
            with self._inbox_lock:
                self._inbox.append((text, addr[0]))

    def _drain_inbox(self):
        with self._inbox_lock:
            items, self._inbox = self._inbox, []
        for text, ip in items:
            if not text:
                continue
            self.rx_count += 1
            self._last_reply = time.monotonic()
            if self._probe_open:
                self._probe_open = False
                self.probes_answered += 1
                if self._probe_cmd == PROBE_PING:
                    self._ping_misses = 0
            if self._probe_sent_at > 0:
                self.rtt_ms = (self._last_reply - self._probe_sent_at) * 1000.0
                self._probe_sent_at = 0.0
            self.packet_received.emit(text, ip)

            # A board running the stock Uduino_Wifi before the "connected"
            # handshake sends ONE DATAGRAM PER CHARACTER, so replies have to be
            # reassembled before they mean anything.
            self._rx_accum = (self._rx_accum + text)[-512:]
            merged = self._rx_accum.replace("\r", "\n")
            chunks = merged.split("\n")
            self._rx_accum = chunks[-1]
            lines = [c.strip() for c in chunks[:-1] if c.strip()]
            if not lines and "uduinoidentity" in text.lower():
                lines = [text.strip()]

            name = ""
            for line in lines:
                low = line.lower()
                if "uduinoidentity" in low:
                    parts = line.replace("\t", " ").split()
                    if len(parts) > 1:
                        name = parts[-1]
                elif low.startswith("uduinoping"):
                    pass
                elif not name and len(line) < 40:
                    name = line
            if name and name != self._identity_name:
                self._identity_name = name
                self.board_identity.emit(name)
                self._rx_accum = ""

            if self._detecting:
                self._detect_success()

            if ip == self.cfg.ip or self._state != STATE_CONNECTED:
                self.board_found.emit(ip, name or self._identity_name)
            if self._state in (STATE_CONNECTING, STATE_RECONNECTING):
                self._set_state(STATE_CONNECTED)

    def _set_state(self, state: str):
        if state == self._state:
            return
        was = self._state
        self._state = state
        self._last_socket_refresh = time.monotonic()
        self.state_changed.emit(state)
        if state == STATE_CONNECTED:
            verb = "reconnected" if was == STATE_RECONNECTING else "connected"
            self.log.emit(f"ESP {verb}." + (f"  [{self._identity_name}]"
                                            if self._identity_name else ""))
            # Unity sends this on discovery; it sets Uduino::init = true, which
            # switches the stock library to buffered (one packet per line)
            # transmission instead of one datagram per character.
            self._send_raw("connected")
            # keep-alive now costs the board nothing: Ping answers from a
            # static string, identity malloc()s 32 bytes it never frees
            self._probe_cmd = PROBE_PING
            self._ping_misses = 0
            self.on_ready.emit()

    def reconnect(self):
        """Force a clean retry without touching any settings."""
        if not self._enabled:
            self.start()
            return
        self._identity_name = ""
        self._last_reply = 0.0
        self._refresh_socket()
        self._set_state(STATE_TRUSTED if not self.cfg.identity_probe
                        else STATE_CONNECTING)
        self.log.emit("Retrying the connection\u2026")

    # ------------------------------------------------------------- discovery
    def scan_for_board(self):
        """Broadcast an identity probe; answers surface via board_found."""
        if self._sock is None:
            return
        targets = ["255.255.255.255"]
        try:
            host_ip = socket.gethostbyname(socket.gethostname())
            octets = host_ip.split(".")
            if len(octets) == 4:
                targets.append(".".join(octets[:3] + ["255"]))
        except OSError:
            pass
        for target in targets:
            for term in _TERMINATOR_CANDIDATES:
                self._send_raw("identity", (target, self.cfg.port), term)
        self.log.emit(f"Broadcast identity probe on UDP :{self.cfg.port} …")

    # ------------------------------------------------------------- protocol probe
    def begin_auto_detect(self, quiet: bool = False):
        """Try CRLF / LF / none until the board answers, then keep the winner."""
        if self._sock is None:
            return
        self._detecting = True
        self._detect_index = 0
        self._detect_started = 0.0
        if not quiet:
            self.log.emit("Auto-detecting line ending (CRLF → LF → none) …")

    def _detect_step(self, now: float):
        if self._detect_started == 0.0:
            self._detect_started = now
            term = _TERMINATOR_CANDIDATES[self._detect_index]
            self._probe_sent_at = now
            self._send_raw("identity", None, term)
            return
        if now - self._detect_started < 0.9:
            return
        self._detect_index += 1
        if self._detect_index >= len(_TERMINATOR_CANDIDATES):
            self._detecting = False
            self.log.emit(
                "No reply to any line ending. Commands are still being sent; "
                "check the IP, the firewall, and that PC + ESP share a network. "
                "If the board booted before this PC got its address, power-cycle "
                "it (the firmware latches the first address it hears from).")
            return
        self._detect_started = 0.0

    def _detect_success(self):
        term = _TERMINATOR_CANDIDATES[min(self._detect_index,
                                          len(_TERMINATOR_CANDIDATES) - 1)]
        self._detecting = False
        if term != self.cfg.terminator:
            self.cfg.terminator = term
            self.log.emit(f"Board answered — line ending set to "
                          f"{self._term_name()}.")
        else:
            self.log.emit(f"Board answered ({self._term_name()}).")

    # ------------------------------------------------------------- stats
    def snapshot(self) -> dict:
        return {
            "state": self._state,
            "identity": self._identity_name,
            "tx": self.tx_count,
            "rx": self.rx_count,
            "rtt_ms": self.rtt_ms,
            "local_port": self._bound_port,
            "reply_age": self.seconds_since_reply,
            "queue": len(self._queue),
            "probes_sent": self.probes_sent,
            "probes_answered": self.probes_answered,
            "probe_cmd": self._probe_cmd,
            "quality": (100.0 * self.probes_answered / self.probes_sent
                        if self.probes_sent else -1.0),
        }


class RelayBank(QObject):
    """Owns every configured relay — as many as you define.

    * bind_to_game True  -> states follow `game_source()` (the engine or the
      Test Mode panel), exactly like DualRelayController.BindToGame.
    * Each relay has its OWN manual hold timer, so testing one never cancels
      another.
    * A programmable sequence can drive them all from a timeline.
    * Any state change is pushed through EspLink as
      `SetRelay <pin> <1|0>` at HIGH priority.
    """

    states_changed = pyqtSignal(dict)                 # {relay_id: bool}
    override_progress = pyqtSignal(str, float, float)  # id, remaining, total
    sequence_progress = pyqtSignal(float, float)       # position, length
    sequence_finished = pyqtSignal()

    def __init__(self, link: EspLink, cfg: EspSettings, parent=None):
        super().__init__(parent)
        self.link = link
        self.cfg = cfg
        self.bind_to_game = True
        self.game_source: Optional[Callable[[], dict]] = None

        self.states: dict = {}
        self._sent: dict = {}
        self._holds: dict = {}          # relay_id -> [remaining, total]

        # UDP has no delivery guarantee, and this board drops packets while its
        # OLED blocks the loop. Sending a state change once and assuming it
        # landed is how a relay ends up ON in the app and OFF on the bench.
        # Every change is therefore repeated a few times, and the full state is
        # re-asserted on a slow rotation. SetRelay is idempotent, so repeating
        # it costs nothing but makes a lost packet self-healing.
        self.confirm_repeats = 3
        self.confirm_spacing = 0.20
        self.refresh_interval = 1.5
        self._confirm_left: dict = {}
        self._confirm_at: dict = {}
        self._next_refresh = 0.0
        self._refresh_index = 0

        self.sequence_running = False
        self._seq_pos = 0.0
        self._seq_fired: set = set()

        self._last_tick = time.monotonic()
        self.sync_relay_list()

        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._update)
        self._timer.start()

        link.on_ready.connect(self.force_sync)
        link.state_changed.connect(self._on_link_state)

    # ------------------------------------------------------------- relay list
    def sync_relay_list(self):
        """Called after relays are added, removed or reordered."""
        ids = [r.id for r in self.cfg.relays]
        for rid in ids:
            self.states.setdefault(rid, False)
        for stale in [k for k in self.states if k not in ids]:
            self.states.pop(stale, None)
            self._sent.pop(stale, None)
            self._holds.pop(stale, None)
        self.states_changed.emit(dict(self.states))

    def relay_ids(self) -> list:
        return [r.id for r in self.cfg.relays]

    def is_held(self, relay_id: str) -> bool:
        return relay_id in self._holds

    # ------------------------------------------------- DualRelayController.Update
    def _update(self):
        now = time.monotonic()
        dt = min(0.25, now - self._last_tick)
        self._last_tick = now

        for rid in list(self._holds.keys()):
            remaining, total = self._holds[rid]
            remaining -= dt
            if remaining <= 0:
                self._holds.pop(rid, None)
                self.override_progress.emit(rid, 0.0, total)
                if not self.bind_to_game or self.game_source is None:
                    self.states[rid] = False
                    self.link.log.emit(f"[RelayBank] {self._name(rid)} hold "
                                       f"finished — switching off.")
            else:
                self._holds[rid] = [remaining, total]
                self.override_progress.emit(rid, remaining, total)

        if self.sequence_running:
            self._advance_sequence(dt)

        self._confirm_and_refresh(now)

        if self.bind_to_game and self.game_source is not None:
            try:
                incoming = self.game_source() or {}
            except Exception:
                incoming = {}
            for rid, value in incoming.items():
                if rid in self.states and rid not in self._holds:
                    self.states[rid] = bool(value)

        self._push()

    def _name(self, relay_id: str) -> str:
        for r in self.cfg.relays:
            if r.id == relay_id:
                return r.name
        return relay_id

    def _pin(self, relay_id: str) -> int:
        for r in self.cfg.relays:
            if r.id == relay_id:
                return int(r.pin)
        return -1

    def _transmit(self, relay_id: str, priority: int = PRIORITY_HIGH):
        pin = self._pin(relay_id)
        if pin >= 0:
            self.link.request_command(
                "SetRelay", priority, pin,
                1 if self.states.get(relay_id) else 0)

    def _push(self):
        changed = False
        for rid, value in self.states.items():
            if self._sent.get(rid) != value:
                self._sent[rid] = value
                self._transmit(rid)
                # queue a couple of confirmations in case that packet is lost
                self._confirm_left[rid] = self.confirm_repeats
                self._confirm_at[rid] = time.monotonic() + self.confirm_spacing
                changed = True
        if changed:
            self.states_changed.emit(dict(self.states))

    def _confirm_and_refresh(self, now: float):
        """Repeat recent changes, then keep re-asserting the whole state."""
        for rid in list(self._confirm_left.keys()):
            if rid not in self.states:
                self._confirm_left.pop(rid, None)
                self._confirm_at.pop(rid, None)
                continue
            if now < self._confirm_at.get(rid, 0.0):
                continue
            self._transmit(rid)
            left = self._confirm_left[rid] - 1
            if left <= 0:
                self._confirm_left.pop(rid, None)
                self._confirm_at.pop(rid, None)
            else:
                self._confirm_left[rid] = left
                self._confirm_at[rid] = now + self.confirm_spacing

        # slow round-robin backstop: one relay per interval, lowest priority so
        # it never delays a real change or the heartbeat
        ids = self.relay_ids()
        if not ids or now < self._next_refresh:
            return
        self._next_refresh = now + max(0.5, self.refresh_interval)
        self._refresh_index %= len(ids)
        rid = ids[self._refresh_index]
        self._refresh_index += 1
        if rid not in self._confirm_left:
            self._transmit(rid, PRIORITY_LOW)

    # ------------------------------------------------------------ manual tests
    def set_relay(self, relay_id: str, state: bool, hold: Optional[float] = None):
        """Switch one relay by hand and hold it for its own duration."""
        if relay_id not in self.states:
            return
        if hold is None:
            hold = self._duration(relay_id)
        self.states[relay_id] = bool(state)
        if hold > 0:
            self._holds[relay_id] = [float(hold), float(hold)]
            self.override_progress.emit(relay_id, float(hold), float(hold))
        self._push()

    def _duration(self, relay_id: str) -> float:
        for r in self.cfg.relays:
            if r.id == relay_id:
                return max(0.1, float(r.test_duration))
        return 3.0

    def pulse(self, relay_id: str, seconds: Optional[float] = None):
        self.set_relay(relay_id, True, seconds)

    def all_off(self):
        self.stop_sequence()
        self._holds.clear()
        for rid in self.states:
            self.states[rid] = False
        self.link.request_command("AllOff", PRIORITY_CRITICAL)
        self._push()

    def force_sync(self):
        """Forget what we believe the board knows and state it all again."""
        self._sent.clear()
        self._next_refresh = 0.0
        self._push()

    def _on_link_state(self, state: str):
        # While the link is down nothing we queue is guaranteed to arrive, so
        # drop our record of the board's state and re-assert on reconnect.
        if state in (STATE_OFFLINE, STATE_RECONNECTING):
            self._sent.clear()

    # -------------------------------------------------------------- sequences
    def start_sequence(self):
        """Replay the programmed test timeline from the beginning."""
        if not self.cfg.test_sequence:
            self.link.log.emit("[RelayBank] The test sequence is empty.")
            return
        self._holds.clear()
        for rid in self.states:
            self.states[rid] = False
        self._push()
        self._seq_pos = 0.0
        self._seq_fired.clear()
        self.sequence_running = True
        self.link.log.emit("[RelayBank] Test sequence started.")

    def stop_sequence(self, switch_off: bool = True):
        if not self.sequence_running:
            return
        self.sequence_running = False
        self._seq_pos = 0.0
        self._seq_fired.clear()
        if switch_off:
            for rid in self.states:
                self.states[rid] = False
            self._push()
        self.sequence_progress.emit(0.0, self.cfg.test_sequence_length)
        self.sequence_finished.emit()

    def _advance_sequence(self, dt: float):
        length = max(1.0, float(self.cfg.test_sequence_length))
        self._seq_pos += dt
        for i, step in enumerate(self.cfg.test_sequence):
            if i in self._seq_fired or step.timestamp > self._seq_pos:
                continue
            self._seq_fired.add(i)
            if step.relay_id in self.states:
                self.states[step.relay_id] = bool(step.state)
        self.sequence_progress.emit(min(self._seq_pos, length), length)
        if self._seq_pos >= length:
            self.link.log.emit("[RelayBank] Test sequence finished.")
            self.stop_sequence()
