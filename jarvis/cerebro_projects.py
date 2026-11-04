"""Visões de projetos derivadas das fontes locais, sem LLM ou conclusão automática."""
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import cerebro
import project_notes
from cerebro_io import atomic_write, file_lock
from cerebro_review import Reviews
from session_harvest import redact

OWNER = "<!-- CEREBRO_ESTADO_GERADO -->"


def field(meta: dict[str, str], key: str) -> str:
    raw = meta.get(key, "")
    try:
        value = json.loads(raw)
        return value if isinstance(value, str) else raw
    except ValueError:
        return raw.strip('"')


def date_key(value: str) -> float:
    try:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            # Cabeçalhos antigos do cofre usam horário de Brasília sem offset.
            moment = moment.replace(tzinfo=timezone(timedelta(hours=-3)))
        return moment.timestamp()
    except ValueError:
        return 0


def safe_path(vault: Path, relative: str) -> Path:
    path = (vault / relative).resolve()
    if not path.is_relative_to(vault.resolve()) or not relative.endswith(".md"):
        raise ValueError("Fonte fora do cofre")
    return path


def build_state(vault: Path, db: Path, name: str, aliases: list[str]) -> dict[str, Any]:
    reviews = Reviews(vault)
    latest = reviews.latest()
    views = {}

    def reviewed(relative):
        raw = safe_path(vault, relative).read_text(encoding="utf-8-sig")
        views[relative] = reviews.view(relative, raw, latest)
        return views[relative]["text"]

    def review_source(relative):
        view = views.get(relative, {})
        return view["event"]["source"] if view.get("state") == "corrected" else ""

    text = reviewed("projetos/" + name + ".md")
    if not text:
        return {"nota": None, "estado": "Nota de projeto desativada nas consultas por revisão explícita.",
                "registros_recentes": [], "consolidado": True}
    base = redact(text.split("<!-- SESSOES_START -->", 1)[0])
    body = base.split("---", 2)[-1].strip() if base.startswith("---") else base
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip() and not p.startswith(("#", "**", "<!--"))]
    objective = paragraphs[0][:600] if paragraphs else "Objetivo não documentado na nota original."
    pending = []
    original_state = ""
    for section in re.split(r"(?m)(?=^## )", base):
        if re.match(r"## Estado\b", section):
            original_state = section.split("\n", 1)[-1].strip()[:1000]
        if re.match(r"## (Próximos passos|Pendências)", section):
            pending += [line[2:].strip() for line in section.splitlines() if line.startswith("- ")]
    names = {cerebro._fold(a) for a in [name, *aliases]}
    with closing(sqlite3.connect(db)) as conn:
        # O índice v2 separa metadados dos trechos pesquisáveis; o v1 guarda o cabeçalho em chunks.
        has_meta = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
        rows = conn.execute("SELECT path,front FROM meta WHERE path LIKE 'registros/%' OR path LIKE 'sessoes/%'"
                            if has_meta else "SELECT path,body FROM chunks WHERE section='Cabeçalho' AND (path LIKE 'registros/%' OR path LIKE 'sessoes/%')").fetchall()
        docs = []
        for relative, header in rows:
            meta = cerebro._frontmatter(header)
            linked = field(meta, "projeto_nota").strip("[]")
            belongs = cerebro._fold(field(meta, "projeto")) in names or cerebro._fold(linked) == cerebro._fold(name)
            if belongs and reviewed(relative):
                stamp = field(meta, "data") or field(meta, "fim") or field(meta, "inicio")
                docs.append((date_key(stamp), relative, meta, stamp))
        docs.sort(key=lambda d: (d[0], d[1]), reverse=True)
        records, sessions, seen, synthetic = [], [], set(), 0
        limits = {"decisao": 4, "resultado": 3, "pendencia": 8, "fato": 4, "preferencia": 2}
        counts = dict.fromkeys(limits, 0)
        for _, relative, meta, stamp in docs:
            if relative.startswith("registros/"):
                kind = field(meta, "tipo")
                if kind not in limits or counts[kind] >= limits[kind]:
                    continue
                note = reviewed(relative)
                content = note.split("---", 2)[-1].strip()
                content = redact(content.split("\n", 1)[-1].strip())
                if kind in {"decisao", "fato"} and re.search(r"\bfictici[oa]s?\b", cerebro._fold(content)):
                    synthetic += 1
                    continue
                records.append({"fonte": relative, "data": stamp, "tipo": field(meta, "tipo"),
                                "texto": content[:1000], "agente": field(meta, "agente"),
                                "fonte_declarada": field(meta, "fonte"), "revisao": review_source(relative)})
                counts[kind] += 1
            elif len(sessions) < 3:
                identity = field(meta, "ferramenta") + ":" + (field(meta, "sessao_id") or relative)
                if identity in seen:
                    continue
                seen.add(identity)
                chunks = conn.execute("SELECT section,body FROM chunks WHERE path=? AND section IN ('O que foi feito','Decisões','Pendências') ORDER BY rowid", (relative,)).fetchall()
                excerpt = redact("\n".join(b for _, b in chunks))[:1400]
                sessions.append({"fonte": relative, "data": stamp, "relato": excerpt,
                                 "revisao": review_source(relative), "verificacao": "Relato de sessão; não validado por esta consolidação."})
    decisions = [r for r in records if r["tipo"] == "decisao"][:4]
    results = [r for r in records if r["tipo"] == "resultado"][:3]
    tasks = [r for r in records if r["tipo"] == "pendencia"][:8]
    def source_label(row):
        return row["fonte"] + ("; revisão: " + row["revisao"] if row.get("revisao") else "")

    lines = [f"## Objetivo documentado\n{objective}",
             "## Último resultado registrado\n" + (f"{results[0]['data']} — {results[0]['texto']} (fonte: {source_label(results[0])})" if results else "Sem resultado estruturado. Consulte os relatos, sem presumir sucesso atual.")]
    if review_source("projetos/" + name + ".md"):
        lines.append("## Revisão da nota original\n" + review_source("projetos/" + name + ".md"))
    if decisions:
        lines.append("## Decisões registradas\n" + "\n".join(f"- {r['data']} — {r['texto'][:350]} (fonte: {source_label(r)})" for r in decisions[:2]))
    lines.append("## Pendências documentadas (não concluídas automaticamente)\n" + (
        "\n".join(f"- {r['texto'][:250]} (fonte: {source_label(r)})" for r in tasks[:3]) if tasks else "Sem pendência estruturada; isso não significa que tudo esteja concluído."))
    if pending:
        lines.append("## Próximos passos da nota original (podem estar desatualizados)\n" + "\n".join(f"- {p[:250]}" for p in pending[:3]))
    if original_state:
        lines.append("## Estado da nota original (referência histórica, não validada agora)\n" + original_state[:500])
    return {"nota": "projetos/" + name + ".md", "consolidado": True, "objetivo": objective,
            "estado": "\n\n".join(lines)[:4200], "decisoes": decisions, "resultados": results,
            "pendencias": tasks, "pendencias_da_nota_original": pending[:8],
            "estado_da_nota_original": original_state,
            "sessoes_recentes": sessions, "registros_recentes": [
                {"fonte": r["fonte"], "registro": r["texto"]} for r in records[:4]],
            "data_referencia": docs[0][3] if docs else "Não há registro datado vinculado",
            "registros_ficticios_separados": synthetic,
            "aviso": "Visão derivada e limitada aos registros recentes; não é lista completa. Fonte declarada não equivale a validação independente. Relatos e passos antigos não comprovam estado atual. Conclusão de pendências cabe a __JARVIS_USER__."}


