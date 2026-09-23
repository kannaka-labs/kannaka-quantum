# Wave 4 — what the field's last six months change, and what they don't

Written 2026-09-22 from a review of quantum computing and quantum-for-AI results
through September 2026. The roadmap's thesis (`quantum-wave-roadmap.md`): durable
quantum advances live in one-way dependencies (entropy), verification artifacts
(correspondence benchmarks), interface contracts (QUBO) and infrastructure — never
in putting a QPU on the hot path. The last six months vindicated it: every claimed
quantum-ML advantage on classical data has been dequantized (QCNNs classically
simulable to 1,024 qubits, *PRX Quantum* Apr 2026; no unambiguous QAOA advantage,
*Quantum* Sep 2026; IBM's advantage tracker lists no ML task), while AI improving
quantum hardware (Google's RL calibration of Willow, *Nature* Jul 2026) and
quantum-inspired classical methods (tensor-network LLM compression, simulated
bifurcation) demonstrably work. So this wave adds nothing to the hot path and
tightens what is already there.

| # | item | status |
|---|---|---|
| W4.1 | **Bit-order cliff.** qBraid's unreleased 0.13 flips measurement bitstrings to little-endian across providers; `_measured_index` encodes the per-device convention verified on real devices. Pinned `qbraid>=0.12,<0.13`, `qbraid-core>=0.6.4,<0.7`; the bench now runs a **bit-order canary** first (`core.bit_order_canary`: an amplified peak at index 1 whose bit reversal is index 8) and fails, even under `--update-baseline`, if it trips. | shipped |
| W4.2 | **IonQ gate cap.** Braket rejects >2,000 gates per circuit on IonQ on demand (Jun 2026) after the per-task fee; `run_qasm` refuses before submission. | shipped |
| W4.3 | **Bell certificate per harvest.** `harvest` runs CHSH on the same device first and records `S` in the provenance line (Quantum Origin's design: a Bell-test seed feeding an extractor). A device that does not violate the classical bound that day is refused as an entropy source; `--no-certify` records `bell: null`. With Open Quantum devices at zero qBraid credits (Jul 2026), a certified monthly harvest is free. | shipped; the harvest itself needs the hosts (T1.5) |
| W4.4 | **Readout mitigation, tensored.** `bench --mitigate-readout` runs two calibration jobs (\|0…0⟩, \|1…1⟩) and inverts the per-qubit confusion on every scenario's counts (`core.readout_calibration`, `core.mitigate_readout`). Two tasks, not 2ⁿ, because the per-task fee is the cost. | shipped |
| W4.5 | **Calibration-aware bench.** `calibrate` ranks candidate qubits by retained P(1) after a delay on the native Rigetti route (`decay.rank_qubits`); `bench --layout` places each recall circuit on the chosen qubits (`core.place_on`). The measurement is taken right before the run it informs and recorded with it. | shipped |
| W4.6 | **The Q4 hardware run, pre-registered.** Same 5-scenario subset as ledger row 1, on Cepheus (99.1% 2Q) and on IQM Emerald (99.5% CZ, $0.0016/shot on Braket, zero credits via Open Quantum), each with and without W4.4/W4.5. **Kept** if agreement ≥ 80% with a 95% interval excluding 60% on at least one device; **declined** if no configuration clears 60%; the ledger gets one row per configuration and the depth-versus-fidelity question is answered by the difference between devices, not by a story. Budget: under the standing $5/quarter. | registered here; runs next quarter |
| W4.7 | **Quantum-inspired, the part that pays** (kannaka-memory). (a) Simulated Bifurcation as a `ConsolidationSolver` beside `ClassicalAnneal`, judged by T3.5's one-week dream diff. (b) Tensor-network compression of `kannaka-brain-7b-v1` for CPU serving, accepted only if E-005's anchored faithfulness stays above its 0.808 floor and latency halves. | proposed; separate repo |
| W4.8 | **QuantumOS boot seed.** `scripts/qseed-from-reservoir.sh` draws 256 bits from the reservoir and prints the `qseed=` kernel argument, so a weekly boot carries real provenance through the existing attestation path. | shipped |

## What this wave deliberately does not do

- No quantum transformer, variational classifier or kernel on HRM data: dequantized.
- No QAOA on hardware for consolidation: no measured advantage; QAOA stays the simulator
  reference for the QUBO interface contract.
- No claim that recall-as-amplitude-amplification is a speedup. It is a verification
  artifact; Aaronson's input-loading objection stands and the first hardware QRAM is at
  81% fidelity for four bits (*Nature Physics*, Jun 2026).
