"""Quantum amplitude estimation of a 1-D transport probability, against Monte Carlo.

The warm-up for Kannaka Scientist's Geant4 work (campaign ``ks-quantum-transport-warmup``):
the uncollided transmission of a thermal-neutron beam through a slab is exactly
``P = exp(-Sigma_t * x)``. A classical Monte Carlo estimates it by sampling free paths, with
an error that falls as 1/sqrt(N). Amplitude estimation promises 1/N in the number of oracle
queries. This module encodes the slab as ``n`` segments, each a qubit whose ``|1>`` amplitude
is the segment's survival probability, so the good state ``|1...1>`` has probability ``P``
and the Grover operator is a genuine multi-qubit circuit the compiler cannot fold away.

Three estimators share one oracle:

  mc         sample ``N`` free paths (Bernoulli with the exact ``P``)
  mlae       Suzuki et al. 2020: measure ``Q^k A|0>`` for a ladder of ``k`` and maximise
             the likelihood over the amplitude angle
  canonical  Brassard et al. 2002: phase estimation of ``Q`` with ``m`` counting qubits

Query accounting (one convention, stated in the pre-registration): an *oracle query* is one
application of ``A`` or ``A^dagger``. A shot of ``Q^k A`` costs ``2k + 1``; a shot of canonical
QAE with ``m`` counting qubits costs ``2 (2^m - 1) + 1``; a Monte Carlo sample costs 1.

Nothing here claims an advantage: the slab is one number, and the point is to measure the
two error exponents and see what one real device does to the estimate.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from typing import Any

import numpy as np

# Natural cadmium at 25 meV: sigma_abs 2520 b (113Cd, 12.22 %, drives it), sigma_scat 6.5 b
# (Mughabghab, Atlas of Neutron Resonances, 6th ed.; ENDF/B-VIII.0 thermal values).
# Density 8.65 g/cm3, A = 112.41 -> N = 4.634e22 /cm3 -> Sigma_t = 117.1 /cm.
CD_SIGMA_T_PER_CM = 117.1
CD_SLAB_X_CM = 0.0100  # 100 um -> Sigma_t x = 1.171 -> P = 0.3101

DEFAULT_SEGMENTS = 3
MLAE_K_LADDER = (0, 1, 2, 4, 8, 16, 32, 64)
CANONICAL_M_LADDER = (1, 2, 3, 4, 5, 6, 7, 8)


# --------------------------------------------------------------------------- #
# the slab
# --------------------------------------------------------------------------- #
def slab_transmission(sigma_t_per_cm: float = CD_SIGMA_T_PER_CM, x_cm: float = CD_SLAB_X_CM) -> float:
    """Uncollided transmission through a slab: exp(-Sigma_t x). Exact for any slab."""
    if sigma_t_per_cm < 0 or x_cm < 0:
        raise ValueError("Sigma_t and x must be non-negative")
    return math.exp(-sigma_t_per_cm * x_cm)


def segment_probabilities(p: float, n: int = DEFAULT_SEGMENTS) -> list[float]:
    """Split one transmission ``p`` into ``n`` equal segments whose product is ``p``."""
    if not 0.0 < p < 1.0:
        raise ValueError("p must be strictly between 0 and 1")
    if n < 1:
        raise ValueError("n must be >= 1")
    return [p ** (1.0 / n)] * n


def theta_from_p(p: float) -> float:
    """Amplitude angle: A|0> = cos(theta)|bad> + sin(theta)|good>, sin^2(theta) = p."""
    return math.asin(math.sqrt(p))


# --------------------------------------------------------------------------- #
# classical Monte Carlo
# --------------------------------------------------------------------------- #
def mc_estimate(p: float, n_samples: int, seed: int) -> float:
    """Sample ``n_samples`` free paths from the exponential and count the ones past the slab.

    A free path ``-ln(u)/Sigma_t`` exceeds ``x`` exactly when ``u < exp(-Sigma_t x) = p``, so
    this is the transport Monte Carlo written without the division."""
    if n_samples < 1:
        raise ValueError("n_samples must be >= 1")
    rng = np.random.default_rng(seed)
    return float(np.count_nonzero(rng.random(n_samples) < p)) / n_samples


# --------------------------------------------------------------------------- #
# circuits
# --------------------------------------------------------------------------- #
def a_operator(seg_probs: list[float]):
    """``A``: one RY per segment so that P(|1...1>) = prod(seg_probs)."""
    from qiskit import QuantumCircuit

    n = len(seg_probs)
    qc = QuantumCircuit(n, name="A")
    for i, pi in enumerate(seg_probs):
        qc.ry(2.0 * math.asin(math.sqrt(pi)), i)
    return qc


def _phase_flip_all_ones(qc, qubits: list[int]) -> None:
    if len(qubits) == 1:
        qc.z(qubits[0])
    else:
        qc.mcp(math.pi, qubits[:-1], qubits[-1])


def grover_operator(seg_probs: list[float]):
    """``Q = A S_0 A^dagger S_chi`` on ``n`` qubits; the good state is ``|1...1>``."""
    from qiskit import QuantumCircuit

    n = len(seg_probs)
    qs = list(range(n))
    a = a_operator(seg_probs)
    qc = QuantumCircuit(n, name="Q")
    _phase_flip_all_ones(qc, qs)  # S_chi
    qc.compose(a.inverse(), qs, inplace=True)
    qc.x(qs)
    _phase_flip_all_ones(qc, qs)  # S_0 (up to the global phase)
    qc.x(qs)
    qc.compose(a, qs, inplace=True)
    # Brassard et al. write Q = -A S_0 A^dagger S_chi. The global sign is invisible to MLAE but a
    # controlled Q carries it as a phase on the control, so phase estimation needs it exact.
    qc.global_phase = math.pi
    return qc


def mlae_circuit(seg_probs: list[float], k: int):
    """``Q^k A |0>`` with every qubit measured. Good outcome = all ones."""
    from qiskit import QuantumCircuit

    if k < 0:
        raise ValueError("k must be >= 0")
    n = len(seg_probs)
    qc = QuantumCircuit(n, n, name=f"mlae_k{k}")
    qc.compose(a_operator(seg_probs), range(n), inplace=True)
    q = grover_operator(seg_probs)
    for _ in range(k):
        qc.compose(q, range(n), inplace=True)
    qc.measure(range(n), range(n))
    return qc


def canonical_circuit(seg_probs: list[float], m: int):
    """Phase estimation of ``Q`` with ``m`` counting qubits (qubits 0..m-1), the state register
    after them. Only the counting register is measured; the classical bit order matches qiskit's
    little-endian integer so ``int(bits, 2)`` is the estimate ``y``."""
    from qiskit import QuantumCircuit
    from qiskit.circuit.library import QFTGate

    if m < 1:
        raise ValueError("m must be >= 1")
    n = len(seg_probs)
    qc = QuantumCircuit(m + n, m, name=f"qae_m{m}")
    state = list(range(m, m + n))
    qc.h(range(m))
    qc.compose(a_operator(seg_probs), state, inplace=True)
    q_gate = grover_operator(seg_probs).to_gate(label="Q")
    for j in range(m):
        power = q_gate.power(2**j).control(1, label=f"cQ^{2**j}")
        qc.append(power, [j, *state])
    qc.append(QFTGate(m).inverse(), range(m))
    qc.measure(range(m), range(m))
    return qc


# --------------------------------------------------------------------------- #
# estimators
# --------------------------------------------------------------------------- #
def good_fraction(counts: dict[str, int], n: int) -> float:
    """Fraction of shots on the good state (all ones), whichever bit order the backend used."""
    total = sum(counts.values())
    if total <= 0:
        raise ValueError("no counts")
    good = sum(c for bits, c in counts.items() if bits.replace(" ", "")[-n:] == "1" * n)
    return good / total


def mlae_loglik(theta: float, ks: list[int], hits: list[int], shots: list[int]) -> float:
    ll = 0.0
    for k, h, s in zip(ks, hits, shots, strict=True):
        pk = math.sin((2 * k + 1) * theta) ** 2
        pk = min(max(pk, 1e-12), 1 - 1e-12)
        ll += h * math.log(pk) + (s - h) * math.log(1 - pk)
    return ll


def mlae_estimate(ks: list[int], hits: list[int], shots: list[int], grid: int = 20000) -> float:
    """Maximum-likelihood amplitude from the ladder's hit counts: returns the estimate of ``p``.

    The likelihood over ``theta`` in [0, pi/2] is multimodal for large ``k``; a dense grid
    followed by a local refinement is the standard, robust choice."""
    thetas = np.linspace(0.0, math.pi / 2, grid)
    lls = np.array([mlae_loglik(t, ks, hits, shots) for t in thetas])
    best = float(thetas[int(np.argmax(lls))])
    lo, hi = max(0.0, best - 2 * (math.pi / 2) / grid), min(math.pi / 2, best + 2 * (math.pi / 2) / grid)
    fine = np.linspace(lo, hi, 2001)
    flls = np.array([mlae_loglik(t, ks, hits, shots) for t in fine])
    return math.sin(float(fine[int(np.argmax(flls))])) ** 2


def canonical_estimate(counts: dict[str, int], m: int) -> float:
    """Most frequent counting-register outcome ``y`` -> ``sin^2(pi y / 2^m)``."""
    if not counts:
        raise ValueError("no counts")
    marginal: dict[str, int] = {}
    for bits, c in counts.items():  # a backend that returns every qubit is marginalised to the counting register
        key = bits.replace(" ", "")[-m:]
        marginal[key] = marginal.get(key, 0) + c
    best = max(marginal.items(), key=lambda kv: kv[1])[0]
    y = int(best, 2)
    return math.sin(math.pi * y / 2**m) ** 2


def mlae_queries(ks: list[int], shots: list[int]) -> int:
    return int(sum((2 * k + 1) * s for k, s in zip(ks, shots, strict=True)))


def canonical_queries(m: int, shots: int) -> int:
    return int((2 * (2**m - 1) + 1) * shots)


def fit_exponent(queries: list[float], errors: list[float]) -> float:
    """Least-squares slope of log(error) against log(queries); a 1/N law gives -1."""
    x = np.log(np.asarray(queries, dtype=float))
    y = np.log(np.asarray(errors, dtype=float))
    if len(x) < 2:
        raise ValueError("need at least two points")
    slope, _ = np.polyfit(x, y, 1)
    return float(slope)


# --------------------------------------------------------------------------- #
# the pre-registered verdict
# --------------------------------------------------------------------------- #
FINDINGS = {
    "quantum_1_over_n": "quantum error falls faster than Monte Carlo's 1/sqrt(N): exponent below -0.75",
    "quantum_like_mc": "quantum error falls like Monte Carlo (exponent between -0.75 and -0.25)",
    "quantum_worse": "quantum error does not fall with queries (exponent above -0.25)",
}


def verdict(exponent_quantum: float, exponent_mc: float) -> str:
    """Classify a fitted quantum exponent against the pre-registered bands. The Monte Carlo
    exponent is reported beside it and must itself sit near -0.5 for the run to be sane."""
    if not (-0.8 <= exponent_mc <= -0.2):
        raise ValueError(f"Monte Carlo exponent {exponent_mc:.3f} is not near -0.5; the run is not sane")
    if exponent_quantum < -0.75:
        return "quantum_1_over_n"
    if exponent_quantum <= -0.25:
        return "quantum_like_mc"
    return "quantum_worse"


# --------------------------------------------------------------------------- #
# sweeps (runner-injected so tests and the pre-registration dry run cost $0)
# --------------------------------------------------------------------------- #
Runner = Callable[[Any, int, int], dict[str, int]]  # (circuit, shots, seed) -> counts


def sweep_mc(p: float, ns: list[int], seeds: list[int]) -> list[dict[str, Any]]:
    rows = []
    for n in ns:
        errs = [abs(mc_estimate(p, n, s) - p) for s in seeds]
        rows.append({"queries": n, "mean_abs_err": float(np.mean(errs)), "errs": errs})
    return rows


def sweep_mlae(seg_probs: list[float], p: float, ks: list[int], shots: int, seeds: list[int], runner: Runner):
    """Cumulative ladder: the estimate after ``k <= k_j`` uses every rung so far."""
    n = len(seg_probs)
    rows = []
    per_seed_hits: dict[int, list[int]] = {s: [] for s in seeds}
    for j, k in enumerate(ks):
        circ = mlae_circuit(seg_probs, k)
        errs = []
        for s in seeds:
            counts = runner(circ, shots, s * 1000 + k)
            per_seed_hits[s].append(round(good_fraction(counts, n) * shots))
            est = mlae_estimate(list(ks[: j + 1]), per_seed_hits[s], [shots] * (j + 1))
            errs.append(abs(est - p))
        rows.append({"k_max": k, "queries": mlae_queries(list(ks[: j + 1]), [shots] * (j + 1)),
                     "mean_abs_err": float(np.mean(errs)), "errs": errs})
    return rows


def sweep_canonical(seg_probs: list[float], p: float, ms: list[int], shots: int, seeds: list[int], runner: Runner):
    rows = []
    for m in ms:
        circ = canonical_circuit(seg_probs, m)
        errs = []
        for s in seeds:
            counts = runner(circ, shots, s * 1000 + m)
            errs.append(abs(canonical_estimate(counts, m) - p))
        rows.append({"m": m, "queries": canonical_queries(m, shots), "mean_abs_err": float(np.mean(errs)),
                     "errs": errs})
    return rows


def local_runner(circuit, shots: int, seed: int) -> dict[str, int]:
    """Noiseless local state-vector sampling through the bridge's own runner (no network)."""
    from .core import _run_local_circuit

    return _run_local_circuit(circuit, shots, "local:sv", seed=seed)["counts"]


