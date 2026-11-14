"""Konsumen `card.verified`: validasi isi kode (tanpa DB) dan aturan versi dengan PostgreSQL sungguhan.

Bagian DB memakai `ASTRIA_TEST_DATABASE_URL` (tersedia di kontainer py devenv); dilewati bila tidak diisi.
"""

from __future__ import annotations

import copy
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import Any

import pytest
from astria_common.consumer import PermanentError
from astria_common.db import Database
from astria_common.events import decode, encode, new_event
from astria_common.outbox import outbox
from astria_contracts import Envelope
from astria_contracts.events.card_verified import CardVerified
from pydantic import SecretStr
from sqlalchemy import func, select, text, update

from astria_ticket import store
from astria_ticket.konsumen import KartuTidakValid, PenerimaKartu, ringkas

DB_URL = os.environ.get("ASTRIA_TEST_DATABASE_URL")
T0 = datetime(2026, 11, 11, 8, 0, tzinfo=UTC)
CARD = "IC-20261111-001"


def _contoh() -> dict[str, Any]:
    """Contoh `card.verified` dari paket contracts (kontrak masuk yang dipegang tim)."""
    berkas = resources.files("astria_contracts").joinpath("schemas/events/examples/card.verified.json")
    if not berkas.is_file():
        pytest.skip("contoh card.verified tidak dikemas di astria_contracts")
    return json.loads(berkas.read_text(encoding="utf-8"))


def _data(versi: int = 1, band: str = "SEDANG", skor: int = 45, status: str = "LULUS", **kode: Any) -> dict[str, Any]:
    return {
        "status": status,
        "percobaan": 1,
        "klaim_total": 1,
        "klaim_lolos": 1 if status == "LULUS" else 0,
        "ditolak": [],
        "policy_version": "verifikasi-1.0.0",
        "checker_version": "verifier-1.0.0",
        "model_terperiksa": "replay-cache",
        "evidence_set_hash": "sha256:" + "0" * 64,
        "dokumen": {
            "card_id": CARD,
            "version": versi,
            "kode": {"judul": "Brute force ke WS-ADM-04", "pola": "P-BRUTE", "skor": skor, "band": band} | kode,
            "model": {"ringkasan": [{"klaim": "20 kegagalan logon.", "bukti": ["EV-hb01-000001"]}]},
        },
    }


def _event(data: dict[str, Any]) -> tuple[Envelope, CardVerified]:
    dok = data["dokumen"]
    env, model = decode(
        encode(new_event("card.verified", data, correlation={"card_id": dok["card_id"], "version": dok["version"]}))
    )
    assert isinstance(model, CardVerified)
    return env, model


# ---------------------------------------------------------------- tanpa DB


def test_ringkas_kode() -> None:
    r = ringkas({"judul": "  Judul  ", "band": "KRITIS", "skor": 77, "pola": "P-AKUN"})
    assert (r.judul, r.band, r.skor, r.pola) == ("Judul", "KRITIS", 77, "P-AKUN")
    assert ringkas({"judul": "x" * 1000, "band": "RENDAH", "skor": 0}).judul == "x" * 300
    assert ringkas({"judul": "J", "band": "RENDAH", "skor": 0}).pola == ""


@pytest.mark.parametrize(
    "kode",
    [
        {"band": "KRITIS", "skor": 1},
        {"judul": " ", "band": "KRITIS", "skor": 1},
        {"judul": "J", "band": "kritis", "skor": 1},
        {"judul": "J", "band": "KRITIS", "skor": 101},
        {"judul": "J", "band": "KRITIS", "skor": True},
        {"judul": "J", "band": "KRITIS", "skor": "7"},
        {"judul": "J", "band": "KRITIS", "skor": 7, "pola": 3},
    ],
)
def test_ringkas_menolak(kode: dict[str, Any]) -> None:
    with pytest.raises(KartuTidakValid):
        ringkas(kode)


def test_kartu_tidak_valid_adalah_galat_permanen() -> None:
    """Consumer langsung mengirim event ke DLQ (common#9), tanpa backoff; tetap ValueError untuk kode lain."""
    assert issubclass(KartuTidakValid, PermanentError)
    assert issubclass(KartuTidakValid, ValueError)
    with pytest.raises(PermanentError):
        ringkas({"judul": "J", "band": "kritis", "skor": 1})


