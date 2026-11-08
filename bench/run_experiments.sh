#!/usr/bin/env bash
# The phase 3 experiments (docs/architecture.md). Each compares a design choice against its
# alternative, interleaved and repeated, with k6 as the load generator inside Docker.
#
#   bench/run_experiments.sh           # about 12 minutes
#   REPS=1 bench/run_experiments.sh    # a quick look
#
# Results land in bench/results/exp-*.json; `python bench/summarize.py` prints the tables.
set -u
REPS="${REPS:-2}"
cd "$(dirname "$0")/.."
export BENCH_COMMIT="${BENCH_COMMIT:-$(git rev-parse --short HEAD 2>/dev/null || echo unknown)}"
mkdir -p bench/results
docker compose up -d db >/dev/null
run() { docker compose --profile bench run --rm -e BENCH_COMMIT bench --strategy atomic_update --driver k6 "$@" | grep -E "reserve:|OVERSOLD" ; }
COMMON="--users 1500 --seats 200"

for rep in $(seq 1 "$REPS"); do
  echo "== rep $rep"
  # E3: where does the confirmation message go? (messages take 300 ms to send)
  echo "-- E3 outbox + worker";   run $COMMON --email-delay-ms 300 --label "exp-e3-outbox-r$rep"
  echo "-- E3 inline in the request"; run $COMMON --email-delay-ms 300 --notify-inline --label "exp-e3-inline-r$rep"
  # E2: the payment provider in-process or as its own service (each charge takes 300 ms)
  echo "-- E2 provider in-process"; run $COMMON --mockpay-latency-ms 300 --label "exp-e2-inprocess-r$rep"
  echo "-- E2 provider as a service"; run $COMMON --mockpay-latency-ms 300 --payments-service --label "exp-e2-service-r$rep"
  # E4: a one-second read cache in the gateway, with students also refreshing the event page
  echo "-- E4 gateway cache off";  run $COMMON --gateway --refreshes 6 --catalog-ttl 0 --claim-rate 100000 --claim-burst 100000 --label "exp-e4-nocache-r$rep"
  echo "-- E4 gateway cache on";   run $COMMON --gateway --refreshes 6 --catalog-ttl 1 --claim-rate 100000 --claim-burst 100000 --label "exp-e4-cache-r$rep"
done
echo "done: python bench/summarize.py"
