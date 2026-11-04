"""Notas de projeto no Cerebro (`projetos/`): criação a partir do código e vínculo com as sessões.

- `build`: lê README, estrutura, pilha e git de cada projeto e pede ao Gemini uma visão geral; cria
  `projetos/<Nome>.md` (ou preenche a nota vazia que o senhor criou). Nota já preenchida não é reescrita.
- `resolve`: dado o nome da pasta da sessão e o texto do resumo, devolve o projeto (`aliases` e
  `palavras_chave` do cabeçalho de cada nota de projeto).
- `refresh_session_blocks`: mantém, em cada nota de projeto, o bloco gerado "Sessões" entre marcadores,
  sem tocar no resto da nota.

Uso: python project_notes.py build [--only Nome] | link | blocks
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import cerebro

HOME = Path.home()
PREFEITURA_NOTE = "Prefeitura Municipal de __JARVIS_CIDADE__"
START, END = "<!-- SESSOES_START -->", "<!-- SESSOES_END -->"
README_CAP = 3500
PROJECTS: List[Dict[str, Any]] = [
    {"nome": "Jarvis", "paths": [HOME / "AppData/Local/hermes/jarvis", HOME / "Projects/Jarvis"],
     "aliases": ["jarvis", "hermes", "hermes-agent", "video-use", "jarvis-teste-time", "vigia"],
     "palavras_chave": ["jarvis", "hermes", "vigia", "segundo cérebro", "cerebro", "evolution api"],
     "extra": [HOME / ".claude/projects/C--Users-__JARVIS_USER__-AppData-Local-hermes/memory/MEMORY.md"]},
]


# ── vínculo sessão → projeto ───────────────────────────────────────────────────────

def _list_field(front: str, key: str) -> List[str]:
    match = re.search(rf"^{key}:\s*\[(.*?)\]\s*$", front, re.M)
    return [x.strip().strip("'\"") for x in match.group(1).split(",") if x.strip()] if match else []


def project_index(vault: Path) -> Dict[str, Dict[str, List[str]]]:
    """{nome da nota: {aliases, palavras}} lido do cabeçalho de cada nota de `projetos/`."""
    index = {}
    for path in (vault / "projetos").glob("*.md"):
        text = path.read_text(encoding="utf-8", errors="replace")
        front = text.split("---", 2)[1] if text.startswith("---") else ""
        index[path.stem] = {"aliases": [path.stem, *_list_field(front, "aliases")],
                            "palavras": _list_field(front, "palavras_chave")}
    return index


def resolve(vault: Path, folder_name: str, text: str = "", *,
            index: dict[str, dict[str, list[str]]] | None = None) -> Optional[str]:
    """Nota de projeto da sessão: primeiro pelo nome da pasta de trabalho; senão pelas palavras-chave do resumo."""
    index = project_index(vault) if index is None else index
    folder = (folder_name or "").casefold()
    for name, data in index.items():
        if folder and folder in {a.casefold() for a in data["aliases"]}:
            return name
    haystack = (text or "").casefold()
    scored = [(sum(haystack.count(w.casefold()) for w in data["palavras"]), name) for name, data in index.items()]
    best = max(scored, default=(0, None))
    return best[1] if best[0] >= 2 else None


def project_has_org(vault: Path, name: Optional[str]) -> bool:
    """A nota do projeto está ligada a uma organização (ex.: Prefeitura): as sessões dele herdam o vínculo."""
    path = vault / "projetos" / f"{name}.md" if name else None
    return bool(path and path.exists() and "organizacao:" in _front(path.read_text(encoding="utf-8", errors="replace")))


def propagate_org(vault: Path) -> int:
    """Liga à Prefeitura as sessões já geradas dos projetos ligados a ela."""
    org = f"[[{PREFEITURA_NOTE}]]"
    changed = 0
    for path in (vault / "sessoes").glob("*.md"):
        text = path.read_text(encoding="utf-8")
        link = re.search(r'^projeto_nota:\s*"\[\[(.+?)\]\]"', _front(text), re.M)
        if not link or "organizacao:" in _front(text) or not project_has_org(vault, link.group(1)):
            continue
        text = text.replace("tipo: sessao-ia\n", f'tipo: sessao-ia\norganizacao: "{org}"\ntags: [prefeitura]\n', 1)
        text = re.sub(r"(\*\*Quando:\*\* [^\n]*)", lambda m: f"{m.group(1)} · **Organização:** {org}", text, count=1)
        path.write_text(text, encoding="utf-8")
        changed += 1
    return changed


# ── bloco de sessões ───────────────────────────────────────────────────────────────

def _front(text: str) -> str:
    return text.split("---", 2)[1] if text.startswith("---") else ""


def refresh_session_blocks(vault: Path, lines: Optional[Dict[str, str]] = None) -> int:
    """Reescreve o bloco 'Sessões' de cada nota de projeto com as sessões ligadas a ela (mais recentes primeiro)."""
    lines = lines or {}
    by_project: Dict[str, List[str]] = {}
    for path in sorted((vault / "sessoes").glob("*.md"), reverse=True):
        text = path.read_text(encoding="utf-8", errors="replace")
        link = re.search(r'^projeto_nota:\s*"\[\[(.+?)\]\]"', _front(text), re.M)
        if not link:
            continue
        title = re.search(r"^# (.+)$", text, re.M)
        summary = lines.get(path.stem) or ""
        by_project.setdefault(link.group(1), []).append(
            f"- {path.name[:10]} · [[{path.stem}|{title.group(1) if title else path.stem}]]" + (f" — {summary}" if summary else ""))
    changed = 0
    for path in (vault / "projetos").glob("*.md"):
        text = path.read_text(encoding="utf-8", errors="replace")
        rows = by_project.get(path.stem, [])
        block = f"{START}\n## Sessões com as IAs (gerado automaticamente)\n" + ("\n".join(rows) or "- (nenhuma ainda)") + f"\n{END}"
        if START in text and END in text:
            new = re.sub(re.escape(START) + r".*?" + re.escape(END), lambda _m: block, text, flags=re.S)
        else:
            new = text.rstrip("\n") + "\n\n" + block + "\n"
        if new != text:
            path.write_text(new, encoding="utf-8")
            changed += 1
    return changed


def link_sessions(vault: Path) -> int:
    """Liga ao projeto as notas de sessão já geradas (cabeçalho `projeto_nota` e link no corpo)."""
    changed = 0
    for path in (vault / "sessoes").glob("*.md"):
        text = path.read_text(encoding="utf-8")
        front = _front(text)
        if "projeto_nota:" in front:
            continue
        folder = re.search(r'^projeto:\s*"?(.*?)"?\s*$', front, re.M)
        summary = text.split("## Arquivos alterados")[0]
        name = resolve(vault, folder.group(1) if folder else "", summary)
        if not name:
            continue
        text = text.replace("tipo: sessao-ia\n", f'tipo: sessao-ia\nprojeto_nota: "[[{name}]]"\n', 1)
        text = re.sub(r"\*\*Projeto:\*\* [^·\n]*", lambda _m: f"**Projeto:** [[{name}]] ", text, count=1)
        path.write_text(text, encoding="utf-8")
        changed += 1
    return changed


# ── criação das notas ──────────────────────────────────────────────────────────────

def _run(cmd: List[str], cwd: Path) -> str:
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=30, encoding="utf-8",
                              errors="replace").stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _stack(path: Path) -> List[str]:
    found = []
    package = path / "package.json"
    if not package.exists():
        package = next(iter(path.glob("*/package.json")), package)
    if package.exists():
        try:
            data = json.loads(package.read_text(encoding="utf-8"))
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            for dep, label in (("next", "Next.js"), ("react", "React"), ("vite", "Vite"), ("express", "Express"),
                               ("tailwindcss", "Tailwind CSS"), ("typescript", "TypeScript"), ("better-sqlite3", "SQLite"),
                               ("pg", "PostgreSQL"), ("@neondatabase/serverless", "Neon/PostgreSQL"), ("prisma", "Prisma"),
                               ("drizzle-orm", "Drizzle")):
                if dep in deps:
                    found.append(label)
        except (OSError, ValueError):
            pass
    for marker, label in (("build.gradle.kts", "Kotlin/Gradle (Android)"), ("build.xml", "Java/Ant"), ("pom.xml", "Java/Maven"),
                          ("requirements.txt", "Python"), ("docker-compose.yml", "Docker Compose"), ("Dockerfile", "Docker")):
        if (path / marker).exists() or any(path.glob(f"*/{marker}")):
            found.append(label)
    return list(dict.fromkeys(found))


def facts(project: Dict[str, Any]) -> Dict[str, Any]:
    from session_harvest import redact
    out: Dict[str, Any] = {"nome": project["nome"], "caminhos": [], "stack": [], "readme": "", "estrutura": [], "git": ""}
    for path in project["paths"]:
        if not path.exists():
            continue
        out["caminhos"].append(str(path))
        out["stack"] += _stack(path)
        out["estrutura"] += sorted(p.name for p in path.iterdir() if p.name not in {"node_modules", ".git", "dist", "build"})[:25]
        for name in ("README.md", "readme.md", "readme.txt", "PROJECT_CONTEXT_L2.md", "UNIFICACAO.md"):
            if (path / name).exists() and len(out["readme"]) < README_CAP:
                out["readme"] += f"\n[{name}]\n" + (path / name).read_text(encoding="utf-8", errors="replace")[:README_CAP]
        if (path / ".git").exists():
            log = _run(["git", "log", "-12", "--date=short", "--pretty=%ad %s"], path)
            count = _run(["git", "rev-list", "--count", "HEAD"], path)
            out["git"] += f"\n[{path.name}] {count} commits; recentes:\n{log}"
    for extra in project.get("extra", []):
        if extra.exists():
            out["readme"] += "\n[memória do assistente]\n" + extra.read_text(encoding="utf-8", errors="replace")[:6000]
    out["stack"] = list(dict.fromkeys(out["stack"]))
    out["readme"], out["git"] = redact(out["readme"]), redact(out["git"])
    return out


PROMPT = """Você documenta um projeto de software do senhor __JARVIS_USER__ para o Segundo Cérebro (Obsidian). Com base APENAS nos
fatos abaixo, produza JSON estrito em português do Brasil, sem inventar:
- "visao_geral": 3 a 5 frases: o que é, para quem, qual problema resolve
- "funcionalidades": lista das funcionalidades principais
- "arquitetura": 2 a 4 frases sobre estrutura, banco, deploy (só o que os fatos mostram)
- "estado": em que pé está (a partir dos commits e arquivos); "proximos_passos": lista só do que os fatos indicam

