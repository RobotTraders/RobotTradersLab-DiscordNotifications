import httpx
import pytest
from robottraderslab_discord_notifications.webhook.rate_limiter import (
    GLOBAL_TIER,
    WEBHOOK_TIER,
    WebhookRateLimiter,
)

WEBHOOK_URL = "https://discord.com/api/webhooks/111111111/first-token"
OTHER_WEBHOOK_URL = "https://discord.com/api/webhooks/222222222/second-token"
ROTATED_TOKEN_URL = "https://discord.com/api/webhooks/111111111/rotated-token"
WINDOW = "1470173023"
RESET_AFTER = 1.0
RETRY_AFTER = 5.0
PACED_GLOBAL_PER_SECOND = 47


@pytest.fixture
def limiter() -> WebhookRateLimiter:
    return WebhookRateLimiter()


def _route_headers(remaining: int) -> dict[str, str]:
    return {
        "X-RateLimit-Limit": "5",
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": WINDOW,
        "X-RateLimit-Reset-After": str(RESET_AFTER),
        "X-RateLimit-Bucket": "abcd1234",
    }


def _refusal(scope: str | None) -> httpx.Response:
    headers = {"Retry-After": str(RETRY_AFTER)}
    if scope is not None:
        headers["X-RateLimit-Scope"] = scope
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


async def test_the_announced_route_budget_bounds_the_posts(limiter, virtual_sleeps):
    await limiter.record(WEBHOOK_URL, _route_headers(remaining=1))
    await limiter.wait(WEBHOOK_URL)

    await limiter.wait(WEBHOOK_URL)

    virtual_sleeps.assert_called_once_with(pytest.approx(RESET_AFTER))


async def test_a_route_the_venue_has_not_answered_for_is_not_paced(
    limiter, virtual_sleeps
):
    await limiter.wait(WEBHOOK_URL)
    await limiter.wait(WEBHOOK_URL)

    virtual_sleeps.assert_not_called()


@pytest.mark.parametrize(
    "missing_header",
    ["X-RateLimit-Remaining", "X-RateLimit-Reset", "X-RateLimit-Reset-After"],
)
async def test_an_answer_short_of_a_header_says_nothing_about_the_route(
    missing_header, limiter, virtual_sleeps
):
    headers = _route_headers(remaining=0)
    del headers[missing_header]

    await limiter.record(WEBHOOK_URL, headers)
    await limiter.wait(WEBHOOK_URL)

    virtual_sleeps.assert_not_called()


async def test_two_webhooks_draw_on_their_own_route_budgets(limiter, virtual_sleeps):
    await limiter.record(WEBHOOK_URL, _route_headers(remaining=1))
    await limiter.wait(WEBHOOK_URL)

    await limiter.wait(OTHER_WEBHOOK_URL)

    virtual_sleeps.assert_not_called()


async def test_limiters_on_one_webhook_draw_on_one_route_budget(virtual_sleeps):
    await WebhookRateLimiter().record(WEBHOOK_URL, _route_headers(remaining=1))
    await WebhookRateLimiter().wait(WEBHOOK_URL)

    await WebhookRateLimiter().wait(WEBHOOK_URL)

    virtual_sleeps.assert_called_once_with(pytest.approx(RESET_AFTER))


async def test_a_rotated_token_draws_on_the_webhooks_own_route_budget(
    limiter, virtual_sleeps
):
    await limiter.record(WEBHOOK_URL, _route_headers(remaining=1))
    await limiter.wait(WEBHOOK_URL)

    await limiter.wait(ROTATED_TOKEN_URL)

    virtual_sleeps.assert_called_once_with(pytest.approx(RESET_AFTER))


async def test_the_webhooks_token_stays_out_of_the_shared_state(limiter, tmp_path):
    await limiter.record(WEBHOOK_URL, _route_headers(remaining=1))
    await limiter.wait(WEBHOOK_URL)

    shared_state = [path.read_text() for path in tmp_path.rglob("*.json")]

    assert shared_state
    assert "first-token" not in "".join(shared_state)


async def test_a_refusal_charged_to_the_address_holds_every_webhook(
    limiter, virtual_sleeps
):
    held_tier = await limiter.hold(WEBHOOK_URL, _refusal(GLOBAL_TIER))

    await limiter.wait(OTHER_WEBHOOK_URL)

    assert held_tier == GLOBAL_TIER
    virtual_sleeps.assert_called_once_with(pytest.approx(RETRY_AFTER))


@pytest.mark.parametrize("scope", ["user", "shared", None])
async def test_a_refusal_about_one_resource_holds_that_webhook_alone(
    scope, limiter, virtual_sleeps
):
    held_tier = await limiter.hold(WEBHOOK_URL, _refusal(scope))

    await limiter.wait(OTHER_WEBHOOK_URL)
    await limiter.wait(WEBHOOK_URL)

    assert held_tier == WEBHOOK_TIER
    virtual_sleeps.assert_called_once_with(pytest.approx(RETRY_AFTER))


async def test_a_refusal_stating_its_delay_in_a_header_alone_is_kept(
    limiter, virtual_sleeps
):
    header_only = httpx.Response(
        httpx.codes.TOO_MANY_REQUESTS, headers={"Retry-After": str(RETRY_AFTER)}
    )

    await limiter.hold(WEBHOOK_URL, header_only)
    await limiter.wait(WEBHOOK_URL)

    virtual_sleeps.assert_called_once_with(pytest.approx(RETRY_AFTER))


async def test_one_second_bounds_the_posts_to_every_webhook_together(
    limiter, virtual_sleeps
):
    for _ in range(PACED_GLOBAL_PER_SECOND):
        await limiter.wait(WEBHOOK_URL)

    await limiter.wait(OTHER_WEBHOOK_URL)

    virtual_sleeps.assert_called_once()
