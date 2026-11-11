"""Prioritas tiket dari band skor triage. Domain murni."""

from typing import Final, Literal, get_args

Band = Literal["KRITIS", "TINGGI", "SEDANG", "RENDAH"]
Prioritas = Literal["P1", "P2", "P3", "P4"]

BAND: Final[tuple[Band, ...]] = get_args(Band)
PRIORITAS: Final[tuple[Prioritas, ...]] = get_args(Prioritas)

_DARI_BAND: Final[dict[str, Prioritas]] = {"KRITIS": "P1", "TINGGI": "P2", "SEDANG": "P3", "RENDAH": "P4"}


def dari_band(band: str) -> Prioritas:
    try:
        return _DARI_BAND[band]
    except KeyError:
        raise ValueError(f"band tidak dikenal: {band!r}") from None
