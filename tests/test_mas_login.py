import asyncio
import json

import aiohttp
import pytest

from src.mas_login import MasLoginError, MasLoginPermanentError, post_login_with_retry


class FakeResponse:
    def __init__(self, status, json_body=None, text_body="", json_exc=None, headers=None):
        self.status = status
        self._json = {} if json_body is None else json_body
        self._text = text_body
        self._json_exc = json_exc
        self.headers = headers or {}

    async def json(self):
        if self._json_exc is not None:
            raise self._json_exc
        return self._json

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class RaisingResponse:
    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if not self._responses:
            raise AssertionError("unexpected extra login attempt")
        return self._responses.pop(0)


def make_sleep(sleeps):
    async def _sleep(delay):
        sleeps.append(delay)

    return _sleep


def test_login_success_first_attempt():
    session = FakeSession([FakeResponse(200, {"access_token": "tok", "device_id": "DEV"})])
    sleeps = []
    payload = {"type": "m.login.password", "device_id": "DEV"}

    result = asyncio.run(
        post_login_with_retry(session, "https://mas.example/", payload, sleep=make_sleep(sleeps))
    )

    assert result == {"access_token": "tok", "device_id": "DEV"}
    assert len(session.calls) == 1
    assert session.calls[0]["url"] == "https://mas.example/_matrix/client/v3/login"
    assert session.calls[0]["json"] == payload
    assert session.calls[0]["timeout"] is not None
    assert sleeps == []


def test_retries_transient_503_then_succeeds():
    session = FakeSession(
        [
            FakeResponse(503, text_body="no available server"),
            FakeResponse(503, text_body="no available server"),
            FakeResponse(200, {"access_token": "tok"}),
        ]
    )
    sleeps = []

    result = asyncio.run(
        post_login_with_retry(
            session,
            "https://mas",
            {"type": "m.login.password"},
            max_attempts=5,
            base_delay=1.0,
            max_delay=60.0,
            sleep=make_sleep(sleeps),
            rand=lambda: 0.5,
        )
    )

    assert result == {"access_token": "tok"}
    assert len(session.calls) == 3
    assert sleeps == [0.75, 1.5]


def test_on_retry_callback_receives_attempt_and_error():
    session = FakeSession(
        [
            FakeResponse(503, text_body="no available server"),
            FakeResponse(503, text_body="still down"),
            FakeResponse(200, {"access_token": "tok"}),
        ]
    )
    sleeps = []
    seen = []

    def on_retry(attempt, exc):
        seen.append((attempt, type(exc).__name__))

    result = asyncio.run(
        post_login_with_retry(
            session,
            "https://mas",
            {},
            max_attempts=5,
            sleep=make_sleep(sleeps),
            rand=lambda: 0.5,
            on_retry=on_retry,
        )
    )

    assert result == {"access_token": "tok"}
    assert seen == [(1, "MasLoginError"), (2, "MasLoginError")]
    assert len(sleeps) == len(seen)


def test_404_is_retried_then_succeeds():
    session = FakeSession(
        [
            FakeResponse(404, text_body="route not registered yet"),
            FakeResponse(200, {"access_token": "tok"}),
        ]
    )
    sleeps = []

    result = asyncio.run(
        post_login_with_retry(
            session, "https://mas", {}, max_attempts=3, sleep=make_sleep(sleeps)
        )
    )

    assert result == {"access_token": "tok"}
    assert len(sleeps) == 1


def test_permanent_401_fails_fast_without_retrying():
    session = FakeSession([FakeResponse(401, text_body="invalid credentials")])
    sleeps = []

    with pytest.raises(MasLoginPermanentError):
        asyncio.run(
            post_login_with_retry(
                session, "https://mas", {}, max_attempts=5, sleep=make_sleep(sleeps)
            )
        )

    assert len(session.calls) == 1
    assert sleeps == []


def test_exhausted_retries_raise_transient_error():
    session = FakeSession([FakeResponse(503, text_body="no available server")] * 3)
    sleeps = []

    with pytest.raises(MasLoginError) as excinfo:
        asyncio.run(
            post_login_with_retry(
                session, "https://mas", {}, max_attempts=3, sleep=make_sleep(sleeps)
            )
        )

    assert not isinstance(excinfo.value, MasLoginPermanentError)
    assert "503" in str(excinfo.value)
    assert len(session.calls) == 3
    assert len(sleeps) == 2


def test_retries_network_error():
    session = FakeSession(
        [
            RaisingResponse(aiohttp.ClientError("connection reset")),
            FakeResponse(200, {"access_token": "tok"}),
        ]
    )
    sleeps = []

    result = asyncio.run(
        post_login_with_retry(
            session, "https://mas", {}, max_attempts=3, sleep=make_sleep(sleeps)
        )
    )

    assert result == {"access_token": "tok"}
    assert len(sleeps) == 1


