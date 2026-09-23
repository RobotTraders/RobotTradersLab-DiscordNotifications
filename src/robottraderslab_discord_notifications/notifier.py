import logging
from typing import Any, NamedTuple, Self

from robottraderslab.exchanges import (
    FillEffect,
    FillSource,
    OrderFill,
    OrderPlacement,
    OrderSide,
    OrderType,
)
from robottraderslab_discord_notifications.webhook import (
    BLUE,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_BACKOFF_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    GREEN,
    ORANGE,
    RED,
    YELLOW,
    AsyncDiscordClient,
)

logger = logging.getLogger(__name__)

_LIQUIDATION_TITLE = "Position Liquidated"
_EFFECT_NAMES: dict[FillEffect, str] = {
    "open": "Opened",
    "increase": "Increased",
    "reduce": "Reduced",
    "close": "Closed",
}
_EXIT_SOURCES: dict[FillSource, tuple[str, int]] = {
    "take-profit": ("Take Profit", BLUE),
    "stop-loss": ("Stop Loss", YELLOW),
}
_SIDE_FOLLOWING_EFFECTS: frozenset[FillEffect] = frozenset({"open", "increase"})


class _FillHeader(NamedTuple):
    """The kind is None when the title or the strategy's reason names the event."""

    title: str
    side: str
    kind: OrderType | None
    colour: int


class DiscordActionNotifier:
    """Colour tells the events apart; the side is always named in the text."""

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
        header = _fill_header(order_fill)
        embed = _embed(
            title=header.title,
            description=_fill_description(order_fill, header),
            colour=header.colour,
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


def _fill_header(order_fill: OrderFill) -> _FillHeader:
    if order_fill.effect is None:
        return _FillHeader(
            "Order Filled",
            order_fill.side.name,
            _reported_kind(order_fill),
            _side_colour(order_fill),
        )
    return _attributed_header(order_fill, order_fill.effect)


def _attributed_header(order_fill: OrderFill, effect: FillEffect) -> _FillHeader:
    side = _position_side(order_fill.side, effect)
    if order_fill.source == "liquidation":
        return _FillHeader(_LIQUIDATION_TITLE, side, None, ORANGE)
    fired_by = _EXIT_SOURCES.get(order_fill.source) if order_fill.source else None
    if fired_by is None or order_fill.reason is not None:
        return _FillHeader(
            f"Position {_EFFECT_NAMES[effect]}",
            side,
            _reported_kind(order_fill),
            _side_colour(order_fill),
        )
    name, colour = fired_by
    return _FillHeader(
        f"Position {_EFFECT_NAMES[effect]} by {name}", side, None, colour
    )


def _reported_kind(order_fill: OrderFill) -> OrderType | None:
    """The kind names the venue mechanism that produced the fill; a reason
    names the trade the strategy made.
    """
    return None if order_fill.reason is not None else order_fill.kind


def _side_colour(order_fill: OrderFill) -> int:
    return GREEN if order_fill.side == OrderSide.BUY else RED


def _position_side(side: OrderSide, effect: FillEffect) -> str:
    """A fill taking a position off trades against the side that position was."""
    bought = side == OrderSide.BUY
    return "LONG" if bought == (effect in _SIDE_FOLLOWING_EFFECTS) else "SHORT"


def _fill_description(order_fill: OrderFill, header: _FillHeader) -> str:
    execution_time = (
        order_fill.execution_time.strftime("%Y-%m-%d %H:%M:%S UTC")
        if order_fill.execution_time
        else "N/A"
    )
    description = f"**Side:** {header.side}\n"
    if header.kind is not None:
        description += f"**Kind:** {header.kind}\n"
    description += (
        f"**Symbol:** {order_fill.symbol}\n"
        f"**Execution Time:** {execution_time}\n"
        f"**Filled Quantity:** {_trimmed(order_fill.filled_quantity)}"
    )
    if order_fill.filled_value is not None:
        description += (
            f"\n**Filled Value:** {order_fill.filled_value:,.2f} "
            f"{order_fill.symbol.quote}"
        )
    if (
        order_fill.realised_profit is not None
        and order_fill.effect is not None
        and order_fill.effect not in _SIDE_FOLLOWING_EFFECTS
    ):
        description += (
            f"\n**Realised Profit:** {order_fill.realised_profit:+,.2f} "
            f"{order_fill.symbol.settlement}"
        )
    if order_fill.reason is not None:
        description += f"\n**Reason:** {order_fill.reason}"
    return description


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
