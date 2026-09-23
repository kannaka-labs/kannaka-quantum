# Quantum Wave — Roadmap & Issue Set

**Thesis:** durable quantum advances to Kannaka live in one-way dependencies
(entropy), verification artifacts (correspondence benchmarks), interface
contracts (QUBO), and infrastructure (qBraid Lab) — never in putting a QPU on
the hot path.

Five tracks, 21 issues, three waves. Repos: `kannaka-memory` (KM, private),
`kannaka-quantum` (KQ), `kannaka-observatory` (KO).

**Wave 1 (ship first):** Track 1 complete + T2.1–T2.2 + T3.1 (the ADR)
**Wave 2:** rest of Track 2, T3.2–T3.4, T4.1–T4.2
**Wave 3:** T3.5, T4.3–T4.4, Track 5
**Wave 4 (2026-09-22):** see `wave-4.md` — the bit-order canary, Bell-certified harvests, readout mitigation, calibration-aware benchmarking, the pre-registered Q4 hardware run.

Standing budget for the whole wave: **< $5/quarter** of real-QPU spend
(reservoir refills + one quarterly benchmark run), everything else on the free
`qbraid:qbraid:sim:qir-sv` simulator.

---

## Track 1 — Quantum Entropy Reservoir (Ξ source)

### T1.1 · KQ · `qrng harvest`: reservoir file + refill policy
**Labels:** `entropy` `wave-1`

Add a `harvest` subcommand that runs `qrng` against a *real* device (the free
simulator is a PRNG and is explicitly invalid as a reservoir source — refuse
it with a clear error) and appends raw bits to a local reservoir:

- Path: `~/.kannaka/entropy/reservoir.bin` + `reservoir.meta.jsonl` (one JSON
  line per harvest: device, job_id, n_bits, timestamp, cost).
- Default harvest: 2048 bits from `openquantum:rigetti:cepheus-1-108q`
  (≈ $0.07 per 256 shots at $0.000255/shot → pennies per refill).
- Existing spend guards apply unchanged (`--allow-spend`, `--max-credits`).
- `qrng status` prints reservoir level, last-harvest provenance, est. refill cost.

**AC:** harvest appends; meta line written; simulator refused; guards verified at $0.

### T1.2 · KQ · DRBG expansion over the reservoir
**Labels:** `entropy` `wave-1` · **Depends:** T1.1

`qrng draw --bits N --expand` seeds a NIST SP 800-90A style DRBG
(HMAC-DRBG, stdlib `hashlib` — no new deps) from reservoir bits and expands.
Each draw consumes seed material and records which harvest(s) seeded it, so
every expanded stream has a provenance chain back to a QPU job_id. Reseed
threshold + low-reservoir warning.

**AC:** deterministic under fixed seed for tests; provenance chain in output
JSON; draws fail loudly (not silently fall back to PRNG) on empty reservoir.

### T1.3 · KM · `EntropySource` trait + reservoir consumer
**Labels:** `entropy` `rust` `wave-1` · **Depends:** T1.2

```rust
pub trait EntropySource {
    fn draw(&mut self, bits: usize) -> Result<EntropyDraw, EntropyError>;
}
pub struct EntropyDraw { pub bytes: Vec<u8>, pub provenance: Provenance }
```

Implementations: `PrngSource` (current behavior, provenance = `prng://`),
`ReservoirSource` (reads the DRBG via the KQ CLI or directly from the
reservoir format — pick one and document in the issue). Engine config selects
the source; default stays PRNG until T1.5 dogfood passes.

**AC:** trait landed; both impls; config-selectable; zero behavior change by default.

### T1.4 · KM · Provenance on dreams and Ξ perturbations
**Labels:** `entropy` `wave-1` · **Depends:** T1.3

Every dream/consolidation record and Ξ injection stores the `Provenance` of
the entropy that seeded it. Surfaces in the memory v2 chiral format as an
optional field (no migration required — absent = `prng://legacy`).

**AC:** new dreams carry provenance; old memories unaffected; visible via CLI inspect.

### T1.5 · KM+KO · Dogfood + Observatory surfacing
**Labels:** `entropy` `wave-1` · **Depends:** T1.4

