"""One person, one name: placeholder Launchpad accounts folded into the real one.

cairn names people by Launchpad account, never by email: publications link
to ~name and that is all it reads. When an upload's changelog address
belongs to no account, Launchpad makes one for it (IPersonSet.ensurePerson):
unactivated, the email hidden, the changelog's display name, and a name from
generate_nick, which is the address's local part with "." and "_" turned
into "-", and "-" plus random characters after it when that is taken
(lp/registry/model/person.py). So a developer who uploads as
user@debian.org while their account holds user@ubuntu.com becomes two
people in cairn, with two owner pages and their work split between them.

Measured on 26 Nov 2026 in packages.json, every one checked on Launchpad:

  placeholder   account    how the name was made
  nadzeya-s     nadzeya    taken, "-s" added
  superm1-m     superm1    taken, "-m" added
  charles05-d   charles05  taken, "-d" added
  ivan-hu       ivan.hu    "." in the address became "-"

Each placeholder was "Unactivated" with the same display name as an Active
account. That is the rule here, and every part of it must hold:

- the placeholder's name is what generate_nick makes from the account's
  name: the same with "." and "_" as "-", optionally with up to three
  characters added before or after a "-";
- the placeholder is a person account Launchpad created and nobody
  activated, and the other is an active person;
- both carry the same display name.

The name rule alone would fold unrelated people (two different "ivan"s), and
the display name alone would fold namesakes; together, with an account
nobody ever logged into, they describe one person who signed with a second
address. Teams are never folded. Launchpad's own merge, when a person claims
the address, makes the placeholder disappear from publications, after which
nothing here applies.

Primary data under AGENTS.md section 4: Launchpad is the system of record
for accounts. One request per account in a candidate pair, a handful a run;
a pair already confirmed is reused without asking again.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from cairn.ingest.base import Fetcher
from cairn.ingest.publications import LAUNCHPAD

log = logging.getLogger(__name__)

# AccountStatus titles (lp/services/identity/interfaces/account.py) for an
# account nobody has used: NOACCOUNT reads "Unactivated", which is what
# ensurePerson leaves, and PLACEHOLDER only holds a username.
UNUSED = frozenset({"Unactivated", "Placeholder"})

# generate_nick's first attempts add one character a try, alternating
# suffix and prefix; three covers everything measured with room to spare,
# and longer would start matching ordinary hyphenated names.
MOST_ADDED = 3


def nick(name: str) -> str:
    """What generate_nick makes of a local part equal to this name."""
    return name.replace(".", "-").replace("_", "-")


def candidates(names: Iterable[str]) -> list[tuple[str, str]]:
    """(placeholder, account) pairs whose names fit generate_nick.

    Only names cairn already holds: a person split in two matters when both
    halves would get a page.
    """
    found = sorted(set(names))
    by_nick: dict[str, list[str]] = {}
    for name in found:
        by_nick.setdefault(nick(name), []).append(name)
    pairs = []
    for name in found:
        if "+" in name:
            continue  # team+python and the like: Debian team addresses
        bases = [name]
        head, _, tail = name.rpartition("-")
        if head and 0 < len(tail) <= MOST_ADDED:
            bases.append(head)
        head, _, tail = name.partition("-")
        if tail and 0 < len(head) <= MOST_ADDED:
            bases.append(tail)
        for base in bases:
            for account in by_nick.get(base, ()):
                if account != name:
                    pairs.append((name, account))
    return sorted(set(pairs))


@dataclass(frozen=True, slots=True)
class Account:
    name: str
    display_name: str
    status: str
    is_valid: bool
    is_team: bool


def account_url(name: str) -> str:
    return f"{LAUNCHPAD}/~{name}"


def fetch_account(fetcher: Fetcher, name: str) -> Account:
    raw = json.loads(fetcher.get(account_url(name)))
    return Account(
        name=name,
        display_name=str(raw.get("display_name") or ""),
        status=str(raw.get("account_status") or ""),
        is_valid=bool(raw.get("is_valid")),
        is_team=bool(raw.get("is_team")),
    )


def same_person(placeholder: Account, account: Account) -> bool:
    return (
        not placeholder.is_team
        and not account.is_team
        and not placeholder.is_valid
        and placeholder.status in UNUSED
        and account.is_valid
        and bool(placeholder.display_name.strip())
        and placeholder.display_name.strip().casefold()
        == account.display_name.strip().casefold()
    )


def aliases(
    fetcher: Fetcher,
    names: Iterable[str],
    *,
    known: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """{placeholder: account} for the people in names.

    A pair confirmed before (known) is kept without asking Launchpad again:
    an account does not stop being someone's. It is kept even when this
    run's names no longer pair it, say because the real account made no
    upload still published, so the placeholder's remaining uploads stay
    with their owner (review on #29). A pair Launchpad cannot be
    asked about this run is left unfolded rather than failing the refresh:
    the cost is one run of split pages, not stale ownership everywhere.
    """
    known = known or {}
    folded: dict[str, str] = dict(known)
    cache: dict[str, Account] = {}

    def account(name: str) -> Account:
        if name not in cache:
            cache[name] = fetch_account(fetcher, name)
        return cache[name]

    for placeholder, other in candidates(names):
        if placeholder in folded:
            continue
        try:
            if same_person(account(placeholder), account(other)):
                folded[placeholder] = other
        except Exception as exc:  # noqa: BLE001 - leave this pair unfolded
            log.warning("people: cannot check %s / %s: %s", placeholder, other, exc)
    # Never a chain: an account that is itself folded away is no target.
    return {p: a for p, a in folded.items() if a not in folded}


def resolve(name: str | None, folded: Mapping[str, str]) -> str | None:
    return folded.get(name, name) if name else name
