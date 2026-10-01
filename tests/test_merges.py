from __future__ import annotations

import gzip
import lzma
from pathlib import Path

import pytest

from cairn.ingest import registry
from cairn.ingest.archive import (
    SourcePackage,
    decompress,
    newest,
    parse_sources,
    sources_url,
)
from cairn.ingest.base import Kind
from cairn.ingest.merges import (
    ArchiveSnapshot,
    MergesIngester,
    base_version,
    has_new_upstream,
    has_ubuntu_delta,
    is_independent_lineage,
    upstream_release,
)

FIXTURES = Path(__file__).parent / "fixtures"
UBUNTU = (FIXTURES / "ubuntu_sources").read_bytes()
DEBIAN = (FIXTURES / "debian_sources").read_bytes()


class TestDecompress:
    def test_gzip(self):
        assert decompress(gzip.compress(b"hi"), "x/Sources.gz") == b"hi"

    def test_xz(self):
        assert decompress(lzma.compress(b"hi"), "x/Sources.xz") == b"hi"

    def test_plain_passthrough(self):
        assert decompress(b"hi", "x/Sources") == b"hi"


class TestParseSources:
    def test_reads_real_stanzas(self):
        packages = {p.name: p for p in parse_sources(UBUNTU, component="main")}
        assert "live-build" in packages
        assert packages["aalib"].component == "main"

    def test_captures_binary_field(self):
        packages = {p.name: p for p in parse_sources(UBUNTU)}
        assert "libaa1" in packages["aalib"].binaries

    def test_skips_stanzas_without_version(self):
        assert list(parse_sources(b"Package: broken\n\n")) == []


class TestNewest:
    def test_keeps_highest_version_not_last_seen(self):
        pkgs = [SourcePackage("x", "1.0-10"), SourcePackage("x", "1.0-9")]
        assert newest(pkgs)["x"].version == "1.0-10"

    def test_uses_debian_ordering_not_string_ordering(self):
        pkgs = [SourcePackage("x", "1.0"), SourcePackage("x", "1.0~beta")]
        assert newest(pkgs)["x"].version == "1.0"


def test_sources_url_is_https():
    url = sources_url("https://archive.ubuntu.com/ubuntu", "stonking", "main")
    assert url.startswith("https://")
    assert url.endswith("/stonking/main/source/Sources.gz")


class TestBaseVersion:
    @pytest.mark.parametrize(
        "version,expected",
        [
            ("3.0~a57-1ubuntu56", "3.0~a57-1"),
            ("2.0-2ubuntu0.1", "2.0-2"),
            ("2.0-2ubuntu0.25.04.1", "2.0-2"),
            ("3.1-0ubuntu0.22.04.1", "3.1-0"),
            ("2.0-2build1", "2.0-2"),
            ("1.4p5-51.1build1", "1.4p5-51.1"),
            ("0.280ubuntu1", "0.280"),
            ("2.12ubuntu9", "2.12"),
            ("1:11.0.3+ds-2ubuntu2", "1:11.0.3+ds-2"),
        ],
    )
    def test_derives_debian_base(self, version, expected):
        assert base_version(version) == expected

    @pytest.mark.parametrize("version", ["1.0-1", "1.0", "1:2.0-3"])
    def test_version_without_delta_is_its_own_base(self, version):
        assert base_version(version) == version

    @pytest.mark.parametrize(
        "version,expected",
        [
            ("1.0.30-0ubuntu6~24.04.1", "1.0.30-0"),
            ("0.3.16-1.1ubuntu6.1~24.04.1", "0.3.16-1.1"),
            ("1:1.94.7+tod1-0ubuntu5~24.04.8", "1:1.94.7+tod1-0"),
            ("4.5ubuntu9+24.04.2", "4.5"),
            ("37.2ubuntu~24.04.1", "37.2"),
            ("1.8.0-2ubuntu2~ppa1", "1.8.0-2"),
        ],
    )
    def test_release_suffixes_are_stripped(self, version, expected):
        """Backport and PPA suffixes, which MoM's get_base() leaves in place."""
        assert base_version(version) == expected
        assert has_ubuntu_delta(version)

    def test_backported_rebuild_has_no_delta(self):
        """buildN is a no-change rebuild and ubuntu0 means no prior delta."""
        version = "1.0.5-3build1~ubuntu0.24.04.1"
        assert base_version(version) == "1.0.5-3"
        assert not has_ubuntu_delta(version)

    @pytest.mark.parametrize(
        "version,expected",
        [
            ("3.0~a57-1ubuntu56", "3.0~a57-1"),
            ("5.0.10+really4.7.0~dfsg-4.1ubuntu1", "5.0.10+really4.7.0~dfsg-4.1"),
            ("1.0~beta1-2ubuntu1", "1.0~beta1-2"),
        ],
    )
    def test_upstream_tilde_is_not_mistaken_for_a_release_suffix(
        self, version, expected
    ):
        assert base_version(version) == expected

    def test_maysync_is_not_an_ubuntu_delta(self):
        """Only an ubuntu revision prevents syncing."""
        assert not has_ubuntu_delta("2.0-2maysync1")

    @pytest.mark.parametrize(
        "version", ["1.4p5-51.1build1", "2.0-2build1", "0.64-1build2"]
    )
    def test_rebuild_is_not_a_delta(self, version):
        """A no-change rebuild must not block syncing, so it is not a merge."""
        assert not has_ubuntu_delta(version)

    def test_rebuild_on_top_of_a_delta_is_still_a_delta(self):
        assert has_ubuntu_delta("2.0-2ubuntu1build1")


