# cairn

Context document for anyone — human or AI agent — working on this repository.
Read this before writing code. It records not just *what* the architecture is
but *why*, and which alternatives were rejected and on what evidence, so that
decisions are not silently re-litigated or accidentally reversed.

---

## 1. The problem

Almost everything an Ubuntu developer needs is public. That is the important
part. But it lives on a different host for every kind of question, each with its
own conventions, and none of them talk to each other:

| Question | Where you have to look |
| --- | --- |
| bugs, uploads, builds, merge proposals | Launchpad |
| is my library transition done? | transitions.ubuntu.com |
| is my SRU verified? | static-reports.ubuntu.com/pending-sru |
| why is my package stuck in proposed? | ubuntu-archive-team.ubuntu.com/proposed-migration |
| what binaries are no longer built? | static-reports.ubuntu.com/nbs |
| does this need a merge from Debian? | merges.ubuntu.com |
| who sponsored what? | udd.debian.org (hosted by Debian) |

There is no single view of what needs your attention. Keeping track means
checking six or seven places by habit and knowing which one answers which
question. That is a real cost for an experienced developer and a barrier to
entry for a newcomer.

## 2. What cairn is

A single view of Ubuntu archive health, assembled from those sources.

> A cairn is built from many separate stones, by many different people passing
> through, and it exists for one reason: to show the next traveller where the
> path goes.

### The core insight — cairn remembers

**Every upstream source is a stateless snapshot.** They tell you what is broken
*now*. None of them remember what was broken last week, or that a package has
been stuck through three re-uploads.

That memory is cairn's reason to exist. It is not a prettier aggregator — it is
the thing that accumulates. Upstream pages are the terrain; cairn is the marks
left by everyone who passed before.

Everything in the architecture below follows from that one requirement. If a
proposed change does not serve "cairn remembers," it is probably out of scope.

### Scope

In scope: package health signals, ownership routing, history and trends.

Out of scope for now: anything that writes to Launchpad or the archive. cairn is
read-only and observational. It tells you what needs attention; it does not act.

**On a merge board:** do not build one. Merge candidates are computed from the
Debian and Ubuntu archive indexes (section 4) and enter as an ordinary
`needs_merge` signal. A separate merge board would recreate exactly the
fragmentation cairn exists to remove.

---

## 3. Architecture

**Python end to end. Append-only history in git. SQLite as a build artifact.
Static HTML out. No server.**

```
ingest  →  reconcile  →  data/signals.jsonl  →  build SQLite  →  render static HTML
(fetch,    (diff vs     (append-only,          (replay the      (Jinja + Vanilla)
 parse,     last run)    committed,             log)
 normalize)              source of truth)
```

Ingestion emits the **current** set of signals. Reconciliation diffs that
against last known state and appends only **changes** to the log. The build
replays the log into SQLite and renders pages.

### Why each piece

**Python throughout.** Three separate reasons, in descending order of strength.

*1. Debian version comparison cannot be hand-rolled.* Version strings do not
compare like strings. `~` sorts before everything, so `3.0~a57` is a pre-release
and is **older** than `3.0`. An epoch dominates everything after it, so `1:1.0`
is **newer** than `2.0`. Numeric runs compare as numbers, so `-1ubuntu1` is
**older** than `-10`. Naive string comparison gets all of these backwards:

| A | B | naive `>` | Debian | |
| --- | --- | --- | --- | --- |
| `3.0~a57-1ubuntu56` | `3.0-1` | A > B | A < B | wrong |
| `1.0~beta1` | `1.0` | A > B | A < B | wrong |
| `1:1.0` | `2.0` | A < B | A > B | wrong |
| `1.0-1ubuntu1` | `1.0-10` | A > B | A < B | wrong |

cairn performs this comparison constantly — is proposed newer than release, is
Ubuntu behind Debian, does this SRU supersede that one. It underpins roughly half
the signals. Getting it wrong raises no exception; it silently publishes wrong
answers, which is fatal for a tool whose whole pitch is "trust this instead of
checking seven tabs." `python-debian` / `apt_pkg` implement the real algorithm
and are Python-only. **Never hand-roll this comparison.**

*2. Two supporting libraries.* `launchpadlib` is Launchpad's own client, and
`lxml` is available if any source still needs HTML parsing. Under the section 4
source policy, transitions should be computed from ben config plus archive
indexes rather than scraped, so `lxml` may end up unused — do not treat it as
load-bearing. Both are replaceable; they only add weight.

