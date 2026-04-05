"""
Certificate utilities for HTTPS traffic test server.
Generates a self-signed certificate dynamically using the cryptography library.
"""

from __future__ import annotations

import datetime
import ipaddress
import tempfile
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def generate_self_signed_cert(
    common_name: str = "traffic-test",
    valid_days: int = 365,
) -> tuple[Path, Path]:
    """
    Generate a self-signed RSA certificate and private key.
    Writes them to temporary files and returns (cert_path, key_path).
    The caller is responsible for deleting the files when done.
    """
    # Generate private key
    key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    # Build subject / issuer
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "TrafficTest"),
    ])

    now = datetime.datetime.utcnow()

    # Build SAN: try to add IP SAN if common_name looks like an IP
    san_list: list[x509.GeneralName] = [x509.DNSName(common_name)]
    try:
        san_list.append(x509.IPAddress(ipaddress.ip_address(common_name)))
    except ValueError:
        pass
    # Always add localhost / 127.0.0.1 for convenience
    san_list += [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
    ]

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=valid_days))
        .add_extension(
            x509.SubjectAlternativeName(san_list),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )

    # Write to temp files (delete=False; caller deletes)
    cert_file = tempfile.NamedTemporaryFile(
        suffix=".pem", prefix="traffic_test_cert_", delete=False
    )
    key_file = tempfile.NamedTemporaryFile(
        suffix=".pem", prefix="traffic_test_key_", delete=False
    )

    cert_file.write(cert.public_bytes(serialization.Encoding.PEM))
    cert_file.flush()
    cert_file.close()

    key_file.write(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    key_file.flush()
    key_file.close()

    return Path(cert_file.name), Path(key_file.name)
