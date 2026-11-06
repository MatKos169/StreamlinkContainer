from types import SimpleNamespace

import pytest

from app.twitch import TwitchClient, error_summary


class FakeHelix:
    def __init__(self, streams):
        self.streams = streams
        self.calls = []

    async def get_streams(self, user_login, first):
        self.calls.append((list(user_login), first))
        for s in self.streams:
            if s.user_login in user_login:
                yield s


def stream(login, game="Just Chatting", game_id="509658", title="t", type_="live"):
    return SimpleNamespace(user_login=login, game_name=game, game_id=game_id, title=title, type=type_)


def test_get_live_batches_and_maps():
    client = TwitchClient("id", "secret")
    logins = [f"user{i}" for i in range(150)]
    client._twitch = FakeHelix([stream("user3"), stream("user120", game="", game_id=""), stream("user5", type_="")])
    live = client.get_live(logins)
    assert set(live) == {"user3", "user120"}
    assert live["user3"].category == "Just Chatting"
    assert [len(c[0]) for c in client._twitch.calls] == [100, 50]


def test_get_live_empty_does_not_call():
    client = TwitchClient("id", "secret")
    assert client.get_live([]) == {}


def test_failure_resets_client():
    class Broken:
        def get_streams(self, **kw):
            raise RuntimeError("boom")

    client = TwitchClient("id", "secret")
    client._twitch = Broken()
    with pytest.raises(RuntimeError):
        client.get_live(["a"])
    assert client._twitch is None


def test_error_summary_hides_message():
    assert error_summary(ValueError("client_secret=abc")) == "ValueError"
