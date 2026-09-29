import logging
from http import HTTPStatus
from importlib.metadata import entry_points

import pytest

from robottraderslab_discord_notifications.webhook_check import (
    WebhookCheckError,
    check_webhook,
)


async def test_the_webhook_receives_one_message(venue):
    lines = await check_webhook(entry="discord-trades", webhook_url=venue.url())

    assert [arrival.body["embeds"][0]["description"] for arrival in venue.arrivals] == [
        "The webhook `discord-trades` receives messages."
    ]
    assert lines == [
        "`discord-trades` is a Discord webhook; posted: The webhook "
        "`discord-trades` receives messages."
    ]


async def test_a_webhook_discord_refuses(venue):
    venue.status = HTTPStatus.NOT_FOUND

    with pytest.raises(
        WebhookCheckError,
        match="Discord refuses the webhook `discord-trades`: Discord API error 404",
    ):
        await check_webhook(entry="discord-trades", webhook_url=venue.url())


async def test_a_refusal_is_said_once_by_the_check(venue, caplog):
    venue.status = HTTPStatus.NOT_FOUND

    with pytest.raises(WebhookCheckError):
        await check_webhook(entry="discord-trades", webhook_url=venue.url())

    assert [r.message for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_a_webhook_that_cannot_be_reached(venue):
    unreachable = venue.url()
    venue.stop()

    with pytest.raises(
        WebhookCheckError, match="cannot reach Discord for the webhook `discord-trades`"
    ):
        await check_webhook(entry="discord-trades", webhook_url=unreachable)


async def test_a_server_error_is_not_retried(venue):
    venue.status = HTTPStatus.INTERNAL_SERVER_ERROR

    with pytest.raises(WebhookCheckError):
        await check_webhook(entry="discord-trades", webhook_url=venue.url())

    assert len(venue.arrivals) == 1


def test_the_engine_finds_the_check_under_webhook_url():
    registered = entry_points(
        group="robot_traders_lab.secret_checks", name="webhook_url"
    )

    assert [entry_point.load() for entry_point in registered] == [check_webhook]
