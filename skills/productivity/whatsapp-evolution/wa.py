"""WhatsApp do senhor __JARVIS_USER__ via Evolution API local (127.0.0.1:8090).

Uso:
  wa.py status
  wa.py conversas [--horas 24] [--limite 40]
  wa.py mensagens [--horas 24 | --desde AAAA-MM-DD[THH:MM]] [--conversa TEXTO] [--limite 400] [--ids]
  wa.py enviar --para "NOME/NÚMERO/GRUPO ou eu" --texto "MENSAGEM"

Saída em texto simples, uma linha por item. Só biblioteca padrão.
Chave lida de EVOLUTION_ENV_FILE (padrão: Projects/evolution-whatsapp/sistema/.env).
"""
import argparse
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

URL = os.environ.get("EVOLUTION_URL", "http://127.0.0.1:8090")
INSTANCE = os.environ.get("EVOLUTION_INSTANCE", "jarvis")
ENV_FILE = Path(os.environ.get("EVOLUTION_ENV_FILE",
                               Path.home() / "Projects" / "evolution-whatsapp" / "sistema" / ".env"))
PAGE = 100


class EvolutionError(RuntimeError):
    pass


def _key() -> str:
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("AUTHENTICATION_API_KEY="):
                return line.split("=", 1)[1].strip()
    except OSError as exc:
        raise EvolutionError(f"Configuração da Evolution não encontrada ({ENV_FILE}).") from exc
    raise EvolutionError("AUTHENTICATION_API_KEY ausente no .env da Evolution.")


