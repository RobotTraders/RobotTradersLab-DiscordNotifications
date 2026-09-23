import io
import logging
import logging.config
from http import HTTPStatus

import pytest
from fake_discord import NO_DELAY, WEBHOOK_ID, FakeDiscord
from robottraderslab_discord_notifications import DiscordHandler
from robottraderslab_discord_notifications.webhook import DEFAULT_MAX_RETRIES
from robottraderslab_discord_notifications.webhook.handler import (
    _WORKER_JOIN_TIMEOUT_SECONDS,
    MAX_CONSECUTIVE_FAILURES,
    MAX_DESCRIPTION_LENGTH,
)

BRIEF_RETRY_AFTER = 0.01
NO_RETRIES = 0
NO_BACKOFF = 0.0
SLOW_ANSWER_SECONDS = 0.5
UNANSWERED_SECONDS = _WORKER_JOIN_TIMEOUT_SECONDS + 1.0
RECORDS_BEHIND_A_STALLED_POST = 2


@pytest.fixture
def handler(venue: FakeDiscord) -> DiscordHandler:
    return DiscordHandler(webhook_url=venue.url(), level=logging.ERROR)


def _logging_to(handler: DiscordHandler, name: str, level: int) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.addHandler(handler)
    logger.setLevel(level)
    return logger


def test_exception_info_in_embed(handler, venue):
    logger = _logging_to(handler, "test", logging.ERROR)

    try:
        raise ValueError("Test exception")
    except ValueError:
        logger.error("Error occurred", exc_info=True)
    handler.flush()

    embed = venue.arrivals[0].body["embeds"][0]
    exception_field = next(f for f in embed["fields"] if f["name"] == "Exception")
    assert "ValueError: Test exception" in exception_field["value"]


def test_traceback_cached_by_another_handler_stays_out_of_description(handler, venue):
    file_like_handler = logging.StreamHandler(io.StringIO())
    file_like_handler.setFormatter(logging.Formatter("%(message)s"))
    logger = _logging_to(handler, "test_cached_traceback", logging.ERROR)
    logger.addHandler(file_like_handler)

    try:
        raise ValueError("Test exception")
    except ValueError:
        logger.error("Error occurred", exc_info=True)
    handler.flush()

    embed = venue.arrivals[0].body["embeds"][0]
    assert embed["description"] == "Error occurred"
    assert any(f["name"] == "Exception" for f in embed["fields"])


def test_description_is_capped_at_the_discord_limit(handler, venue):
    logger = _logging_to(handler, "test_description_cap", logging.ERROR)

    logger.error("x" * (MAX_DESCRIPTION_LENGTH + 500))
    handler.flush()

    embed = venue.arrivals[0].body["embeds"][0]
    assert len(embed["description"]) == MAX_DESCRIPTION_LENGTH


def test_default_level_is_warning(venue):
    handler = DiscordHandler(webhook_url=venue.url())

    assert handler.level == logging.WARNING


def test_dictconfig_integration(venue):
    logging.config.dictConfig(
        {
            "version": 1,
            "handlers": {
                "discord": {
                    "class": "robottraderslab_discord_notifications.DiscordHandler",
                    "level": "ERROR",
                    "webhook_url": venue.url(),
                }
            },
            "root": {"level": "DEBUG", "handlers": ["discord"]},
        }
    )

    logging.getLogger().error("Error via dictConfig")
    logging.getLogger().handlers[0].flush()

    assert venue.arrivals[0].body["embeds"][0]["title"] == "ERROR"


def test_custom_formatter(venue):
    handler = DiscordHandler(webhook_url=venue.url(), level=logging.INFO)
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger = _logging_to(handler, "test_formatter_unique", logging.INFO)

    logger.info("Custom formatted message")
    handler.flush()

    embed = venue.arrivals[0].body["embeds"][0]
    assert embed["description"] == "[INFO] Custom formatted message"


def _corrupt_the_pacing_state(state_dir) -> None:
    for state_file in state_dir.rglob("*.json"):
        state_file.write_text("{not json")


def test_a_warning_from_the_worker_never_stalls_the_shutdown(venue, tmp_path):
    logging.config.dictConfig(
        {
            "version": 1,
            "handlers": {
                "discord": {
                    "class": "robottraderslab_discord_notifications.DiscordHandler",
                    "level": "INFO",
                    "webhook_url": venue.url(),
                }
            },
            "root": {"level": "INFO", "handlers": ["discord"]},
        }
    )
    logging.getLogger("bot").info("First record")
    logging.getLogger().handlers[0].flush()
    _corrupt_the_pacing_state(tmp_path)
    venue.answer_delay = SLOW_ANSWER_SECONDS

    logging.getLogger("bot").info("Second record")
    logging.shutdown()

    assert len(venue.arrivals) == 2


def test_a_venue_that_stops_answering_does_not_hold_the_flush(venue, capsys):
    venue.answer_delay = UNANSWERED_SECONDS
    handler = DiscordHandler(webhook_url=venue.url(), level=logging.WARNING)
    logger = _logging_to(handler, "external_unanswered", logging.WARNING)
    for index in range(RECORDS_BEHIND_A_STALLED_POST + 1):
        logger.warning(f"Sent to a venue that will not answer {index}")

    handler.flush()
    said = capsys.readouterr().err
    venue.answer_delay = NO_DELAY
    handler.close()

    assert f"{RECORDS_BEHIND_A_STALLED_POST} log records still queued" in said


