"""Network-free tests for the quantum logic (verified locally via qiskit's
state-vector, so CI doesn't need a qBraid account). The end-to-end execution
path is exercised against the live free simulator by hand / the demo.
"""

import sys

import numpy as np
import pytest

from kannaka_quantum import core


def test_optimal_iterations_matches_grover_geometry():
    # Uniform start over 4 states: target amplitude 0.5 (θ=30°) → 1 iteration.
    assert core._optimal_iterations(0.5) == 1
    # Already dominant → don't rotate (would overshoot π/2 and de-amplify).
    assert core._optimal_iterations(0.95) == 0
    assert core._optimal_iterations(1.0) == 0
    assert core._optimal_iterations(0.0) == 0
    # Uniform over 16 (amp 0.25, θ≈14.5°): (π/2−θ)/(2θ) ≈ 2.6 → 3, matching
    # the textbook (π/4)√16 ≈ 3.14.
    assert core._optimal_iterations(0.25) == 3


@pytest.mark.parametrize("target", [0, 1, 2, 3])
def test_amplitude_amplification_marks_target_local(target):
    """Build prep(uniform) + one amplitude-amplification step toward ``target``
    and confirm (on a local state-vector) that the target basis state is the
    most probable — verifies _phase_flip's bit order and the diffuser."""
    from qiskit import QuantumCircuit
    from qiskit.circuit.library import StatePreparation
    from qiskit.quantum_info import Statevector

    n, dim = 2, 4
    vec = np.ones(dim) / np.sqrt(dim)
    prep = StatePreparation(vec)
    qc = QuantumCircuit(n)
    qc.append(prep, range(n))
    target_bits = format(target, f"0{n}b")[::-1]
    core._phase_flip(qc, target_bits)
    qc.append(prep.inverse(), range(n))
    core._phase_flip(qc, "0" * n)
    qc.append(prep, range(n))

    probs = Statevector(qc).probabilities()  # qiskit little-endian index
    assert int(np.argmax(probs)) == target
    assert probs[target] > 0.9  # one Grover step on a uniform 4-state ≈ certainty


def test_counts_parser_handles_shapes():
    class _D:
        def get_counts(self):
            return {"00": 5, "11": 7}

    class _R:
        data = _D()

    assert core._counts_from_result(_R()) == {"00": 5, "11": 7}
    assert core._counts_from_result(object()) == {}


def test_measured_index_device_aware():
    # qBraid-native backends report big-endian → reverse to the qiskit index.
    assert core._measured_index("10", "qbraid:qbraid:sim:qir-sv") == 1
    assert core._measured_index("01", "qbraid:qbraid:sim:qir-sv") == 2
    # AWS-routed devices (Rigetti via Braket) report the opposite order — no
    # reversal. Verified against a live aws:rigetti recall (2026-06-29): raw '01'
    # was the amplified target 'signal' (index 1), not the bit-reversed index 2.
    assert core._measured_index("10", "aws:rigetti:qpu:cepheus-1-108q") == 2
    assert core._measured_index("01", "aws:rigetti:qpu:cepheus-1-108q") == 1


# --- bitstring decode: device-aware endianness ------------------------------ #
# Commit #3 fixed silent decode corruption ("AWS QPUs report opposite qubit
# order"). These lock in BOTH endianness classes across widths/devices so a
# regression that drops the qBraid reversal (or applies it everywhere) can't
# ship again — it would flip the recalled memory index without any error.

# device.startswith("qbraid:") → big-endian, reverse before int(); else parse
# the raw string. Expected values are computed by hand.
_QBRAID_REVERSE_DEVICES = [
    "qbraid:qbraid:sim:qir-sv",
    "qbraid:qbraid:sim:qir-dm",
]
_RAW_DEVICES = [
    "aws:rigetti:qpu:cepheus-1-108q",
    "aws:ionq:qpu:forte-1",
    "openquantum:iqm:garnet",
    "openquantum:ionq:forte-1",
]


@pytest.mark.parametrize("device", _QBRAID_REVERSE_DEVICES)
@pytest.mark.parametrize(
    "bits,expected",
    [("001", 4), ("110", 3), ("01", 2), ("10", 1), ("0001", 8), ("1000", 1)],
)
def test_measured_index_qbraid_reverses(device, bits, expected):
    assert core._measured_index(bits, device) == expected


@pytest.mark.parametrize("device", _RAW_DEVICES)
@pytest.mark.parametrize(
    "bits,expected",
    [("001", 1), ("110", 6), ("01", 1), ("10", 2), ("0001", 1), ("1000", 8)],
)
def test_measured_index_non_qbraid_no_reverse(device, bits, expected):
    assert core._measured_index(bits, device) == expected


