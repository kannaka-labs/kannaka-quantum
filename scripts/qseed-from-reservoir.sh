#!/usr/bin/env sh
# Draw a QuantumOS boot seed from the entropy reservoir and print the kernel argument.
#
#   qseed="$(scripts/qseed-from-reservoir.sh)"   →   qseed=<64 hex chars>
#
# 256 raw reservoir bits (no DRBG expansion: the kernel mixes its own pool), each
# carrying the harvest's QPU job_id and, since Wave 4, its Bell certificate. An
# empty reservoir fails loudly here as everywhere; there is no PRNG fallback, so
# a boot without a seed reports `prng`, never fake quantum provenance.
set -eu
out="$(kannaka-quantum qrng-draw --bits 256)"
hex="$(printf '%s' "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); b=d.get("bits") or ""; print("%064x" % int(b, 2)) if b else sys.exit("qseed: reservoir draw returned no bits: " + json.dumps(d)))')"
printf 'qseed=%s\n' "$hex"
