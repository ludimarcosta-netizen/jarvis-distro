"""Fetch public issuer CRLs only; never sends a PDF, credential or private key."""

import hashlib
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, build_opener

from asn1crypto import crl, x509
from evolution import Blocked
from windows_signer import inventory


def public_url(url):
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise Blocked("URL de revogação não permitida.")
    addresses = socket.getaddrinfo(
        parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
    )
    if not addresses or any(
        not ipaddress.ip_address(item[4][0]).is_global for item in addresses
    ):
        raise Blocked("URL de revogação deve apontar para emissor público.")
    return url


class PublicRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def refresh(folder):
    import base64

    records = inventory(online=True)
    if not records:
        raise Blocked("Certificados não disponíveis neste contexto Windows.")
    if any(not row["chain_ok"] for row in records):
        statuses = {flag for row in records for flag in row["chain_status"]}
        if "Revoked" in statuses:
            raise Blocked(
                "Windows reportou certificado revogado; assinatura bloqueada."
            )
        raise Blocked(
            "Cadeia online não comprovada; pode haver falha de rede/emissor. Não equivale a revogação confirmada."
        )
    urls = set()
    for row in records:
        for encoded in row["chain"]:
            cert = x509.Certificate.load(base64.b64decode(encoded, validate=True))
            for point in cert.crl_distribution_points or []:
                name = point["distribution_point"]
                if name.name == "full_name":
                    for location in name.chosen:
                        if location.name == "uniform_resource_identifier":
                            urls.add(location.native)
    if not 0 < len(urls) <= 24:
        raise Blocked(
            "Quantidade de URLs CRL ausente ou acima do limite; preparar dados manualmente."
        )
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    opener = build_opener(PublicRedirects())
    downloaded, unavailable = 0, 0
    for url in sorted(urls):
        try:
            with opener.open(public_url(url), timeout=15) as response:
                raw = response.read(5 * 1024 * 1024 + 1)
            if len(raw) > 5 * 1024 * 1024:
                raise Blocked("CRL acima do limite.")
            parsed = crl.CertificateList.load(raw, strict=True)
            if parsed.dump() != raw:
                raise Blocked("Formato CRL inesperado.")
            (folder / (hashlib.sha256(raw).hexdigest() + ".crl")).write_bytes(raw)
            downloaded += 1
        except (ValueError, OSError):
            unavailable += 1
    if not downloaded:
        raise Blocked(
            "Windows validou online, mas CRLs públicas não foram obtidas para pyHanko. Manter bloqueio."
        )
    return {
        "windows_chains_valid": True,
        "crls_downloaded": downloaded,
        "crls_unavailable": unavailable,
        "private_key_used": False,
    }
