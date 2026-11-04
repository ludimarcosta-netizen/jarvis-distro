"""Ledger local de orquestração: quem delegou o quê, a qual executor e modelo, com que custo e resultado.

Métricas desconhecidas ficam nulas, nunca zero. Custo é a estimativa informada pelo CLI, não a fatura da
assinatura. Tokens de entrada são o total enviado ao modelo; `tokens_cache` é a parte lida do cache.
A tarefa completa só passa pelo stdin do executor; o banco guarda um resumo mascarado.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

import psutil

from cerebro_review import safe_text

ROOT = Path(__file__).resolve().parent
DB = ROOT / "database" / "orquestracao.db"
HERMES_STATE = ROOT.parent / "state.db"
EXECUTORES = {"claude", "codex", "antigravity"}
MODOS = {"leitura", "escrita"}
ORIGENS = {"jarvis", "claude", "codex", "antigravity", "manual"}
VALIDACOES = {"", "contem", "exato", "json"}
ESFORCOS = {"", "low", "medium", "high", "xhigh", "max"}
TERMINAIS = {"concluido", "falhou", "validacao_falhou", "tempo_esgotado", "incerto"}
MAX_SIMULTANEAS = 2
MAX_TAREFA = 100_000
MAX_TAREFA_ARGUMENTO = 30_000  # o Antigravity recebe a tarefa como argumento (limite do Windows: 32 767)
# Um executor aberto pelo ledger herda esta variável e não pode delegar de novo (evita cascata).
PROFUNDIDADE_ENV = "JARVIS_ORQUESTRACAO_PROFUNDIDADE"
NOME_MODELO = re.compile(r"^[\w.:\[\]/-]{1,80}$")


def connect(db: Path = DB) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS execucoes(
            id TEXT PRIMARY KEY, criado REAL NOT NULL, atualizado REAL NOT NULL,
            origem TEXT NOT NULL, executor TEXT NOT NULL, modo TEXT NOT NULL,
            modelo_pedido TEXT NOT NULL DEFAULT '', modelo_observado TEXT, esforco TEXT NOT NULL DEFAULT '',
            pasta TEXT NOT NULL, tarefa TEXT NOT NULL, status TEXT NOT NULL, pid INTEGER,
            duracao_s REAL, tokens_entrada INTEGER, tokens_saida INTEGER, tokens_cache INTEGER,
            custo_estimado_usd REAL, validacao TEXT NOT NULL DEFAULT '', esperado TEXT NOT NULL DEFAULT '',
            validacao_ok INTEGER, resposta TEXT NOT NULL DEFAULT '', erro TEXT NOT NULL DEFAULT '',
            sessao TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS execucoes_criado ON execucoes(criado);
        PRAGMA user_version=1;
    """)
    return conn


def comando(executor: str, modo: str, modelo: str, esforco: str, tarefa: str) -> list[str]:
    """Linha de comando do executor. Claude e Codex leem a tarefa do stdin: são shims .cmd e um
    argumento livre passaria pelo cmd.exe."""
    escrita = modo == "escrita"
    if executor == "claude":
        args = [shutil.which("claude") or "claude", "-p", "--output-format", "json"]
        args += ["--permission-mode", "acceptEdits"] if escrita else []
        args += ["--model", modelo] if modelo else []
        return args + (["--effort", esforco] if esforco else [])
    if executor == "codex":
        args = [shutil.which("codex") or "codex", "exec", "--json",
                "--sandbox", "workspace-write" if escrita else "read-only"]
        args += [] if escrita else ["--skip-git-repo-check"]
        args += ["-m", modelo] if modelo else []
        args += ["-c", f'model_reasoning_effort="{esforco}"'] if esforco else []
        return args + ["-"]
    args = [shutil.which("agy") or "agy", "-p", tarefa, "--output-format", "json",
            "--mode", "accept-edits" if escrita else "plan"]
    args += ["--model", modelo] if modelo else []
    return args + (["--effort", esforco] if esforco else [])


def modelo_codex(thread: str, home: Path | None = None) -> str | None:
    """O `codex exec --json` não informa o modelo; a sessão gravada pelo próprio Codex informa."""
    if not re.fullmatch(r"[0-9a-f-]{36}", thread or ""):
        return None
    sessions = (home or Path.home()) / ".codex" / "sessions"
    dias = sorted((d for d in sessions.glob("*/*/*") if d.is_dir()), reverse=True)[:3]
    for dia in dias:
        for arquivo in dia.glob(f"rollout-*-{thread}.jsonl"):
            with arquivo.open(encoding="utf-8", errors="replace") as linhas:
                for linha in linhas:
                    if '"turn_context"' in linha:
                        return json.loads(linha).get("payload", {}).get("model")
    return None


