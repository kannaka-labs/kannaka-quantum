"""Core quantum operations for Kannaka, executed on real quantum backends via
qBraid.

Kannaka's memory is a *Holographic Resonance Medium*: recall is wave
interference, and "attention acts as gravity — wavefronts whose phase/amplitude
align with the query are pulled forward." That is, almost verbatim, the
definition of quantum amplitude amplification. This module makes the
correspondence literal:

- ``run_qasm`` / ``run_qiskit`` — execute arbitrary circuits on qBraid devices
  (free simulator by default; real QPUs when the account has credits).
- ``qrng`` — true quantum random bits (the medium's irrationality, Ξ, drawn
  from measurement collapse rather than a PRNG).
- ``quantum_recall`` — amplitude-encode a set of memory resonances into a
  quantum state and (optionally) amplitude-amplify toward the strongest, so
  recall is performed *by interference* on a quantum computer.

Auth: a qBraid API key from ``QBRAID_API_KEY``, the saved ``~/.qbraid/qbraidrc``
(``QbraidProvider.save_config()``), or — as a convenience on this workstation —
``~/Downloads/QBraid.txt``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

#: qBraid's native state-vector simulator — free, 30 qubits, no credits needed.
DEFAULT_DEVICE = "qbraid:qbraid:sim:qir-sv"
SIM_QUBIT_CAP = 28

#: A fully local, credentials-free state-vector backend (qiskit only — no qBraid
#: account, no network, $0). Circuits whose device id starts with ``local:`` are
#: evaluated here instead of being submitted to a provider. Same circuit build
#: and same measured-index decode as the hosted simulator, so the recall
#: correspondence can be exercised hermetically (used by the benchmark harness
#: and CI). It is NOT a valid entropy source — ``qrng``/``harvest`` still refuse
#: simulators for the reservoir.
LOCAL_PREFIX = "local:"
LOCAL_DEVICE = "local:statevector"

#: Devices whose id starts with this route to OpenQuantum (Quantum Rings) instead
#: of qBraid. Form: ``openquantum:<backend short_code>`` e.g. ``openquantum:iqm:garnet``.
OPENQUANTUM_PREFIX = "openquantum:"

#: OpenQuantum has NO free simulator — every job spends real "Spark" credits
#: (1 credit = $2; the free tier is 25 credits / $50 per 90 days). These are the
#: documented public-compute per-shot USD prices, used for a *pre-flight* cost
#: estimate so a careless large-shot run on a pricey QPU can't silently drain the
#: budget (e.g. 1024 shots on IonQ ≈ $49 = the whole free tier). The real charge
#: is the device's live quote; this table is the guard rail, not the invoice.
OQ_USD_PER_SHOT = {
    "ionq:forte-1": 0.04800,
    "aqt:ibex-q1": 0.01410,
    "iqm:emerald": 0.00096,
    "iqm:garnet": 0.00087,
    "rigetti:cepheus-1-108q": 0.000255,
}
OQ_USD_PER_CREDIT = 2.0
#: Default ceiling for a single OpenQuantum run, in credits (≈ $2). Raise per-call
#: (``max_credits`` / ``--max-credits``) or via ``OPENQUANTUM_MAX_CREDITS``.
OQ_DEFAULT_MAX_CREDITS = 1.0
#: Required workload tag on every OpenQuantum job. Override with the subcategory
#: arg / ``OPENQUANTUM_SUBCATEGORY``; we learn the canonical value from the first
#: real submission's error if this default is rejected.
OQ_DEFAULT_SUBCATEGORY = "phys:oth"

#: qBraid credits are $0.01 each; real QPUs expose live per-task/per-shot/per-minute
#: pricing in device metadata. Default ceiling 200 credits (≈ $2), matching the
#: OpenQuantum default. Per-minute-billed devices (e.g. native Rigetti at 12000
#: credits/min ≈ $120/min) are refused unless the caller bounds wall-clock time
#: with max_seconds (ADR-0002); they bill actual execution time, prorated.
QBRAID_USD_PER_CREDIT = 0.01
QBRAID_DEFAULT_MAX_CREDITS = 200.0


def _resolve_api_key() -> str | None:
    key = os.environ.get("QBRAID_API_KEY")
    if key:
        return key.strip()
    # Fallback for this workstation's setup.
    dl = Path.home() / "Downloads" / "QBraid.txt"
    if dl.exists():
        m = re.search(r"qbr_[A-Za-z0-9_\-]+", dl.read_text())
        if m:
            return m.group(0)
    return None


def _provider():
    from qbraid.runtime import QbraidProvider

    key = _resolve_api_key()
    # If a key is found we pass it; otherwise rely on a saved qbraidrc.
    return QbraidProvider(api_key=key) if key else QbraidProvider()


# ---------------------------------------------------------------------------
# OpenQuantum (Quantum Rings) — a second backend provider. Real QPUs (IonQ,
# Rigetti, IQM, AQT) behind an OAuth2 client-credentials API. No free simulator,
# so every run is gated behind an explicit spend opt-in + a credit ceiling.
# ---------------------------------------------------------------------------


def _oq_credentials():
    """Resolve OpenQuantum SDK credentials (client_id + client_secret).

    Order: ``OPENQUANTUM_CLIENT_ID``/``_SECRET`` env, then a JSON sdk-key at
    ``OPENQUANTUM_SDK_KEY``, then ``~/.openquantum/sdk-key.json``, then — a
    workstation convenience — the downloaded ``~/Downloads/sdk-key-*.json``.
    Returns a ``ClientCredentials`` or ``None``.
    """
    import json

    from openquantum_sdk.auth import ClientCredentials

    cid = os.environ.get("OPENQUANTUM_CLIENT_ID")
    csec = os.environ.get("OPENQUANTUM_CLIENT_SECRET")
    if cid and csec:
        return ClientCredentials(cid.strip(), csec.strip())

    candidates = [os.environ.get("OPENQUANTUM_SDK_KEY"), str(Path.home() / ".openquantum" / "sdk-key.json")]
    candidates += [str(p) for p in sorted((Path.home() / "Downloads").glob("sdk-key-*.json"))]
    for cand in candidates:
        if not cand:
            continue
        p = Path(cand)
        if not p.exists():
            continue
        try:
            d = json.loads(p.read_text())
        except Exception:  # noqa: BLE001, S112 - skip malformed/failed entry, keep processing the rest
            continue
        if d.get("client_id") and d.get("client_secret"):
            return ClientCredentials(d["client_id"], d["client_secret"])
    return None


def _oq_clients():
    """A (SchedulerClient, ManagementClient) pair sharing one auth/token."""
    from openquantum_sdk import ManagementClient, SchedulerClient
    from openquantum_sdk.clients import ClientCredentialsAuth

    creds = _oq_credentials()
    if creds is None:
        raise RuntimeError(
            "OpenQuantum credentials not found. Set OPENQUANTUM_CLIENT_ID/"
            "OPENQUANTUM_CLIENT_SECRET, or OPENQUANTUM_SDK_KEY to the sdk-key JSON "
            "path, or place it at ~/.openquantum/sdk-key.json."
        )
    auth = ClientCredentialsAuth(creds)
    mgmt = ManagementClient(auth=auth)
    sched = SchedulerClient(auth=auth, management_client=mgmt)
    return sched, mgmt


def _oq_page(paginated, *attrs) -> list:
    """Pull the item list out of an SDK paginated response across field names."""
    for a in attrs:
        v = getattr(paginated, a, None)
        if v is not None:
            return list(v)
    try:
        return list(paginated)
    except Exception:  # noqa: BLE001 - best-effort probe; falls back to a safe default
        return []


def _oq_backends(mgmt) -> list:
    return _oq_page(mgmt.list_backend_classes(limit=50), "backend_classes", "data", "items", "results")


def _oq_org_id(mgmt) -> str | None:
    orgs = _oq_page(mgmt.list_user_organizations(limit=20), "organizations", "data", "items")
    return getattr(orgs[0], "id", None) if orgs else None


def _oq_backend_code(b) -> str | None:
    """The short backend id (e.g. ``iqm:garnet``) used in device strings."""
    return getattr(b, "short_code", None) or getattr(b, "name", None) or getattr(b, "id", None)


def _oq_estimate_cost(device: str, shots: int, max_credits: float | None, allow_spend: bool) -> dict | None:
    """Gate an OpenQuantum run on (1) an explicit spend opt-in and (2) a credit
    ceiling, using the documented per-shot price as a pre-flight estimate.

    Raises ``RuntimeError`` if not opted in or if the estimate exceeds the cap.
    Returns the estimate dict (or ``None`` for an unpriced device the caller
    explicitly accepted via ``max_credits``).
    """
    if not (allow_spend or os.environ.get("KANNAKA_QUANTUM_ALLOW_SPEND") == "1"):
        raise RuntimeError(
            "OpenQuantum runs spend real Spark credits — there is no free simulator. "
            "Re-run with allow_spend=True (CLI: --allow-spend) or set "
            "KANNAKA_QUANTUM_ALLOW_SPEND=1. Use the free qBraid simulator "
            f"({DEFAULT_DEVICE}) for $0 testing."
        )
    cap = max_credits if max_credits is not None else float(os.environ.get("OPENQUANTUM_MAX_CREDITS", OQ_DEFAULT_MAX_CREDITS))
    code = device[len(OPENQUANTUM_PREFIX):]
    price = OQ_USD_PER_SHOT.get(code)
    if price is None:
        if max_credits is None:
            raise RuntimeError(
                f"unknown per-shot price for '{device}' — pass max_credits=<credits> to "
                "acknowledge a run whose cost can't be pre-estimated."
            )
        return None  # caller accepted an unpriced device by giving an explicit cap
    est_usd = price * int(shots)
    est_credits = est_usd / OQ_USD_PER_CREDIT
    if est_credits > cap:
        raise RuntimeError(
            f"estimated {est_credits:.3f} credits (${est_usd:.2f}) for {shots} shots on "
            f"{device} exceeds the {cap}-credit cap — lower shots or raise max_credits."
        )
    return {"per_shot_usd": price, "est_usd": round(est_usd, 4), "est_credits": round(est_credits, 4)}


def _oq_counts(output: Any) -> dict[str, int]:
    """Best-effort {bitstring: count} extraction from download_job_output."""
    if isinstance(output, dict):
        for key in ("counts", "measurement_counts", "histogram", "meas"):
            c = output.get(key)
            if isinstance(c, dict) and c:
                return {str(k): int(v) for k, v in c.items()}
        # A bare {bitstring: int} dict.
        if output and all(isinstance(v, int) for v in output.values()):
            return {str(k): int(v) for k, v in output.items()}
    for getter in (lambda o: o.get_counts(), lambda o: o.counts, lambda o: o.data.meas.get_counts()):
        try:
            c = getter(output)
            if c:
                return {str(k): int(v) for k, v in dict(c).items()}
        except Exception:  # noqa: BLE001, S112 - skip malformed/failed entry, keep processing the rest
            continue
    return {}


def _run_openquantum(
    qasm: str,
    device: str,
    shots: int,
    allow_spend: bool = False,
    max_credits: float | None = None,
    subcategory: str | None = None,
) -> dict[str, Any]:
    """Submit an OpenQASM program to an OpenQuantum QPU and return counts.

    Spends real Spark credits — gated by :func:`_oq_estimate_cost`.
    """
    from openquantum_sdk.clients import JobSubmissionConfig

    estimate = _oq_estimate_cost(device, shots, max_credits, allow_spend)
    sched, mgmt = _oq_clients()
    backend_class_id = device[len(OPENQUANTUM_PREFIX):]
    subcat = subcategory or os.environ.get("OPENQUANTUM_SUBCATEGORY") or OQ_DEFAULT_SUBCATEGORY
    cfg = JobSubmissionConfig(
        backend_class_id=backend_class_id,
        name="kannaka-quantum",
        job_subcategory_id=subcat,
        shots=int(shots),
        organization_id=_oq_org_id(mgmt),
        auto_approve_quote=True,
    )
    job = sched.submit_job(cfg, file_content=qasm.encode("utf-8"))
    output = sched.download_job_output(job)
    counts = _oq_counts(output)
    result = {
        "device": device,
        "shots": shots,
        "job_id": getattr(job, "id", None),
        "counts": counts,
        "provider": "openquantum",
        "backend": backend_class_id,
        "cost_estimate": estimate,
    }
    if not counts:  # surface the raw shape so we can tighten _oq_counts after a first run
        result["raw_output"] = str(output)[:2000]
    return result


def list_devices(online_only: bool = False, include_openquantum: bool = True) -> list[dict[str, Any]]:
    """List quantum devices across providers with status and qubit counts.

    qBraid's ``qbraid:qbraid:sim:qir-sv`` simulator is free (no credits).
    OpenQuantum entries (``openquantum:*``) are real QPUs that spend Spark
    credits — there is no free OpenQuantum simulator.
    """
    devices: list[dict[str, Any]] = []

    # qBraid fleet (free simulator + QPUs). Wrapped so an OpenQuantum-only setup
    # (or a missing qBraid key) still returns a usable list.
    try:
        provider = _provider()
        for dev in provider.get_devices():
            try:
                md = dev.metadata()
            except Exception:  # pragma: no cover - network/SDK variance  # noqa: BLE001 - best-effort probe; falls back to a safe default
                md = {}
            did = md.get("device_id") or getattr(dev, "id", None)
            status = str(md.get("status") or "")
            is_sim = "sim" in str(did).lower()
            devices.append(
                {
                    "id": did,
                    "qubits": md.get("num_qubits"),
                    "status": status.split(".")[-1] or "UNKNOWN",
                    "simulator": is_sim,
                    "provider": str(did).split(":")[1] if did and ":" in did else None,
                    "cost": "free" if is_sim else "qbraid-credits",
                }
            )
    except Exception as e:  # pragma: no cover - report but don't fail the listing  # noqa: BLE001 - boundary: failure is surfaced in structured output
        devices.append({"id": None, "provider": "qbraid", "status": "ERROR", "error": str(e)})

    # OpenQuantum fleet (real QPUs; spends Spark credits). Best-effort; skipped
    # silently when no OpenQuantum credentials are configured.
    if include_openquantum and _oq_credentials() is not None:
        try:
            _, mgmt = _oq_clients()
            for b in _oq_backends(mgmt):
                code = _oq_backend_code(b)
                status = str(getattr(b, "status", "") or "")
                accepting = getattr(b, "accepting_jobs", None)
                price = OQ_USD_PER_SHOT.get(code or "")
                devices.append(
                    {
                        "id": f"{OPENQUANTUM_PREFIX}{code}",
                        "qubits": getattr(b, "num_qubits", None) or getattr(b, "qubits", None),
                        # Normalize to qBraid's casing so the online_only filter is uniform.
                        "status": (status.split(".")[-1] or ("ONLINE" if accepting else "UNKNOWN")).upper(),
                        "simulator": False,
                        "provider": "openquantum",
                        "cost": (f"${price}/shot" if price is not None else "spark-credits"),
                    }
                )
        except Exception as e:  # pragma: no cover  # noqa: BLE001 - boundary: failure is surfaced in structured output
            devices.append({"id": None, "provider": "openquantum", "status": "ERROR", "error": str(e)})

    if online_only:
        devices = [d for d in devices if d.get("status") == "ONLINE"]
    devices.sort(key=lambda d: (not d.get("simulator"), d.get("provider") or "", d.get("id") or ""))
    return devices


def _counts_from_result(res: Any) -> dict[str, int]:
    """Pull a {bitstring: count} dict out of a qBraid Result across SDK shapes."""
    for getter in (
        lambda r: r.data.get_counts(),
        lambda r: r.measurement_counts(),
        lambda r: r.get_counts(),
        lambda r: r.data.measurement_counts,
    ):
        try:
            c = getter(res)
            if c:
                return {str(k): int(v) for k, v in dict(c).items()}
        except Exception:  # noqa: BLE001, S112 - skip malformed/failed entry, keep processing the rest
            continue
    return {}


def _qbraid_quote(device: str, shots: int) -> dict[str, Any] | None:
    """Ask qBraid what a job would cost, for devices whose metadata carries no
    pricing.

    ``dev.metadata()["pricing"]`` is empty for some real QPUs — the direct
    Rigetti path among them — and multiplying absent per-shot prices gives
    0.0 credits, which sails under any ceiling. qbraid-core 0.6.4 added a
    server-side estimate that aggregates the device's execution history, so
    ask it rather than guess.

    Returns None whenever no usable number comes back: the package is too old
    or absent, the call fails, or the device simply cannot be quoted. The
    caller must treat None as "unknown", never as "free".

    Two details from qBraid, both load-bearing:

    * A device that cannot be quoted returns ``pricingAvailable=False`` with a
      ``reason`` — it does NOT raise. Branch on the flag, or an unquotable
      device looks like a successful quote of whatever ``estimatedCost``
      happens to hold.
    * Omitting ``shots`` quotes 1000 rather than quoting shot-independently,
      so it is always passed explicitly here.

    The estimate is deliberately biased high (aggregated with a buffer; about
    95% of jobs land under it) and nothing enforces it, so it is a working
    ceiling rather than a guarantee. A deep or dense circuit can still exceed
    it.
    """
    try:
        from qbraid_core.services.runtime import QuantumRuntimeClient  # type: ignore
    except ImportError:
        # qbraid-core absent, or older than 0.6.4 where estimate_cost landed.
        return None
    try:
        quote = QuantumRuntimeClient().estimate_cost(device, shots=int(shots))
    except Exception:  # noqa: BLE001 - deliberate: see below
        # Every failure mode of a third-party network client — auth, transport,
        # a schema change, a raise where a flag was promised — must read as
        # "price unknown" so the caller refuses. Narrowing this would let an
        # unanticipated error escape and abort the run, or worse, be caught
        # upstream and treated as free. Unknown is the safe answer here.
        return None
    if not getattr(quote, "pricingAvailable", False):
        return {"unavailable_reason": str(getattr(quote, "reason", "") or "no reason given")}
    try:
        return {"est_credits": float(quote.estimatedCost)}
    except (AttributeError, TypeError, ValueError):
        return None


def _qbraid_spend_guard(
    pricing: dict, device: str, shots: int, allow_spend: bool, max_credits: float | None,
    max_seconds: float | None = None,
) -> dict[str, Any]:
    """Gate a real (non-simulator) qBraid run on an explicit spend opt-in + a
    credit ceiling, using the device's live pricing metadata.

    Per-minute-billed devices (the native Rigetti path, ~12000 credits/min) are
    refused unless the caller also passes ``max_seconds``: the accepted
    wall-clock bound. The ceiling ``per_min * max_seconds / 60`` must fit under
    the credit cap (ADR-0002). The device bills actual execution time, prorated,
    so the ceiling is the bound you accept, not the expected cost — a 1,000-shot
    job runs tens to hundreds of milliseconds. Raises on opt-out / over-cap;
    returns the estimate otherwise.
    """
    if not (allow_spend or os.environ.get("KANNAKA_QUANTUM_ALLOW_SPEND") == "1"):
        raise RuntimeError(
            f"{device} is a real qBraid QPU and spends qBraid credits. Re-run with "
            f"allow_spend=True (CLI: --allow-spend) or set KANNAKA_QUANTUM_ALLOW_SPEND=1. "
            f"Use the free simulator ({DEFAULT_DEVICE}) for $0 testing."
        )
    per_task = float(pricing.get("perTask") or 0.0)
    per_shot = float(pricing.get("perShot") or 0.0)
    per_min = float(pricing.get("perMinute") or 0.0)
    cap = max_credits if max_credits is not None else float(
        os.environ.get("QBRAID_MAX_CREDITS", QBRAID_DEFAULT_MAX_CREDITS)
    )
    if per_min > 0:
        if max_seconds is None or float(max_seconds) <= 0:
            raise RuntimeError(
                f"{device} bills per-minute ({per_min:g} credits/min ≈ ${per_min * QBRAID_USD_PER_CREDIT:.0f}/min) — "
                "cost cannot be bounded from a shot count. Pass max_seconds=<s> (CLI: --max-seconds) to accept "
                "a wall-clock ceiling of per_min*max_seconds/60 credits (ADR-0002), or choose a per-shot device "
                "(e.g. aws:rigetti:qpu:cepheus-1-108q or openquantum:rigetti:cepheus-1-108q)."
            )
        ceiling = per_min * float(max_seconds) / 60.0 + per_task + per_shot * int(shots)
        if ceiling > cap:
            raise RuntimeError(
                f"per-minute ceiling {ceiling:.1f} credits (${ceiling * QBRAID_USD_PER_CREDIT:.2f}) for "
                f"max_seconds={max_seconds} on {device} exceeds the {cap}-credit cap — lower max_seconds or "
                "raise max_credits."
            )
        return {
            "billing": "per-minute",
            "per_minute_credits": per_min,
            "max_seconds": float(max_seconds),
            "ceiling_credits": round(ceiling, 3),
            "ceiling_usd": round(ceiling * QBRAID_USD_PER_CREDIT, 4),
            "note": "billed for actual execution time, prorated; the ceiling is the accepted bound, not the expected cost",
        }
    if per_task == 0.0 and per_shot == 0.0:
        # No pricing metadata at all. The arithmetic below would make this
        # 0.0 credits and wave a real QPU through under any ceiling — the same
        # "unknown price" case _oq_estimate_cost refuses outright. Ask qBraid
        # for a server-side quote first; only an explicitly accepted ceiling
        # gets past an unquotable device.
        quoted = _qbraid_quote(device, int(shots))
        if quoted is not None and "est_credits" in quoted:
            est_credits = quoted["est_credits"]
            est_usd = est_credits * QBRAID_USD_PER_CREDIT
            if est_credits > cap:
                raise RuntimeError(
                    f"qBraid estimates {est_credits:.2f} credits (${est_usd:.2f}) for {shots} shots on "
                    f"{device}, over the {cap}-credit cap — lower shots or raise max_credits."
                )
            return {
                "source": "qbraid.estimate_cost",
                "est_credits": round(est_credits, 3),
                "est_usd": round(est_usd, 4),
                "note": (
                    "server-side estimate, biased high (~95% of jobs land under) and NOT enforced by "
                    "qBraid — a working ceiling, not a guarantee"
                ),
            }
        if max_credits is None:
            why = (quoted or {}).get("unavailable_reason", "no pricing in device metadata and no quote available")
            raise RuntimeError(
                f"cannot price '{device}': {why}. Refusing rather than assuming free — pass "
                f"max_credits=<credits> (CLI: --max-credits) to accept a ceiling you choose."
            )
        return {
            "source": "operator-accepted",
            "est_credits": None,
            "cap_credits": cap,
            "note": "price unknown; the caller accepted an explicit ceiling instead",
        }

    est_credits = per_task + per_shot * int(shots)
    est_usd = est_credits * QBRAID_USD_PER_CREDIT
    if est_credits > cap:
        raise RuntimeError(
            f"estimated {est_credits:.2f} qBraid credits (${est_usd:.2f}) for {shots} shots on "
            f"{device} exceeds the {cap}-credit cap — lower shots or raise max_credits."
        )
    return {
        "per_task_credits": per_task,
        "per_shot_credits": per_shot,
        "est_credits": round(est_credits, 3),
        "est_usd": round(est_usd, 4),
    }


def _json_safe(v: Any) -> Any:
    """Job metadata carries datetimes (timeStamps) and Decimals; the CLI prints JSON, so a
    non-primitive value here once cost a paid result. Primitives pass, containers recurse,
    everything else becomes str()."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, dict):
        return {str(k): _json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    return str(v)


#: Braket enforces a per-circuit gate cap on IonQ devices since 2026-06-04: 2,000
#: gates on demand (5,000 reserved). A job over it is rejected after the per-task
#: fee is incurred, so it is refused here, before submission.
IONQ_GATE_CAP = 2000

_QASM_NON_GATE = ("OPENQASM", "include", "qubit", "bit", "qreg", "creg", "measure", "barrier", "//", "reset")


def count_gates(qasm3: str) -> int:
    """Count gate statements in an OpenQASM program: every non-empty statement
    that is not a header, declaration, measurement, barrier, reset or comment."""
    n = 0
    for raw in qasm3.replace(";", ";\n").splitlines():
        line = raw.strip()
        if not line or line.startswith(_QASM_NON_GATE) or line in ("{", "}") or "measure" in line:
            continue
        n += 1
    return n


def _check_gate_cap(qasm3: str, device: str) -> None:
    if "ionq" in device.lower():
        n = count_gates(qasm3)
        if n > IONQ_GATE_CAP:
            raise RuntimeError(
                f"circuit has {n} gates; IonQ devices reject more than {IONQ_GATE_CAP} per circuit "
                "on demand (Braket, 2026-06-04). Not submitting: the per-task fee would be spent on a rejection."
            )


def place_on(qc, layout: Sequence[int] | None):
    """Return ``qc`` placed on physical qubits ``layout`` (one per circuit qubit),
    as a wider circuit whose other qubits are idle. With no layout, ``qc`` itself.

    This is how a caller chooses *which* qubits a job runs on without a
    coupling map: the emitted OpenQASM declares ``max(layout) + 1`` qubits and
    only touches the chosen ones, so the device's index-based decoding is
    unchanged (the classical register is still ``qc``'s own). Used by the
    calibration-aware bench: ``calibrate`` ranks qubits by their measured
    decay, and ``--layout`` puts the recall circuit on the best of them.
    """
    if layout is None:
        return qc
    from qiskit import QuantumCircuit

    layout = [int(q) for q in layout]
    if len(layout) != qc.num_qubits:
        raise ValueError(f"layout has {len(layout)} qubits, circuit needs {qc.num_qubits}")
    if len(set(layout)) != len(layout) or min(layout) < 0:
        raise ValueError(f"layout must be distinct non-negative qubit indices, got {layout}")
    wide = QuantumCircuit(max(layout) + 1, qc.num_clbits)
    wide.compose(qc, qubits=layout, clbits=list(range(qc.num_clbits)), inplace=True)
    return wide


def readout_calibration(counts_zero: dict[str, int], counts_one: dict[str, int]) -> list[tuple[float, float]]:
    """Per-qubit readout confusion from two calibration runs, |0…0⟩ and |1…1⟩,
    in the device's own bitstring order (position i of the string is qubit
    position i here, whatever the device's convention, so no reversal is ever
    needed as long as the same device decodes the data).

    Returns, per string position, ``(p(read 1 | prepared 0), p(read 0 | prepared 1))``.
    Tensored: each qubit's readout error is taken as independent of the others,
    which is the two-job calibration a per-task fee makes affordable (the full
    2^n-state version costs 2^n tasks).
    """
    n = len(next(iter(counts_zero)))
    z = sum(counts_zero.values())
    o = sum(counts_one.values())
    cal = []
    for i in range(n):
        e0 = sum(c for b, c in counts_zero.items() if b[i] == "1") / z if z else 0.0
        e1 = sum(c for b, c in counts_one.items() if b[i] == "0") / o if o else 0.0
        cal.append((e0, e1))
    return cal


def mitigate_readout(counts: dict[str, int], cal: list[tuple[float, float]]) -> dict[str, int]:
    """Invert the tensored readout confusion on ``counts`` and return integer
    counts summing to the original shots. Negative probabilities from the
    inversion are clipped to zero before renormalising, the standard lossy
    step; a perfect calibration (all errors 0) returns ``counts`` unchanged.
    """
    import numpy as _np

    if not counts:
        return counts
    n = len(next(iter(counts)))
    if len(cal) != n:
        raise ValueError(f"calibration covers {len(cal)} qubits, counts have {n}")
    shots = sum(counts.values())
    # Per-qubit inverse confusion matrices M_i^-1 where M_i = [[1-e0, e1], [e0, 1-e1]]
    invs = []
    for e0, e1 in cal:
        m = _np.array([[1.0 - e0, e1], [e0, 1.0 - e1]])
        invs.append(_np.linalg.inv(m) if abs(_np.linalg.det(m)) > 1e-9 else _np.eye(2))
    probs: dict[str, float] = {b: c / shots for b, c in counts.items()}
    # Apply the tensor-product inverse one qubit at a time (sparse in the strings).
    for i, inv in enumerate(invs):
        nxt: dict[str, float] = {}
        for b, p in probs.items():
            for out_bit in (0, 1):
                w = inv[out_bit, int(b[i])]
                if w == 0.0:
                    continue
                nb = b[:i] + str(out_bit) + b[i + 1 :]
                nxt[nb] = nxt.get(nb, 0.0) + p * w
        probs = nxt
    clipped = {b: max(0.0, p) for b, p in probs.items()}
    total = sum(clipped.values()) or 1.0
    out = {b: round(shots * p / total) for b, p in clipped.items() if p > 0}
    return out


#: The bit-order canary: 16 amplitudes over 4 qubits whose argmax is index 1
#: (``0001``). Its bit reversal is index 8 (``1000``), a different, weak
#: candidate, so a decoder that reads the wrong end of the string names the
#: wrong memory and the bench fails. Any change in a provider's bitstring
#: convention (qBraid 0.13 flips to little-endian) trips it.
BIT_ORDER_CANARY = [0.05, 0.95, 0.05, 0.05, 0.05, 0.05, 0.05, 0.05, 0.06, 0.05, 0.05, 0.05, 0.05, 0.05, 0.05, 0.05]
BIT_ORDER_CANARY_TARGET = 1


def bit_order_canary(
    device: str = LOCAL_DEVICE,
    shots: int = 512,
    allow_spend: bool = False,
    max_credits: float | None = None,
    subcategory: str | None = None,
) -> dict[str, Any]:
    """Run the canary recall and say whether this device's bitstrings decode the
    way the bridge assumes. ``ok`` is False when the amplified peak is read at
    the bit-reversed index, which is what a flipped provider convention does."""
    labels = [f"canary-{i:02d}" for i in range(len(BIT_ORDER_CANARY))]
    res = quantum_recall(
        BIT_ORDER_CANARY, labels=labels, shots=shots, amplify=True, device=device,
        allow_spend=allow_spend, max_credits=max_credits, subcategory=subcategory,
    )
    expected = labels[BIT_ORDER_CANARY_TARGET]
    reversed_idx = int(format(BIT_ORDER_CANARY_TARGET, "04b")[::-1], 2)
    return {
        "ok": res.get("quantum_top") == expected,
        "expected": expected,
        "got": res.get("quantum_top"),
        "bit_reversed_would_be": labels[reversed_idx],
        "device": device,
        "job_id": res.get("job_id"),
    }


def run_qasm(
    qasm3: str,
    device: str = DEFAULT_DEVICE,
    shots: int = 100,
    allow_spend: bool = False,
    max_credits: float | None = None,
    subcategory: str | None = None,
    max_seconds: float | None = None,
) -> dict[str, Any]:
    """Run an OpenQASM program on a device and return measurement counts.

    Routes to OpenQuantum (real QPUs, spends Spark credits — gated) when
    ``device`` starts with ``openquantum:``; otherwise runs on qBraid (the free
    simulator by default).
    """
    _check_gate_cap(qasm3, device)
    if device.startswith(OPENQUANTUM_PREFIX):
        return _run_openquantum(
            qasm3, device, shots, allow_spend=allow_spend, max_credits=max_credits, subcategory=subcategory
        )
    provider = _provider()
    dev = provider.get_device(device)
    cost_estimate = None
    if "sim" not in device.lower():  # real qBraid QPU — gate the spend
        try:
            pricing = (dev.metadata() or {}).get("pricing") or {}
        except Exception:  # noqa: BLE001 - best-effort probe; falls back to a safe default
            pricing = {}
        cost_estimate = _qbraid_spend_guard(pricing, device, shots, allow_spend, max_credits, max_seconds)
    job = dev.run(qasm3, shots=shots)
    return _finish_qbraid_job(job, device, shots, cost_estimate)


def _qbraid_device_and_estimate(device: str, shots: int, allow_spend: bool, max_credits: float | None,
                                max_seconds: float | None):
    provider = _provider()
    dev = provider.get_device(device)
    cost_estimate = None
    if "sim" not in device.lower():  # real qBraid QPU — gate the spend
        try:
            pricing = (dev.metadata() or {}).get("pricing") or {}
        except Exception:  # noqa: BLE001 - best-effort probe; falls back to a safe default
            pricing = {}
        cost_estimate = _qbraid_spend_guard(pricing, device, shots, allow_spend, max_credits, max_seconds)
    return dev, cost_estimate


def run_quil(
    quil: str,
    device: str,
    shots: int = 100,
    allow_spend: bool = False,
    max_credits: float | None = None,
    max_seconds: float | None = None,
) -> dict[str, Any]:
    """Run Quil / Quil-T text on a qBraid Rigetti device (the ONLY route that executes
    DELAY: Braket drops it, OpenQuantum rejects it, the simulator fails it). Timing
    programs bypass quilc, so use native gates only (RX ±pi/2 ±pi, RZ, CZ, I, MEASURE)
    and 32 ns-aligned durations. Same spend guard and job handling as :func:`run_qasm`."""
    from pyquil import Program

    dev, cost_estimate = _qbraid_device_and_estimate(device, shots, allow_spend, max_credits, max_seconds)
    job = dev.run(Program(quil), shots=shots)
    return _finish_qbraid_job(job, device, shots, cost_estimate)


def _finish_qbraid_job(job, device: str, shots: int, cost_estimate) -> dict[str, Any]:
    try:
        job.wait_for_final_state(timeout=300)
    except Exception:  # noqa: BLE001, S110 - best-effort; failure here must not break the primary path
        pass
    # A FAILED job must never come back as an empty success: two delay-bearing circuits on the
    # free simulator FAILED and this path returned counts: {} (2026-09-07). Surface the status
    # and whatever the backend said, then refuse to hand back a result with no counts.
    status = None
    try:
        status = str(job.status())
    except Exception:  # noqa: BLE001 - some backends have no status(); fall through to the counts check
        status = None
    job_meta: dict[str, Any] = {}
    try:
        job_meta = dict(job.metadata() or {})
    except Exception:  # noqa: BLE001 - metadata is best-effort
        job_meta = {}
    if status and any(w in status.upper() for w in ("FAILED", "CANCEL")):
        detail = str(job_meta.get("statusText") or job_meta.get("error") or job_meta.get("message") or "")[:300]
        raise RuntimeError(
            f"{device} job {getattr(job, 'id', None)} ended {status}"
            + (f": {detail}" if detail else "") + " — no counts were produced"
        )
    res = job.result()
    counts = _counts_from_result(res)
    if not counts:
        raise RuntimeError(
            f"{device} job {getattr(job, 'id', None)} returned no counts (status {status}) — "
            "the backend rejected or dropped the circuit; treat this as a failed run"
        )
    out: dict[str, Any] = {
        "device": device,
        "shots": shots,
        "job_id": getattr(job, "id", None),
        "counts": counts,
    }
    billed = {k: _json_safe(job_meta[k]) for k in ("cost", "executionDuration", "timeStamps") if k in job_meta}
    if billed:
        out["billed"] = billed
    if cost_estimate is not None:
        out["cost_estimate"] = cost_estimate
    return out


def _run_local_circuit(circuit, shots: int, device: str, seed: int = 1234) -> dict[str, Any]:
    """Sample a Qiskit circuit on a local, noiseless state-vector — no qBraid
    account, no network. Returns the same ``{device, shots, job_id, counts}``
    shape as the hosted path so callers stay backend-agnostic.

    Measurement bitstrings are emitted big-endian (``int(bits, 2)`` == the qiskit
    basis index), which is exactly what :func:`_measured_index` parses for a
    non-``qbraid:`` device — so recall decodes a local run identically to a
    hosted one.

    Counts are keyed by the **classical register**, as hosted devices key
    theirs: a circuit placed on physical qubits (``place_on``) measures a few of
    many, and the string has one bit per classical bit (leftmost = highest
    clbit), never one per qubit. With no placement this is the old string
    exactly.
    """
    from qiskit.quantum_info import Statevector

    base = circuit.remove_final_measurements(inplace=False)
    probs = np.asarray(Statevector.from_instruction(base).probabilities(), dtype=float)
    probs = np.clip(probs, 0.0, None)
    total = probs.sum()
    probs = probs / total if total > 0 else np.full(len(probs), 1.0 / len(probs))
    n = base.num_qubits
    # qubit → clbit for every final measurement; identity when none are declared.
    pairs: list[tuple[int, int]] = []
    for inst in circuit.data:
        if inst.operation.name == "measure":
            pairs.append((circuit.find_bit(inst.qubits[0]).index, circuit.find_bit(inst.clbits[0]).index))
    n_cl = circuit.num_clbits if pairs else n
    rng = np.random.default_rng(seed)
    counts: dict[str, int] = {}
    for idx in rng.choice(len(probs), size=int(shots), p=probs):
        if pairs:
            out = ["0"] * n_cl
            for q, c in pairs:
                out[n_cl - 1 - c] = str((int(idx) >> q) & 1)
            bits = "".join(out)
        else:
            bits = format(int(idx), f"0{n}b")
        counts[bits] = counts.get(bits, 0) + 1
    return {"device": device, "shots": int(shots), "job_id": None, "counts": counts}


def run_qiskit(
    circuit,
    device: str = DEFAULT_DEVICE,
    shots: int = 100,
    allow_spend: bool = False,
    max_credits: float | None = None,
    max_seconds: float | None = None,
    subcategory: str | None = None,
) -> dict[str, Any]:
    """Run a Qiskit circuit on a device.

    ``local:*`` devices are evaluated on a local state-vector (no provider);
    everything else is serialized to OpenQASM 3 and submitted via :func:`run_qasm`.
    """
    if device.startswith(LOCAL_PREFIX):
        return _run_local_circuit(circuit, shots, device)

    from qiskit import transpile
    from qiskit.qasm3 import dumps

    # Hosted backends receive plain OpenQASM 3. Library gates such as StatePreparation
    # would be exported as an undefined custom gate ("Undefined gate 'state_preparation'",
    # OpenQuantum 2026-09-07); qBraid's own route happened to compile them away. Lower to
    # a standard basis first. No coupling map is given, so logical qubit indices are kept
    # and the bench's index-based decoding stays valid.
    circuit = transpile(circuit, basis_gates=["rz", "sx", "x", "cx"], optimization_level=1)

    return run_qasm(
        dumps(circuit),
        device=device,
        shots=shots,
        allow_spend=allow_spend,
        max_credits=max_credits,
        max_seconds=max_seconds,
        subcategory=subcategory,
    )


def qrng(
    n_bits: int = 8,
    device: str = DEFAULT_DEVICE,
    allow_spend: bool = False,
    max_credits: float | None = None,
    subcategory: str | None = None,
) -> dict[str, Any]:
    """Generate ``n_bits`` of true quantum randomness from measurement collapse.

    Runs an ``H^⊗k`` circuit and reads measured bitstrings (one per shot),
    concatenating until ``n_bits`` are produced. Returns the bits, their integer
    value, and a [0,1) float — a drop-in quantum entropy source for the medium's
    irrationality / dream noise.
    """
    from qiskit import QuantumCircuit

    n_bits = max(1, int(n_bits))
    width = min(n_bits, SIM_QUBIT_CAP)
    shots = (n_bits + width - 1) // width
    qc = QuantumCircuit(width, width)
    qc.h(range(width))
    qc.measure(range(width), range(width))
    out = run_qiskit(
        qc, device=device, shots=shots, allow_spend=allow_spend, max_credits=max_credits, subcategory=subcategory
    )
    # Expand counts into a flat list of per-shot bitstrings (order-independent;
    # fine for entropy). qBraid returns big-endian bitstrings.
    samples: list[str] = []
    for bits, c in out["counts"].items():
        samples.extend([bits.zfill(width)] * int(c))
    if not samples:
        raise RuntimeError("no measurements returned from device")
    bitstr = "".join(samples)[:n_bits]
    value = int(bitstr, 2) if bitstr else 0
    return {
        "bits": bitstr,
        "n_bits": n_bits,
        "int": value,
        "float": value / (2 ** n_bits),
        "device": device,
        "job_id": out["job_id"],
    }


def _optimal_iterations(target_amplitude: float) -> int:
    """Optimal amplitude-amplification iterations for a target that starts with
    amplitude ``a`` (sin θ = a) in the prepared state.

    Each Grover iteration rotates the state by 2θ toward the target, so the
    angle reaches π/2 (probability 1) after m = (π/2 − θ)/(2θ) iterations.
    Unlike the textbook ``(π/4)√N`` (which assumes a *uniform* start), this
    accounts for amplitude-encoded resonances — where the top memory may
    already start near the top, so the right answer is often 0 or 1 iterations.
    Over-rotating past π/2 would *de*-amplify the target.
    """
    a = float(min(1.0, max(0.0, target_amplitude)))
    if a <= 1e-9:
        return 0
    theta = float(np.arcsin(a))
    if theta >= np.pi / 2:
        return 0
    return max(0, round((np.pi / 2 - theta) / (2 * theta)))


def _measured_index(bits: str, device: str) -> int:
    """Decode a measurement bitstring to a candidate index, accounting for the
    device's qubit-ordering convention.

    qBraid's own backends report bitstrings big-endian, so we reverse to recover
    the qiskit little-endian index that ``StatePreparation`` used. AWS-routed
    devices (``aws:*`` — e.g. Rigetti via Braket) report in the opposite order,
    so no reversal. Verified on ``qbraid:qbraid:sim:qir-sv`` (reverse) and
    ``aws:rigetti:qpu:cepheus-1-108q`` (no reverse) — before this fix the latter's
    amplified peak was mis-labelled to the bit-reversed candidate. Non-qBraid
    providers (e.g. OpenQuantum) are treated like AWS pending a real recall to
    confirm their convention.
    """
    return int(bits[::-1], 2) if device.startswith("qbraid:") else int(bits, 2)


def quantum_recall(
    amplitudes: Sequence[float],
    labels: Sequence[str] | None = None,
    shots: int = 1024,
    amplify: bool = True,
    iterations: int | None = None,
    device: str = DEFAULT_DEVICE,
    allow_spend: bool = False,
    max_credits: float | None = None,
    subcategory: str | None = None,
    layout: Sequence[int] | None = None,
    readout_cal: list[tuple[float, float]] | None = None,
) -> dict[str, Any]:
    """Perform Kannaka's resonance recall *as a quantum circuit*.

    ``layout`` places the circuit on those physical qubits (see ``place_on``);
    ``readout_cal`` applies tensored readout mitigation (``mitigate_readout``)
    to the counts before decoding, and the result says so.

    The candidate memory resonances are amplitude-encoded into a quantum state
    ``|ψ⟩ = Σ (aᵢ/‖a‖)|i⟩`` (the query's interference pattern over the medium).
    Measuring already samples memories in proportion to ``aᵢ²``. With
    ``amplify=True`` we run amplitude amplification *about the prepared state*
    toward the strongest resonance — sharpening the recall by interference, the
    quantum analogue of "attention as gravity."

    Returns the measured distribution over candidates, the quantum top pick, and
    the classical argmax for comparison (they should agree — the point is that
    the recall ran on a quantum computer).
    """
    a = np.clip(np.asarray(amplitudes, dtype=float), 0.0, None)
    k = len(a)
    if k == 0:
        raise ValueError("need at least one amplitude")
    if labels is not None and len(labels) != k:
        raise ValueError("labels must match amplitudes length")
    n = max(1, int(np.ceil(np.log2(k))))
    dim = 2 ** n
    if n > SIM_QUBIT_CAP:
        raise ValueError(f"{k} candidates need {n} qubits (> {SIM_QUBIT_CAP} cap)")

    vec = np.zeros(dim)
    vec[:k] = a
    if not np.any(vec):
        vec[:k] = 1.0
    vec = vec / np.linalg.norm(vec)
    classical_top = int(np.argmax(a))

    from qiskit import QuantumCircuit
    from qiskit.circuit.library import StatePreparation

    prep = StatePreparation(vec)
    qc = QuantumCircuit(n, n)
    qc.append(prep, range(n))

    iters = 0
    if amplify and k > 1:
        if iterations is not None:
            iters = max(0, min(int(iterations), 8))
        else:
            iters = min(_optimal_iterations(vec[classical_top]), 8)
    if iters > 0:
        # LSB-first bit order so _phase_flip targets the qiskit basis state
        # ``classical_top`` (qubit q holds bit q), matching StatePreparation.
        target_bits = format(classical_top, f"0{n}b")[::-1]
        for _ in range(iters):
            # Oracle: phase-flip the strongest-resonance basis state.
            _phase_flip(qc, target_bits)
            # Diffuser about |ψ⟩:  A (2|0><0| - I) A†.
            qc.append(prep.inverse(), range(n))
            _phase_flip(qc, "0" * n)
            qc.append(prep, range(n))

    qc.measure(range(n), range(n))
    out = run_qiskit(
        place_on(qc, layout), device=device, shots=shots, allow_spend=allow_spend,
        max_credits=max_credits, subcategory=subcategory,
    )
    counts = out["counts"]
    if readout_cal is not None:
        counts = mitigate_readout(counts, readout_cal)

    dist: dict[int, int] = {}
    for bits, c in counts.items():
        # Bitstring qubit-order is device-dependent (qBraid-native reverses,
        # AWS-routed does not) — see _measured_index.
        idx = _measured_index(bits, device)
        if idx < k:
            dist[idx] = dist.get(idx, 0) + int(c)
    quantum_top = max(dist, key=dist.get) if dist else None

    def lbl(i: int | None):
        if i is None:
            return None
        return labels[i] if labels is not None else i

    return {
        "distribution": {str(lbl(i)): v for i, v in sorted(dist.items())},
        "quantum_top": lbl(quantum_top),
        "classical_top": lbl(classical_top),
        "agree": quantum_top == classical_top,
        "qubits": n,
        "candidates": k,
        "amplified": iters > 0,
        "iterations": iters,
        "device": device,
        "shots": shots,
        "job_id": out["job_id"],
        "layout": list(layout) if layout is not None else None,
        "readout_mitigated": readout_cal is not None,
    }


def _phase_flip(qc, bitstring: str) -> None:
    """Append a phase flip (Z) on the basis state ``bitstring`` (big-endian)."""
    n = len(bitstring)
    # X-mask the 0 bits so the all-ones controlled-Z targets ``bitstring``.
    zeros = [i for i, b in enumerate(bitstring) if b == "0"]
    for i in zeros:
        qc.x(i)
    if n == 1:
        qc.z(0)
    else:
        qc.h(n - 1)
        qc.mcx(list(range(n - 1)), n - 1)
        qc.h(n - 1)
    for i in zeros:
        qc.x(i)
