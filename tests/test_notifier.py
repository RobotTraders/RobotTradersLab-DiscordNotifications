from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest
from robottraderslab_discord_notifications.notifier import DiscordActionNotifier
from robottraderslab_discord_notifications.webhook import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_BACKOFF_SECONDS,
    AsyncDiscordClient,
)

from robottraderslab import Symbol
from robottraderslab.exchanges import OrderFill, OrderPlacement, OrderSide


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
        filled_quantity=0.5,
        execution_time=datetime(2025, 10, 27, 12, 0, 0, tzinfo=timezone.utc),
    )


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


@pytest.mark.parametrize(
    "side",
    [
        OrderSide.BUY,
        OrderSide.SELL,
    ],
    ids=["buy_order", "sell_order"],
)
async def test_order_notification(notifier, mock_discord_client, side):
    order = OrderFill(
        order_id="12345",
        symbol=Symbol.create("BTC/USDT:USDT"),
        side=side,
        kind="market",
        filled_quantity=0.5,
        execution_time=datetime(2025, 10, 27, 12, 0, 0, tzinfo=timezone.utc),
    )

    async with notifier:
        await notifier.notify_order(order)

    mock_discord_client.send.assert_called_once()


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
        filled_quantity=1.5,
        execution_time=datetime(2025, 10, 27, 14, 30, 0, tzinfo=timezone.utc),
    )

    async with notifier:
        await notifier.notify_order(order_fill)

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert sent_embed["title"] == "Order Filled"
    assert "BTC/USDT:USDT" in sent_embed["description"]
    assert sent_embed["description"].index("**Side:**") < sent_embed[
        "description"
    ].index("**Symbol:**")


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
    notifier, mock_discord_client, quantity, expected
):
    order = OrderFill(
        order_id="12345",
        symbol=Symbol.create("BTC/USDT:USDT"),
        side=OrderSide.BUY,
        kind="market",
        filled_quantity=quantity,
        execution_time=datetime(2025, 10, 27, 12, 0, 0, tzinfo=timezone.utc),
    )

    async with notifier:
        await notifier.notify_order(order)

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert f"**Filled Quantity:** {expected}" in sent_embed["description"]


@pytest.mark.parametrize(
    ("side", "side_text", "colour"),
    [
        (OrderSide.BUY, "BUY", 0x2ECC71),
        (OrderSide.SELL, "SELL", 0xE74C3C),
    ],
    ids=["buy", "sell"],
)
async def test_order_fill_reports_the_order_side(
    notifier, mock_discord_client, side, side_text, colour
):
    order_fill = OrderFill(
        order_id="12345",
        symbol=Symbol.create("BTC/USDT:USDT"),
        side=side,
        kind="market",
        filled_quantity=0.5,
        execution_time=datetime(2025, 10, 27, 12, 0, 0, tzinfo=timezone.utc),
    )

    async with notifier:
        await notifier.notify_order(order_fill)

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert sent_embed["title"] == "Order Filled"
    assert f"**Side:** {side_text}" in sent_embed["description"]
    assert sent_embed["color"] == colour


async def test_side_has_no_colored_circle(notifier, mock_discord_client, buy_order):
    async with notifier:
        await notifier.notify_order(buy_order)

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert "🟢" not in sent_embed["description"]
    assert "🔴" not in sent_embed["description"]


async def test_value_profit_and_reason_are_omitted_by_default(
    notifier, mock_discord_client, buy_order
):
    async with notifier:
        await notifier.notify_order(buy_order)

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert "Filled Value" not in sent_embed["description"]
    assert "Realised Profit" not in sent_embed["description"]
    assert "Reason" not in sent_embed["description"]


async def test_value_and_reason_are_rendered_when_present(
    notifier, mock_discord_client
):
    order_fill = OrderFill(
        order_id="12345",
        symbol=Symbol.create("BTC/USDT:USDT"),
        side=OrderSide.BUY,
        kind="market",
        filled_quantity=0.5,
        execution_time=datetime(2025, 10, 27, 12, 0, 0, tzinfo=timezone.utc),
        filled_value=366.294,
        reason="entry rung 2 of 4",
    )

    async with notifier:
        await notifier.notify_order(order_fill)

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert "**Filled Value:** 366.29 USDT" in sent_embed["description"]
    assert "**Reason:** entry rung 2 of 4" in sent_embed["description"]


async def test_realised_profit_is_rendered_in_the_settlement_currency_on_a_close(
    notifier, mock_discord_client
):
    order_fill = OrderFill(
        order_id="12345",
        symbol=Symbol.create("BTC/USDT:USDT"),
        side=OrderSide.SELL,
        kind="take-profit",
        filled_quantity=0.5,
        execution_time=datetime(2025, 10, 27, 12, 0, 0, tzinfo=timezone.utc),
        realised_profit=-12.345,
        effect="close",
        source="take-profit",
    )

    async with notifier:
        await notifier.notify_order(order_fill)

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert "**Realised Profit:** -12.35 USDT" in sent_embed["description"]


