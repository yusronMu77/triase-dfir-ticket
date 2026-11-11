"""API ticket `/svc/v1` (rancangan 3.1). Dipanggil `api` (BFF), `dfir`, dan `scheduler` dengan token layanan.

Semua endpoint wajib token layanan audiens `svc:ticket` dari pemanggil yang diizinkan (`ASTRIA_TICKET_CALLERS`).
Peran pengguna dibaca dari klaim `act` (`sub`, `roles`); ticket memeriksa peran sendiri (defense in depth, K8):
tulis hanya analis/dfir/admin, `pimpinan` hanya baca. Panggilan layanan tanpa `act` hanya boleh membaca.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from astria_common.auth import ServicePrincipal, require_service
from astria_common.db import Database
from astria_common.errors import error_body
from fastapi import APIRouter, Depends, Header, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncConnection

from astria_ticket import layanan
from astria_ticket.layanan import Aktor, GalatTiket, Perubahan, Saring, VersiBentrok
from astria_ticket.prioritas import Band, Prioritas
from astria_ticket.settings import Settings
from astria_ticket.status import Kesimpulan, Status, boleh_membaca

router = APIRouter(prefix="/svc/v1", tags=["ticket"])

AUDIENCE = "svc:ticket"
CARD_ID = r"^IC-[0-9]{8}-[0-9]{3,}$"
PENGGUNA = r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$"
LABEL = r"^[a-z0-9][a-z0-9_-]{0,31}$"
IDEM = r"^[A-Za-z0-9._:-]{1,200}$"

_service = require_service(AUDIENCE)
CardId = Annotated[str, Path(pattern=CARD_ID)]
IdemKey = Annotated[str | None, Header(alias="Idempotency-Key", pattern=IDEM)]


def sekarang() -> datetime:
    return datetime.now(UTC)


async def _aktor(request: Request, principal: Annotated[ServicePrincipal, Depends(_service)]) -> Aktor:
    settings: Settings = request.app.state.settings
    if principal.subject not in settings.ticket_callers:
        raise GalatTiket("PEMANGGIL_DITOLAK", "layanan ini tidak boleh memanggil ticket", 403)
    sub, roles = layanan.peran_dari_act(principal.act)
    return Aktor(caller=principal.subject, sub=sub, roles=roles)


async def _pembaca(aktor: Annotated[Aktor, Depends(_aktor)]) -> Aktor:
    if aktor.sub is not None and not boleh_membaca(aktor.roles):
        raise GalatTiket("PERAN_TIDAK_CUKUP", "peran tidak boleh membaca tiket", 403)
    return aktor


Pembaca = Annotated[Aktor, Depends(_pembaca)]
Penulis = Annotated[Aktor, Depends(_aktor)]  # peran tulis diperiksa di layanan (wajib_menulis)


def _db(request: Request) -> Database:
    db: Database | None = request.app.state.components.get("postgres")
    if db is None or not db.is_ready:
        raise GalatTiket("TICKET_TIDAK_SIAP", "basis data ticket belum siap", 503)
    return db


@asynccontextmanager
async def _baca(request: Request) -> AsyncIterator[AsyncConnection]:
    async with _db(request).engine.connect() as conn:
        yield conn


async def _tulis(request: Request, kerja: Callable[[AsyncConnection], Awaitable[Any]]) -> Any:
    async with _db(request).engine.begin() as conn:
        return await kerja(conn)


def _settings(request: Request) -> Settings:
    return request.app.state.settings


# ---------------------------------------------------------------- baca


@router.get("/tickets")
async def antrean(
    request: Request,
    _aktor: Pembaca,
    status: Status | None = None,
    priority: Prioritas | None = None,
    band: Band | None = None,
    pemilik: Annotated[str | None, Query(pattern=PENGGUNA)] = None,
    host: Annotated[str | None, Query(min_length=1, max_length=255)] = None,
    dari: datetime | None = None,
    sampai: datetime | None = None,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    saring = Saring(status, priority, band, pemilik, host, _utc(dari), _utc(sampai))
    async with _baca(request) as conn:
        return await layanan.antrean(conn, saring, limit, cursor, sekarang())


@router.get("/tickets/{card_id}")
async def detail(request: Request, card_id: CardId, _aktor: Pembaca) -> dict[str, Any]:
    async with _baca(request) as conn:
        return await layanan.detail(conn, card_id, sekarang())


@router.get("/tickets/{card_id}/versions")
async def daftar_versi(request: Request, card_id: CardId, _aktor: Pembaca) -> dict[str, Any]:
    async with _baca(request) as conn:
        return await layanan.daftar_versi(conn, card_id)


@router.get("/tickets/{card_id}/versions/{versi}")
async def satu_versi(
    request: Request, card_id: CardId, versi: Annotated[int, Path(ge=1)], _aktor: Pembaca
) -> dict[str, Any]:
    async with _baca(request) as conn:
        return await layanan.satu_versi(conn, card_id, versi)


@router.get("/stats")
async def statistik(
    request: Request, _aktor: Pembaca, dari: datetime | None = None, sampai: datetime | None = None
) -> dict[str, Any]:
    async with _baca(request) as conn:
        return await layanan.statistik(conn, _utc(dari), _utc(sampai), sekarang())


def _utc(nilai: datetime | None) -> datetime | None:
    if nilai is not None and nilai.tzinfo is None:
        raise GalatTiket("WAKTU_TANPA_ZONA", "waktu wajib memuat zona (mis. Z)", 400)
    return nilai


# ---------------------------------------------------------------- tulis


class UbahTiket(BaseModel):
    model_config = ConfigDict(extra="forbid")
    if_row_version: int = Field(ge=1)
    status: Status | None = None
    pemilik: str | None = Field(default=None, pattern=PENGGUNA)
    prioritas: Prioritas | None = None
    kesimpulan: Kesimpulan | None = None
    kesimpulan_catatan: str | None = Field(default=None, max_length=8000)
    alasan: str | None = Field(default=None, max_length=8000)


@router.patch("/tickets/{card_id}")
async def ubah(request: Request, card_id: CardId, body: UbahTiket, aktor: Penulis) -> Any:
    p = Perubahan(
        if_row_version=body.if_row_version,
        status=body.status,
        pemilik=body.pemilik,
        hapus_pemilik="pemilik" in body.model_fields_set and body.pemilik is None,
        prioritas=body.prioritas,
        kesimpulan=body.kesimpulan,
        kesimpulan_catatan=body.kesimpulan_catatan,
        alasan=body.alasan,
    )
    maks = _settings(request).ticket_comment_max_chars
    try:
        return await _tulis(request, lambda conn: layanan.ubah(conn, card_id, p, aktor, sekarang(), maks))
    except VersiBentrok as e:
        body_galat = error_body(e.code, e.message)
        body_galat["error"]["row_version"] = e.row_version
        return JSONResponse(body_galat, status_code=409)


class TambahAktivitas(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tipe: Literal["comment", "label"]
    isi: str | None = Field(default=None, max_length=20000)
    label: str | None = Field(default=None, pattern=LABEL)
    aksi: Literal["tambah", "hapus"] = "tambah"


@router.post("/tickets/{card_id}/events", status_code=201)
async def tambah_aktivitas(
    request: Request, card_id: CardId, body: TambahAktivitas, aktor: Penulis, idem: IdemKey = None
) -> Any:
    maks = _settings(request).ticket_comment_max_chars
    hasil, baru = await _tulis(
        request,
        lambda conn: layanan.tambah_aktivitas(
            conn, card_id, body.tipe, body.isi, body.label, body.aksi, aktor, sekarang(), maks, idem
        ),
    )
    return hasil if baru else JSONResponse(hasil, status_code=200)


class UmpanBalik(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Literal["benar", "salah_positif", "perlu_dfir"]
    catatan: str = Field(default="", max_length=8000)


@router.post("/tickets/{card_id}/feedback", status_code=201)
async def umpan_balik(request: Request, card_id: CardId, body: UmpanBalik, aktor: Penulis, idem: IdemKey = None) -> Any:
    maks = _settings(request).ticket_comment_max_chars
    hasil, baru = await _tulis(
        request,
        lambda conn: layanan.umpan_balik(conn, card_id, body.label, body.catatan, aktor, sekarang(), maks, idem),
    )
    return hasil if baru else JSONResponse(hasil, status_code=200)
