"""T2.2 — recall-correspondence benchmark harness.

Runs a ``kannaka-recall-bench/1`` scenario corpus (exported by kannaka-memory,
T2.1) through :func:`core.quantum_recall` and measures the *agreement rate*: how
often the quantum top pick (recall by amplitude amplification) equals the
classical argmax. On a noiseless simulator the two should agree ~always — the
whole point is that recall runs *as a quantum circuit* — so a drop is a
regression (a broken oracle, a flipped endianness, a bad diffuser).

The harness is backend-agnostic: it calls ``core.quantum_recall`` with whatever
``device`` is given. ``local:statevector`` (the default) is hermetic and free;
``qbraid:qbraid:sim:qir-sv`` is the hosted free simulator used by CI when a
qBraid key is available. A committed baseline plus a regression threshold turn
the harness into a CI gate (see ``.github/workflows/bench.yml``).
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import core

#: Corpus wire format this harness consumes (produced by kannaka-memory T2.1).
CORPUS_FORMAT = "kannaka-recall-bench/1"
#: Result / baseline wire formats this harness emits.
RESULT_FORMAT = "kannaka-recall-bench-result/1"
BASELINE_FORMAT = "kannaka-recall-bench-baseline/1"
#: A run regresses if the agreement rate drops more than this many points below
#: the committed baseline.
DEFAULT_REGRESSION_POINTS = 2.0

#: A recall backend: ``quantum_recall``-compatible callable. Injected in tests.
RecallFn = Callable[..., dict]


class BenchError(Exception):
    """A corpus is malformed or a baseline is missing/invalid."""


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def load_corpus(path: str | Path) -> dict[str, Any]:
    """Load and validate a ``kannaka-recall-bench/1`` corpus file."""
    p = Path(path)
    if not p.exists():
        raise BenchError(f"corpus not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BenchError(f"corpus is not valid JSON: {exc}") from exc
    fmt = data.get("format")
    if fmt != CORPUS_FORMAT:
        raise BenchError(f"unsupported corpus format {fmt!r}; expected {CORPUS_FORMAT!r}")
    if not isinstance(data.get("scenarios"), list):
        raise BenchError("corpus has no 'scenarios' list")
    return data


def _scenario_candidates(scn: dict[str, Any]) -> tuple[list[float], list[str]]:
    """Extract (amplitudes, labels) from one scenario's candidate list."""
    cands = scn.get("candidates") or []
    amplitudes: list[float] = []
    labels: list[str] = []
    for c in cands:
        amplitudes.append(float(c["amplitude"]))
        labels.append(str(c["label"]))
    return amplitudes, labels


def run_bench(
    corpus: dict[str, Any],
    *,
    device: str = core.LOCAL_DEVICE,
    shots: int = 1024,
    amplify: bool = True,
    limit: int | None = None,
    allow_spend: bool = False,
    max_credits: float | None = None,
    subcategory: str | None = None,
    recall_fn: RecallFn | None = None,
    layout: list[int] | None = None,
    readout_cal: list[tuple[float, float]] | None = None,
) -> dict[str, Any]:
    """Run every scenario through recall and aggregate the agreement rate.

    Args:
        corpus: A parsed ``kannaka-recall-bench/1`` document.
        device: Backend device id (default: local state-vector).
        shots: Shots per scenario.
        amplify: Whether to run amplitude amplification.
        recall_fn: Recall backend (defaults to ``core.quantum_recall``); injected
            in tests so the aggregation logic is exercised without a circuit.

    Returns:
        A ``kannaka-recall-bench-result/1`` document.
    """
    recall = recall_fn or core.quantum_recall
    scenarios = corpus.get("scenarios", [])
    if limit is not None:
        scenarios = scenarios[: max(0, int(limit))]

    per_scenario: list[dict[str, Any]] = []
    agreements = 0
    scored = 0
    skipped = 0
    argmax_mismatches = 0

    for i, scn in enumerate(scenarios):
        amplitudes, labels = _scenario_candidates(scn)
        if len(amplitudes) < 1:
            skipped += 1
            continue

        extra: dict[str, Any] = {}
        if layout is not None:
            extra["layout"] = layout
        if readout_cal is not None:
            extra["readout_cal"] = readout_cal
        res = recall(
            amplitudes,
            labels=labels,
            shots=shots,
            amplify=amplify,
            device=device,
            allow_spend=allow_spend,
            max_credits=max_credits,
            subcategory=subcategory,
            **extra,
        )
        agree = bool(res.get("agree"))
        scored += 1
        if agree:
            agreements += 1

        # Corpus-integrity check: recall's own classical argmax (over the same
        # amplitudes) should point at the same candidate the exporter recorded.
        corpus_argmax = scn.get("classical_argmax")
        recall_classical = res.get("classical_top")
        expected_label = None
        if isinstance(corpus_argmax, int) and 0 <= corpus_argmax < len(labels):
            expected_label = labels[corpus_argmax]
        argmax_ok = expected_label is None or recall_classical == expected_label
        if not argmax_ok:
            argmax_mismatches += 1

        per_scenario.append(
            {
                "index": i,
                "query_label": scn.get("query_label"),
                "hemisphere": scn.get("hemisphere"),
                "candidates": res.get("candidates"),
                "qubits": res.get("qubits"),
                "iterations": res.get("iterations"),
                "amplified": res.get("amplified"),
                "quantum_top": res.get("quantum_top"),
                "classical_top": recall_classical,
                "corpus_classical_argmax": corpus_argmax,
                "argmax_matches_corpus": argmax_ok,
                "agree": agree,
                "distribution": res.get("distribution"),
            }
        )

    agreement_rate = round(100.0 * agreements / scored, 4) if scored else 0.0
    return {
        "format": RESULT_FORMAT,
        "generated_at": _utcnow(),
        "corpus_format": corpus.get("format"),
        "device": device,
        "shots": shots,
        "amplify": amplify,
        "limit": limit,
        "scenarios_total": len(scenarios),
        "scenarios_scored": scored,
        "scenarios_skipped": skipped,
        "agreements": agreements,
        "agreement_rate": agreement_rate,
        "argmax_mismatches": argmax_mismatches,
        "layout": layout,
        "readout_mitigated": readout_cal is not None,
        "scenarios": per_scenario,
    }


