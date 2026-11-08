"""Jalankan layanan: `astria-ticket` atau `python -m astria_ticket`."""

from astria_common.app import run

from astria_ticket.main import build_app
from astria_ticket.settings import Settings


def main() -> None:
    settings = Settings()
    run(build_app(settings), settings)


if __name__ == "__main__":
    main()
