"""Mesin status tiket (rancangan bagian 5.1). Tabel di sini adalah sumber kebenaran, bukan diagram.

Domain murni: tidak mengimpor basis data, bus, atau FastAPI.
"""

from collections.abc import Iterable
from typing import Final, Literal, get_args

Status = Literal["baru", "ditinjau", "diselidiki", "eskalasi", "selesai"]
Peran = Literal["analis", "dfir", "pimpinan", "admin"]
Kesimpulan = Literal["insiden_benar", "bukan_insiden", "ditutup_tanpa_tindakan"]

STATUS: Final[tuple[Status, ...]] = get_args(Status)
PERAN: Final[tuple[Peran, ...]] = get_args(Peran)
KESIMPULAN: Final[tuple[Kesimpulan, ...]] = get_args(Kesimpulan)

STATUS_AWAL: Final[Status] = "baru"

_PENGOLAH: Final[frozenset[str]] = frozenset({"analis", "dfir", "admin"})
_LANJUTAN: Final[frozenset[str]] = frozenset({"dfir", "admin"})

TRANSISI: Final[dict[tuple[str, str], frozenset[str]]] = {
    ("baru", "ditinjau"): _PENGOLAH,
    ("ditinjau", "diselidiki"): _PENGOLAH,
    ("ditinjau", "selesai"): _PENGOLAH,
    ("diselidiki", "eskalasi"): _PENGOLAH,
    ("diselidiki", "selesai"): _PENGOLAH,
    ("eskalasi", "diselidiki"): _LANJUTAN,
    ("eskalasi", "selesai"): _LANJUTAN,
    # selesai -> ditinjau tidak ada di sini: hanya oleh sistem saat versi baru datang (status_setelah_versi_baru).
}

KESIMPULAN_WAJIB: Final[frozenset[str]] = frozenset({"selesai"})


class TransisiDitolak(ValueError):
    """Perubahan status tidak sah. `kode` dipakai API sebagai kode galat 422 (atau 403 untuk peran)."""

    def __init__(self, kode: str, pesan: str) -> None:
        super().__init__(pesan)
        self.kode = kode


def transisi_diizinkan(dari: str, ke: str, peran: str) -> bool:
    return peran in TRANSISI.get((dari, ke), frozenset())


def periksa_transisi(dari: str, ke: str, peran: str | Iterable[str], kesimpulan: str | None = None) -> None:
    """Lempar `TransisiDitolak` bila pengguna dengan `peran` (satu atau beberapa) tidak boleh memindah `dari` -> `ke`.

    Kode mengikuti rancangan 3.1: `TRANSISI_TIDAK_VALID`, `PERAN_TIDAK_CUKUP`, `KESIMPULAN_WAJIB`.
    """
    semua = {peran} if isinstance(peran, str) else set(peran)
    pasangan = (dari, ke)
    if pasangan not in TRANSISI:
        raise TransisiDitolak("TRANSISI_TIDAK_VALID", f"transisi {dari} -> {ke} tidak diizinkan")
    if not semua & TRANSISI[pasangan]:
        raise TransisiDitolak("PERAN_TIDAK_CUKUP", f"peran tidak boleh memindah {dari} -> {ke}")
    if ke in KESIMPULAN_WAJIB:
        if kesimpulan is None:
            raise TransisiDitolak("KESIMPULAN_WAJIB", f"status {ke} wajib disertai kesimpulan")
        if kesimpulan not in KESIMPULAN:
            raise TransisiDitolak("KESIMPULAN_TIDAK_VALID", "kesimpulan tidak dikenal")


def status_setelah_versi_baru(status: Status) -> Status:
    """Versi kartu baru membuka ulang tiket yang sudah `selesai` (K11); status lain tetap."""
    return "ditinjau" if status == "selesai" else status


def boleh_menulis(peran: str | Iterable[str]) -> bool:
    """`pimpinan` hanya membaca (K8). Beberapa peran: cukup salah satu yang boleh."""
    semua = {peran} if isinstance(peran, str) else set(peran)
    return bool(semua & _PENGOLAH)


def boleh_membaca(peran: str | Iterable[str]) -> bool:
    semua = {peran} if isinstance(peran, str) else set(peran)
    return bool(semua & set(PERAN))
