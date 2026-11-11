"""Use case tiket untuk API `/svc/v1`: baca antrean dan detail, ubah tiket, aktivitas, umpan balik, statistik.

Setiap perubahan ditulis bersama `ticket_event` dan `audit.record` (outbox) dalam satu transaksi. Pembaruan kepala
tiket memakai kunci optimistik `row_version` (rancangan 5.2) di bawah `SELECT ... FOR UPDATE`, jadi dua pengubah
serentak dengan `row_version` sama menghasilkan satu sukses dan satu `VERSI_BENTROK`.
"""

from __future__ import annotations

import base64
import binascii
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from astria_common.audit_client import audit_event
from astria_common.errors import AstriaError
from astria_common.outbox import enqueue
from sqlalchemy import cast, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncConnection

from astria_ticket import sla
from astria_ticket.status import TransisiDitolak, boleh_menulis, periksa_transisi
from astria_ticket.store import card, card_version, feedback, ticket_event

MAKS_LABEL = 20
AKTIVITAS_DETAIL = 50


class GalatTiket(AstriaError):
    pass


def tidak_ditemukan() -> GalatTiket:
    return GalatTiket("TIKET_TIDAK_DITEMUKAN", "tiket tidak ditemukan", 404)


class VersiBentrok(GalatTiket):
    def __init__(self, row_version: int) -> None:
        super().__init__("VERSI_BENTROK", "tiket sudah diubah pihak lain; muat ulang", 409)
        self.row_version = row_version


@dataclass(frozen=True, slots=True)
class Aktor:
    """Pengguna di balik token layanan (`act`) atau layanan itu sendiri bila tanpa `act`."""

    caller: str
    sub: str | None
    roles: frozenset[str] = field(default_factory=frozenset)

    @property
    def pelaku(self) -> str:
        return self.sub or self.caller

    def wajib_menulis(self) -> str:
        if self.sub is None or not boleh_menulis(self.roles):
            raise GalatTiket("PERAN_TIDAK_CUKUP", "peran tidak boleh mengubah tiket", 403)
        return self.sub


# ---------------------------------------------------------------- bentuk jawaban


def iso(nilai: datetime | None) -> str | None:
    return nilai.isoformat().replace("+00:00", "Z") if nilai is not None else None


def kepala(r: Any, sekarang: datetime) -> dict[str, Any]:
    return {
        "card_id": r.card_id,
        "judul": r.judul,
        "pola": r.pola,
        "band": r.band,
        "skor": r.skor,
        "status": r.status,
        "prioritas": r.prioritas,
        "prioritas_sumber": r.prioritas_sumber,
        "pemilik": r.pemilik,
        "kesimpulan": r.kesimpulan,
        "label": list(r.label or []),
        "status_verifikasi": r.status_verifikasi,
        "versi": r.versi_terbaru,
        "row_version": r.row_version,
        "tanggapan_awal_target": iso(r.tanggapan_awal_target),
        "tanggapan_awal_at": iso(r.tanggapan_awal_at),
        "terlambat": sla.terlambat(r.tanggapan_awal_target, r.tanggapan_awal_at, sekarang),
        "dibuat_pada": iso(r.dibuat_pada),
        "diperbarui_pada": iso(r.diperbarui_pada),
    }


def aktivitas(r: Any) -> dict[str, Any]:
    return {"id": r.event_id, "ts": iso(r.ts), "pelaku": r.pelaku, "jenis": r.jenis, "detail": r.detail}


# ---------------------------------------------------------------- baca


@dataclass(frozen=True, slots=True)
class Saring:
    status: str | None = None
    prioritas: str | None = None
    band: str | None = None
    pemilik: str | None = None
    host: str | None = None
    dari: datetime | None = None
    sampai: datetime | None = None


def kode_cursor(dibuat: datetime, card_id: str) -> str:
    return base64.urlsafe_b64encode(f"{dibuat.isoformat()}|{card_id}".encode()).decode().rstrip("=")


