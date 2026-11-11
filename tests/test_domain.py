"""Domain murni: mesin status, aturan versi, prioritas, SLA."""

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from astria_ticket import prioritas, sla, status, versi
from astria_ticket.status import TransisiDitolak

T0 = datetime(2026, 11, 11, 8, 0, tzinfo=UTC)

# ---------------------------------------------------------------- status

HARAPAN = {
    ("baru", "ditinjau"): {"analis", "dfir", "admin"},
    ("ditinjau", "diselidiki"): {"analis", "dfir", "admin"},
    ("ditinjau", "selesai"): {"analis", "dfir", "admin"},
    ("diselidiki", "eskalasi"): {"analis", "dfir", "admin"},
    ("diselidiki", "selesai"): {"analis", "dfir", "admin"},
    ("eskalasi", "diselidiki"): {"dfir", "admin"},
    ("eskalasi", "selesai"): {"dfir", "admin"},
}


@pytest.mark.parametrize("dari", status.STATUS)
@pytest.mark.parametrize("ke", status.STATUS)
@pytest.mark.parametrize("peran", status.PERAN)
def test_tabel_transisi_lengkap(dari: str, ke: str, peran: str) -> None:
    boleh = peran in HARAPAN.get((dari, ke), set())
    assert status.transisi_diizinkan(dari, ke, peran) is boleh


def test_pimpinan_tidak_pernah_boleh_menulis() -> None:
    assert not status.boleh_menulis("pimpinan")
    assert all(status.boleh_menulis(p) for p in ("analis", "dfir", "admin"))
    assert not status.boleh_menulis("tamu")
    assert status.boleh_menulis(["pimpinan", "analis"]) and not status.boleh_menulis([])
    assert status.boleh_membaca(["pimpinan"]) and not status.boleh_membaca(["tamu"])


def test_selesai_ke_ditinjau_hanya_sistem() -> None:
    for peran in status.PERAN:
        assert not status.transisi_diizinkan("selesai", "ditinjau", peran)
    assert status.status_setelah_versi_baru("selesai") == "ditinjau"
    for s in ("baru", "ditinjau", "diselidiki", "eskalasi"):
        assert status.status_setelah_versi_baru(s) == s


@pytest.mark.parametrize(
    ("dari", "ke", "peran", "kesimpulan", "kode"),
    [
        ("baru", "tutup", "analis", None, "TRANSISI_TIDAK_VALID"),
        ("baru", "selesai", "analis", "bukan_insiden", "TRANSISI_TIDAK_VALID"),
        ("baru", "baru", "analis", None, "TRANSISI_TIDAK_VALID"),
        ("eskalasi", "selesai", "analis", "insiden_benar", "PERAN_TIDAK_CUKUP"),
        ("baru", "ditinjau", "pimpinan", None, "PERAN_TIDAK_CUKUP"),
        ("ditinjau", "selesai", "analis", None, "KESIMPULAN_WAJIB"),
        ("ditinjau", "selesai", "analis", "jinak", "KESIMPULAN_TIDAK_VALID"),
    ],
)
def test_periksa_transisi_menolak(dari: str, ke: str, peran: str, kesimpulan: str | None, kode: str) -> None:
    with pytest.raises(TransisiDitolak) as e:
        status.periksa_transisi(dari, ke, peran, kesimpulan)
    assert e.value.kode == kode


def test_periksa_transisi_lolos() -> None:
    status.periksa_transisi("baru", "ditinjau", "analis")
    status.periksa_transisi("ditinjau", "selesai", "analis", "bukan_insiden")
    status.periksa_transisi("eskalasi", "selesai", "dfir", "insiden_benar")
    status.periksa_transisi("eskalasi", "diselidiki", ["pimpinan", "dfir"])
    with pytest.raises(TransisiDitolak):
        status.periksa_transisi("eskalasi", "diselidiki", ["pimpinan", "analis"])


@given(st.lists(st.tuples(st.sampled_from(status.STATUS), st.sampled_from(status.PERAN)), max_size=40))
def test_urutan_acak_tetap_di_lima_status(langkah: list[tuple[str, str]]) -> None:
    sekarang: str = status.STATUS_AWAL
    for ke, peran in langkah:
        try:
            status.periksa_transisi(sekarang, ke, peran, "insiden_benar")
        except TransisiDitolak:
            continue
        sekarang = ke
    assert sekarang in status.STATUS


# ---------------------------------------------------------------- versi


def test_sha256_kanonik_tidak_peduli_urutan_kunci() -> None:
    a = {"card_id": "IC-20261111-001", "version": 1, "kode": {"b": 1, "a": [1, "é"]}, "model": {}}
    b = {"model": {}, "kode": {"a": [1, "é"], "b": 1}, "version": 1, "card_id": "IC-20261111-001"}
    assert versi.sha256_kanonik(a) == versi.sha256_kanonik(b)
    assert versi.sha256_kanonik(a).startswith("sha256:")
    assert versi.sha256_kanonik(a) != versi.sha256_kanonik({**a, "version": 2})


