"""Aturan versi kartu (rancangan bagian 5.3). Domain murni.

`card.verified` untuk satu kartu bisa datang tidak berurutan atau berulang. Aturan ini membuat urutan tidak penting:
versi lebih lama dan duplikat diabaikan, versi sama dengan isi berbeda dicatat sebagai konflik tanpa menimpa.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any


def sha256_kanonik(dokumen: dict[str, Any]) -> str:
    """Hash JSON kanonik (kunci terurut, tanpa spasi, UTF-8). Sama untuk isi yang sama apa pun urutan kuncinya."""
    teks = json.dumps(dokumen, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return "sha256:" + hashlib.sha256(teks.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Tersimpan:
    versi_terbaru: int
    dokumen_sha256: str


@dataclass(frozen=True, slots=True)
class Buat:
    versi: int


@dataclass(frozen=True, slots=True)
class Abaikan:
    alasan: str


@dataclass(frozen=True, slots=True)
class Konflik:
    versi: int
    alasan: str = "versi sama, isi berbeda"


@dataclass(frozen=True, slots=True)
class Perbarui:
    versi: int


Keputusan = Buat | Abaikan | Konflik | Perbarui


def terapkan_versi(saat: Tersimpan | None, versi_masuk: int, sha256_masuk: str) -> Keputusan:
    if versi_masuk < 1:
        raise ValueError(f"versi kartu harus >= 1, dapat {versi_masuk}")
    if saat is None:
        return Buat(versi=versi_masuk)
    if versi_masuk < saat.versi_terbaru:
        return Abaikan(alasan="versi lebih lama dari yang tersimpan")
    if versi_masuk == saat.versi_terbaru:
        if sha256_masuk == saat.dokumen_sha256:
            return Abaikan(alasan="duplikat")
        return Konflik(versi=versi_masuk)
    return Perbarui(versi=versi_masuk)
