import json
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, NamedTuple

WEBHOOK_ID = "111111111"
OTHER_WEBHOOK_ID = "222222222"
WEBHOOK_TOKEN = "first-token"
SERVED_LIMIT = 5
SERVED_RESET_AFTER = 2.0
NO_DELAY = 0.0
_LOOPBACK = "127.0.0.1"
_ANY_FREE_PORT = 0
_WINDOW_ID = "1470173023"
_NO_BODY = b""


class Arrival(NamedTuple):
    webhook_id: str
    at: float
    body: dict[str, Any]


class Answer(NamedTuple):
    status: int
    headers: dict[str, str]
    body: bytes


class FakeDiscord:
    """A Discord stand-in on the loopback interface.

    It keeps one route bucket per webhook, announces it in the headers
    Discord announces it in, and refuses a post arriving on a bucket it has
    already served in full, so a client is measured against a venue counting
    the way the real one does.
    """

    def __init__(self) -> None:
        self.status = HTTPStatus.NO_CONTENT
        self.limit = SERVED_LIMIT
        self.retry_after = SERVED_RESET_AFTER
        self.refuse_everything = False
        self.drop_connection = False
        self.answer_delay = NO_DELAY
        self.arrivals: list[Arrival] = []
        self.refusals: list[Arrival] = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(
            (_LOOPBACK, _ANY_FREE_PORT), _request_handler(self)
        )

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def answer(self, webhook_id: str, body: dict[str, Any]) -> Answer:
        """Record a post and answer it the way the venue would.

        A bucket is spent by the posts still inside its window, so a client
        pacing from the announced count never draws a refusal.
        """
        with self._lock:
            arrival = Arrival(webhook_id, time.monotonic(), body)
            self.arrivals.append(arrival)
            spent = [
                other
                for other in self.arrivals
                if other.webhook_id == webhook_id
                and arrival.at - other.at < SERVED_RESET_AFTER
            ]
            if self.refuse_everything or len(spent) > self.limit:
                self.refusals.append(arrival)
                return _refusal(self.retry_after)
            return Answer(
                self.status,
                _route_headers(
                    self.limit,
                    self.limit - len(spent),
                    spent[0].at + SERVED_RESET_AFTER - arrival.at,
                ),
                _NO_BODY,
            )

    def serve(self) -> None:
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def url(self, webhook_id: str = WEBHOOK_ID, token: str = WEBHOOK_TOKEN) -> str:
        return f"http://{_LOOPBACK}:{self.port}/api/webhooks/{webhook_id}/{token}"


def _route_headers(limit: int, remaining: int, reset_after: float) -> dict[str, str]:
    return {
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": _WINDOW_ID,
        "X-RateLimit-Reset-After": f"{reset_after:.3f}",
        "X-RateLimit-Bucket": "abcd1234",
    }


def _refusal(retry_after: float) -> Answer:
    return Answer(
        HTTPStatus.TOO_MANY_REQUESTS,
        {
            "X-RateLimit-Scope": "user",
            "Retry-After": f"{retry_after:.3f}",
            **_route_headers(SERVED_LIMIT, 0, retry_after),
        },
        json.dumps(
            {
                "message": "You are being rate limited.",
                "retry_after": retry_after,
                "global": False,
            }
        ).encode(),
    )


def _request_handler(fake_discord: FakeDiscord) -> type[BaseHTTPRequestHandler]:
    class RequestHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            posted = self.rfile.read(int(self.headers["Content-Length"]))
            time.sleep(fake_discord.answer_delay)
            answer = fake_discord.answer(self.path.split("/")[-2], json.loads(posted))
            if fake_discord.drop_connection:
                self.close_connection = True
                return
            self.send_response(answer.status)
            for name, value in answer.headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(answer.body)))
            self.end_headers()
            self.wfile.write(answer.body)

        def log_message(self, format: str, *args: Any) -> None:
            """The venue's own log would drown the test output."""

    return RequestHandler
