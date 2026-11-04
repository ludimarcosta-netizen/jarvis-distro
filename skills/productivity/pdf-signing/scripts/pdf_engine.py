"""Incremental signatures and strict offline validation. No real key export."""

import hashlib
import io
from datetime import UTC, datetime

import pymupdf as fitz
from appearance import certificate_stamp
from evolution import Blocked
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.pdf_utils.misc import PdfStrictReadError
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign import fields, signers
from pyhanko.sign.validation import validate_pdf_signature
from pyhanko_certvalidator import ValidationContext


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def inspect(raw):
    with fitz.open(stream=raw, filetype="pdf") as pdf:
        if pdf.is_encrypted or not 0 < len(pdf) <= 300:
            raise Blocked("PDF protegido ou fora do limite de 300 páginas.")
        pages = []
        for page in pdf:
            text = page.get_text()
            if len(text) > 30000:
                raise Blocked(
                    "Página extensa exige análise em partes; extração parcial bloqueada."
                )
            if page.rotation or page.cropbox != page.mediabox:
                raise Blocked(
                    "Página rotacionada/recortada exige preparação manual para posição segura."
                )
            pages.append(
                {
                    "width": page.rect.width,
                    "height": page.rect.height,
                    "text": text,
                    "signature_boxes": [
                        [
                            w.rect.x0,
                            page.rect.height - w.rect.y1,
                            w.rect.x1,
                            page.rect.height - w.rect.y0,
                        ]
                        for w in page.widgets() or []
                        if w.field_type == fitz.PDF_WIDGET_TYPE_SIGNATURE
                    ],
                }
            )
    return pages


def certificate_id(cert):
    return sha(cert.dump())


def check_certificate(cert):
    now = datetime.now(UTC)
    validity = cert["tbs_certificate"]["validity"]
    if not validity["not_before"].native <= now <= validity["not_after"].native:
        raise Blocked("Certificado fora da validade.")
    if cert.key_usage_value is not None and not (
        {"digital_signature", "non_repudiation"} & cert.key_usage_value.native
    ):
        raise Blocked("Certificado sem uso permitido para assinatura.")


def validate(raw, roots, crls=None, *, only_fields=None):
    if not roots:
        raise Blocked("Raízes de confiança não configuradas; validação bloqueada.")
    context = ValidationContext(
        trust_roots=roots,
        allow_fetching=False,
        revocation_mode="hard-fail",
        crls=crls or [],
        ocsps=[],
    )
    reader = PdfFileReader(io.BytesIO(raw), strict=True)
    result = []
    for signature in reader.embedded_signatures:
        if only_fields is not None and signature.field_name not in only_fields:
            continue
        status = validate_pdf_signature(signature, signer_validation_context=context)
        if not status.bottom_line:
            raise Blocked(
                "Assinatura inválida, alteração proibida ou confiança/revogação não comprovada."
            )
        result.append(
            {
                "field": signature.field_name,
                "certificate": certificate_id(signature.signer_cert),
                "valid": True,
            }
        )
    return result


def preview(raw, placements, target):
    pdf = fitz.open(stream=raw, filetype="pdf")
    try:
        paths = []
        for index, placement in enumerate(placements):
            page = pdf[placement["page"]]
            x0, y0, x1, y1 = placement["box"]
            rectangle = fitz.Rect(x0, page.rect.height - y1, x1, page.rect.height - y0)
            page.draw_rect(rectangle, color=(0.9, 0.1, 0.1), width=2)
            page.insert_text(
                (rectangle.x0 + 4, rectangle.y0 + 12), placement["signer"], fontsize=9
            )
            path = target / f"preview-{index}.png"
            page.get_pixmap(matrix=fitz.Matrix(1.2, 1.2)).save(path)
            paths.append(path)
        return paths
    finally:
        pdf.close()


def without_hybrid_xref(raw):
    """Word often saves hybrid xrefs, which pyHanko rightly refuses to sign (readers may disagree).

    An unsigned file is rewritten by the same engine that rendered the approved preview, so the
    signed content is exactly what the human reviewed. Signed files are never rewritten.
    Foxit incremental saves with free xref entries fail the strict read; they are rewritten too.
    """
    try:
        if not PdfFileReader(io.BytesIO(raw), strict=True).xrefs.hybrid_xrefs_present:
            return raw
    except PdfStrictReadError:
        if PdfFileReader(io.BytesIO(raw), strict=False).embedded_signatures:
            raise Blocked(
                "PDF já assinado com estrutura irregular; assinatura bloqueada."
            ) from None
    with fitz.open(stream=raw, filetype="pdf") as pdf:
        before = [page.get_text() for page in pdf]
        rewritten = pdf.tobytes(garbage=3, deflate=True)
    with fitz.open(stream=rewritten, filetype="pdf") as pdf:
        if [page.get_text() for page in pdf] != before:
            raise Blocked("Regravação do PDF alterou o conteúdo; assinatura bloqueada.")
    return rewritten


def sign_incrementally(raw, placements, providers, roots, crls=None):
    # Existing signatures are opaque: preserve their bytes without validating them.
    try:
        previous_count = len(PdfFileReader(io.BytesIO(raw), strict=True).embedded_signatures)
    except PdfStrictReadError:
        previous_count = len(PdfFileReader(io.BytesIO(raw), strict=False).embedded_signatures)
        if previous_count:
            raise Blocked("PDF já assinado com estrutura irregular; assinatura bloqueada.") from None
    if not previous_count:
        raw = without_hybrid_xref(raw)
    added_fields = set()
    output = raw
    for index, placement in enumerate(placements):
        signer = providers[placement["signer"]]
        check_certificate(signer.signing_cert)
        if certificate_id(signer.signing_cert) != placement["certificate"]:
            raise Blocked("Certificado difere da prévia aprovada.")
        field = f"Jarvis_{sha(raw)[:16]}_{index}"
        added_fields.add(field)
        buffer = io.BytesIO()
        writer = IncrementalPdfFileWriter(io.BytesIO(output), strict=True)
        with certificate_stamp(signer.signing_cert) as style:
            signers.PdfSigner(
                signers.PdfSignatureMetadata(field_name=field, md_algorithm="sha256"),
                signer=signer,
                stamp_style=style,
                new_field_spec=fields.SigFieldSpec(
                    field, on_page=placement["page"], box=tuple(placement["box"])
                ),
            ).sign_pdf(writer, output=buffer)
        candidate = buffer.getvalue()
        if not candidate.startswith(output):
            raise Blocked("Assinatura não preservou bytes anteriores.")
        output = candidate
    final = validate(output, roots, crls, only_fields=added_fields)
    if len(final) != len(placements):
        raise Blocked("Conjunto de novas assinaturas incompleto.")
    return output, final


def real_providers(placements, validation_dir):
    from validation_data import refresh
    from windows_signer import StaleRevocation, load_providers

    try:
        return load_providers(placements, validation_dir)
    except StaleRevocation:
        # CRLs expire within hours; refresh public data once (no key use), then revalidate strictly.
        refresh(validation_dir)
        return load_providers(placements, validation_dir)