<fatos>
Projeto: {nome}
Caminhos: {caminhos}
Pilha detectada: {stack}
Estrutura: {estrutura}
{readme}
Git:{git}
</fatos>"""


def _bullets(items: Any) -> str:
    return "\n".join(f"- {i}" for i in (items or []) if str(i).strip()) or "- (sem informação)"


def build(vault: Path, only: Optional[str] = None) -> List[str]:
    created = []
    org = f"[[{PREFEITURA_NOTE}]]" if (vault / "organizacoes" / f"{PREFEITURA_NOTE}.md").exists() else ""
    folder = vault / "projetos"
    folder.mkdir(exist_ok=True)
    for project in PROJECTS:
        if only and project["nome"].casefold() != only.casefold():
            continue
        path = folder / f"{project['nome']}.md"
        if path.exists() and path.stat().st_size > 0:
            continue  # nota já preenchida: não reescreve
        info = facts(project)
        if not info["caminhos"]:
            continue
        # Metadados e README permanecem locais; não inferir funcionalidades nem estado.
        data: Dict[str, Any] = {"visao_geral": f"Projeto {info['nome']}; documentação local inicial, sem inferência por IA.",
                               "arquitetura": "Pilha identificada: " + (", ".join(info["stack"]) or "não identificada"),
                               "estado": "Estado operacional ainda não validado nesta nota.",
                               "proximos_passos": ["Consultar README, código e fontes do projeto para validar o estado."]}
        pref = bool(project.get("prefeitura")) and bool(org)
        front = ("---\ntipo: projeto\n" + f"nome: {project['nome']}\n"
                 + f"aliases: [{', '.join(project['aliases'])}]\n" + f"palavras_chave: [{', '.join(project['palavras_chave'])}]\n"
                 + (f'organizacao: "{org}"\ntags: [projeto, prefeitura]\n' if pref else "tags: [projeto]\n")
                 + f"caminhos: [{', '.join(info['caminhos'])}]\n" + f"pilha: [{', '.join(info['stack'])}]\n"
                 + "origem: criada pelo Jarvis (project_notes.py); o senhor pode editar à vontade\n---\n")
        body = (f"# {project['nome']}\n\n{data.get('visao_geral') or ''}\n\n"
                + (f"**Organização:** {org}\n\n" if pref else "")
                + f"**Onde está:** {', '.join(f'`{p}`' for p in info['caminhos'])}\n"
                + f"**Pilha:** {', '.join(info['stack']) or '(não detectada)'}\n\n"
                + f"## Funcionalidades\n{_bullets(data.get('funcionalidades'))}\n\n## Arquitetura\n{data.get('arquitetura') or ''}\n\n"
                + f"## Estado\n{data.get('estado') or ''}\n\n## Próximos passos\n{_bullets(data.get('proximos_passos'))}\n\n"
                + f"## Histórico\n- {__import__('datetime').date.today():%Y-%m-%d} — nota criada a partir do código e do git (fonte: project_notes.py)\n")
        path.write_text(front + "\n" + body, encoding="utf-8")
        created.append(project["nome"])
    return created


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["build", "link", "blocks", "org"])
    parser.add_argument("--only")
    args = parser.parse_args()
    vault = cerebro.vault_dir()
    if vault is None:
        sys.exit("Cerebro não configurado.")
    if args.command == "build":
        print({"criadas": build(vault, args.only)})
    elif args.command == "link":
        print({"ligadas": link_sessions(vault)})
    elif args.command == "org":
        print({"ligadas_a_prefeitura": propagate_org(vault)})
    else:
        print({"blocos": refresh_session_blocks(vault)})