def test_endianness_classes_disagree_on_asymmetric_string():
    # The reversal is load-bearing: on an asymmetric bitstring the two classes
    # MUST decode to different indices. If someone deletes the qBraid reversal
    # both branches collapse to the raw value and this fails.
    q = core._measured_index("001", "qbraid:qbraid:sim:qir-sv")
    aws = core._measured_index("001", "aws:rigetti:qpu:cepheus-1-108q")
    assert (q, aws) == (4, 1)
    assert q != aws


# --- spend guards: credit-spending ops refuse without an explicit opt-in ---- #
# These are the money paths. Every assertion is offline (no provider client is
# constructed) — the guards raise before any network call.


def test_oq_estimate_cost_refuses_without_opt_in(monkeypatch):
    monkeypatch.delenv("KANNAKA_QUANTUM_ALLOW_SPEND", raising=False)
    with pytest.raises(RuntimeError, match="allow_spend"):
        core._oq_estimate_cost("openquantum:ionq:forte-1", 100, None, False)


def test_oq_estimate_cost_env_opt_in_returns_estimate(monkeypatch):
    monkeypatch.setenv("KANNAKA_QUANTUM_ALLOW_SPEND", "1")
    est = core._oq_estimate_cost("openquantum:iqm:garnet", 10, 1.0, False)
    assert est["per_shot_usd"] == core.OQ_USD_PER_SHOT["iqm:garnet"]
    assert est["est_credits"] < 1.0  # well under the cap


def test_oq_estimate_cost_refuses_over_cap(monkeypatch):
    monkeypatch.delenv("KANNAKA_QUANTUM_ALLOW_SPEND", raising=False)
    # 1024 shots on IonQ ≈ $49 ≈ 24.6 credits — far over the 1-credit cap.
    with pytest.raises(RuntimeError, match="exceeds"):
        core._oq_estimate_cost("openquantum:ionq:forte-1", 1024, 1.0, True)


def test_oq_estimate_cost_unknown_device_needs_max_credits(monkeypatch):
    monkeypatch.delenv("KANNAKA_QUANTUM_ALLOW_SPEND", raising=False)
    with pytest.raises(RuntimeError, match="unknown per-shot price"):
        core._oq_estimate_cost("openquantum:mystery:qpu", 100, None, True)
    # Explicitly acknowledged with a cap → allowed (returns None, no estimate).
    assert core._oq_estimate_cost("openquantum:mystery:qpu", 100, 5.0, True) is None


def test_qbraid_spend_guard_refuses_without_opt_in(monkeypatch):
    monkeypatch.delenv("KANNAKA_QUANTUM_ALLOW_SPEND", raising=False)
    with pytest.raises(RuntimeError, match="allow_spend"):
        core._qbraid_spend_guard(
            {"perShot": 0.5}, "aws:ionq:qpu:forte-1", 100, allow_spend=False, max_credits=None
        )


def test_qbraid_spend_guard_refuses_per_minute_billing(monkeypatch):
    monkeypatch.setenv("KANNAKA_QUANTUM_ALLOW_SPEND", "1")
    # Per-minute devices are refused outright — cost can't be bounded by shots.
    with pytest.raises(RuntimeError, match="per-minute"):
        core._qbraid_spend_guard(
            {"perMinute": 12000}, "qbraid:rigetti:qpu:ankaa", 100, allow_spend=True, max_credits=None
        )


def test_qbraid_spend_guard_allows_with_opt_in(monkeypatch):
    monkeypatch.delenv("KANNAKA_QUANTUM_ALLOW_SPEND", raising=False)
    est = core._qbraid_spend_guard(
        {"perTask": 1.0, "perShot": 0.1}, "aws:ionq:qpu:forte-1", 10, allow_spend=True, max_credits=200.0
    )
    assert est["est_credits"] == 2.0  # 1 + 0.1*10


# --- an unpriced device is unknown, never free ------------------------------ #


class _Quote:
    """Stands in for qbraid_core's estimate_cost result."""

    def __init__(self, available, cost=None, reason=""):
        self.pricingAvailable = available
        self.estimatedCost = cost
        self.reason = reason


def test_qbraid_spend_guard_refuses_a_device_it_cannot_price(monkeypatch):
    # The bug: dev.metadata()["pricing"] is empty for some real QPUs (the direct
    # Rigetti path among them). per_task and per_shot are then 0.0, so
    # est_credits = 0 + 0*shots = 0.0, which is under every ceiling — a real QPU
    # waved through with a confident estimate of "free".
    monkeypatch.setattr(core, "_qbraid_quote", lambda device, shots: None)
    with pytest.raises(RuntimeError, match="cannot price"):
        core._qbraid_spend_guard(
            {}, "qbraid:rigetti:qpu:cepheus-1-108q", 1000, allow_spend=True, max_credits=None
        )


