# cairn

A single view of Ubuntu archive health.

> A cairn is built from many separate stones, by many different people passing
> through, and it exists for one reason: to show the next traveller where the
> path goes.

Almost everything an Ubuntu developer needs is already public. The problem is
that it lives somewhere different for every kind of question. Launchpad for
bugs and uploads, `transitions.ubuntu.com` for library transitions,
`static-reports.ubuntu.com` for SRUs and NBS, `ubuntu-archive-team.ubuntu.com`
for anything stuck in proposed. Keeping track means checking six or seven
places by habit and knowing which one answers which question.

cairn puts those signals on one page.

## cairn remembers

This is the part that makes it worth building. Every one of those sources is a
snapshot: it tells you what is broken right now. None of them remember what
was broken last week, or that a package has already been stuck through three
re-uploads.

cairn keeps a record of every change it sees. So it can answer the questions
the upstream pages cannot. How long has this been waiting? Is this the second
time it has come back? Is the backlog growing or shrinking?

## What it shows today

Merge candidates: packages where Debian has moved ahead of Ubuntu and someone
needs to merge the difference. These are computed from the Ubuntu and Debian
archive indexes directly, rather than read off an existing report, which is
what lets cairn explain *why* a package is a candidate instead of just listing
it.

Still to come: pending SRUs, packages blocked in proposed, binaries no longer
built, and stalled sponsorships.

## Status

Early, and honest about it. The log started on 29 September 2026, so anything
that depends on history will look thin for a few weeks yet. That is the nature
of a tool whose whole value accumulates: it has to run for a while before it
can tell you anything the archive cannot.

## More

[HACKING.md](HACKING.md) covers running cairn locally and how it is deployed.
[AGENTS.md](AGENTS.md) explains why it is built the way it is.
