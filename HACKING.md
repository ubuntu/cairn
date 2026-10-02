# Hacking on cairn

Running it locally, and how it gets published.

## Running it

Both paths produce the same thing: a static site in `site/`.

### With Docker

Nothing needed on the host but Docker.

```console
$ docker compose run --rm cairn build
$ docker compose up site
```

The site is then on <http://localhost:8080>. `cairn build` replays the log
that is already committed in `data/`, so this works without touching the
network.

To collect fresh data:

```console
$ docker compose run --rm cairn ingest --dry-run
$ docker compose run --rm cairn ingest
```

### Without Docker

Needs Python 3.13 or newer and [Poetry](https://python-poetry.org/). Note that
Ubuntu 24.04 ships Python 3.12, so on the current LTS you will want either the
Docker path above or a newer interpreter from `pyenv` or deadsnakes.

```console
$ poetry install
$ poetry run cairn build
$ python -m http.server -d site 8080
```

## The commands

`cairn ingest` fetches every source and appends what changed to the log. It is
the only thing that writes. `--dry-run` reports without writing, `--series`
overrides the auto-detected development series, and `--source` runs a single
source and can be repeated.

It exits non-zero only when every source fails, because one broken source must
not look like a broken run.

`cairn build` replays the log and renders `site/`. `--out` puts it somewhere
else.

## How the pipeline fits together

```
ingest  →  reconcile  →  data/signals.jsonl  →  render static HTML
(fetch,    (diff vs     (append-only,          (Jinja + Vanilla)
 parse,     last run)    committed,
 normalize)              source of truth)
```

Ingestion emits the current set of signals. Reconciliation compares that
against the last known state and appends only the differences. Rendering
replays the whole log to recover `first_seen`, `occurrences` and the rest.

## The log

`data/signals.jsonl` is the only thing in the repository that cannot be
regenerated. One JSON object per line, each recording a single state change:
opened, updated or resolved.

It is append-only. Corrections are new events rather than rewrites, and it is
never hand-edited. If you need to change what the log says, add to it.

`data/health.jsonl` records the outcome of each run separately. That is
deliberate: without it there is no way to tell "this package has not changed"
apart from "cairn failed to collect it", and those mean very different things
to someone reading the page.

Everything else is generated. `site/` and `.cache/` are gitignored and never
committed.

## Tests

```console
$ poetry run pytest
$ poetry run ruff check . && poetry run ruff format --check .
```

## Deployment

There is no server. Three workflows do the work.

`tests.yaml` runs ruff and pytest on every push and pull request.

`ingest.yaml` runs twice a day, at 05:37 and 17:37 UTC, calls `cairn ingest`, and commits
`data/` back to `main`. The odd minute is deliberate, since GitHub is most
likely to delay or drop scheduled jobs on the hour. History cannot be
backfilled, so a run that does not happen is a permanent gap, which is why
this belongs to the repository rather than to anyone's laptop.

`publish.yaml` runs after an ingest finishes, or when the templates change,
and deploys the rendered site to GitHub Pages. It is triggered by
`workflow_run` rather than by the push, because commits made with
`GITHUB_TOKEN` deliberately do not trigger further workflows.

Pages must be configured with its source set to GitHub Actions for the deploy
to land.

## Before you change anything

Read [AGENTS.md](AGENTS.md). It records why the architecture is what it is and
which alternatives were rejected on what evidence. Two rules in particular
will save you time: ingest primary data rather than other people's dashboards,
and never hand-roll Debian version comparison.
