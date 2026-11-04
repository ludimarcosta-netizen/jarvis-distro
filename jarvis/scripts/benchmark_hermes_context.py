"""Real read-only comparison of agent discovery versus prepared meeting context."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HOME = ROOT.parent
sys.path[:0] = [str(ROOT), str(HOME / "hermes-agent"), str(HOME / "plugins/gemini-live-bridge/dashboard")]
os.environ["HERMES_HOME"] = str(HOME)

import plugin_api as pa

QUESTION = "Jarvis resuma as atas das minhas reuniões de hoje."


async def benchmark(base: str, baseline_session: str):
    from hermes_cli import web_server_chat
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1':
        raise ValueError('Use somente o dashboard local')
    def read_html():
        with urllib.request.urlopen(base, timeout=10) as response:
            return response.read().decode()
    html = await asyncio.to_thread(read_html)
    match = re.search(r'window\.__HERMES_SESSION_TOKEN__\s*=\s*("(?:\\.|[^"\\])*")', html)
    if not match:
        raise RuntimeError('Credencial local indisponível')
    token = json.loads(match.group(1))
    url = f'ws://127.0.0.1:{parsed.port}/api/ws?' + urllib.parse.urlencode({'token': token})
    with sqlite3.connect((HOME / 'state.db').as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        baseline = conn.execute('SELECT * FROM sessions WHERE id=?', (baseline_session,)).fetchone()
        stamps = conn.execute("SELECT role,content,timestamp FROM messages WHERE session_id=? ORDER BY id",
                              (baseline_session,)).fetchall()
    if baseline is None or not stamps or not stamps[0]['content'].endswith(QUESTION):
        raise ValueError('A sessão base precisa conter a mesma pergunta de teste')
    model, provider = baseline['model'], baseline['billing_provider']
    elapsed = max(row['timestamp'] for row in stamps if row['role'] == 'assistant') - stamps[0]['timestamp']
    rows = [{'mode': 'normal', 'elapsed_s': round(elapsed, 3), 'model': model,
             'api_calls': baseline['api_call_count'], 'tool_calls': baseline['tool_call_count'],
             'session_id': baseline_session}]
    print(json.dumps(rows[0]), flush=True)
    original_rpc = pa._gateway_rpc
    async def controlled_rpc(ws, method, args, pending, deadline):
        if method == 'session.create':
            args = {**args, 'model': model, 'provider': provider, 'follow_profile_config': False}
        return await original_rpc(ws, method, args, pending, deadline)
    with patch.object(web_server_chat, '_build_gateway_ws_url', return_value=url), \
            patch.object(pa, '_local_runtime', object()), \
            patch.object(pa, '_gateway_rpc', side_effect=controlled_rpc):
        for mode in ('prepared',):
            pa._voice.session = 'context-benchmark-' + mode + '-' + uuid.uuid4().hex
            title = pa._voice_session_title()
            started = time.perf_counter()
            reply = await pa._hermes_gateway_turn(QUESTION)
            client_elapsed = time.perf_counter() - started
            # The complete event may precede the background persistence commit.
            for _ in range(50):
                with sqlite3.connect((HOME / 'state.db').as_uri() + '?mode=ro', uri=True) as conn:
                    conn.row_factory = sqlite3.Row
                    session = conn.execute('''
                        SELECT id, model, api_call_count, tool_call_count FROM sessions
                        WHERE title=? ORDER BY started_at DESC LIMIT 1
                    ''', (title,)).fetchone()
                if session is not None and session['api_call_count']:
                    break
                await asyncio.sleep(.1)
            if session is None:
                raise RuntimeError('Persistência da sessão não confirmada')
            if session['model'] != model:
                raise RuntimeError('Modelo efetivo mudou; comparação rejeitada')
            with sqlite3.connect((HOME / 'state.db').as_uri() + '?mode=ro', uri=True) as conn:
                tools = [row[0] for row in conn.execute(
                    "SELECT tool_name FROM messages WHERE session_id=? AND role='tool' ORDER BY id", (session['id'],))]
                stamps_after = conn.execute('SELECT role,content,timestamp FROM messages WHERE session_id=? ORDER BY id',
                                            (session['id'],)).fetchall()
            if stamps_after[0][1] != stamps[0]['content']:
                raise RuntimeError('Pedido original mudou; comparação rejeitada')
            elapsed = max(row[2] for row in stamps_after if row[0] == 'assistant') - stamps_after[0][2]
            row = {'mode': mode, 'elapsed_s': round(elapsed, 3), 'reply_chars': len(reply),
                   'gateway_elapsed_s': round(client_elapsed, 3),
                   'model': session['model'], 'api_calls': session['api_call_count'],
                   'tool_calls': session['tool_call_count'], 'tools': tools, 'session_id': session['id']}
            rows.append(row)
            print(json.dumps(row), flush=True)
    report = {'question': QUESTION, 'rows': rows,
              'faster_percent': round((1 - rows[1]['elapsed_s'] / rows[0]['elapsed_s']) * 100, 1)}
    folder = ROOT / 'logs/voice-latency'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'hermes-context-benchmark.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'faster_percent': report['faster_percent']}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dashboard-url', required=True)
    parser.add_argument('--baseline-session', required=True)
    args = parser.parse_args()
    asyncio.run(benchmark(args.dashboard_url, args.baseline_session))
