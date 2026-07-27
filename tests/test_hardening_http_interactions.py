import io
import stat

import pytest

from tests.async_utils import async_test
from pk_botcore import http
from pk_botcore.interactions import InteractionLogger


class Content:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            yield chunk


class Response:
    def __init__(self, *, status=200, headers=None, chunks=()):
        self.status = status
        self.headers = headers or {}
        self.content = Content(chunks)
        self.connection = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError("http failure")


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append((url, kwargs))
        return next(self.responses)


@async_test
async def test_image_fetch_rejects_private_literal_without_request(monkeypatch):
    async def should_not_get_session():
        raise AssertionError("network should not be reached")

    monkeypatch.setattr(http, "get_http_session", should_not_get_session)
    assert await http.get_image_data("http://127.0.0.1/secret.png") is None


@async_test
async def test_image_fetch_streams_public_image_with_limit(monkeypatch):
    session = Session(
        [
            Response(
                headers={"Content-Type": "image/png", "Content-Length": "6"},
                chunks=[b"abc", b"def"],
            )
        ]
    )

    async def get_session():
        return session

    monkeypatch.setattr(http, "get_http_session", get_session)
    result = await http.get_image_data(
        "https://8.8.8.8/path/picture.png?token=x",
        max_bytes=6,
    )
    assert result["content"].read() == b"abcdef"
    assert result["filename"] == "picture.png"
    assert session.urls[0][1]["allow_redirects"] is False


@async_test
async def test_image_fetch_pins_validated_dns_address(monkeypatch):
    session = Session(
        [Response(headers={"Content-Type": "image/png"}, chunks=[b"x"])]
    )

    async def get_session():
        return session

    async def validate(_url):
        return ["8.8.8.8"]

    monkeypatch.setattr(http, "get_http_session", get_session)
    monkeypatch.setattr(http, "_validate_public_url", validate)

    result = await http.get_image_data("https://images.example/picture.png")

    assert result is not None
    requested_url, kwargs = session.urls[0]
    assert requested_url == "https://8.8.8.8/picture.png"
    assert kwargs["headers"]["Host"] == "images.example"
    assert kwargs["server_hostname"] == "images.example"


@async_test
@pytest.mark.parametrize(
    "response",
    [
        Response(headers={"Content-Type": "text/html"}, chunks=[b"not image"]),
        Response(headers={"Content-Type": "image/png"}, chunks=[b"123", b"456"]),
    ],
)
async def test_image_fetch_rejects_wrong_type_and_streamed_oversize(monkeypatch, response):
    async def get_session():
        return Session([response])

    monkeypatch.setattr(http, "get_http_session", get_session)
    assert (
        await http.get_image_data("https://8.8.8.8/image", max_bytes=5)
        is None
    )


@async_test
async def test_image_fetch_revalidates_redirect_target(monkeypatch):
    session = Session(
        [Response(status=302, headers={"Location": "http://10.0.0.1/private.png"})]
    )

    async def get_session():
        return session

    monkeypatch.setattr(http, "get_http_session", get_session)
    assert await http.get_image_data("https://8.8.8.8/start") is None
    assert len(session.urls) == 1


def test_interaction_logs_use_private_modes_and_rotate(tmp_path):
    path = tmp_path / "private" / "interactions.jsonl"
    interaction_logger = InteractionLogger(
        "test",
        path,
        max_bytes=350,
        backup_count=2,
    )
    for index in range(8):
        interaction_logger.log_custom(
            "event",
            channel_id=1,
            data={"payload": f"{index}-" + ("x" * 120)},
        )

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.with_name(path.name + ".1").exists()
    assert not path.with_name(path.name + ".3").exists()
    for candidate in path.parent.glob("interactions.jsonl*"):
        assert stat.S_IMODE(candidate.stat().st_mode) == 0o600


def test_explicit_existing_log_parent_keeps_its_mode(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o750)
    path = parent / "interactions.jsonl"

    InteractionLogger("test", path)

    assert stat.S_IMODE(parent.stat().st_mode) == 0o750
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
