#!/usr/bin/env bash
# The documented comparison: every strategy at small and large scale, several repetitions,
# interleaved so that slow drift on the machine hits every strategy equally.
#
#   bench/run_matrix.sh            # the full matrix (about 20 minutes)
#   REPS=1 bench/run_matrix.sh     # one repetition, for a quick look
#
# Runs inside Docker (see docker-compose.yml) so the environment is the same for everyone, and
# writes one JSON file per run to bench/results/. Then run `python bench/summarize.py`.
set -u
REPS="${REPS:-3}"
cd "$(dirname "$0")/.."
export BENCH_COMMIT="${BENCH_COMMIT:-$(git rev-parse --short HEAD 2>/dev/null || echo unknown)}"
mkdir -p bench/results
docker compose up -d db >/dev/null
bench() { docker compose --profile bench run --rm -e BENCH_COMMIT bench "$@" | grep -E "^(spike|retry|soak|large|small)|OVERSOLD|reserve:" ; }

for rep in $(seq 1 "$REPS"); do
  for scale in "300 30 small" "5000 200 large"; do
    set -- $scale
    for strategy in naive row_lock atomic_update skip_locked; do
      echo "== rep $rep: $3 / $strategy"
      extra=""
      [ "$strategy" = "naive" ] && extra="--expect-oversell"
      bench --strategy "$strategy" --users "$1" --seats "$2" --label "$3-$strategy-r$rep" $extra || true
    done
  done
done

echo "== retry storm (every write sent twice with the same key)"
bench --strategy atomic_update --scenario retry-storm --users 5000 --seats 200 --label "storm-atomic_update-r1" || true
bench --strategy skip_locked --scenario retry-storm --users 5000 --seats 200 --label "storm-skip_locked-r1" || true

echo "== soak: 3 events, 60 s, slow and unreliable payment provider, short holds"
bench --strategy atomic_update --scenario soak --users 200 --seats 150 --events 3 --duration 60 \
  --hold-minutes 1 --mockpay-latency-ms 40 --mockpay-decline-rate 0.05 --mockpay-timeout-rate 0.05 \
  --label "soak-atomic_update-r1" || true
echo "done: python bench/summarize.py"
