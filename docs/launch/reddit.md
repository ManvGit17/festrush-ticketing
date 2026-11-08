**Title:** I rebuilt a fest ticketing app and tried to make it oversell (FastAPI + PostgreSQL)

Four ways of claiming a seat, one deliberately unsafe, then a seat rush of thousands of simulated
students and a check of the database afterwards. The unsafe one oversold; the others did not. Retry
storms and chaos runs (killing the worker, the payment service and the database mid-sale) are in
there too.

It is a learning project with a mock payment provider. I would like feedback on the concurrency
write-up, and on anything I measured badly. Code, scripts and raw results are in the repo.