*3. Therefore a second language does not buy what it appears to.* Because of
reason 1, cairn cannot be Python-free. Writing the web layer in Rust or Go would
not produce a Rust project — it would produce a Python project that also contains
Rust, with Python still in the repo, still in CI, and still in every
contributor's setup. That is the full cost of two languages (two toolchains, two
CI pipelines, two dependency sets, contributors needing both) in exchange for a
goal that is not actually reached. A second language only wins this argument if
it can *replace* Python here, and it cannot.

**Append-only JSONL, committed to git.** History is the whole differentiator, and
this makes it diffable, reviewable and git-native. The archive team reached the
same conclusion independently: `nbs.csv` is a `time,removable,total` trend series
in 7.8 KB. Follow that precedent.

**SQLite as a build artifact, never committed.** Fully rebuildable by replaying
the log, so there is no binary churn in git. Gives real SQL for the joins that
make the unified view possible.

**Static output.** Removes the entire operational surface — no server, no
database to run, no secrets. Proven for this exact domain by `auto-mp-reviewer`
(a sibling project: Python script → committed JSON → GitHub Pages, two
dependencies total).

### Deliberately deferred — do not build these without the trigger

| Deferred | Build it when |
| --- | --- |
| FastAPI over the same SQLite file | a query must be live: full-text search, arbitrary per-developer filters |
| PostgreSQL | `signals.jsonl` exceeds ~100 MB, or concurrent writers appear |
| Rust or Go rewrite | there is a running service with real traffic and a *measured* reason |

Rust is **postponed, not rejected.** The project's eventual home may prefer
Go/Rust, and the seam is deliberately placed so that swapping the render/serve
layer costs templates only — the ingestion layer and the log format are
language-neutral. Rewriting a render script is cheap. Rewriting a system you
guessed at is not.

### Measured signal volume

Counted live from the current archive, as a sanity check on the static approach:

| Source | Active signals |
| --- | --- |
| proposed-migration (entries not a candidate) | ~1,080 of ~1,335 |
| merge candidates (main + universe) | ~690 |
| NBS binaries | ~675 |
| pending SRU (all series) | ~215 |
| transitions, sponsorships, LP | not yet counted |

That is roughly **2,700 active signals**, comfortably under the 5–10k first
guess. At a few hundred bytes each the rendered payload is low single-digit MB,
so the static approach holds with room to spare.

The remaining unknown is history growth, not breadth: the append-only log grows
with every state change, forever. Re-measure when it passes 50 MB. Grouping
pages are bounded — 51 distinct package set names and a few hundred teams — so
pre-rendering one page per group stays cheap.

---

## 4. Source policy — primary data, not other people's dashboards

**Rule: ingest the data a report is computed *from*, not the report.**

cairn exists to replace the seven-tab habit. If it reads those seven pages, it is
a dashboard of dashboards: it inherits their outages, their refresh lag, their
schema churn, and it can never show anything they do not already show. Worse, it
cannot answer *why* — only repeat a verdict.

Primary data means the archive itself, Launchpad's API, the autopkgtest API, and
the Debian archive. All verified reachable and anonymous.

### The one exception — decision engines

Some outputs are not *descriptions* of truth, they are *constitutive* of it.
Britney's migration verdict is the clearest case: a package migrates because
britney said so. If cairn recomputed migration and disagreed, cairn would be
wrong even if its logic were better.

So the distinction is not "derived vs primary," it is:

- **Report generators** (`sru-report`, `nbs-report`, Merge-o-Matic's HTML, the
  transitions pages) — these are dashboards. Replace them. Compute from primary
  data instead.
- **Decision engines** (britney) and **systems of record** (Launchpad, UDD,
  autopkgtest) — these own their data. Read them.

Britney is not a tool cairn replaces. It is archive infrastructure, in the same
category as Launchpad. Recomputing it is infeasible anyway — verified from its
source, its verdict depends on autopkgtest results, `bugs.debian.org`,
`tracker.debian.org`, `buildd.debian.org` and raw archive indexes, while touching
`api.launchpad.net` in only three peripheral places.

### Verified primary inputs

Raw data. Nothing here is a report, and nothing here is a page cairn replaces.

| Need | Endpoint | Size | Notes |
| --- | --- | --- | --- |
| Ubuntu archive state | `archive.ubuntu.com/ubuntu/dists/<series>[-proposed]/<component>/source/Sources.gz` | ~1.3 MB per component | Authoritative published versions. Every stanza carries `Binary:` listing what it builds — that field is what makes NBS computable. Also `binary-<arch>/Packages.gz`. |
| Debian archive state | `deb.debian.org/debian/dists/unstable/main/source/Sources.xz` | ~12 MB | Needed for merge detection. |
| Test results | `autopkgtest.ubuntu.com/results/autopkgtest-<series>/?format=json` | ~2 MB | `queues.json` (~370 KB) shows pending runs. |
| Bugs, uploads, queues, builds, MPs, package sets, teams | `api.launchpad.net/devel` | varies | System of record. Anonymous reads work. No `Access-Control-Allow-Origin`, so the browser cannot call it — a second reason fetching happens at build time. Slow; stalls of ~270 s observed. |
| Sponsorships | `udd-mirror.debian.net:5432` | PostgreSQL | Debian's own mined data; there is no source closer to the origin. The CGI's `format=json` is ignored and returns HTML — do not scrape it. |

