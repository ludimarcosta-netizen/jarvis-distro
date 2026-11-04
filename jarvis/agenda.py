"""
Compromissos do dia em um comando: eventos da Google Agenda + pendências abertas.
Script leve (não importa o motor de áudio do jarvis.py) para o Jarvis responder
"quais os compromissos de hoje?" com uma única chamada.

Uso: python agenda.py [--desde N] [--dias M]
  --desde N: começa N dias depois de hoje (1 = amanhã); --dias M: inclui mais M dias (6 = semana).

Também é importado (só biblioteca padrão no topo) pelo atalho sem modelo do chat do Hermes e pela
voz do Jarvis, que chamam ``consultar()``: ela roda este script no venv do Jarvis e devolve o texto.
"""
import argparse
import json
import os
import subprocess
from datetime import date, datetime, timedelta
from typing import Any

HERMES_HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERMES_PYTHON = os.path.join(HERMES_HOME, "hermes-agent", "venv", "Scripts", "python.exe")
JARVIS_PYTHON = os.path.join(HERMES_HOME, "jarvis", ".venv311", "Scripts", "python.exe")
GOOGLE_API = os.path.join(HERMES_HOME, "skills", "productivity", "google-workspace", "scripts", "google_api.py")
CALENDAR_TIMEOUT_S = 20
CONSULTA_TIMEOUT_S = 40


def fetch_calendar(inicio: date, fim: date) -> list[dict[str, Any]]:
    """Eventos da Google Agenda entre inicio e fim (inclusive), horário local. Levanta RuntimeError se falhar."""
    tz = datetime.now().astimezone().strftime("%z")
    tz = f"{tz[:3]}:{tz[3:]}"
    cmd = [HERMES_PYTHON, GOOGLE_API, "calendar", "list",
           "--start", f"{inicio.isoformat()}T00:00:00{tz}", "--end", f"{fim.isoformat()}T23:59:59{tz}"]
    env = {**os.environ, "HERMES_HOME": HERMES_HOME, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=CALENDAR_TIMEOUT_S, env=env, check=False)
    if proc.returncode != 0:
        if "invalid_grant" in proc.stderr:
            raise RuntimeError("token expirado ou revogado")
        raise RuntimeError((proc.stderr.strip().splitlines() or ["erro desconhecido"])[-1])
    return json.loads(proc.stdout or "[]")


def _hora(texto: str) -> str:
    """'2026-10-07T09:00:00-03:00' -> '07/10 09:00'; data pura (dia inteiro) -> '07/10 dia todo'."""
    if not texto:
        return "?"
    if "T" not in texto:
        return f"{texto[8:10]}/{texto[5:7]} dia todo"
    return f"{texto[8:10]}/{texto[5:7]} {texto[11:16]}"


def format_eventos(eventos: list[dict[str, Any]]) -> list[str]:
    linhas = []
    for ev in eventos:
        linha = f"- {_hora(ev.get('start'))} {ev.get('summary') or '(sem título)'}"
        if ev.get("location"):
            linha += f" @ {ev['location']}"
        linhas.append(linha)
    return linhas or ["- Nenhum evento."]


def format_pendencias(rows: list[dict[str, Any]], hoje: date) -> list[str]:
    """Pendências abertas: vencidas e de hoje primeiro, depois com prazo futuro, depois sem prazo."""
    def prazo_de(row: dict[str, Any]) -> date | None:
        try:
            return date.fromisoformat(str(row.get("prazo") or "")[:10])
        except ValueError:
            return None

    def rotulo(p: date | None) -> str:
        if p is None:
            return "sem prazo"
        if p < hoje:
            return f"VENCIDA {p:%d/%m}"
        if p == hoje:
            return "VENCE HOJE"
        return f"prazo {p:%d/%m}"

    ordenadas = sorted(rows, key=lambda r: (prazo_de(r) is None, prazo_de(r) or hoje, r["id"]))
    linhas = [f"- #{r['id']} [{rotulo(prazo_de(r))}] {r['descricao']}"
              + (f" (resp.: {r['responsavel']})" if r.get("responsavel") else "")
              for r in ordenadas]
    return linhas or ["- Nenhuma pendência aberta."]


def build_report(hoje: date, desde: int = 0, dias: int = 0, fetch=fetch_calendar,
                 pendencias: list[dict[str, Any]] | None = None) -> str:
    inicio = hoje + timedelta(days=max(desde, 0))
    fim = inicio + timedelta(days=max(dias, 0))
    periodo = f"{inicio:%d/%m/%Y}" if fim == inicio else f"{inicio:%d/%m} a {fim:%d/%m/%Y}"
    linhas = [f"Agenda Google ({periodo}):"]
    try:
        linhas += format_eventos(fetch(inicio, fim))
    except (RuntimeError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
        linhas.append(f"- Google Agenda indisponível ({exc}); é preciso reautorizar a conta Google.")
    if pendencias is None:
        from storage import list_pendencias  # só existe no venv/pasta do Jarvis
        pendencias = list_pendencias()
    linhas += ["", "Pendências abertas:", *format_pendencias(pendencias, hoje)]
    return "\n".join(linhas)


def consultar(desde: int = 0, dias: int = 0) -> str:
    """Roda este script no venv do Jarvis e devolve o texto. Levanta RuntimeError se falhar."""
    here = os.path.dirname(os.path.abspath(__file__))
    proc = subprocess.run([JARVIS_PYTHON, os.path.join(here, "agenda.py"), "--desde", str(desde), "--dias", str(dias)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=CONSULTA_TIMEOUT_S, check=False, cwd=here,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError((proc.stderr.strip().splitlines() or ["sem saída"])[-1])
    return proc.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Compromissos: Google Agenda + pendências do Jarvis")
    parser.add_argument("--desde", type=int, default=0, help="começa N dias depois de hoje (1 = amanhã)")
    parser.add_argument("--dias", type=int, default=0, help="dias a mais no período (6 = uma semana)")
    args = parser.parse_args()
    hoje = date.today()  # noqa: DTZ011 - data local é a desejada
    print(build_report(hoje, args.desde, args.dias))


if __name__ == "__main__":
    main()
