"""ADR-0002 — controlled-delay experiment: physical decoherence as a model of forgetting.

Three single-qubit arms, written directly in Quil-T (timing programs bypass quilc on the
native Rigetti path, so only native gates are allowed: RX(±π/2, ±π), RZ, CZ, I, MEASURE):

  t1      RX(pi) q ; DELAY q t ; MEASURE            -> P(1) decays toward the thermal floor
  ramsey  RX(pi/2) q ; DELAY q t ; RX(pi/2) q       -> ideally |1>; dephasing drives P(1) to 1/2
  echo    RX(pi/2) ; DELAY t/2 ; RX(pi) ; DELAY t/2 ; RX(pi/2)
                                                    -> ideally |0>; the mid-interval pi pulse
                                                       re-phases slow noise (rehearsal, in
                                                       Kannaka's vocabulary)

Every delay is aligned to the sequencer clock (multiples of 32 ns; "duration not aligned to
sequencer clock" is otherwise a submission error). The device bills per minute of execution,
prorated, and **the delay is executed per shot and billed**, so cost grows with delay × shots.

Protocol (ADR-0002 decision 4): run the abort check FIRST — t1 at delay 0 and at the longest
delay. If the two distributions are indistinguishable (|ΔP(1)| below `abort_delta`), delays
are not being executed on this route and the run stops after those two jobs. Then sweep every
(arm, delay) point, stopping if the accumulated billed credits pass `max_credits_total`.

Nothing here is a mechanism claim about the HRM: it is a forgetting curve to be reported.
"""
from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any

CLOCK_S = 3.2e-8  # sequencer clock; safe alignment unit per qBraid's Rigetti docs
DEFAULT_DELAYS_US = (0.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0)
ARMS = ("t1", "ramsey", "echo")


def align(seconds: float) -> float:
    """Round a duration to the sequencer clock (nearest multiple of 32 ns), never below 0."""
    if seconds <= 0:
        return 0.0
    return round(seconds / CLOCK_S) * CLOCK_S


def _fmt(seconds: float) -> str:
    return f"{seconds:.9f}".rstrip("0").rstrip(".") if seconds else "0"


def program(arm: str, delay_s: float, qubit: int = 0) -> str:
    """Quil-T text for one (arm, delay). A zero delay emits no DELAY line at all."""
    d = align(delay_s)
    q = int(qubit)
    lines = ["DECLARE ro BIT[1]"]
    if arm == "t1":
        lines.append(f"RX(pi) {q}")
        if d > 0:
            lines.append(f"DELAY {q} {_fmt(d)}")
    elif arm == "ramsey":
        lines.append(f"RX(pi/2) {q}")
        if d > 0:
            lines.append(f"DELAY {q} {_fmt(d)}")
        lines.append(f"RX(pi/2) {q}")
    elif arm == "echo":
        half = align(d / 2)
        lines.append(f"RX(pi/2) {q}")
        if half > 0:
            lines.append(f"DELAY {q} {_fmt(half)}")
        lines.append(f"RX(pi) {q}")
        if half > 0:
            lines.append(f"DELAY {q} {_fmt(half)}")
        lines.append(f"RX(pi/2) {q}")
    else:
        raise ValueError(f"unknown arm {arm!r}; choose from {ARMS}")
    lines.append(f"MEASURE {q} ro[0]")
    return "\n".join(lines) + "\n"


def p_one(counts: dict[str, int]) -> float:
    tot = sum(int(v) for v in counts.values())
    if tot == 0:
        return float("nan")
    ones = sum(int(v) for k, v in counts.items() if str(k).strip().endswith("1"))
    return ones / tot


def se(p: float, n: int) -> float:
    return math.sqrt(max(p * (1 - p), 0.0) / n) if n else float("nan")


