# Changelog

## [Unreleased]

### Added

- Tiket dari kartu terverifikasi (ticket#2): konsumen `card.verified` menyimpan tiket dan semua versi kartu (`card`, `card_version`), prioritas dari band, tenggat SLA tanggapan awal per band, versi baru membuka ulang tiket `selesai`, konflik versi dicatat ke audit.
- API `/svc/v1` untuk `api`, `dfir`, dan `scheduler`: antrean berfilter dengan paginasi cursor, detail, versi, `PATCH` dengan `if_row_version` (409 `VERSI_BENTROK`), komentar dan label, umpan balik, statistik. Peran dari klaim `act`; pimpinan hanya membaca.
- Aturan status dan transisi, `ticket_event`, audit `ticket.*` lewat outbox, `Idempotency-Key` untuk aktivitas dan umpan balik.
- Uji domain (hypothesis), konsumen, dan API (termasuk uji DB PostgreSQL dan PATCH bersamaan), pyright di CI.

### Changed

- `astria-common` v0.2.2 dan `astria-contracts` v0.3.0.

### Added (Tahap 0)

- Kerangka layanan Tahap 0 dari templat `common` v0.1.0: `/health`, `/ready`, `/metrics`, token layanan, Dockerfile non-root, CI (lint, uji, gitleaks, build image, smoke `/ready`).
- Logo Astria di kepala README (`.github/assets/`, dari kit logo di repo `triase-dfir-uiux`).
