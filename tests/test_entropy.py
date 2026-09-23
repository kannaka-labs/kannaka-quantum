"""Tests for the entropy reservoir (#11) and HMAC-DRBG expansion (#12).

All offline: the real-QPU harvest path is either mocked or exercised only up to
the $0 pre-submit spend guard (no network, no credits).
"""

import json

import pytest

from kannaka_quantum import entropy


@pytest.fixture(autouse=True)
def _isolated_reservoir(tmp_path, monkeypatch):
    # Every test gets a fresh, throwaway reservoir under a temp data dir.
    monkeypatch.setenv("KANNAKA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("KANNAKA_QUANTUM_ALLOW_SPEND", raising=False)
    return tmp_path


def _fake_qrng(bit_pattern="10110001"):
    """A core.qrng stand-in that returns a deterministic bitstring of n_bits."""
    def qrng(n_bits, device="d", allow_spend=False, max_credits=None, subcategory=None):
        bits = (bit_pattern * ((n_bits // len(bit_pattern)) + 1))[:n_bits]
        return {"bits": bits, "n_bits": n_bits, "int": int(bits, 2), "device": device, "job_id": "job-abc"}
    return qrng


def _seed_reservoir(data: bytes, harvests):
    """Directly write reservoir.bin + meta.jsonl (bypasses the QPU) for draw tests."""
    entropy._reservoir_path().write_bytes(data)
    with open(entropy._meta_path(), "w", encoding="utf-8") as f:
        f.writelines(json.dumps(h) + "\n" for h in harvests)


# ── T1.1: harvest ──────────────────────────────────────────────────────────


def _fake_chsh(s_value: float = 2.4, calls: list | None = None):
    """A bell.chsh stand-in: a certificate with Bell parameter ``s_value``."""

    def run(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        return {"S": s_value, "abs_S": abs(s_value), "violates_classical": abs(s_value) > 2.0,
                "job_ids": {"a0b0": "bell-1"}, "device": kwargs.get("device"), "shots": kwargs.get("shots")}

    return run


def test_harvest_appends_bits_and_meta(monkeypatch):
    monkeypatch.setattr(entropy.core, "qrng", _fake_qrng())
    out = entropy.harvest(64, device="openquantum:rigetti:cepheus-1-108q", allow_spend=True, chsh_fn=_fake_chsh())
    assert out["harvested"] is True
    assert out["bytes_added"] == 8  # 64 bits → 8 bytes
    assert out["job_id"] == "job-abc"
    # reservoir.bin grew and a meta line was appended.
    assert entropy._reservoir_total_bytes() == 8
    meta = entropy._read_meta()
    assert len(meta) == 1
    assert meta[0]["device"] == "openquantum:rigetti:cepheus-1-108q"
    assert meta[0]["job_id"] == "job-abc"
    assert meta[0]["n_bits"] == 64
    assert meta[0]["timestamp"]
    assert meta[0]["cost_usd"] is not None  # priced device
    # The Bell certificate is in the provenance line and in the result.
    assert meta[0]["bell"]["S"] == 2.4 and meta[0]["bell"]["violates_classical"] is True
    assert out["bell"]["job_ids"] == {"a0b0": "bell-1"}


def test_harvest_certificate_runs_first_on_the_same_device_and_a_classical_day_is_refused(monkeypatch):
    order = []
    calls: list = []

    def qrng_spy(n_bits, device="d", allow_spend=False, max_credits=None, subcategory=None):
        order.append("qrng")
        bits = "1" * n_bits
        return {"bits": bits, "n_bits": n_bits, "int": int(bits, 2), "device": device, "job_id": "j"}

    monkeypatch.setattr(entropy.core, "qrng", qrng_spy)
    # S = 1.9: the device did not beat the classical bound. Refused, nothing written, qrng never ran.
    with pytest.raises(RuntimeError, match="Bell certificate failed"):
        entropy.harvest(16, device="openquantum:iqm:garnet", allow_spend=True, max_credits=5.0,
                        chsh_fn=_fake_chsh(1.9, calls))
    assert order == [] and entropy._reservoir_total_bytes() == 0 and entropy._read_meta() == []
    assert calls[0]["device"] == "openquantum:iqm:garnet" and calls[0]["allow_spend"] is True
    assert calls[0]["max_credits"] == 5.0 and calls[0]["shots"] == entropy.DEFAULT_CERTIFY_SHOTS
    # A violating certificate: it ran before the harvest, and the harvest happened.
    entropy.harvest(16, device="openquantum:iqm:garnet", allow_spend=True, chsh_fn=_fake_chsh(2.3, calls))
    assert order == ["qrng"] and entropy._reservoir_total_bytes() == 2


def test_harvest_without_certificate_records_null_so_a_reader_can_tell(monkeypatch):
    monkeypatch.setattr(entropy.core, "qrng", _fake_qrng())
    out = entropy.harvest(64, device="openquantum:rigetti:cepheus-1-108q", allow_spend=True, certify=False)
    assert out["bell"] is None
    assert entropy._read_meta()[0]["bell"] is None


def test_harvest_refuses_simulator(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(entropy.core, "qrng", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    with pytest.raises(RuntimeError, match="simulator"):
        entropy.harvest(64, device="qbraid:qbraid:sim:qir-sv", allow_spend=True)
    assert called["n"] == 0  # refused before any qrng call
    assert entropy._reservoir_total_bytes() == 0


def test_harvest_forwards_spend_guard_args(monkeypatch):
    seen = {}

    def spy(n_bits, device="d", allow_spend=False, max_credits=None, subcategory=None):
        seen.update(allow_spend=allow_spend, max_credits=max_credits)
        bits = "1" * n_bits
        return {"bits": bits, "n_bits": n_bits, "int": int(bits, 2), "device": device, "job_id": "j"}

    monkeypatch.setattr(entropy.core, "qrng", spy)
    entropy.harvest(16, device="openquantum:iqm:garnet", allow_spend=True, max_credits=3.0, chsh_fn=_fake_chsh())
    assert seen == {"allow_spend": True, "max_credits": 3.0}


def test_harvest_real_device_refuses_without_opt_in():
    # $0 end-to-end through the REAL core path: the OpenQuantum spend guard raises
    # pre-submit (no network), so no reservoir write happens.
    with pytest.raises(RuntimeError, match="allow_spend"):
        entropy.harvest(64, device="openquantum:rigetti:cepheus-1-108q", allow_spend=False)
    assert entropy._reservoir_total_bytes() == 0


def test_harvest_real_device_over_cap_raises():
    with pytest.raises(RuntimeError, match="exceeds"):
        entropy.harvest(2048, device="openquantum:rigetti:cepheus-1-108q", allow_spend=True, max_credits=0.0001)
    assert entropy._reservoir_total_bytes() == 0


# ── T1.1: qrng status ──────────────────────────────────────────────────────


def test_status_empty_reservoir():
    st = entropy.status()
    assert st["available_bytes"] == 0
    assert st["harvest_count"] == 0
    assert st["last_harvest"] is None
    assert st["estimated_refill"]["cost_usd"] is not None


def test_status_after_harvest(monkeypatch):
    monkeypatch.setattr(entropy.core, "qrng", _fake_qrng())
    entropy.harvest(128, device="openquantum:iqm:garnet", allow_spend=True, chsh_fn=_fake_chsh())
    st = entropy.status()
    assert st["available_bytes"] == 16
    assert st["available_bits"] == 128
    assert st["harvest_count"] == 1
    assert st["last_harvest"]["device"] == "openquantum:iqm:garnet"
    assert st["last_harvest"]["job_id"] == "job-abc"


# ── T1.2: HMAC-DRBG determinism (no hardware) ──────────────────────────────


def test_hmac_drbg_deterministic_under_fixed_seed():
    seed = b"quantum-seed-material-32-bytes!!"
    a = entropy.HmacDrbg(seed).generate(64)
    b = entropy.HmacDrbg(seed).generate(64)
    assert a == b  # reproducible
    assert len(a) == 64
    # Different seed → different stream.
    c = entropy.HmacDrbg(seed + b"x").generate(64)
    assert c != a
    # Stream advances: consecutive blocks from one instance differ.
    d = entropy.HmacDrbg(seed)
    assert d.generate(32) != d.generate(32)


def test_hmac_drbg_rejects_empty_seed():
    with pytest.raises(ValueError):
        entropy.HmacDrbg(b"")


# ── T1.2: draw provenance + empty-reservoir loud failure ───────────────────


def test_draw_empty_reservoir_raises_no_prng_fallback():
    with pytest.raises(RuntimeError, match="empty"):
        entropy.draw(64, expand=True)
    with pytest.raises(RuntimeError, match="empty"):
        entropy.draw(64, expand=False)


def test_draw_raw_has_provenance_and_consumes_bytes():
    _seed_reservoir(
        b"\xaa" * 32,
        [{"device": "openquantum:iqm:garnet", "job_id": "J1", "n_bits": 256, "bytes": 32, "timestamp": "t1"}],
    )
    out = entropy.draw(64, expand=False)  # 64 bits = 8 bytes
    assert out["mode"] == "raw"
    assert len(out["bits"]) == 64
    assert out["provenance"] == [{"device": "openquantum:iqm:garnet", "job_id": "J1", "harvested_at": "t1"}]
    # Consumed exactly 8 bytes.
    assert entropy._available_bytes() == 24


def test_draw_expand_has_provenance_and_seeds_drbg():
    _seed_reservoir(
        b"\x01" * 48,
        [{"device": "openquantum:rigetti:cepheus-1-108q", "job_id": "J2", "n_bits": 384, "bytes": 48, "timestamp": "t2"}],
    )
    out = entropy.draw(256, expand=True)
    assert out["mode"] == "drbg-expand"
    assert out["expanded"] is True
    assert len(out["bits"]) == 256
    assert out["provenance"][0]["job_id"] == "J2"
    # Expansion consumes only seed material (48 bytes here), not 256 bits.
    assert entropy._available_bytes() == 0


def test_draw_expand_reseeds_and_chains_multiple_harvests():
    # Two harvests → the reservoir spans two job_ids. A large expand draw crosses
    # the reseed threshold and must pull fresh seed from the 2nd harvest.
    data = b"\x02" * 60 + b"\x03" * 60
    _seed_reservoir(
        data,
        [
            {"device": "openquantum:iqm:garnet", "job_id": "H1", "n_bits": 480, "bytes": 60, "timestamp": "t1"},
            {"device": "openquantum:iqm:emerald", "job_id": "H2", "n_bits": 480, "bytes": 60, "timestamp": "t2"},
        ],
    )
    # Output > RESEED_OUTPUT_BYTES so at least one reseed occurs.
    n_bits = (entropy.RESEED_OUTPUT_BYTES + 4096) * 8
    out = entropy.draw(n_bits, expand=True)
    assert out["reseeds"] >= 1
    job_ids = {p["job_id"] for p in out["provenance"]}
    assert job_ids == {"H1", "H2"}
