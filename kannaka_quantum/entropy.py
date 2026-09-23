"""Quantum entropy reservoir + provenance-tracked DRBG expansion.

Two layers, mirroring issues #11 (T1.1) and #12 (T1.2):

1. **Reservoir** — ``harvest`` runs :func:`core.qrng` against a *real* per-shot
   QPU and appends the raw measured bits to a local pool. The free qBraid
   simulator is a PRNG, so it is **explicitly invalid** as a reservoir source
   and refused. Files under ``$KANNAKA_DATA_DIR`` (or ``~/.kannaka``)/``entropy``:

   - ``reservoir.bin``      — raw harvested bytes (append-only).
   - ``reservoir.meta.jsonl`` — one JSON line per harvest (``device``, ``job_id``,
     ``n_bits``, ``bytes``, ``timestamp``, ``cost_usd``); the provenance ledger.
   - ``reservoir.state.json`` — the read cursor (bytes already consumed).

2. **DRBG** — ``draw`` consumes reservoir bytes as seed material for a
   NIST SP 800-90A HMAC-DRBG (stdlib ``hmac``/``hashlib`` only) and expands.
   Every draw records which harvest(s) seeded it, so an expanded stream chains
   back to a QPU ``job_id``. An empty reservoir fails loudly — there is no
   silent PRNG fallback, by design.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import core

# --------------------------------------------------------------------------- #
# Policy constants
# --------------------------------------------------------------------------- #
#: Default refill: 2048 real quantum bits from the cheapest per-shot QPU
#: (~$0.000255/shot ⇒ pennies per refill).
DEFAULT_HARVEST_BITS = 2048
DEFAULT_HARVEST_DEVICE = "openquantum:rigetti:cepheus-1-108q"

#: Seed material pulled from the reservoir per DRBG instantiation (384 bits —
#: comfortably above SHA-256's 256-bit security strength).
SEED_BYTES = 48
#: Reseed from the reservoir after this many bytes of DRBG output (issue #12).
RESEED_OUTPUT_BYTES = 1 << 16
#: Warn when the reservoir drops below this many unconsumed bytes.
LOW_WATERMARK_BYTES = 64


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def _entropy_dir() -> Path:
    base = os.environ.get("KANNAKA_DATA_DIR") or str(Path.home() / ".kannaka")
    d = Path(base) / "entropy"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _reservoir_path() -> Path:
    return _entropy_dir() / "reservoir.bin"


def _meta_path() -> Path:
    return _entropy_dir() / "reservoir.meta.jsonl"


def _state_path() -> Path:
    return _entropy_dir() / "reservoir.state.json"


# --------------------------------------------------------------------------- #
# Reservoir bookkeeping
# --------------------------------------------------------------------------- #
def _reservoir_total_bytes() -> int:
    p = _reservoir_path()
    return p.stat().st_size if p.exists() else 0


def _consumed_bytes() -> int:
    p = _state_path()
    if not p.exists():
        return 0
    try:
        return int(json.loads(p.read_text()).get("consumed_bytes", 0))
    except Exception:  # noqa: BLE001 - best-effort probe; falls back to a safe default
        return 0


def _set_consumed_bytes(n: int) -> None:
    _state_path().write_text(json.dumps({"consumed_bytes": int(n)}))


def _available_bytes() -> int:
    return max(0, _reservoir_total_bytes() - _consumed_bytes())


def _read_meta() -> list[dict[str, Any]]:
    """Harvest ledger with cumulative absolute byte offsets attached.

    Each entry gets ``abs_start``/``abs_end`` — the byte range it occupies in
    the append-only ``reservoir.bin`` — so a consumed range can be mapped back
    to the harvest(s) (and thus QPU job_ids) that produced it.
    """
    p = _meta_path()
    if not p.exists():
        return []
    entries: list[dict[str, Any]] = []
    offset = 0
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:  # noqa: BLE001, S112 - skip malformed/failed entry, keep processing the rest
            continue
        nbytes = int(e.get("bytes", 0))
        e["abs_start"] = offset
        e["abs_end"] = offset + nbytes
        offset += nbytes
        entries.append(e)
    return entries


def _provenance_for(abs_start: int, abs_end: int) -> list[dict[str, Any]]:
    """The harvest provenance records covering absolute byte range [start, end)."""
    prov = []
    for e in _read_meta():
        if e["abs_end"] > abs_start and e["abs_start"] < abs_end:
            prov.append(
                {
                    "device": e.get("device"),
                    "job_id": e.get("job_id"),
                    "harvested_at": e.get("timestamp"),
                }
            )
    return prov


def _bits_to_bytes(bitstr: str) -> bytes:
    """Pack a '0'/'1' string into bytes (drops a trailing partial byte)."""
    n = (len(bitstr) // 8) * 8
    if n == 0:
        return b""
    return int(bitstr[:n], 2).to_bytes(n // 8, "big")


def _harvest_cost_usd(device: str, n_bits: int) -> float | None:
    """Best-effort per-refill cost, mirroring qrng's shot math + the OQ price table."""
    if not device.startswith(core.OPENQUANTUM_PREFIX):
        return None
    code = device[len(core.OPENQUANTUM_PREFIX):]
    price = core.OQ_USD_PER_SHOT.get(code)
    if price is None:
        return None
    width = min(n_bits, core.SIM_QUBIT_CAP)
    shots = (n_bits + width - 1) // width
    return round(price * shots, 6)


