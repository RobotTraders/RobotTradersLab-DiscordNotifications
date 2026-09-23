import importlib
from collections.abc import Iterator

import pytest
from robottraderslab_discord_notifications.webhook import colours

_VARIABLES = (
    "DISCORD_GREEN",
    "DISCORD_RED",
    "DISCORD_BLUE",
    "DISCORD_YELLOW",
    "DISCORD_ORANGE",
    "DISCORD_GREY",
)


@pytest.fixture(autouse=True)
def _restore_palette() -> Iterator[None]:
    yield
    importlib.reload(colours)


@pytest.mark.parametrize(
    ("stated", "expected"),
    [
        ("0x123456", 0x123456),
        ("1193046", 0x123456),
    ],
    ids=["hexadecimal", "decimal"],
)
def test_environment_overrides_a_colour(monkeypatch, stated, expected):
    monkeypatch.setenv("DISCORD_GREEN", stated)

    overridden = importlib.reload(colours)

    assert overridden.GREEN == expected


def test_a_malformed_override_names_its_variable(monkeypatch):
    monkeypatch.setenv("DISCORD_GREEN", "notacolour")

    with pytest.raises(ValueError, match="DISCORD_GREEN"):
        importlib.reload(colours)


def test_palette_defaults_without_overrides(monkeypatch):
    for variable in _VARIABLES:
        monkeypatch.delenv(variable, raising=False)

    reloaded = importlib.reload(colours)

    assert reloaded.GREEN == 0x2ECC71
    assert reloaded.RED == 0xE74C3C
    assert reloaded.BLUE == 0x3498DB
    assert reloaded.YELLOW == 0xF1C40F
    assert reloaded.ORANGE == 0xE67E22
    assert reloaded.GREY == 0x95A5A6