def baca_cursor(teks: str) -> tuple[datetime, str]:
    try:
        mentah = base64.urlsafe_b64decode(teks + "=" * (-len(teks) % 4)).decode()
        waktu, _, card_id = mentah.partition("|")
        hasil = datetime.fromisoformat(waktu)
        if hasil.tzinfo is None or not card_id:
            raise ValueError
        return hasil, card_id
    except ValueError, binascii.Error, UnicodeDecodeError:
        raise GalatTiket("CURSOR_TIDAK_VALID", "cursor tidak valid", 400) from None


async def antrean(
    conn: AsyncConnection, saring: Saring, limit: int, cursor: str | None, sekarang: datetime
) -> dict[str, Any]:
    q = select(card)
    if saring.status:
        q = q.where(card.c.status == saring.status)
    if saring.prioritas:
        q = q.where(card.c.prioritas == saring.prioritas)
    if saring.band:
        q = q.where(card.c.band == saring.band)
    if saring.pemilik:
        q = q.where(card.c.pemilik == saring.pemilik)
    if saring.host:
        entitas = cast(card.c.dokumen, JSONB)["kode"]["entitas"]
        q = q.where(entitas.contains(cast([{"jenis": "host", "nilai": saring.host}], JSONB)))
    if saring.dari:
        q = q.where(card.c.dibuat_pada >= saring.dari)
    if saring.sampai:
        q = q.where(card.c.dibuat_pada < saring.sampai)
    if cursor:
        waktu, card_id = baca_cursor(cursor)
        q = q.where(tuple_(card.c.dibuat_pada, card.c.card_id) < tuple_(waktu, card_id))
    rows = (await conn.execute(q.order_by(card.c.dibuat_pada.desc(), card.c.card_id.desc()).limit(limit + 1))).all()
    lanjut = None
    if len(rows) > limit:
        rows = rows[:limit]
        lanjut = kode_cursor(rows[-1].dibuat_pada, rows[-1].card_id)
    return {"items": [kepala(r, sekarang) for r in rows], "next_cursor": lanjut}


async def _baris(conn: AsyncConnection, card_id: str, *, kunci: bool = False) -> Any:
    q = select(card).where(card.c.card_id == card_id)
    r = (await conn.execute(q.with_for_update() if kunci else q)).first()
    if r is None:
        raise tidak_ditemukan()
    return r


async def detail(conn: AsyncConnection, card_id: str, sekarang: datetime) -> dict[str, Any]:
    r = await _baris(conn, card_id)
    akt = (
        await conn.execute(
            select(ticket_event)
            .where(ticket_event.c.card_id == card_id)
            .order_by(ticket_event.c.event_id.desc())
            .limit(AKTIVITAS_DETAIL)
        )
    ).all()
    return kepala(r, sekarang) | {
        "kesimpulan_catatan": r.kesimpulan_catatan,
        # Rule id belum ada di `kode` triage (K6): daftar kosong sampai triage#4.
        "rule_ids": [],
        "dokumen": r.dokumen,
        "aktivitas": [aktivitas(a) for a in akt],
    }


async def daftar_versi(conn: AsyncConnection, card_id: str) -> dict[str, Any]:
    await _baris(conn, card_id)
    rows = (
        await conn.execute(
            select(
                card_version.c.versi,
                card_version.c.dokumen_sha256,
                card_version.c.status_verifikasi,
                card_version.c.diterima_pada,
            )
            .where(card_version.c.card_id == card_id)
            .order_by(card_version.c.versi)
        )
    ).all()
    return {
        "card_id": card_id,
        "items": [
            {
                "versi": r.versi,
                "dokumen_sha256": r.dokumen_sha256,
                "status_verifikasi": r.status_verifikasi,
                "diterima_pada": iso(r.diterima_pada),
            }
            for r in rows
        ],
    }


async def satu_versi(conn: AsyncConnection, card_id: str, versi: int) -> dict[str, Any]:
    r = (
        await conn.execute(select(card_version).where(card_version.c.card_id == card_id, card_version.c.versi == versi))
    ).first()
    if r is None:
        raise GalatTiket("VERSI_TIDAK_DITEMUKAN", "versi kartu tidak ditemukan", 404)
    return {
        "card_id": card_id,
        "versi": r.versi,
        "dokumen_sha256": r.dokumen_sha256,
        "status_verifikasi": r.status_verifikasi,
        "diterima_pada": iso(r.diterima_pada),
        "dokumen": r.dokumen,
    }


