from typing import Any

import httpx

from robottraderslab_discord_notifications.webhook import (
    DEFAULT_TIMEOUT_SECONDS,
    GREEN,
    AsyncDiscordClient,
)

_NO_RETRY = 0
_TITLE = "rtlab check"


class WebhookCheckError(Exception):
    """A webhook Discord refuses or that cannot be reached, named by its entry."""


async def check_webhook(*, entry: str, webhook_url: str, **_kwargs: Any) -> list[str]:
    """Entry point of the secret check plugin system for an entry carrying a
    `webhook_url`; a post the webhook accepts is the whole proof, so a
    refusal is final.

    Args:
        **_kwargs: The entry's other fields, which the check ignores.

    Raises:
        WebhookCheckError: If Discord refuses the post or cannot be reached.
    """
    description = f"The webhook `{entry}` receives messages."
    embed = {"title": _TITLE, "description": description, "color": GREEN}
    async with AsyncDiscordClient.from_webhook_url(
        webhook_url, timeout_seconds=DEFAULT_TIMEOUT_SECONDS, max_retries=_NO_RETRY
    ) as client:
        try:
            await client.send(embed)
        except httpx.HTTPStatusError as e:
            raise WebhookCheckError(
                f"Discord refuses the webhook `{entry}`: {e}"
            ) from e
        except httpx.HTTPError as e:
            raise WebhookCheckError(
                f"cannot reach Discord for the webhook `{entry}`: {e!r}"
            ) from e
    return [f"`{entry}` is a Discord webhook; posted: {description}"]