@pytest.mark.parametrize(
    "effect", ["open", "increase", None], ids=["opened", "increased", "unattributed"]
)
async def test_realised_profit_is_left_out_unless_the_fill_took_position_off(
    notifier, mock_discord_client, effect
):
    order_fill = OrderFill(
        order_id="12345",
        symbol=Symbol.create("BTC/USDT:USDT"),
        side=OrderSide.BUY,
        kind="trigger",
        filled_quantity=0.5,
        execution_time=datetime(2025, 10, 27, 12, 0, 0, tzinfo=timezone.utc),
        realised_profit=0.0,
        effect=effect,
        source="strategy",
    )

    async with notifier:
        await notifier.notify_order(order_fill)

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert "Realised Profit" not in sent_embed["description"]


async def test_market_fill_embed_names_its_kind(notifier, mock_discord_client):
    order_fill = OrderFill(
        order_id="12345",
        symbol=Symbol.create("BTC/USDT:USDT"),
        side=OrderSide.BUY,
        kind="market",
        filled_quantity=0.5,
        execution_time=datetime(2025, 10, 27, 12, 0, 0, tzinfo=timezone.utc),
    )

    async with notifier:
        await notifier.notify_order(order_fill)

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert "**Kind:** market" in sent_embed["description"]


async def test_reconciled_trigger_fill_embed_names_its_kind(
    notifier, mock_discord_client
):
    order_fill = OrderFill(
        order_id="fired-1",
        symbol=Symbol.create("BTC/USDT:USDT"),
        side=OrderSide.BUY,
        kind="trigger",
        filled_quantity=1.0,
        execution_time=datetime(2025, 10, 27, 12, 0, 0, tzinfo=timezone.utc),
    )

    async with notifier:
        await notifier.notify_order(order_fill)

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert "**Kind:** trigger" in sent_embed["description"]


@pytest.mark.parametrize(
    ("effect", "source", "side", "title", "position_side", "colour"),
    [
        ("open", "strategy", OrderSide.BUY, "Position Opened", "LONG", 0x2ECC71),
        ("open", "strategy", OrderSide.SELL, "Position Opened", "SHORT", 0xE74C3C),
        (
            "increase",
            "strategy",
            OrderSide.BUY,
            "Position Increased",
            "LONG",
            0x2ECC71,
        ),
        ("reduce", "strategy", OrderSide.SELL, "Position Reduced", "LONG", 0xE74C3C),
        ("close", "strategy", OrderSide.BUY, "Position Closed", "SHORT", 0x2ECC71),
        (
            "reduce",
            "take-profit",
            OrderSide.SELL,
            "Position Reduced by Take Profit",
            "LONG",
            0x3498DB,
        ),
        (
            "close",
            "take-profit",
            OrderSide.BUY,
            "Position Closed by Take Profit",
            "SHORT",
            0x3498DB,
        ),
        (
            "reduce",
            "stop-loss",
            OrderSide.SELL,
            "Position Reduced by Stop Loss",
            "LONG",
            0xF1C40F,
        ),
        (
            "close",
            "stop-loss",
            OrderSide.BUY,
            "Position Closed by Stop Loss",
            "SHORT",
            0xF1C40F,
        ),
        (
            "close",
            "liquidation",
            OrderSide.SELL,
            "Position Liquidated",
            "LONG",
            0xE67E22,
        ),
    ],
    ids=[
        "long_opened",
        "short_opened",
        "long_increased",
        "long_reduced",
        "short_closed",
        "long_reduced_by_take_profit",
        "short_closed_by_take_profit",
        "long_reduced_by_stop_loss",
        "short_closed_by_stop_loss",
        "long_liquidated",
    ],
)
async def test_attributed_fill_titles_what_happened_to_the_position(
    notifier,
    mock_discord_client,
    effect,
    source,
    side,
    title,
    position_side,
    colour,
):
    order_fill = OrderFill(
        order_id="attributed-1",
        symbol=Symbol.create("BTC/USDT:USDT"),
        side=side,
        kind="market",
        filled_quantity=0.5,
        execution_time=datetime(2025, 10, 27, 12, 0, 0, tzinfo=timezone.utc),
        effect=effect,
        source=source,
    )

    async with notifier:
        await notifier.notify_order(order_fill)

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert sent_embed["title"] == title
    assert f"**Side:** {position_side}" in sent_embed["description"]
    assert sent_embed["color"] == colour


async def test_attributed_strategy_fill_keeps_naming_its_kind(
    notifier, mock_discord_client
):
    order_fill = OrderFill(
        order_id="attributed-2",
        symbol=Symbol.create("BTC/USDT:USDT"),
        side=OrderSide.BUY,
        kind="trigger",
        filled_quantity=0.5,
        execution_time=datetime(2025, 10, 27, 12, 0, 0, tzinfo=timezone.utc),
        effect="open",
        source="strategy",
    )

    async with notifier:
        await notifier.notify_order(order_fill)

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert "**Kind:** trigger" in sent_embed["description"]