def interpretar(executor: str, saida: str) -> dict[str, Any]:
    """Resposta e métricas a partir da saída JSON do executor; o que não vier fica None."""
    dados: dict[str, Any] = {"resposta": "", "modelo_observado": None, "tokens_entrada": None,
                             "tokens_saida": None, "tokens_cache": None, "custo_estimado_usd": None,
                             "sessao": "", "erro_executor": ""}
    if executor == "codex":
        textos, uso = [], {}
        for linha in saida.splitlines():
            try:
                evento = json.loads(linha)
            except ValueError:
                continue
            if evento.get("type") == "thread.started":
                dados["sessao"] = evento.get("thread_id", "")
            elif evento.get("type") == "item.completed" and evento.get("item", {}).get("type") == "agent_message":
                textos.append(evento["item"].get("text", ""))
            elif evento.get("type") == "turn.completed":
                for chave, valor in evento.get("usage", {}).items():
                    uso[chave] = uso.get(chave, 0) + valor
            elif evento.get("type") in {"turn.failed", "error"}:
                dados["erro_executor"] = json.dumps(evento.get("error") or evento, ensure_ascii=False)[:500]
        dados["resposta"] = textos[-1] if textos else ""
        if uso:
            dados.update(tokens_entrada=uso.get("input_tokens"), tokens_saida=uso.get("output_tokens"),
                         tokens_cache=uso.get("cached_input_tokens"))
        dados["modelo_observado"] = modelo_codex(dados["sessao"])
        return dados
    linhas = [linha for linha in saida.strip().splitlines() if linha.startswith("{")]
    resultado = json.loads(linhas[-1]) if linhas else {}
    uso = resultado.get("usage") or {}
    if executor == "claude":
        cache = (uso.get("cache_read_input_tokens") or 0)
        criacao = (uso.get("cache_creation_input_tokens") or 0)
        dados.update(resposta=resultado.get("result", ""), sessao=resultado.get("session_id", ""),
                     modelo_observado=",".join(resultado.get("modelUsage") or {}) or None,
                     custo_estimado_usd=resultado.get("total_cost_usd"),
                     tokens_saida=uso.get("output_tokens"), tokens_cache=uso.get("cache_read_input_tokens"))
        if "input_tokens" in uso:
            dados["tokens_entrada"] = uso["input_tokens"] + cache + criacao
        if resultado.get("is_error") or not resultado:
            dados["erro_executor"] = str(resultado.get("result") or resultado.get("subtype") or "sem resultado JSON")[:500]
        return dados
    dados.update(resposta=resultado.get("response", ""), sessao=resultado.get("conversation_id", ""),
                 tokens_entrada=uso.get("input_tokens"), tokens_saida=uso.get("output_tokens"),
                 tokens_cache=uso.get("cache_read_tokens"))
    if resultado.get("status") != "SUCCESS":
        dados["erro_executor"] = str(resultado.get("status") or "sem resultado JSON")[:500]
    return dados


def validar(tipo: str, esperado: str, resposta: str) -> bool | None:
    if not tipo:
        return None
    if tipo == "contem":
        return esperado in resposta
    if tipo == "exato":
        return resposta.strip() == esperado.strip()
    try:
        json.loads(resposta)
        return True
    except ValueError:
        return False


