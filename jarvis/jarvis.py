"""
Orquestrador Principal do J.A.R.V.I.S.
Unifica Escuta Ambiente, Transcrição Local na GPU, Wake Word e Gemini Live.
"""
import os
import time
import re
import json
import asyncio
import argparse
import threading
import traceback
from typing import Optional

import numpy as np

from storage import init_db
from ambient_engine import JarvisAmbientEngine, note_meeting_participant
from wake_detector import WakeWordDetector
from transcriber import DEFAULT_MODEL
from jarvis_live import run_live_session
import desktop_bridge
import meeting_tracker
import meetings_service
import listening_mode
import web_app
from context_extractor import extract_meeting_and_save, get_live_context_for_jarvis
from storage import (add_pendencia, concluir_pendencia, insert_marker, list_pendencias, list_summaries,
                     search_summaries, search_transcripts)

# Prefixos que o Whisper costuma produzir para "Hey Jarvis" — removidos do
# trecho capturado para sobrar só o comando ("jarvis, abra o youtube").
_WAKE_PREFIXES = (
    "hey jarvis", "ei jarvis", "ok jarvis", "okay jarvis", "jarvis",
    "hey jervis", "ei jervis", "jervis", "beijarps", "beijarps,",
)
_WAKE_ALONE_RE = re.compile(r"^[a-zà-ú,.!? ]{0,26}$")


# "J.A.R.V.I.S." (Whisper soletrando) -> "jarvis".
_SPELLED_JARVIS_RE = re.compile(r"j\.?\s?a\.?\s?r\.?\s?v\.?\s?i\.?\s?s\.?", re.IGNORECASE)
# Só a wake word: termina no "jarvis" (qualquer grafia) com no máximo uma palavra
# antes (o "hey" mal transcrito: "vídeos", "ei", "ok"). "Jarvis, que horas são"
# não casa: há conteúdo depois da wake word.
_WAKE_ONLY_RE = re.compile(r"^(?:\w+[\s,]+)?\w*(?:arvis|ervis|jarps)\W*$")


def _normalize_wake(text: str) -> str:
    return _SPELLED_JARVIS_RE.sub("jarvis", text.lower()).strip()


def _looks_like_wake_only(text: str) -> bool:
    """True quando o trecho inteiro é só a wake word (ou lixo dela)."""
    low = _normalize_wake(text).strip(",.!? ")
    if low in ("", "hey", "ei", "ok") or "wake" in low:
        return True
    return bool(_WAKE_ONLY_RE.match(low))


def _strip_wake_prefix(text: str) -> str:
    """Remove o prefixo da wake word do comando capturado."""
    low = _normalize_wake(text).lstrip(",.!? ")
    for prefix in sorted(_WAKE_PREFIXES, key=len, reverse=True):
        if low.startswith(prefix):
            rest = low[len(prefix):].lstrip(",.!? ")
            return rest
    return text.strip()


