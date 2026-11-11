"""Konsumen `card.verified`: simpan kartu terverifikasi sebagai tiket (rancangan bagian 5.5).

Dipanggil `astria_common.consumer.Consumer` di dalam transaksi yang sudah mencatat `processed_events` (dedupe).
Langkah: kunci baris kartu (`FOR UPDATE`), terapkan aturan versi, lalu tulis `card`, `card_version`, `ticket_event`,
dan `audit.record` (outbox) di transaksi yang sama. Kartu TEMPLAT/SEBAGIAN tetap menjadi tiket (K9). Event
`ticket.*` belum diterbitkan (K3).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from astria_common.audit_client import audit_event
from astria_common.log import get_logger
from astria_common.outbox import enqueue
from astria_contracts import Envelope
from astria_contracts.events.card_verified import CardVerified
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from astria_ticket import prioritas, sla, versi
from astria_ticket.prioritas import BAND
from astria_ticket.status import STATUS_AWAL, status_setelah_versi_baru
from astria_ticket.store import card, card_version, ticket_event

log = get_logger(__name__)

PELAKU_SISTEM = "sistem"
AKTOR_LAYANAN = "svc:ticket"
MAKS_JUDUL = 300


class KartuTidakValid(ValueError):
    """Galat data (permanen): isi `card.verified` tidak bisa dijadikan tiket."""


@dataclass(frozen=True, slots=True)
class Ringkas:
    """Kolom kepala tiket yang diambil dari `dokumen.kode`."""

    judul: str
    pola: str
    band: str
    skor: int


def ringkas(kode: Mapping[str, Any]) -> Ringkas:
    judul, band, skor, pola = kode.get("judul"), kode.get("band"), kode.get("skor"), kode.get("pola", "")
    if not isinstance(judul, str) or not judul.strip():
        raise KartuTidakValid("kode.judul wajib berupa teks")
    if band not in BAND:
        raise KartuTidakValid(f"kode.band tidak dikenal: {band!r}")
    if not isinstance(skor, int) or isinstance(skor, bool) or not 0 <= skor <= 100:
        raise KartuTidakValid(f"kode.skor harus bilangan bulat 0-100: {skor!r}")
    if not isinstance(pola, str):
        raise KartuTidakValid("kode.pola harus teks")
    return Ringkas(judul=judul.strip()[:MAKS_JUDUL], pola=pola, band=band, skor=skor)


def _audit(card_id: str, versi_kartu: int, aksi: str, kunci: str, payload: dict[str, Any], waktu: datetime) -> Envelope:
    return audit_event(
        {
            "idempotency_key": f"ticket:{card_id}:v{versi_kartu}:{kunci}",
            "occurred_at": waktu.isoformat(),
            "actor": AKTOR_LAYANAN,
            "actor_type": "service",
            "action": aksi,
            "target_type": "card",
            "target_id": card_id,
            "via": "system",
            "payload": payload,
        },
        correlation={"card_id": card_id, "version": versi_kartu},
    )


class PenerimaKartu:
    """Handler `card.verified`. `jam` dan `menit_sla` diinjeksi agar bisa diuji."""

    def __init__(
        self,
        menit_sla: Mapping[str, int] = sla.MENIT_BAWAAN,
        jam: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._menit_sla = dict(menit_sla)
        self._jam = jam

    async def on_card_verified(self, conn: AsyncConnection, envelope: Envelope, data: BaseModel) -> None:
        if not isinstance(data, CardVerified):
            raise KartuTidakValid(f"data bukan CardVerified: {type(data).__name__}")
        dok = data.dokumen.model_dump(mode="json")
        card_id, versi_masuk = dok["card_id"], dok["version"]
        kor = envelope.correlation
        if (kor.card_id is not None and kor.card_id != card_id) or (
            kor.version is not None and kor.version != versi_masuk
        ):
            raise KartuTidakValid("correlation card_id/version tidak sama dengan dokumen")
        kepala = ringkas(dok["kode"])
        sha = versi.sha256_kanonik(dok)
        sekarang = self._jam()

        baris = (await conn.execute(select(card).where(card.c.card_id == card_id).with_for_update())).first()
        tersimpan = versi.Tersimpan(baris.versi_terbaru, baris.dokumen_sha256) if baris is not None else None
        keputusan = versi.terapkan_versi(tersimpan, versi_masuk, sha)
        log_kunci = {"card_id": card_id, "version": versi_masuk, "event_id": envelope.id}

        match keputusan:
            case versi.Abaikan(alasan=alasan):
                # Versi lama yang datang terlambat tetap masuk riwayat (tiket tidak berubah); duplikat dilewati.
                await self._simpan_versi(conn, envelope, data, dok, sha, sekarang)
                log.info("kartu_diabaikan", alasan=alasan, **log_kunci)
                return
            case versi.Konflik():
                assert baris is not None
                await enqueue(
                    conn,
                    _audit(
                        card_id,
                        versi_masuk,
                        "ticket.versi_konflik",
                        f"konflik:{envelope.id}",
                        {"sha256_tersimpan": baris.dokumen_sha256, "sha256_masuk": sha, "event_id": envelope.id},
                        sekarang,
                    ),
                )
                log.warning("kartu_versi_konflik", **log_kunci)
                return
            case versi.Buat():
                await self._buat(conn, envelope, data, dok, kepala, sha, sekarang)
            case versi.Perbarui():
                assert baris is not None
                await self._perbarui(conn, envelope, data, dok, kepala, sha, sekarang, baris)

        await self._simpan_versi(conn, envelope, data, dok, sha, sekarang)
        log.info("tiket_disimpan", keputusan=type(keputusan).__name__, **log_kunci)

    @staticmethod
    async def _simpan_versi(
        conn: AsyncConnection,
        envelope: Envelope,
        data: CardVerified,
        dok: dict[str, Any],
        sha: str,
        sekarang: datetime,
    ) -> None:
        """Simpan satu versi kartu ke `card_version`; versi yang sudah ada dibiarkan (idempoten)."""
        await conn.execute(
            pg_insert(card_version)
            .values(
                card_id=dok["card_id"],
                versi=dok["version"],
                dokumen=dok,
                dokumen_sha256=sha,
                status_verifikasi=data.status,
                diterima_pada=sekarang,
                event_id=envelope.id,
            )
            .on_conflict_do_nothing(index_elements=[card_version.c.card_id, card_version.c.versi])
        )

    async def _buat(
        self,
        conn: AsyncConnection,
        envelope: Envelope,
        data: CardVerified,
        dok: dict[str, Any],
        kepala: Ringkas,
        sha: str,
        sekarang: datetime,
    ) -> None:
        card_id, versi_masuk = dok["card_id"], dok["version"]
        prio = prioritas.dari_band(kepala.band)
        target = sla.tenggat(sekarang, kepala.band, self._menit_sla)
        await conn.execute(
            card.insert().values(
                card_id=card_id,
                judul=kepala.judul,
                pola=kepala.pola,
                band=kepala.band,
                skor=kepala.skor,
                status=STATUS_AWAL,
                prioritas=prio,
                prioritas_sumber="band",
                label=[],
                status_verifikasi=data.status,
                versi_terbaru=versi_masuk,
                row_version=1,
                tanggapan_awal_target=target,
                dibuat_pada=sekarang,
                diperbarui_pada=sekarang,
                dokumen=dok,
                dokumen_sha256=sha,
            )
        )
        detail = {"versi": versi_masuk, "prioritas": prio, "status_verifikasi": data.status}
        await conn.execute(
            ticket_event.insert().values(
                card_id=card_id, ts=sekarang, pelaku=PELAKU_SISTEM, jenis="created", detail=detail
            )
        )
        await enqueue(
            conn,
            _audit(
                card_id,
                versi_masuk,
                "ticket.dibuat",
                "dibuat",
                detail | {"band": kepala.band, "tanggapan_awal_target": target.isoformat(), "event_id": envelope.id},
                sekarang,
            ),
        )

    async def _perbarui(
        self,
        conn: AsyncConnection,
        envelope: Envelope,
        data: CardVerified,
        dok: dict[str, Any],
        kepala: Ringkas,
        sha: str,
        sekarang: datetime,
        baris: Any,
    ) -> None:
        card_id, versi_masuk = dok["card_id"], dok["version"]
        status_baru = status_setelah_versi_baru(baris.status)
        nilai: dict[str, Any] = {
            "judul": kepala.judul,
            "pola": kepala.pola,
            "band": kepala.band,
            "skor": kepala.skor,
            "status": status_baru,
            "status_verifikasi": data.status,
            "versi_terbaru": versi_masuk,
            "dokumen": dok,
            "dokumen_sha256": sha,
            "row_version": card.c.row_version + 1,
            "diperbarui_pada": sekarang,
        }
        if baris.prioritas_sumber == "band":
            nilai["prioritas"] = prioritas.dari_band(kepala.band)
        if status_baru != baris.status:
            # Dibuka ulang: kesimpulan lama tidak berlaku untuk versi baru (riwayatnya ada di ticket_event).
            nilai |= {"kesimpulan": None, "kesimpulan_catatan": None}
        if baris.tanggapan_awal_at is None:
            # Band naik sebelum ada tanggapan: tenggat boleh maju, tidak pernah mundur.
            nilai["tanggapan_awal_target"] = min(
                baris.tanggapan_awal_target, sla.tenggat(baris.dibuat_pada, kepala.band, self._menit_sla)
            )
        await conn.execute(update(card).where(card.c.card_id == card_id).values(**nilai))
        detail = {
            "versi_dari": baris.versi_terbaru,
            "versi_ke": versi_masuk,
            "status_dari": baris.status,
            "status_ke": status_baru,
            "band_dari": baris.band,
            "band_ke": kepala.band,
            "status_verifikasi": data.status,
        }
        await conn.execute(
            ticket_event.insert().values(
                card_id=card_id, ts=sekarang, pelaku=PELAKU_SISTEM, jenis="version_bumped", detail=detail
            )
        )
        await enqueue(
            conn,
            _audit(
                card_id, versi_masuk, "ticket.versi_naik", "versi_naik", detail | {"event_id": envelope.id}, sekarang
            ),
        )