def publish_states(vault: Path, db: Path) -> dict[str, int]:
    index = project_notes.project_index(vault)
    changed, preserved = 0, 0
    for name, meta in index.items():
        view = build_state(vault, db, name, meta["aliases"])
        if not view.get("nota"):
            preserved += 1
            continue
        path = safe_path(vault, "projetos/estados/Estado — " + name + ".md")
        parts = [f"---\ntipo: estado-projeto\nprojeto: {json.dumps(name, ensure_ascii=False)}\ndata_referencia: {json.dumps(view['data_referencia'], ensure_ascii=False)}\n---",
                 OWNER, f"# Estado documentado — {name}",
                 f"Nota original: [[{name}]].\n\n{view['aviso']}", view["estado"],
                 "## Sessões recentes (relatos não verificados)"]
        for session in view["sessoes_recentes"]:
            source = Path(session["fonte"]).stem
            parts.append(f"### {session['data']} — [[{source}]]\n{session['relato']}")
        if not view["sessoes_recentes"]:
            parts.append("Nenhuma sessão vinculada identificada.")
        if view["registros_ficticios_separados"]:
            parts.append(f"Registros fictícios/testes preservados nas fontes: {view['registros_ficticios_separados']}. Não foram tratados como decisões operacionais.")
        new = "\n\n".join(parts) + "\n"
        with file_lock(path):
            old = path.read_text(encoding="utf-8") if path.exists() else ""
            if old and OWNER not in old:
                preserved += 1
                continue
            if new != old:
                atomic_write(path, new)
                changed += 1
    return {"projetos": len(index), "atualizados": changed, "arquivos_nao_gerados_preservados": preserved}
