#!/usr/bin/env python3
"""A heartbeat for a future machine, and a catcher waiting for an answer.

SEND measures 64 qubits on a real quantum computer, each prepared in an equal
superposition, and seals what came out together with the circuit, the job id
and the time. Hardware has a minimum number of shots, so every shot is kept
and the heartbeat is their bitwise XOR: 64 bits that no ordering can change.

The seal names several rounds of the drand public randomness beacon that do
not exist yet: an hour from now, a day, a month, a year, ten years. Nobody can
know those values today. That is the door left open: if some future hardware
could act backward in time on this measurement, the one thing it could do that
nothing else can is make today's bits agree with a number that is only
published later.

CATCH is the catcher. Whenever a named round has been published it compares
the round's first 64 bits with the sealed bits and writes down how many agree.
Ordinary physics says about 32 of 64, every time. The rule for calling
anything a signal is fixed in each record before the answer exists.

What this proves end to end today: the forward half. A real measurement, sealed
and dated, and a comparison that runs by itself when the future arrives. No
known physics sends the answer back, and a run of ordinary results is the
expected outcome, reported as such.

    python scripts/heartbeat.py send --device openquantum:rigetti:cepheus-1-108q --allow-spend
    python scripts/heartbeat.py catch
    python scripts/heartbeat.py status
"""
import argparse
import hashlib
import json
import math
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DRAND = "https://api.drand.sh"
CHAIN = "52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971"  # quicknet: one round every 3 s
GENESIS, PERIOD = 1692803367, 3
BITS = 64
HORIZONS = {"1h": 3600, "1d": 86400, "30d": 30 * 86400, "1y": 365 * 86400, "10y": 3652 * 86400}
# Fixed before any answer exists: a single comparison counts as a signal only at 58 or more
# of 64 bits agreeing, or 6 or fewer (the same message inverted).
HIT_HIGH, HIT_LOW = 58, 6
RULE = (
    f"Compare bit i of `bits` with bit i of the first {BITS} bits (most significant first) of the drand "
    f"quicknet randomness for each target round. Chance gives about {BITS // 2} agreements. A comparison is a "
    f"signal only if {HIT_HIGH} or more agree, or {HIT_LOW} or fewer. Anything else is an ordinary result."
)
QASM = f'OPENQASM 3;\ninclude "stdgates.inc";\nqubit[{BITS}] q;\nbit[{BITS}] c;\nh q;\nc = measure q;\n'


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "heartbeat"}), timeout=20) as r:
        return json.load(r)


def round_at(ts):
    return int((ts - GENESIS) // PERIOD) + 1


def round_time(rnd):
    return GENESIS + (rnd - 1) * PERIOD


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tail_p(k, n=BITS):
    """Chance of a result at least this far from n/2, either side."""
    d = abs(k - n / 2)
    return min(1.0, sum(math.comb(n, j) for j in range(n + 1) if abs(j - n / 2) >= d) / 2 ** n)


def measure(device, allow_spend, max_credits, cli, shots):
    cmd = [cli, "run", "--qasm", "-", "--device", device, "--shots", str(shots), "--max-credits", str(max_credits)]
    if allow_spend:
        cmd.append("--allow-spend")
    r = subprocess.run(cmd, input=QASM, capture_output=True, text=True, timeout=1800, check=False)
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise SystemExit(f"the quantum bridge did not answer in JSON: {r.stdout[-300:]} {r.stderr[-300:]}")
    if out.get("error"):
        raise SystemExit(f"the run failed: {out['error']}")
    counts = out.get("counts") or out.get("measurement_counts") or {}
    strings = [k.replace(" ", "") for k, v in counts.items() for _ in range(int(v))]
    if len(strings) != shots or any(len(s) != BITS or set(s) - {"0", "1"} for s in strings):
        raise SystemExit(f"expected {shots} results of {BITS} bits, got {list(counts.items())[:3]}")
    x = 0
    for s in strings:
        x ^= int(s, 2)
    return bin(x)[2:].zfill(BITS), sorted(strings), out


def send(a):
    out_dir = Path(a.records); out_dir.mkdir(parents=True, exist_ok=True)
    anchor = get(f"{DRAND}/{CHAIN}/public/latest")           # proves the record is not older than this round
    bits, shots, raw = measure(a.device, a.allow_spend, a.max_credits, a.cli, a.shots)
    now = time.time()
    record = {
        "heartbeat": 1,
        "sent_at": iso(now),
        "device": a.device,
        "simulated": ":sim:" in a.device,
        "job_id": raw.get("job_id") or raw.get("id"),
        "circuit_qasm3": QASM,
        "shots": shots,
        "bits": bits,
        "bits_are": "the bitwise XOR of every shot",
        "not_before": {"chain": CHAIN, "round": anchor["round"], "randomness": anchor["randomness"]},
        "targets": [{"horizon": h, "round": round_at(now + s), "due": iso(round_time(round_at(now + s)))} for h, s in HORIZONS.items()],
        "rule": RULE,
    }
    body = json.dumps(record, sort_keys=True, separators=(",", ":"))
    record["seal_sha256"] = hashlib.sha256(body.encode()).hexdigest()
    path = out_dir / f"{record['sent_at'][:10]}-{record['seal_sha256'][:12]}.json"
    path.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"sealed": str(path), "seal_sha256": record["seal_sha256"], "bits": bits, "job_id": record["job_id"],
                      "targets": [t["due"] for t in record["targets"]]}))


