"""Envio do WhatsApp não duplica em retry; nenhum envio real (call é simulado)."""
import argparse
import importlib.util
import json
import multiprocessing
import os
import socket
import urllib.error
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
WA = Path(os.environ.get("JARVIS_WA_PY", "")) if os.environ.get("JARVIS_WA_PY") else next(
    (p for p in (_ROOT / "hermes_integration/skills/whatsapp-evolution/wa.py",  # repositório
                 _ROOT.parent / "skills/productivity/whatsapp-evolution/wa.py") if p.is_file()),  # produção
    _ROOT / "wa.py")


@pytest.fixture
def wa(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("wa_under_test", WA)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "SENT_LOG", tmp_path / "sent.json")
    monkeypatch.setattr(module, "names", lambda: {})
    monkeypatch.setattr(module, "resolve", lambda query, directory: ["5551999990000@s.whatsapp.net"])
    return module


def args(texto="oi", repetir=False):
    return argparse.Namespace(para="Fulano", texto=texto, repetir=repetir)


def test_second_identical_send_is_refused(wa, monkeypatch):
    calls = []
    monkeypatch.setattr(wa, "call", lambda path, body: calls.append(body) or {"key": {"id": "A1"}})
    wa.cmd_enviar(args())
    with pytest.raises(wa.EvolutionError, match="já foi enviada"):
        wa.cmd_enviar(args())
    assert len(calls) == 1
    assert "oi" not in wa.SENT_LOG.read_text(encoding="utf-8")  # só hash, sem conteúdo


def test_other_text_and_explicit_repeat_are_sent(wa, monkeypatch):
    calls = []
    monkeypatch.setattr(wa, "call", lambda path, body: calls.append(body) or {"key": {"id": "A1"}})
    wa.cmd_enviar(args())
    wa.cmd_enviar(args(texto="outra"))
    wa.cmd_enviar(args(repetir=True))
    assert len(calls) == 3


def test_timeout_blocks_retry_as_uncertain(wa, monkeypatch):
    def slow(path, body):
        raise socket.timeout("read timed out")
    monkeypatch.setattr(wa, "call", slow)
    with pytest.raises(wa.EvolutionError, match="Confira a conversa"):
        wa.cmd_enviar(args())
    monkeypatch.setattr(wa, "call", lambda path, body: pytest.fail("não pode reenviar"))
    with pytest.raises(wa.EvolutionError, match="sem confirmação"):
        wa.cmd_enviar(args())


def test_definite_refusal_allows_retry(wa, monkeypatch):
    def refused(path, body):
        raise wa.EvolutionError("400") from urllib.error.HTTPError("u", 400, "bad", {}, None)
    monkeypatch.setattr(wa, "call", refused)
    with pytest.raises(wa.EvolutionError):
        wa.cmd_enviar(args())
    monkeypatch.setattr(wa, "call", lambda path, body: {"key": {"id": "B2"}})
    wa.cmd_enviar(args())


def test_server_error_is_uncertain(wa, monkeypatch):
    def broken(path, body):
        raise wa.EvolutionError("500") from urllib.error.HTTPError("u", 500, "err", {}, None)
    monkeypatch.setattr(wa, "call", broken)
    with pytest.raises(wa.EvolutionError):
        wa.cmd_enviar(args())
    with pytest.raises(wa.EvolutionError, match="sem confirmação"):
        wa.cmd_enviar(args())


def _send_in_process(module_path, log, start, called, release, outcomes, text):
    spec = importlib.util.spec_from_file_location("wa_process_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.SENT_LOG = Path(log)
    module.names = lambda: {}
    module.resolve = lambda query, directory: ["5551999990000@s.whatsapp.net"]

    def fake_call(path, body):
        called.put(text)
        if not release.wait(15):
            raise TimeoutError("fixture blocked")
        return {"key": {"id": "fixture"}}

    module.call = fake_call
    start.wait(15)
    try:
        module.cmd_enviar(args(texto=text))
        outcomes.put("sent")
    except module.EvolutionError:
        outcomes.put("blocked")


@pytest.mark.parametrize("texts", [("oi", "oi"), ("primeira", "segunda")])
def test_atomic_reservations_between_processes(tmp_path, texts):
    ctx = multiprocessing.get_context("spawn")
    start, release = ctx.Event(), ctx.Event()
    called, outcomes = ctx.Queue(), ctx.Queue()
    log = tmp_path / "sent.json"
    processes = [ctx.Process(target=_send_in_process, args=(str(WA), str(log), start, called, release, outcomes, text))
                 for text in texts]
    try:
        for process in processes:
            process.start()
        start.set()
        called.get(timeout=15)
        if texts[0] == texts[1]:
            # Enquanto o primeiro aguarda a API, o segundo já deve recusar a reserva.
            assert outcomes.get(timeout=10) == "blocked"
            release.set()
            assert outcomes.get(timeout=15) == "sent"
        else:
            called.get(timeout=10)  # redes independentes não seguram a trava
            release.set()
            assert [outcomes.get(timeout=15) for _ in processes] == ["sent", "sent"]
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
        rows = json.loads(log.read_text(encoding="utf-8"))
        assert len(rows) == len(set(texts))
        assert all(row["status"] == "sent" for row in rows.values())
        assert called.empty()
    finally:
        release.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(10)
        called.close()
        outcomes.close()


def test_corrupt_journal_blocks_sending(wa, monkeypatch):
    wa.SENT_LOG.write_text("broken", encoding="utf-8")
    monkeypatch.setattr(wa, "call", lambda *a: pytest.fail("não enviar sem proteção"))
    with pytest.raises(wa.EvolutionError, match="Registro de envios indisponível"):
        wa.cmd_enviar(args())


def test_late_result_does_not_overwrite_explicit_repeat(wa):
    first = wa._reserve_send("hash", False)
    second = wa._reserve_send("hash", True)
    wa._record_send("hash", "uncertain", reservation=second)
    wa._record_send("hash", "failed", reservation=first)
    assert wa._sent_log()["hash"]["status"] == "uncertain"
