"""API `/svc/v1`: auth dan peran (tanpa DB), lalu alur lengkap dengan PostgreSQL sungguhan.

Bagian DB memakai `ASTRIA_TEST_DATABASE_URL` (kontainer py devenv); dilewati bila tidak diisi.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from astria_common.auth import (
    JwksSource,
    ServiceTokenVerifier,
    SigningKey,
    issue_service_token,
    load_or_create_signing_key,
)
from astria_common.db import Database
from astria_common.events import decode, encode, new_event
from astria_common.outbox import outbox
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select, text

from astria_ticket import api as api_mod
from astria_ticket import layanan, store
from astria_ticket.konsumen import PenerimaKartu
from astria_ticket.layanan import Aktor, Perubahan, VersiBentrok
from astria_ticket.main import build_app
from astria_ticket.settings import Settings

DB_URL = os.environ.get("ASTRIA_TEST_DATABASE_URL")
T0 = datetime(2026, 11, 11, 8, 0, tzinfo=UTC)
CARD = "IC-20261111-001"


def _verifier(key: SigningKey) -> ServiceTokenVerifier:
    return ServiceTokenVerifier(JwksSource("http://identity.invalid", fetch=lambda: {"keys": [key.public_jwk()]}))


def _auth(key: SigningKey, peran: str | None = "analis", caller: str = "svc:api", sub: str = "usr_ana") -> dict:
    act = {"sub": sub, "roles": [peran]} if peran else None
    token, _ = issue_service_token(key, subject=caller, audience="svc:ticket", act=act)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- tanpa DB


@pytest.fixture
def tanpa_db(tmp_path: Path) -> Iterator[tuple[TestClient, SigningKey]]:
    key = load_or_create_signing_key(tmp_path / "identity.pem")
    with TestClient(build_app(Settings(env="test"), token_verifier=_verifier(key))) as client:  # pyright: ignore[reportCallIssue]
        yield client, key


def test_tanpa_token_401(tanpa_db: tuple[TestClient, SigningKey]) -> None:
    client, _ = tanpa_db
    r = client.get("/svc/v1/tickets")
    assert r.status_code == 401 and r.json()["error"]["code"] == "TOKEN_WAJIB"


def test_audiens_salah_403(tanpa_db: tuple[TestClient, SigningKey]) -> None:
    client, key = tanpa_db
    token, _ = issue_service_token(key, subject="svc:api", audience="svc:ingest")
    r = client.get("/svc/v1/tickets", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "AUDIENS_SALAH"


def test_pemanggil_lain_403(tanpa_db: tuple[TestClient, SigningKey]) -> None:
    client, key = tanpa_db
    r = client.get("/svc/v1/tickets", headers=_auth(key, caller="svc:gateway"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "PEMANGGIL_DITOLAK"


def test_peran_tak_dikenal_tidak_boleh_baca(tanpa_db: tuple[TestClient, SigningKey]) -> None:
    client, key = tanpa_db
    r = client.get("/svc/v1/tickets", headers=_auth(key, peran="tamu"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "PERAN_TIDAK_CUKUP"


def test_act_rusak_401(tanpa_db: tuple[TestClient, SigningKey]) -> None:
    client, key = tanpa_db
    token, _ = issue_service_token(key, subject="svc:api", audience="svc:ticket", act={"sub": 7, "roles": ["analis"]})
    r = client.get("/svc/v1/tickets", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "TOKEN_TIDAK_VALID"


def test_act_bukan_objek_ditolak() -> None:
    with pytest.raises(layanan.GalatTiket) as e:
        layanan.peran_dari_act(["analis"])  # pyright: ignore[reportArgumentType]
    assert (e.value.code, e.value.status) == ("TOKEN_TIDAK_VALID", 401)


def test_tanpa_db_503(tanpa_db: tuple[TestClient, SigningKey]) -> None:
    client, key = tanpa_db
    r = client.get("/svc/v1/tickets", headers=_auth(key))
    assert r.status_code == 503 and r.json()["error"]["code"] == "TICKET_TIDAK_SIAP"


@pytest.mark.parametrize(
    "query",
    ["status=tutup", "priority=P9", "band=kritis", "limit=0", "limit=201", "pemilik=a%20b", "dari=2026-11-11T08:00"],
)
def test_query_tidak_valid(tanpa_db: tuple[TestClient, SigningKey], query: str) -> None:
    client, key = tanpa_db
    assert client.get(f"/svc/v1/tickets?{query}", headers=_auth(key)).status_code == 400


def test_path_card_id_diperiksa(tanpa_db: tuple[TestClient, SigningKey]) -> None:
    client, key = tanpa_db
    assert client.get("/svc/v1/tickets/..%2Fetc", headers=_auth(key)).status_code in (400, 404)
    assert client.get("/svc/v1/tickets/IC-1", headers=_auth(key)).status_code == 400


def test_body_tidak_dikenal_ditolak(tanpa_db: tuple[TestClient, SigningKey]) -> None:
    client, key = tanpa_db
    r = client.patch(f"/svc/v1/tickets/{CARD}", json={"if_row_version": 1, "skor": 99}, headers=_auth(key))
    assert r.status_code == 400


def test_cursor_rusak() -> None:
    with pytest.raises(layanan.GalatTiket) as e:
        layanan.baca_cursor("bukan-cursor")
    assert e.value.code == "CURSOR_TIDAK_VALID"
    waktu, cid = layanan.baca_cursor(layanan.kode_cursor(T0, CARD))
    assert (waktu, cid) == (T0, CARD)


def test_bersihkan_teks() -> None:
    assert layanan.bersihkan_teks("  a\u0000b\nc‮  ", 10) == "ab\nc"
    for teks, kode in (("   ", "ISI_KOSONG"), ("x" * 11, "ISI_TERLALU_PANJANG")):
        with pytest.raises(layanan.GalatTiket) as e:
            layanan.bersihkan_teks(teks, 10)
        assert e.value.code == kode


# ---------------------------------------------------------------- DB

db_mark = pytest.mark.skipif(not DB_URL, reason="ASTRIA_TEST_DATABASE_URL tidak diisi")


class Jam:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> datetime:
        return self.t


def _data(card_id: str, versi: int, band: str, skor: int, host: str = "WS-ADM-04") -> dict[str, Any]:
    return {
        "status": "LULUS",
        "percobaan": 1,
        "klaim_total": 1,
        "klaim_lolos": 1,
        "ditolak": [],
        "policy_version": "verifikasi-1.0.0",
        "checker_version": "verifier-1.0.0",
        "model_terperiksa": "replay-cache",
        "evidence_set_hash": "sha256:" + "0" * 64,
        "dokumen": {
            "card_id": card_id,
            "version": versi,
            "kode": {
                "judul": f"Kartu {card_id}",
                "pola": "P-BRUTE",
                "skor": skor,
                "band": band,
                "entitas": [{"jenis": "host", "nilai": host, "peran": "target", "bukti": ["EV-hb01-000001"]}],
            },
            "model": {"ringkasan": [{"klaim": "x", "bukti": ["EV-hb01-000001"]}]},
        },
    }


async def _seed(schema: str) -> None:
    """Tiga kartu lewat konsumen sungguhan: 001 KRITIS (WS-ADM-04), 002 SEDANG (WS-HR-11), 003 RENDAH."""
    db = Database(SecretStr(DB_URL or ""), schema=schema, metadata=[outbox.metadata, store.metadata])
    await db.start()
    await db.wait_ready(10)
    jam = Jam()
    p = PenerimaKartu(jam=jam)
    for i, (band, skor, host) in enumerate(
        (("KRITIS", 77, "WS-ADM-04"), ("SEDANG", 45, "WS-HR-11"), ("RENDAH", 10, "WS-ADM-04"))
    ):
        jam.t = T0 + timedelta(minutes=i)
        cid = f"IC-20261111-00{i + 1}"
        data = _data(cid, 1, band, skor, host)
        env, model = decode(encode(new_event("card.verified", data, correlation={"card_id": cid, "version": 1})))
        async with db.engine.begin() as conn:
            await p.on_card_verified(conn, env, model)
    await db.stop()


async def _drop(schema: str) -> None:
    db = Database(SecretStr(DB_URL or ""))
    await db.start()
    await db.wait_ready(10)
    async with db.engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    await db.stop()


@pytest.fixture
def dengan_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, SigningKey, str]]:
    key = load_or_create_signing_key(tmp_path / "identity.pem")
    schema = f"t_{uuid.uuid4().hex[:8]}"
    asyncio.run(_seed(schema))
    monkeypatch.setattr(api_mod, "sekarang", lambda: T0 + timedelta(minutes=10))
    settings = Settings(env="test", database_url=SecretStr(DB_URL or ""), db_schema=schema)  # pyright: ignore[reportCallIssue]
    with TestClient(build_app(settings, token_verifier=_verifier(key))) as client:
        deadline = time.monotonic() + 15
        while client.get("/ready").status_code != 200 and time.monotonic() < deadline:
            time.sleep(0.2)
        yield client, key, schema
    asyncio.run(_drop(schema))


async def _audit(schema: str) -> list[dict[str, Any]]:
    db = Database(SecretStr(DB_URL or ""), schema=schema, metadata=[outbox.metadata, store.metadata])
    await db.start()
    await db.wait_ready(10)
    async with db.engine.connect() as conn:
        rows = (await conn.execute(select(outbox.c.envelope).order_by(outbox.c.created_at, outbox.c.id))).all()
    await db.stop()
    return [r.envelope["data"] for r in rows]


@db_mark
def test_antrean_filter_dan_cursor(dengan_db: tuple[TestClient, SigningKey, str]) -> None:
    client, key, _ = dengan_db
    h = _auth(key, "pimpinan")
    semua = client.get("/svc/v1/tickets", headers=h).json()
    assert [i["card_id"] for i in semua["items"]] == ["IC-20261111-003", "IC-20261111-002", "IC-20261111-001"]
    assert semua["next_cursor"] is None
    pertama = semua["items"][2]
    assert {"card_id", "judul", "band", "skor", "status", "prioritas", "row_version", "terlambat"} <= set(pertama)
    # KRITIS 15 menit, jam API = T0 + 10 menit: belum terlambat.
    assert pertama["prioritas"] == "P1" and pertama["terlambat"] is False

    hal1 = client.get("/svc/v1/tickets?limit=2", headers=h).json()
    hal2 = client.get(f"/svc/v1/tickets?limit=2&cursor={hal1['next_cursor']}", headers=h).json()
    assert [i["card_id"] for i in hal1["items"] + hal2["items"]] == [i["card_id"] for i in semua["items"]]
    assert hal2["next_cursor"] is None

    def ids(q: str) -> list[str]:
        return [i["card_id"] for i in client.get(f"/svc/v1/tickets?{q}", headers=h).json()["items"]]

    assert ids("priority=P3") == ["IC-20261111-002"]
    assert ids("band=RENDAH") == ["IC-20261111-003"]
    assert ids("host=WS-ADM-04") == ["IC-20261111-003", "IC-20261111-001"]
    assert ids("status=ditinjau") == []
    assert ids("dari=2026-11-11T08:01:00Z&sampai=2026-11-11T08:02:00Z") == ["IC-20261111-002"]
    assert client.get("/svc/v1/tickets?cursor=xyz", headers=h).json()["error"]["code"] == "CURSOR_TIDAK_VALID"


@db_mark
def test_detail_dan_versi(dengan_db: tuple[TestClient, SigningKey, str]) -> None:
    client, key, _ = dengan_db
    d = client.get(f"/svc/v1/tickets/{CARD}", headers=_auth(key)).json()
    assert d["rule_ids"] == [] and d["dokumen"]["card_id"] == CARD and d["dokumen"]["kode"]["band"] == "KRITIS"
    assert [a["jenis"] for a in d["aktivitas"]] == ["created"]
    assert client.get("/svc/v1/tickets/IC-20261111-999", headers=_auth(key)).json()["error"]["code"] == (
        "TIKET_TIDAK_DITEMUKAN"
    )
    v = client.get(f"/svc/v1/tickets/{CARD}/versions", headers=_auth(key)).json()
    assert [i["versi"] for i in v["items"]] == [1]
    assert client.get(f"/svc/v1/tickets/{CARD}/versions/1", headers=_auth(key)).json()["dokumen"]["version"] == 1
    assert client.get(f"/svc/v1/tickets/{CARD}/versions/2", headers=_auth(key)).status_code == 404
    # Layanan tanpa act (scheduler) boleh membaca.
    assert client.get(f"/svc/v1/tickets/{CARD}", headers=_auth(key, None, "svc:scheduler")).status_code == 200


@db_mark
def test_patch_alur_status(dengan_db: tuple[TestClient, SigningKey, str]) -> None:
    client, key, schema = dengan_db
    url = f"/svc/v1/tickets/{CARD}"
    r = client.patch(url, json={"if_row_version": 1, "status": "ditinjau", "pemilik": "usr_ana"}, headers=_auth(key))
    assert r.status_code == 200, r.text
    b = r.json()
    assert (b["status"], b["pemilik"], b["row_version"]) == ("ditinjau", "usr_ana", 2)
    assert b["tanggapan_awal_at"] == "2026-11-11T08:10:00Z" and b["terlambat"] is False

    # Versi baca lama -> 409 dengan row_version terbaru.
    r = client.patch(url, json={"if_row_version": 1, "status": "diselidiki"}, headers=_auth(key))
    assert (
        r.status_code == 409 and r.json()["error"]["code"] == "VERSI_BENTROK" and r.json()["error"]["row_version"] == 2
    )

    cases = [
        ({"status": "selesai"}, 422, "KESIMPULAN_WAJIB"),
        ({"status": "eskalasi"}, 422, "TRANSISI_TIDAK_VALID"),
        ({"prioritas": "P2"}, 422, "ALASAN_WAJIB"),
        ({"kesimpulan": "bukan_insiden"}, 422, "KESIMPULAN_HANYA_SAAT_SELESAI"),
        ({}, 422, "TIDAK_ADA_PERUBAHAN"),
    ]
    for body, kode_http, kode in cases:
        r = client.patch(url, json={"if_row_version": 2} | body, headers=_auth(key))
        assert (r.status_code, r.json()["error"]["code"]) == (kode_http, kode), body

    r = client.patch(url, json={"if_row_version": 2, "status": "diselidiki"}, headers=_auth(key, "pimpinan"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "PERAN_TIDAK_CUKUP"

    r = client.patch(url, json={"if_row_version": 2, "prioritas": "P2", "alasan": "aset kritis"}, headers=_auth(key))
    assert (r.json()["prioritas"], r.json()["prioritas_sumber"], r.json()["row_version"]) == ("P2", "analis", 3)

    for rv, status, peran in ((3, "diselidiki", "analis"), (4, "eskalasi", "analis")):
        assert (
            client.patch(url, json={"if_row_version": rv, "status": status}, headers=_auth(key, peran)).status_code
            == 200
        )
    # Dari eskalasi hanya dfir/admin.
    body = {"if_row_version": 5, "status": "selesai", "kesimpulan": "insiden_benar", "kesimpulan_catatan": "ditangani"}
    assert client.patch(url, json=body, headers=_auth(key, "analis")).status_code == 403
    r = client.patch(url, json=body, headers=_auth(key, "dfir", sub="usr_dfir"))
    assert r.status_code == 200 and r.json()["kesimpulan"] == "insiden_benar"
    assert client.patch(url, json={"if_row_version": 6, "pemilik": None}, headers=_auth(key)).json()["pemilik"] is None

    d = client.get(url, headers=_auth(key)).json()
    assert d["kesimpulan_catatan"] == "ditangani"
    jenis = [a["jenis"] for a in reversed(d["aktivitas"])]
    assert jenis == [
        "created",
        "status_changed",
        "assigned",
        "priority_changed",
        "status_changed",
        "status_changed",
        "status_changed",
        "assigned",
    ]
    aksi = [a["action"] for a in asyncio.run(_audit(schema)) if a["actor"] != "svc:ticket"]
    # Dua audit dari PATCH pertama berada di satu transaksi (created_at sama): urutan di antara keduanya bebas.
    assert sorted(aksi[:2]) == ["ticket.assigned", "ticket.status_changed"] and aksi[2] == "ticket.priority_changed"
    assert all(a["actor"] in ("usr_ana", "usr_dfir") for a in asyncio.run(_audit(schema)) if a["actor_type"] == "user")


@db_mark
def test_komentar_label_dan_idempotensi(dengan_db: tuple[TestClient, SigningKey, str]) -> None:
    client, key, schema = dengan_db
    url = f"/svc/v1/tickets/{CARD}/events"
    h = _auth(key) | {"Idempotency-Key": "ui-1"}
    r1 = client.post(url, json={"tipe": "comment", "isi": "Cek <b>akun</b> svc-backup\u0000"}, headers=h)
    assert r1.status_code == 201 and r1.json()["detail"]["isi"] == "Cek <b>akun</b> svc-backup"
    r2 = client.post(url, json={"tipe": "comment", "isi": "lain"}, headers=h)
    assert r2.status_code == 200 and r2.json()["id"] == r1.json()["id"]

    assert client.post(url, json={"tipe": "comment", "isi": "x" * 4001}, headers=_auth(key)).json()["error"][
        "code"
    ] == ("ISI_TERLALU_PANJANG")
    assert client.post(url, json={"tipe": "comment"}, headers=_auth(key)).status_code == 422
    assert client.post(url, json={"tipe": "label", "label": "Bad Label"}, headers=_auth(key)).status_code == 400
    assert client.post(url, json={"tipe": "comment", "isi": "x"}, headers=_auth(key, "pimpinan")).status_code == 403

    r = client.post(url, json={"tipe": "label", "label": "phishing"}, headers=_auth(key))
    assert r.status_code == 201 and r.json()["row_version"] == 2
    client.post(url, json={"tipe": "label", "label": "aset-kritis"}, headers=_auth(key))
    client.post(url, json={"tipe": "label", "label": "phishing", "aksi": "hapus"}, headers=_auth(key))
    assert client.get(f"/svc/v1/tickets/{CARD}", headers=_auth(key)).json()["label"] == ["aset-kritis"]
    for body in ({"tipe": "label", "label": "aset-kritis"}, {"tipe": "label", "label": "phishing", "aksi": "hapus"}):
        r = client.post(url, json=body, headers=_auth(key))
        assert r.status_code == 422 and r.json()["error"]["code"] == "TIDAK_ADA_PERUBAHAN"
    assert client.get(f"/svc/v1/tickets/{CARD}", headers=_auth(key)).json()["row_version"] == 4
    komentar = [a for a in asyncio.run(_audit(schema)) if a["action"] == "ticket.comment"]
    assert len(komentar) == 1 and komentar[0]["payload"] == {"panjang": 26}  # isi komentar tidak masuk audit


@db_mark
def test_umpan_balik_dan_statistik(dengan_db: tuple[TestClient, SigningKey, str]) -> None:
    client, key, _ = dengan_db
    url = f"/svc/v1/tickets/{CARD}/feedback"
    h = _auth(key) | {"Idempotency-Key": "cmd-1"}
    r1 = client.post(url, json={"label": "salah_positif"}, headers=h)
    r2 = client.post(url, json={"label": "salah_positif"}, headers=h)
    assert (r1.status_code, r2.status_code) == (201, 200) and r1.json()["feedback_id"] == r2.json()["feedback_id"]
    assert client.post(url, json={"label": "ragu"}, headers=_auth(key)).status_code == 400
    assert client.post(url, json={"label": "benar", "catatan": "\u0000\u200b"}, headers=_auth(key)).status_code == 201
    assert (
        client.post("/svc/v1/tickets/IC-20261111-999/feedback", json={"label": "benar"}, headers=_auth(key)).status_code
        == 404
    )

    s = client.get("/svc/v1/stats", headers=_auth(key, None, "svc:scheduler")).json()
    assert s["total"] == 3 and s["per_band"] == {"KRITIS": 1, "RENDAH": 1, "SEDANG": 1}
    assert s["per_status"] == {"baru": 3} and s["umpan_balik"] == {"benar": 1, "salah_positif": 1}
    assert s["sla_terlanggar"] == 0
    s = client.get("/svc/v1/stats?dari=2026-11-11T08:01:00Z", headers=_auth(key, "pimpinan")).json()
    assert s["total"] == 2 and s["umpan_balik"] == {}


@db_mark
@pytest.mark.anyio
async def test_dua_patch_serentak_satu_409() -> None:
    schema = f"t_{uuid.uuid4().hex[:8]}"
    await _seed(schema)
    db = Database(SecretStr(DB_URL or ""), schema=schema, metadata=[outbox.metadata, store.metadata], pool_size=4)
    await db.start()
    await db.wait_ready(10)
    aktor = Aktor(caller="svc:api", sub="usr_ana", roles=frozenset({"analis"}))

    async def coba(pemilik: str) -> str:
        try:
            async with db.engine.begin() as conn:
                await layanan.ubah(conn, CARD, Perubahan(if_row_version=1, pemilik=pemilik), aktor, T0, 4000)
                await asyncio.sleep(0.2)  # tahan kunci baris agar yang lain menunggu
            return "ok"
        except VersiBentrok:
            return "409"

    try:
        hasil = await asyncio.gather(coba("usr_a"), coba("usr_b"))
        assert sorted(hasil) == ["409", "ok"]
        async with db.engine.connect() as conn:
            rv = (await conn.execute(select(store.card.c.row_version).where(store.card.c.card_id == CARD))).scalar_one()
        assert rv == 2
    finally:
        async with db.engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await db.stop()
