"""
Ponte Sentinel → App Desktop (plugin gemini-live-bridge).

O sentinel detecta "Hey Jarvis" e aciona a conversa Gemini Live NO APP,
passando o comando dito na sequência ("jarvis, abra o youtube").

O backend do app escuta em uma porta local fixa (17832..17835), porque a
porta principal do backend do desktop é dinâmica (--port 0). Endpoints:

    POST /wake {"text": "...", "audio_pcm16": "..."}
                                  -> incrementa o contador; o renderer do app
                                     inicia a sessão Live e semeia o comando.
    GET  /state                  -> {"session": "live"|None, "model": ...}
                                    (sentinel usa para pausar/retomar a
                                    escuta ambiente e aguardar o fim).
"""
import base64
import json
import socket
import time
import urllib.error
import urllib.request
from typing import Optional

# Lock de instância única do sentinel (porta exclusiva no loopback): quem
# segura esta porta É o sentinel vivo; o segundo processo sai imediatamente.
SENTINEL_LOCK_PORT = 17999

# Faixa acordada com o plugin ( tenta 17832 primeiro; desloca em colisão ).
PORT_RANGE = range(17832, 17836)
TIMEOUT_S = 3.0
# No Windows, conectar numa porta fechada do loopback leva ~2 s (retentativas
# de SYN); com 150 ms a sondagem das 4 portas custa no máximo ~0,6 s.
PROBE_TIMEOUT_S = 0.15
_last_port: Optional[int] = None  # última porta que respondeu (tentada primeiro)


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=PROBE_TIMEOUT_S):
            return True
    except OSError:
        return False


def _probe_ports():
    """Yields candidate (port, base_url); a última porta boa vem primeiro e as
    demais só entram se aceitarem conexão na sondagem rápida."""
    ordered = ([_last_port] if _last_port is not None else []) + [p for p in PORT_RANGE if p != _last_port]
    for port in ordered:
        if _port_open(port):
            yield port, f"http://127.0.0.1:{port}"


def _request(method: str, path: str, body: Optional[dict] = None,
             timeout: float = TIMEOUT_S):
    """Faz a requisição tentando cada porta da faixa. Retorna (port, data)
    ou (None, None) quando o app não está alcançável."""
    global _last_port
    last_error = None
    for _port, base in _probe_ports():
        url = f"{base}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                try:
                    parsed = json.loads(raw) if raw.strip() else {}
                except ValueError:
                    parsed = {}
                _last_port = _port
                return _port, parsed
        except (urllib.error.URLError, ConnectionError, OSError, TimeoutError) as exc:
            last_error = exc
            if _port == _last_port:
                _last_port = None
            continue
    if last_error is not None:
        pass  # porta fechada = app fechado; silêncio é a resposta correta
    return None, None


def wake_desktop(text: str, pcm16: Optional[bytes] = None) -> bool:
    """Acorda a conversa Live no app; text = comando dito após a wake word
    (vazio quando o operador só chamou 'hey jarvis'). O áudio, quando presente,
    é PCM16 mono/16 kHz e serve somente à biometria local."""
    body = {"text": text or ""}
    if pcm16:
        body["audio_pcm16"] = base64.b64encode(pcm16).decode("ascii")
        body["audio_rate"] = 16000
    port, _result = _request("POST", "/wake", body=body)
    return port is not None


def session_state() -> Optional[str]:
    """'live' quando a sessão de voz do app está aberta; None caso contrário
    (inclui app fechado)."""
    _port, result = _request("GET", "/state")
    if not result:
        return None
    return result.get("session") or None


def bridge_alive() -> bool:
    """True quando o canal do app responde — porta viva = backend do app vivo
    (o watchdog do sentinel gerenciado usa isto para morrer junto com o app)."""
    port, _result = _request("GET", "/state")
    return port is not None


def sentinel_running() -> bool:
    """True quando um sentinel jarvis está vivo (lock de instância única)."""
    import socket as _socket
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", SENTINEL_LOCK_PORT)) == 0


def wait_session_end(poll_interval_s: float = 2.0, first_grace_s: float = 30.0):
    """Bloqueia até a sessão Live do app encerrar (ou o app fechar).

    first_grace_s: tempo mínimo de espera antes de começar a considerar o
    estado — evita uma corrida em que o app ainda não abriu a sessão e o
    sentinel retomaria o microfone imediatamente.
    """
    deadline_for_start = time.time() + first_grace_s
    started = False
    while True:
        state = session_state()
        if state == "live":
            started = True
        elif started:
            # A sessão estava aberta e agora fechou — encerrou de verdade.
            return
        elif time.time() > deadline_for_start and state is None:
            # O app nunca abriu a sessão ( Wake não chegou? ) — solta o
            # sentinel para não travar a escuta para sempre.
            return
        time.sleep(poll_interval_s)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        print("state:", session_state())
    else:
        print("uso: python desktop_bridge.py test")