def call(path: str, body=None, method: str = "POST"):
    data = json.dumps(body or {}).encode() if method == "POST" else None
    req = urllib.request.Request(URL + path, data=data, method=method,
                                 headers={"apikey": _key(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise EvolutionError(f"Evolution respondeu {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise EvolutionError("Evolution API fora do ar. Abra o Docker Desktop e rode "
                             "'docker compose up -d' em Projects/evolution-whatsapp.") from exc


def fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", str(text).casefold()) if not unicodedata.combining(c))


def text_of(msg: dict) -> str:
    m = msg.get("message") or {}
    for key, path in (("conversation", None), ("extendedTextMessage", "text"), ("imageMessage", "caption"),
                      ("videoMessage", "caption"), ("documentMessage", "fileName"),
                      ("documentWithCaptionMessage", None)):
        value = m.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict) and path and value.get(path):
            prefix = {"imageMessage": "[imagem] ", "videoMessage": "[vídeo] ", "documentMessage": "[documento] "}
            return prefix.get(key, "") + str(value[path])
    labels = {"audioMessage": "[áudio]", "imageMessage": "[imagem]", "videoMessage": "[vídeo]",
              "stickerMessage": "[figurinha]", "documentMessage": "[documento]", "locationMessage": "[localização]",
              "contactMessage": "[contato]", "pollCreationMessageV3": "[enquete]", "albumMessage": "[álbum]",
              "reactionMessage": "", "protocolMessage": ""}
    kind = msg.get("messageType") or next(iter(m), "")
    return labels.get(kind, f"[{kind}]" if kind else "")


def load_aliases() -> dict:
    alias_file = Path(__file__).parent / "aliases.json"
    if alias_file.exists():
        try:
            with open(alias_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def learn_alias(name: str, jid: str) -> None:
    """Grava o apelido confirmado pelo senhor (nome dito -> conversa)."""
    alias_file = Path(__file__).parent / "aliases.json"
    aliases = load_aliases()
    aliases[name.strip()] = jid
    tmp = alias_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(aliases, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(alias_file)


# Agenda do celular exportada em .vcf: a Evolution só conhece o nome de perfil, não o nome salvo.
CONTACTS_DIR = Path(os.environ.get("HERMES_HOME", Path(__file__).resolve().parents[3])) / "state" / "contacts"
_book_cache: tuple = (None, {})


def phone_key(value: str) -> str:
    """Chave estável de telefone; no Brasil ignora o nono dígito (WhatsApp antigo não o usa)."""
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("0") and not digits.startswith("00"):
        digits = digits[1:]
        if len(digits) in (12, 13):  # 0 + operadora + DDD + número
            digits = digits[2:]
    elif digits.startswith("00"):  # discagem internacional
        digits = digits[2:]
    if len(digits) in (8, 9):  # salvo sem DDD: número local
        digits = os.environ.get("WA_DDD_PADRAO", "64") + digits
    if len(digits) in (10, 11) and not digits.startswith("55"):
        digits = "55" + digits
    if digits.startswith("55") and len(digits) in (12, 13):
        return digits[:4] + digits[-8:]
    return digits


def parse_vcf(text: str) -> dict:
    """phone_key -> nome salvo. Aceita vCard 2.1/3.0/4.0, inclusive QUOTED-PRINTABLE do Android."""
    import quopri

    lines: list = []
    for raw in text.splitlines():
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif lines and lines[-1].endswith("=") and "QUOTED-PRINTABLE" in lines[-1].upper():
            lines[-1] = lines[-1][:-1] + raw
        else:
            lines.append(raw)
    book, name, phones = {}, "", []
    for line in lines:
        head, _, value = line.partition(":")
        field = head.split(";")[0].split(".")[-1].upper()
        if "QUOTED-PRINTABLE" in head.upper():
            value = quopri.decodestring(value.encode("latin-1", "replace")).decode("utf-8", "replace")
        if field == "BEGIN":
            name, phones = "", []
        elif field == "FN":
            name = value.replace("\\,", ",").replace("\\;", ";").strip()
        elif field == "N" and not name:
            parts = [p for p in value.split(";") if p.strip()]
            name = " ".join(reversed(parts[:2])).strip()
        elif field == "TEL":
            phones.append(value)
        elif field == "END" and name:
            for phone in phones:
                key = phone_key(phone)
                if len(key) >= 8:
                    book.setdefault(key, name)
    return book


def address_book() -> dict:
    global _book_cache
    files = sorted(CONTACTS_DIR.glob("*.vcf"), key=lambda p: p.stat().st_mtime) if CONTACTS_DIR.is_dir() else []
    if not files:
        return {}
    stamp = tuple((p.name, p.stat().st_mtime) for p in files)
    if _book_cache[0] != stamp:
        book: dict = {}
        for path in files:  # o arquivo mais novo prevalece
            book.update(parse_vcf(path.read_text(encoding="utf-8", errors="replace")))
        _book_cache = (stamp, book)
    return _book_cache[1]


def saved_name(jid: str, alt: str = "") -> str:
    """Nome salvo na agenda para a conversa (usa o número alternativo quando o jid é @lid)."""
    book = address_book()
    for candidate in (jid, alt):
        if candidate and candidate.endswith("@s.whatsapp.net"):
            name = book.get(phone_key(candidate.split("@")[0]))
            if name:
                return name
    return ""


def names() -> dict:
    """jid -> nome legível (nome salvo na agenda, assunto do grupo ou nome de perfil)."""
    out = {}
    try:
        contacts = call(f"/chat/findContacts/{INSTANCE}", body={}, method="POST")
        if isinstance(contacts, list):
            for c in contacts:
                name = c.get("pushName") or c.get("fullName") or c.get("name") or c.get("savedName")
                if name and c.get("remoteJid"):
                    out[c["remoteJid"]] = name
    except Exception:
        pass
    alternates = {}
    try:
        for chat in call(f"/chat/findChats/{INSTANCE}"):
            if chat.get("pushName"):
                out[chat["remoteJid"]] = chat["pushName"]
            last = chat.get("lastMessage") if isinstance(chat.get("lastMessage"), dict) else {}
            alt = (last.get("key") or {}).get("remoteJidAlt") or ""
            if alt:
                alternates[chat["remoteJid"]] = alt
    except Exception:
        pass
    if address_book():
        for jid in set(out) | set(alternates):
            name = saved_name(jid, alternates.get(jid, ""))
            if name:
                out[jid] = name
    try:
        groups = call(f"/group/fetchAllGroups/{INSTANCE}?getParticipants=false", method="GET")
        if isinstance(groups, list):
            for group in groups:
                if group.get("subject"):
                    out[group["id"]] = group["subject"]
    except Exception:
        pass
    aliases = load_aliases()
    for key, val in aliases.items():
        if key.endswith("@s.whatsapp.net") or key.endswith("@g.us") or key.endswith("@lid"):
            out[key] = val
    return out


SELF_NAMES = {"eu", "mim", "para mim", "me", "meu numero", "eu mesmo", "__JARVIS_USER_LOWER__"}


def own_jid() -> str:
    """Número do próprio senhor (dono da instância conectada)."""
    found = call(f"/instance/fetchInstances?instanceName={INSTANCE}", method="GET")
    jid = (found[0] if found else {}).get("ownerJid") or ""
    if not jid:
        raise EvolutionError("Não consegui ler o número conectado na Evolution.")
    return jid


def resolve(query: str, directory: dict) -> list:
    if fold(query).strip() in SELF_NAMES:
        return [own_jid()]
    aliases = load_aliases()
    for k, v in aliases.items():
        if fold(k) == fold(query):
            target = v if (v.endswith("@s.whatsapp.net") or v.endswith("@g.us") or v.endswith("@lid")) else k
            return [target]
    digits = re.sub(r"\D", "", query)
    if len(digits) >= 8:
        matched = [j for j in directory if digits in j]
        if matched:
            return matched
        last8 = digits[-8:]
        matched = [j for j in directory if last8 in j]
        if matched:
            return matched
        return [f"55{digits}@s.whatsapp.net" if not digits.startswith("55") else f"{digits}@s.whatsapp.net"]
    q = fold(query)
    exact = [j for j, n in directory.items() if fold(n) == q]
    if exact:
        return exact
    sub = [j for j, n in directory.items() if q in fold(n)]
    if sub:
        return sub
    for k, v in aliases.items():
        if q in fold(k):
            target = v if (v.endswith("@s.whatsapp.net") or v.endswith("@g.us") or v.endswith("@lid")) else k
            return [target]
    return []


def since_from(args) -> datetime:
    if args.desde:
        return datetime.fromisoformat(args.desde).astimezone(timezone.utc)
    return datetime.now(timezone.utc) - timedelta(hours=args.horas)


def local(ts: int) -> str:
    return datetime.fromtimestamp(int(ts)).strftime("%d/%m %H:%M")


def cmd_status(_args) -> None:
    state = call(f"/instance/connectionState/{INSTANCE}", method="GET").get("instance", {}).get("state")
    print(f"WhatsApp: {state}" + ("" if state == "open" else " (precisa reconectar: rode sistema/conectar.py)"))


def cmd_conversas(args) -> None:
    directory = names()
    since = since_from(args).timestamp()
    chats = [c for c in call(f"/chat/findChats/{INSTANCE}")
             if int((c.get("lastMessage") or {}).get("messageTimestamp") or 0) >= since]
    chats.sort(key=lambda c: -int(c["lastMessage"]["messageTimestamp"]))
    for chat in chats[:args.limite]:
        jid = chat["remoteJid"]
        kind = "grupo" if jid.endswith("@g.us") else "contato"
        last = chat["lastMessage"]
        unread = f" · {chat['unreadCount']} não lidas" if chat.get("unreadCount") else ""
        print(f"[{local(last['messageTimestamp'])}] {directory.get(jid, jid)} ({kind}{unread}) — "
              f"{text_of(last)[:120]}")
    if not chats:
        print("Nenhuma conversa com atividade no período.")


def cmd_mensagens(args) -> None:
    directory = names()
    where = {"messageTimestamp": {"gte": since_from(args).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                  "lte": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}}
    jids = [None]
    if args.conversa:
        jids = resolve(args.conversa, directory)
        if not jids:
            raise EvolutionError(f"Nenhuma conversa encontrada para '{args.conversa}'.")
        if len(jids) > 5:
            raise EvolutionError("Muitas conversas combinam: " + "; ".join(directory.get(j, j) for j in jids[:10]))
    records = []
    for jid in jids:
        body_where = dict(where, **({"key": {"remoteJid": jid}} if jid else {}))
        page = 1
        while len(records) < args.limite:
            data = call(f"/chat/findMessages/{INSTANCE}", {"where": body_where, "page": page, "offset": PAGE})
            batch = data.get("messages", {}).get("records", [])
            records += batch
            if page >= data.get("messages", {}).get("pages", 0) or not batch:
                break
            page += 1
    records.sort(key=lambda r: int(r.get("messageTimestamp") or 0))
    for r in records[-args.limite:]:
        text = text_of(r)
        if not text:
            continue
        jid = (r.get("key") or {}).get("remoteJid", "")
        key = r.get("key") or {}
        push = r.get("pushName") or ""
        who = "Eu" if key.get("fromMe") else (push if re.search(r"[^\W\d_]", push)
                                              else directory.get(key.get("participant", ""), push or "?"))
        where_txt = directory.get(jid, jid) if jid.endswith("@g.us") else directory.get(jid, who)
        ref = f" [chat={jid} id={key.get('id', '')}]" if args.ids else ""
        print(f"[{local(r['messageTimestamp'])}] {where_txt} | {who}: {text[:500]}{ref}")
    if not records:
        print("Nenhuma mensagem no período.")


# Envios recentes: só hash de destino+texto (sem conteúdo), para um retry não duplicar a mensagem.
SENT_LOG = CONTACTS_DIR.parent / "whatsapp" / "sent.json"
DUP_WINDOW_S = 900


@contextmanager
def _send_lock():
    """Serialize reservation and journal writes across CLI processes, never the network call."""
    SENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with SENT_LOG.with_suffix(".lock").open("a+b") as handle:
        deadline = time.monotonic() + 10
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise EvolutionError("Registro de envios ocupado. Nenhuma mensagem foi enviada.") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _sent_log() -> dict:
    try:
        data = json.loads(SENT_LOG.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or any(
                not isinstance(v, dict) or type(v.get("at")) not in (int, float)
                or v.get("status") not in {"pending", "sent", "failed", "uncertain"} for v in data.values()):
            raise ValueError("Invalid send journal")
        return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise EvolutionError("Registro de envios indisponível. Confira a conversa antes de tentar novamente.") from None


def _write_send(log: dict, fingerprint: str, status: str, message_id: str = "", reservation: str = "") -> None:
    now = datetime.now(timezone.utc).timestamp()
    log = {k: v for k, v in log.items() if now - v["at"] < 86400}
    log[fingerprint] = {"status": status, "at": now, "id": message_id, "reservation": reservation}
    tmp = SENT_LOG.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(log))
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(SENT_LOG)


def _record_send(fingerprint: str, status: str, message_id: str = "", *, reservation: str) -> None:
    with _send_lock():
        log = _sent_log()
        if log.get(fingerprint, {}).get("reservation") == reservation:
            _write_send(log, fingerprint, status, message_id, reservation)


def _reserve_send(fingerprint: str, repeat: bool) -> str:
    import secrets

    with _send_lock():
        log = _sent_log()
        previous = log.get(fingerprint)
        age = datetime.now(timezone.utc).timestamp() - previous["at"] if previous else None
        if age is not None and age < DUP_WINDOW_S and previous["status"] != "failed" and not repeat:
            state = "já foi enviada" if previous["status"] == "sent" else "foi tentada sem confirmação"
            raise EvolutionError(f"A mesma mensagem para este destinatário {state} há {int(age // 60)} min. "
                                 "Não reenviei. Confira a conversa; para repetir de propósito use --repetir.")
        reservation = secrets.token_hex(16)
        _write_send(log, fingerprint, "pending", reservation=reservation)
        return reservation


def cmd_enviar(args) -> None:
    import hashlib

    targets = resolve(args.para, names())
    if len(targets) != 1:
        found = "; ".join(targets[:10]) or "nenhum"
        raise EvolutionError(f"Destinatário ambíguo ou inexistente para '{args.para}' (encontrados: {found}). "
                             "Peça o nome exato ou o número.")
    number = targets[0] if targets[0].endswith(("@g.us", "@lid")) else targets[0].split("@")[0]
    fingerprint = hashlib.sha256(f"{number}\n{args.texto}".encode()).hexdigest()
    reservation = _reserve_send(fingerprint, args.repetir)
    try:
        result = call(f"/message/sendText/{INSTANCE}", {"number": number, "text": args.texto})
    except EvolutionError as exc:
        cause = exc.__cause__
        refused = (isinstance(cause, urllib.error.HTTPError) and cause.code < 500) or (
            isinstance(cause, urllib.error.URLError) and not isinstance(cause, urllib.error.HTTPError)
            and not isinstance(cause.reason, TimeoutError))
        _record_send(fingerprint, "failed" if refused else "uncertain", reservation=reservation)
        raise
    except OSError as exc:  # timeout de leitura: a Evolution pode ter enviado
        _record_send(fingerprint, "uncertain", reservation=reservation)
        raise EvolutionError("Envio sem resposta da Evolution. Confira a conversa antes de repetir.") from exc
    key = result.get("key") or {}
    if not key.get("id"):
        _record_send(fingerprint, "uncertain", reservation=reservation)
        raise EvolutionError(f"Envio sem confirmação da Evolution: {str(result)[:200]}. Não repita sem verificar.")
    _record_send(fingerprint, "sent", str(key["id"]), reservation=reservation)
    print(f"Mensagem enviada ao WhatsApp (id {key['id']}).")


def downloads_dir() -> Path:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
            value = winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")[0]
        return Path(os.path.expandvars(value))
    except (ImportError, OSError):
        return Path.home() / "Downloads"


def cmd_importar_agenda(args) -> None:
    import shutil

    source = Path(args.arquivo) if args.arquivo else None
    if source is None:
        found = sorted(downloads_dir().glob("*.vcf"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not found:
            raise EvolutionError(f"Nenhum .vcf em {downloads_dir()}. Exporte a agenda do celular para lá.")
        source = found[0]
    book = parse_vcf(source.read_text(encoding="utf-8", errors="replace"))
    if not book:
        raise EvolutionError(f"{source.name} não tem contatos com telefone.")
    CONTACTS_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, CONTACTS_DIR / "agenda.vcf")
    print(f"Agenda importada de {source}: {len(book)} telefones com nome salvo.")


def main() -> int:
    parser = argparse.ArgumentParser(description="WhatsApp via Evolution API")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    for name in ("conversas", "mensagens"):
        p = sub.add_parser(name)
        p.add_argument("--horas", type=float, default=24)
        p.add_argument("--desde", help="AAAA-MM-DD ou AAAA-MM-DDTHH:MM (hora local)")
        p.add_argument("--limite", type=int, default=40 if name == "conversas" else 400)
        if name == "mensagens":
            p.add_argument("--conversa", help="nome do contato/grupo ou número")
            p.add_argument("--ids", action="store_true", help="mostra chat e id de cada mensagem (pdf-signing fetch)")
    p = sub.add_parser("enviar")
    p.add_argument("--para", required=True)
    p.add_argument("--texto", required=True)
    p.add_argument("--repetir", action="store_true", help="reenvia mesmo se a mesma mensagem saiu há pouco")
    p = sub.add_parser("importar-agenda")
    p.add_argument("arquivo", nargs="?", help=".vcf exportado do celular (padrão: o mais novo em Downloads)")
    args = parser.parse_args()
    try:
        {"status": cmd_status, "conversas": cmd_conversas, "mensagens": cmd_mensagens,
         "enviar": cmd_enviar, "importar-agenda": cmd_importar_agenda}[args.cmd](args)
    except EvolutionError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