def test_qbraid_spend_guard_unpriced_device_never_reports_zero(monkeypatch):
    # Guards the specific wrong ANSWER, not just the raise: if someone reinstates
    # the arithmetic, this catches an estimate of 0.0 slipping through a cap.
    monkeypatch.setattr(core, "_qbraid_quote", lambda device, shots: None)
    est = core._qbraid_spend_guard(
        {}, "qbraid:rigetti:qpu:cepheus-1-108q", 1000, allow_spend=True, max_credits=50.0
    )
    assert est["est_credits"] is None, "an unknown price must not be reported as a number"
    assert est["est_credits"] != 0.0
    assert est["cap_credits"] == 50.0
    assert est["source"] == "operator-accepted"


def test_qbraid_spend_guard_uses_the_server_quote_when_metadata_has_none(monkeypatch):
    monkeypatch.setattr(core, "_qbraid_quote", lambda device, shots: {"est_credits": 12.0})
    est = core._qbraid_spend_guard(
        {}, "rigetti:rigetti:qpu:cepheus-1-108q", 1000, allow_spend=True, max_credits=50.0
    )
    assert est["source"] == "qbraid.estimate_cost"
    assert est["est_credits"] == 12.0
    # Said out loud, because qBraid does not enforce this number.
    assert "not a guarantee" in est["note"]


def test_qbraid_spend_guard_server_quote_still_respects_the_cap(monkeypatch):
    # A quote is an estimate, not permission to spend.
    monkeypatch.setattr(core, "_qbraid_quote", lambda device, shots: {"est_credits": 900.0})
    with pytest.raises(RuntimeError, match="over the .* cap"):
        core._qbraid_spend_guard(
            {}, "rigetti:rigetti:qpu:cepheus-1-108q", 1000, allow_spend=True, max_credits=50.0
        )


def test_qbraid_spend_guard_surfaces_why_a_device_could_not_be_quoted(monkeypatch):
    monkeypatch.setattr(
        core, "_qbraid_quote", lambda device, shots: {"unavailable_reason": "device is offline"}
    )
    with pytest.raises(RuntimeError, match="device is offline"):
        core._qbraid_spend_guard(
            {}, "rigetti:rigetti:qpu:cepheus-1-108q", 1000, allow_spend=True, max_credits=None
        )


def test_qbraid_quote_branches_on_pricing_available(monkeypatch):
    # qBraid returns pricingAvailable=False with a reason rather than raising, so
    # a bridge that ignores the flag reads whatever estimatedCost happens to hold
    # as a successful quote. Here that would be 0.0 — free, again.
    import types

    mod = types.ModuleType("qbraid_core.services.runtime")
    captured = {}

    class Client:
        def estimate_cost(self, device, shots=None):
            captured["device"] = device
            captured["shots"] = shots
            return _Quote(False, cost=0.0, reason="no pricing for this device")

    mod.QuantumRuntimeClient = Client
    monkeypatch.setitem(sys.modules, "qbraid_core.services.runtime", mod)

    got = core._qbraid_quote("rigetti:rigetti:qpu:cepheus-1-108q", 250)
    assert got == {"unavailable_reason": "no pricing for this device"}
    assert "est_credits" not in got
    # shots is passed explicitly: omitting it quotes 1000 rather than quoting
    # shot-independently, which would silently misprice every other shot count.
    assert captured["shots"] == 250


def test_qbraid_quote_returns_none_when_the_package_is_absent(monkeypatch):
    # Old or missing qbraid-core must read as "unknown", so the caller refuses.
    monkeypatch.setitem(sys.modules, "qbraid_core.services.runtime", None)
    assert core._qbraid_quote("rigetti:rigetti:qpu:cepheus-1-108q", 100) is None


# --- free-simulator default holds (no spend guard, no opt-in required) ------ #


class _FakeCounts:
    def get_counts(self):
        return {"0": 100}


class _FakeResult:
    data = _FakeCounts()


class _FakeJob:
    id = "fake-job"

    def wait_for_final_state(self, timeout=None):
        return None

    def result(self):
        return _FakeResult()


class _FakeDevice:
    def __init__(self, pricing=None):
        self._pricing = pricing or {}
        self.ran = False

    def metadata(self):
        return {"pricing": self._pricing}

    def run(self, qasm3, shots=100):
        self.ran = True
        return _FakeJob()


class _FakeProvider:
    def __init__(self, device):
        self._device = device

    def get_device(self, device):
        return self._device


