"""
Rastreador de Reuniões do Jarvis.

A cada SCAN_INTERVAL_S chama meetings_service.tick(): as falas novas entram na
reunião em andamento; 2 min de silêncio fazem o app perguntar se encerra; sem
resposta em 5 min, encerra sozinha. Comandos falados ("Jarvis, iniciando reunião
com X" / "Jarvis, encerrar reunião") abrem e fecham reuniões na hora.

Reunião encerrada gera ata (meeting_summaries) e as tarefas de __JARVIS_USER__ e os
compromissos viram pendências (tabela pendencias). Reuniões, atas e pendências
ficam SÓ neste banco: a memória permanente do Hermes guarda fatos sobre o usuário.
Revisão manual (dividir/juntar, participantes): página local (web_app.py).
"""
import re
import threading
from typing import Any, Dict, List, Optional

import meetings_service
import voice_autoname
from storage import DB_PATH, add_pendencia

SCAN_INTERVAL_S = 15.0


def register_pendencias(ata: Dict[str, Any], ata_id: Optional[int] = None,
                        db_path: str = DB_PATH) -> List[int]:
    """Registra tarefas de __JARVIS_USER__ e compromissos da ata como pendências abertas
    (tabela pendencias). A memória permanente do Hermes não é tocada."""
    ids = [add_pendencia(t, responsavel="__JARVIS_USER__", ata_id=ata_id, db_path=db_path)
           for t in ata.get("tasks_owner") or []]
    ids += [add_pendencia(c, ata_id=ata_id, db_path=db_path) for c in ata.get("commitments") or []]
    ids = [i for i in ids if i > 0]
    if ids:
        print(f"[+] {len(ids)} pendência(s) registrada(s) da ata.")
    return ids


class MeetingTracker:
    """Thread do sentinel: ciclo das reuniões (silêncio, encerramento, atas)."""

    def __init__(self, db_path: str = DB_PATH, interval_s: float = SCAN_INTERVAL_S):
        self.db_path = db_path
        self.interval_s = interval_s
        self.is_running = False
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self.is_running:
            return
        self.is_running = True
        meetings_service.offline.recover(self.db_path)
        self._thread = threading.Thread(target=self._loop, daemon=True, name="jarvis-meeting-tracker")
        self._thread.start()
        print(f"[+] Rastreador de reuniões ativo (pergunta após {meetings_service.PROMPT_AFTER_S / 60:.0f} min "
              f"de silêncio; encerra sozinho {meetings_service.AUTO_CLOSE_AFTER_S / 60:.0f} min depois).")

    def stop(self):
        self.is_running = False
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=3.0)

    def wake(self):
        """Processa o mais rápido possível (ex.: após um comando falado)."""
        self._wake.set()

    def _loop(self):
        while self.is_running:
            self._wake.wait(timeout=self.interval_s)
            self._wake.clear()
            if not self.is_running:
                break
            try:
                meetings_service.tick(db_path=self.db_path)
                voice_autoname.kick(self.db_path)
            except Exception as exc:  # noqa: BLE001 — o rastreador nunca derruba o sentinel
                print(f"[!] Rastreador de reuniões: {exc}")


# ── Detecção dos comandos falados de reunião ─────────────────────────────────

_RE_START = re.compile(
    r"jarvis.{0,50}?\b(iniciando|iniciar|inicia|come(?:c|ç)ando|come(?:c|ç)a|"
    r"abrindo|abrir|marcando|marcar)\b.{0,30}?\breuni", re.IGNORECASE)
_RE_END = re.compile(
    r"jarvis.{0,50}?\b(encerrando|encerrar|encerra|finalizando|finalizar|finaliza|"
    r"terminando|terminar|termina|fechando|fechar|fecha)\b.{0,30}?\breuni", re.IGNORECASE)
_RE_WITH = re.compile(
    r"\breuni(?:ão|ao)\b\s*(?:de\s+|com\s+|sobre\s+)?(?:(?:o|a|os|as|um|uma|meu|minha)\s+)?"
    r"(?P<name>.{2,60})", re.IGNORECASE)


def detect_meeting_command(text: str) -> Optional[Dict[str, str]]:
    """'Jarvis, iniciando reunião com X' -> {'kind': 'start', 'name': 'X'};
    'Jarvis, encerrar reunião' -> {'kind': 'end', 'name': ''}. None caso contrário."""
    text = (text or "").strip()
    if "reuni" not in text.lower() or "jarvis" not in text.lower():
        return None
    if _RE_END.search(text):
        return {"kind": "end", "name": ""}
    match = _RE_START.search(text)
    if match:
        name = ""
        with_match = _RE_WITH.search(text)
        if with_match:
            name = with_match.group("name").strip(" .!?;,")
        return {"kind": "start", "name": name}
    return None