def half_life_us(points: list[tuple[float, float]]) -> float | None:
    """Crude time constant: the delay at which P(1) has fallen halfway from its first to its
    last value, by linear interpolation. None when there is no fall. The fitted exponential
    belongs in the writeup, not in the runner."""
    pts = sorted(points)
    if len(pts) < 2:
        return None
    p0, pinf = pts[0][1], pts[-1][1]
    if not (p0 > pinf):
        return None
    mid = (p0 + pinf) / 2
    for (t_a, p_a), (t_b, p_b) in pairwise(pts):
        if p_a >= mid >= p_b:
            if p_a == p_b:
                return t_a
            return t_a + (p_a - mid) * (t_b - t_a) / (p_a - p_b)
    return None


Runner = Callable[[str, int], dict[str, Any]]  # (quil_text, shots) -> {"counts": {...}, "billed": {...}, ...}


def run_decay(
    runner: Runner,
    delays_us: tuple[float, ...] = DEFAULT_DELAYS_US,
    shots: int = 500,
    arms: tuple[str, ...] = ARMS,
    qubit: int = 0,
    max_credits_total: float = 800.0,
    abort_delta: float = 0.10,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Execute the protocol through `runner` (injected so the logic is testable at $0).
    Returns the full record: every job's counts, P(1) ± SE, billed credits, the abort
    decision, per-arm half-lives, and the total spend."""
    say = log or (lambda _m: None)
    delays = tuple(sorted({float(d) for d in delays_us}))
    if 0.0 not in delays:
        delays = (0.0, *delays)
    longest = delays[-1]
    jobs: list[dict[str, Any]] = []
    spent = 0.0

    def one(arm: str, d_us: float) -> dict[str, Any]:
        nonlocal spent
        quil = program(arm, d_us * 1e-6, qubit)
        t0 = time.time()
        out = runner(quil, shots)
        counts = out.get("counts") or {}
        billed = out.get("billed") or {}
        cost = float(billed.get("cost") or 0.0)
        spent += cost
        p = p_one(counts)
        rec = {"arm": arm, "delay_us": d_us, "shots": shots, "counts": counts, "p1": p, "se": se(p, shots),
               "credits": cost, "exec_ms": (billed.get("timeStamps") or {}).get("executionDuration")
               if isinstance(billed.get("timeStamps"), dict) else None,
               "job_id": out.get("job_id"), "wall_s": round(time.time() - t0, 1), "quil": quil}
        jobs.append(rec)
        say(f"{arm:6s} {d_us:7.1f} us  P(1)={p:.3f}±{rec['se']:.3f}  credits={cost:.2f}  total={spent:.2f}")
        return rec

    # 1. abort check: t1 at 0 and at the longest delay, before anything else
    a0 = one("t1", 0.0)
    a1 = one("t1", longest)
    delta = a0["p1"] - a1["p1"]
    aborted = not (delta > abort_delta)
    record: dict[str, Any] = {
        "protocol": "ADR-0002 controlled-delay", "qubit": qubit, "shots": shots, "delays_us": list(delays),
        "arms": list(arms), "abort_check": {"p1_at_0": a0["p1"], "p1_at_longest": a1["p1"], "delta": delta,
                                             "threshold": abort_delta, "aborted": aborted},
    }
    if aborted:
        say(f"ABORT: P(1) at 0 us = {a0['p1']:.3f} vs {longest:g} us = {a1['p1']:.3f} (Δ {delta:.3f} ≤ {abort_delta}); "
            "delays are not being executed on this route")
        record.update(jobs=jobs, credits_total=spent, status="aborted-delays-not-executed")
        return record

    # 2. the sweep, cheapest points first so a cap stop still leaves a curve
    stopped = None
    for d_us in delays:
        for arm in arms:
            if arm == "t1" and d_us in (0.0, longest):
                continue  # already measured by the abort check
            if spent >= max_credits_total:
                stopped = f"credit cap {max_credits_total} reached after {len(jobs)} jobs"
                break
            one(arm, d_us)
        if stopped:
            break

    # 3. summary
    curves: dict[str, list[tuple[float, float]]] = {a: [] for a in arms}
    for j in jobs:
        if j["arm"] in curves and not math.isnan(j["p1"]):
            curves[j["arm"]].append((j["delay_us"], j["p1"]))
    summary = {}
    for arm, pts in curves.items():
        pts = sorted(pts)
        if arm == "echo":  # echo ideally returns to |0>; report the rise of P(1) instead
            hl = half_life_us([(t, 1 - p) for t, p in pts])
        else:
            hl = half_life_us(pts)
        summary[arm] = {"points": pts, "half_life_us": hl}
    record.update(jobs=jobs, curves=summary, credits_total=round(spent, 3),
                  status=stopped or "complete")
    say(f"done: {len(jobs)} jobs, {spent:.2f} credits (${spent / 100:.2f}); {record['status']}")
    return record


def rank_qubits(
    runner: Runner,
    qubits: Sequence[int],
    delay_us: float = 20.0,
    shots: int = 300,
    arm: str = "t1",
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Calibration-aware qubit choice: run one ``arm`` point at ``delay_us`` on
    each candidate qubit and rank them by retained P(1). The best qubits that
    day go to ``quantum_recall(layout=…)``. One job per qubit; on the native
    Rigetti route each is tens of milliseconds, cents apiece.

    This is the poor man's version of learned calibration (Google's RL
    calibration of Willow from syndrome data, Nature 2026): not a model, a
    measurement taken right before the run it informs, and recorded with it.
    """
    say = log or (lambda _m: None)
    rows = []
    for q in qubits:
        out = runner(program(arm, delay_us * 1e-6, int(q)), shots)
        counts = out.get("counts") or {}
        p = p_one(counts)
        billed = out.get("billed") or {}
        rows.append({"qubit": int(q), "p1": p, "se": se(p, shots), "credits": float(billed.get("cost") or 0.0),
                     "job_id": out.get("job_id")})
        say(f"qubit {q:3d}  P(1) at {delay_us:g} us = {p:.3f} ± {rows[-1]['se']:.3f}")
    ranked = sorted(rows, key=lambda r: r["p1"], reverse=True)
    return {"protocol": "calibrate", "arm": arm, "delay_us": delay_us, "shots": shots,
            "ranked": ranked, "best": [r["qubit"] for r in ranked],
            "credits_total": round(sum(r["credits"] for r in rows), 4)}


def ledger_row(record: dict[str, Any], device: str, row_no: str = "n") -> str:
    """One row in bench/LEDGER.md's table: | # | date | benchmark | device | shots | metric | sim | hardware | cost |"""
    c = record.get("curves", {})
    hl = " / ".join(f"{a} {v['half_life_us']:.1f} us" if v.get("half_life_us") is not None else f"{a} —"
                    for a, v in c.items())
    ab = record["abort_check"]
    n_jobs = len(record.get("jobs", []))
    return (f"| {row_no} | {time.strftime('%Y-%m-%d')} | decay (ADR-0002): t1 / ramsey / echo | `{device}` | "
            f"{n_jobs} × {record['shots']} | half-way delay (P(1) mid-point); abort Δ(0→{record['delays_us'][-1]:g} us) "
            f"= {ab['delta']:.2f} | n/a (no simulator executes DELAY) | **{hl or '—'}** ({record['status']}) | "
            f"{record['credits_total']:.1f} cr ≈ ${record['credits_total'] / 100:.2f} |")


def save(record: dict[str, Any], out_dir: Path, device: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"decay-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    p.write_text(json.dumps({"device": device, **record}, indent=1), encoding="utf-8")
    ledger = out_dir / "LEDGER.md"
    if ledger.exists():
        with ledger.open("a", encoding="utf-8") as f:
            f.write(ledger_row(record, device) + "\n")
    return p