class JarvisOrchestrator:
    def __init__(self, model_size: str = DEFAULT_MODEL, language: str = "pt", managed: bool = False):
        self.model_size = model_size
        self.language = language
        self.managed = managed  # True = gerenciado pelo app (spawn oculto + watchdog)
        self.is_running = False
        self.ambient_engine: Optional[JarvisAmbientEngine] = None
        self.wake_detector: Optional[WakeWordDetector] = None
        self.tracker: Optional[meeting_tracker.MeetingTracker] = None
        self.web_server = None
        self._in_live_session = False
        self._muted = False
        self._wake_event = threading.Event()
        self._lock_socket = None  # lock de instância única (porta exclusiva)
        # Health check da escuta: espera crescente entre tentativas de religar
        # microfone/wake word depois de uma falha de dispositivo.
        self._listen_backoff_s = 0.0
        self._next_listen_attempt = 0.0

    def _acquire_singleton_lock(self) -> bool:
        """Instância única: quem segura a porta SENTINEL_LOCK_PORT é o sentinel
        vivo. Um segundo processo (spawn duplicado do app, cmd manual com o app
        aberto) sai silenciosamente."""
        import socket as pysocket
        try:
            sock = pysocket.socket(pysocket.AF_INET, pysocket.SOCK_STREAM)
            sock.setsockopt(pysocket.SOL_SOCKET, pysocket.SO_REUSEADDR, 0)
            sock.bind(("127.0.0.1", desktop_bridge.SENTINEL_LOCK_PORT))
            sock.listen(4)
            self._lock_socket = sock
            return True
        except OSError:
            return False

    # ── Mudo global (Fase 5): Ctrl+Shift+M ou botão no app ─────────────────

    def _set_muted(self, muted: bool) -> dict:
        """Pausa/retoma a escuta ambiente + wake word (microfone liberado)."""
        if muted == self._muted:
            pass  # idempotente
        elif muted:
            self._muted = True
            self._wake_event.clear()
            if not self._in_live_session:
                if self.wake_detector:
                    self.wake_detector.stop()
                if self.ambient_engine:
                    self.ambient_engine.stop(discard_pending=True)
            print("[MUDO] Escuta ambiente PAUSADA (privacidade). Ctrl+Shift+M ou o botão no app retomam.")
        else:
            self._muted = False
            if not self._in_live_session and self.is_running:
                try:
                    if self.ambient_engine:
                        self.ambient_engine.start()
                    if self.wake_detector:
                        self.wake_detector.start()
                    print("[MUDO] Escuta ambiente RETOMADA.")
                except Exception as exc:
                    print(f"[!] Falha ao retomar a escuta: {exc}")
        return self._sentinel_state()

    def _sentinel_state(self) -> dict:
        return {
            "running": bool(self.is_running),
            "muted": bool(self._muted),
            "live": bool(self._in_live_session),
            "session": self.ambient_engine.active_session_id if self.ambient_engine else "ambient",
            "model": self.model_size,
        }

    def _start_control_channel(self):
        """O lock de instância também é o canal de controle: MUTE/UNMUTE/STATE."""
        def loop():
            import socket as pysocket
            while self.is_running:
                try:
                    client, _addr = self._lock_socket.accept()
                except OSError:
                    break  # socket fechado no encerramento
                with client:
                    client.settimeout(3.0)
                    try:
                        raw = client.recv(64)
                    except OSError:
                        continue
                    command = raw.decode("utf-8", "replace").strip().upper()
                    if command == "MUTE":
                        state = self._set_muted(True)
                    elif command == "UNMUTE":
                        state = self._set_muted(False)
                    elif command == "TOGGLE":
                        state = self._set_muted(not self._muted)
                    else:  # STATE e qualquer outro comando
                        state = self._sentinel_state()
                    try:
                        client.sendall((json.dumps(state, ensure_ascii=False) + "\n").encode("utf-8"))
                    except OSError:
                        pass
        threading.Thread(target=loop, daemon=True, name="jarvis-control").start()

    def _parse_hotkey(self, spec: str):
        """'ctrl+shift+m' -> (mods, vk). Padrão Ctrl+Shift+M."""
        mods = 0
        vk = None
        mapping = {"ctrl": 0x2, "control": 0x2, "shift": 0x4, "alt": 0x1, "win": 0x8}
        for part in (spec or "ctrl+shift+m").lower().split("+"):
            part = part.strip()
            if part in mapping:
                mods |= mapping[part]
            elif len(part) == 1 and part.isalnum():
                vk = ord(part.upper())
        if not vk:
            return None
        return mods, vk

    def _start_hotkey_listener(self):
        """Hotkey global (Win32 RegisterHotKey): Ctrl+Shift+M alterna o mudo."""
        if os.name != "nt":
            return
        spec = os.environ.get("JARVIS_MUTE_HOTKEY", "ctrl+shift+m")
        parsed = self._parse_hotkey(spec)
        if not parsed:
            print(f"[!] Hotkey inválida: {spec!r}")
            return
        mods, vk = parsed

        def loop():
            import ctypes
            import ctypes.wintypes as wt
            WM_HOTKEY = 0x0312
            if not ctypes.windll.user32.RegisterHotKey(None, 1, mods, vk):
                print(f"[!] Hotkey '{spec}' já usada por outro app; mudo fica só via botão do app.")
                return
            print(f"[+] Mudo global ativo: {spec} alterna a escuta ambiente.")
            msg = wt.MSG()
            pump = ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            while pump and self.is_running:
                if msg.message == WM_HOTKEY and msg.wParam == 1:
                    self._set_muted(not self._muted)
                pump = ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            ctypes.windll.user32.UnregisterHotKey(None, 1)
        threading.Thread(target=loop, daemon=True, name="jarvis-hotkey").start()

    def _start_watchdog(self):
        """Modo gerenciado: morre junto com o app (canal wake some = app
        fechado/crashou; tolera ~45s de reinício do app antes de desistir)."""
        def loop():
            fails = 0
            while self.is_running:
                time.sleep(15)
                if not self.is_running:
                    break
                try:
                    alive = desktop_bridge.bridge_alive()
                except Exception:
                    alive = False
                fails = 0 if alive else fails + 1
                if fails >= 3:
                    print("[*] App Hermes ausente — sentinel gerenciado encerrando.")
                    self.is_running = False
                    self._wake_event.set()  # destrava o loop principal
                    break
        threading.Thread(target=loop, daemon=True, name="jarvis-watchdog").start()

    def _on_meeting_command(self, kind: str, name: str):
        """Comandos falados de reunião vindos do motor ambiente."""
        _spoken_meeting_command(kind, name)

    def _signal_wake(self):
        """Wake-word callback: only signal the main thread."""
        if self.is_running and not self._muted and not self._in_live_session and not listening_mode.enabled():
            self._wake_event.set()

    def _capture_wake_command(self, window_s: float = 4.0) -> tuple[str, bytes]:
        """Captura o que o operador disse logo após 'Hey Jarvis'.

        O motor ambiente continua rodando em modo captura (transcreve sem
        gravar na memória) por uma janela curta; o VAD fecha o trecho ~1,1s
        depois do fim da fala. Retorna o comando sem a wake word e o PCM16
        16 kHz correspondente para a biometria local.
        """
        engine = self.ambient_engine
        if not engine:
            return "", b""
        chunks: list[tuple[str, np.ndarray]] = []

        def on_chunk(text: str, audio_data: np.ndarray, source: str, duration: float):
            # O loopback também usa o motor ambiente. Para autorização vocal,
            # só o microfone do operador pode compor o comando.
            if source != "mic":
                return
            clean = text.strip()
            if clean:
                chunks.append((clean, audio_data.copy()))

        engine.capture_mode = "capture"
        engine.on_captured_chunk = on_chunk
        try:
            deadline = time.time() + window_s
            first_at = None
            while time.time() < deadline:
                if chunks:
                    if first_at is None:
                        first_at = time.time()
                    # Depois do primeiro trecho, dá só mais 2s de continuação
                    # (pause curto entre wake word e comando).
                    if time.time() - first_at > 2.0:
                        break
                time.sleep(0.1)
        finally:
            engine.on_captured_chunk = None
            engine.capture_mode = "save"
        raw = " ".join(text for text, _audio in chunks if text).strip()
        if not raw or _looks_like_wake_only(raw):
            return "", b""
        command = _strip_wake_prefix(raw)
        if not command:
            return "", b""
        samples = np.concatenate([audio for _text, audio in chunks])
        pcm16 = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        return command, pcm16

    def _on_wake_detected(self):
        if not self.is_running or self._muted or self._in_live_session or listening_mode.enabled():
            return

        self._in_live_session = True
        print("\n" + "="*50)
        print(">>> J.A.R.V.I.S. ACORDOU VIA COMANDO DE VOZ <<<")
        print("="*50)

        # Libera o microfone do detector (o app abre o dele ao iniciar a Live).
        if self.wake_detector:
            self.wake_detector.stop()

        try:
            # 1. Acorda o app imediatamente (a discagem da sessão Live leva
            #    2-4s; fazer em paralelo esconde a latência).
            desktop_reachable = desktop_bridge.wake_desktop("")
            if desktop_reachable:
                print("[+] App desktop acionado — abrindo conversa Gemini Live...")

            # 2. Captura o comando dito na sequência ("jarvis, abra o youtube").
            command, wake_pcm16 = self._capture_wake_command()
            if command:
                note_meeting_participant(command)
                print(f"[+] Comando capturado: \"{command}\"")
                if desktop_reachable:
                    # Segunda chamada semeia o comando na sessão que está
                    # abrindo — o usuário não precisa repetir. O PCM segue
                    # apenas para a biometria local; o Gemini recebe o texto.
                    desktop_bridge.wake_desktop(command, pcm16=wake_pcm16)

            # 3. Pausa a escuta ambiente (não gravar a própria conversa). O que
            #    ainda está na fila é o próprio comando: descarta, não vai ao banco.
            if self.ambient_engine:
                self.ambient_engine.stop(discard_pending=True)

            if not desktop_reachable:
                if self.managed:
                    # Gerenciado pelo app: sem fallback local — se o canal sumiu,
                    # o watchdog está de olho; só ignoramos este acorde.
                    print("[*] App não respondeu (gerenciado); watchdog cuidará disso.")
                elif os.environ.get("JARVIS_LOCAL_LIVE_FALLBACK") == "1":
                    # Opt-in: a sessão local delega ao Hermes sem a confirmação
                    # por voz/tela do app, por isso não é o padrão.
                    print("[*] App desktop não respondeu; usando sessão Live local.")
                    asyncio.run(run_live_session())
                else:
                    print("[*] App desktop fechado; abra o Hermes para conversar "
                          "(ou defina JARVIS_LOCAL_LIVE_FALLBACK=1 para a sessão local).")
            else:
                print("[*] Conversa ativa no app. Aguardando encerramento "
                      "(despedida ou ~30s de silêncio)...")
                desktop_bridge.wait_session_end()
        except Exception as exc:
            print(f"[!] Falha na sessão Live: {exc}")
            traceback.print_exc()
        finally:
            self._in_live_session = False
            # Retoma o motor de escuta ambiente + sentinela.
            if self.is_running and not self._muted and self.ambient_engine:
                print("[*] Retomando monitoramento ambiente...")
                try:
                    self.ambient_engine.start()
                    if self.wake_detector:
                        self.wake_detector.start()
                except Exception as exc:
                    print(f"[!] Falha ao retomar o modo sentinela: {exc}")
                    traceback.print_exc()

    def _sync_desktop_session(self, state):
        """Manual desktop conversations suspend the same ambient engines as wake."""
        active = state in ("connecting", "live")
        if active == self._in_live_session:
            return
        self._in_live_session = active
        if active:
            if self.wake_detector:
                self.wake_detector.stop()
            if self.ambient_engine:
                self.ambient_engine.stop()
        elif self.is_running and not self._muted:
            if self.ambient_engine:
                self.ambient_engine.start()
            if self.wake_detector:
                self.wake_detector.start()

    def _ensure_listening(self):
        """Religa motor ambiente e wake word que caíram (dispositivo sumiu, wake
        ignorado), com espera crescente de 5s a 60s entre tentativas."""
        if not self.is_running or self._muted or self._in_live_session or self._wake_event.is_set():
            return
        engine, detector = self.ambient_engine, self.wake_detector
        engine_down = engine is not None and not engine.is_running
        detector_down = detector is not None and not detector.is_running
        if not (engine_down or detector_down):
            self._listen_backoff_s = 0.0
            return
        now = time.monotonic()
        if now < self._next_listen_attempt:
            return
        try:
            if engine_down:
                engine.start()
            if detector_down:
                detector.start()
            print("[+] Escuta ambiente/sentinela restabelecidos.")
            self._listen_backoff_s = 0.0
        except Exception as exc:
            self._listen_backoff_s = min(max(self._listen_backoff_s * 2, 5.0), 60.0)
            self._next_listen_attempt = now + self._listen_backoff_s
            print(f"[!] Escuta indisponível ({exc}); nova tentativa em {self._listen_backoff_s:.0f}s.")

    def start(self):
        if self.managed and not self._acquire_singleton_lock():
            print("[*] Sentinel jarvis já está ativo — esta instância sai em silêncio.")
            return
        init_db()
        self.is_running = True
        # Canal de controle + mudo ANTES dos engines: o lock aceita conexões no
        # backlog desde o bind; sem o accept loop cedo, probes travariam
        # durante o carregamento do whisper (~10s).
        self._start_control_channel()
        self._start_hotkey_listener()
        mode_label = " (gerenciado pelo app)" if self.managed else ""
        print("="*60)
        print("  J.A.R.V.I.S. - SISTEMA EXECUTIVO ATIVO")
        print("  Operador: Senhor __JARVIS_USER__")
        print(f"  Transcrição Local: NVIDIA RTX 2070 (faster-whisper '{self.model_size}')")
        print("  Sentinela: openWakeWord ('Hey Jarvis')")
        print("  Voz e Diálogo: Gemini Multimodal Live API")
        print("  Atas & Pendências: blocos fechados viram ata estruturada")
        print("                     + pendências em cobrança (transcripts.db)")
        print(f"  Modo: sentinel sempre ligado{mode_label}")
        print("="*60)

        # 1. Inicia o motor de escuta ambiente (RTX 2070)
        self.ambient_engine = JarvisAmbientEngine(model_size=self.model_size, language=self.language)
        try:
            self.ambient_engine.start()
            self.ambient_engine.on_meeting_command = self._on_meeting_command

            # 2. Inicia o detector de Wake Word
            self.wake_detector = WakeWordDetector(
                wake_word="hey_jarvis",
                threshold=0.5,
                on_detected=self._signal_wake,
            )
            self.wake_detector.start()

            # 3. Rastreador de reuniões (silêncio, atas, pendências) + página local
            self.tracker = meeting_tracker.MeetingTracker()
            self.tracker.start()
            self.web_server = web_app.start_in_background()

            # 4. Modo gerenciado: watchdog morre junto com o app
            if self.managed:
                self._start_watchdog()
        except Exception:
            self.stop()
            raise

        print("\n[+] Todos os subsistemas operacionais. Pressione Ctrl+C para encerrar.\n")
        last_desktop_check = 0.0
        try:
            while self.is_running:
                try:
                    if time.monotonic() - last_desktop_check >= 1.0:
                        last_desktop_check = time.monotonic()
                        self._sync_desktop_session(desktop_bridge.session_state())
                    if self._wake_event.wait(timeout=0.2):
                        self._wake_event.clear()
                        self._on_wake_detected()
                    self._ensure_listening()
                except Exception as exc:
                    # Falha de dispositivo/ponte não derruba o sentinel: o
                    # health check religa a escuta na próxima volta.
                    print(f"[!] Loop do sentinel: {exc}")
                    traceback.print_exc()
                    time.sleep(1.0)
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        print("\n[*] Encerrando subsistemas do J.A.R.V.I.S....")
        self.is_running = False
        if self.tracker:
            self.tracker.stop()
        if self.web_server:
            self.web_server.shutdown()
        if self.wake_detector:
            self.wake_detector.stop()
        if self.ambient_engine:
            self.ambient_engine.stop()
        print("[+] J.A.R.V.I.S. desativado com sucesso.")

