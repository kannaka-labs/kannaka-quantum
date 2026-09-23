"""Wave 4 (2026-09-22): the bit-order canary, IonQ gate cap, tensored readout
mitigation, qubit placement, and calibration ranking. All hermetic ($0, no
network): the local state-vector backend and fakes.
"""

from __future__ import annotations

import pytest

from kannaka_quantum import bench, core, decay
from kannaka_quantum.cli import main


# ── bit-order canary ────────────────────────────────────────────────────────
def test_canary_passes_on_the_local_backend_and_names_the_reversed_trap():
    out = core.bit_order_canary(device=core.LOCAL_DEVICE, shots=512)
    assert out["ok"] is True
    assert out["expected"] == "canary-01"
    assert out["bit_reversed_would_be"] == "canary-08"


def test_canary_fails_when_a_decoder_reads_the_other_end(monkeypatch):
    # Flip the convention the bridge assumes for the local device (no reversal): the canary must catch it.
    real = core._measured_index
    monkeypatch.setattr(core, "_measured_index", lambda bits, device: int(bits[::-1], 2) if device == core.LOCAL_DEVICE else real(bits, device))
    out = core.bit_order_canary(device=core.LOCAL_DEVICE, shots=512)
    assert out["ok"] is False
    assert out["got"] == "canary-08"


def test_bench_fails_on_a_failed_canary_even_when_updating_the_baseline(tmp_path):
    corpus = tmp_path / "c.json"
    corpus.write_text(
        '{"format": "kannaka-recall-bench/1", "n": 1, "scenarios": [{"query_label": "b", '
        '"candidates": [{"label": "a", "amplitude": 0.1}, {"label": "b", "amplitude": 0.9}], "classical_argmax": 1}]}',
        encoding="utf-8",
    )
    bad = lambda **k: {"ok": False, "expected": "canary-01", "got": "canary-08"}
    good = lambda **k: {"ok": True, "expected": "canary-01", "got": "canary-01"}
    res, code = bench.bench_command(scenarios=str(corpus), canary_fn=bad, baseline=str(tmp_path / "b.json"),
                                    update_baseline=True)
    assert code == 1 and res["canary_failed"] is True and not (tmp_path / "b.json").exists()
    res, code = bench.bench_command(scenarios=str(corpus), canary_fn=good)
    assert code == 0 and res["bit_order_canary"]["ok"] is True
    res, code = bench.bench_command(scenarios=str(corpus), canary=False)
    assert code == 0 and res["bit_order_canary"] is None


def test_bench_passes_layout_and_readout_cal_to_recall(tmp_path):
    corpus = tmp_path / "c.json"
    corpus.write_text(
        '{"format": "kannaka-recall-bench/1", "n": 1, "scenarios": [{"query_label": "b", '
        '"candidates": [{"label": "a", "amplitude": 0.1}, {"label": "b", "amplitude": 0.9}], "classical_argmax": 1}]}',
        encoding="utf-8",
    )
    seen = {}

    def recall(amps, **kw):
        seen.update(kw)
        return {"agree": True, "classical_top": "b", "quantum_top": "b"}

    res, code = bench.bench_command(scenarios=str(corpus), recall_fn=recall, canary=False, layout=[5, 7],
                                    readout_cal=[(0.01, 0.02), (0.0, 0.0)])
    assert code == 0
    assert seen["layout"] == [5, 7] and seen["readout_cal"] == [(0.01, 0.02), (0.0, 0.0)]
    assert res["layout"] == [5, 7] and res["readout_mitigated"] is True


# ── IonQ gate cap ───────────────────────────────────────────────────────────
def test_gate_count_and_ionq_cap():
    qasm = "OPENQASM 3.0;\ninclude \"stdgates.inc\";\nqubit[2] q;\nbit[2] c;\nh q[0];\ncx q[0], q[1];\nbarrier q;\nc = measure q;\n"
    assert core.count_gates(qasm) == 2
    big = "OPENQASM 3.0;\nqubit[1] q;\n" + "x q[0];\n" * (core.IONQ_GATE_CAP + 1)
    with pytest.raises(RuntimeError, match="IonQ devices reject"):
        core._check_gate_cap(big, "aws:ionq:qpu:forte-1")
    core._check_gate_cap(big, "aws:rigetti:qpu:cepheus-1-108q")  # other devices: no cap here