# ---------------------------------------------------------------- tulis


def bersihkan_teks(teks: str, maks: int, *, boleh_kosong: bool = False) -> str:
    """Buang karakter kontrol (kecuali baris baru dan tab), rapikan tepi; tolak bila terlalu panjang, atau kosong
    kecuali `boleh_kosong`."""
    bersih = "".join(c for c in teks if c in "\n\t" or unicodedata.category(c)[0] != "C").strip()
    if not bersih and not boleh_kosong:
        raise GalatTiket("ISI_KOSONG", "isi tidak boleh kosong", 422)
    if len(bersih) > maks:
        raise GalatTiket("ISI_TERLALU_PANJANG", f"isi melebihi {maks} karakter", 422)
    return bersih


def _audit(aktor: str, aksi: str, card_id: str, kunci: str, payload: dict[str, Any], waktu: datetime) -> Any:
    return audit_event(
        {
            "idempotency_key": f"ticket:{card_id}:{kunci}"[:200],
            "occurred_at": waktu.isoformat(),
            "actor": aktor,
            "actor_type": "user",
            "action": aksi,
            "target_type": "card",
            "target_id": card_id,
            "via": "api",
            "payload": payload,
        },
        correlation={"card_id": card_id},
    )


async def _catat(
    conn: AsyncConnection,
    card_id: str,
    pelaku: str,
    jenis: str,
    detail_: Mapping[str, Any],
    waktu: datetime,
    idem: str | None = None,
) -> int:
    return (
        await conn.execute(
            ticket_event.insert()
            .values(card_id=card_id, ts=waktu, pelaku=pelaku, jenis=jenis, detail=dict(detail_), idempotency_key=idem)
            .returning(ticket_event.c.event_id)
        )
    ).scalar_one()


@dataclass(frozen=True, slots=True)
class Perubahan:
    if_row_version: int
    status: str | None = None
    pemilik: str | None = None
    hapus_pemilik: bool = False
    prioritas: str | None = None
    kesimpulan: str | None = None
    kesimpulan_catatan: str | None = None
    alasan: str | None = None

    def kosong(self) -> bool:
        return self.status is None and self.pemilik is None and not self.hapus_pemilik and self.prioritas is None


