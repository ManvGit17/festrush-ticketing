### Environment

- Machine: Intel64 Family 6 Model 170 Stepping 4, GenuineIntel (22 logical CPUs visible), Windows-11-10.0.26300-SP0
- PostgreSQL 18.6 (Debian 18.6-1.pgdg13+2), Python 3.13.5
- 2 web processes, 10 database connections each, one worker process
- Commit(s): 85f7f44, 94d7802, unknown; 55 runs

### Spike: 100 students, 20 seats

| Strategy | Runs | Seats sold (confirmed) | Oversold seats | Reserve p50 (s) | Reserve p95 (s) | Reserve req/s | Failed requests |
| :-- | --: | --: | --: | --: | --: | --: | --: |
| `atomic_update` | 1 | 20 | 0 | 0.3 | 4.6 | 17 | 34 |

### Spike: 300 students, 30 seats

| Strategy | Runs | Seats sold (confirmed) | Oversold seats | Reserve p50 (s) | Reserve p95 (s) | Reserve req/s | Failed requests |
| :-- | --: | --: | --: | --: | --: | --: | --: |
| `naive` | 3 | 40 (37-44) | 10 (7-14) | 0.8 (0.8-1.3) | 1.3 (1.3-2.2) | 157 (94-163) | 0 (0-0) |
| `row_lock` | 3 | 30 (30-30) | 0 (0-0) | 1.1 (1.0-2.0) | 1.6 (1.6-3.4) | 147 (73-157) | 0 (0-0) |
| `atomic_update` | 6 | 30 (27-30) | 0 (0-0) | 1.2 (0.8-1.4) | 1.7 (1.4-2.0) | 116 (65-187) | 0 (0-0) |
| `skip_locked` | 3 | 30 (30-30) | 0 (0-0) | 0.9 (0.9-1.5) | 1.7 (1.4-2.6) | 148 (92-166) | 0 (0-0) |

### Spike: 1,500 students, 200 seats

| Strategy | Runs | Seats sold (confirmed) | Oversold seats | Reserve p50 (s) | Reserve p95 (s) | Reserve req/s | Failed requests |
| :-- | --: | --: | --: | --: | --: | --: | --: |
| `atomic_update` | 13 | 200 (200-200) | 0 (0-0) | 2.1 (1.9-8.3) | 3.3 (2.6-12.8) | 228 (90-270) | 0 (0-0) |

### Spike: 5,000 students, 200 seats

| Strategy | Runs | Seats sold (confirmed) | Oversold seats | Reserve p50 (s) | Reserve p95 (s) | Reserve req/s | Failed requests |
| :-- | --: | --: | --: | --: | --: | --: | --: |
| `naive` | 3 | 210 (204-211) | 10 (4-11) | 22.0 (22.0-31.0) | 34.0 (33.0-42.0) | 131 (107-138) | 0 (0-0) |
| `row_lock` | 3 | 200 (200-200) | 0 (0-0) | 19.0 (17.0-22.0) | 28.0 (24.0-31.0) | 152 (136-176) | 0 (0-0) |
| `atomic_update` | 14 | 200 (200-200) | 0 (0-0) | 13.4 (0.0-25.9) | 20.6 (0.4-39.1) | 198 (42-3,341) | 0 (0-1) |
| `skip_locked` | 3 | 200 (200-200) | 0 (0-0) | 25.0 (24.0-25.0) | 36.0 (36.0-36.0) | 125 (122-127) | 0 (0-0) |

### Retry storm: every write sent twice with the same key

| Strategy | Users | Seats | Writes sent | Seats sold | Oversold | Orders | Charges at provider | Duplicate answered differently | Failed requests |
| :-- | --: | --: | --: | --: | --: | --: | --: | --: | --: |
| `atomic_update` | 5,000 | 200 | 10,800 | 200 | 0 | 200 | 200 | 0 | 0 |
| `skip_locked` | 5,000 | 200 | 10,800 | 200 | 0 | 200 | 200 | 0 | 0 |

### Soak: slow, declining and forgetful payment provider, short holds

| Strategy | Duration (s) | Requests | Failed | Reservations | Orders | Payments | Messages | Oversold | Violated invariants |
| :-- | --: | --: | --: | :-- | :-- | :-- | --: | --: | :-- |
| `atomic_update` | 60 | 14,245 | 0 | {'confirmed': 440, 'held': 10} | {'confirmed': 422, 'payment_failed': 28} | {'failed': 28, 'succeeded': 422} | 422 | 0 | none |

### E0: what limits throughput (5,000 students, k6)

| Web processes | Threads each | Students | Requests/s overall | Reserve p50 (s) |
| --: | --: | --: | --: | --: |
| 1 | 40 | 1,500 | 114 | 8.3 |
| 4 | 10 | 5,000 | 214 | 12.7 |
| 4 | 40 | 5,000 | 260 | 9.2 |
| 8 | 10 | 5,000 | 320 | 8.2 |
| 16 | 5 | 5,000 | 404 | 6.3 |

