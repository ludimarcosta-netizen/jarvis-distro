import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agenda

HOJE = date(2026, 10, 7)


def test_format_eventos_horario_e_dia_todo():
    linhas = agenda.format_eventos([
        {"summary": "Reunião secretariado", "start": "2026-10-07T09:00:00-03:00", "location": "Gabinete"},
        {"summary": "Feriado", "start": "2026-10-07"},
    ])
    assert linhas == ["- 07/10 09:00 Reunião secretariado @ Gabinete", "- 07/10 dia todo Feriado"]


def test_format_eventos_vazio():
    assert agenda.format_eventos([]) == ["- Nenhum evento."]


def test_format_pendencias_ordena_vencidas_hoje_futuras_sem_prazo():
    rows = [
        {"id": 1, "descricao": "Sem prazo", "responsavel": "__JARVIS_USER__", "prazo": ""},
        {"id": 2, "descricao": "Futura", "responsavel": "", "prazo": "2026-10-10"},
        {"id": 3, "descricao": "Hoje", "responsavel": "__JARVIS_USER__", "prazo": "2026-10-07"},
        {"id": 4, "descricao": "Vencida", "responsavel": "__JARVIS_USER__", "prazo": "2026-09-30"},
        {"id": 5, "descricao": "Prazo inválido", "responsavel": "", "prazo": "tarde do dia 02"},
    ]
    assert agenda.format_pendencias(rows, HOJE) == [
        "- #4 [VENCIDA 30/09] Vencida (resp.: __JARVIS_USER__)",
        "- #3 [VENCE HOJE] Hoje (resp.: __JARVIS_USER__)",
        "- #2 [prazo 10/10] Futura",
        "- #1 [sem prazo] Sem prazo (resp.: __JARVIS_USER__)",
        "- #5 [sem prazo] Prazo inválido",
    ]


def test_format_pendencias_vazio():
    assert agenda.format_pendencias([], HOJE) == ["- Nenhuma pendência aberta."]


def test_build_report_amanha_com_agenda_indisponivel():
    def falha(inicio, fim):
        assert (inicio, fim) == (date(2026, 10, 8), date(2026, 10, 8))
        raise RuntimeError("token expirado ou revogado")

    texto = agenda.build_report(HOJE, desde=1, fetch=falha,
                                pendencias=[{"id": 9, "descricao": "Ofício", "responsavel": "", "prazo": "2026-10-08"}])
    assert texto.splitlines() == [
        "Agenda Google (08/10/2026):",
        "- Google Agenda indisponível (token expirado ou revogado); é preciso reautorizar a conta Google.",
        "",
        "Pendências abertas:",
        "- #9 [prazo 08/10] Ofício",
    ]


def test_build_report_semana():
    texto = agenda.build_report(HOJE, dias=6, fetch=lambda i, f: [], pendencias=[])
    assert texto.splitlines()[0] == "Agenda Google (07/10 a 13/10/2026):"
