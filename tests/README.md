# Tests: running fast, writing fast

## Running

```
scripts/test.sh            # unit + integration (-n min(cores,6)), then e2e (-n 3)
scripts/test.sh fast       # unit + integration, -m "not llm and not slow and not eval"
scripts/test.sh unit|integration|e2e
scripts/test.sh -- tests/unit/crm -k keys      # any pytest args
uv run pytest tests/integration/crm -n 3       # by hand
```

Without a `.env` key set `SALES0_ANTHROPIC_API_KEY=sk-test-dummy` and add `-m "not llm"` (the script does both).
Cap workers on an 8 GB laptop: `-n 3` or `-n 4` for integration, `-n 3` for e2e (each e2e worker is a
Chromium plus a server). `addopts` carries `--dist loadgroup`: tests marked
`@pytest.mark.xdist_group("name")` run on the same worker, in order.

Markers: `llm` (real Anthropic call), `browser`, `e2e`, `slow` (opt-in label for the >5 s tests; excluded by
`fast`), `sqlite_only`, `unmigrated`, `eval`.

## Isolation rules (what makes `-n` safe)

* Every test gets its own `tmp_path` data dir and DB (`_isolated_runtime`, autouse). xdist gives every worker its own
  basetemp, so nothing is shared. The `datalock` flock is per data dir, hence per test.
* Never hard-code a port. `tests.support.ports.free_port()` returns a free port from the worker's own slice of
  8790-8999 (8765 is never in the range and is also blocked by the socket guard). `os_free_port()` gives an OS
  ephemeral port (fine for in-process servers). `live_server(...)` swaps in a worker-slice port under xdist
  (read `server.port` / `server.base_url`, do not assume the number you passed).
* Never write outside `tmp_path`/`tmp_path_factory` (no `/tmp`, no repo `data/`).
* Module/session-scoped fixtures are per worker process. Do not mutate them; if a set of tests must share
  mutable state, mark them `xdist_group`.
* With `SALES0_TEST_POSTGRES_URL` every test still clones its own database from a template; names include the
  worker pid so workers do not collide.

## Cheap database

`db_url` (fixture) returns the URL of a fully migrated, private database for the test. For SQLite it copies a
template that was migrated once per worker (about 2 ms instead of 0.7-1.3 s of Alembic per test; for example
`tests/integration/services` went from 135 s to 8 s serial). So:

* Use `db_url` (or `get_settings().resolved_database_url` after requesting it); do NOT call
  `command.upgrade(alembic_config(url), "head")` in a non-migration test.
* Migration tests that exercise Alembic (`tests/integration/db/test_migration_*`) keep calling `command.upgrade`
  on their own URL/revisions; nothing changes for them.
* Debug aid: `SALES0_TEST_MIGRATE_EACH=1` forces the old per-test Alembic run (to check the template is not the
  cause of a failure).

## Helpers

* `no_sleep` fixture: `time.sleep` returns at once; returns the list of requested durations. Opt in
  (`def test_x(no_sleep)`); not autouse because thread-based tests poll with `time.sleep`.
* `fake_clock` fixture (`tests/support/clock.py`): `clock.now()`, `clock.advance(minutes=5)`,
  `clock.sleep` (records and advances, never blocks), `clock.monotonic`. Pass `clock.sleep` / `clock.now` to
  code that takes `sleep=` / `now=`. Most pacing/backoff in `src` already takes an injected `sleep`, so
  prefer `sleep=lambda _s: None` over real waits.
* Seeders: `tests/support/crm_seed.py`, `metrics_seed.py`, `fake_llm.py`, `fake_google.py`, `fake_gcs.py`,
  `research_site.py`.
* Test pacing env (`TEST_ENV` in `conftest.py`) already shrinks scrape delays to 50-150 ms.
