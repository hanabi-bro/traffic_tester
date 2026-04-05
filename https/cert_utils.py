"""
cryptography ライブラリを使用して自己署名証明書を動的生成する。
"""
from __future__ import annotations

import datetime
import ipaddress
import os
import tempfile

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def generate_self_signed_cert(
    common_name: str = "localhost",
) -> tuple[str, str]:
    """
    自己署名証明書と秘密鍵を生成し、一時ファイルに書き出す。
    返り値: (cert_path, key_path)
    """
    # 秘密鍵生成
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    # 証明書の Subject / Issuer
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ])

    # SAN（Subject Alternative Names）
    san_list: list[x509.GeneralName] = [x509.DNSName(common_name)]
    try:
        san_list.append(x509.IPAddress(ipaddress.ip_address(common_name)))
    except ValueError:
        pass  # hostname の場合は IP SAN なし

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow())
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(san_list), critical=False)
        .sign(key, hashes.SHA256())
    )

    # 一時ファイルへ書き出し
    fd_cert, cert_path = tempfile.mkstemp(suffix=".pem", prefix="traffic_cert_")
    fd_key, key_path = tempfile.mkstemp(suffix=".pem", prefix="traffic_key_")

    with os.fdopen(fd_cert, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    with os.fdopen(fd_key, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))

    return cert_path, key_path