async def ubah(
    conn: AsyncConnection, card_id: str, p: Perubahan, aktor: Aktor, sekarang: datetime, maks_catatan: int
) -> dict[str, Any]:
    sub = aktor.wajib_menulis()
    if (p.kesimpulan is not None or p.kesimpulan_catatan is not None) and p.status != "selesai":
        raise GalatTiket("KESIMPULAN_HANYA_SAAT_SELESAI", "kesimpulan hanya dikirim bersama status selesai", 422)
    if p.kosong():
        raise GalatTiket("TIDAK_ADA_PERUBAHAN", "tidak ada field yang diubah", 422)
    alasan = bersihkan_teks(p.alasan, maks_catatan) if p.alasan is not None else None
    if p.prioritas is not None and alasan is None:
        raise GalatTiket("ALASAN_WAJIB", "ubah prioritas wajib disertai alasan", 422)
    catatan = bersihkan_teks(p.kesimpulan_catatan, maks_catatan) if p.kesimpulan_catatan is not None else None

    r = await _baris(conn, card_id, kunci=True)
    if r.row_version != p.if_row_version:
        raise VersiBentrok(r.row_version)

    nilai: dict[str, Any] = {}
    jejak: list[tuple[str, dict[str, Any]]] = []
    if p.status is not None and p.status != r.status:
        try:
            periksa_transisi(r.status, p.status, aktor.roles, p.kesimpulan)
        except TransisiDitolak as e:
            raise GalatTiket(e.kode, str(e), 403 if e.kode == "PERAN_TIDAK_CUKUP" else 422) from None
        nilai["status"] = p.status
        if p.status == "selesai":
            nilai |= {"kesimpulan": p.kesimpulan, "kesimpulan_catatan": catatan}
        if r.tanggapan_awal_at is None:
            nilai["tanggapan_awal_at"] = sekarang
        jejak.append(("status_changed", {"dari": r.status, "ke": p.status, "kesimpulan": p.kesimpulan}))
    elif p.status is not None and p.kesimpulan is not None:
        raise GalatTiket("TRANSISI_TIDAK_VALID", "status sudah selesai", 422)

    pemilik_baru = None if p.hapus_pemilik else p.pemilik
    if (p.hapus_pemilik or p.pemilik is not None) and pemilik_baru != r.pemilik:
        nilai["pemilik"] = pemilik_baru
        jejak.append(("assigned", {"dari": r.pemilik, "ke": pemilik_baru}))

    if p.prioritas is not None and (p.prioritas != r.prioritas or r.prioritas_sumber != "analis"):
        nilai |= {"prioritas": p.prioritas, "prioritas_sumber": "analis"}
        jejak.append(("priority_changed", {"dari": r.prioritas, "ke": p.prioritas}))

    if nilai:
        row_baru = r.row_version + 1
        nilai |= {"row_version": row_baru, "diperbarui_pada": sekarang}
        await conn.execute(update(card).where(card.c.card_id == card_id).values(**nilai))
        for jenis, d in jejak:
            d = d | ({"alasan": alasan} if alasan else {})
            await _catat(conn, card_id, sub, jenis, d, sekarang)
            await enqueue(conn, _audit(sub, f"ticket.{jenis}", card_id, f"r{row_baru}:{jenis}", d, sekarang))
    return kepala(await _baris(conn, card_id), sekarang)


async def tambah_aktivitas(
    conn: AsyncConnection,
    card_id: str,
    tipe: str,
    isi: str | None,
    label: str | None,
    aksi_label: str,
    aktor: Aktor,
    sekarang: datetime,
    maks_komentar: int,
    idem: str | None,
) -> tuple[dict[str, Any], bool]:
    """Komentar atau label. Hasil kedua: `True` bila baru, `False` bila ulangan dengan Idempotency-Key sama."""
    sub = aktor.wajib_menulis()
    r = await _baris(conn, card_id, kunci=True)
    if idem:
        lama = (
            await conn.execute(
                select(ticket_event).where(ticket_event.c.card_id == card_id, ticket_event.c.idempotency_key == idem)
            )
        ).first()
        if lama is not None:
            return aktivitas(lama) | {"row_version": r.row_version}, False
    if tipe == "comment":
        if isi is None:
            raise GalatTiket("ISI_KOSONG", "komentar wajib berisi teks", 422)
        detail_ = {"isi": bersihkan_teks(isi, maks_komentar)}
        jenis = "comment"
        row_version = r.row_version
    else:
        if label is None:
            raise GalatTiket("LABEL_WAJIB", "label wajib diisi", 422)
        sekarang_label: list[str] = list(r.label or [])
        if (label in sekarang_label) == (aksi_label == "tambah"):
            raise GalatTiket("TIDAK_ADA_PERUBAHAN", "label sudah dalam keadaan itu", 422)
        if aksi_label == "tambah":
            if len(sekarang_label) >= MAKS_LABEL:
                raise GalatTiket("LABEL_TERLALU_BANYAK", f"maksimal {MAKS_LABEL} label", 422)
            sekarang_label.append(label)
        else:
            sekarang_label.remove(label)
        detail_ = {"label": label, "aksi": aksi_label}
        jenis = "label"
        row_version = r.row_version + 1
        await conn.execute(
            update(card)
            .where(card.c.card_id == card_id)
            .values(label=sorted(sekarang_label), row_version=row_version, diperbarui_pada=sekarang)
        )
    event_id = await _catat(conn, card_id, sub, jenis, detail_, sekarang, idem)
    payload = detail_ if jenis == "label" else {"panjang": len(detail_["isi"])}
    await enqueue(conn, _audit(sub, f"ticket.{jenis}", card_id, f"e{event_id}", payload, sekarang))
    hasil = {"id": event_id, "ts": iso(sekarang), "pelaku": sub, "jenis": jenis, "detail": detail_}
    return hasil | {"row_version": row_version}, True


