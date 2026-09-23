import asyncio
import logging
from typing import Any, Self

import httpx

from .common import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_BACKOFF_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    extract_error_message,
    webhook_id,
)
from .rate_limiter import WebhookRateLimiter

logger = logging.getLogger(__name__)


class AsyncDiscordClient:
    """Asynchronous client for sending embeds to Discord webhooks."""

    def __init__(
        self,
        webhook_url: str,
        http_client: httpx.AsyncClient,
        rate_limiter: WebhookRateLimiter,
        *,
        max_retries: int,
        retry_backoff_seconds: float,
        bot_name: str | None,
    ):
        """
        Args:
            rate_limiter: Paces every post this client makes, alongside
                whatever else on the machine posts to the same webhook.
            retry_backoff_seconds: Base delay that doubles with each retry
                attempt.
            bot_name: Name to post under, the webhook's own when left out.
        """
        self.webhook_url = webhook_url
        self._http_client = http_client
        self._rate_limiter = rate_limiter
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._bot_name = bot_name

    @classmethod
    def from_webhook_url(
        cls,
        webhook_url: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
        bot_name: str | None = None,
    ) -> Self:
        """Build a client posting to one webhook.

        Args:
            retry_backoff_seconds: Base delay that doubles with each retry
                attempt.
            bot_name: Name to post under, the webhook's own when left out.
        """
        return cls(
            webhook_url,
            httpx.AsyncClient(timeout=timeout_seconds),
            WebhookRateLimiter(),
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            bot_name=bot_name,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._http_client.aclose()

    async def send(self, embed: dict[str, Any]) -> None:
        """Every attempt claims its own room in the budget, since the venue
        counts a retry like any other request.

        Args:
            embed: One embed object of Discord's message schema.

        Raises:
            httpx.HTTPError: If the webhook rejects the request, holds its
                budget past the retries, or stays unreachable after them.
        """
        payload: dict[str, Any] = {"embeds": [embed]}
        if self._bot_name:
            payload["username"] = self._bot_name

        for attempt in range(self._max_retries + 1):
            await self._rate_limiter.wait(self.webhook_url)
            try:
                response = await self._http_client.post(self.webhook_url, json=payload)
                await self._rate_limiter.record(self.webhook_url, response.headers)

                if response.status_code == 429:
                    await self._hold_the_refused_tier(response, attempt)
                    continue

                if await self._handle_response(response, attempt):
                    continue
                return
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                if attempt < self._max_retries:
                    await self._wait_with_backoff(attempt, f"request failed: {e}")
                else:
                    raise

    async def _hold_the_refused_tier(
        self, response: httpx.Response, attempt: int
    ) -> None:
        """A held tier makes the next attempt wait on its own account.

        Raises:
            httpx.HTTPStatusError: If no attempt is left, naming the webhook
                and the tier so what was not delivered can be reported.
        """
        held_tier = await self._rate_limiter.hold(self.webhook_url, response)
        held = (
            f"Discord held the {held_tier} tier for webhook "
            f"{webhook_id(self.webhook_url)}"
        )
        if attempt >= self._max_retries:
            raise httpx.HTTPStatusError(
                f"{held}; giving up after {attempt + 1} attempts",
                request=response.request,
                response=response,
            )
        logger.warning("%s, retrying", held)

    async def _handle_response(self, response: httpx.Response, attempt: int) -> bool:
        """Answer a response the venue did not refuse for its budget.

        Returns:
            Whether the post has to be made again, which a server error with
            an attempt left asks for.

        Raises:
            httpx.HTTPStatusError: On a rejection, and on a server error the
                attempts ran out on.
        """
        if response.status_code < 400:
            return False

        error_msg = extract_error_message(response)
        if response.status_code >= 500 and attempt < self._max_retries:
            await self._wait_with_backoff(
                attempt, f"server error {response.status_code} ({error_msg})"
            )
            return True

        error_type = (
            "Discord server error"
            if response.status_code >= 500
            else "Discord API error"
        )
        error_detail = f"{error_type} {response.status_code}: {error_msg}"
        logger.error(error_detail)
        raise httpx.HTTPStatusError(
            error_detail, request=response.request, response=response
        )

    async def _wait_with_backoff(self, attempt: int, reason: str) -> None:
        backoff = self._retry_backoff_seconds * (2**attempt)
        logger.warning(f"Discord {reason}, retrying in {backoff}s")
        await asyncio.sleep(backoff)
