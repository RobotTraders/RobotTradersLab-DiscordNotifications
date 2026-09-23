import asyncio
import logging
import logging.config
import tempfile
from collections.abc import Generator, Iterator
from unittest.mock import AsyncMock, patch

import pytest

START_TIME = 1_000.0

_unpatched_sleep = asyncio.sleep


class Clock:
    """Time moves only when a test or a sleep moves it, so a delay a limiter
    asks for never elapses on its own.
    """

    def __init__(self) -> None:
        self.now = START_TIME

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _temporary_state_dir(tmp_path) -> Iterator[None]:
    """`state_file_path` resolves the temporary directory on every call, so a
    scope written by one test is unreachable from the next.

    The patch is held without `monkeypatch`, whose teardown would otherwise
    be ordered behind every autouse fixture asking for it.
    """
    with patch.object(tempfile, "gettempdir", return_value=str(tmp_path)):
        yield


@pytest.fixture(autouse=True)
def _reset_logging() -> Generator[None, None, None]:
    yield
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "handlers": {},
            "root": {
                "level": "DEBUG",
                "handlers": [],
            },
        }
    )
    for logger_name in list(logging.Logger.manager.loggerDict.keys()):
        logger = logging.getLogger(logger_name)
        logger.handlers = []
        logger.setLevel(logging.NOTSET)
        logger.propagate = True


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def virtual_sleeps(clock: Clock) -> Iterator[AsyncMock]:
    """One clock behind `time.time` and `asyncio.sleep`: a sleep advances it
    by the delay and hands the loop back, so a limiter that re-checks on
    waking finds the moment it asked for without the test taking that long.
    """

    async def advance(delay: float) -> None:
        clock.now += delay
        await _unpatched_sleep(0)

    with (
        patch("time.time", side_effect=clock),
        patch("time.monotonic", side_effect=clock),
        patch(
            "asyncio.sleep", new_callable=AsyncMock, side_effect=advance
        ) as sleep_mock,
    ):
        yield sleep_mock
