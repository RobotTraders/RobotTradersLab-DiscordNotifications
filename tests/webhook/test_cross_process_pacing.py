import os
import subprocess
import sys
from pathlib import Path

import pytest
from fake_discord import (
    OTHER_WEBHOOK_ID,
    SERVED_LIMIT,
    SERVED_RESET_AFTER,
    WEBHOOK_ID,
)

POSTS_PER_PROCESS_ON_ONE_ROUTE = 3
POSTS_PER_PROCESS_ON_THE_ADDRESS = 40
PACED_GLOBAL_PER_SECOND = 47
WIDE_OPEN_ROUTE = 1_000
CHILD_TIMEOUT_SECONDS = 60
NO_STALL_SECONDS = 0.0
MID_RUN_STALL_SECONDS = 0.4
# A child's post is recorded when it reaches the venue, so scheduling delays
# land between the claim and the arrival. Measuring the peak over a shorter
# window leaves that skew room while still catching a limiter that overshoots.
WINDOW_LESS_ARRIVAL_JITTER_SECONDS = 0.6
ARRIVAL_JITTER_SECONDS = 0.2

_CHILD = """
import asyncio, sys, time
from pathlib import Path
from robottraderslab_discord_notifications.webhook import AsyncDiscordClient

EMBED = {"title": "post", "color": 1}

async def main():
    url, gate, posts, stall = (
        sys.argv[1], Path(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]),
    )
    async with AsyncDiscordClient.from_webhook_url(url) as client:
        while not gate.exists():
            await asyncio.sleep(0.005)
        for index in range(posts):
            await client.send(EMBED)
            if stall and index == posts // 2:
                time.sleep(stall)

asyncio.run(main())
"""


def test_processes_on_one_webhook_wait_for_its_reset_instead_of_being_refused(
    venue, tmp_path
):
    gate = tmp_path / "gate"
    children = [
        _start_child(
            venue.url(WEBHOOK_ID),
            gate,
            tmp_path,
            POSTS_PER_PROCESS_ON_ONE_ROUTE,
            stall,
        )
        for stall in (NO_STALL_SECONDS, MID_RUN_STALL_SECONDS)
    ]

    gate.touch()
    _await(children)

    assert len(venue.arrivals) == 2 * POSTS_PER_PROCESS_ON_ONE_ROUTE
    assert venue.refusals == []
    arrivals = sorted(arrival.at for arrival in venue.arrivals)
    assert (
        arrivals[SERVED_LIMIT] - arrivals[0]
        >= SERVED_RESET_AFTER - ARRIVAL_JITTER_SECONDS
    )


def test_processes_on_different_webhooks_stay_under_one_paced_second(venue, tmp_path):
    venue.limit = WIDE_OPEN_ROUTE
    gate = tmp_path / "gate"
    children = [
        _start_child(
            venue.url(webhook_id),
            gate,
            tmp_path,
            POSTS_PER_PROCESS_ON_THE_ADDRESS,
            NO_STALL_SECONDS,
        )
        for webhook_id in (WEBHOOK_ID, OTHER_WEBHOOK_ID)
    ]

    gate.touch()
    _await(children)

    assert len(venue.arrivals) == 2 * POSTS_PER_PROCESS_ON_THE_ADDRESS
    assert venue.refusals == []
    arrivals = sorted(arrival.at for arrival in venue.arrivals)
    assert _rolling_window_peak(arrivals) <= PACED_GLOBAL_PER_SECOND


def _rolling_window_peak(arrivals: list[float]) -> int:
    peak = 0
    window_start = 0
    for index in range(len(arrivals)):
        while (
            arrivals[index] - arrivals[window_start]
            > WINDOW_LESS_ARRIVAL_JITTER_SECONDS
        ):
            window_start += 1
        peak = max(peak, index - window_start + 1)
    return peak


def _start_child(
    webhook_url: str, gate: Path, state_dir: Path, posts: int, stall_seconds: float
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            _CHILD,
            webhook_url,
            str(gate),
            str(posts),
            str(stall_seconds),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "TMP": str(state_dir),
            "TEMP": str(state_dir),
            "TMPDIR": str(state_dir),
        },
    )


def _await(children: list[subprocess.Popen[str]]) -> None:
    for child in children:
        _, stderr = child.communicate(timeout=CHILD_TIMEOUT_SECONDS)
        if child.returncode != 0:
            pytest.fail(f"child exited with {child.returncode}: {stderr}")
