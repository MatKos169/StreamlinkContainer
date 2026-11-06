"""Checks of the Twitch credentials: the app credentials (client id/secret) and the
optional OAuth token used for playback (TWITCH_OAUTH_TOKEN).

The OAuth token is checked with Twitch's official validate endpoint. Results never
contain the token or the secret, only a state and the account name.
"""
import datetime
import logging
import threading
import time
from dataclasses import asdict, dataclass

logger = logging.getLogger(__name__)

VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
VALIDATE_TIMEOUT = 15
EXPIRY_WARNING_SECONDS = 7 * 86400

NOT_CONFIGURED = "not_configured"
VALID = "valid"
INVALID = "invalid"
UNKNOWN = "unknown"      # could not check (network, Twitch down); last known state is kept for decisions


@dataclass
class CheckResult:
    state: str
    login: str = None
    expires_at: float = None     # unix time, None = no expiry reported
    detail: str = None           # error type only, never a message
    checked_at: float = None

    def as_status(self, now=None):
        now = time.time() if now is None else now
        data = asdict(self)
        data["expires_in"] = int(self.expires_at - now) if self.expires_at else None
        data["expiring_soon"] = bool(self.expires_at and self.expires_at - now < EXPIRY_WARNING_SECONDS)
        for key in ("checked_at", "expires_at"):
            if data[key]:
                data[key] = datetime.datetime.fromtimestamp(data[key], datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return data


def validate_oauth_token(token, http_get=None, now=None):
    """Check a Twitch OAuth (browser auth-token) with GET https://id.twitch.tv/oauth2/validate."""
    now = time.time() if now is None else now
    if not token:
        return CheckResult(NOT_CONFIGURED, checked_at=now)
    if http_get is None:
        import requests
        http_get = requests.get
    try:
        response = http_get(VALIDATE_URL, headers={"Authorization": f"OAuth {token}"}, timeout=VALIDATE_TIMEOUT)
    except Exception as error:
        return CheckResult(UNKNOWN, detail=type(error).__name__, checked_at=now)
    if response.status_code == 401:
        return CheckResult(INVALID, detail="HTTP 401", checked_at=now)
    if response.status_code != 200:
        return CheckResult(UNKNOWN, detail=f"HTTP {response.status_code}", checked_at=now)
    try:
        data = response.json()
    except ValueError:
        return CheckResult(UNKNOWN, detail="invalid response", checked_at=now)
    expires_in = data.get("expires_in")
    return CheckResult(VALID, login=data.get("login"),
                       expires_at=now + expires_in if isinstance(expires_in, (int, float)) and expires_in > 0 else None,
                       checked_at=now)


def validate_app_credentials(twitch_client, now=None):
    """Check client id/secret by getting an app access token."""
    from .twitch import error_summary
    now = time.time() if now is None else now
    try:
        twitch_client.authenticate()
        return CheckResult(VALID, checked_at=now)
    except Exception as error:
        detail = error_summary(error)
        state = INVALID if type(error).__name__ in ("TwitchAuthorizationException", "UnauthorizedException") else UNKNOWN
        return CheckResult(state, detail=detail, checked_at=now)


class OAuthChecker:
    """Runs the OAuth check in a background thread so a slow Twitch never blocks the recorder loop."""

    def __init__(self, token, http_get=None):
        self.token = token
        self.http_get = http_get
        self.result = CheckResult(NOT_CONFIGURED) if not token else CheckResult(UNKNOWN)
        self.known = None          # last VALID/INVALID result; decides while the state is UNKNOWN
        self._thread = None
        self._lock = threading.Lock()

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return False
        self._thread = threading.Thread(target=self._run, name="oauth-check", daemon=True)
        self._thread.start()
        return True

    def _run(self):
        result = validate_oauth_token(self.token, self.http_get)
        with self._lock:
            self.result = result
            if result.state in (VALID, INVALID):
                self.known = result
        level = logging.ERROR if result.state == INVALID else logging.WARNING if result.state == UNKNOWN else logging.INFO
        logger.log(level, "Twitch OAuth token: %s%s%s", result.state,
                   f" ({result.login})" if result.login else "", f" ({result.detail})" if result.detail else "")

    def is_invalid(self):
        """True if the token is known to be invalid (an UNKNOWN check keeps the last known state)."""
        with self._lock:
            return self.known is not None and self.known.state == INVALID
