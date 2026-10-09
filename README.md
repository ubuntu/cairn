# Cairn

A single view of Ubuntu archive health: <https://ubuntu.github.io/cairn/>

> A cairn is built from many separate stones, by many different people passing
> through, and it exists for one reason: to show the next traveller where the
> path goes.

Almost everything an Ubuntu developer needs is already public. The problem is
that it lives somewhere different for every kind of question: Launchpad for
bugs and uploads, `ubuntu-archive-team.ubuntu.com` for anything stuck in
proposed, `merges.ubuntu.com` for merges, `static-reports.ubuntu.com` for SRUs,
`transitions.ubuntu.com` for library transitions. Keeping track means checking
six or seven places by habit and knowing which one answers which question.

Cairn puts those answers in one place, and sorts them by who they belong to.
A team, a package set or an uploader gets one page listing what needs their
attention, instead of a long list of everything that they have to filter in
their head. Where two problems concern the same package, such as a merge
waiting on an upload that is stuck in proposed, Cairn links one to the other.

Start from the question you have:

- **What is the status of my package?** Every package has a page with its
  merge, why britney is holding its upload, what it is holding up, who looks
  after it, and its history.
- **What needs my attention?** Find your team, package set or Launchpad name
  under *Browse → Teams and people*.
- **Is my work holding others up?** The *Holding others up* tab on your
  page lists tests of yours that are failing on other people's uploads, and
  stuck uploads waiting for yours. Britney's own page does not show either.
- **What can I work on?** *Up for grabs* lists stuck Debian syncs, abandoned
  uploads and smaller merges that anyone may pick up.
- **I am on +1 maintenance: where do I start?** *Up for grabs → +1
  maintenance shift* orders the archive's unowned work as the +1 guide
  does, starting with the fixes that unblock the most uploads.

Every source Cairn reads only shows the present. Cairn keeps a record from
every run, so it can also show what those sources cannot: a package stuck
through several uploads, one that was merged and came back, and whether a
board is shrinking.

[HACKING.md](HACKING.md) covers running Cairn locally and how it is deployed.
[AGENTS.md](AGENTS.md) explains why it is built the way it is.