def evaluate_regression(
    current_rate: float,
    baseline_rate: float,
    threshold: float = DEFAULT_REGRESSION_POINTS,
) -> dict[str, Any]:
    """Compare a run's agreement rate to the baseline.

    A drop of more than ``threshold`` points is a regression. Improvements and
    small dips within the threshold pass.
    """
    drop = round(baseline_rate - current_rate, 4)
    return {
        "baseline_rate": baseline_rate,
        "current_rate": current_rate,
        "drop": drop,
        "threshold": threshold,
        "regressed": drop > threshold,
    }


def _baseline_from_result(result: dict[str, Any], corpus_path: str) -> dict[str, Any]:
    return {
        "format": BASELINE_FORMAT,
        "generated_at": result["generated_at"],
        "device": result["device"],
        "shots": result["shots"],
        "scenarios_scored": result["scenarios_scored"],
        "agreement_rate": result["agreement_rate"],
        "corpus": corpus_path,
        "note": (
            "Ideal-simulator agreement ceiling for the recall<->amplitude-"
            "amplification correspondence. Real-hardware runs (T2.3) sit below "
            "this due to noise. Regenerate: kannaka-quantum bench --scenarios "
            f"{corpus_path} --baseline <this file> --update-baseline."
        ),
    }


def _load_baseline(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise BenchError(f"baseline not found: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if "agreement_rate" not in data:
        raise BenchError(f"baseline {p} has no 'agreement_rate'")
    return data


def _write_json(path: str | Path, doc: dict[str, Any]) -> None:
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def bench_command(
    *,
    scenarios: str,
    device: str = core.LOCAL_DEVICE,
    shots: int = 1024,
    amplify: bool = True,
    limit: int | None = None,
    allow_spend: bool = False,
    max_credits: float | None = None,
    subcategory: str | None = None,
    out: str | None = None,
    baseline: str | None = None,
    regression_threshold: float = DEFAULT_REGRESSION_POINTS,
    update_baseline: bool = False,
    recall_fn: RecallFn | None = None,
    canary: bool = True,
    canary_fn: Callable[..., dict[str, Any]] | None = None,
    layout: list[int] | None = None,
    readout_cal: list[tuple[float, float]] | None = None,
) -> tuple[dict[str, Any], int]:
    """Orchestrate a benchmark run for the CLI.

    Returns ``(result_document, exit_code)``. Exit code is 1 iff a baseline was
    given (and not being updated) and the run regressed past the threshold — so
    the CI gate fails the PR — **or** the bit-order canary failed, which means
    the device's bitstrings no longer decode the way the bridge assumes and
    every agreement number after it would be wrong. ``--update-baseline``
    rewrites the baseline and passes the regression gate, never the canary.
    """
    corpus = load_corpus(scenarios)
    canary_result = None
    if canary:
        run_canary = canary_fn or core.bit_order_canary
        canary_result = run_canary(
            device=device, shots=shots, allow_spend=allow_spend, max_credits=max_credits, subcategory=subcategory
        )
    result = run_bench(
        corpus,
        device=device,
        shots=shots,
        amplify=amplify,
        limit=limit,
        allow_spend=allow_spend,
        max_credits=max_credits,
        subcategory=subcategory,
        recall_fn=recall_fn,
        layout=layout,
        readout_cal=readout_cal,
    )
    result["bit_order_canary"] = canary_result

    if out:
        _write_json(out, result)

    if canary_result is not None and not canary_result.get("ok"):
        result["canary_failed"] = True
        return result, 1

    if update_baseline:
        if not baseline:
            raise BenchError("--update-baseline requires --baseline <path>")
        _write_json(baseline, _baseline_from_result(result, scenarios))
        result["baseline_written"] = baseline
        return result, 0

    if baseline:
        base = _load_baseline(baseline)
        reg = evaluate_regression(result["agreement_rate"], float(base["agreement_rate"]), regression_threshold)
        result["regression"] = reg
        return result, (1 if reg["regressed"] else 0)

    return result, 0
