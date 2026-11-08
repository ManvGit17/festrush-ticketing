# How do you sell the last seat exactly once?

Ticketing has one job that cannot go wrong: two people must never get the same seat. I rebuilt a
small college-fest app around that promise and measured whether it holds.

## The setup

FestRush has seat holds that expire, orders, payments through a separate mock provider, and a
worker that confirms tickets. Four ways to claim a seat sit behind one setting. One is deliberately
unsafe, as a baseline.

## What happened

With 300 students chasing 30 seats, the unsafe version sold 40. The three safe versions sold 30,
every run. At 5,000 students and 200 seats the unsafe one oversold again; the safe ones did not.
The safe strategies were close in speed, so the choice is about clarity, not performance.
Numbers: [bench/results/SUMMARY.md](../../bench/results/SUMMARY.md).

Retries were safe too. Sending every write twice with the same idempotency key produced 200 orders
and 200 charges for 200 seats.

## What surprised me

The database was not the bottleneck; the web tier was. A read cache and an in-app waiting room did
not help when everyone arrived at the same instant. I wrote those results down even though they
disagreed with my plan ([architecture](../architecture.md)).

## Limits

This is a teaching project run on one machine. The payment provider is a mock. The inventory is
not a separate service. The measurements are from my hardware and will differ on yours; the scripts
to rerun them are in the repository.