class TestLineage:
    @pytest.mark.parametrize(
        "version", ["3.0-1ubuntu56", "1.0-2ubuntu1", "1:11.0.3+ds-2ubuntu2"]
    )
    def test_derived_from_a_debian_revision(self, version):
        assert not is_independent_lineage(version)

    @pytest.mark.parametrize("version", ["50.0-0ubuntu1", "3.1-0ubuntu0.22.04.1"])
    def test_independent(self, version):
        assert is_independent_lineage(version)

    @pytest.mark.parametrize(
        "version", ["0.280ubuntu1", "2.12ubuntu9", "0.0.16ubuntu1"]
    )
    def test_native_package_is_derived_not_independent(self, version):
        """No Debian revision, but still derived from Debian's native version."""
        assert not is_independent_lineage(version)

    @pytest.mark.parametrize(
        "version,independent",
        [
            ("1.0.30-0ubuntu6~24.04.1", True),
            ("0.3.16-1.1ubuntu6.1~24.04.1", False),
            ("13.3.0-6ubuntu2~24.04.1", False),
            ("1:1.94.7+tod1-0ubuntu5~24.04.8", True),
        ],
    )
    def test_release_suffix_does_not_distort_lineage(self, version, independent):
        assert is_independent_lineage(version) is independent

    def test_delta_detection(self):
        assert has_ubuntu_delta("1.0-1ubuntu1")
        assert not has_ubuntu_delta("1.0-1")


class FixtureFetcher:
    def __init__(self):
        self.urls: list[str] = []

    def get(self, url: str) -> bytes:
        self.urls.append(url)
        if "archive.ubuntu.com" in url:
            return gzip.compress(UBUNTU)
        return lzma.compress(DEBIAN)


def ingester(**kw):
    return MergesIngester(FixtureFetcher(), series="stonking", **kw)


def snapshot(ubuntu_version, debian_version):
    return ArchiveSnapshot(
        ubuntu={"x": SourcePackage("x", ubuntu_version)},
        debian={"x": SourcePackage("x", debian_version)},
    )


