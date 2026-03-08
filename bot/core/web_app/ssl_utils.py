"""
web_app/ssl_utils.py  –  SSL context factory
"""
from __future__ import annotations
import logging, os, ssl
from pathlib import Path

log = logging.getLogger("web.ssl")


def get_ssl_context() -> ssl.SSLContext:
    cert_file = os.getenv("SSL_CERT", "certs/localhost.pem")
    key_file  = os.getenv("SSL_KEY",  "certs/localhost-key.pem")
    if not os.path.isfile(cert_file) or not os.path.isfile(key_file):
        log.info("[SSL] Generiere selbstsigniertes Zertifikat...")
        _generate_self_signed(cert_file, key_file)
        log.info(f"[SSL] Zertifikat erstellt: {cert_file}")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_file, key_file)
    return ctx


def _generate_self_signed(cert_path: str, key_path: str) -> None:
    import subprocess, shutil
    Path(cert_path).parent.mkdir(parents=True, exist_ok=True)

    if shutil.which("openssl"):
        try:
            subprocess.run([
                "openssl", "req", "-x509", "-newkey", "rsa:2048",
                "-keyout", key_path, "-out", cert_path,
                "-days", "3650", "-nodes", "-subj", "/CN=localhost",
                "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost",
            ], check=True, capture_output=True)
            return
        except subprocess.CalledProcessError:
            pass

    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import datetime, ipaddress

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject).issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
            .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            ]), critical=False)
            .sign(key, hashes.SHA256())
        )
        Path(key_path).write_bytes(
            key.private_bytes(serialization.Encoding.PEM,
                              serialization.PrivateFormat.TraditionalOpenSSL,
                              serialization.NoEncryption()))
        Path(cert_path).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return
    except ImportError:
        pass

    raise RuntimeError(
        "Konnte kein SSL-Zertifikat erstellen.\n"
        "Lösung A: openssl installieren\n"
        "Lösung B: pip install cryptography\n"
        "Lösung C: SSL_CERT / SSL_KEY in .env setzen"
    )