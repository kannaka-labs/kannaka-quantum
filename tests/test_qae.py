"""qae — amplitude estimation of a slab transmission, at $0 on the local state-vector."""
import math

import numpy as np
import pytest

from kannaka_quantum import qae

P = qae.slab_transmission()
SEG = qae.segment_probabilities(P, 3)


def test_slab_is_exponential_attenuation():
    assert abs(qae.slab_transmission(117.1, 0.01) - math.exp(-1.171)) < 1e-12
    assert 0.30 < P < 0.32
    with pytest.raises(ValueError):
        qae.slab_transmission(-1.0, 0.01)


def test_segments_multiply_back_to_p():
    assert abs(math.prod(SEG) - P) < 1e-12
    with pytest.raises(ValueError):
        qae.segment_probabilities(1.0, 3)


def test_a_operator_prepares_p_on_all_ones():
    from qiskit.quantum_info import Statevector

    sv = Statevector.from_instruction(qae.a_operator(SEG))
    assert abs(abs(sv.data[-1]) ** 2 - P) < 1e-9


@pytest.mark.parametrize("k", [1, 2, 3])
def test_grover_rotates_by_2theta(k):
    from qiskit.quantum_info import Statevector

    theta = qae.theta_from_p(P)
    circ = qae.mlae_circuit(SEG, k).remove_final_measurements(inplace=False)
    good = abs(Statevector.from_instruction(circ).data[-1]) ** 2
    assert abs(good - math.sin((2 * k + 1) * theta) ** 2) < 1e-9


def test_grover_carries_brassard_sign():
    """A controlled Q needs the -1 of Q = -A S_0 A^dagger S_chi; without it the eigenphase shifts by 1/2."""
    from qiskit.quantum_info import Operator

    theta = qae.theta_from_p(P)
    eig = np.linalg.eigvals(Operator(qae.grover_operator(SEG)).data)
    phases = sorted({round(float(np.angle(e)) / (2 * math.pi) % 1.0, 4) for e in eig})
    want = round(theta / math.pi, 4)
    assert want in phases and round(1 - theta / math.pi, 4) in phases
    assert round(0.5 - theta / math.pi, 4) not in phases


def test_mc_error_falls_like_inverse_sqrt_n():
    rows = qae.sweep_mc(P, [100, 1000, 10000, 100000], list(range(12)))
    expo = qae.summarize(rows)["exponent"]
    assert -0.7 < expo < -0.3


def test_mlae_recovers_p_from_exact_hit_counts():
    theta = qae.theta_from_p(P)
    ks = [0, 1, 2, 4, 8]
    shots = [10000] * len(ks)
    hits = [round(math.sin((2 * k + 1) * theta) ** 2 * s) for k, s in zip(ks, shots, strict=True)]
    assert abs(qae.mlae_estimate(ks, hits, shots) - P) < 2e-3


def test_mlae_on_local_statevector_beats_shot_noise():
    rows = qae.sweep_mlae(SEG, P, [0, 1, 2, 4, 8], 50, [1, 2, 3], qae.local_runner)
    assert rows[0]["queries"] == 50 and rows[-1]["queries"] == qae.mlae_queries([0, 1, 2, 4, 8], [50] * 5)
    assert rows[-1]["mean_abs_err"] < rows[0]["mean_abs_err"]


@pytest.mark.parametrize("m", [3, 5])
def test_canonical_estimate_within_phase_resolution(m):
    counts = qae.local_runner(qae.canonical_circuit(SEG, m), 200, 7)
    assert abs(qae.canonical_estimate(counts, m) - P) <= math.pi / 2**m


def test_query_accounting():
    assert qae.mlae_queries([0, 1, 2], [10, 10, 10]) == 10 * (1 + 3 + 5)
    assert qae.canonical_queries(3, 10) == 10 * (2 * 7 + 1)


def test_fit_exponent_recovers_power_law():
    qs = [10, 100, 1000, 10000]
    assert abs(qae.fit_exponent(qs, [1 / q for q in qs]) + 1.0) < 1e-9
    assert abs(qae.fit_exponent(qs, [q**-0.5 for q in qs]) + 0.5) < 1e-9


def test_verdict_bands_and_sanity_gate():
    assert qae.verdict(-0.95, -0.5) == "quantum_1_over_n"
    assert qae.verdict(-0.75, -0.5) == "quantum_like_mc"  # the band edge belongs to the weaker claim
    assert qae.verdict(-0.5, -0.5) == "quantum_like_mc"
    assert qae.verdict(-0.1, -0.5) == "quantum_worse"
    with pytest.raises(ValueError):
        qae.verdict(-0.95, -1.2)  # a Monte Carlo that is not ~1/sqrt(N) means the run is broken
    assert set(qae.FINDINGS) == {"quantum_1_over_n", "quantum_like_mc", "quantum_worse"}


def test_good_fraction_reads_either_bit_order():
    assert qae.good_fraction({"111": 3, "011": 1}, 3) == 0.75
    assert qae.good_fraction({"0111": 1, "1000": 1}, 3) == 0.5  # extra leading bits ignored
    with pytest.raises(ValueError):
        qae.good_fraction({}, 3)


def test_run_qae_record_shape_and_verdicts():
    rec = qae.run_qae(seeds=[1, 2], shots=30, mc_ns=[100, 1000, 10000], ks=[0, 1, 2], ms=[2, 3])
    assert set(rec["arms"]) == {"mc", "mlae", "canonical"} and abs(rec["slab"]["p"] - P) < 1e-12
    for arm in ("mlae", "canonical"):
        assert rec["arms"][arm]["verdict"] in qae.FINDINGS or rec["arms"][arm]["verdict"].startswith("refused")
    assert len(rec["arms"]["mlae"]["rows"]) == 3 and rec["arms"]["mlae"]["rows"][-1]["queries"] == 30 * (1 + 3 + 5)
    assert isinstance(rec["arms"]["mc"]["exponent"], float)
