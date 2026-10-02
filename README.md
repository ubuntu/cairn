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

[HACKING.md](HACKING.md) covers running Cairn locally and how it is deployed.
[AGENTS.md](AGENTS.md) explains why it is built the way it is.
