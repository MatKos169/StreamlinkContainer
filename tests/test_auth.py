import json
import os
import time

import pytest

from app import auth
from app.recorder import Supervisor
from app.streams import Options

from fakes import info, make_settings, make_stream, save_streams


class Response:
    def __init__(self, status, data=None):
        self.status_code, self._data = status, data

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


def getter(response=None, error=None, calls=None):
    def get(url, headers=None, timeout=None):
        if calls is not None:
            calls.append((url, headers))
        if error:
            raise error
        return response
    return get


def test_validate_oauth_token_states():
    calls = []
    ok = auth.validate_oauth_token("tok", getter(Response(200, {"login": "me", "expires_in": 3600}), calls=calls), now=1000)
    assert (ok.state, ok.login, ok.expires_at) == ("valid", "me", 4600)
    assert calls == [(auth.VALIDATE_URL, {"Authorization": "OAuth tok"})]
    assert auth.validate_oauth_token("tok", getter(Response(200, {"login": "me", "expires_in": 0}))).expires_at is None
    assert auth.validate_oauth_token("tok", getter(Response(401))).state == "invalid"
    assert auth.validate_oauth_token("tok", getter(Response(503))).state == "unknown"
    unknown = auth.validate_oauth_token("tok", getter(error=OSError("http://x?token=SECRET")))
    assert unknown.state == "unknown" and "SECRET" not in json.dumps(unknown.as_status())
    assert auth.validate_oauth_token(None).state == "not_configured"


def test_status_expiry_warning():
    r = auth.CheckResult("valid", login="me", expires_at=1000 + 3 * 86400, checked_at=1000)
    s = r.as_status(now=1000)
    assert s["expires_in"] == 3 * 86400 and s["expiring_soon"] is True
    assert s["checked_at"] == "1970-01-01T00:16:40Z"


def test_validate_app_credentials():
    class TwitchAuthorizationException(Exception):
        pass

    class Client:
        def __init__(self, error=None):
            self.error = error

        def authenticate(self):
            if self.error:
                raise self.error
    assert auth.validate_app_credentials(Client()).state == "valid"
    bad = auth.validate_app_credentials(Client(TwitchAuthorizationException("client_secret=SECRET")))
    assert bad.state == "invalid" and "SECRET" not in (bad.detail or "")
    assert auth.validate_app_credentials(Client(TimeoutError())).state == "unknown"


def test_unknown_check_keeps_last_known_state():
    responses = [Response(401), None]
    checker = auth.OAuthChecker("tok", http_get=lambda *a, **k: responses.pop(0) or (_ for _ in ()).throw(OSError()))
    checker.start(); checker._thread.join(5)
    assert checker.is_invalid()
    checker.start(); checker._thread.join(5)
    assert checker.result.state == "unknown" and checker.is_invalid()       # still treated as invalid


# -- supervisor --------------------------------------------------------------------

TOKEN = "SECRET-OAUTH-TOKEN-4711"


class FakeTwitch:
    def __init__(self, auth_error=None):
        self.auth_error, self.auth_calls, self.live = auth_error, 0, {}

    def authenticate(self):
        self.auth_calls += 1
        if self.auth_error:
            raise self.auth_error

    def get_live(self, logins):
        if self.auth_error:
            raise self.auth_error          # bad credentials fail every Helix call
        return {k: v for k, v in self.live.items() if k in logins}


class FakeRecording:
    created = []

    def __init__(self, stream, info, settings, options):
        self.stream, self.settings = stream, settings
        self.state, self.file, self.error, self.alive = "recording", "x.ts", None, True
        FakeRecording.created.append(self)

    def start(self):
        pass

    def is_alive(self):
        return self.alive

    def stop(self):
        self.alive = False

    def update(self, info):
        pass

    def join(self, timeout=None):
        pass


def supervisor(tmp_path, oauth_result, policy="without_token", twitch=None):
    FakeRecording.created = []
    settings = make_settings(tmp_path, oauth_token=TOKEN)
    a = make_stream("alpha")
    save_streams(settings.streams_file, [a], {"auth_invalid_policy": policy})
    clock = [1000.0]
    sup = Supervisor(settings, twitch or FakeTwitch(), recording_factory=FakeRecording, clock=lambda: clock[0])
    sup.oauth = auth.OAuthChecker(TOKEN, http_get=getter(oauth_result))
    return sup, settings, clock, a