class TestMergesIngester:
    def signals(self):
        ing = ingester()
        return {s.source_package: s for s in ing.parse(ing.fetch())}

    def test_fetches_https_only(self):
        ing = ingester()
        ing.fetch()
        assert all(u.startswith("https://") for u in ing.fetcher.urls)

    def test_emits_needs_merge_kind(self):
        assert all(s.kind is Kind.NEEDS_MERGE for s in self.signals().values())

    def test_detects_a_known_merge(self):
        signal = self.signals()["live-build"]
        assert signal.payload["debian_version"].startswith("1:")
        assert signal.payload["base_version"] == "3.0~a57-1"
        assert not signal.payload["independent_lineage"]

    def test_ignores_packages_without_ubuntu_delta(self):
        assert "aalib" not in self.signals()

    def test_records_series(self):
        assert self.signals()["live-build"].series == "stonking"

    def test_ignores_packages_absent_from_debian(self):
        raw = ArchiveSnapshot(
            ubuntu={"x": SourcePackage("x", "1.0-1ubuntu1")}, debian={}
        )
        assert ingester().parse(raw) == []

    def test_ignores_when_ubuntu_is_ahead(self):
        assert ingester().parse(snapshot("2.0-1ubuntu1", "1.0-1")) == []

    def test_epoch_is_respected_not_string_compared(self):
        assert len(ingester().parse(snapshot("9.0-1ubuntu1", "1:1.0-1"))) == 1

    def test_tilde_prerelease_is_not_newer(self):
        assert ingester().parse(snapshot("2.0-1ubuntu1", "2.0~rc1-1")) == []

    def test_numeric_revision_ordering(self):
        assert len(ingester().parse(snapshot("1.0-1ubuntu1", "1.0-10"))) == 1

    def test_identity_is_stable_when_debian_version_moves(self):
        ing = ingester()
        first = ing.parse(snapshot("1.0-1ubuntu1", "1.0-2"))[0]
        second = ing.parse(snapshot("1.0-1ubuntu1", "1.0-3"))[0]
        assert first.signal_id == second.signal_id
        assert first.payload != second.payload


def stanza(name, version):
    return f"Package: {name}\nVersion: {version}\n\n".encode()


class PocketFetcher:
    """Serves a different Ubuntu Sources per suite; unlisted suites are empty."""

    def __init__(self, ubuntu: dict[str, bytes], debian: bytes):
        self.ubuntu = ubuntu
        self.debian = debian
        self.urls: list[str] = []

    def get(self, url: str) -> bytes:
        self.urls.append(url)
        if "archive.ubuntu.com" not in url:
            return lzma.compress(self.debian)
        suite = url.split("/dists/")[1].split("/")[0]
        component = url.split("/dists/")[1].split("/")[1]
        body = self.ubuntu.get(suite, b"") if component == "main" else b""
        return gzip.compress(body)


class TestProposedPocket:
    """Issue #10. Every development upload lands in -proposed first, so
    release alone shows the version before the last upload."""

    def signals(self, release=b"", proposed=b"", debian=b""):
        fetcher = PocketFetcher(
            {"stonking": release, "stonking-proposed": proposed}, debian
        )
        ing = MergesIngester(fetcher, series="stonking")
        return {s.source_package: s for s in ing.run()}

    def test_reads_both_pockets(self):
        ing = ingester()
        ing.fetch()
        suites = {u.split("/dists/")[1].split("/")[0] for u in ing.fetcher.urls}
        assert {"stonking", "stonking-proposed"} <= suites

    def test_a_newer_proposed_version_is_what_ubuntu_has(self):
        """The shadow case from the issue, using its real versions."""
        found = self.signals(
            release=stanza("shadow", "1:4.19.3-2ubuntu1"),
            proposed=stanza("shadow", "1:4.19.3-2ubuntu2"),
            debian=stanza("shadow", "1:4.20.2-2"),
        )
        payload = found["shadow"].payload
        assert payload["ubuntu_version"] == "1:4.19.3-2ubuntu2"
        assert payload["base_version"] == "1:4.19.3-2"
        assert payload["in_proposed"] is True

    def test_a_merge_waiting_in_proposed_is_not_outstanding(self):
        found = self.signals(
            release=stanza("x", "1.0-1ubuntu1"),
            proposed=stanza("x", "1.0-2ubuntu1"),
            debian=stanza("x", "1.0-2"),
        )
        assert "x" not in found

    def test_a_sync_waiting_in_proposed_is_not_outstanding(self):
        found = self.signals(
            release=stanza("x", "1.0-1ubuntu1"),
            proposed=stanza("x", "1.0-2"),
            debian=stanza("x", "1.0-2"),
        )
        assert "x" not in found

    def test_a_release_version_carries_no_flag(self):
        """Absent rather than False, so existing payloads stay byte-identical
        and the log gains no UPDATED event per signal."""
        found = self.signals(
            release=stanza("x", "1.0-1ubuntu1"), debian=stanza("x", "1.0-2")
        )
        assert "in_proposed" not in found["x"].payload

    def test_an_older_proposed_version_does_not_win(self):
        found = self.signals(
            release=stanza("x", "1.0-1ubuntu2"),
            proposed=stanza("x", "1.0-1ubuntu1"),
            debian=stanza("x", "1.0-2"),
        )
        assert found["x"].payload["ubuntu_version"] == "1.0-1ubuntu2"
        assert "in_proposed" not in found["x"].payload

    def test_release_wins_a_tie(self):
        found = self.signals(
            release=stanza("x", "1.0-1ubuntu1"),
            proposed=stanza("x", "1.0-1ubuntu1"),
            debian=stanza("x", "1.0-2"),
        )
        assert "in_proposed" not in found["x"].payload

    def test_a_package_only_in_proposed_is_seen(self):
        found = self.signals(
            proposed=stanza("x", "1.0-1ubuntu1"), debian=stanza("x", "1.0-2")
        )
        assert found["x"].payload["in_proposed"] is True

    def test_identity_survives_migration(self):
        """Moving from -proposed to release is the same signal, updated."""
        before = self.signals(
            release=stanza("x", "1.0-1ubuntu1"),
            proposed=stanza("x", "1.0-1ubuntu2"),
            debian=stanza("x", "1.0-2"),
        )["x"]
        after = self.signals(
            release=stanza("x", "1.0-1ubuntu2"), debian=stanza("x", "1.0-2")
        )["x"]
        assert before.signal_id == after.signal_id
        assert before.payload != after.payload


