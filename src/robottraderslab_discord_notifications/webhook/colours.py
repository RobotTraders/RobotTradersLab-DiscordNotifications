import os


def _colour(variable: str, default: int) -> int:
    """One environment variable restyles every bot a machine runs."""
    stated = os.environ.get(variable)
    if not stated:
        return default
    try:
        return int(stated, 0)
    except ValueError as e:
        raise ValueError(f"{variable} is not a valid colour: {stated!r}") from e


GREEN = _colour("DISCORD_GREEN", 0x2ECC71)
RED = _colour("DISCORD_RED", 0xE74C3C)
BLUE = _colour("DISCORD_BLUE", 0x3498DB)
YELLOW = _colour("DISCORD_YELLOW", 0xF1C40F)
ORANGE = _colour("DISCORD_ORANGE", 0xE67E22)
GREY = _colour("DISCORD_GREY", 0x95A5A6)
