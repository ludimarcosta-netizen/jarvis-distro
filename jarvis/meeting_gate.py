"""Só o som de aplicativo de reunião entra nas reuniões e atas.

O áudio do sistema (loopback) mistura tudo que toca no PC: chamada, mas também vídeo do YouTube,
áudio do WhatsApp, música. Esta guarda descobre, a cada poucos segundos, se há som saindo de um
aplicativo de reunião (Zoom, Teams, Discord, Slack, Webex, Skype ou uma aba de Meet/Teams/Zoom no
navegador) e registra os intervalos em `meeting_app_intervals`.

- Ao vivo: o motor ambiente descarta o loopback quando a guarda está fechada.
- Passada offline: o áudio do loopback fora desses intervalos é silenciado.
- Sem leitura confiável (Core Audio indisponível) a guarda fica aberta, nunca esconde uma reunião;
  `meeting_gate_coverage` diz em que períodos a guarda realmente estava de olho.
"""

import ctypes
import re
import sqlite3
import threading
import time
from contextlib import closing
from ctypes import wintypes
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from storage import DB_PATH

POLL_S = 2.0
HOLD_S = 30.0  # a chamada fica muda quando ninguém fala: segura a guarda aberta por um tempo

MEETING_PROCESSES = {
    "zoom.exe", "cpthost.exe", "teams.exe", "ms-teams.exe", "msteams.exe", "discord.exe", "slack.exe",
    "webex.exe", "webexhost.exe", "atmgr.exe", "skype.exe", "lync.exe", "bluejeans.exe", "gotomeeting.exe",
}
BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe"}
# O título da janela do navegador é o da aba ativa: Meet/Teams/Zoom/Discord na aba em uso contam.
MEETING_TITLE = re.compile(
    r"google meet|meet\.google|\bmeet\b\s*[-–]|microsoft teams|\bteams\b|zoom|discord|webex|whereby|jitsi|"
    r"skype|gotomeeting|bluejeans|huddle", re.IGNORECASE)

Session = Tuple[str, int]  # (nome do processo em minúsculas, estado: 1 = tocando)


def meeting_app_playing(sessions: Iterable[Session], titles: Dict[str, Sequence[str]]) -> Optional[str]:
    """Nome do aplicativo de reunião com som saindo agora, ou None. Função pura (testável)."""
    for name, state in sessions:
        if state != 1:
            continue
        if name in MEETING_PROCESSES:
            return name
        if name in BROWSERS and any(MEETING_TITLE.search(t) for t in titles.get(name, ())):
            return name
    return None


# ── leitura do Windows ────────────────────────────────────────────────────────────

def read_sessions() -> List[Session]:
    from pycaw.pycaw import AudioUtilities  # noqa: PLC0415 — só existe no Windows com a dependência
    found = []
    for session in AudioUtilities.GetAllSessions():
        if session.Process is not None:
            found.append((session.Process.name().lower(), int(session.State)))
    return found


def read_window_titles() -> Dict[str, List[str]]:
    import psutil  # noqa: PLC0415
    user32 = ctypes.windll.user32
    titles: Dict[str, List[str]] = {}
    names: Dict[int, str] = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _param):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in names:
            try:
                names[pid.value] = psutil.Process(pid.value).name().lower()
            except psutil.Error:
                names[pid.value] = ""
        if names[pid.value] in BROWSERS:
            titles.setdefault(names[pid.value], []).append(buffer.value)
        return True

    user32.EnumWindows(visit, 0)
    return titles


# ── intervalos no banco ────────────────────────────────────────────────────────────

def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS meeting_app_intervals "
                 "(id INTEGER PRIMARY KEY AUTOINCREMENT, started_at REAL NOT NULL, ended_at REAL NOT NULL, app TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS meeting_gate_coverage "
                 "(id INTEGER PRIMARY KEY AUTOINCREMENT, started_at REAL NOT NULL, ended_at REAL NOT NULL)")
    return conn


