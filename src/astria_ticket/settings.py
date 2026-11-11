"""Konfigurasi ticket dari env `ASTRIA_*`. Tambahkan field milik layanan ini di sini."""

from astria_common.settings import AstriaSettings
from pydantic import Field

from astria_ticket.sla import MENIT_BAWAAN


class Settings(AstriaSettings):
    service_name: str = "ticket"

    # SLA tanggapan awal per band, menit (K4; mode 24_jam). Bawaan dari sla.MENIT_BAWAAN (angka contoh).
    ticket_sla_menit_kritis: int = Field(default=MENIT_BAWAAN["KRITIS"], ge=0)
    ticket_sla_menit_tinggi: int = Field(default=MENIT_BAWAAN["TINGGI"], ge=0)
    ticket_sla_menit_sedang: int = Field(default=MENIT_BAWAAN["SEDANG"], ge=0)
    ticket_sla_menit_rendah: int = Field(default=MENIT_BAWAAN["RENDAH"], ge=0)

    ticket_callers: frozenset[str] = frozenset({"svc:api", "svc:dfir", "svc:scheduler"})
    """Layanan yang boleh memanggil `/svc/v1` (K8). Env JSON, mis. ASTRIA_TICKET_CALLERS='["svc:api"]'."""

    ticket_comment_max_chars: int = Field(default=4000, ge=1, le=20000)
    """Batas panjang komentar, alasan, dan catatan (rancangan 5.7)."""

    def sla_menit_per_band(self) -> dict[str, int]:
        return {
            "KRITIS": self.ticket_sla_menit_kritis,
            "TINGGI": self.ticket_sla_menit_tinggi,
            "SEDANG": self.ticket_sla_menit_sedang,
            "RENDAH": self.ticket_sla_menit_rendah,
        }