def _spoken_meeting_command(kind: str, name: str) -> None:
    """'Jarvis, iniciando reunião com X' / 'Jarvis, encerrar reunião'. Roda fora da
    thread de transcrição: encerrar gera a ata (chamada de rede)."""
    insert_marker(kind, name if kind == "start" else "")
    print(f"\n[>>> REUNIÃO INICIADA: {name or 'sem nome'} <<<]" if kind == "start"
          else "\n[<<< REUNIÃO ENCERRADA — gerando ata... >>>]")

    def run():
        try:
            meetings_service.handle_spoken_command(kind, name)
        except Exception as exc:  # noqa: BLE001
            print(f"[!] Comando de reunião falhou: {exc}")
    threading.Thread(target=run, daemon=True, name="jarvis-meeting-command").start()


def main():
    parser = argparse.ArgumentParser(description="J.A.R.V.I.S. Master Controller")
    parser.add_argument("command", choices=["start", "live", "listen", "minutes", "context", "search",
                                                "pendencias", "pendencia-add", "pendencia-ok", "web",
                                                "limpar-transcricoes", "atas-pendentes"], nargs="?", default="start",
                        help="Comando de execução (start = sistema completo, live = conversa direta, listen = apenas escuta, minutes = encerra a reunião em andamento, web = página de reuniões, search = consultar atas/transcrições, pendencias = listar pendências, pendencia-add TEXTO = registrar, pendencia-ok ID = concluir)")
    parser.add_argument("query", nargs="?", default=None, help="Termo de busca (search), descrição (pendencia-add) ou id (pendencia-ok)")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="Modelo Faster-Whisper (padrão large-v3-turbo; tiny, base, small, medium também servem)")
    parser.add_argument("--minutes", type=int, default=60, help="Janela de análise para atas/contexto")
    parser.add_argument("--days", type=int, default=14, help="Janela de dias para o comando search")
    parser.add_argument("--managed", action="store_true", help="start: modo gerenciado pelo app (spawn oculto, watchdog, sem janela)")
    parser.add_argument("--prazo", default="", help="pendencia-add: prazo (ex.: 2026-10-05)")
    parser.add_argument("--responsavel", default="__JARVIS_USER__", help="pendencia-add: responsável")
    parser.add_argument("--todas", action="store_true", help="pendencias: inclui as concluídas")
    parser.add_argument("--aplicar", action="store_true",
                        help="limpar-transcricoes: marca de fato (sem a opção, só mostra o que seria marcado)")
    args = parser.parse_args()

    if args.command == "start":
        orchestrator = JarvisOrchestrator(model_size=args.model, managed=args.managed)
        orchestrator.start()
    elif args.command == "live":
        print("[*] Iniciando canal de voz direto com o J.A.R.V.I.S....")
        asyncio.run(run_live_session())
    elif args.command == "listen":
        print(f"[*] Iniciando apenas escuta ambiente na GPU (Whisper {args.model})...")
        engine = JarvisAmbientEngine(model_size=args.model)
        engine.start()
        tracker = meeting_tracker.MeetingTracker()
        tracker.start()
        engine.on_meeting_command = _spoken_meeting_command
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            tracker.stop()
            engine.stop()
    elif args.command == "minutes":
        meetings_service.tick()  # falas recentes entram na reunião em andamento
        active = meetings_service.repo.get_active_meeting()
        if not active:
            print("[*] Nenhuma reunião em andamento. Atas anteriores: jarvis.py search \"termo\""
                  " ou a página http://127.0.0.1:17850")
            return
        print(f"[*] Encerrando a reunião em andamento (#{active['id']}) e gerando a ata...")
        meetings_service.close_meeting(active["id"], closed_by="user")
        ata = meetings_service.repo.get_summary(active["id"])
        if not ata:
            print("[!] A ata não pôde ser gerada agora; o rastreador tenta de novo sozinho.")
            return
        print("\n--- ATA GERADA ---")
        print(f"Título: {ata.get('title')}")
        print(f"Resumo: {ata.get('summary')}")
        for item in str(ata.get("key_decisions") or "").splitlines():
            print(f"  Decisão: {item}")
        for item in str(ata.get("action_items") or "").splitlines():
            print(f"  Tarefa: {item}")
    elif args.command == "limpar-transcricoes":
        from storage import DB_PATH
        import transcript_hygiene
        counts = transcript_hygiene.scan_history(DB_PATH, apply=args.aplicar)
        verb = "Marcados como ignorados" if args.aplicar else "Seriam marcados (simulação; use --aplicar)"
        print(f"[*] {verb}:")
        for reason, n in counts.items():
            print(f"    {n:5}  {reason}")
        if not counts:
            print("    nada a marcar.")
        elif args.aplicar:
            print("[*] As atas afetadas são refeitas pelo rastreador do Jarvis (ou agora: jarvis.py atas-pendentes).")
    elif args.command == "atas-pendentes":
        print("[*] Gerando as atas pendentes (Vertex)...")
        done = meetings_service.generate_pending_atas()
        print(f"[+] {done} ata(s) gerada(s).")
    elif args.command == "web":
        server = web_app.make_server()
        print(f"[+] Página de reuniões: http://127.0.0.1:{server.server_address[1]}  (Ctrl+C encerra)")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.server_close()
    elif args.command == "search":
        if not args.query:
            print("[!] Uso: jarvis.py search \"termo\" [--days 14]")
            return
        print(f"[*] Busca por '{args.query}' (atas dos últimos {args.days} dias):\n")
        found = 0
        for row in search_summaries(args.query, days=args.days):
            found += 1
            print(f"[ATA] {row.get('created_at')} — {row.get('title')}")
            print(f"      {row.get('summary')}")
            for line in str(row.get('action_items') or '').splitlines():
                if line.strip():
                    print(f"      • {line.strip()}")
            print()
        for row in search_transcripts(args.query, days=args.days, limit=40):
            found += 1
            print(f"[FALA] {row.get('timestamp')} ({row.get('source')}): {row.get('text')}")
        if not found:
            print("[*] Nada encontrado nesta janela.")
    elif args.command == "pendencias":
        rows = list_pendencias(incluir_concluidas=args.todas)
        if not rows:
            print("[*] Nenhuma pendência aberta.")
        for row in rows:
            extras = [f"resp.: {row['responsavel']}" if row.get("responsavel") else "",
                      f"prazo: {row['prazo']}" if row.get("prazo") else "",
                      f"ata: {row['ata_titulo']}" if row.get("ata_titulo") else "",
                      f"registrada: {row['created_at']} UTC"]
            mark = "[x]" if row["status"] == "concluida" else "[ ]"
            print(f"{mark} #{row['id']} {row['descricao']} ({'; '.join(e for e in extras if e)})")
    elif args.command == "pendencia-add":
        if not args.query:
            print('[!] Uso: jarvis.py pendencia-add "descrição" [--prazo AAAA-MM-DD] [--responsavel Nome]')
            return
        pid = add_pendencia(args.query, responsavel=args.responsavel, prazo=args.prazo)
        print(f"[+] Pendência #{pid} registrada.")
    elif args.command == "pendencia-ok":
        if not args.query or not args.query.isdigit():
            print("[!] Uso: jarvis.py pendencia-ok ID")
            return
        if concluir_pendencia(int(args.query)):
            print(f"[+] Pendência #{args.query} concluída.")
        else:
            print(f"[!] Pendência #{args.query} não encontrada ou já concluída.")
    elif args.command == "context":
        print(f"[*] Contexto recente ({args.minutes} min):\n")
        print(get_live_context_for_jarvis(minutes=args.minutes))

if __name__ == "__main__":
    main()
