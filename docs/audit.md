# MonolithicApplication: Project Audit

_Audited on 2026-11-08 against GitHub `main` (`78d9351`, "Monolithic architecture") plus the lab working copy's local edits, which are recorded in commit `b9aeaa2`: the SRN line, three wording changes in templates, and a `fest.db` that was never committed to GitHub._

## 1. What the project is

A small FastAPI app for a college fest, written for the Cloud Computing course at PES University.
Students sign up, log in, browse events, register for them and "check out". Data lives in SQLite,
pages are server-rendered Jinja templates, and four Locust files load-test the user journeys.

| Part | Files | Lines |
| :-- | :-- | --: |
| Application and routes | `main.py` | 131 |
| Database helper | `database.py` | 6 |
| Checkout logic | `checkout/__init__.py` | 23 |
| Seed script | `insert_events.py` | 32 |
| Templates (Tailwind via CDN) | `templates/*.html` (7 files) | 361 |
| Load tests | `locust/*.py`, `locust/locust/journey_locustfile.py` (4 files) | 44 |
| Dependencies | `requirements.txt` | 6 |
| Database in the lab working copy (not on GitHub's `main`) | `fest.db` | 24 KB |

Line counts: `wc -l main.py database.py checkout/__init__.py insert_events.py templates/*.html locust/*.py locust/locust/*.py`.

There is no README, license, test, CI, linter, type checker, Dockerfile or documentation.

## 2. Architecture

```
 browser ──GET/POST──▶ main.py (FastAPI, sync handlers)
                         │  get_db() on every call, never closed
                         ├──▶ checkout/__init__.py ──▶ get_db()
                         ▼
                      fest.db  (SQLite file, path relative to the working directory)
```

- One process, one file for every route, no service layer: handlers build SQL and render HTML.
- "Who is logged in" is a `?user=` query parameter copied from page to page. There is no session.
- Three tables are created at startup: `users`, `events`, `registrations`. Events have a name and a
  fee, nothing else. There is no capacity, no order and no payment.

## 3. Method

Everything in section 5 was reproduced, not guessed:

1. Read every file in the repository.
2. Ran the app on the dependency versions in the repo's own virtualenv (FastAPI 0.162, Starlette
   1.7) and on the versions the lab was written against (FastAPI 0.115.6, Starlette 0.41.3).
3. Wrote a characterization suite, `tests/characterization/` (25 tests), that pins current behaviour
   including the bugs. Run it with the pinned versions in
   `tests/characterization/requirements-legacy.txt`:
   `pytest tests/characterization`.
4. Ran `scripts/audit_probe_v1.py` against a scratch copy of v1 (never a real database) to
   measure the concurrency findings.
5. Opened the lab working copy's `fest.db` and listed its schema and rows.

Evidence column: `file:line` points at the code, `test` names a characterization test that fails if
the finding is fixed (each test flips in the commit that fixes its finding).

Severity: **Critical** = the app can't be used or secrets are exposed, **High** = wrong results,
data loss or an exploitable hole, **Medium** = misleading or fragile, **Low** = hygiene.

## 4. What works

- All eight routes respond on the original dependency versions: sign-up, login, events,
  register, my-events, checkout and the error page.
- SQL uses bound parameters everywhere, so there is no SQL injection
  (`test_login_is_not_sql_injectable`).
- Jinja autoescaping stops script injection through `?user=`
  (`test_user_param_is_html_escaped`).
- The four Locust files are valid and each targets one route plus a full journey.
- The "optimized" checkout (`sum()` over fees) is correct arithmetic. Its problem is what it sums.

## 5. Findings

### 5.1 Security