def test_aturan_versi() -> None:
    saat = versi.Tersimpan(versi_terbaru=2, dokumen_sha256="sha256:aa")
    assert versi.terapkan_versi(None, 1, "sha256:x") == versi.Buat(versi=1)
    assert versi.terapkan_versi(saat, 1, "sha256:x") == versi.Abaikan(alasan="versi lebih lama dari yang tersimpan")
    assert versi.terapkan_versi(saat, 2, "sha256:aa") == versi.Abaikan(alasan="duplikat")
    assert versi.terapkan_versi(saat, 2, "sha256:bb") == versi.Konflik(versi=2)
    assert versi.terapkan_versi(saat, 3, "sha256:cc") == versi.Perbarui(versi=3)
    with pytest.raises(ValueError):
        versi.terapkan_versi(None, 0, "sha256:x")


@given(st.lists(st.tuples(st.integers(min_value=1, max_value=6), st.sampled_from(["a", "b"])), min_size=1))
def test_versi_monoton_dan_urutan_tidak_penting(masuk: list[tuple[int, str]]) -> None:
    """Versi tersimpan tidak pernah turun; versi akhir = versi tertinggi yang pernah datang."""
    saat: versi.Tersimpan | None = None
    for v, isi in masuk:
        sha = f"sha256:{v}{isi}"
        k = versi.terapkan_versi(saat, v, sha)
        if isinstance(k, versi.Buat | versi.Perbarui):
            assert saat is None or k.versi > saat.versi_terbaru
            saat = versi.Tersimpan(versi_terbaru=k.versi, dokumen_sha256=sha)
    assert saat is not None
    assert saat.versi_terbaru == max(v for v, _ in masuk)


@given(st.lists(st.integers(min_value=1, max_value=5), min_size=1))
def test_event_berulang_tidak_mengubah_keadaan(urut: list[int]) -> None:
    saat: versi.Tersimpan | None = None
    for v in urut + urut:
        k = versi.terapkan_versi(saat, v, f"sha256:{v}")
        assert not isinstance(k, versi.Konflik)
        if isinstance(k, versi.Buat | versi.Perbarui):
            saat = versi.Tersimpan(k.versi, f"sha256:{v}")
    assert saat == versi.Tersimpan(max(urut), f"sha256:{max(urut)}")


# ---------------------------------------------------------------- prioritas


def test_prioritas_dari_band() -> None:
    assert [prioritas.dari_band(b) for b in prioritas.BAND] == ["P1", "P2", "P3", "P4"]
    with pytest.raises(ValueError):
        prioritas.dari_band("kritis")


# ---------------------------------------------------------------- sla


def test_tenggat_bawaan() -> None:
    assert sla.tenggat(T0, "KRITIS") == T0 + timedelta(minutes=15)
    assert sla.tenggat(T0, "RENDAH") == T0 + timedelta(days=1)
    assert sla.tenggat(T0, "SEDANG", {"SEDANG": 30}) == T0 + timedelta(minutes=30)


def test_tenggat_menolak_input_salah() -> None:
    with pytest.raises(ValueError):
        sla.tenggat(T0.replace(tzinfo=None), "KRITIS")
    with pytest.raises(ValueError):
        sla.tenggat(T0, "SANGAT")
    with pytest.raises(ValueError):
        sla.tenggat(T0, "KRITIS", {"KRITIS": -1})


def test_terlambat() -> None:
    target = T0 + timedelta(minutes=15)
    assert not sla.terlambat(target, None, target)
    assert sla.terlambat(target, None, target + timedelta(seconds=1))
    assert not sla.terlambat(target, target - timedelta(minutes=1), target + timedelta(days=3))
    assert sla.terlambat(target, target + timedelta(minutes=1), target + timedelta(minutes=1))


@given(
    st.datetimes(min_value=datetime(2020, 1, 1), max_value=datetime(2035, 1, 1), timezones=st.just(UTC)),
    st.sampled_from(prioritas.BAND),
    st.integers(min_value=0, max_value=60 * 24 * 30),
)
def test_tenggat_tidak_sebelum_terbit(dibuat: datetime, band: str, menit: int) -> None:
    target = sla.tenggat(dibuat, band, dict.fromkeys(prioritas.BAND, menit))
    assert target >= dibuat
    assert not sla.terlambat(target, None, dibuat)


def test_settings_sla_dari_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from astria_ticket.settings import Settings

    monkeypatch.setenv("ASTRIA_TICKET_SLA_MENIT_KRITIS", "5")
    s = Settings(env="test")  # pyright: ignore[reportCallIssue]
    assert s.sla_menit_per_band() == {"KRITIS": 5, "TINGGI": 60, "SEDANG": 240, "RENDAH": 1440}