class TestRegistry:
    def test_merges_is_registered(self):
        assert "merges" in registry.available()

    def test_build_returns_a_configured_ingester(self):
        ing = registry.build("merges", FixtureFetcher(), "stonking")
        assert isinstance(ing, MergesIngester)
        assert ing.series == "stonking"

    def test_unknown_name_lists_what_is_known(self):
        with pytest.raises(KeyError, match="merges"):
            registry.build("nope", FixtureFetcher(), "stonking")

    def test_duplicate_registration_is_rejected(self):
        with pytest.raises(ValueError):
            registry.register("merges", lambda f, s: None)

    def test_available_is_a_copy(self):
        registry.available()["injected"] = None
        assert "injected" not in registry.available()


class TestUpstreamRelease:
    """Distinguishing real merge work from a packaging-only difference."""

    @pytest.mark.parametrize(
        ("version", "expected"),
        [
            ("0.5.6+22.04.20220217-0ubuntu6", "0.5.6"),
            ("0.5.6+repack-2", "0.5.6"),
            ("3.1.6+dfsg-1ubuntu2", "3.1.6"),
            ("1:2.13.0-7ubuntu2", "2.13.0"),
            ("1.0~rc1-1", "1.0~rc1"),
            ("20260526.0-2", "20260526.0"),
            ("3.4", "3.4"),
        ],
    )
    def test_strips_packaging_decoration_but_keeps_prereleases(self, version, expected):
        assert upstream_release(version) == expected

    @pytest.mark.parametrize(
        ("ubuntu", "debian", "expected"),
        [
            # An Ubuntu snapshot marker against a Debian repack marker is not
            # upstream movement, even though the full versions differ.
            ("0.5.6+22.04.20220217-0ubuntu6", "0.5.6+repack-2", False),
            ("3.1.6+dfsg-1ubuntu2", "3.1.6+dfsg-3", False),
            ("2.3.10-1ubuntu2", "2.3.10-2", False),
            ("20260526.0-1ubuntu5", "20260526.0-2", False),
            ("1.5.5-1ubuntu1", "1.5.7-1", True),
            ("2.21.1~rc1-2ubuntu1", "2.22.0~beta1-1", True),
            ("1.0~rc1-1ubuntu1", "1.0-1", True),
            ("1:2.13.0-7ubuntu2", "1:2.15.0-2", True),
        ],
    )
    def test_new_upstream_verdict(self, ubuntu, debian, expected):
        assert has_new_upstream(ubuntu, debian) is expected


class TestNewUpstreamIsStrictlyNewer:
    """The board says "Debian carries a newer upstream release", so a merely
    different one must not set the flag."""

    def test_an_older_debian_upstream_is_not_new(self):
        # A higher epoch makes this a candidate while upstream went backwards.
        assert has_new_upstream("1:0.9.14.2-0ubuntu3", "2:0.8.18-9") is False

    def test_a_really_downgrade_is_not_new(self):
        assert has_new_upstream("3.24-1ubuntu1", "3.24+really3.22-1") is False

    def test_a_newer_debian_upstream_still_is(self):
        assert has_new_upstream("1.5.5-1ubuntu1", "1.5.7-1") is True
