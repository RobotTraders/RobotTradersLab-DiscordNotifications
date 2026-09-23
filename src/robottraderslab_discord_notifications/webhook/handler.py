import asyncio
import logging
import logging.handlers
import sys
import threading
import traceback
from typing import Any, NamedTuple

import httpx

from .async_client import AsyncDiscordClient
from .colours import GREEN, GREY, ORANGE, RED, YELLOW

logger = logging.getLogger(__name__)

PLUGIN_LOGGER_PREFIX = __name__.split(".")[0]
MAX_CONSECUTIVE_FAILURES = 5
MAX_DESCRIPTION_LENGTH = 4096

_MAX_EXCEPTION_LENGTH = 1000
_WORKER_JOIN_TIMEOUT_SECONDS = 5.0


class _PluginLogFilter(logging.Filter):
    """Drops records emitted by the Discord plugin's own loggers, keeping the
    handler from reporting on itself.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(PLUGIN_LOGGER_PREFIX)


class _Worker(NamedTuple):
    loop: asyncio.AbstractEventLoop
    thread: threading.Thread


class DiscordHandler(logging.Handler):
    """Logging handler that sends log records to Discord webhooks."""

    LEVEL_COLOURS = {
        logging.DEBUG: GREY,
        logging.INFO: GREEN,
        logging.WARNING: YELLOW,
        logging.ERROR: ORANGE,
        logging.CRITICAL: RED,
    }

    def __init__(
        self,
        webhook_url: str,
        level: int = logging.WARNING,
        bot_name: str | None = None,
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
        retry_backoff_seconds: float = 1.0,
    ):
        """
        Args:
            level: Minimum severity forwarded to Discord.
            bot_name: Shown as the message author; the webhook's own name when
                left out.
            retry_backoff_seconds: Base delay that doubles with each retry attempt.
        """
        super().__init__(level)
        self.webhook_url = webhook_url
        self.bot_name = bot_name
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._queue: asyncio.Queue[logging.LogRecord | None] = asyncio.Queue()
        self._worker: _Worker | None = None
        self._start_lock = threading.Lock()
        self._consecutive_failures = 0
        self._circuit_open = False
        self.addFilter(_PluginLogFilter())

    def close(self) -> None:
        self.flush()
        worker = self._worker
        if worker is not None and not worker.loop.is_closed():
            worker.loop.call_soon_threadsafe(self._queue.put_nowait, None)
            worker.thread.join(_WORKER_JOIN_TIMEOUT_SECONDS)
        super().close()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            worker = self._worker or self._started_worker()
            worker.loop.call_soon_threadsafe(self._queue.put_nowait, record)
        except RuntimeError:
            self.handleError(record)

    def filter(self, record: logging.LogRecord) -> bool | logging.LogRecord:
        """Keep a record the worker thread emitted out of this handler.

        `logging.shutdown` holds a handler's lock across its flush, and the
        engine code the worker runs logs on its own loggers, so a record from
        that thread reaching this handler would wait for a lock its own send
        has to release first. Every other handler still carries it.
        """
        worker = self._worker
        if worker is not None and record.thread == worker.thread.ident:
            return False
        return super().filter(record)

    def flush(self) -> None:
        """Wait for the queued log records to be sent.

        A venue that stops answering must not hold the interpreter's exit, so
        the wait ends at a deadline and says what it left behind.
        """
        worker = self._worker
        if worker is None or worker.loop.is_closed():
            return
        drained = asyncio.run_coroutine_threadsafe(self._queue.join(), worker.loop)
        try:
            drained.result(_WORKER_JOIN_TIMEOUT_SECONDS)
        except TimeoutError:
            _say_to_the_terminal(
                "DiscordHandler stopped waiting for Discord with "
                f"{self._queue.qsize()} log records still queued"
            )

    def _started_worker(self) -> _Worker:
        """A handler ``dictConfig`` builds takes no start call, so the worker
        begins on the first record it has to carry.
        """
        with self._start_lock:
            if self._worker is None:
                loop = asyncio.new_event_loop()
                self._worker = _Worker(
                    loop,
                    threading.Thread(
                        target=self._run_worker, args=(loop,), daemon=True
                    ),
                )
                self._worker.thread.start()
            return self._worker

    def _run_worker(self, loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._drain())
        finally:
            loop.close()

    async def _drain(self) -> None:
        """One client serves the thread, so its records pace against one
        another.
        """
        async with AsyncDiscordClient.from_webhook_url(
            webhook_url=self.webhook_url,
            timeout_seconds=self._timeout_seconds,
            max_retries=self._max_retries,
            retry_backoff_seconds=self._retry_backoff_seconds,
            bot_name=self.bot_name,
        ) as client:
            while True:
                record = await self._queue.get()
                try:
                    if record is None:
                        return
                    await self._send_record(client, record)
                finally:
                    self._queue.task_done()

    async def _send_record(
        self, client: AsyncDiscordClient, record: logging.LogRecord
    ) -> None:
        if self._circuit_open:
            return
        try:
            await client.send(self._create_embed(record))
            self._consecutive_failures = 0
        except Exception as send_error:
            self._record_failure(send_error)

    def _record_failure(self, send_error: Exception) -> None:
        if _is_permanent_failure(send_error):
            self._disable(f"permanent send failure: {send_error}")
            return
        if _is_rate_limit_hold(send_error):
            logger.error("Dropping a log record: %s", send_error)
            return
        if _is_message_rejection(send_error):
            logger.error("Discord rejected a log message, dropping it: %s", send_error)
            return
        logger.error("Dropping a log record: %s", send_error)
        self._consecutive_failures += 1
        if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            self._disable(
                f"{MAX_CONSECUTIVE_FAILURES} consecutive send failures: {send_error}"
            )

    def _disable(self, reason: str) -> None:
        self._circuit_open = True
        _say_to_the_terminal(f"DiscordHandler disabled after {reason}")

    def _get_colour_for_level(self, levelno: int) -> int:
        if levelno in self.LEVEL_COLOURS:
            return self.LEVEL_COLOURS[levelno]

        for level in reversed(self.LEVEL_COLOURS.keys()):
            if levelno >= level:
                return self.LEVEL_COLOURS[level]

        return self.LEVEL_COLOURS[logging.DEBUG]

    def _create_embed(self, record: logging.LogRecord) -> dict[str, Any]:
        fields: list[dict[str, Any]] = [
            {"name": "Module", "value": record.module, "inline": True},
        ]

        if record.exc_info:
            exc_text = "".join(traceback.format_exception(*record.exc_info))
            if exc_text:
                fields.append(
                    {
                        "name": "Exception",
                        "value": f"```\n{exc_text[:_MAX_EXCEPTION_LENGTH]}\n```",
                        "inline": False,
                    }
                )

        return {
            "title": record.levelname,
            "description": self._format_without_traceback(record)[
                :MAX_DESCRIPTION_LENGTH
            ],
            "color": self._get_colour_for_level(record.levelno),
            "fields": fields,
        }

    def _format_without_traceback(self, record: logging.LogRecord) -> str:
        """Format the record's message alone, keeping the traceback out.

        A formatter that ran on another handler first caches the rendered
        traceback on ``record.exc_text``, and ``Formatter.format`` appends
        that cache even when ``exc_info`` is unset, so both are cleared and
        restored around the call.
        """
        exc_info_backup = record.exc_info
        exc_text_backup = record.exc_text
        record.exc_info = None
        record.exc_text = None
        try:
            return self.format(record)
        finally:
            record.exc_info = exc_info_backup
            record.exc_text = exc_text_backup


def _say_to_the_terminal(message: str) -> None:
    """What this handler cannot deliver cannot be reported through logging
    either, so it goes to the stream the interpreter keeps for that.
    """
    print(message, file=sys.stderr)


_WEBHOOK_DEAD_STATUS_CODES = frozenset(
    {httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN, httpx.codes.NOT_FOUND}
)


def _is_permanent_failure(send_error: Exception) -> bool:
    """The webhook itself is gone or inaccessible, so no message can ever land."""
    return _status_code(send_error) in _WEBHOOK_DEAD_STATUS_CODES


def _is_rate_limit_hold(send_error: Exception) -> bool:
    """Discord asked for a delay this record ran out of attempts waiting for;
    the webhook is healthy and the next record still goes.
    """
    return _status_code(send_error) == httpx.codes.TOO_MANY_REQUESTS


def _is_message_rejection(send_error: Exception) -> bool:
    """Discord refused this one message; the webhook still accepts others."""
    status_code = _status_code(send_error)
    if status_code is None:
        return False
    is_client_error = (
        httpx.codes.BAD_REQUEST <= status_code < httpx.codes.INTERNAL_SERVER_ERROR
    )
    return (
        is_client_error
        and status_code not in _WEBHOOK_DEAD_STATUS_CODES
        and status_code != httpx.codes.TOO_MANY_REQUESTS
    )


def _status_code(send_error: Exception) -> int | None:
    if not isinstance(send_error, httpx.HTTPStatusError):
        return None
    return int(send_error.response.status_code)
