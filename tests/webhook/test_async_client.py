from typing import Any

import httpx
import pytest
from robottraderslab_discord_notifications.webhook.async_client import (
    AsyncDiscordClient,
)
from robottraderslab_discord_notifications.webhook.rate_limiter import (
    GLOBAL_TIER,
    WEBHOOK_TIER,
    WebhookRateLimiter,
)

WEBHOOK_ID = "111111111"
WEBHOOK_URL = f"https://discord.com/api/webhooks/{WEBHOOK_ID}/first-token"
MAX_RETRIES = 2
RESET_AFTER = 1.0
RETRY_AFTER = 5.0


class FakeVenue:
    """Answers each post in turn and keeps to its last answer once they run
    out, so a test states only the answers it cares about.
    """

    def __init__(self, *answers: httpx.Response | Exception) -> None:
        self.answers = list(answers)
        self.posts: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.posts.append(request)
        answer = self.answers[min(len(self.posts), len(self.answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def sample_embed() -> dict[str, Any]:
    return {
        "title": "Test Order",
        "color": 0x00FF00,
        "fields": [{"name": "Symbol", "value": "BTC/USDT", "inline": True}],
    }


def _accepted(remaining: int = 5) -> httpx.Response:
    return httpx.Response(
        httpx.codes.NO_CONTENT,
        headers={
            "X-RateLimit-Limit": "5",
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": "1470173023",
            "X-RateLimit-Reset-After": str(RESET_AFTER),
            "X-RateLimit-Bucket": "abcd1234",
        },
    )


def _refused(scope: str) -> httpx.Response:
    headers = {"X-RateLimit-Scope": scope, "Retry-After": str(RETRY_AFTER)}
    if scope == GLOBAL_TIER:
        headers["X-RateLimit-Global"] = "true"
    return httpx.Response(
        httpx.codes.TOO_MANY_REQUESTS,
        headers=headers,
        json={
            "message": "You are being rate limited.",
            "retry_after": RETRY_AFTER,
            "global": scope == GLOBAL_TIER,
        },
    )


def _server_error() -> httpx.Response:
    return httpx.Response(
        httpx.codes.INTERNAL_SERVER_ERROR, json={"message": "Internal Server Error"}
    )


def _rejected() -> httpx.Response:
    return httpx.Response(
        httpx.codes.BAD_REQUEST, json={"message": "Cannot send an empty message"}
    )


def _client(venue: FakeVenue, bot_name: str | None = None) -> AsyncDiscordClient:
    return AsyncDiscordClient(
        WEBHOOK_URL,
        httpx.AsyncClient(transport=httpx.MockTransport(venue)),
        WebhookRateLimiter(),
        max_retries=MAX_RETRIES,
        retry_backoff_seconds=0.01,
        bot_name=bot_name,
    )


async def test_the_embed_travels_as_the_webhooks_only_embed(
    sample_embed, virtual_sleeps
):
    venue = FakeVenue(_accepted())

    await _client(venue).send(sample_embed)

    assert (
        venue.posts[0].read()
        == httpx.Request(
            "POST",
            WEBHOOK_URL,
            json={"embeds": [sample_embed]},
        ).read()
    )


async def test_a_stated_name_is_sent_with_the_embed(sample_embed, virtual_sleeps):
    venue = FakeVenue(_accepted())

    await _client(venue, bot_name="Envelope replace").send(sample_embed)

    assert b'"username":"Envelope replace"' in venue.posts[0].read()


async def test_no_name_leaves_the_webhook_its_own(sample_embed, virtual_sleeps):
    venue = FakeVenue(_accepted())

    await _client(venue).send(sample_embed)

    assert b"username" not in venue.posts[0].read()


async def test_the_announced_budget_paces_the_post_after_it(
    sample_embed, virtual_sleeps
):
    venue = FakeVenue(_accepted(remaining=0))
    client = _client(venue)
    await client.send(sample_embed)

    await client.send(sample_embed)

    virtual_sleeps.assert_called_once_with(pytest.approx(RESET_AFTER))


async def test_a_timeout_is_retried(sample_embed, virtual_sleeps):
    venue = FakeVenue(httpx.TimeoutException("timeout"), _accepted())

    await _client(venue).send(sample_embed)

    assert len(venue.posts) == 2


async def test_a_timeout_outlasting_the_retries_is_raised(sample_embed, virtual_sleeps):
    venue = FakeVenue(httpx.TimeoutException("timeout"))

    with pytest.raises(httpx.TimeoutException):
        await _client(venue).send(sample_embed)

    assert len(venue.posts) == MAX_RETRIES + 1


async def test_a_server_error_is_retried(sample_embed, virtual_sleeps):
    venue = FakeVenue(_server_error(), _accepted())

    await _client(venue).send(sample_embed)

    assert len(venue.posts) == 2


async def test_a_server_error_outlasting_the_retries_is_raised(
    sample_embed, virtual_sleeps
):
    venue = FakeVenue(_server_error())

    with pytest.raises(httpx.HTTPStatusError):
        await _client(venue).send(sample_embed)

    assert len(venue.posts) == MAX_RETRIES + 1


async def test_a_rejected_message_is_not_retried(sample_embed, virtual_sleeps):
    venue = FakeVenue(_rejected())

    with pytest.raises(httpx.HTTPStatusError):
        await _client(venue).send(sample_embed)

    assert len(venue.posts) == 1


async def test_a_refusal_is_retried_once_the_delay_it_asks_for_has_passed(
    sample_embed, virtual_sleeps
):
    venue = FakeVenue(_refused("user"), _accepted())

    await _client(venue).send(sample_embed)

    assert len(venue.posts) == 2
    virtual_sleeps.assert_called_once_with(pytest.approx(RETRY_AFTER))


@pytest.mark.parametrize(
    ("scope", "held_tier"),
    [(GLOBAL_TIER, GLOBAL_TIER), ("user", WEBHOOK_TIER), ("shared", WEBHOOK_TIER)],
)
async def test_a_refusal_outlasting_the_retries_names_the_webhook_and_the_tier(
    scope, held_tier, sample_embed, virtual_sleeps
):
    venue = FakeVenue(_refused(scope))

    with pytest.raises(httpx.HTTPStatusError) as refusal:
        await _client(venue).send(sample_embed)

    assert f"held the {held_tier} tier" in str(refusal.value)
    assert f"webhook {WEBHOOK_ID}" in str(refusal.value)
    assert f"{MAX_RETRIES + 1} attempts" in str(refusal.value)


async def test_the_stated_timeout_reaches_the_http_client():
    client = AsyncDiscordClient.from_webhook_url(WEBHOOK_URL, timeout_seconds=5.0)

    assert client._http_client.timeout.read == 5.0