def test_run_qasm_free_simulator_default_needs_no_opt_in(monkeypatch):
    # DEFAULT_DEVICE is the free qBraid simulator ("sim" in its id) → the spend
    # guard is never consulted and no allow_spend is needed.
    monkeypatch.delenv("KANNAKA_QUANTUM_ALLOW_SPEND", raising=False)
    assert "sim" in core.DEFAULT_DEVICE.lower()
    dev = _FakeDevice()
    monkeypatch.setattr(core, "_provider", lambda: _FakeProvider(dev))
    out = core.run_qasm("OPENQASM 3;", device=core.DEFAULT_DEVICE, shots=100)
    assert out["counts"] == {"0": 100}
    assert "cost_estimate" not in out  # free path — no spend estimate
    assert dev.ran


def test_run_qasm_real_qbraid_qpu_refuses_without_opt_in(monkeypatch):
    # A real (non-"sim") qBraid QPU must refuse before running the circuit.
    monkeypatch.delenv("KANNAKA_QUANTUM_ALLOW_SPEND", raising=False)
    dev = _FakeDevice(pricing={"perShot": 0.5})
    monkeypatch.setattr(core, "_provider", lambda: _FakeProvider(dev))
    with pytest.raises(RuntimeError, match="allow_spend"):
        core.run_qasm("OPENQASM 3;", device="aws:ionq:qpu:forte-1", shots=100)
    assert not dev.ran  # refused before any job was submitted


def test_qbraid_spend_guard_per_minute_allowed_with_max_seconds(monkeypatch):
    monkeypatch.delenv("QBRAID_MAX_CREDITS", raising=False)
    est = core._qbraid_spend_guard(
        {"perMinute": 12000}, "rigetti:rigetti:qpu:cepheus-1-108q", 100, allow_spend=True, max_credits=500, max_seconds=2
    )
    assert est["billing"] == "per-minute"
    assert abs(est["ceiling_credits"] - 400.0) < 1e-9  # 12000/min * 2 s


def test_qbraid_spend_guard_per_minute_ceiling_over_cap():
    with pytest.raises(RuntimeError, match="exceeds"):
        core._qbraid_spend_guard({"perMinute": 12000}, "d", 100, allow_spend=True, max_credits=100, max_seconds=2)


def test_qbraid_spend_guard_per_minute_zero_seconds_refused():
    with pytest.raises(RuntimeError, match="per-minute"):
        core._qbraid_spend_guard({"perMinute": 12000}, "d", 100, allow_spend=True, max_credits=1000, max_seconds=0)


class _FailedJob:
    id = "job-failed"

    def wait_for_final_state(self, timeout=None):
        return None

    def status(self):
        return "JobStatus.FAILED"

    def metadata(self):
        return {"statusText": "Delay is not supported"}

    def result(self):  # pragma: no cover - must never be reached
        raise AssertionError("result() must not be called for a FAILED job")


class _EmptyJob(_FailedJob):
    id = "job-empty"

    def status(self):
        return "JobStatus.COMPLETED"

    def metadata(self):
        return {}

    def result(self):
        class _R:
            def measurement_counts(self):
                return {}

            class data:  # mimics qbraid's Result.data
                @staticmethod
                def get_counts():
                    return {}

        return _R()


def _provider_with(job):
    class _Dev:
        def metadata(self):
            return {}

        def run(self, qasm3, shots=100):
            return job

    class _Prov:
        def get_device(self, device):
            return _Dev()

    return lambda: _Prov()


def test_run_qasm_failed_job_raises_not_empty_success(monkeypatch):
    monkeypatch.setattr(core, "_provider", _provider_with(_FailedJob()))
    with pytest.raises(RuntimeError, match="FAILED.*Delay is not supported"):
        core.run_qasm("OPENQASM 3.0;", device="qbraid:qbraid:sim:qir-sv", shots=10)


def test_run_qasm_empty_counts_raise(monkeypatch):
    monkeypatch.setattr(core, "_provider", _provider_with(_EmptyJob()))
    with pytest.raises(RuntimeError, match="no counts"):
        core.run_qasm("OPENQASM 3.0;", device="qbraid:qbraid:sim:qir-sv", shots=10)


class _BilledJob(_EmptyJob):
    id = "job-billed"

    def metadata(self):
        import datetime as _dt
        return {"cost": 7.3, "timeStamps": {"executed": _dt.datetime(2026, 9, 7, 1, 2, 3, tzinfo=_dt.timezone.utc)}}

    def result(self):
        class _R:
            def measurement_counts(self):
                return {"1": 90, "0": 10}

        return _R()


def test_run_qasm_billed_metadata_is_json_safe(monkeypatch):
    import json
    monkeypatch.setattr(core, "_provider", _provider_with(_BilledJob()))
    out = core.run_qasm("OPENQASM 3.0;", device="qbraid:qbraid:sim:qir-sv", shots=100)
    assert out["counts"] == {"1": 90, "0": 10}
    assert out["billed"]["cost"] == 7.3
    json.dumps(out)  # a datetime in timeStamps must not be able to kill the CLI output
