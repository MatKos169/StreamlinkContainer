"""HTTPS for the web UI: a self-signed certificate, created on first start.

The certificate and key live in /config/tls (key file mode 0600). That folder
is never served by the web UI. A certificate that expires within RENEW_DAYS or
lacks a requested host name (TLS_HOSTNAMES) is replaced. With TLS_CERT_FILE /
TLS_KEY_FILE an own certificate (e.g. from an own CA) is used as is.
"""
import datetime
import ipaddress
import os
import socket
import ssl
import tempfile

VALID_DAYS = 3650
RENEW_DAYS = 30
COMMON_NAME = "streamlink-recorder"


def _san_entries(hostnames):
    from cryptography import x509
    names, seen = [], set()
    for host in ["localhost", "127.0.0.1", "::1", socket.gethostname(), *hostnames]:
        host = (host or "").strip()
        if not host or host.lower() in seen:
            continue
        seen.add(host.lower())
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            names.append(x509.DNSName(host))
    return names


def _write_private(path, data, mode):
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=folder)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def generate(cert_path, key_path, hostnames=(), now=None):
    """Write a new self-signed certificate (ECDSA P-256) and key."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = now or datetime.datetime.now(datetime.timezone.utc)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, COMMON_NAME)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(_san_entries(hostnames)), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    _write_private(key_path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()), 0o600)
    _write_private(cert_path, cert.public_bytes(serialization.Encoding.PEM), 0o644)


def needs_renewal(cert_path, hostnames=(), now=None):
    """True if the certificate is missing, unreadable, expiring or lacks a requested host name."""
    from cryptography import x509
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
    except (OSError, ValueError):
        return True
    if cert.not_valid_after_utc - now < datetime.timedelta(days=RENEW_DAYS):
        return True
    try:
        present = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return True
    have = {str(v).lower() for v in present.get_values_for_type(x509.DNSName)}
    have |= {str(v) for v in present.get_values_for_type(x509.IPAddress)}
    for host in hostnames:
        host = host.strip()
        if not host:
            continue
        try:
            wanted = str(ipaddress.ip_address(host))
        except ValueError:
            wanted = host.lower()
        if wanted not in have:
            return True
    return False


def fingerprint(cert_path):
    """SHA-256 fingerprint as shown by browsers (AA:BB:...)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    with open(cert_path, "rb") as f:
        digest = x509.load_pem_x509_certificate(f.read()).fingerprint(hashes.SHA256())
    return ":".join(f"{b:02X}" for b in digest)


def prepare(settings):
    """Make sure a certificate exists; returns (cert_path, key_path, generated)."""
    cert, key = settings.tls_cert_file, settings.tls_key_file
    if settings.tls_custom:
        if not (os.path.isfile(cert) and os.path.isfile(key)):
            raise FileNotFoundError("TLS_CERT_FILE / TLS_KEY_FILE not found")
        return cert, key, False
    if needs_renewal(cert, settings.tls_hostnames) or not os.path.isfile(key):
        generate(cert, key, settings.tls_hostnames)
        return cert, key, True
    return cert, key, False


def server_context(cert_path, key_path):
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_path, key_path)
    return context