def test_a_closed_handler_carries_no_further_record(handler, venue):
    logger = _logging_to(handler, "test_closed", logging.ERROR)
    logger.error("Sent before the close")

    handler.close()
    logger.error("Emitted after the close")
    handler.flush()

    assert len(venue.arrivals) == 1


class TestAllLogLevels:
    def test_correct_colours_and_bot_name_per_level(self, venue):
        handler = DiscordHandler(
            webhook_url=venue.url(),
            level=logging.DEBUG,
            bot_name="Integration Test Bot",
        )
        logger = _logging_to(handler, "integration_test", logging.DEBUG)

        logger.debug("DEBUG message")
        logger.info("INFO message")
        logger.warning("WARNING message")
        logger.error("ERROR message")
        logger.critical("CRITICAL message")
        handler.flush()

        assert [arrival.body["username"] for arrival in venue.arrivals] == (
            ["Integration Test Bot"] * 5
        )
        assert [arrival.body["embeds"][0]["color"] for arrival in venue.arrivals] == [
            0x95A5A6,
            0x2ECC71,
            0xF1C40F,
            0xE67E22,
            0xE74C3C,
        ]


class TestFeedbackLoopGuard:
    def test_plugin_failure_logging_does_not_trigger_additional_sends(self, venue):
        venue.status = HTTPStatus.NOT_FOUND
        handler = DiscordHandler(webhook_url=venue.url(), level=logging.WARNING)
        root_logger = _logging_to(handler, "", logging.WARNING)

        root_logger.warning("First and only externally originated warning")
        handler.flush()

        assert len(venue.arrivals) == 1

    @pytest.mark.parametrize(
        "dead_status",
        [HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND],
    )
    def test_a_dead_webhook_is_never_posted_to_again(self, dead_status, venue):
        venue.status = dead_status
        handler = DiscordHandler(webhook_url=venue.url(), level=logging.WARNING)
        logger = _logging_to(handler, "external_permanent", logging.WARNING)

        for index in range(MAX_CONSECUTIVE_FAILURES + 3):
            logger.warning(f"Independent warning {index}")
        handler.flush()

        assert len(venue.arrivals) == 1

    def test_transient_failures_open_circuit_after_threshold(self, venue):
        venue.drop_connection = True
        handler = DiscordHandler(
            webhook_url=venue.url(), level=logging.WARNING, max_retries=NO_RETRIES
        )
        logger = _logging_to(handler, "external_transient", logging.WARNING)

        for index in range(MAX_CONSECUTIVE_FAILURES + 3):
            logger.warning(f"Independent warning {index}")
        handler.flush()

        assert len(venue.arrivals) == MAX_CONSECUTIVE_FAILURES

    def test_rejected_messages_are_dropped_and_the_next_still_sends(
        self, venue, caplog
    ):
        rejections = MAX_CONSECUTIVE_FAILURES + 3
        venue.status = HTTPStatus.BAD_REQUEST
        handler = DiscordHandler(webhook_url=venue.url(), level=logging.WARNING)
        logger = _logging_to(handler, "external_rejected", logging.WARNING)

        with caplog.at_level(logging.ERROR):
            for index in range(rejections):
                logger.warning(f"Rejected message {index}")
            handler.flush()

        assert len(venue.arrivals) == rejections
        assert caplog.text.count("Discord rejected a log message") == rejections

    def test_a_record_lost_to_a_server_error_is_said(self, venue, caplog):
        attempts = DEFAULT_MAX_RETRIES + 1
        venue.status = HTTPStatus.INTERNAL_SERVER_ERROR
        handler = DiscordHandler(
            webhook_url=venue.url(),
            level=logging.WARNING,
            retry_backoff_seconds=NO_BACKOFF,
        )
        logger = _logging_to(handler, "external_server_error", logging.WARNING)

        with caplog.at_level(logging.ERROR):
            logger.warning("Lost to a server error")
            handler.flush()

        assert len(venue.arrivals) == attempts
        assert "Dropping a log record: Discord server error 500" in caplog.text

    def test_a_record_dropped_on_a_refusal_names_the_webhook_and_the_tier(
        self, venue, caplog
    ):
        venue.refuse_everything = True
        venue.retry_after = BRIEF_RETRY_AFTER
        handler = DiscordHandler(
            webhook_url=venue.url(), level=logging.WARNING, max_retries=NO_RETRIES
        )
        logger = _logging_to(handler, "external_refused", logging.WARNING)

        with caplog.at_level(logging.ERROR):
            logger.warning("Refused message")
            handler.flush()

        assert "Dropping a log record" in caplog.text
        assert f"webhook {WEBHOOK_ID}" in caplog.text
        assert "webhook tier" in caplog.text

    def test_the_handler_keeps_sending_after_a_refusal(self, venue, caplog):
        venue.refuse_everything = True
        venue.retry_after = BRIEF_RETRY_AFTER
        handler = DiscordHandler(
            webhook_url=venue.url(), level=logging.WARNING, max_retries=NO_RETRIES
        )
        logger = _logging_to(handler, "external_refused_twice", logging.WARNING)

        logger.warning("Refused message")
        logger.warning("Refused too, and still sent")
        handler.flush()

        assert len(venue.arrivals) == 2
