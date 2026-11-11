"""Rakit aplikasi ticket: komponen standar (PostgreSQL, NATS, relay outbox), konsumen `card.verified`, dan router.

Dev memakai `uvicorn astria_ticket.main:build_app --factory --reload`; image memakai `astria-ticket`.
"""

from astria_common.app import create_app
from astria_common.auth import ServiceTokenVerifier
from astria_common.consumer import Consumer
from astria_common.service import standard_runtime
from fastapi import FastAPI

from astria_ticket import api, store
from astria_ticket.konsumen import PenerimaKartu
from astria_ticket.settings import Settings


def build_app(settings: Settings | None = None, *, token_verifier: ServiceTokenVerifier | None = None) -> FastAPI:
    settings = settings or Settings()  # pyright: ignore[reportCallIssue]  # field wajib diisi dari env ASTRIA_*
    runtime = standard_runtime(settings, metadata=[store.metadata])
    if runtime.db is not None and runtime.bus is not None:
        penerima = PenerimaKartu(menit_sla=settings.sla_menit_per_band())
        runtime.add(
            Consumer(
                runtime.bus,
                runtime.db,
                durable="ticket",
                event_type="card.verified",
                handler=penerima.on_card_verified,
            )
        )
    return create_app(settings, components=runtime.components, routers=[api.router], token_verifier=token_verifier)