def hosted_runner(device: str, allow_spend: bool = False, max_credits: float | None = None) -> Runner:
    """A runner that submits through :func:`core.run_qiskit`; the seed is ignored (the backend
    samples), and the spend guard applies on real hardware."""
    from .core import run_qiskit

    def _run(circuit, shots: int, seed: int) -> dict[str, int]:
        out = run_qiskit(circuit, device=device, shots=shots, allow_spend=allow_spend, max_credits=max_credits)
        return out["counts"]

    return _run


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    qs = [r["queries"] for r in rows]
    es = [r["mean_abs_err"] for r in rows]
    return {"queries": qs, "mean_abs_err": es, "exponent": fit_exponent(qs, es)}


def to_json(obj: Any) -> str:
    return json.dumps(obj, indent=1, sort_keys=True)


def run_qae(
    *,
    arms: tuple[str, ...] = ("mc", "mlae", "canonical"),
    device: str = "local:sv",
    seeds: list[int] | None = None,
    shots: int = 50,
    mc_ns: list[int] | None = None,
    ks: list[int] | None = None,
    ms: list[int] | None = None,
    sigma_t_per_cm: float = CD_SIGMA_T_PER_CM,
    x_cm: float = CD_SLAB_X_CM,
    segments: int = DEFAULT_SEGMENTS,
    allow_spend: bool = False,
    max_credits: float | None = None,
    runner: Runner | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run the pre-registered sweeps and return one JSON-serialisable record.

    ``device`` selects the runner: ``local:*`` samples the noiseless state-vector here (seeded,
    repeatable); anything else goes through the bridge, where the seed is ignored and real
    hardware is spend-guarded. ``mc`` never touches a device."""
    seeds = seeds or list(range(10))
    mc_ns = mc_ns or [10, 100, 1000, 10000, 100000]
    ks = ks or list(MLAE_K_LADDER)
    ms = ms or list(CANONICAL_M_LADDER)
    say = log or (lambda _m: None)
    p = slab_transmission(sigma_t_per_cm, x_cm)
    seg = segment_probabilities(p, segments)
    if runner is None:
        runner = local_runner if device.startswith("local:") else hosted_runner(device, allow_spend, max_credits)
    rec: dict[str, Any] = {
        "slab": {"sigma_t_per_cm": sigma_t_per_cm, "x_cm": x_cm, "p": p, "segments": segments, "seg_probs": seg},
        "device": device, "shots": shots, "seeds": seeds, "arms": {},
    }
    if "mc" in arms:
        say(f"mc: n in {mc_ns} x {len(seeds)} seeds")
        rows = sweep_mc(p, mc_ns, seeds)
        rec["arms"]["mc"] = {"rows": rows, **summarize(rows)}
    if "mlae" in arms:
        say(f"mlae: k ladder {ks}, {shots} shots per rung x {len(seeds)} seeds on {device}")
        rows = sweep_mlae(seg, p, ks, shots, seeds, runner)
        rec["arms"]["mlae"] = {"rows": rows, **summarize(rows)}
    if "canonical" in arms:
        say(f"canonical: m in {ms}, {shots} shots x {len(seeds)} seeds on {device}")
        rows = sweep_canonical(seg, p, ms, shots, seeds, runner)
        rec["arms"]["canonical"] = {"rows": rows, **summarize(rows)}
    if "mc" in rec["arms"]:
        e_mc = rec["arms"]["mc"]["exponent"]
        for arm in ("mlae", "canonical"):
            if arm in rec["arms"]:
                try:
                    rec["arms"][arm]["verdict"] = verdict(rec["arms"][arm]["exponent"], e_mc)
                except ValueError as exc:
                    rec["arms"][arm]["verdict"] = f"refused: {exc}"
    return rec
