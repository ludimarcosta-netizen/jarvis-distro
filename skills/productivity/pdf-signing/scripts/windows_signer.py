"""Non-exporting Windows CSP/CNG adapter. Public certificates only in Python."""

import asyncio
import base64
import json
import shutil
import subprocess
from pathlib import Path

from asn1crypto import crl, x509
from evolution import Blocked
from pyhanko.sign.signers import Signer
from pyhanko_certvalidator import CertificateValidator, ValidationContext
from pyhanko_certvalidator.errors import (
    InvalidCertificateError,
    PathBuildingError,
    PathValidationError,
)
from pyhanko_certvalidator.registry import SimpleCertificateStore


class StaleRevocation(Blocked):
    """Offline revocation data missing or expired; refreshing public CRLs may resolve it."""


def invoke(mode, body=None, run=subprocess.run):
    powershell = shutil.which("pwsh") or shutil.which("powershell.exe")
    if not powershell:
        raise Blocked("PowerShell não disponível para o provedor Windows.")
    try:
        result = run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(Path(__file__).with_name("windows_certificate.ps1")),
                "-Mode",
                mode,
            ],
            input=json.dumps(body).encode() if body is not None else None,
            capture_output=True,
            timeout=90,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.SubprocessError as exc:
        raise Blocked(
            "Provedor Windows não respondeu; não repetir assinatura automaticamente."
        ) from exc
    if result.returncode or len(result.stdout) > 2000000:
        raise Blocked("Operação do certificado bloqueada pelo Windows.")
    return json.loads(result.stdout.decode("utf-8-sig"))


def inventory(online=False):
    result = invoke("inventory-online" if online else "inventory")
    if not isinstance(result, list):
        raise Blocked("Inventário de certificados inválido.")
    return result


class WindowsSigner(Signer):
    def __init__(self, record):
        self.record = record
        public = x509.Certificate.load(
            base64.b64decode(record["certificate"], validate=True)
        )
        registry = SimpleCertificateStore()
        for encoded in record["chain"]:
            certificate = x509.Certificate.load(
                base64.b64decode(encoded, validate=True)
            )
            # PdfSigner adds the leaf separately; the registry contains issuers only.
            if certificate.dump() != public.dump():
                registry.register(certificate)
        super().__init__(signing_cert=public, cert_registry=registry, prefer_pss=False)

    async def async_sign_raw(self, data, digest_algorithm, dry_run=False):
        if digest_algorithm != "sha256":
            raise Blocked("Somente SHA-256 é permitido.")
        if dry_run:
            return bytes(self.signing_cert.public_key.bit_size // 8)
        response = await asyncio.to_thread(
            invoke,
            "sign",
            {
                "alias": self.record["alias"],
                "fingerprint": self.record["fingerprint"],
                "digest": digest_algorithm,
                "data": base64.b64encode(data).decode(),
            },
        )
        return base64.b64decode(response["signature"], validate=True)


def load_providers(placements, validation_dir):
    from pdf_engine import certificate_id, check_certificate

    records = inventory()
    providers, roots = {}, []
    validation_dir = Path(validation_dir)
    crls = []
    if validation_dir.is_dir():
        for path in validation_dir.glob("*.crl"):
            if path.stat().st_size > 5 * 1024 * 1024:
                raise Blocked("CRL excede limite local.")
            crls.append(crl.CertificateList.load(path.read_bytes()))
    for placement in placements:
        matches = [
            r
            for r in records
            if r.get("alias") == placement["signer"]
            and r.get("fingerprint") == placement["certificate"]
        ]
        if len(matches) != 1:
            raise Blocked("Certificado aprovado não mapeado de forma única no Windows.")
        record = matches[0]
        if not record.get("has_private_key") or not record.get("valid_now"):
            raise Blocked(
                "Chave ou validade do certificado não comprovada pelo Windows."
            )
        if not record.get("chain_ok"):
            raise StaleRevocation(
                "Revogação ou cadeia offline não comprovada pelo Windows."
            )
        provider = WindowsSigner(record)
        check_certificate(provider.signing_cert)
        if certificate_id(provider.signing_cert) != placement["certificate"]:
            raise Blocked("Certificado público difere do aprovado.")
        providers[placement["signer"]] = provider
        # Only an OS-validated chain can contribute a root; never trust an arbitrary leaf.
        roots.append(
            x509.Certificate.load(base64.b64decode(record["chain"][-1], validate=True))
        )
    context = ValidationContext(
        trust_roots=roots,
        crls=crls,
        ocsps=[],
        allow_fetching=False,
        revocation_mode="hard-fail",
    )
    for provider in providers.values():
        validator = CertificateValidator(
            provider.signing_cert,
            intermediate_certs=list(provider.cert_registry),
            validation_context=context,
        )
        try:
            asyncio.run(validator.async_validate_usage({"digital_signature"}))
        except (
            ValueError,
            PathBuildingError,
            PathValidationError,
            InvalidCertificateError,
        ) as exc:
            raise StaleRevocation(
                "Confiança/revogação não comprovada antes de acessar a chave."
            ) from exc
    return providers, roots, crls