def iniciar(executor: str, tarefa: str, pasta: str, *, origem: str, modo: str = "leitura", modelo: str = "",
            esforco: str = "", validacao: str = "", esperado: str = "", timeout_s: int = 1800,
            aguardar: bool = False, db: Path = DB) -> dict[str, Any]:
    if os.environ.get(PROFUNDIDADE_ENV):
        raise ValueError("Este executor já foi aberto pelo orquestrador e não pode delegar de novo")
    if executor not in EXECUTORES or modo not in MODOS or origem not in ORIGENS:
        raise ValueError("Executor, modo ou origem inválido")
    if validacao not in VALIDACOES or esforco not in ESFORCOS or (modelo and not NOME_MODELO.match(modelo)):
        raise ValueError("Validação, esforço ou modelo inválido")
    if validacao in {"contem", "exato"} and not esperado:
        raise ValueError("Informe o valor esperado da validação")
    limite = MAX_TAREFA_ARGUMENTO if executor == "antigravity" else MAX_TAREFA
    if not isinstance(tarefa, str) or not tarefa.strip() or len(tarefa) > limite:
        raise ValueError(f"A tarefa deve ter entre 1 e {limite} caracteres")
    if type(timeout_s) is not int or not 30 <= timeout_s <= 7200:
        raise ValueError("Tempo limite deve estar entre 30 e 7200 segundos")
    diretorio = Path(pasta).expanduser()
    if not diretorio.is_absolute() or not diretorio.is_dir():
        raise ValueError("Informe a pasta de trabalho como caminho absoluto existente")
    if modo == "escrita" and executor == "codex" and not (diretorio / ".git").exists():
        raise ValueError("O Codex só escreve dentro de um repositório git")
    atualizar_orfas(db)
    execucao, agora = uuid.uuid4().hex, time.time()
    with closing(connect(db)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT count(*) FROM execucoes WHERE status='rodando'").fetchone()[0] >= MAX_SIMULTANEAS:
            raise ValueError(f"Já há {MAX_SIMULTANEAS} execuções em andamento; aguarde uma terminar")
        conn.execute("""INSERT INTO execucoes(id,criado,atualizado,origem,executor,modo,modelo_pedido,esforco,
            pasta,tarefa,status,validacao,esperado) VALUES(?,?,?,?,?,?,?,?,?,?,'rodando',?,?)""",
            (execucao, agora, agora, origem, executor, modo, modelo, esforco, str(diretorio),
             safe_text(tarefa)[:2000], validacao, safe_text(esperado)[:500]))
    if aguardar:
        executar(execucao, tarefa, timeout_s, db)
        return consultar(execucao, db)
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
             if os.name == "nt" else 0)
    worker = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "_executar", execucao,
                               "--timeout", str(timeout_s), "--db", str(db)],
                              stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              creationflags=flags, start_new_session=os.name != "nt", cwd=ROOT)
    with closing(connect(db)) as conn, conn:
        conn.execute("UPDATE execucoes SET pid=? WHERE id=?", (worker.pid, execucao))
    worker.stdin.write(tarefa.encode("utf-8"))
    worker.stdin.close()
    return {"id": execucao, "status": "rodando",
            "aviso": "Acompanhe com status; o resultado não é verificado além da validação pedida."}


def executar(execucao: str, tarefa: str, timeout_s: int, db: Path = DB) -> None:
    with closing(connect(db)) as conn:
        linha = conn.execute("SELECT * FROM execucoes WHERE id=? AND status='rodando'", (execucao,)).fetchone()
    if linha is None:
        return
    with closing(connect(db)) as conn, conn:
        conn.execute("UPDATE execucoes SET pid=? WHERE id=?", (os.getpid(), execucao))
    args = comando(linha["executor"], linha["modo"], linha["modelo_pedido"], linha["esforco"], tarefa)
    ambiente = {**os.environ, PROFUNDIDADE_ENV: "1"}
    inicio = time.monotonic()
    status, erro, dados = "concluido", "", {}
    try:
        processo = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", errors="replace", cwd=linha["pasta"], env=ambiente,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            saida, erros = processo.communicate(None if linha["executor"] == "antigravity" else tarefa, timeout_s)
        except subprocess.TimeoutExpired:
            encerrar_arvore(processo.pid)
            processo.communicate()
            raise
        try:
            dados = interpretar(linha["executor"], saida)
        except (ValueError, AttributeError, OSError):
            dados = {"erro_executor": "saída do executor ilegível"}
        erro = dados.pop("erro_executor", "")
        if processo.returncode != 0 or erro:
            status = "falhou"
            # Código de saída ≠ 0: o stderr explica melhor que a falta de JSON; sem stderr fica o erro do JSON.
            stderr = erros.strip().splitlines()[-1][:500] if processo.returncode != 0 and erros.strip() else ""
            erro = stderr or erro or f"código {processo.returncode}"
    except subprocess.TimeoutExpired:
        status, erro = "tempo_esgotado", f"Sem resposta em {timeout_s} s; o executor foi encerrado."
    except OSError as exc:
        status, erro = "falhou", f"Executor indisponível ({type(exc).__name__})"
    resposta = dados.get("resposta") or ""
    ok = validar(linha["validacao"], linha["esperado"], resposta) if status == "concluido" else None
    if ok is False:
        status = "validacao_falhou"
    campos = {k: dados.get(k) for k in ("modelo_observado", "tokens_entrada", "tokens_saida", "tokens_cache",
                                         "custo_estimado_usd")}
    with closing(connect(db)) as conn, conn:
        conn.execute("""UPDATE execucoes SET status=?,atualizado=?,duracao_s=?,modelo_observado=?,tokens_entrada=?,
            tokens_saida=?,tokens_cache=?,custo_estimado_usd=?,validacao_ok=?,resposta=?,erro=?,sessao=?
            WHERE id=? AND status='rodando'""",
            (status, time.time(), round(time.monotonic() - inicio, 1), campos["modelo_observado"],
             campos["tokens_entrada"], campos["tokens_saida"], campos["tokens_cache"], campos["custo_estimado_usd"],
             None if ok is None else int(ok), safe_text(resposta)[:20000], safe_text(erro), dados.get("sessao") or "",
             execucao))


