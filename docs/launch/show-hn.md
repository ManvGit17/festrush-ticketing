**Title:** Show HN: FestRush, a ticketing app that tests whether it can oversell

I rebuilt a small college-fest ticketing app to answer one question: can it sell the same seat
twice? It has seat holds, idempotent payments, an outbox and worker, a gateway, and load and chaos
tests that check invariants against the database afterwards.

In my runs the deliberately unsafe strategy oversold and the three safe ones did not. I also report
the results that went against my plan: a read cache and a waiting room did not help a simultaneous
arrival.

It is a learning project on one machine with a mock payment provider, not a production system.
`docker compose up` runs it; the benchmark scripts are in the repo.
