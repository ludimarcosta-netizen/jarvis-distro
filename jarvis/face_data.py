"""Dados dos painéis da face do Jarvis: pendências abertas e saúde dos subsistemas locais.

Só leitura, exceto `finish_pendencia`, que o __JARVIS_USER__ aciona com um clique: o agente nunca conclui
pendência por conta própria (ele revisa a lista e decide).
"""
import os
import socket
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import cerebro
import storage
from capability_status import integration_checks

WEB_PORT = 17850  # página de reuniões (web_app.py)
EVOLUTION_PORT = 8090  # WhatsApp (Projects/evolution-whatsapp)
_PENDENCIA_FIELDS = ("id", "descricao", "responsavel", "prazo", "created_at", "ata_titulo")


def list_open_pendencias(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    rows = storage.list_pendencias(db_path=db_path or storage.DB_PATH)
    return [{key: row.get(key) for key in _PENDENCIA_FIELDS} for row in rows]


def finish_pendencia(pendencia_id: int, db_path: Optional[str] = None) -> bool:
    return storage.concluir_pendencia(pendencia_id, db_path=db_path or storage.DB_PATH)


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _check(check_id: str, label: str, status: str, detail: str) -> Dict[str, str]:
    return {"id": check_id, "label": label, "status": status, "detail": detail}


def system_checks(db_path: Optional[str] = None, probe: Callable[[int], bool] = port_open,
                  home: Optional[Path] = None) -> List[Dict[str, str]]:
    """status: ok | warn | off. Cada item é uma linha do painel de status."""
    web = probe(WEB_PORT)
    checks = [
        _check("web", "Página de reuniões", "warn" if web else "off",
               "Porta aberta; autenticação e API conferidas ao usar a página." if web else "desligada"),
    ]
    vault = cerebro.vault_dir()
    accessible = bool(vault and vault.is_dir() and os.access(vault, os.R_OK))
    checks.append(_check("cerebro", "Cofre do Segundo Cérebro", "ok" if accessible else "off",
                         "Cofre acessível; índice e revisões são validados nas consultas." if accessible else "cofre não configurado ou inacessível"))
    try:
        open_count = len(storage.list_pendencias(db_path=db_path or storage.DB_PATH))
        checks.append(_check("pendencias", "Pendências abertas", "warn" if open_count else "ok", str(open_count)))
    except Exception as error:  # banco ilegível: o painel mostra, não derruba a rota
        checks.append(_check("pendencias", "Pendências abertas", "off", f"erro ao ler o banco: {type(error).__name__}"))
    return checks + capability_checks(probe, home)


def capability_checks(probe: Callable[[int], bool] = port_open, home: Optional[Path] = None,
                      *, verify: Callable[[str, int], dict[str, tuple[str, str]]] = integration_checks) -> List[Dict[str, str]]:
    """Catálogo honesto: disponível (ok), depende de configuração/serviço (warn) ou indisponível (off)."""
    if home is None:
        try:
            from hermes_constants import get_hermes_home
            home = get_hermes_home()
        except ImportError:
            home = Path(__file__).resolve().parent.parent
    evolution = probe(EVOLUTION_PORT)
    integrations = verify(str(home.resolve()), int(time.monotonic() // 30))
    required = ("local_workspace.py", "local_files.py", "local_media.py", "local_memory.py", "delegation_journal.py")
    installed = all((home / "jarvis" / name).is_file() for name in required)
    return [
        _check("acoes-locais", "Ações locais", "warn" if installed else "off",
               "Componentes instalados; cada operação verifica sessão e dependências antes de executar." if installed
               else "Instalação local incompleta; confira os componentes do Jarvis."),
        _check("whatsapp", "WhatsApp (Evolution)", "warn" if evolution else "off",
               "Porta da Evolution aberta; API e conexão do celular ainda precisam ser conferidas." if evolution
               else "fora do ar: abra o Docker Desktop"),
        _check("pdf", "Prévia de PDF", *integrations["pdf"]),
        _check("spotify-faixa", "Spotify: conexão e aparelho", *integrations["spotify-faixa"]),
    ]