### The single non-primary input

One source is consumed despite not being primary, under the decision-engine
exception above. It is listed separately so it is never mistaken for raw data:

| Need | Endpoint | Size | Why it is here |
| --- | --- | --- | --- |
| Migration verdict | `ubuntu-archive-team.ubuntu.com/proposed-migration/update_excuses.yaml` | 1.7 MB xz → 26 MB | Britney's output. A package migrates *because britney said so*, so this is the verdict, not a description of it. Served as `application/x-xz` despite the `.yaml` name. ~1,335 entries, ~1,080 blocked. |

Note what cairn replaces here: `update_excuses_by_team.html` — the *page*. The
YAML underneath is britney's structured output and is the thing worth keeping.
When displaying a blocked package, show britney's verdict as the verdict, and
compute the *explanation* (failing tests, missing builds) from the primary inputs
above. That separation is what lets cairn answer "why" instead of relaying
"blocked".

### How each signal is derived

This is the table an implementer needs. Every signal kind, what it is computed
from, and which published report validates it as an oracle.

| Signal kind | Computed from | Oracle |
| --- | --- | --- |
| `needs_merge` | Ubuntu `Sources` vs Debian `Sources`, compared with Debian version ordering | `merges.ubuntu.com/{main,universe}.json` |
| `nbs` | binaries in `Packages.gz` that no current source's `Binary:` field claims | `static-reports.ubuntu.com/nbs/` |
| `sru_pending`, `sru_verification_failed` | LP bugs with `verification-*` tags + `-proposed` queue + archive state | `static-reports.ubuntu.com/pending-sru/sru_report.yaml` |
| `migration_blocked` | britney verdict (consumed); explanation from autopkgtest + archive + LP builds | — verdict is itself authoritative |
| `transition_*` | ben config from the transition tracker + archive state | `transitions.ubuntu.com` |
| `sponsorship_pending` | UDD | — UDD is the system of record |
| `build_failed`, `bug_*` | LP API | — LP is the system of record |

### Keep the old reports as oracles, not inputs

The published reports remain useful for **validation**. When cairn computes NBS
or merge candidates from primary data, diff the result against
`static-reports.ubuntu.com/nbs/` and `merges.ubuntu.com/*.json` and alert on
divergence. That is how you earn confidence that the reimplementation is correct,
and it is the honest way to find out when it is not. Never let an oracle become
an input — if a computation cannot stand without it, it is not primary.

### What this costs

This is a deliberate trade of complexity for independence:

- cairn must parse archive indexes and do full-archive dependency analysis,
  which is real work. It raises the weight of the Debian version comparison
  rule in section 3 from "important" to "load-bearing."
- Bandwidth per run goes from a few MB to a few tens of MB. Cache aggressively;
  honour `If-Modified-Since` on the archive indexes.
- NBS and merge detection become cairn's own algorithms, with cairn's own bugs.
  Hence the oracles above.

The gain is that cairn can explain *why*, not just relay *what* — and that it
keeps working when a report generator breaks.

---

## 5. The signal model

The single most important design rule:

> **Do not model each source as its own table with its own schema.**
> Normalize everything into one observation type.

Two raw records illustrate why. Both describe "a package needing attention," yet
the package field is `pkg` in one and `source_package` in the other, the person
is `creator` versus `user`, and **both have a field called `age` meaning
different things** (783 days in proposed vs 5138 days since the Debian version
diverged). Those two records came from `sru_report.yaml` and Merge-o-Matic —
which section 4 now treats as oracles rather than inputs, but the lesson holds
for primary data too: the archive indexes, the Launchpad API and the autopkgtest
API each have their own vocabulary. Normalizing them is the entire job of the
ingest layer.

A `Signal` is one observation about one package from one source. The ingest base
module will carry the authoritative definition once written; until then this
section is the specification.

Identity is `(kind, source_package, binary_package, series)`. That tuple is
hashed into a stable `signal_id` used to match observations across runs — which
is what makes `first_seen` and `resolved_at` possible.

### Vocabulary discipline

- **Kinds** (`needs_merge`, `sru_verification_failed`, …) are defined in
  `base.py` and nowhere else.