def test_timeout_is_retried():
    session = FakeSession(
        [
            RaisingResponse(asyncio.TimeoutError()),
            FakeResponse(200, {"access_token": "tok"}),
        ]
    )
    sleeps = []

    result = asyncio.run(
        post_login_with_retry(
            session, "https://mas", {}, max_attempts=3, sleep=make_sleep(sleeps)
        )
    )

    assert result == {"access_token": "tok"}
    assert len(sleeps) == 1


def test_backoff_is_capped_at_max_delay():
    session = FakeSession([FakeResponse(503, text_body="no")] * 6)
    sleeps = []

    with pytest.raises(MasLoginError):
        asyncio.run(
            post_login_with_retry(
                session,
                "https://mas",
                {},
                max_attempts=6,
                base_delay=1.0,
                max_delay=5.0,
                sleep=make_sleep(sleeps),
                rand=lambda: 1.0,
            )
        )

    assert sleeps == [1.0, 2.0, 4.0, 5.0, 5.0]
    assert all(delay <= 5.0 for delay in sleeps)


def test_single_attempt_does_not_sleep():
    session = FakeSession([FakeResponse(503, text_body="no")])
    sleeps = []

    with pytest.raises(MasLoginError):
        asyncio.run(
            post_login_with_retry(
                session, "https://mas", {}, max_attempts=1, sleep=make_sleep(sleeps)
            )
        )

    assert len(session.calls) == 1
    assert sleeps == []


def test_malformed_json_200_is_retried_then_succeeds():
    session = FakeSession(
        [
            FakeResponse(200, json_exc=json.JSONDecodeError("bad body", "not json", 0)),
            FakeResponse(200, {"access_token": "tok"}),
        ]
    )
    sleeps = []

    result = asyncio.run(
        post_login_with_retry(
            session, "https://mas", {}, max_attempts=3, sleep=make_sleep(sleeps)
        )
    )

    assert result == {"access_token": "tok"}
    assert len(session.calls) == 2
    assert len(sleeps) == 1


def test_200_without_access_token_is_retried_then_succeeds():
    session = FakeSession(
        [
            FakeResponse(200, {"device_id": "DEV"}),
            FakeResponse(200, {"access_token": "tok"}),
        ]
    )
    sleeps = []

    result = asyncio.run(
        post_login_with_retry(
            session, "https://mas", {}, max_attempts=3, sleep=make_sleep(sleeps)
        )
    )

    assert result == {"access_token": "tok"}
    assert len(session.calls) == 2
    assert len(sleeps) == 1


def test_200_without_access_token_exhausts_retries_as_transient():
    session = FakeSession([FakeResponse(200, {"device_id": "DEV"})] * 2)
    sleeps = []

    with pytest.raises(MasLoginError) as excinfo:
        asyncio.run(
            post_login_with_retry(
                session, "https://mas", {}, max_attempts=2, sleep=make_sleep(sleeps)
            )
        )

    assert not isinstance(excinfo.value, MasLoginPermanentError)
    assert "access_token" in str(excinfo.value)
    assert len(session.calls) == 2
    assert len(sleeps) == 1


def test_retry_after_header_is_honoured():
    session = FakeSession(
        [
            FakeResponse(503, text_body="busy", headers={"Retry-After": "7"}),
            FakeResponse(200, {"access_token": "tok"}),
        ]
    )
    sleeps = []

    result = asyncio.run(
        post_login_with_retry(
            session,
            "https://mas",
            {},
            max_attempts=3,
            base_delay=1.0,
            max_delay=60.0,
            sleep=make_sleep(sleeps),
            rand=lambda: 0.5,
        )
    )

    assert result == {"access_token": "tok"}
    assert sleeps == [7.0]


def test_retry_after_header_is_capped_at_max_delay():
    session = FakeSession(
        [
            FakeResponse(503, text_body="busy", headers={"Retry-After": "999"}),
            FakeResponse(200, {"access_token": "tok"}),
        ]
    )
    sleeps = []

    result = asyncio.run(
        post_login_with_retry(
            session,
            "https://mas",
            {},
            max_attempts=3,
            base_delay=1.0,
            max_delay=5.0,
            sleep=make_sleep(sleeps),
        )
    )

    assert result == {"access_token": "tok"}
    assert sleeps == [5.0]


def test_invalid_max_attempts_raises():
    session = FakeSession([])

    with pytest.raises(ValueError):
        asyncio.run(
            post_login_with_retry(session, "https://mas", {}, max_attempts=0)
        )

    assert session.calls == []


def test_invalid_timeout_raises():
    session = FakeSession([])

    with pytest.raises(ValueError):
        asyncio.run(post_login_with_retry(session, "https://mas", {}, timeout=0))

    assert session.calls == []
