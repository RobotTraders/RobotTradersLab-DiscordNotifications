import logging
from typing import Any, Self

from robottraderslab.exchanges import (
    FillEffect,
    FillSource,
    OrderFill,
    OrderPlacement,
    OrderSide,
    tag_of,
)
from robottraderslab_discord_notifications.webhook import (
    BLUE,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_BACKOFF_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    GREEN,
    GREY,
    RED,
    AsyncDiscordClient,
)

logger = logging.getLogger(__name__)

_TITLE_SEPARATOR = "·"
_EFFECT_VERBS: dict[FillEffect, str] = {
    "open": "opened",
    "increase": "increased",
    "reduce": "reduced",
    "close": "closed",
}
_EXIT_SOURCES: dict[FillSource, str] = {
    "take-profit": "Take Profit",
    "stop-loss": "Stop Loss",
}
_SIDE_FOLLOWING_EFFECTS: frozenset[FillEffect] = frozenset({"open", "increase"})


class DiscordActionNotifier:
    def __init__(self, client: AsyncDiscordClient):
        self._client = client

    @classmethod
    def from_webhook_url(
        cls,
        webhook_url: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
        bot_name: str | None = None,
    ) -> Self:
        """Build a notifier posting to one webhook.

        Args:
            retry_backoff_seconds: Base delay that doubles with each retry attempt.
            bot_name: Name to post under, which tells apart the accounts
                sharing a channel. The webhook's own name is used when left out.
        """
        client = AsyncDiscordClient.from_webhook_url(
            webhook_url=webhook_url,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            bot_name=bot_name,
        )
        return cls(client)

    async def __aenter__(self) -> Self:
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._client.__aexit__(*args)

    async def __call__(self, order_fill: OrderFill) -> None:
        """Send a Discord notification for a filled order."""
        await self.notify_order(order_fill)

    async def notify_order(self, order_fill: OrderFill) -> None:
        """Send a Discord notification for a filled order."""
        embed = _embed(
            title=_fill_title(order_fill),
            description=_fill_description(order_fill),
            colour=_fill_colour(order_fill),
        )
        await self._send(embed, order_fill.order_id)

    async def notify_placement(self, placement: OrderPlacement) -> None:
        """Send a Discord notification for an order the venue accepted."""
        embed = _embed(
            title=f"{_placement_kind(placement)} Order Placed",
            description=_placement_description(placement),
            colour=GREEN if placement.side == OrderSide.BUY else RED,
        )
        await self._send(embed, placement.order_id)

    async def _send(self, embed: dict[str, Any], order_id: str) -> None:
        try:
            await self._client.send(embed)
            logger.debug(f"Discord notification sent for {order_id}")
        except Exception as e:
            logger.error(f"Failed to send Discord notification for {order_id}: {e}")


def _fill_title(order_fill: OrderFill) -> str:
    symbol = order_fill.symbol
    effect = order_fill.effect
    if effect is None:
        return f"{order_fill.side.name} filled {_TITLE_SEPARATOR} {symbol}"
    side = _position_side(order_fill.side, effect)
    if order_fill.source == "liquidation":
        return f"{side} liquidated {_TITLE_SEPARATOR} {symbol}"
    title = f"{side} {_EFFECT_VERBS[effect]}"
    fired_by = _EXIT_SOURCES.get(order_fill.source) if order_fill.source else None
    if fired_by is not None and order_fill.reason is None:
        title += f" by {fired_by}"
    return f"{title} {_TITLE_SEPARATOR} {symbol}"


def _position_side(side: OrderSide, effect: FillEffect) -> str:
    """A fill taking a position off trades against the side that position was."""
    bought = side == OrderSide.BUY
    return "Long" if bought == (effect in _SIDE_FOLLOWING_EFFECTS) else "Short"


def _fill_description(order_fill: OrderFill) -> str:
    lines = []
    if order_fill.reason is not None:
        lines.append(f"**Reason:** {order_fill.reason}")
    tag = tag_of(order_fill.client_order_id)
    if tag is not None:
        lines.append(f"**Tag:** {tag}")
    lines.append(f"**Filled Quantity:** {_trimmed(order_fill.quantity)}")
    if order_fill.filled_value is not None:
        lines.append(
            f"**Filled Value:** {order_fill.filled_value:,.2f} "
            f"{order_fill.symbol.quote}"
        )
    if (
        order_fill.realised_profit is not None
        and order_fill.effect is not None
        and order_fill.effect not in _SIDE_FOLLOWING_EFFECTS
    ):
        lines.append(
            f"**Profit:** {order_fill.realised_profit:+,.2f} "
            f"{order_fill.symbol.settlement}"
        )
    timestamp = (
        order_fill.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
        if order_fill.timestamp
        else "N/A"
    )
    lines.append(f"**Execution Time:** {timestamp}")
    return "\n".join(lines)


def _fill_colour(order_fill: OrderFill) -> int:
    effect = order_fill.effect
    if effect is None:
        return GREY
    if effect in _SIDE_FOLLOWING_EFFECTS:
        return BLUE
    if order_fill.source == "liquidation":
        return RED
    if order_fill.realised_profit is None:
        return GREY
    return GREEN if order_fill.realised_profit >= 0 else RED


def _placement_kind(placement: OrderPlacement) -> str:
    """Name the order the way the venue will treat it.

    A trigger price makes the order conditional, so its kind describes how it
    fills once the condition is met.
    """
    if placement.trigger_price is None:
        return placement.kind.title()
    return f"Trigger {placement.kind.title()}"


def _placement_description(placement: OrderPlacement) -> str:
    description = (
        f"**Side:** {placement.side.name}\n"
        f"**Symbol:** {placement.symbol}\n"
        f"**Quantity:** {_trimmed(placement.quantity)}"
    )
    if placement.price is not None:
        description += f"\n**Price:** {_trimmed(placement.price)}"
    if placement.trigger_price is not None:
        description += f"\n**Trigger:** {_trimmed(placement.trigger_price)}"
    return description


def _embed(*, title: str, description: str, colour: int) -> dict[str, Any]:
    return {"title": title, "description": description, "color": colour}


def _trimmed(quantity: float) -> str:
    return f"{quantity:,.8f}".rstrip("0").rstrip(".")