# ── readout mitigation ──────────────────────────────────────────────────────
def test_readout_calibration_and_mitigation_recover_a_clean_distribution():
    # Two qubits: position 0 flips 0→1 10% of the time, position 1 flips 1→0 20%.
    cal = core.readout_calibration({"00": 900, "10": 100}, {"11": 800, "10": 200})
    assert cal == [(0.1, 0.0), (0.0, 0.2)]
    # A true |11> state read through that confusion: 80% "11", 20% "10".
    mitigated = core.mitigate_readout({"11": 800, "10": 200}, cal)
    assert mitigated.get("11", 0) >= 990 and sum(mitigated.values()) == 1000
    # A perfect calibration is the identity.
    assert core.mitigate_readout({"01": 3, "10": 7}, [(0.0, 0.0), (0.0, 0.0)]) == {"01": 3, "10": 7}
    with pytest.raises(ValueError, match="calibration covers"):
        core.mitigate_readout({"01": 1}, [(0.0, 0.0)])


def test_recall_with_readout_cal_reports_it_and_still_agrees():
    out = core.quantum_recall([0.1, 0.9, 0.2, 0.15], shots=512, device=core.LOCAL_DEVICE,
                              readout_cal=[(0.0, 0.0), (0.0, 0.0)])
    assert out["agree"] and out["readout_mitigated"] is True


# ── placement ───────────────────────────────────────────────────────────────
def test_place_on_widens_the_circuit_and_keeps_the_classical_register():
    from qiskit import QuantumCircuit

    qc = QuantumCircuit(2, 2)
    qc.h(0)
    qc.cx(0, 1)
    qc.measure([0, 1], [0, 1])
    wide = core.place_on(qc, [5, 7])
    assert wide.num_qubits == 8 and wide.num_clbits == 2
    used = {wide.find_bit(q).index for inst in wide.data for q in inst.qubits}
    assert used == {5, 7}
    with pytest.raises(ValueError, match="layout has"):
        core.place_on(qc, [1])
    with pytest.raises(ValueError, match="distinct"):
        core.place_on(qc, [3, 3])
    out = core.quantum_recall([0.1, 0.9, 0.2, 0.1], shots=256, device=core.LOCAL_DEVICE, layout=[2, 4])
    assert out["agree"] and out["layout"] == [2, 4]


# ── calibration ranking ─────────────────────────────────────────────────────
def test_rank_qubits_orders_by_retained_p1_and_sums_credits():
    def runner(quil: str, shots: int):
        # Pull the qubit index the program targets from its text.
        import re

        m = re.search(r"\b(?:X|RX\([^)]*\))\s+(\d+)", quil)
        qb = int(m.group(1)) if m else 0
        p1 = {0: 0.55, 3: 0.82, 6: 0.70}.get(qb, 0.1)
        ones = round(shots * p1)
        return {"counts": {"1": ones, "0": shots - ones}, "billed": {"cost": 0.5}, "job_id": f"j{qb}"}

    out = decay.rank_qubits(runner, [0, 3, 6], delay_us=20.0, shots=100)
    assert out["best"] == [3, 6, 0]
    assert out["credits_total"] == 1.5
    assert out["ranked"][0]["p1"] == 0.82


# ── CLI wiring ──────────────────────────────────────────────────────────────
def test_cli_bench_no_canary_and_layout_parse(tmp_path, capsys):
    corpus = tmp_path / "c.json"
    corpus.write_text(
        '{"format": "kannaka-recall-bench/1", "n": 1, "scenarios": [{"query_label": "b", '
        '"candidates": [{"label": "a", "amplitude": 0.1}, {"label": "b", "amplitude": 0.9}, '
        '{"label": "c", "amplitude": 0.2}, {"label": "d", "amplitude": 0.1}], "classical_argmax": 1}]}',
        encoding="utf-8",
    )
    code = main(["bench", "--scenarios", str(corpus), "--no-canary", "--layout", "1,3", "--shots", "128"])
    import json

    doc = json.loads(capsys.readouterr().out)
    assert code == 0 and doc["layout"] == [1, 3] and doc["bit_order_canary"] is None
