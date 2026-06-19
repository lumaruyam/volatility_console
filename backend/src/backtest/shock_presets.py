"""Named historical shock date ranges — PDF Part XVI."""

from __future__ import annotations

_PRESETS: dict[str, tuple[str, str]] = {
    "2008 Crash":           ("2008-09-01", "2009-03-31"),
    "2020 Liquidity Shock": ("2020-02-01", "2020-04-30"),
    "BREXIT":               ("2016-06-01", "2016-09-30"),
    "COVID Vol Spike":      ("2020-03-01", "2020-05-31"),
}

_DEFAULT: tuple[str, str] = ("2020-02-01", "2020-04-30")


def shock_date_range(preset: str) -> tuple[str, str]:
    """Return (start_date, end_date) ISO strings for a named shock preset.

    Falls back to the 2020 Liquidity Shock window for unknown preset names.
    """
    return _PRESETS.get(preset, _DEFAULT)


def available_presets() -> list[str]:
    """Return the list of named shock presets in chronological order."""
    return sorted(_PRESETS, key=lambda k: _PRESETS[k][0])
