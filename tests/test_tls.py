import datetime
import os
import socket
import ssl
import stat

import pytest
from aiohttp import ClientSession, web

from app import tls
from app.settings import SettingsError, load_settings
from app.web import create_app

from fakes import make_settings

NOW = datetime.datetime(2026, 11, 6, tzinfo=datetime.timezone.utc)


def paths(tmp_path):
    return str(tmp_path / "tls" / "cert.pem"), str(tmp_path / "tls" / "key.pem")


def sans(cert_path):
    from cryptography import x509
    cert = x509.load_pem_x509_certificate(open(cert_path, "rb").read())
    ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return {str(v) for v in ext.get_values_for_type(x509.DNSName)} | {str(v) for v in ext.get_values_for_type(x509.IPAddress)}


def test_generate_writes_cert_and_private_key(tmp_path):
    cert, key = paths(tmp_path)
    tls.generate(cert, key, ["10.0.0.196", "streamlink.local"], now=NOW)
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o600
    assert {"localhost", "127.0.0.1", "10.0.0.196", "streamlink.local"} <= sans(cert)
    assert len(tls.fingerprint(cert).split(":")) == 32
    assert sorted(os.listdir(tmp_path / "tls")) == ["cert.pem", "key.pem"]   # no temp files left
    tls.server_context(cert, key)                                           # key matches cert


def test_needs_renewal(tmp_path):
    cert, key = paths(tmp_path)
    assert tls.needs_renewal(cert)                                          # missing
    tls.generate(cert, key, ["10.0.0.196"], now=NOW)
    assert not tls.needs_renewal(cert, ["10.0.0.196"], now=NOW)
    assert tls.needs_renewal(cert, ["10.0.0.197"], now=NOW)                 # new host name
    later = NOW + datetime.timedelta(days=tls.VALID_DAYS - tls.RENEW_DAYS + 1)
    assert tls.needs_renewal(cert, now=later)                               # about to expire
    open(cert, "w").write("garbage")
    assert tls.needs_renewal(cert)


def test_prepare_generates_once_and_keeps(tmp_path):
    settings = make_settings(tmp_path, tls_hostnames=["nas.local"])
    settings.tls_cert_file, settings.tls_key_file = paths(tmp_path)
    _, _, generated = tls.prepare(settings)
    first = tls.fingerprint(settings.tls_cert_file)
    _, _, generated_again = tls.prepare(settings)
    assert generated and not generated_again and tls.fingerprint(settings.tls_cert_file) == first


def test_prepare_custom_certificate_is_never_replaced(tmp_path):
    settings = make_settings(tmp_path, tls_custom=True)
    settings.tls_cert_file, settings.tls_key_file = paths(tmp_path)
    with pytest.raises(FileNotFoundError):
        tls.prepare(settings)
    tls.generate(settings.tls_cert_file, settings.tls_key_file, now=NOW - datetime.timedelta(days=tls.VALID_DAYS))
    before = open(settings.tls_cert_file).read()
    assert tls.prepare(settings)[2] is False
    assert open(settings.tls_cert_file).read() == before                    # even when expiring


def test_settings_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("TLS_HOSTNAMES", "10.0.0.196, nas.local ,")
    s = load_settings()
    assert (s.web_tls, s.tls_custom, s.tls_hostnames) == (True, False, ["10.0.0.196", "nas.local"])
    assert s.tls_cert_file == os.path.join(str(tmp_path), "tls", "cert.pem")
    monkeypatch.setenv("WEB_TLS", "false")
    assert load_settings().web_tls is False
    monkeypatch.setenv("TLS_CERT_FILE", "/x/cert.pem")
    with pytest.raises(SettingsError):
        load_settings()                                                     # key file missing
    monkeypatch.setenv("TLS_KEY_FILE", "/x/key.pem")
    s = load_settings()
    assert (s.tls_custom, s.tls_cert_file, s.tls_key_file) == (True, "/x/cert.pem", "/x/key.pem")


async def test_https_server_and_key_not_served(tmp_path):
    from app.settings import parse_networks
    settings = make_settings(tmp_path, allowed_networks=parse_networks("127.0.0.1"))
    settings.tls_cert_file, settings.tls_key_file = os.path.join(settings.config_dir, "tls", "cert.pem"), \
        os.path.join(settings.config_dir, "tls", "key.pem")
    cert, key, _ = tls.prepare(settings)
    runner = web.AppRunner(create_app(settings))
    await runner.setup()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    site = web.TCPSite(runner, "127.0.0.1", port, ssl_context=tls.server_context(cert, key))
    await site.start()
    try:
        trust = ssl.create_default_context(cafile=cert)                     # the self-signed cert verifies
        async with ClientSession() as client:
            async with client.get(f"https://localhost:{port}/api/settings", ssl=trust) as r:
                assert r.status == 200
            for path in ("/config/tls/key.pem", "/tls/key.pem", "/static/../../config/tls/key.pem", "/api/../config"):
                async with client.get(f"https://localhost:{port}{path}", ssl=trust) as r:
                    assert r.status == 404
                    assert "PRIVATE KEY" not in await r.text()
            with pytest.raises(Exception):
                async with client.get(f"http://localhost:{port}/"):          # plain HTTP is not answered
                    pass
    finally:
        await runner.cleanup()