def _merge(spans: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    merged: List[Tuple[float, float]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def kept_intervals(t0: float, t1: float, db_path: str = DB_PATH) -> List[Tuple[float, float]]:
    """Trechos de [t0, t1] (epoch) em que o loopback vale: com aplicativo de reunião tocando ou fora da
    cobertura da guarda (não sabemos, então não escondemos)."""
    with closing(_connect(db_path)) as conn:
        meeting = conn.execute("SELECT started_at, ended_at FROM meeting_app_intervals "
                               "WHERE ended_at >= ? AND started_at <= ?", (t0, t1)).fetchall()
        covered = conn.execute("SELECT started_at, ended_at FROM meeting_gate_coverage "
                               "WHERE ended_at >= ? AND started_at <= ?", (t0, t1)).fetchall()
    unknown, cursor = [], t0
    for start, end in _merge([(max(s, t0), min(e, t1)) for s, e in covered]):
        if start > cursor:
            unknown.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < t1:
        unknown.append((cursor, t1))
    return _merge([(max(s, t0), min(e, t1)) for s, e in meeting] + unknown)


# ── a guarda ──────────────────────────────────────────────────────────────────────

class MeetingAudioGate:
    def __init__(self, db_path: str = DB_PATH, poll_s: float = POLL_S, hold_s: float = HOLD_S,
                 sessions: Callable[[], List[Session]] = read_sessions,
                 titles: Callable[[], Dict[str, List[str]]] = read_window_titles,
                 clock: Callable[[], float] = time.time):
        self.db_path, self.poll_s, self.hold_s = db_path, poll_s, hold_s
        self._sessions, self._titles, self._clock = sessions, titles, clock
        self._available = True
        self._last_hit = 0.0
        self._app: Optional[str] = None
        self._interval_id: Optional[int] = None
        self._coverage_id: Optional[int] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def app(self) -> Optional[str]:
        return self._app

    def active(self) -> bool:
        """True com aplicativo de reunião tocando (ou recém-tocado). Aberta quando a leitura falha."""
        return self._app is not None or not self._available

    def poll(self) -> None:
        now = self._clock()
        try:
            sessions = self._sessions()
            hit = meeting_app_playing(sessions, self._titles() if any(n in BROWSERS for n, _ in sessions) else {})
        except Exception as exc:  # noqa: BLE001 — sem leitura confiável a guarda abre, nunca esconde reunião
            if self._available:
                print(f"[!] Guarda de reunião indisponível, áudio do sistema liberado: {exc}")
            self._available, self._coverage_id = False, None
            return
        self._available = True
        if hit:
            self._last_hit, self._app = now, hit
        elif self._app and now - self._last_hit > self.hold_s:
            self._app, self._interval_id = None, None
        self._record(now)

    def _record(self, now: float) -> None:
        with closing(_connect(self.db_path)) as conn, conn:
            if self._coverage_id is None:
                self._coverage_id = conn.execute("INSERT INTO meeting_gate_coverage (started_at, ended_at) "
                                                 "VALUES (?, ?)", (now, now)).lastrowid
            else:
                conn.execute("UPDATE meeting_gate_coverage SET ended_at = ? WHERE id = ?", (now, self._coverage_id))
            if self._app is None:
                return
            if self._interval_id is None:
                self._interval_id = conn.execute("INSERT INTO meeting_app_intervals (started_at, ended_at, app) "
                                                 "VALUES (?, ?, ?)", (now, now, self._app)).lastrowid
            else:
                conn.execute("UPDATE meeting_app_intervals SET ended_at = ? WHERE id = ?", (now, self._interval_id))

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception as exc:  # noqa: BLE001 — banco ocupado etc.: tenta de novo no próximo ciclo
                print(f"[!] Guarda de reunião: {exc}")
            self._stop.wait(self.poll_s)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._coverage_id = self._interval_id = None
        self._thread = threading.Thread(target=self._loop, daemon=True, name="jarvis-meeting-gate")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
