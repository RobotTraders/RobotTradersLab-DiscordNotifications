from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from robottraderslab import Symbol
from robottraderslab.exchanges import OrderFill, OrderPlacement, OrderSide
from robottraderslab_discord_notifications.notifier import DiscordActionNotifier
from robottraderslab_discord_notifications.webhook import (
    BLUE,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_BACKOFF_SECONDS,
    GREEN,
    GREY,
    RED,
    AsyncDiscordClient,
)


@pytest.fixture
def mock_discord_client() -> Mock:
    client = Mock(spec=AsyncDiscordClient)
    client.send = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock()
    return client


@pytest.fixture
def notifier(mock_discord_client) -> DiscordActionNotifier:
    return DiscordActionNotifier(client=mock_discord_client)


@pytest.fixture
def buy_order() -> OrderFill:
    return OrderFill(
        order_id="12345",
        symbol=Symbol.create("BTC/USDT:USDT"),
        side=OrderSide.BUY,
        kind="market",
        quantity=0.5,
        timestamp=datetime(2025, 10, 27, 12, 0, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def make_fill() -> Callable[..., OrderFill]:
    def _make_fill(**fields: Any) -> OrderFill:
        defaults: dict[str, Any] = {
            "order_id": "12345",
            "symbol": Symbol.create("HYPE/USDT:USDT"),
            "side": OrderSide.BUY,
            "kind": "market",
            "quantity": 0.5,
            "timestamp": datetime(2025, 10, 27, 12, 0, 0, tzinfo=timezone.utc),
        }
        return OrderFill(**(defaults | fields))

    return _make_fill


def test_from_webhook_url():
    notifier = DiscordActionNotifier.from_webhook_url(
        "https://discord.com/api/webhooks/test"
    )

    assert isinstance(notifier._client, AsyncDiscordClient)
    assert notifier._client.webhook_url == "https://discord.com/api/webhooks/test"
    assert notifier._client._max_retries == DEFAULT_MAX_RETRIES
    assert notifier._client._retry_backoff_seconds == DEFAULT_RETRY_BACKOFF_SECONDS


def test_from_webhook_url_with_custom_config():
    notifier = DiscordActionNotifier.from_webhook_url(
        "https://discord.com/api/webhooks/test",
        max_retries=5,
        retry_backoff_seconds=2.0,
    )

    assert notifier._client._max_retries == 5
    assert notifier._client._retry_backoff_seconds == 2.0


async def test_send_failure_does_not_propagate(
    notifier, mock_discord_client, buy_order
):
    mock_discord_client.send.side_effect = Exception("Network error")

    async with notifier:
        await notifier.notify_order(buy_order)

    mock_discord_client.send.assert_called_once()


async def test_send_failure_names_the_failed_order(
    notifier, mock_discord_client, buy_order, caplog
):
    mock_discord_client.send.side_effect = Exception("Network error")

    async with notifier:
        await notifier.notify_order(buy_order)

    assert "Failed to send Discord notification for 12345" in caplog.text


async def test_call_sends_without_explicit_session_management(
    notifier, mock_discord_client, buy_order
):
    await notifier(buy_order)

    mock_discord_client.send.assert_called_once()
    mock_discord_client.__aenter__.assert_not_awaited()
    mock_discord_client.__aexit__.assert_not_awaited()


async def test_notify_order_embed_content(notifier, mock_discord_client):
    order_fill = OrderFill(
        order_id="integration_test_12345",
        symbol=Symbol.create("BTC/USDT:USDT"),
        side=OrderSide.BUY,
        kind="market",
        quantity=1.5,
        timestamp=datetime(2025, 10, 27, 14, 30, 0, tzinfo=timezone.utc),
        effect="open",
        source="strategy",
    )

    async with notifier:
        await notifier.notify_order(order_fill)

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert sent_embed == {
        "title": "Long opened · BTC/USDT:USDT",
        "description": (
            "**Filled Quantity:** 1.5\n**Execution Time:** 2025-10-27 14:30:00 UTC"
        ),
        "color": BLUE,
    }


@pytest.mark.parametrize(
    ("quantity", "expected"),
    [
        (0.5, "0.5"),
        (12.0, "12"),
        (1234.5, "1,234.5"),
        (0.000123, "0.000123"),
    ],
    ids=["fractional", "whole", "thousands", "small"],
)
async def test_filled_quantity_strips_trailing_zeros(
    notifier, mock_discord_client, make_fill, quantity, expected
):
    async with notifier:
        await notifier.notify_order(make_fill(quantity=quantity))

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert f"**Filled Quantity:** {expected}" in sent_embed["description"]


async def test_a_fill_opens_with_the_reason_the_strategy_gave(
    notifier, mock_discord_client, make_fill
):
    fill = make_fill(
        filled_value=366.294,
        reason="entry rung 2 of 4",
        client_order_id="a1b2c3d4e5f6-7-4h-alpha",
    )

    async with notifier:
        await notifier.notify_order(fill)

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert sent_embed["description"] == (
        "**Reason:** entry rung 2 of 4\n"
        "**Tag:** 4h-alpha\n"
        "**Filled Quantity:** 0.5\n"
        "**Filled Value:** 366.29 USDT\n"
        "**Execution Time:** 2025-10-27 12:00:00 UTC"
    )


@pytest.mark.parametrize(
    "client_order_id",
    [None, "a1b2c3d4e5f6-7", "manual"],
    ids=["no_id", "untagged_id", "foreign_id"],
)
async def test_a_fill_whose_order_the_engine_did_not_tag_names_no_tag(
    notifier, mock_discord_client, make_fill, client_order_id
):
    async with notifier:
        await notifier.notify_order(make_fill(client_order_id=client_order_id))

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert "Tag" not in sent_embed["description"]


async def test_a_close_states_the_profit_in_the_settlement_currency(
    notifier, mock_discord_client, make_fill
):
    fill = make_fill(
        symbol=Symbol.create("BTC/USD:BTC"),
        side=OrderSide.SELL,
        effect="close",
        realised_profit=-12.345,
        filled_value=9.0,
    )

    async with notifier:
        await notifier.notify_order(fill)

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert "**Filled Value:** 9.00 USD" in sent_embed["description"]
    assert "**Profit:** -12.35 BTC" in sent_embed["description"]
    assert sent_embed["description"].index("**Filled Value:**") < sent_embed[
        "description"
    ].index("**Profit:**")


@pytest.mark.parametrize(
    "effect", ["open", "increase", None], ids=["opened", "increased", "unsettled"]
)
async def test_profit_is_left_out_unless_the_fill_took_position_off(
    notifier, mock_discord_client, make_fill, effect
):
    async with notifier:
        await notifier.notify_order(make_fill(effect=effect, realised_profit=0.0))

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert "Profit" not in sent_embed["description"]


FILL_SCENARIOS = [
    pytest.param(
        {
            "side": OrderSide.BUY,
            "effect": "close",
            "reason": "impulse short exit",
            "realised_profit": 0.42,
        },
        "Short closed · HYPE/USDT:USDT",
        GREEN,
        id="short_closed_at_a_profit",
    ),
    pytest.param(
        {
            "side": OrderSide.BUY,
            "effect": "close",
            "reason": "impulse short exit",
            "realised_profit": -0.80,
        },
        "Short closed · HYPE/USDT:USDT",
        RED,
        id="short_closed_at_a_loss",
    ),
    pytest.param(
        {
            "side": OrderSide.BUY,
            "effect": "close",
            "reason": "impulse short exit",
            "realised_profit": 0.0,
        },
        "Short closed · HYPE/USDT:USDT",
        GREEN,
        id="short_closed_at_breakeven",
    ),
    pytest.param(
        {
            "side": OrderSide.BUY,
            "effect": "open",
            "reason": "impulse long entry",
            "realised_profit": 0.0,
        },
        "Long opened · HYPE/USDT:USDT",
        BLUE,
        id="long_opened",
    ),
    pytest.param(
        {"side": OrderSide.SELL, "effect": "open"},
        "Short opened · HYPE/USDT:USDT",
        BLUE,
        id="short_opened",
    ),
    pytest.param(
        {"side": OrderSide.BUY, "effect": "increase"},
        "Long increased · HYPE/USDT:USDT",
        BLUE,
        id="long_increased",
    ),
    pytest.param(
        {
            "side": OrderSide.SELL,
            "kind": "stop-loss",
            "effect": "close",
            "source": "stop-loss",
            "realised_profit": 1.5,
        },
        "Long closed by Stop Loss · HYPE/USDT:USDT",
        GREEN,
        id="long_closed_by_stop_loss_in_gain",
    ),
    pytest.param(
        {
            "side": OrderSide.SELL,
            "kind": "stop-loss",
            "effect": "reduce",
            "source": "stop-loss",
            "realised_profit": -1.5,
        },
        "Long reduced by Stop Loss · HYPE/USDT:USDT",
        RED,
        id="long_reduced_by_stop_loss_at_a_loss",
    ),
    pytest.param(
        {
            "side": OrderSide.BUY,
            "kind": "take-profit",
            "effect": "close",
            "source": "take-profit",
            "realised_profit": 2.0,
        },
        "Short closed by Take Profit · HYPE/USDT:USDT",
        GREEN,
        id="short_closed_by_take_profit",
    ),
    pytest.param(
        {
            "side": OrderSide.SELL,
            "kind": "take-profit",
            "effect": "close",
            "source": "take-profit",
            "reason": "exit at reference",
            "realised_profit": 2.0,
        },
        "Long closed · HYPE/USDT:USDT",
        GREEN,
        id="venue_fired_exit_the_strategy_named",
    ),
    pytest.param(
        {
            "side": OrderSide.SELL,
            "kind": "liquidation",
            "effect": "close",
            "source": "liquidation",
            "realised_profit": -30.0,
        },
        "Long liquidated · HYPE/USDT:USDT",
        RED,
        id="long_liquidated",
    ),
    pytest.param(
        {
            "side": OrderSide.BUY,
            "kind": "liquidation",
            "effect": "reduce",
            "source": "liquidation",
            "reason": "margin call",
            "realised_profit": 3.0,
        },
        "Short liquidated · HYPE/USDT:USDT",
        RED,
        id="short_liquidated_whatever_the_profit",
    ),
    pytest.param(
        {"side": OrderSide.BUY, "effect": "reduce", "realised_profit": -0.4},
        "Short reduced · HYPE/USDT:USDT",
        RED,
        id="short_reduced_at_a_loss",
    ),
    pytest.param(
        {"side": OrderSide.SELL, "effect": "close", "realised_profit": None},
        "Long closed · HYPE/USDT:USDT",
        GREY,
        id="close_the_venue_reported_no_profit_for",
    ),
    pytest.param(
        {"side": OrderSide.BUY},
        "BUY filled · HYPE/USDT:USDT",
        GREY,
        id="buy_with_no_settled_effect",
    ),
    pytest.param(
        {"side": OrderSide.SELL, "kind": "stop-loss", "source": "stop-loss"},
        "SELL filled · HYPE/USDT:USDT",
        GREY,
        id="sell_with_no_settled_effect",
    ),
]


@pytest.mark.parametrize(("fields", "title", "colour"), FILL_SCENARIOS)
async def test_a_fill_is_titled_and_coloured_by_its_outcome_and_names_no_side(
    notifier, mock_discord_client, make_fill, fields, title, colour
):
    async with notifier:
        await notifier.notify_order(make_fill(**fields))

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert sent_embed["title"] == title
    assert sent_embed["color"] == colour
    assert "**Side:**" not in sent_embed["description"]


async def test_notify_placement_embed_content(notifier, mock_discord_client):
    placement = OrderPlacement(
        order_id="venue-1",
        symbol=Symbol.create("BTC/USDT:USDT"),
        side=OrderSide.BUY,
        quantity=1.5,
        kind="limit",
        price=90_000.0,
    )

    async with notifier:
        await notifier.notify_placement(placement)

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert sent_embed["title"] == "Limit Order Placed"
    assert "BTC/USDT:USDT" in sent_embed["description"]
    assert "**Price:** 90,000" in sent_embed["description"]


async def test_notify_placement_of_a_trigger_order(notifier, mock_discord_client):
    placement = OrderPlacement(
        order_id="venue-2",
        symbol=Symbol.create("BTC/USDT:USDT"),
        side=OrderSide.SELL,
        quantity=0.25,
        kind="limit",
        price=90_000.0,
        trigger_price=89_000.0,
    )

    async with notifier:
        await notifier.notify_placement(placement)

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert sent_embed["title"] == "Trigger Limit Order Placed"
    assert "**Trigger:** 89,000" in sent_embed["description"]


async def test_notify_placement_of_a_market_order_shows_no_price(
    notifier, mock_discord_client
):
    placement = OrderPlacement(
        order_id="venue-3",
        symbol=Symbol.create("BTC/USDT:USDT"),
        side=OrderSide.BUY,
        quantity=1.0,
        kind="market",
    )

    async with notifier:
        await notifier.notify_placement(placement)

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert "**Price:**" not in sent_embed["description"]


async def test_notify_placement_of_a_trigger_market_order(
    notifier, mock_discord_client
):
    placement = OrderPlacement(
        order_id="venue-4",
        symbol=Symbol.create("BTC/USDT:USDT"),
        side=OrderSide.BUY,
        quantity=1.0,
        kind="market",
        trigger_price=91_000.0,
    )

    async with notifier:
        await notifier.notify_placement(placement)

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert sent_embed["title"] == "Trigger Market Order Placed"


async def test_notify_placement_of_a_market_order_names_it_in_the_title(
    notifier, mock_discord_client
):
    placement = OrderPlacement(
        order_id="venue-5",
        symbol=Symbol.create("BTC/USDT:USDT"),
        side=OrderSide.BUY,
        quantity=1.0,
        kind="market",
    )

    async with notifier:
        await notifier.notify_placement(placement)

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert sent_embed["title"] == "Market Order Placed"


@pytest.mark.parametrize(
    ("side", "side_text", "colour"),
    [
        (OrderSide.BUY, "BUY", 0x2ECC71),
        (OrderSide.SELL, "SELL", 0xE74C3C),
    ],
    ids=["buy", "sell"],
)
async def test_notify_placement_reports_the_order_side(
    notifier, mock_discord_client, side, side_text, colour
):
    placement = OrderPlacement(
        order_id="venue-6",
        symbol=Symbol.create("BTC/USDT:USDT"),
        side=side,
        quantity=1.0,
        kind="limit",
        price=90_000.0,
    )

    async with notifier:
        await notifier.notify_placement(placement)

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert f"**Side:** {side_text}" in sent_embed["description"]
    assert sent_embed["color"] == colour
