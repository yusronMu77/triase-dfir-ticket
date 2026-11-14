# Changelog

## [Unreleased]

### Added

- Tiket dari kartu terverifikasi (ticket#2): konsumen `card.verified` menyimpan tiket dan semua versi kartu (`card`, `card_version`), prioritas dari band, tenggat SLA tanggapan awal per band, versi baru membuka ulang tiket `selesai`, konflik versi dicatat ke audit.
- API `/svc/v1` untuk `api`, `dfir`, dan `scheduler`: antrean berfilter dengan paginasi cursor, detail, versi, `PATCH` dengan `if_row_version` (409 `VERSI_BENTROK`), komentar dan label, umpan balik, statistik. Peran dari klaim `act`; pimpinan hanya membaca.
- Aturan status dan transisi, `ticket_event`, audit `ticket.*` lewat outbox, `Idempotency-Key` untuk aktivitas dan umpan balik.
- Uji domain (hypothesis), konsumen, dan API (termasuk uji DB PostgreSQL dan PATCH bersamaan), pyright di CI.

### Changed

- `astria-common` v0.3.1 (naik dari v0.2.2) dan `astria-contracts` v0.4.0 (naik dari v0.3.0; common v0.3.1 mengunci v0.4.0).
- `KartuTidakValid` turunan `astria_common.consumer.PermanentError` (common#9): kartu dengan isi tidak valid (band, skor, judul, pola, correlation) langsung masuk DLQ `astria.dlq.<konsumen>`, tidak lagi dicoba ulang sampai `max_deliver` (sekitar 2,5 menit). Galat database tetap sementara dan dicoba ulang.

### Added (Tahap 0)

- Kerangka layanan Tahap 0 dari templat `common` v0.1.0: `/health`, `/ready`, `/metrics`, token layanan, Dockerfile non-root, CI (lint, uji, gitleaks, build image, smoke `/ready`).
- Logo Astria di kepala README (`.github/assets/`, dari kit logo di repo `triase-dfir-uiux`).
