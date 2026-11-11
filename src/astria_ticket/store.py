"""Tabel milik ticket (skema layanan; `create_all`, sama dengan layanan lain sampai Alembic). PostgreSQL.

| Tabel | Isi |
|---|---|
| card | Agregat tiket: satu baris per `card_id`, status kerja analis + dokumen kartu versi terbaru |
| card_version | Dokumen kartu per versi yang diterima dari `card.verified`; tidak pernah dihapus |
| ticket_event | Aktivitas tiket (dibuat, status, pemilik, komentar, versi naik, ...); tidak pernah dihapus |
| feedback | Umpan balik analis per kartu |

Klaim tidak punya tabel sendiri: dibaca dari `dokumen.model` (K7). `processed_events` dan `outbox` milik common.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    text,
)

from astria_ticket.prioritas import BAND, PRIORITAS
from astria_ticket.status import KESIMPULAN, STATUS

JENIS_AKTIVITAS = (
    "created",
    "status_changed",
    "assigned",
    "comment",
    "priority_changed",
    "label",
    "version_bumped",
    "conclusion",
)
LABEL_UMPAN_BALIK = ("benar", "salah_positif", "perlu_dfir")
STATUS_VERIFIKASI = ("LULUS", "SEBAGIAN", "TEMPLAT")


def _dalam(kolom: str, nilai: tuple[str, ...]) -> str:
    return f"{kolom} IN ({', '.join(repr(v) for v in nilai)})"


metadata = MetaData()

card = Table(
    "card",
    metadata,
    Column("card_id", Text, primary_key=True),
    Column("judul", Text, nullable=False),
    Column("pola", Text, nullable=False),
    Column("band", Text, nullable=False),
    Column("skor", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("prioritas", Text, nullable=False),
    Column("prioritas_sumber", Text, nullable=False, server_default="band"),
    Column("pemilik", Text, nullable=True),
    Column("kesimpulan", Text, nullable=True),
    Column("kesimpulan_catatan", Text, nullable=True),
    Column("label", JSON, nullable=False, server_default=text("'[]'")),
    Column("status_verifikasi", Text, nullable=False),
    Column("versi_terbaru", Integer, nullable=False),
    Column("row_version", Integer, nullable=False, server_default="1"),
    Column("tanggapan_awal_target", DateTime(timezone=True), nullable=False),
    Column("tanggapan_awal_at", DateTime(timezone=True), nullable=True),
    Column("dibuat_pada", DateTime(timezone=True), nullable=False),
    Column("diperbarui_pada", DateTime(timezone=True), nullable=False),
    Column("dokumen", JSON, nullable=False),
    Column("dokumen_sha256", Text, nullable=False),
    CheckConstraint(_dalam("band", BAND), name="card_band"),
    CheckConstraint("skor BETWEEN 0 AND 100", name="card_skor"),
    CheckConstraint(_dalam("status", STATUS), name="card_status"),
    CheckConstraint(_dalam("prioritas", PRIORITAS), name="card_prioritas"),
    CheckConstraint(_dalam("prioritas_sumber", ("band", "analis")), name="card_prioritas_sumber"),
    CheckConstraint(f"kesimpulan IS NULL OR {_dalam('kesimpulan', KESIMPULAN)}", name="card_kesimpulan"),
    CheckConstraint(_dalam("status_verifikasi", STATUS_VERIFIKASI), name="card_status_verifikasi"),
    CheckConstraint("versi_terbaru >= 1", name="card_versi"),
)
Index("card_filter", card.c.status, card.c.prioritas, card.c.band)
Index("card_antrean", card.c.dibuat_pada.desc(), card.c.card_id.desc())

card_version = Table(
    "card_version",
    metadata,
    Column("card_id", Text, ForeignKey("card.card_id"), nullable=False),
    Column("versi", Integer, nullable=False),
    Column("dokumen", JSON, nullable=False),
    Column("dokumen_sha256", Text, nullable=False),
    Column("status_verifikasi", Text, nullable=False),
    Column("diterima_pada", DateTime(timezone=True), nullable=False),
    Column("event_id", Text, nullable=False),
    PrimaryKeyConstraint("card_id", "versi"),
)

ticket_event = Table(
    "ticket_event",
    metadata,
    Column("event_id", BigInteger, Identity(always=True), primary_key=True),
    Column("card_id", Text, ForeignKey("card.card_id"), nullable=False),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("pelaku", Text, nullable=False),
    Column("jenis", Text, nullable=False),
    Column("detail", JSON, nullable=False, server_default=text("'{}'")),
    Column("idempotency_key", Text, nullable=True),
    CheckConstraint(_dalam("jenis", JENIS_AKTIVITAS), name="ticket_event_jenis"),
)
Index("ticket_event_kartu_ts", ticket_event.c.card_id, ticket_event.c.ts)
Index(
    "ticket_event_idem",
    ticket_event.c.card_id,
    ticket_event.c.idempotency_key,
    unique=True,
    postgresql_where=ticket_event.c.idempotency_key.is_not(None),
)

feedback = Table(
    "feedback",
    metadata,
    Column("feedback_id", BigInteger, Identity(always=True), primary_key=True),
    Column("card_id", Text, ForeignKey("card.card_id"), nullable=False),
    Column("analis", Text, nullable=False),
    Column("label", Text, nullable=False),
    Column("catatan", Text, nullable=False, server_default=""),
    Column("dibuat_pada", DateTime(timezone=True), nullable=False),
    Column("idempotency_key", Text, nullable=True),
    CheckConstraint(_dalam("label", LABEL_UMPAN_BALIK), name="feedback_label"),
)
Index("feedback_kartu", feedback.c.card_id)
Index(
    "feedback_idem",
    feedback.c.card_id,
    feedback.c.idempotency_key,
    unique=True,
    postgresql_where=feedback.c.idempotency_key.is_not(None),
)
