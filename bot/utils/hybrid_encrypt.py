"""
bot/utils/hybrid_encrypt.py
============================
Hybrides Verschlüsselungssystem: RSA-OAEP + AES-256-GCM

Format der verschlüsselten Daten (pipe-separiertes Base64):
    <enc_aes_key_b64>|<aes_tag_b64>|<iv_b64>|<ciphertext_b64>

Dieses Format ist kompatibel mit der Browser-seitigen Verschlüsselung
(WebCrypto API) in voting_routes.py → _VOTE_TEMPLATE.

SERVER-SEITIGE VERWENDUNG (optional, z.B. für Batch-Tests):
    from bot.utils.hybrid_encrypt import encrypt, decrypt

    enc = encrypt("Hallo Welt", public_key_pem)
    plain = decrypt(enc, private_key_pem)
"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.asymmetric import rsa, padding as asym_padding
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


# ══════════════════════════════════════════════════════════════════════════════
# ENCRYPT
# ══════════════════════════════════════════════════════════════════════════════

def encrypt(plaintext: str, public_key_pem: str) -> str:
    """
    Verschlüsselt einen Klartext-String mit RSA-OAEP + AES-256-GCM.

    Args:
        plaintext:       Der zu verschlüsselnde Text (UTF-8).
        public_key_pem:  RSA Public Key im PEM-Format.

    Returns:
        Verschlüsselter String im Format:
        <enc_aes_key_b64>|<aes_tag_b64>|<iv_b64>|<ciphertext_b64>
    """
    # AES-256-GCM Key + IV generieren
    aes_key = AESGCM.generate_key(bit_length=256)
    iv      = os.urandom(12)   # 96-bit IV
    aesgcm  = AESGCM(aes_key)

    # Plaintext verschlüsseln
    # AESGCM.encrypt gibt ciphertext + 16-byte tag zurück (angehängt)
    ct_with_tag = aesgcm.encrypt(iv, plaintext.encode("utf-8"), None)
    ciphertext  = ct_with_tag[:-16]
    tag         = ct_with_tag[-16:]

    # AES Key mit RSA-OAEP verschlüsseln
    pub_key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
    enc_aes_key = pub_key.encrypt(
        aes_key,
        asym_padding.OAEP(
            mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )

    # Base64-kodiert und pipe-separiert
    def b64(data: bytes) -> str:
        return base64.b64encode(data).decode("ascii")

    return f"{b64(enc_aes_key)}|{b64(tag)}|{b64(iv)}|{b64(ciphertext)}"


# ══════════════════════════════════════════════════════════════════════════════
# DECRYPT
# ══════════════════════════════════════════════════════════════════════════════

def decrypt(
    encrypted: str,
    private_key_pem: str,
    password: str | None = None,
) -> str:
    """
    Entschlüsselt einen mit `encrypt()` verschlüsselten String.

    Args:
        encrypted:        Der verschlüsselte String (pipe-separiert).
        private_key_pem:  RSA Private Key im PEM-Format (PKCS#8).
        password:         Optionales Passwort wenn der Key passwortgeschützt ist.

    Returns:
        Entschlüsselter Klartext-String (UTF-8).

    Raises:
        ValueError:  Wenn das Format ungültig oder die Entschlüsselung fehlschlägt.
    """
    parts = encrypted.strip().split("|")
    if len(parts) != 4:
        raise ValueError(f"Ungültiges Format: erwartet 4 Teile, erhalten {len(parts)}")

    enc_aes_key_b64, tag_b64, iv_b64, ciphertext_b64 = parts

    def b64d(s: str) -> bytes:
        return base64.b64decode(s.encode("ascii"))

    enc_aes_key = b64d(enc_aes_key_b64)
    tag         = b64d(tag_b64)
    iv          = b64d(iv_b64)
    ciphertext  = b64d(ciphertext_b64)

    # Private Key laden
    pw_bytes = password.encode("utf-8") if password else None
    prv_key  = serialization.load_pem_private_key(
        private_key_pem.encode("utf-8"),
        password=pw_bytes,
    )

    # AES Key entschlüsseln
    aes_key = prv_key.decrypt(
        enc_aes_key,
        asym_padding.OAEP(
            mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )

    # Ciphertext + Tag zusammenführen und mit AES-GCM entschlüsseln
    aesgcm      = AESGCM(aes_key)
    ct_with_tag = ciphertext + tag
    plaintext   = aesgcm.decrypt(iv, ct_with_tag, None)

    return plaintext.decode("utf-8")


# ══════════════════════════════════════════════════════════════════════════════
# KEY GENERATION HELPER (für Tests / CLI)
# ══════════════════════════════════════════════════════════════════════════════

def generate_keypair(key_size: int = 4096, password: str | None = None) -> tuple[str, str]:
    """
    Generiert ein RSA-Schlüsselpaar.

    Args:
        key_size:  Schlüsselgröße in Bit (2048 oder 4096).
        password:  Optionales Passwort für den privaten Schlüssel.

    Returns:
        (private_key_pem, public_key_pem) als Strings.
    """
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=key_size,
    )

    if password:
        enc = serialization.BestAvailableEncryption(password.encode("utf-8"))
    else:
        enc = serialization.NoEncryption()

    prv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=enc,
    ).decode("utf-8")

    pub_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")

    return prv_pem, pub_pem


# ══════════════════════════════════════════════════════════════════════════════
# CLI USAGE (python -m bot.utils.hybrid_encrypt)
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import json
    import sys

    print("🔐 Hybrid Encrypt – Selbsttest\n")

    # 1) Schlüssel generieren
    print("Generiere 2048-bit Schlüsselpaar (für Test)…")
    prv, pub = generate_keypair(2048)
    print("✅ Schlüssel generiert\n")

    # 2) Testnachricht verschlüsseln
    msg = json.dumps({"Frage 1": "Sehr gut", "Frage 2": [1, 3], "Frage 3": "Test-Antwort"})
    print(f"Klartext: {msg}")
    enc = encrypt(msg, pub)
    print(f"Verschlüsselt: {enc[:60]}…\n")

    # 3) Entschlüsseln
    decrypted = decrypt(enc, prv)
    print(f"Entschlüsselt: {decrypted}")

    ok = decrypted == msg
    print(f"\n{'✅ Selbsttest erfolgreich!' if ok else '❌ Selbsttest FEHLGESCHLAGEN'}")
    sys.exit(0 if ok else 1)