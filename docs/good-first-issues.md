# Good first issues

Each is small, self-contained and has a clear finish line. Open an issue to claim one.

1. **Seat labels on the seat map.** The map counts seats by state; show the physical label from the
   `seats` table for the `skip_locked` strategy. Done when the label appears in a test.
2. **Trace spans in the System Lens.** Show the current request's spans in the drawer
   (`src/festrush/static/live.js`, `src/festrush/routes/live.py`).
3. **Keyboard hint for the seat map.** Add a visible, screen-reader-friendly summary of held and
   sold counts under the map.
4. **Dark-mode check for the seat map.** Add an axe run with `colorScheme: "dark"` in `e2e/`.
5. **`festrush check --json`.** Print the invariant results as JSON for scripts.
6. **Pagination for `GET /api/events`.** Add `limit` and `offset`, with tests and a docs rebuild.
7. **Order history page.** List past orders at `/orders`, reusing `orders.get_order`.
8. **Admin seed options.** Let `festrush seed` take `--events N` and `--capacity N`.
9. **Retry-After on 429.** Return a `Retry-After` header from the login rate limiter.
10. **Replay demo.** Record a real run of the seat rush and publish it on GitHub Pages, labelled as
    a recording.
11. **Locale-aware prices.** Format fees in `events.html` with a currency symbol from settings.
12. **More chaos.** Add a run that pauses the gateway mid-sale and checks the invariants after.
