"""Certificate-derived, two-column signature appearance."""

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pymupdf as fitz
from pyhanko.stamp import StaticStampStyle


def appearance_pdf(certificate, when=None):
    identity = certificate.subject.native.get("common_name")
    if not identity:
        raise ValueError("Certificado sem nome comum para o carimbo.")
    when = when or datetime.now().astimezone()
    details = (
        "Assinado de forma digital por\n"
        f"{identity}\n"
        f"Dados: {when.strftime('%Y.%m.%d %H:%M:%S %z')}"
    )
    with fitz.open() as pdf:
        page = pdf.new_page(width=440, height=100)
        for rectangle, text, initial in (
            (fitz.Rect(3, 5, 213, 95), identity, 19),
            (fitz.Rect(223, 5, 437, 95), details, 12),
        ):
            size = initial
            while size >= 5:
                shape = page.new_shape()
                if (
                    shape.insert_textbox(
                        rectangle, text, fontsize=size, fontname="helv"
                    )
                    >= 0
                ):
                    shape.commit()
                    break
                size -= 0.5
            else:
                raise ValueError("Dados do certificado excedem a área do carimbo.")
        return pdf.tobytes()


@contextmanager
def certificate_stamp(certificate):
    with TemporaryDirectory(prefix="jarvis-stamp-") as directory:
        path = Path(directory) / "appearance.pdf"
        path.write_bytes(appearance_pdf(certificate))
        yield StaticStampStyle.from_pdf_file(str(path), border_width=0)
