"""Opt-in real round trip: F5 question -> local STT -> Hermes archive lookup -> F5 reply.

Run with the Hermes Python 3.11 environment and an active local dashboard URL.
Only the explicit read-only meeting query is submitted. Artifacts stay under jarvis/logs.
"""
from __future__ import annotations

import argparse
import asyncio
import audioop
import base64
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import uuid
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME = ROOT.parent
sys.path[:0] = [str(ROOT), str(HOME / "hermes-agent"), str(HOME / "plugins/gemini-live-bridge/dashboard")]
os.environ["HERMES_HOME"] = str(HOME)
import plugin_api as pa


async def verify(url: str) -> dict:
    from hermes_cli import web_server_chat
    from local_voice import speech_parts, stt, tts

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise ValueError("Use somente o dashboard local em 127.0.0.1")
    def dashboard_html():
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.read().decode()
    html = await asyncio.to_thread(dashboard_html)
    match = re.search(r'window\.__HERMES_SESSION_TOKEN__\s*=\s*("(?:\\.|[^"\\])*")', html)
    if not match:
        raise RuntimeError("Credencial local do dashboard indisponível")
    token = json.loads(match.group(1))
    web_server_chat._build_gateway_ws_url = lambda: (
        f"ws://127.0.0.1:{parsed.port}/api/ws?" + urllib.parse.urlencode({"token": token}))
    pa._local_runtime = object()  # enable the production Hermes voice-context instruction
    pa._voice.session = "local-voice-verification-" + uuid.uuid4().hex
    metrics = {}
    try:
        started = time.monotonic()
        question = await asyncio.to_thread(tts.call, {"text": "Jarvis, resuma as atas das minhas reuniões de hoje."})
        metrics["question_synthesis_s"] = round(time.monotonic() - started, 2)
        pcm, _ = audioop.ratecv(base64.b64decode(question["pcm"]), 2, 1, 24000, 16000, None)
        started = time.monotonic()
        result = await asyncio.to_thread(stt.call, {"pcm": base64.b64encode(pcm).decode()})
        metrics["transcription_s"] = round(time.monotonic() - started, 2)
        metrics["recognized_question"] = result["text"]
        (ROOT / "logs/local-voice-roundtrip.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
        if "ata" not in result["text"].casefold() or "reuni" not in result["text"].casefold():
            raise RuntimeError("A transcrição não preservou o pedido sobre atas")
        started = time.monotonic()
        answer = await pa._hermes_gateway_turn(result["text"] +
            " Faça apenas a consulta e cite os IDs consultados. Não crie nem altere arquivos, tarefas ou memórias.")
        metrics["hermes_s"] = round(time.monotonic() - started, 2)
        (ROOT / "logs/local-voice-roundtrip-answer.txt").write_text(answer, encoding="utf-8")
        started = time.monotonic()
        response = await asyncio.to_thread(tts.call, {"text": speech_parts(answer)[0]})
        metrics["answer_synthesis_s"] = round(time.monotonic() - started, 2)
        pcm = base64.b64decode(response["pcm"])
        with wave.open(str(ROOT / "logs/local-voice-roundtrip.wav"), "wb") as output:
            output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            output.writeframes(pcm)
        metrics.update(reply_chars=len(answer), audio_s=round(len(pcm) / 48000, 2), ok=True)
        (ROOT / "logs/local-voice-roundtrip.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
        return metrics
    finally:
        stt.close()
        tts.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dashboard-url", required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(verify(args.dashboard_url)), ensure_ascii=False))
