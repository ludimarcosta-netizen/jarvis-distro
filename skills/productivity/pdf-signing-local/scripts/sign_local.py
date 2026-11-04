# -*- coding: utf-8 -*-
"""CLI script to sign local PDF documents using Windows Certificate Store."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Path to pdf-signing scripts
PDF_SIGNING_DIR = Path("__JARVIS_HERMES_HOME_FS__/skills/productivity/pdf-signing")
SCRIPTS_DIR = PDF_SIGNING_DIR / "scripts"
VAL_DIR = PDF_SIGNING_DIR / "validation"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import pymupdf as fitz
import unicodedata
from windows_signer import inventory
import pdf_engine


def fold(text: str) -> str:
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKD", text)
    without_accents = "".join(c for c in normalized if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", without_accents).strip().lower()


def resolve_pdf_path(path_str: str) -> Path:
    p = Path(path_str)
    if p.is_file():
        return p.resolve()

    # Common search folders if relative or filename only
    candidates = [
        Path.cwd() / path_str,
        Path("__JARVIS_DOWNLOADS_FS__") / path_str,
        Path.home() / "Downloads" / path_str,
        Path("__JARVIS_DOWNLOADS_FS__") / f"{path_str}.pdf",
        Path.home() / "Downloads" / f"{path_str}.pdf",
    ]
    for c in candidates:
        if c.is_file():
            return c.resolve()

    # Try wildcard search in __JARVIS_DOWNLOADS_BS__
    downloads_dir = Path("__JARVIS_DOWNLOADS_FS__")
    if downloads_dir.is_dir():
        matches = list(downloads_dir.glob(f"*{path_str}*"))
        pdf_matches = [m for m in matches if m.suffix.lower() == ".pdf"]
        if pdf_matches:
            return pdf_matches[0].resolve()

    raise FileNotFoundError(f"Documento PDF não encontrado: {path_str}")


def find_placement_for_signer(doc: fitz.Document, alias: str) -> tuple[int, list[float]]:
    alias_folded = fold(alias)
    search_terms = [alias_folded]
    if alias_folded == "__JARVIS_USER_LOWER__":
        search_terms.extend(["__JARVIS_USER_LOWER__", "__JARVIS_USER_FULLNAME_LOWER__"])
    elif alias_folded == "beltrano":
        search_terms.extend(["beltrano seis", "beltrano seis de castro"])

    for page_idx in range(len(doc) - 1, -1, -1):
        page = doc[page_idx]
        rect = page.rect
        blocks = page.get_text("dict")["blocks"]
        text_lines = [
            fitz.Rect(line["bbox"])
            for block in blocks
            for line in block.get("lines", [])
            if "".join(span["text"] for span in line["spans"]).strip()
        ]

        for block in blocks:
            for line in block.get("lines", []):
                text = " ".join(span["text"] for span in line["spans"]).strip()
                folded_line = fold(text)
                matched = any(term in folded_line for term in search_terms)
                if matched and not re.search(r"[,;:]", text):
                    line_rect = fitz.Rect(line["bbox"])
                    center = (line_rect.x0 + line_rect.x1) / 2
                    left = max(0, center - 175)
                    right = min(rect.width, center + 175)
                    bottom = line_rect.y0 - 2
                    above = [
                        r.y1 for r in text_lines
                        if r.y1 <= line_rect.y0 and r.x0 < right and r.x1 > left
                    ]
                    top = max(max(above, default=0) + 2, bottom - 65)
                    if bottom - top >= 25:
                        return page_idx, [left, rect.height - bottom, right, rect.height - top]
                    return page_idx, [left, rect.height - (line_rect.y1 + 50), right, rect.height - line_rect.y0]

    last_page = doc[-1]
    pw, ph = last_page.rect.width, last_page.rect.height
    return len(doc) - 1, [pw / 2 - 150, 40, pw / 2 + 150, 100]


def get_available_certificates() -> list[dict]:
    inv = inventory()
    return [c for c in inv if c.get("has_private_key") and c.get("valid_now")]


def sign_local_pdf(
    input_path: Path | str,
    output_path: Path | str | None = None,
    signer_alias: str | None = None,
    cert_fingerprint: str | None = None,
    box_coords: list[float] | None = None,
    page_num: int | None = None,
) -> dict:
    pdf_file = resolve_pdf_path(str(input_path))
    pdf_bytes = pdf_file.read_bytes()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    alias = signer_alias or "__JARVIS_USER_LOWER__"
    certs = get_available_certificates()
    selected_cert = None

    if cert_fingerprint:
        for c in certs:
            if c.get("fingerprint") == cert_fingerprint:
                selected_cert = c
                break
    elif alias:
        for c in certs:
            if c.get("alias") == alias:
                selected_cert = c
                break

    if not selected_cert and certs:
        selected_cert = certs[0]
        alias = selected_cert.get("alias", "__JARVIS_USER_LOWER__")

    if not selected_cert:
        raise RuntimeError("Nenhum certificado digital ICP-Brasil com chave privada válido encontrado no Windows Store.")

    if box_coords is not None and page_num is not None:
        placement_page = page_num
        placement_box = box_coords
    else:
        placement_page, placement_box = find_placement_for_signer(doc, alias)

    placements = [{
        "signer": alias,
        "page": placement_page,
        "box": placement_box,
        "certificate": selected_cert["fingerprint"],
    }]

    provs, roots, crls = pdf_engine.real_providers(placements, VAL_DIR)
    signed_bytes, validation = pdf_engine.sign_incrementally(pdf_bytes, placements, provs, roots, crls)

    if output_path:
        target_path = Path(output_path).resolve()
    else:
        target_path = pdf_file.parent / f"{pdf_file.stem}_assinado.pdf"

    target_path.write_bytes(signed_bytes)

    return {
        "status": "success",
        "input_file": str(pdf_file),
        "signed_file": str(target_path),
        "signer": alias,
        "signer_name": selected_cert.get("alias", alias),
        "certificate_fingerprint": selected_cert["fingerprint"],
        "page": placement_page + 1,
        "box": placement_box,
        "validation": validation,
    }


def main():
    parser = argparse.ArgumentParser(description="Assina documento PDF local usando certificado digital do Windows")
    parser.add_argument("file", help="Caminho ou nome do arquivo PDF")
    parser.add_argument("--signer", "-s", help="Alias do signatário (ex: __JARVIS_USER_LOWER__, beltrano)")
    parser.add_argument("--output", "-o", help="Caminho do arquivo de saída assinado")
    parser.add_argument("--json", action="store_true", help="Retornar saída em formato JSON")
    args = parser.parse_args()

    try:
        result = sign_local_pdf(
            input_path=args.file,
            output_path=args.output,
            signer_alias=args.signer,
        )
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"[OK] Documento assinado com sucesso!")
            print(f"Arquivo assinado: {result['signed_file']}")
            print(f"Signatário: {result['signer_name']}")
            print(f"Página: {result['page']}")
    except Exception as e:
        if args.json:
            print(json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False))
        else:
            print(f"[ERRO] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