def catch(a):
    done = 0
    for path in sorted(Path(a.records).glob("*.json")):
        rec = json.loads(path.read_text(encoding="utf-8"))
        changed = False
        for t in rec.get("targets", []):
            if "agree" in t or round_time(t["round"]) > time.time() - 10:
                continue
            try:
                beacon = get(f"{DRAND}/{CHAIN}/public/{t['round']}")
            except (OSError, ValueError) as e:
                print(f"{path.name} {t['horizon']}: beacon not readable yet ({e})", file=sys.stderr)
                continue
            future = bin(int(beacon["randomness"], 16))[2:].zfill(256)[:BITS]
            agree = sum(x == y for x, y in zip(rec["bits"], future))
            t.update({"randomness": beacon["randomness"], "agree": agree, "of": BITS, "chance_of_this_or_stranger": tail_p(agree),
                      "signal": agree >= HIT_HIGH or agree <= HIT_LOW, "caught_at": iso(time.time())})
            changed = True; done += 1
            print(f"{path.name} {t['horizon']}: {agree}/{BITS} agree -> {'SIGNAL' if t['signal'] else 'ordinary'}")
        if changed:
            # The sealed part is untouched: the seal covers the record as sent, results are added beside it.
            path.write_text(json.dumps(rec, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"compared_now": done}))


def seal_ok(rec):
    """The seal covers the record as sent: results the catcher added later are set aside first."""
    sent = {k: v for k, v in rec.items() if k != "seal_sha256"}
    sent["targets"] = [{k: t[k] for k in ("horizon", "round", "due")} for t in rec["targets"]]
    body = json.dumps(sent, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest() == rec["seal_sha256"]


def status(a):
    rows = []
    for path in sorted(Path(a.records).glob("*.json")):
        rec = json.loads(path.read_text(encoding="utf-8"))
        for t in rec["targets"]:
            rows.append({"heartbeat": rec["sent_at"], "device": rec["device"], "seal_ok": seal_ok(rec), "horizon": t["horizon"], "due": t["due"],
                         "agree": t.get("agree"), "signal": t.get("signal")})
    seen = [r for r in rows if r["agree"] is not None]
    print(json.dumps({"comparisons_made": len(seen), "waiting": len(rows) - len(seen),
                      "signals": sum(1 for r in seen if r["signal"]),
                      "mean_agreement": round(sum(r["agree"] for r in seen) / len(seen), 2) if seen else None, "rows": rows}, indent=1))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--records", default=str(Path(__file__).resolve().parent.parent / "heartbeat"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send")
    s.add_argument("--device", default="qbraid:qbraid:sim:qir-sv")
    s.add_argument("--allow-spend", action="store_true")
    s.add_argument("--max-credits", type=float, default=1.0)
    s.add_argument("--shots", type=int, default=10)
    s.add_argument("--cli", default="kannaka-quantum")
    sub.add_parser("catch"); sub.add_parser("status")
    a = ap.parse_args()
    {"send": send, "catch": catch, "status": status}[a.cmd](a)


if __name__ == "__main__":
    main()
