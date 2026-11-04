"""Short spoken-name hints for review; never an authentication signal."""
from __future__ import annotations

import re
import unicodedata


def folded(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value.casefold())
                   if not unicodedata.combining(c))


def spoken_name(text: str, profiles: list[dict]) -> tuple[str, str]:
    text = str(text or "")[:300]
    plain = folded(text)
    named = [profile for profile in profiles
             if re.search(r"(?<!\w)" + re.escape(folded(profile["name"])) + r"(?!\w)", plain)]
    if len(named) == 1:
        return named[0]["name"], named[0]["id"]
    if named:
        return "", ""  # multiple names in one utterance are ambiguous
    match = re.search(
        r"\b(?:essa e|este e|essa pessoa e|estou com|aqui e|o nome (?:dela|dele) e)\s+"
        r"(?:a |o )?([A-Za-zÀ-ÿ]{2,40})(?=\b)", plain, re.IGNORECASE)
    if not match:
        return "", ""
    return match.group(1).title(), ""


def meeting_participant_name(text: str) -> str:
    """Extract one person named after a meeting; ambiguous lists stay unassigned."""
    plain = folded(str(text or "")[:300])
    match = re.search(r"\b(?:essa|a) reuniao(?: de hoje| agora)? foi com (?:a |o )?"
                      r"([a-z]{2,40}(?: [a-z]{1,40}){0,4})(?=$|[,.!?])", plain)
    if not match:
        return ""
    words = match.group(1).split()
    if len(words) > 3 or any(word in {"e", "mais", "ou", "tambem"} for word in words):
        return ""
    return " ".join(words).title()
