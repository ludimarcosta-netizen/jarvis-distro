"""Verify the authenticated production voice routes with a known local PCM question."""
from __future__ import annotations

import argparse
import audioop
import base64
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from local_voice_phrases import Bank


def verify(base: str) -> dict:
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise ValueError("Use somente o dashboard local")
    with urllib.request.urlopen(base, timeout=10) as response:
        html = response.read().decode()
    match = re.search(r'window\.__HERMES_SESSION_TOKEN__\s*=\s*("(?:\\.|[^"\\])*")', html)
    if not match:
        raise RuntimeError("Credencial local indisponível")
    token = json.loads(match.group(1))

    def call(path, body=None, timeout=30):
        request = urllib.request.Request(base + '/api/plugins/gemini-live-bridge' + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)

    if call('/config').get('engine') != 'local':
        raise RuntimeError("O backend ativo ainda não está no modo local")
    started = time.monotonic()
    try:
        result = call('/start', {}, timeout=240)
        if not result.get('ok'):
            raise RuntimeError(result.get('error', 'Não iniciou'))
        prepare_s = round(time.monotonic() - started, 2)
        print(json.dumps({'started': True, 'prepare_s': prepare_s}), flush=True)
        with wave.open(str(ROOT / 'logs/f5-ptbr-question-final.wav')) as question:
            pcm, _ = audioop.ratecv(question.readframes(question.getnframes()), 2, 1, 24000, 16000, None)
        pcm += bytes(32000)
        submitted = time.monotonic()
        for offset in range(0, len(pcm), 8192):
            result = call('/audio', {'data': base64.b64encode(pcm[offset:offset + 8192]).decode(), 'playback': False})
            if not result.get('ok'):
                raise RuntimeError('Falha no envio PCM local')
        inputs, outputs, chunks = [], [], []
        bank = Bank()
        bank.load()
        first_audio_s = first_reply_s = None
        received_bytes = 0
        cursor = {'i': 0, 'o': 0, 'a': 0}
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            result = call(f"/poll?in_offset={cursor['i']}&out_offset={cursor['o']}&a_offset={cursor['a']}")
            inputs.extend(result.get('input', []))
            outputs.extend(result.get('output', []))
            chunks.extend(result.get('audio', []))
            received_bytes += sum(len(base64.b64decode(chunk)) for chunk in result.get('audio', []))
            if chunks and first_audio_s is None:
                first_audio_s = round(time.monotonic() - submitted, 3)
            ack = bank.match(outputs[0]) if outputs else None
            if first_reply_s is None and received_bytes > (len(ack.pcm) if ack else 0):
                first_reply_s = round(time.monotonic() - submitted, 3)
                print(json.dumps({'first_reply_audio_s': first_reply_s}), flush=True)
            cursor = {'i': result['in_total'], 'o': result['out_total'], 'a': result['a_total']}
            if result.get('error'):
                raise RuntimeError(result['error'])
            if chunks and outputs and result.get('interaction') != 'IN_PROGRESS':
                break
            time.sleep(.5)
        else:
            raise RuntimeError('O fluxo não concluiu dentro do prazo')
        metrics = {'ok': True, 'elapsed_s': round(time.monotonic() - started, 2),
                   'prepare_s': prepare_s, 'first_audio_after_input_s': first_audio_s,
                   'first_reply_after_input_s': first_reply_s,
                   'recognized': ' '.join(x for x in inputs if x != '—'), 'audio_chunks': len(chunks),
                   'reply_chars': sum(map(len, outputs)), 'model': result['model']}
        (ROOT / 'logs/voice-http-verification.json').write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding='utf-8')
        (ROOT / 'logs/voice-http-answer.txt').write_text('\n'.join(outputs), encoding='utf-8')
        with wave.open(str(ROOT / 'logs/voice-http-answer.wav'), 'wb') as output:
            output.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            for chunk in chunks:
                output.writeframes(base64.b64decode(chunk))
        return metrics
    finally:
        call('/close', {}, timeout=15)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dashboard-url', required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.dashboard_url), ensure_ascii=False))