def wait_check(sup):
    if sup.oauth._thread:
        sup.oauth._thread.join(5)


def test_invalid_token_records_without_token(tmp_path):
    sup, settings, clock, a = supervisor(tmp_path, Response(401))
    sup.step(); wait_check(sup)
    sup.twitch.live = {"alpha": info("alpha")}
    clock[0] += 60; sup.step()
    assert FakeRecording.created[0].settings.oauth_token is None              # token dropped, still recording
    st = json.load(open(settings.status_file))["auth"]
    assert st["oauth"]["state"] == "invalid" and st["oauth_known_invalid"] and not st["token_in_use"]
    assert TOKEN not in json.dumps(st)


def test_invalid_token_policy_skip(tmp_path):
    sup, settings, clock, a = supervisor(tmp_path, Response(401), policy="skip")
    sup.step(); wait_check(sup)
    sup.twitch.live = {"alpha": info("alpha")}
    clock[0] += 60; sup.step()
    assert FakeRecording.created == []
    assert json.load(open(settings.status_file))["auth"]["blocked"] is True


def test_valid_token_is_used_and_app_checked(tmp_path):
    sup, settings, clock, a = supervisor(tmp_path, Response(200, {"login": "me", "expires_in": 0}))
    sup.step(); wait_check(sup)
    assert sup.twitch.auth_calls == 1
    sup.twitch.live = {"alpha": info("alpha")}
    clock[0] += 60; sup.step()
    assert FakeRecording.created[0].settings.oauth_token == TOKEN
    st = json.load(open(settings.status_file))["auth"]
    assert (st["app"]["state"], st["oauth"]["state"], st["oauth"]["login"]) == ("valid", "valid", "me")


def test_checks_repeat_on_interval_and_on_command(tmp_path):
    sup, settings, clock, a = supervisor(tmp_path, Response(200, {"login": "me"}))
    sup.step(); wait_check(sup)
    clock[0] += 3599; sup.step()
    assert sup.twitch.auth_calls == 1
    clock[0] += 1; sup.step(); wait_check(sup)
    assert sup.twitch.auth_calls == 2                                         # hourly by default
    os.makedirs(os.path.join(settings.config_dir, "commands"), exist_ok=True)
    open(os.path.join(settings.config_dir, "commands", "check-auth"), "w").close()
    clock[0] += 1; sup.step(); wait_check(sup)
    assert sup.twitch.auth_calls == 3 and os.listdir(os.path.join(settings.config_dir, "commands")) == []


def test_invalid_app_credentials_reported(tmp_path):
    class TwitchAuthorizationException(Exception):
        pass
    sup, settings, clock, a = supervisor(tmp_path, Response(200, {}), twitch=FakeTwitch(TwitchAuthorizationException()))
    sup.step(); wait_check(sup)
    assert json.load(open(settings.status_file))["auth"]["app"]["state"] == "invalid"


def test_failed_recording_triggers_token_check(tmp_path):
    sup, settings, clock, a = supervisor(tmp_path, Response(200, {"login": "me"}))
    sup.step(); wait_check(sup)
    sup.twitch.live = {"alpha": info("alpha")}
    clock[0] += 60; sup.step()
    sup.oauth.http_get = getter(Response(401))                                # token revoked meanwhile
    rec = FakeRecording.created[0]
    rec.state, rec.alive = "error", False
    clock[0] += 1; sup.step(); wait_check(sup)
    assert sup.oauth.is_invalid()


def test_legacy_policy_values(monkeypatch, tmp_path):
    from app.settings import load_settings
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    for legacy in ("exit", "notify"):
        monkeypatch.setenv("authinvalidpolicy", legacy)
        assert load_settings().defaults.auth_invalid_policy == "skip"
    monkeypatch.setenv("TWITCH_AUTH_VALIDATION_INTERVAL", "100")
    with pytest.raises(Exception):
        load_settings()                                                       # below 300 s