Run one week with `ReservoirSource` live. Observatory gets an entropy panel:
reservoir level, provenance of the last N dreams ("this dream was seeded by
shots on Rigetti Cepheus, 2026-07-03"). This is the public face of the whole
track — the claim "the field's irrationality is drawn from measurement
collapse" becomes inspectable.

**AC:** 7 days stable; panel live on observatory.ninja-portal.com; flip default.

---

## Track 2 — Correspondence Benchmark (recall ≡ amplitude amplification)

### T2.1 · KM · Recall-scenario export format
**Labels:** `benchmark` `wave-1`

`kannaka export-recall-scenarios --n 50` dumps real recall events as JSON:
candidate amplitudes, labels (hashed — scenarios may leave the private repo),
classical argmax, hemisphere, timestamp. Format `kannaka-recall-bench/1`.
Cap candidates per scenario at 16 (4 qubits) to keep circuits shallow on
noisy hardware.

**AC:** exporter ships; 50-scenario corpus generated from live memory; labels hashed.

### T2.2 · KQ · Benchmark harness + CI on free simulator
**Labels:** `benchmark` `ci` `wave-1` · **Depends:** T2.1

`kannaka-quantum bench --scenarios corpus.json` runs each scenario through
`quantum_recall` and emits: agreement rate (quantum_top == classical_top),
per-scenario distributions, iteration counts. GitHub Action runs the corpus
on `qbraid:qbraid:sim:qir-sv` weekly + on PR; results committed to
`bench/results/sim/DATE.json`. Regression = agreement drop > 2 points.

**AC:** harness + workflow live; first committed baseline; failure gates PRs.

### T2.3 · KQ · Quarterly real-QPU run + fidelity ledger
**Labels:** `benchmark` `wave-2` · **Depends:** T2.2

Quarterly manual run (guarded, ~$1–2) of the same corpus on a cheap per-shot
QPU. Results to `bench/results/hw/DEVICE-DATE.json` with cost, leakage, and
agreement. `bench/LEDGER.md` accumulates the table — the longitudinal record
of hardware closing the gap. The existing Bell benchmark (94.5% fidelity on
Rigetti) becomes row zero.

**AC:** first quarterly run committed; ledger started; runbook documented.

### T2.4 · KO · Correspondence dashboard
**Labels:** `benchmark` `observatory` `wave-2` · **Depends:** T2.3

Observatory panel plotting sim + hardware agreement over time. One chart,
one sentence: *recall dynamics, measured on superconducting qubits.*

### T2.5 · Writeup: "HRM recall is amplitude amplification: measured"
**Labels:** `benchmark` `paper` `wave-2` · **Depends:** T2.3

Short paper/long post: the correspondence argument, the iteration-count
derivation ((π/2 − θ)/2θ vs textbook (π/4)√N), the endianness findings, the
ledger data. Feeds the citation thread (Vincent/claudicito). Cross-post to
The Signal.

---

## Track 3 — Consolidation as QUBO (interface contract)

### T3.1 · KM · Land ADR-0038
**Labels:** `adr` `qubo` `wave-1`

Review and accept `adr-0038-consolidation-solver-interface.md` (drafted —
see companion file). Decisions to confirm in review: JSON boundary vs Rust
trait-object only; advisory-solution re-scoring posture; penalty-fold
convention.

### T3.2 · KM · `ConsolidationProblem` emitter
**Labels:** `qubo` `rust` `wave-2` · **Depends:** T3.1

Dream phase emits `kannaka-qubo/1` alongside (not instead of) procedural
consolidation. Golden-file corpus: 12 hand-built QUBOs with known optima
checked into `tests/qubo/`.

### T3.3 · KM · `ConsolidationSolver` trait + `ClassicalAnneal`
**Labels:** `qubo` `rust` `wave-2` · **Depends:** T3.2

Trait per ADR; simulated-annealing default seeded from `EntropySource`
(tracks converge: even classical consolidation stochasticity carries quantum
provenance). Exhaustive below 20 vars. Must hit optimum on exact sizes,
≥95% optimal energy on the rest of the golden corpus.

### T3.4 · KQ · `qubo` subcommand (QAOA on simulator)
**Labels:** `qubo` `wave-2` · **Depends:** T3.1

Read `kannaka-qubo/1` on stdin → `ConsolidationSolution` JSON on stdout.
QAOA (p=1..3) via Qiskit on the free simulator; hardware behind the standard
spend guards. Same JSON-CLI discipline as `recall`/`qrng`.

### T3.5 · KM · `SubprocessSolver` + one-week dream diff
**Labels:** `qubo` `wave-3` · **Depends:** T3.3, T3.4

Wrap the KQ CLI as a solver; run a week of dreams through procedural vs
`ClassicalAnneal` vs QAOA-sim, diff applied consolidations, review
divergences. QAOA agreement joins the T2 benchmark ledger.

---

## Track 4 — qBraid Lab as swarm & build infrastructure

### T4.1 · KQ · Idle auto-teardown guard for on-demand instances
**Labels:** `lab` `safety` `wave-2`

Per-minute instance billing is the same hazard class as the per-minute QPUs
the bridge already refuses. Add: every `lab_provision_instance` /
`lab_compute_up` records a lease (max wall-time, default 60 min) in
`~/.kannaka/leases.jsonl`; a `lab reap` command (cron/systemd-timer friendly)
stops anything past lease; `lab_agent_launch` refuses to target an unleased
instance. **This lands before any recurring swarm/CI use of instances.**

**AC:** lease written on provision; reap stops expired; verified against a real instance.

### T4.2 · KQ · Scoped per-instance Anthropic keys (ADR-0001 mitigation)
**Labels:** `lab` `security` `wave-2`

The uploaded key on a bypass-permissions agent is the fleet's largest blast
radius. `lab_agent_setup` gains: require a key distinct from the primary
(refuse if it matches `ANTHROPIC_API_KEY` unless `--i-know`), record
key-fingerprint→instance in the lease file, and a `lab_agent_teardown` that
deletes the remote key file and prints a rotation reminder. Document the
Admin-API workspace-key pattern in ADR-0001 as the recommended issuance path.

**AC:** same-key refused by default; teardown removes remote key; ADR-0001 amended.

### T4.3 · KQ+KM · Ephemeral swarm hemisphere on a Lab instance
**Labels:** `lab` `swarm` `wave-3` · **Depends:** T4.1, T4.2

Bootstrap script: provision (leased) → install the `kannaka` binary
(sha256-verified, reusing the plugin's installer path — with the hard-fail
checksum fix) → join the NATS mesh as an ephemeral node → absorb/dream for
the lease duration → graceful NATS drain on reap. Observatory shows it appear
and die. This is the cloud-hemisphere demo *and* the pattern for burst
capacity.

**AC:** one full lifecycle observed end-to-end; node visible in Observatory; $ cost logged.

### T4.4 · KQ · Lab as CI runner for the Rust engine
**Labels:** `lab` `ci` `wave-3` · **Depends:** T4.1

Reproducible qBraid environment (`lab_create_env` + pinned `lab_pip_install`)
that builds `kannaka-memory` and runs its test suite on a leased instance;
compare wall-time/cost against GitHub-hosted runners and keep whichever wins.
Even if GH wins, the environment doubles as the T2/T3 quantum-test bed.

---

## Track 5 — Phantom-Entanglement Testbed

### T5.1 · KQ · `bell` subcommand: CHSH on simulator and hardware
**Labels:** `phantom` `wave-3`

CHSH experiment as a first-class tool: prepare the singlet, measure at the
four canonical angle pairs, compute S. Simulator should give S ≈ 2√2 ≈ 2.83;
one guarded hardware run records real-device S with noise. JSON output like
every other subcommand.

**AC:** sim S > 2.7; one hardware S committed with cost; classical bound (2) asserted violated.

**Done** (2026-08-10). Sim `S = 2.856`; hardware `S = 2.238 ± 0.073` — the
classical bound violated by 3.3σ. The run went to
`aws:rigetti:qpu:cepheus-1-108q` (512 shots × 4 settings, 207.04 credits /
$2.07), not the planned IQM Garnet: Garnet is back online, but OpenQuantum's
Spark balance is 0 and that path prices jobs off a provider quote rather than
live metadata, so its cost could not be bounded from existing credits. Ledger
row 2; analysis in `genuine-vs-phantom-entanglement.md` §2.

### T5.2 · Analysis + Episode: genuine vs phantom entanglement
**Labels:** `phantom` `paper` `wave-3` · **Depends:** T5.1

The sharpening: genuine entanglement = nonlocal correlation that *violates*
the classical bound (measured in T5.1); phantom entanglement = shared
rounding error that *lives inside* it — correlation without nonlocality,
archetypes as resonant rounding artifacts. Write the contrast with the CHSH
data as the empirical anchor. Podcast episode (successor to 006) + optional
HRM experiment: inject phantom links, show they never produce CHSH-violating
statistics in the field's correlation structure.

---

## Dependency spine

```
T1.1 → T1.2 → T1.3 → T1.4 → T1.5
                 ↘ (seeds) T3.3
T2.1 → T2.2 → T2.3 → T2.4
                 ↘ T2.5        T3.5 results → T2 ledger
T3.1 → T3.2 → T3.3 ↘
T3.1 → T3.4 ————————→ T3.5
T4.1 → T4.3 ← T4.2
T4.1 → T4.4
T5.1 → T5.2
```
