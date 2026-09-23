import hashlib
from collections.abc import Mapping
from typing import Literal

import httpx

from robottraderslab.exchanges import SharedRequestWindow, SharedVenueWindow

from .common import parse_retry_after, webhook_id

type DiscordTier = Literal["global", "webhook"]

GLOBAL_TIER: DiscordTier = "global"
WEBHOOK_TIER: DiscordTier = "webhook"

_BUCKET_SCOPE = "discord-buckets"
_GLOBAL_SCOPE = "discord-global"
_GLOBAL_REQUESTS_PER_SECOND = 50
_PACE_FRACTION = 0.95
_GLOBAL_CAP = int(_GLOBAL_REQUESTS_PER_SECOND * _PACE_FRACTION)
_KEY_LENGTH = 16
_REMAINING_HEADER = "X-RateLimit-Remaining"
_RESET_HEADER = "X-RateLimit-Reset"
_RESET_AFTER_HEADER = "X-RateLimit-Reset-After"
_SCOPE_HEADER = "X-RateLimit-Scope"
_GLOBAL_HEADER = "X-RateLimit-Global"
_MARKED_TRUE = "true"


class WebhookRateLimiter:
    """Paces posts at the tiers Discord enforces on a webhook.

    Discord publishes no figure for a webhook's route and serves the whole
    budget in the headers of each response, so every route figure this client
    paces from is one the venue announced. It publishes one figure, 50
    requests a second, charged to the address a post leaves from because a
    webhook post carries no authorization.

    Both tiers therefore reach beyond the process: every process on the
    machine posting to one webhook draws on that webhook's route budget, and
    every process posting to any webhook draws on the address's second. Each
    lives in machine-shared state, keyed by a digest of the webhook's public
    id so its token never reaches a state file.

    A refusal names the tier it came from, and holding the tier it names
    leaves the others free: an address held for a global refusal, and one
    webhook alone for a refusal about the webhook or the channel behind it.
    """

    def __init__(self) -> None:
        self._routes = SharedVenueWindow(_BUCKET_SCOPE)
        self._address = SharedRequestWindow(
            _GLOBAL_SCOPE, max_per_second={GLOBAL_TIER: _GLOBAL_CAP}
        )

    async def hold(self, webhook_url: str, response: httpx.Response) -> DiscordTier:
        """Keep the delay a refusal asks for, at the tier it names.

        Returns:
            The tier held, which a caller reports as what stopped its post.
        """
        retry_after = parse_retry_after(response)
        if _refuses_the_address(response):
            await self._routes.hold(GLOBAL_TIER, retry_after)
            return GLOBAL_TIER
        await self._routes.hold(_route_key(webhook_url), retry_after)
        return WEBHOOK_TIER

    async def record(self, webhook_url: str, headers: Mapping[str, str]) -> None:
        """Take in the route budget a response announced.

        A response announces a count, a window and a reset together, so one
        that carries less than the three says nothing about the route.
        """
        remaining = headers.get(_REMAINING_HEADER)
        window = headers.get(_RESET_HEADER)
        reset_after = headers.get(_RESET_AFTER_HEADER)
        if remaining is None or window is None or reset_after is None:
            return
        await self._routes.record(
            _route_key(webhook_url),
            remaining=int(remaining),
            window=window,
            reset_after=float(reset_after),
        )

    async def wait(self, webhook_url: str) -> None:
        """Return once every tier a post is charged against has room for it."""
        await self._routes.wait(GLOBAL_TIER)
        await self._routes.wait(_route_key(webhook_url))
        await self._address.wait(GLOBAL_TIER)


def _refuses_the_address(response: httpx.Response) -> bool:
    """Discord marks a refusal charged to the whole address twice, and a
    refusal about one resource carries neither mark.
    """
    return bool(
        response.headers.get(_SCOPE_HEADER) == GLOBAL_TIER
        or response.headers.get(_GLOBAL_HEADER) == _MARKED_TRUE
    )


def _route_key(webhook_url: str) -> str:
    return hashlib.sha256(webhook_id(webhook_url).encode()).hexdigest()[:_KEY_LENGTH]