def _consume(n_bytes: int) -> tuple[bytes, list[dict[str, Any]]]:
    """Read the next ``n_bytes`` from the reservoir, advance the cursor, and
    return them with their provenance. Raises if fewer are available."""
    avail = _available_bytes()
    if n_bytes > avail:
        raise RuntimeError(
            f"entropy reservoir has {avail} bytes but {n_bytes} were requested — "
            "run `harvest` to refill from a real QPU."
        )
    start = _consumed_bytes()
    end = start + n_bytes
    with open(_reservoir_path(), "rb") as f:
        f.seek(start)
        data = f.read(n_bytes)
    prov = _provenance_for(start, end)
    _set_consumed_bytes(end)
    return data, prov


# --------------------------------------------------------------------------- #
# HMAC-DRBG (NIST SP 800-90A, HMAC-SHA-256)
# --------------------------------------------------------------------------- #
class HmacDrbg:
    """A deterministic HMAC-DRBG (SP 800-90A) over SHA-256.

    Fully determined by its seed inputs, so ``generate`` is reproducible under a
    fixed seed (testable without hardware).
    """

    def __init__(self, entropy: bytes, nonce: bytes = b"", personalization: bytes = b"") -> None:
        if not entropy:
            raise ValueError("HMAC-DRBG requires non-empty seed entropy")
        self._K = b"\x00" * 32
        self._V = b"\x01" * 32
        self.reseed_counter = 1
        self._update(entropy + nonce + personalization)

    def _hmac(self, key: bytes, data: bytes) -> bytes:
        return hmac.new(key, data, hashlib.sha256).digest()

    def _update(self, provided_data: bytes = b"") -> None:
        self._K = self._hmac(self._K, self._V + b"\x00" + provided_data)
        self._V = self._hmac(self._K, self._V)
        if provided_data:
            self._K = self._hmac(self._K, self._V + b"\x01" + provided_data)
            self._V = self._hmac(self._K, self._V)

    def reseed(self, entropy: bytes, additional: bytes = b"") -> None:
        if not entropy:
            raise ValueError("reseed requires non-empty entropy")
        self._update(entropy + additional)
        self.reseed_counter = 1

    def generate(self, num_bytes: int, additional: bytes = b"") -> bytes:
        if num_bytes <= 0:
            return b""
        if additional:
            self._update(additional)
        out = b""
        while len(out) < num_bytes:
            self._V = self._hmac(self._K, self._V)
            out += self._V
        self._update(additional)
        self.reseed_counter += 1
        return out[:num_bytes]


# --------------------------------------------------------------------------- #
# Public operations
# --------------------------------------------------------------------------- #
#: Shots per CHSH setting for a harvest's Bell certificate. 256 × 4 settings is
#: the cheapest run that still separates S from the classical bound at the
#: fidelities the ledger has seen (row 2: 2.238 ± 0.073 at 512 shots).
DEFAULT_CERTIFY_SHOTS = 256