async def umpan_balik(
    conn: AsyncConnection,
    card_id: str,
    label: str,
    catatan: str,
    aktor: Aktor,
    sekarang: datetime,
    maks_catatan: int,
    idem: str | None,
) -> tuple[dict[str, Any], bool]:
    sub = aktor.wajib_menulis()
    await _baris(conn, card_id)
    bersih = bersihkan_teks(catatan, maks_catatan, boleh_kosong=True)
    stmt = insert(feedback).values(
        card_id=card_id, analis=sub, label=label, catatan=bersih, dibuat_pada=sekarang, idempotency_key=idem
    )
    if idem:
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["card_id", "idempotency_key"], index_where=feedback.c.idempotency_key.is_not(None)
        )
    stmt = stmt.returning(feedback.c.feedback_id)
    fid = (await conn.execute(stmt)).scalar_one_or_none()
    if fid is None:
        lama = (
            await conn.execute(
                select(feedback).where(feedback.c.card_id == card_id, feedback.c.idempotency_key == idem)
            )
        ).one()
        return {"feedback_id": lama.feedback_id, "card_id": card_id, "label": lama.label}, False
    await enqueue(
        conn, _audit(sub, "ticket.feedback", card_id, f"f{fid}", {"label": label, "feedback_id": fid}, sekarang)
    )
    return {"feedback_id": fid, "card_id": card_id, "label": label}, True


async def statistik(
    conn: AsyncConnection, dari: datetime | None, sampai: datetime | None, sekarang: datetime
) -> dict[str, Any]:
    syarat = []
    if dari:
        syarat.append(card.c.dibuat_pada >= dari)
    if sampai:
        syarat.append(card.c.dibuat_pada < sampai)

    async def hitung(kolom: Any) -> dict[str, int]:
        rows = await conn.execute(select(kolom, func.count()).where(*syarat).group_by(kolom).order_by(kolom))
        return {k: n for k, n in rows.all()}

    rows = (await conn.execute(select(card.c.tanggapan_awal_target, card.c.tanggapan_awal_at).where(*syarat))).all()
    terlambat = sum(1 for t, a in rows if sla.terlambat(t, a, sekarang))
    fb_syarat = [feedback.c.card_id.in_(select(card.c.card_id).where(*syarat))] if syarat else []
    fb = {
        k: n
        for k, n in (
            await conn.execute(select(feedback.c.label, func.count()).where(*fb_syarat).group_by(feedback.c.label))
        ).all()
    }
    return {
        "dari": iso(dari),
        "sampai": iso(sampai),
        "total": len(rows),
        "per_status": await hitung(card.c.status),
        "per_band": await hitung(card.c.band),
        "per_prioritas": await hitung(card.c.prioritas),
        "sla_terlanggar": terlambat,
        "umpan_balik": fb,
    }


def peran_dari_act(act: Mapping[str, Any] | None) -> tuple[str | None, frozenset[str]]:
    """`act` dari token layanan: `sub` (id pengguna) dan `roles`. Bentuk salah = ditolak."""
    if act is None:
        return None, frozenset()
    if not isinstance(act, Mapping):
        raise GalatTiket("TOKEN_TIDAK_VALID", "klaim act tidak valid", 401)
    sub, roles = act.get("sub"), act.get("roles", [])
    if not isinstance(sub, str) or not 1 <= len(sub) <= 64 or not sub.isprintable():
        raise GalatTiket("TOKEN_TIDAK_VALID", "klaim act.sub tidak valid", 401)
    if not isinstance(roles, list) or not all(isinstance(x, str) for x in roles):
        raise GalatTiket("TOKEN_TIDAK_VALID", "klaim act.roles tidak valid", 401)
    return sub, frozenset(roles)