def encerrar_arvore(pid: int) -> None:
    """Claude e Codex rodam via cmd.exe → node; matar só o cmd deixaria o node trabalhando."""
    try:
        raiz = psutil.Process(pid)
        processos = raiz.children(recursive=True) + [raiz]
    except psutil.Error:
        return
    for processo in processos:
        try:
            processo.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(processos, timeout=10)


def atualizar_orfas(db: Path = DB) -> None:
    """Execução cujo processo sumiu (reinício, queda) vira incerta; nunca é repetida sozinha."""
    with closing(connect(db)) as conn, conn:
        for linha in conn.execute("SELECT id,pid,criado FROM execucoes WHERE status='rodando'").fetchall():
            vivo = False
            if linha["pid"]:
                try:
                    # PID reaproveitado por outro programa nasceu depois do worker.
                    vivo = psutil.Process(linha["pid"]).create_time() <= linha["criado"] + 120
                except psutil.Error:
                    vivo = False
            elif time.time() - linha["criado"] < 60:
                vivo = True  # recém-criada, o worker ainda está subindo
            if not vivo:
                conn.execute("""UPDATE execucoes SET status='incerto',atualizado=?,erro=? WHERE id=? AND status='rodando'""",
                             (time.time(), "O processo terminou sem registrar o resultado. Confira a pasta antes de repetir.",
                              linha["id"]))


def consultar(execucao: str, db: Path = DB) -> dict[str, Any]:
    if not isinstance(execucao, str) or not re.fullmatch(r"[0-9a-f]{32}", execucao):
        raise ValueError("ID de execução inválido")
    atualizar_orfas(db)
    with closing(connect(db)) as conn:
        linha = conn.execute("SELECT * FROM execucoes WHERE id=?", (execucao,)).fetchone()
    if linha is None:
        raise ValueError("Execução não encontrada")
    return {k: linha[k] for k in linha.keys() if k not in {"pid", "esperado"}}  # noqa: SIM118 (Row itera valores)


def listar(limite: int = 10, db: Path = DB) -> list[dict[str, Any]]:
    if type(limite) is not int or not 1 <= limite <= 50:
        raise ValueError("Limite deve estar entre 1 e 50")
    atualizar_orfas(db)
    with closing(connect(db)) as conn:
        linhas = conn.execute("""SELECT id,criado,origem,executor,modo,modelo_pedido,modelo_observado,status,
            duracao_s,custo_estimado_usd,substr(tarefa,1,160) AS tarefa FROM execucoes
            ORDER BY criado DESC LIMIT ?""", (limite,)).fetchall()
    return [dict(linha) for linha in linhas]


