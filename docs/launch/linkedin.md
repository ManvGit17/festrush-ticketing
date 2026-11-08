I turned a small college-fest app into a study of one problem: never selling a seat twice.

It now has expiring seat holds, idempotent payments, an outbox and worker, a gateway, metrics and
traces, and load and chaos tests that check invariants in the database. The deliberately unsafe
strategy oversold under load; the three safe ones did not. I also wrote up the results that
disagreed with my plan.

It is a learning project on a single machine with a mock payment provider. The code, benchmark
scripts and raw results are public.