### The gateway: rate limiting a 5,000-student spike

| Setup | Whole run (s) | Reserve attempt p50 / p95 (s) | Order p50 (s) | Pay p50 (s) | Rejected with 429 | Seats sold | Oversold |
| :-- | --: | --: | --: | --: | --: | --: | --: |
| no gateway | 44 | 24.8 / 35.1 | 22.9 | 11.1 | 0 | 200 | 0 |
| gateway, 300 claims/s | 48 | 0.0 / 20.1 | 17.0 | 22.2 | 47,831 | 200 | 0 |
| gateway, 100 claims/s | 48 | 0.0 / 0.4 | 6.9 | 15.2 | 157,141 | 200 | 0 |

### The waiting room in front of the app

| Admission rate | Whole run (s) | Join p50 (s) | Status poll p50 (s) | Reserve p50 (s) | Status polls | Seats sold | Oversold |
| --: | --: | --: | --: | --: | --: | --: | --: |
| 100/s | 119 | 37.7 | 25.5 | 24.6 | 5,000 | 200 | 0 |
| 300/s | 119 | 32.7 | 32.0 | 25.9 | 5,000 | 200 | 0 |

### Chaos: break something while 300 students buy 30 seats

| Fault | What was broken | Students | Seats sold | Messages | Failed requests | Oversold | Invariants | App back after |
| :-- | :-- | --: | --: | --: | --: | --: | :-- | --: |
| kill the worker | killing background; starting background again | 300 | 30 | 30 | 0 | 0 | all hold | n/a |
| kill the payments service | killing payments; starting payments again | 300 | 30 | 30 | 0 | 0 | all hold | n/a |
| stop PostgreSQL for 5 s | stopping PostgreSQL; starting PostgreSQL again | 100 | 20 | 20 | 34 | 0 | all hold | 0.0 s |
| slow, lossy provider | 800 ms latency, 20% lost answers, 10% declines | 300 | 27 | 27 | 0 | 0 | all hold | n/a |

#### E2: the payment provider (each charge takes 300 ms)

| Variant | Runs | Whole run (s) | Reserve p50 (s) | Order p50 (s) | Pay p50 (s) | Pay p95 (s) | Page refresh p50 (s) | Oversold |
| :-- | --: | --: | --: | --: | --: | --: | --: | --: |
| `inprocess` | 2 | 5.6 (5.5-5.6) | 2.1 (2.1-2.1) | 2.3 (2.3-2.4) | 1.0 (0.9-1.0) | 2.3 (2.3-2.3) | n/a | 0 (0-0) |
| `service` | 2 | 6.3 (6.1-6.5) | 2.3 (2.3-2.3) | 2.4 (2.3-2.5) | 1.6 (1.6-1.6) | 3.0 (2.8-3.3) | n/a | 0 (0-0) |

#### E3: where the confirmation goes (each message takes 300 ms)

| Variant | Runs | Whole run (s) | Reserve p50 (s) | Order p50 (s) | Pay p50 (s) | Pay p95 (s) | Page refresh p50 (s) | Oversold |
| :-- | --: | --: | --: | --: | --: | --: | --: | --: |
| `outbox` | 2 | 6.2 (6.1-6.4) | 2.5 (2.4-2.6) | 2.8 (2.5-3.1) | 1.0 (0.8-1.1) | 2.5 (2.1-3.0) | n/a | 0 (0-0) |
| `inline` | 2 | 6.9 (6.6-7.2) | 2.3 (2.1-2.5) | 2.6 (2.2-3.0) | 2.0 (1.8-2.2) | 3.4 (3.3-3.5) | n/a | 0 (0-0) |

#### E4: a one-second read cache in the gateway (students refresh the page 6 times)

| Variant | Runs | Whole run (s) | Reserve p50 (s) | Order p50 (s) | Pay p50 (s) | Pay p95 (s) | Page refresh p50 (s) | Oversold |
| :-- | --: | --: | --: | --: | --: | --: | --: | --: |
| `nocache` | 2 | 7.5 (7.4-7.5) | 2.0 (2.0-2.1) | 2.3 (2.2-2.4) | 0.9 (0.8-0.9) | 3.0 (3.0-3.1) | 0.00 (0.00-0.00) | 0 (0-0) |
| `cache` | 2 | 7.8 (7.5-8.2) | 1.9 (1.9-2.0) | 1.9 (1.8-1.9) | 1.2 (1.1-1.2) | 3.1 (3.1-3.1) | 0.00 (0.00-0.00) | 0 (0-0) |

Median, with the lowest to highest of the repeated runs in brackets.
