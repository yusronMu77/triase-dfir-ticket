"""SLA tanggapan awal sebagai fungsi murni (rancangan bagian 5.4, keputusan K4).

MVP hanya mode `24_jam`: tenggat = waktu tiket dibuat + menit per band. Fungsi tidak membaca jam sistem; waktu
diberikan dari luar. Pelanggaran dihitung saat dibaca (`terlambat`), tanpa loop pemeriksa.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Final

from astria_ticket.prioritas import BAND

MENIT_BAWAAN: Final[Mapping[str, int]] = {"KRITIS": 15, "TINGGI": 60, "SEDANG": 240, "RENDAH": 1440}
"""Angka contoh (bukan kebijakan resmi); bisa diubah lewat env ASTRIA_TICKET_SLA_MENIT_*."""


def _wajib_aware(nama: str, waktu: datetime) -> None:
    if waktu.tzinfo is None or waktu.utcoffset() is None:
        raise ValueError(f"{nama} harus bertimezone")


def tenggat(dibuat: datetime, band: str, menit_per_band: Mapping[str, int] = MENIT_BAWAAN) -> datetime:
    _wajib_aware("dibuat", dibuat)
    if band not in BAND:
        raise ValueError(f"band tidak dikenal: {band!r}")
    menit = menit_per_band[band]
    if menit < 0:
        raise ValueError(f"menit SLA band {band} tidak boleh negatif")
    return dibuat + timedelta(minutes=menit)


def terlambat(target: datetime, tanggapan_awal_at: datetime | None, sekarang: datetime) -> bool:
    """Terlambat bila tanggapan awal sesudah tenggat, atau belum ada dan tenggat sudah lewat."""
    _wajib_aware("target", target)
    _wajib_aware("sekarang", sekarang)
    if tanggapan_awal_at is not None:
        _wajib_aware("tanggapan_awal_at", tanggapan_awal_at)
        return tanggapan_awal_at > target
    return sekarang > target
