"""Placeholder Launchpad accounts folded into the person's real account.

Fixture: Launchpad's ~person responses on 26 Nov 2026, trimmed to the fields
read. The four pairs are every one found in that day's packages.json.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cairn.ingest import metadata, people
from cairn.ingest.metadata import PackageMetadata, Snapshot
from cairn.ingest.people import account_url, aliases, candidates, nick

ACCOUNTS = json.loads(
    (Path(__file__).parent / "fixtures" / "lp_people.json").read_text()
)
MEASURED = {
    "nadzeya-s": "nadzeya",
    "superm1-m": "superm1",
    "charles05-d": "charles05",
    "ivan-hu": "ivan.hu",
}


class Launchpad:
    def __init__(self, accounts=None, *, down=()):
        self.accounts = ACCOUNTS if accounts is None else accounts
        self.down = set(down)
        self.urls: list[str] = []

    def get(self, url: str) -> bytes:
        self.urls.append(url)
        name = url.rsplit("~", 1)[1]
        if name in self.down:
            raise OSError("launchpad 503")
        return json.dumps(self.accounts[name]).encode()


class TestNames:
    def test_nick_is_launchpads(self):
        """generate_nick turns "." and "_" in the local part into "-"."""
        assert nick("ivan.hu") == "ivan-hu"
        assert nick("a_b") == "a-b"

    @pytest.mark.parametrize(("placeholder", "account"), MEASURED.items())
    def test_every_measured_pair_is_a_candidate(self, placeholder, account):
        assert (placeholder, account) in candidates([placeholder, account])

    def test_a_prefix_counts_too(self):
        """generate_nick tries a prefix after a suffix."""
        assert candidates(["x-doko", "doko"]) == [("x-doko", "doko")]

    def test_unrelated_names_are_not(self):
        assert candidates(["doko", "seb128", "bdrung", "jbicha"]) == []

    def test_more_than_a_few_added_characters_are_not(self):
        """An ordinary hyphenated name, not one Launchpad made."""
        assert candidates(["ivan-smith", "ivan"]) == []

    def test_debian_team_addresses_are_not(self):
        assert candidates(["team+python", "python"]) == []

    def test_only_names_cairn_holds(self):
        """A placeholder whose real account never appears splits nobody."""
        assert candidates(["nadzeya-s"]) == []


class TestLaunchpadConfirms:
    def test_every_measured_pair_is_folded(self):
        names = [*MEASURED, *MEASURED.values(), "seb128"]
        assert aliases(Launchpad(), names) == MEASURED

    def test_a_different_display_name_is_two_people(self):
        accounts = ACCOUNTS | {
            "nadzeya-s": ACCOUNTS["nadzeya-s"] | {"display_name": "Someone Else"}
        }
        assert aliases(Launchpad(accounts), ["nadzeya-s", "nadzeya"]) == {}

    def test_an_activated_account_is_its_own_person(self):
        """Someone logged into it: it is not a placeholder."""
        accounts = ACCOUNTS | {
            "nadzeya-s": ACCOUNTS["nadzeya-s"]
            | {"account_status": "Active", "is_valid": True}
        }
        assert aliases(Launchpad(accounts), ["nadzeya-s", "nadzeya"]) == {}

    def test_an_inactive_target_takes_nothing(self):
        accounts = ACCOUNTS | {
            "nadzeya": ACCOUNTS["nadzeya"]
            | {"account_status": "Deactivated", "is_valid": False}
        }
        assert aliases(Launchpad(accounts), ["nadzeya-s", "nadzeya"]) == {}

    def test_teams_are_never_folded(self):
        accounts = ACCOUNTS | {"nadzeya": ACCOUNTS["nadzeya"] | {"is_team": True}}
        assert aliases(Launchpad(accounts), ["nadzeya-s", "nadzeya"]) == {}

    def test_a_confirmed_pair_is_not_asked_again(self):
        lp = Launchpad()
        found = aliases(lp, ["nadzeya-s", "nadzeya"], known={"nadzeya-s": "nadzeya"})
        assert found == {"nadzeya-s": "nadzeya"}
        assert lp.urls == []

    def test_a_confirmed_pair_outlives_its_names(self):
        """Review on #29: the real account made no upload still published,
        so this run's names no longer pair it; the placeholder's uploads
        must stay with their owner."""
        lp = Launchpad()
        found = aliases(lp, ["nadzeya-s"], known={"nadzeya-s": "nadzeya"})
        assert found == {"nadzeya-s": "nadzeya"}
        assert aliases(lp, [], known={"nadzeya-s": "nadzeya"}) == {
            "nadzeya-s": "nadzeya"
        }
        assert lp.urls == []

    def test_launchpad_down_leaves_the_pair_unfolded(self):
        """One run of split pages, not a failed refresh."""
        lp = Launchpad(down={"superm1-m"})
        found = aliases(lp, ["superm1-m", "superm1", "nadzeya-s", "nadzeya"])
        assert found == {"nadzeya-s": "nadzeya"}

    def test_each_account_is_asked_once(self):
        lp = Launchpad()
        aliases(lp, [*MEASURED, *MEASURED.values()])
        assert len(lp.urls) == len(set(lp.urls)) == 8
        assert account_url("ivan.hu") in lp.urls


class TestCollect:
    """The names in packages.json are the person's, on every field."""

    @pytest.fixture
    def collected(self, monkeypatch):
        from datetime import UTC, datetime

        from cairn.ingest import debian_uploads, packagesets, publications, teams
        from cairn.ingest.publications import Publication

        found = {
            # Measured: audit 1:4.2.1-1.1.2ubuntu1, changelog address with no
            # account of its own, signed by the developer's real one.
            "audit": [
                Publication(
                    "audit",
                    "1:4.2.1-1.1.2ubuntu1",
                    uploaded=datetime(2026, 11, 20, tzinfo=UTC),
                    uploader="nadzeya-s",
                    signer="nadzeya",
                    pocket="Proposed",
                )
            ],
            "fwts": [
                Publication(
                    "fwts",
                    "26.09.00-0ubuntu1",
                    uploader="ivan-hu",
                    signer="ivan.hu",
                    pocket="Release",
                )
            ],
            "lemonade": [
                Publication(
                    "lemonade",
                    "2026.39.1-2",
                    uploader="superm1-m",
                    signer=None,
                    pocket="Proposed",
                    requester="superm1",
                )
            ],
            "shadow": [
                Publication(
                    "shadow", "1:4.19.3-2ubuntu1", uploader="nadzeya", signer="seb128"
                )
            ],
        }
        monkeypatch.setattr(publications, "fetch_all", lambda *a, **k: found)
        monkeypatch.setattr(packagesets, "fetch", lambda *a, **k: {})
        monkeypatch.setattr(teams, "fetch", lambda *a, **k: {})
        monkeypatch.setattr(debian_uploads, "fetch", lambda *a, **k: {})
        return metadata.collect(Launchpad(), "tenacious")

    def test_the_uploader_is_the_person(self, collected):
        audit = collected.get("audit")
        assert audit.uploader == "nadzeya"
        assert audit.uploader_of("1:4.2.1-1.1.2ubuntu1") == "nadzeya"

    def test_one_person_one_name_across_packages(self, collected):
        """audit and shadow now route to the same owner page."""
        assert collected.get("audit").uploader == collected.get("shadow").uploader

    def test_a_dot_in_the_address(self, collected):
        assert collected.get("fwts").uploader == "ivan.hu"

    def test_a_sync_still_reads_as_a_sync(self, collected):
        """Only names change: the signer stays absent."""
        [sync] = collected.get("lemonade").publications
        assert sync.synced
        assert sync.uploader == "superm1"
        assert sync.requester == "superm1"

    def test_the_snapshot_records_what_it_folded(self, collected):
        assert collected.aliases == {
            "nadzeya-s": "nadzeya",
            "ivan-hu": "ivan.hu",
            "superm1-m": "superm1",
        }


class TestSnapshot:
    def test_aliases_survive_a_save(self, tmp_path):
        path = tmp_path / "packages.json"
        snapshot = Snapshot(
            packages={"audit": PackageMetadata(uploader="nadzeya")},
            aliases={"nadzeya-s": "nadzeya"},
        )
        metadata.save(path, snapshot)
        assert metadata.load(path).aliases == {"nadzeya-s": "nadzeya"}

    def test_a_snapshot_from_before_has_none(self, tmp_path):
        path = tmp_path / "packages.json"
        path.write_text(json.dumps({"collected_at": None, "packages": {}}))
        assert metadata.load(path).aliases == {}


def test_resolve_leaves_everyone_else_alone():
    assert people.resolve("nadzeya-s", MEASURED) == "nadzeya"
    assert people.resolve("seb128", MEASURED) == "seb128"
    assert people.resolve(None, MEASURED) is None