def test_contoh_kontrak_bisa_diringkas() -> None:
    contoh = _contoh()
    env, model = decode(json.dumps(contoh).encode())
    assert env.type == "card.verified"
    assert isinstance(model, CardVerified)
    assert ringkas(model.dokumen.kode).band == "KRITIS"


@pytest.mark.anyio
async def test_correlation_beda_ditolak() -> None:
    env, model = _event(_data())
    env = env.model_copy(update={"correlation": env.correlation.model_copy(update={"version": 9})})
    with pytest.raises(PermanentError):
        await PenerimaKartu(jam=lambda: T0).on_card_verified(None, env, model)  # pyright: ignore[reportArgumentType]


# ---------------------------------------------------------------- DB

db_mark = pytest.mark.skipif(not DB_URL, reason="ASTRIA_TEST_DATABASE_URL tidak diisi")


@pytest.fixture
async def db():
    schema = f"t_{uuid.uuid4().hex[:8]}"
    database = Database(SecretStr(DB_URL or ""), schema=schema, metadata=[outbox.metadata, store.metadata])
    await database.start()
    await database.wait_ready(10)
    yield database
    async with database.engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    await database.stop()


class Jam:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> datetime:
        return self.t


async def _kirim(database: Database, penerima: PenerimaKartu, data: dict[str, Any]) -> None:
    env, model = _event(data)
    async with database.engine.begin() as conn:
        await penerima.on_card_verified(conn, env, model)


async def _kartu(database: Database) -> Any:
    async with database.engine.connect() as conn:
        return (await conn.execute(select(store.card).where(store.card.c.card_id == CARD))).one()


async def _hitung(database: Database) -> dict[str, int]:
    async with database.engine.connect() as conn:
        n = {}
        for nama, tabel in (("versi", store.card_version), ("aktivitas", store.ticket_event), ("outbox", outbox)):
            n[nama] = (await conn.execute(select(func.count()).select_from(tabel))).scalar_one()
        return n


async def _aksi_audit(database: Database) -> list[str]:
    async with database.engine.connect() as conn:
        rows = await conn.execute(select(outbox.c.envelope).order_by(outbox.c.created_at, outbox.c.id))
        return [r.envelope["data"]["action"] for r in rows]


@db_mark
@pytest.mark.anyio
async def test_kartu_baru_jadi_tiket(db: Database) -> None:
    jam = Jam()
    await _kirim(db, PenerimaKartu(jam=jam), _data(band="KRITIS", skor=77))
    k = await _kartu(db)
    assert (k.status, k.prioritas, k.prioritas_sumber, k.versi_terbaru, k.row_version) == ("baru", "P1", "band", 1, 1)
    assert (k.judul, k.pola, k.band, k.skor, k.status_verifikasi) == (
        "Brute force ke WS-ADM-04",
        "P-BRUTE",
        "KRITIS",
        77,
        "LULUS",
    )
    assert k.tanggapan_awal_target == T0 + timedelta(minutes=15)
    assert k.tanggapan_awal_at is None and k.pemilik is None and k.label == []
    assert k.dokumen["card_id"] == CARD and k.dokumen_sha256.startswith("sha256:")
    assert await _hitung(db) == {"versi": 1, "aktivitas": 1, "outbox": 1}
    assert await _aksi_audit(db) == ["ticket.dibuat"]


@db_mark
@pytest.mark.anyio
async def test_event_sama_dua_kali_satu_perubahan(db: Database) -> None:
    p = PenerimaKartu(jam=Jam())
    data = _data()
    await _kirim(db, p, data)
    await _kirim(db, p, copy.deepcopy(data))
    assert await _hitung(db) == {"versi": 1, "aktivitas": 1, "outbox": 1}
    assert (await _kartu(db)).row_version == 1