- **Severity** is assigned by `core/rules.py`, never inside an ingester. Every
  source must inherit byte-identical rules; that is the point of normalizing.
  Deciding that `verification-failed` means `HIGH` is a product judgement — it
  belongs in one reviewable place.
- **Human labels** live only in templates. Ingesters produce data, not prose.
- Preserve the source's own wording in `payload` for display and debugging, but
  never branch on it outside its own ingester.

Never let "stale" (a package with no activity) and "out of date" (cairn's own
pipeline has not run) share a word. Distinguish them in code and in the UI.

### Ownership — two axes, never conflated

The per-developer view depends on this, and Launchpad exposes ownership through
two *different* mechanisms that are easy to mistake for one:

| | Package set | Team subscription |
| --- | --- | --- |
| Means | who may **upload** (an ACL) | who is **responsible** (bugs) |
| API | `lp.packagesets.getBySeries(distroseries=…)` then `ws.op=getSourcesIncluded` | `~team?ws.op=getBugSubscriberPackages` |
| Keyed by | `(name, series)` — sets are per-series | team name — not per-series |
| Scale | 51 distinct names, ~45 per series | hundreds of teams |
| Example | `desktop-core` → 320 sources | `debcrafters-packages` → 75+ sources |

Both are anonymous reads from the Launchpad API, so both are primary under
section 4.

**The trap:** `ubuntu-desktop` exists on *both* axes — a team and a package set
sharing one name, covering different package populations. Model them as one flat
"group" and that name silently merges two sets, after which the per-developer
view is quietly wrong. This is the same class of bug as the two incompatible
`age` fields above. Keep them in separate tables with separate keys.

A corollary worth stating because it has already caused one wrong conclusion:
`debcrafters-packages` is a **team**, not a package set. Grouping pages published
elsewhere (for example the per-team version pages on `people.canonical.com`) are
built from the team axis, so do not expect to find them via `getBySeries`.

The resulting mapping, which is all the per-developer view needs:

```
package   → package sets   (upload rights, per-series)
package   → teams          (responsibility)
developer → teams          (LP team membership)
```

**Pagination.** Launchpad collections paginate. Follow `next_collection_link`
rather than trusting the first page — reading one page of `/package-sets` and
concluding the list was short is a mistake that has already been made once during
this project's design.

---

## 6. How to add a new source

0. **Check it is primary.** Apply section 4: is this the data a report is
   computed from, or the report? If it is a report generator's output, find its
   inputs instead. If it is a decision engine or system of record, it qualifies.
   Record the answer in the module docstring so the next person does not have to
   re-derive it.
1. Create `cairn/ingest/<source>.py` with a class subclassing `Ingester`.
2. Implement `fetch()` → raw bytes/rows, and `parse(raw)` → `list[Signal]`.
3. Add any new `Kind` constants to `base.py`. Do not invent kinds locally.
4. If the source implies severity, express it in `core/rules.py`, not in the
   ingester.
5. Register the module in `cairn/ingest/__init__.py`.
6. Add a fixture under `tests/fixtures/` — a trimmed real response, not
   hand-written — and a parse test.
7. If this ingester reimplements a published report, wire up the corresponding
   oracle check from section 4 and record the current divergence.

An ingester never writes to the log, never assigns severity, and never renders.
It translates one source into `Signal` objects. That is all.

---

## 7. Conventions

**Error posture: degrade to data, never crash.** Borrowed from `auto-mp-reviewer`
and validated there. One source failing must not lose the others. A failed
ingest carries forward the last known signals marked stale, and the UI says so
plainly. Exit non-zero only if *every* source failed.

**Network rules.** Explicit connect/read timeouts on every request. Bounded
retries with exponential backoff and jitter on `{429, 500, 502, 503, 504}`.
Prefer a short read timeout and a retry over waiting on a stall — Launchpad has
been observed stalling for ~270 s. Disable urllib3's internal retries so there is
one visible retry path.

**Style.** `from __future__ import annotations`, PEP 604 unions, dataclasses,
type hints throughout. Module docstrings should record *measured evidence*
(status codes, sizes, latencies) rather than assertions. Mark every intentionally
broad `except` with `# noqa: BLE001` and a reason.

**Never hand-edit `data/signals.jsonl`.** It is append-only. Corrections are new
events, not rewrites. Never edit `site/` — it is generated.

**Frontend.** Canonical [Vanilla](https://vanillaframework.io/) for CSS,
server-rendered Jinja templates. Avoid JavaScript; there is no build step and no
framework. Colour must never be the sole carrier of meaning — pair it with text
or an icon, and check contrast.
