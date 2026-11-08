### Environment

- Machine: x86_64 (22 logical CPUs visible), Linux-6.6.87.2-microsoft-standard-WSL2-x86_64-with-glibc2.41
- PostgreSQL 18.6 (Debian 18.6-1.pgdg13+2), Python 3.13.18
- 4 web processes, 10 database connections each, one worker process
- Commit(s): 94d7802; 27 runs

### Spike: 300 students, 30 seats

| Strategy | Runs | Seats sold (confirmed) | Oversold seats | Reserve p50 (s) | Reserve p95 (s) | Reserve req/s | Failed requests |
| :-- | --: | --: | --: | --: | --: | --: | --: |
| `naive` | 3 | 40 (37-44) | 10 (7-14) | 0.8 (0.8-1.3) | 1.3 (1.3-2.2) | 157 (94-163) | 0 (0-0) |
| `row_lock` | 3 | 30 (30-30) | 0 (0-0) | 1.1 (1.0-2.0) | 1.6 (1.6-3.4) | 147 (73-157) | 0 (0-0) |
| `atomic_update` | 3 | 30 (30-30) | 0 (0-0) | 0.8 (0.8-1.3) | 1.5 (1.4-2.0) | 158 (104-187) | 0 (0-0) |
| `skip_locked` | 3 | 30 (30-30) | 0 (0-0) | 0.9 (0.9-1.5) | 1.7 (1.4-2.6) | 148 (92-166) | 0 (0-0) |

### Spike: 5,000 students, 200 seats

| Strategy | Runs | Seats sold (confirmed) | Oversold seats | Reserve p50 (s) | Reserve p95 (s) | Reserve req/s | Failed requests |
| :-- | --: | --: | --: | --: | --: | --: | --: |
| `naive` | 3 | 210 (204-211) | 10 (4-11) | 22.0 (22.0-31.0) | 34.0 (33.0-42.0) | 131 (107-138) | 0 (0-0) |
| `row_lock` | 3 | 200 (200-200) | 0 (0-0) | 19.0 (17.0-22.0) | 28.0 (24.0-31.0) | 152 (136-176) | 0 (0-0) |
| `atomic_update` | 3 | 200 (200-200) | 0 (0-0) | 21.0 (20.0-22.0) | 31.0 (31.0-32.0) | 133 (133-138) | 0 (0-0) |
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

Median, with the lowest to highest of the repeated runs in brackets.
