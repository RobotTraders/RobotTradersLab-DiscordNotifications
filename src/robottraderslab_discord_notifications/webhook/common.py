import json
from urllib.parse import urlsplit

import httpx

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 1.0

_RETRY_AFTER_HEADER = "Retry-After"
_RETRY_AFTER_FIELD = "retry_after"
_ASSUMED_RETRY_AFTER_SECONDS = 1.0


def extract_error_message(response: httpx.Response) -> str:
    try:
        error_data = response.json()
        return str(error_data.get("message", response.text))
    except json.JSONDecodeError:
        return response.text


def parse_retry_after(response: httpx.Response) -> float:
    """Read the delay Discord asks a refused caller to keep.

    Discord states the delay in the body of a refusal and repeats it in a
    header, so a body that does not parse still yields the venue's figure.
    """
    stated = _body_retry_after(response)
    if stated is None:
        stated = _seconds(response.headers.get(_RETRY_AFTER_HEADER))
    return _ASSUMED_RETRY_AFTER_SECONDS if stated is None else stated


def webhook_id(webhook_url: str) -> str:
    """Read the public half of a webhook URL, which names it in a log line
    and in machine-shared state while its token stays in this process.
    """
    return urlsplit(webhook_url).path.rstrip("/").split("/")[-2]


def _body_retry_after(response: httpx.Response) -> float | None:
    try:
        return _seconds(response.json().get(_RETRY_AFTER_FIELD))
    except (AttributeError, json.JSONDecodeError):
        return None


def _seconds(stated: str | float | None) -> float | None:
    if stated is None:
        return None
    try:
        return float(stated)
    except ValueError:
        return None
