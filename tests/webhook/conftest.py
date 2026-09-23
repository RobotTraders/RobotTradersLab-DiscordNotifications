from collections.abc import Iterator

import pytest
from fake_discord import FakeDiscord


@pytest.fixture
def venue() -> Iterator[FakeDiscord]:
    fake_discord = FakeDiscord()
    fake_discord.serve()
    yield fake_discord
    fake_discord.stop()