def relatorio(dias: int = 7, db: Path = DB, hermes_state: Path = HERMES_STATE) -> dict[str, Any]:
    if type(dias) is not int or not 1 <= dias <= 365:
        raise ValueError("Período deve estar entre 1 e 365 dias")
    desde = time.time() - dias * 86400
    atualizar_orfas(db)
    with closing(connect(db)) as conn:
        grupos = conn.execute("""SELECT origem,executor,COALESCE(modelo_observado,NULLIF(modelo_pedido,''),'padrão do CLI') AS modelo,
            count(*) AS execucoes, sum(status='concluido') AS concluidas, sum(status='validacao_falhou') AS validacao_falhou,
            sum(status IN ('falhou','tempo_esgotado','incerto')) AS falhas, sum(status='rodando') AS rodando,
            round(avg(duracao_s),1) AS duracao_media_s, sum(tokens_entrada) AS tokens_entrada,
            sum(tokens_cache) AS tokens_cache, sum(tokens_saida) AS tokens_saida,
            round(sum(custo_estimado_usd),4) AS custo_estimado_usd, sum(custo_estimado_usd IS NULL) AS sem_custo
            FROM execucoes WHERE criado>=? GROUP BY 1,2,3 ORDER BY execucoes DESC""", (desde,)).fetchall()
    subagentes: list[dict[str, Any]] | str = []
    try:
        with closing(sqlite3.connect(f"file:{hermes_state.as_posix()}?mode=ro", uri=True, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            subagentes = [dict(r) for r in conn.execute("""SELECT model AS modelo, billing_provider AS provedor,
                count(*) AS execucoes, sum(end_reason='agent_close') AS encerradas_normalmente,
                round(avg(ended_at-started_at),1) AS duracao_media_s, sum(input_tokens+cache_read_tokens) AS tokens_entrada,
                sum(cache_read_tokens) AS tokens_cache, sum(output_tokens) AS tokens_saida,
                round(sum(estimated_cost_usd),4) AS custo_estimado_usd, group_concat(DISTINCT cost_status) AS situacao_custo
                FROM sessions WHERE source='subagent' AND started_at>=? GROUP BY 1,2 ORDER BY execucoes DESC""", (desde,))]
    except sqlite3.Error:
        subagentes = "Banco do Hermes indisponível"
    return {"periodo_dias": dias, "ledger": [dict(g) for g in grupos], "subagentes_hermes": subagentes,
            "aviso": ("Custos são estimativas dos clientes (Claude informa preço de tabela; Codex e Antigravity não "
                      "informam). 'included' no Hermes = coberto pela assinatura. Nulo = desconhecido, não zero. "
                      "Sem comparação de qualidade, isto não prova economia.")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="acao", required=True)
    run = sub.add_parser("run", help="Delegar uma tarefa (a tarefa vem de --tarefa ou do stdin)")
    run.add_argument("--executor", required=True, choices=sorted(EXECUTORES))
    run.add_argument("--pasta", required=True)
    run.add_argument("--tarefa", default="")
    run.add_argument("--origem", default="manual", choices=sorted(ORIGENS))
    run.add_argument("--modo", default="leitura", choices=sorted(MODOS))
    run.add_argument("--modelo", default="")
    run.add_argument("--esforco", default="", choices=sorted(ESFORCOS))
    run.add_argument("--validacao", default="", choices=sorted(VALIDACOES))
    run.add_argument("--esperado", default="")
    run.add_argument("--timeout", type=int, default=1800)
    run.add_argument("--aguardar", action="store_true", help="Esperar o fim em vez de rodar em segundo plano")
    status = sub.add_parser("status")
    status.add_argument("id")
    lista = sub.add_parser("listar")
    lista.add_argument("--limite", type=int, default=10)
    rel = sub.add_parser("relatorio")
    rel.add_argument("--dias", type=int, default=7)
    worker = sub.add_parser("_executar")
    worker.add_argument("id")
    worker.add_argument("--timeout", type=int, required=True)
    worker.add_argument("--db", type=Path, default=DB)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    try:
        if args.acao == "_executar":
            executar(args.id, sys.stdin.read(MAX_TAREFA + 1), args.timeout, args.db)
            return
        if args.acao == "run":
            tarefa = args.tarefa or sys.stdin.read(MAX_TAREFA + 1)
            resultado: Any = iniciar(args.executor, tarefa, args.pasta, origem=args.origem, modo=args.modo,
                                     modelo=args.modelo, esforco=args.esforco, validacao=args.validacao,
                                     esperado=args.esperado, timeout_s=args.timeout, aguardar=args.aguardar)
        elif args.acao == "status":
            resultado = consultar(args.id)
        elif args.acao == "listar":
            resultado = listar(args.limite)
        else:
            resultado = relatorio(args.dias)
        print(json.dumps(resultado, ensure_ascii=False, indent=1))
    except ValueError as exc:
        parser.exit(2, f"Orquestração: {exc}\n")
    except (OSError, sqlite3.Error) as exc:
        parser.exit(1, f"Orquestração: armazenamento indisponível ({type(exc).__name__})\n")


if __name__ == "__main__":
    main()