@db_mark
@pytest.mark.anyio
async def test_v2_setelah_v1_lalu_v1_diabaikan(db: Database) -> None:
    jam = Jam()
    p = PenerimaKartu(jam=jam)
    await _kirim(db, p, _data(versi=1, band="SEDANG", skor=45))
    jam.t = T0 + timedelta(minutes=5)
    await _kirim(db, p, _data(versi=2, band="KRITIS", skor=77, status="SEBAGIAN"))
    k = await _kartu(db)
    assert (k.versi_terbaru, k.band, k.prioritas, k.row_version, k.status_verifikasi) == (
        2,
        "KRITIS",
        "P1",
        2,
        "SEBAGIAN",
    )
    # Band naik sebelum tanggapan: tenggat maju ke dibuat + 15 menit.
    assert k.tanggapan_awal_target == T0 + timedelta(minutes=15)
    await _kirim(db, p, _data(versi=1, band="RENDAH", skor=5))
    assert (await _kartu(db)).versi_terbaru == 2
    assert await _hitung(db) == {"versi": 2, "aktivitas": 2, "outbox": 2}
    assert await _aksi_audit(db) == ["ticket.dibuat", "ticket.versi_naik"]


@db_mark
@pytest.mark.anyio
async def test_v1_datang_setelah_v2(db: Database) -> None:
    p = PenerimaKartu(jam=Jam())
    await _kirim(db, p, _data(versi=2))
    await _kirim(db, p, _data(versi=1))
    k = await _kartu(db)
    assert (k.versi_terbaru, k.row_version) == (2, 1)
    # v1 yang terlambat tetap masuk riwayat versi, tanpa aktivitas atau audit baru.
    assert await _hitung(db) == {"versi": 2, "aktivitas": 1, "outbox": 1}
    await _kirim(db, p, _data(versi=1))
    assert await _hitung(db) == {"versi": 2, "aktivitas": 1, "outbox": 1}


@db_mark
@pytest.mark.anyio
async def test_konflik_tidak_menimpa(db: Database) -> None:
    p = PenerimaKartu(jam=Jam())
    await _kirim(db, p, _data(versi=1, skor=45))
    sebelum = await _kartu(db)
    await _kirim(db, p, _data(versi=1, skor=46))
    sesudah = await _kartu(db)
    assert (sesudah.skor, sesudah.dokumen_sha256, sesudah.row_version) == (45, sebelum.dokumen_sha256, 1)
    assert await _aksi_audit(db) == ["ticket.dibuat", "ticket.versi_konflik"]
    assert (await _hitung(db))["versi"] == 1


@db_mark
@pytest.mark.anyio
async def test_selesai_dibuka_ulang_dan_prioritas_analis_tetap(db: Database) -> None:
    p = PenerimaKartu(jam=Jam())
    await _kirim(db, p, _data(versi=1, band="SEDANG"))
    tanggap = T0 + timedelta(minutes=2)
    async with db.engine.begin() as conn:
        await conn.execute(
            update(store.card)
            .where(store.card.c.card_id == CARD)
            .values(
                status="selesai",
                kesimpulan="bukan_insiden",
                prioritas="P2",
                prioritas_sumber="analis",
                tanggapan_awal_at=tanggap,
            )
        )
    await _kirim(db, p, _data(versi=2, band="RENDAH", skor=10))
    k = await _kartu(db)
    assert (k.status, k.kesimpulan, k.prioritas, k.band) == ("ditinjau", None, "P2", "RENDAH")
    assert k.tanggapan_awal_target == T0 + timedelta(minutes=240)  # sudah ditanggapi: tenggat tidak diubah
    async with db.engine.connect() as conn:
        akt = (await conn.execute(select(store.ticket_event).order_by(store.ticket_event.c.event_id))).all()
    assert [a.jenis for a in akt] == ["created", "version_bumped"]
    assert akt[1].detail["status_dari"] == "selesai" and akt[1].detail["status_ke"] == "ditinjau"


@db_mark
@pytest.mark.anyio
async def test_templat_tetap_jadi_tiket(db: Database) -> None:
    await _kirim(db, PenerimaKartu(jam=Jam()), _data(status="TEMPLAT", band="RENDAH", skor=10))
    k = await _kartu(db)
    assert (k.status_verifikasi, k.prioritas, k.tanggapan_awal_target) == ("TEMPLAT", "P4", T0 + timedelta(days=1))


@db_mark
@pytest.mark.anyio
async def test_data_rusak_membatalkan_transaksi(db: Database) -> None:
    with pytest.raises(KartuTidakValid):
        await _kirim(db, PenerimaKartu(jam=Jam()), _data(band="ENTAH"))
    assert await _hitung(db) == {"versi": 0, "aktivitas": 0, "outbox": 0}