#: Names the Bell parameter a harvest must clear. The classical bound is 2; a
#: device that cannot beat it that day is, for the reservoir's purposes, a
#: classical noise source, and its bits are refused as quantum provenance.
CERTIFY_BOUND = 2.0


def harvest(
    n_bits: int = DEFAULT_HARVEST_BITS,
    device: str = DEFAULT_HARVEST_DEVICE,
    allow_spend: bool = False,
    max_credits: float | None = None,
    subcategory: str | None = None,
    certify: bool = True,
    certify_shots: int = DEFAULT_CERTIFY_SHOTS,
    chsh_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run ``qrng`` on a REAL QPU and append the raw bits to the reservoir.

    The free simulator is a PRNG and is refused. Spend guards are unchanged —
    ``core.qrng`` routes an ``openquantum:`` device through the same pre-flight
    cost gate (``--allow-spend`` / ``--max-credits``), so a no-opt-in or over-cap
    harvest raises before any job is submitted.

    With ``certify`` (the default) a CHSH test runs on the same device first
    and its Bell parameter goes into the harvest's provenance line. This is
    the design Quantum Origin uses — a Bell-test seed feeding an extractor — at
    the price of four short jobs. A device that does not violate the classical
    bound that day (``S <= 2``) is refused: the reservoir's claim is that its
    bits come from a non-classical process, and a certificate that cannot say
    so is not a certificate. ``--no-certify`` keeps the old behaviour and the
    meta line records ``bell: null`` so a reader can tell the two apart.
    """
    if "sim" in device.lower():
        raise RuntimeError(
            f"'{device}' is a simulator — a PRNG, not a valid entropy source. "
            "Harvest from a real per-shot QPU, e.g. "
            f"{DEFAULT_HARVEST_DEVICE} (~$0.000255/shot) or openquantum:iqm:garnet."
        )

    bell: dict[str, Any] | None = None
    if certify:
        run_chsh = chsh_fn or _default_chsh
        cert = run_chsh(
            device=device,
            shots=certify_shots,
            allow_spend=allow_spend,
            max_credits=max_credits,
            subcategory=subcategory,
        )
        bell = {
            "S": cert.get("S"),
            "abs_S": cert.get("abs_S"),
            "shots_per_setting": certify_shots,
            "violates_classical": bool(cert.get("violates_classical")),
            "job_ids": cert.get("job_ids"),
        }
        if not bell["violates_classical"]:
            raise RuntimeError(
                f"Bell certificate failed on {device}: |S| = {bell['abs_S']} does not exceed the "
                f"classical bound {CERTIFY_BOUND}. Not harvesting: the device did not behave "
                "non-classically in this sitting, so its bits cannot carry quantum provenance. "
                "Retry later, choose another device, or pass --no-certify to record an "
                "uncertified harvest."
            )

    result = core.qrng(
        n_bits, device=device, allow_spend=allow_spend, max_credits=max_credits, subcategory=subcategory
    )
    raw = _bits_to_bytes(result["bits"])
    if not raw:
        raise RuntimeError(f"harvest produced too few bits ({len(result['bits'])}) to store a byte")

    with open(_reservoir_path(), "ab") as f:
        f.write(raw)

    entry = {
        "device": device,
        "job_id": result.get("job_id"),
        "n_bits": result["n_bits"],
        "bytes": len(raw),
        "timestamp": _now_iso(),
        "cost_usd": _harvest_cost_usd(device, result["n_bits"]),
        "bell": bell,
    }
    with open(_meta_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    return {
        "harvested": True,
        "device": device,
        "job_id": entry["job_id"],
        "bits_harvested": result["n_bits"],
        "bytes_added": len(raw),
        "cost_usd": entry["cost_usd"],
        "bell": bell,
        "reservoir_available_bytes": _available_bytes(),
    }


def _default_chsh(**kwargs: Any) -> dict[str, Any]:
    from . import bell as _bell  # local import: bell imports core, entropy must not at load

    return _bell.chsh(**kwargs)


def status() -> dict[str, Any]:
    """Reservoir level, last-harvest provenance, and estimated refill cost."""
    meta = _read_meta()
    avail = _available_bytes()
    last = meta[-1] if meta else None
    refill_cost = _harvest_cost_usd(DEFAULT_HARVEST_DEVICE, DEFAULT_HARVEST_BITS)
    return {
        "reservoir_path": str(_reservoir_path()),
        "available_bytes": avail,
        "available_bits": avail * 8,
        "consumed_bytes": _consumed_bytes(),
        "total_harvested_bytes": _reservoir_total_bytes(),
        "harvest_count": len(meta),
        "low": avail < LOW_WATERMARK_BYTES,
        "last_harvest": (
            {
                "device": last.get("device"),
                "job_id": last.get("job_id"),
                "harvested_at": last.get("timestamp"),
                "bits": last.get("n_bits"),
                "cost_usd": last.get("cost_usd"),
            }
            if last
            else None
        ),
        "estimated_refill": {
            "device": DEFAULT_HARVEST_DEVICE,
            "bits": DEFAULT_HARVEST_BITS,
            "cost_usd": refill_cost,
        },
    }


def draw(n_bits: int, expand: bool = True) -> dict[str, Any]:
    """Draw ``n_bits`` from the reservoir, with a provenance chain to the QPU.

    ``expand=False``  — return raw quantum bits straight from the reservoir
    (consumes exactly ``ceil(n_bits/8)`` bytes).
    ``expand=True``   — seed an HMAC-DRBG from reservoir bytes and expand to
    ``n_bits`` (consumes seed material, reseeding every
    :data:`RESEED_OUTPUT_BYTES` from the reservoir when material remains).

    An empty reservoir raises — never a silent PRNG fallback.
    """
    n_bits = int(n_bits)
    if n_bits <= 0:
        raise ValueError("n_bits must be positive")
    if _available_bytes() <= 0:
        raise RuntimeError(
            "entropy reservoir is empty — run `harvest` against a real QPU first. "
            "This tool never falls back to a software PRNG."
        )

    n_bytes = (n_bits + 7) // 8

    if not expand:
        data, prov = _consume(n_bytes)
        out_bytes = data
        reseeds = 0
        mode = "raw"
    else:
        seed_len = min(SEED_BYTES, _available_bytes())
        seed, prov = _consume(seed_len)
        drbg = HmacDrbg(seed, nonce=_now_iso().encode(), personalization=b"kannaka-quantum/qrng-draw")
        chunks: list[bytes] = []
        produced = 0
        reseeds = 0
        while produced < n_bytes:
            take = min(RESEED_OUTPUT_BYTES, n_bytes - produced)
            chunks.append(drbg.generate(take))
            produced += take
            # Reseed from fresh quantum material between output blocks if any remains.
            if produced < n_bytes and _available_bytes() > 0:
                extra_len = min(SEED_BYTES, _available_bytes())
                extra, extra_prov = _consume(extra_len)
                drbg.reseed(extra)
                reseeds += 1
                for pp in extra_prov:
                    if pp not in prov:
                        prov.append(pp)
        out_bytes = b"".join(chunks)
        mode = "drbg-expand"

    bitstr = bin(int.from_bytes(out_bytes, "big"))[2:].zfill(len(out_bytes) * 8)[:n_bits] if out_bytes else ""
    value = int(bitstr, 2) if bitstr else 0
    result = {
        "bits": bitstr,
        "n_bits": n_bits,
        "int": value,
        "mode": mode,
        "expanded": expand,
        "reseeds": reseeds,
        "provenance": prov,
        "reservoir_available_bytes": _available_bytes(),
    }
    if not prov:
        # Should not happen (we raise on empty), but never let a draw look sourceless.
        raise RuntimeError("draw produced no provenance — reservoir/meta out of sync")
    if _available_bytes() < LOW_WATERMARK_BYTES:
        result["warning"] = (
            f"reservoir low ({_available_bytes()} bytes left) — run `harvest` to refill."
        )
    return result


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