| # | Severity | Finding | Evidence |
| :-- | :-- | :-- | :-- |
| S1 | Critical | **Passwords are stored and compared in plaintext.** | `main.py:30`, `main.py:45-48`; `test_password_is_stored_in_plaintext` |
| S2 | Critical | **There is no authentication after login.** Identity is the `?user=` query string, so anyone can view or act as any user by typing a name. State-changing registration is a `GET`, so a link, an image tag or a browser prefetch registers someone. | `main.py:56`, `main.py:61`, `main.py:76-84`; `test_login_success_puts_the_username_in_the_url`, `test_register_for_event_without_logging_in` |
| S3 | High | **Personal data in source and data.** The student SRN is hardcoded and unused (nothing reads `SRN`) in the lab working copy; GitHub's `main` did not have the line until this branch. The same string is also the username and the password of the only row in the lab copy's `users` table. | `main.py:8`; `fest.db` `users` table |
| S4 | Medium | **Nothing stops the database being committed.** The lab folder holds a live `fest.db` with a real account in it, `.gitignore` does not list it, and every run of the app rewrites it. (It is not on GitHub's `main`; it was only in a local snapshot commit.) | `git ls-files fest.db`; `.gitignore` |
| S5 | Medium | **Exception text is shown to users.** The global handler renders `str(exc)` into the error page. | `main.py:118-132`, `templates/error.html:20`; `test_event_404_is_a_deliberate_crash` |
| S6 | Medium | **No input validation.** Empty usernames and passwords create accounts on the lab's dependency versions (newer FastAPI happens to answer 422 and hide this). No length limit, no password rules. | `main.py:27`; `test_empty_username_and_password_create_an_account` |
| S7 | Medium | **No abuse controls.** No rate limit or lockout on login, no CSRF defence, no security headers. Registration answers "Username already exists", which confirms which usernames exist. | `main.py:26-56` |
| S8 | Low | **The Tailwind "play" CDN script is loaded at runtime** from `cdn.tailwindcss.com` with no integrity hash. It is meant for prototyping, adds a third-party script to every page, and the app has no styling offline. | `templates/base.html:7` |

### 5.2 Correctness and design

| # | Severity | Finding | Evidence |
| :-- | :-- | :-- | :-- |
| A1 | Critical | **A fresh install does not work.** `requirements.txt` has no version pins. On current Starlette (1.x) the old `TemplateResponse("page.html", {"request": request})` call form no longer exists, so every HTML page, including the error page, answers 500. 14 of the 25 characterization tests fail on the repo's own virtualenv, and all 25 pass on the pinned legacy versions. `@app.on_event` is deprecated too. | `requirements.txt`; every `TemplateResponse` call in `main.py`; `tests/characterization/requirements-legacy.txt` |
| A2 | High | **Bare `except:` hides every failure as "Username already exists."** A locked database, a full disk or a typo in the SQL would all be reported to the user as a duplicate name. (Not reproduced as a wrong message under 200 concurrent sign-ups; see section 6.) | `main.py:32-33` |
| A3 | High | **Checkout charges everyone the sum of every event.** It reads all fees, ignores which events the user registered for, and the route takes no user, so the page also renders as logged out. There is no order, no payment and no receipt. | `checkout/__init__.py:8`, `checkout/__init__.py:20`, `main.py:111-117`; `test_checkout_totals_every_event_not_the_users_registrations`, `test_checkout_forgets_who_is_logged_in` |
| A4 | High | **Registering twice registers twice.** There is no uniqueness on `(username, event_id)`, so a double click or a retry creates duplicate rows, and the user is shown the same event several times. 100 simultaneous clicks created 100 rows. | `main.py:17`, `main.py:81`; `test_repeated_registration_creates_duplicate_rows`; probe P1 |
| A5 | High | **Registrations can point at events that do not exist**, and at users that do not exist. No foreign keys, no existence check. | `main.py:75-84`; `test_registration_for_a_missing_event_is_accepted` |
| A6 | High | **Nothing limits seats.** The UI promises "limited seats" but `events` has no capacity column, so the central problem of event ticketing (selling more places than exist) cannot even be expressed, let alone prevented. | `templates/events.html:38`, `main.py:16`; `test_registrations_have_no_keys_or_constraints` |
| A7 | Medium | **A crash is built into the product.** `event_id == 404` divides by zero on purpose, and the events page has a red button for it. It belongs to the lab exercise, not to a real app. | `main.py:77-78`, `templates/events.html:49-58` |
| A8 | Medium | **The seed script is not idempotent**, which is how `fest.db` ended up with 20 events, including three "Hackathon" rows and an orphan "Dance". It also runs `CREATE TABLE` itself, duplicating `main.py`. | `insert_events.py:14-30`; `fest.db`; `test_insert_events_is_not_idempotent` |
| A9 | Medium | **Leftover "slow code" and claims about it that are no longer true.** Two commented-out busy loops remain in the handlers, a commented "OLD SLOW WAY" block remains in checkout, and the my-events page still says it "intentionally contains a backend bottleneck". | `main.py:65-67`, `main.py:101-103`, `checkout/__init__.py:10-18`, `templates/my_events.html:51` |
| A10 | Medium | **The load tests measure the wrong thing.** Every user is the same fictional `locust_user`, who is never created. The journey registers event 1 on every iteration, so the table grows without bound and the my-events page slows down as the test runs. The files give no `--host`, and the journey lives in a nested `locust/locust/` folder. | `locust/*.py`, `locust/locust/journey_locustfile.py:8,14` |
| A11 | Medium | **Database connections are never closed.** Each handler calls `get_db()` and drops the connection, relying on garbage collection. The file name `fest.db` is relative, so the database depends on the directory the server is started from. | `database.py:4`, `main.py` (5 call sites), `checkout/__init__.py:4` |
| A12 | Medium | **Schema and behaviour differ between code paths.** Three modules each create tables or assume them (`main.py`, `insert_events.py`). There are no migrations. | `main.py:12-18`, `insert_events.py:6-12` |
| A13 | Low | **No logout.** The "Logout" button is a link to `/login`; nothing ends anything. | `templates/base.html:36`; `test_logout_is_just_a_link_to_login` |
| A14 | Low | **Links break for some usernames.** `?user=` is written into `href`s without URL encoding, so a name containing `&` or `#` produces a wrong link (`a&b#c` renders as `/my-events?user=a&amp;b#c`). | `templates/base.html:27-30`, `templates/events.html:14,41` |
| A15 | Low | **Lab instructions are baked into the UI.** Pages tell the reader to "take a screenshot (crash demonstration)" and mention "Next Lab". Templates index rows by position (`e[0]`, `e[1]`). | `templates/error.html:35`, `templates/checkout.html:7-8,17,30`, `templates/events.html:27-31` |

### 5.3 Data

| # | Severity | Finding | Evidence |
| :-- | :-- | :-- | :-- |
| D1 | High | **The schema has no integrity.** `registrations` has no primary key, no foreign keys, no unique constraint and no timestamps. `events` has no capacity. `users` is keyed by a free-text username, compared case-sensitively. | `main.py:15-17`; `test_registrations_have_no_keys_or_constraints` |
| D2 | Low | **Money is a bare integer with no currency.** The UI hardcodes the rupee sign. | `main.py:16`, templates |
| D3 | Low | **The lab copy's data is junk.** Three registrations belong to an empty username, and there are duplicate events. | `fest.db` |

### 5.4 Repository hygiene

| # | Severity | Finding |
| :-- | :-- | :-- |
| H1 | Medium | No README, so nobody can tell what the project is or how to run it. |
| H2 | Medium | No license, so the code is "all rights reserved" by default. |
| H3 | Medium | No tests, linting, type checking, CI or dependency updates. |
| H4 | Low | `locust` is listed as a runtime dependency of the app. |
| H5 | Low | `.gitignore` is five lines and does not cover `*.db`, `.env` or test output. |

## 6. Measurements

From `scripts/audit_probe_v1.py` against a scratch copy of v1, on the lab's dependency versions
(Windows 11, Intel Core Ultra 7 155H, one uvicorn worker):

| Probe | Result |
| :-- | :-- |
| P1: 100 simultaneous `GET /register_event/1?user=probe_double_click` | 100 of 100 answered `302`; **100 duplicate rows** were created for one user and one event |
| P2: 200 simultaneous sign-ups with distinct usernames | 200 created, **0 false "already exists" answers**. The bare `except:` (A2) did not misreport under this load, so A2 is a latent risk, not an observed failure |
| P3: checkout asked three times | identical page each time, with no user in it |

The probe was run once on this machine. The numbers describe v1 and are not benchmarks of the
rebuilt app; the rebuild's benchmarks will come from the load-test scripts added in later phases.

## 7. What this means for the rebuild

1. **The app cannot be extended in place.** It does not install on current dependencies (A1),
   has no identity model (S2) and has no place to put the core of an event-ticketing system
   (inventory, orders, payments: A3, A6, D1).
2. **The domain is exactly the one where concurrency matters**, and v1 has no concurrency control
   at all (A4, A6). That makes it a good baseline: the naive version already exists, so the rebuild
   can show an oversell happen and then show it cannot.
3. **The Locust files are worth keeping** but need real users, unique registrations and a host
   (A10), so they test the journeys instead of one growing table.
4. **Things the course may require** (SRN in `main.py`, the four Locust files and their paths, the
   `/events`, `/register_event/{id}`, `/my-events`, `/checkout` routes, a monolithic structure) are
   listed as questions in [roadmap.md](roadmap.md#decisions-needed) and are kept working until
   answered.

## 8. Resolution status

Updated at the end of each phase. "Test" names the test that proves the fix; v1's behaviour stays
pinned in `tests/characterization/` against `legacy/v1/`.

### After phase 1 (foundation)

| # | Status | How |
| :-- | :-- | :-- |
| S1 | Fixed | Argon2id hashes (`test_password_is_hashed_with_argon2id`) |
| S2 | Fixed | Server-side sessions, `POST` for writes, CSRF tokens, `?user=` ignored (`test_anonymous_visitors_are_sent_to_sign_in_not_to_a_registration`, `test_the_user_query_parameter_no_longer_means_anything`) |
| S3 | **Open, your call** | The SRN is still at `main.py` line 8-ish, untouched, until you decide. The username/password row existed only in the lab folder's `fest.db`, which is no longer in the tree |
| S4 | Fixed | `*.db` is ignored and the database file is no longer in the project folder. It never reached GitHub's `main` |
| S5 | Fixed | Error pages show a request ID, never exception text (`test_errors_do_not_show_exception_text`) |
| S6 | Fixed | Username and password rules (`test_empty_and_malformed_credentials_are_rejected`) |
| S7 | Partly fixed | Login throttling per (username, client), CSRF and security headers are in. Sign-up still says when a username is taken, which is a usability trade-off |
| S8 | Fixed | No third-party requests; strict CSP (`test_pages_make_no_third_party_requests`) |
| A1 | Fixed | Locked dependencies, tested on current FastAPI and Starlette, Python 3.13 |
| A2 | Fixed | No bare `except`; the duplicate-user case is an `ON CONFLICT` clause |
| A3 | Fixed | Checkout totals the signed-in user's events only (`test_checkout_totals_only_my_events`) |
| A4 | Fixed | Unique `(user_id, event_id)` (`test_registering_twice_is_harmless`, `test_a_double_click_storm_registers_once`) |
| A5 | Fixed | Foreign keys and a 404 (`test_registering_for_a_missing_event_is_a_404`) |
| A6 | Fixed for registration | Capacity column, `CHECK` constraint and an atomic conditional `UPDATE` (`test_a_rush_for_a_few_seats_never_oversells`). Phase 2 compares this with the other strategies under load |
| A7 | Fixed | The crash route exists only with `FESTRUSH_DEBUG=true` |
| A8 | Fixed | Idempotent seed (`test_seeding_twice_does_not_duplicate_events`) |
| A9 | Fixed | Slow-code leftovers are gone from the app; they remain only in the archived `legacy/v1/` |
| A10 | Fixed | One new account per simulated user, host documented, the four files keep their paths |
| A11 | Fixed | Pooled connections used as context managers; no relative database path |
| A12 | Fixed | Numbered migrations and one runner |
| A13 | Fixed | `POST /logout` deletes the session (`test_logout_ends_the_session`) |
| A14 | Fixed | The username no longer appears in URLs |
| A15 | Fixed | Lab instructions removed from the UI |
| D1 | Fixed | Keys, foreign keys, uniqueness, capacity (`test_the_database_refuses_orphans_and_duplicates`) |
| D2 | Open (low) | Fees stay whole rupees as an integer; documented in the schema |
| D3 | Fixed | The lab copy's database is no longer in the project folder |
| H1-H5 | Fixed | README, MIT license, tests, lint, types, CI and Dependabot, `locust` moved to dev dependencies, `.gitignore` extended |

### After phase 2 (the concurrency core)

| # | Status | How |
| :-- | :-- | :-- |
| A4 | Fixed under load | The unique active reservation per student and event holds when requests race (`test_a_double_click_storm_holds_one_seat`), and retries with an idempotency key take effect once (`tests/test_idempotency.py`, the retry-storm run in [concurrency.md](concurrency.md)) |
| A6 | Fixed under load | Four strategies, three of which never oversold in the benchmark runs; the naive one did. Measured in [concurrency.md](concurrency.md) |
| A3 | Extended | Checkout now creates an order and a payment. Only the signed-in student's live holds are charged, and the payment moves through a state machine the database enforces |
| D1 | Extended | Reservations, orders, payments, the outbox and idempotency keys all have keys, foreign keys and checks; order and reservation status moves are validated by triggers |