@pytest.mark.parametrize(
    "source",
    ["take-profit", "liquidation"],
    ids=["by_take_profit", "liquidated"],
)
async def test_a_title_naming_what_fired_it_leaves_the_kind_out(
    notifier, mock_discord_client, source
):
    order_fill = OrderFill(
        order_id="attributed-3",
        symbol=Symbol.create("BTC/USDT:USDT"),
        side=OrderSide.SELL,
        kind="take-profit",
        filled_quantity=0.5,
        execution_time=datetime(2025, 10, 27, 12, 0, 0, tzinfo=timezone.utc),
        effect="close",
        source=source,
    )

    async with notifier:
        await notifier.notify_order(order_fill)

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert "**Kind:**" not in sent_embed["description"]


@pytest.mark.parametrize(
    ("effect", "side", "title", "position_side", "colour"),
    [
        ("close", OrderSide.SELL, "Position Closed", "LONG", 0xE74C3C),
        ("reduce", OrderSide.BUY, "Position Reduced", "SHORT", 0x2ECC71),
    ],
    ids=["long_closed", "short_reduced"],
)
async def test_a_reasoned_venue_fired_fill_is_the_strategys_own_event(
    notifier, mock_discord_client, effect, side, title, position_side, colour
):
    order_fill = OrderFill(
        order_id="reasoned-1",
        symbol=Symbol.create("BTC/USDT:USDT"),
        side=side,
        kind="take-profit",
        filled_quantity=0.5,
        execution_time=datetime(2025, 10, 27, 12, 0, 0, tzinfo=timezone.utc),
        reason="exit at reference",
        effect=effect,
        source="take-profit",
    )

    async with notifier:
        await notifier.notify_order(order_fill)

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert sent_embed["title"] == title
    assert f"**Side:** {position_side}" in sent_embed["description"]
    assert "**Kind:**" not in sent_embed["description"]
    assert "**Reason:** exit at reference" in sent_embed["description"]
    assert sent_embed["color"] == colour


async def test_a_reasoned_venue_fired_fill_stating_no_effect_renders_as_an_order(
    notifier, mock_discord_client
):
    order_fill = OrderFill(
        order_id="reasoned-2",
        symbol=Symbol.create("BTC/USDT:USDT"),
        side=OrderSide.SELL,
        kind="stop-loss",
        filled_quantity=0.5,
        execution_time=datetime(2025, 10, 27, 12, 0, 0, tzinfo=timezone.utc),
        reason="trend broke",
        source="stop-loss",
    )

    async with notifier:
        await notifier.notify_order(order_fill)

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert sent_embed["title"] == "Order Filled"
    assert "**Side:** SELL" in sent_embed["description"]
    assert "**Kind:**" not in sent_embed["description"]
    assert sent_embed["color"] == 0xE74C3C


async def test_a_reasoned_liquidation_keeps_its_title(notifier, mock_discord_client):
    order_fill = OrderFill(
        order_id="reasoned-3",
        symbol=Symbol.create("BTC/USDT:USDT"),
        side=OrderSide.SELL,
        kind="liquidation",
        filled_quantity=0.5,
        execution_time=datetime(2025, 10, 27, 12, 0, 0, tzinfo=timezone.utc),
        reason="margin call",
        effect="close",
        source="liquidation",
    )

    async with notifier:
        await notifier.notify_order(order_fill)

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert sent_embed["title"] == "Position Liquidated"
    assert "**Side:** LONG" in sent_embed["description"]
    assert "**Reason:** margin call" in sent_embed["description"]
    assert sent_embed["color"] == 0xE67E22


@pytest.mark.parametrize(
    ("kind", "side", "colour"),
    [
        ("take-profit", OrderSide.SELL, 0xE74C3C),
        ("stop-loss", OrderSide.BUY, 0x2ECC71),
        ("liquidation", OrderSide.SELL, 0xE74C3C),
    ],
    ids=["take_profit", "stop_loss", "liquidation"],
)
async def test_venue_fired_fill_stating_no_effect_renders_as_an_order(
    notifier, mock_discord_client, kind, side, colour
):
    order_fill = OrderFill(
        order_id="unattributed-1",
        symbol=Symbol.create("BTC/USDT:USDT"),
        side=side,
        kind=kind,
        filled_quantity=0.5,
        execution_time=datetime(2025, 10, 27, 12, 0, 0, tzinfo=timezone.utc),
        source=kind,
    )

    async with notifier:
        await notifier.notify_order(order_fill)

    sent_embed = mock_discord_client.send.call_args.args[0]
    assert sent_embed["title"] == "Order Filled"
    assert f"**Side:** {side.name}" in sent_embed["description"]
    assert f"**Kind:** {kind}" in sent_embed["description"]
    assert sent_embed["color"] == colour


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
