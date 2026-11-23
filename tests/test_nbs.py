"""NBS from the archive indexes.

Fixtures in tests/fixtures/nbs are the real tenacious release pocket of
23 Nov 2026, trimmed to a handful of packages, two components and two
architectures, and to the fields cairn reads. The oracle_* files are the
static-reports NBS report of the same day.
"""

from __future__ import annotations

import gzip
import lzma
from pathlib import Path

import pytest

from cairn.ingest import registry
from cairn.ingest.archive import (
    Relation,
    arch_matches,
    packages_url,
    parse_packages,
    parse_relations,
    relations,
    stanzas,
)
from cairn.ingest.base import Kind
from cairn.ingest.nbs import NbsIngester, parse_release, referenced_packages
from cairn.oracles import nbs as oracle

FIXTURES = Path(__file__).parent / "fixtures" / "nbs"
SERIES = "tenacious"


class ArchiveFetcher:
    """Serves the fixture files at the URLs the ingester asks for."""

    def __init__(self):
        self.urls: list[str] = []

    def get(self, url: str) -> bytes:
        self.urls.append(url)
        if url.endswith("/Release"):
            return (FIXTURES / "Release").read_bytes()
        if url.startswith(oracle.ORACLE):
            name = url.removeprefix(oracle.ORACLE)
            if name == "NBS/":
                return (FIXTURES / "oracle_listing.html").read_bytes()
            if name == "nbs.html":
                return (FIXTURES / "oracle_nbs.html").read_bytes()
            path = FIXTURES / f"oracle_{name.removeprefix('NBS/')}"
            return path.read_bytes() if path.exists() else b""
        parts = url.split("/")
        component = parts[-3]
        if "/source/" in url:
            return gzip.compress((FIXTURES / f"Sources_{component}").read_bytes())
        arch = parts[-2].removeprefix("binary-")
        return lzma.compress((FIXTURES / f"Packages_{component}_{arch}").read_bytes())


@pytest.fixture(scope="module")
def signals():
    found = NbsIngester(ArchiveFetcher(), series=SERIES).run()
    return {s.binary_package: s for s in found}


class TestReader:
    def test_stanzas_keep_only_the_fields_asked_for(self):
        data = b"Package: a\nVersion: 1\nDescription: x\n more\n\nPackage: b\n"
        assert list(stanzas(data, ["Package", "Version"])) == [
            {"Package": "a", "Version": "1"},
            {"Package": "b"},
        ]

    def test_continuation_lines_are_folded(self):
        data = b"Package: a\nDepends: b,\n c\n"
        assert next(stanzas(data, ["Depends"])) == {"Depends": "b, c"}

    def test_a_binnmu_names_its_source_version(self):
        data = b"Package: libfmt10\nVersion: 10.1-4build1\nSource: fmtlib (10.1-4)\n"
        [package] = parse_packages(data)
        assert (package.source, package.source_version) == ("fmtlib", "10.1-4")

    def test_without_a_source_field_the_source_is_the_binary(self):
        [package] = parse_packages(b"Package: ccache\nVersion: 4.1-1\n")
        assert (package.source, package.source_version) == ("ccache", "4.1-1")

    def test_relations_are_or_groups_of_names(self):
        text = "a (>= 1) | b:any, c [amd64] <!nocheck>, d"
        assert relations(text) == [("a", "b"), ("c",), ("d",)]

    def test_ports_architectures_come_from_ports(self):
        assert packages_url(SERIES, "main", "amd64").startswith(
            "https://archive.ubuntu.com/ubuntu/dists/tenacious/main/binary-amd64/"
        )
        assert packages_url(SERIES, "main", "amd64v3").startswith(
            "https://archive.ubuntu.com/"
        )
        assert packages_url(SERIES, "main", "arm64").startswith(
            "https://ports.ubuntu.com/ubuntu-ports/dists/tenacious/main/binary-arm64/"
        )

    def test_release_names_architectures_and_components(self):
        release = parse_release((FIXTURES / "Release").read_bytes())
        assert release.architectures == ("amd64", "arm64")
        assert release.components == ("main", "universe")


class TestNbs:
    def test_finds_the_binaries_no_source_builds(self, signals):
        assert set(signals) == {
            "libcpgplot0",
            "libgiza0",
            "libfmt10",
            "libmaven-exec-plugin-java",
            "network-manager-gnome",
        }

    def test_signal_identity(self, signals):
        signal = signals["libfmt10"]
        assert signal.kind is Kind.NBS
        assert signal.source_package == "fmtlib"
        assert signal.series == SERIES
        assert signal.payload["version"] == "10.1.1+ds1-4build1"
        assert signal.payload["current_version"] == "11.1.1+ds1-2"

    def test_what_still_needs_it(self, signals):
        [ccache] = signals["libfmt10"].payload["rdepends"]
        assert ccache["package"] == "ccache"
        assert ccache["architectures"] == ["amd64", "arm64"]
        assert "removable" not in signals["libfmt10"].payload

    def test_a_cluster_needed_only_by_itself_is_removable(self, signals):
        """libcpgplot0 needs libgiza0; nothing else needs either."""
        assert signals["libgiza0"].payload["removable"]
        assert signals["libcpgplot0"].payload["removable"]
        [rdep] = signals["libgiza0"].payload["rdepends"]
        assert rdep["package"] == "libcpgplot0" and rdep["nbs"]

    def test_a_name_a_built_package_provides_holds_nothing(self, signals):
        """libexec-maven-plugin-java provides its old name, so libphonenumber's
        build dependency on it survives the removal. The report says not
        removable; this is the recorded divergence."""
        payload = signals["libmaven-exec-plugin-java"].payload
        assert payload["provided_by"] == ["libexec-maven-plugin-java"]
        assert payload["removable"]
        assert "build_rdepends" not in payload

    def test_an_or_group_with_a_built_alternative_does_not_hold_it(self, signals):
        """gnome-control-center recommends nm-connection-editor |
        network-manager-gnome; lubuntu-desktop depends on it alone."""
        rdeps = signals["network-manager-gnome"].payload["rdepends"]
        assert [r["package"] for r in rdeps] == ["lubuntu-desktop"]
        assert rdeps[0]["source"] == "lubuntu-meta"

    def test_registered(self):
        assert isinstance(registry.build("nbs", ArchiveFetcher(), SERIES), NbsIngester)

    def test_reads_every_component_and_architecture_of_the_release_pocket(self):
        fetcher = ArchiveFetcher()
        NbsIngester(fetcher, series=SERIES).run()
        packages = [u for u in fetcher.urls if "/binary-" in u]
        assert len(packages) == 4
        assert not any("-proposed" in u for u in fetcher.urls)


class TestOracle:
    def test_reads_the_report(self):
        report = oracle.published(ArchiveFetcher())
        assert "libfmt10" in report.needed_by
        assert oracle.FTBFS not in report.needed_by
        assert report.needed_by["libgiza0"] == {"libcpgplot0"}
        assert "libcpgplot0" in report.removable
        assert len(report.removable) == 9

    def test_the_divergence_is_the_documented_one(self, signals):
        report = oracle.published(ArchiveFetcher())
        trimmed = oracle.Published(
            needed_by={n: report.needed_by[n] for n in signals},
            removable=report.removable,
        )
        found = oracle.compare(signals.values(), trimmed, scope=SERIES)
        assert not found.missing and not found.cairn_only
        assert found.cairn_removable - found.published_removable == {
            "libmaven-exec-plugin-java"
        }
        # Alternatives still built, and a provided name: both towards
        # "not needed", never the other way.
        for ours, theirs in found.needed_by.values():
            assert not ours
            assert theirs


class TextFetcher:
    """A release pocket from text: {"Sources": ..., ("main", "amd64"): ...}."""

    def __init__(self, sources, packages, arches=("amd64", "arm64")):
        self.sources = sources
        self.packages = packages
        self.arches = arches

    def get(self, url):
        if url.endswith("/Release"):
            return (
                f"Architectures: {' '.join(self.arches)}\nComponents: main\n".encode()
            )
        if "/source/" in url:
            return gzip.compress(self.sources.encode())
        arch = url.split("/")[-2].removeprefix("binary-")
        return lzma.compress(self.packages.get(arch, "").encode())


def analyse(sources, packages, arches=("amd64", "arm64")):
    found = NbsIngester(TextFetcher(sources, packages, arches), series=SERIES).run()
    return {s.binary_package: s.payload for s in found}


# "gone" is left behind by lib (now 2.0, building libnew); user needs it.
SOURCES = """Package: lib
Binary: libnew
Version: 2.0

Package: user
Binary: user
Version: 1.0
"""
GONE = "Package: gone\nVersion: 1.0\nSource: lib (1.0)\nArchitecture: {arch}\n"


def user(depends, arch):
    return f"Package: user\nVersion: 1.0\nArchitecture: {arch}\nDepends: {depends}\n"


def new(arch, version="2.0", provides=None):
    text = f"Package: libnew\nVersion: {version}\nSource: lib\nArchitecture: {arch}\n"
    return text + (f"Provides: {provides}\n" if provides else "")


def both(depends, **new_by_arch):
    return {
        arch: "\n".join(
            [GONE.format(arch=arch), user(depends, arch), new_by_arch.get(arch, "")]
        )
        for arch in ("amd64", "arm64")
    }


class TestRelations:
    def test_constraints_are_kept(self):
        assert parse_relations("a (>= 1.0) | b:any, c (<< 2) [amd64]") == [
            (Relation("a", ">=", "1.0"), Relation("b")),
            (Relation("c", "<<", "2", ("amd64",)),),
        ]

    def test_architecture_restrictions(self):
        [[arm], [not_i386], [cpu], [hurd]] = parse_relations(
            "a [arm64], b [!i386], c [any-arm], d [hurd-any]"
        )
        assert arm.applies_to("arm64") and not arm.applies_to("amd64")
        assert not_i386.applies_to("amd64") and not not_i386.applies_to("i386")
        assert cpu.applies_to("armhf") and not cpu.applies_to("arm64")
        assert not hurd.applies_to("amd64")

    def test_restrictions_match_as_dpkg_does(self):
        """Checked with dpkg-architecture -a <arch> -i <wildcard>, 23 Nov 2026.
        linux-arm is the old arm ABI, not armhf (review on #26 said it was)."""
        cases = {
            ("armhf", "linux-arm"): False,
            ("armhf", "linux-armhf"): True,
            ("armhf", "any-arm"): True,
            ("arm64", "any-arm"): False,
            ("amd64", "linux-amd64"): True,
            ("amd64", "any-linux-any"): True,
            ("amd64", "gnu-any-any"): True,
            ("amd64", "musl-linux-any"): False,
            ("armhf", "eabihf-any-any-any"): True,
            ("amd64", "hurd-any"): False,
            ("amd64", "armhf"): False,
        }
        for (arch, restriction), expected in cases.items():
            assert arch_matches(arch, restriction) is expected, (arch, restriction)

    def test_amd64v3_is_amd64_to_a_restriction(self):
        [[r]] = parse_relations("a [amd64]")
        assert r.applies_to("amd64v3")

    def test_obsolete_operators_mean_or_equal(self):
        assert parse_relations("a (> 1)") == [(Relation("a", ">=", "1"),)]

    def test_satisfied_by_uses_debian_ordering(self):
        assert not Relation("a", ">=", "1.0").satisfied_by("1.0~rc1")
        assert Relation("a", ">=", "1.0").satisfied_by("1:0.5")

    def test_an_unversioned_provide_satisfies_only_unversioned(self):
        assert Relation("a").satisfied_by(None)
        assert not Relation("a", ">=", "1").satisfied_by(None)


class TestReviewOn26:
    """Copilot's review on #26: each case would have offered a still-needed
    binary for removal, or misreported what needs it."""

    def test_an_alternative_too_old_does_not_satisfy(self):
        found = analyse(
            SOURCES,
            both("gone | libnew (>= 3)", amd64=new("amd64"), arm64=new("arm64")),
        )
        assert [r["package"] for r in found["gone"]["rdepends"]] == ["user"]
        assert "removable" not in found["gone"]

    def test_an_alternative_new_enough_does(self):
        found = analyse(
            SOURCES,
            both("gone | libnew (>= 2)", amd64=new("amd64"), arm64=new("arm64")),
        )
        assert found["gone"]["removable"]

    def test_an_unversioned_provide_does_not_satisfy_a_versioned_dependency(self):
        found = analyse(
            SOURCES,
            both(
                "gone (>= 1)",
                amd64=new("amd64", provides="gone"),
                arm64=new("arm64", provides="gone"),
            ),
        )
        assert "removable" not in found["gone"]
        assert found["gone"]["provided_by"] == ["libnew"]

    def test_a_versioned_provide_does(self):
        found = analyse(
            SOURCES,
            both(
                "gone (>= 1)",
                amd64=new("amd64", provides="gone (= 1.5)"),
                arm64=new("arm64", provides="gone (= 1.5)"),
            ),
        )
        assert found["gone"]["removable"]

    def test_a_replacement_on_one_architecture_does_not_satisfy_another(self):
        found = analyse(SOURCES, both("gone | libnew", amd64=new("amd64")))
        [rdep] = found["gone"]["rdepends"]
        assert rdep["architectures"] == ["arm64"]
        assert "removable" not in found["gone"]

    def test_a_provider_on_one_architecture_does_not_satisfy_another(self):
        found = analyse(SOURCES, both("gone", amd64=new("amd64", provides="gone")))
        assert found["gone"]["rdepends"][0]["architectures"] == ["arm64"]

    def test_an_older_copy_keeps_its_architecture(self):
        packages = both("gone")
        packages["arm64"] = packages["arm64"].replace("Version: 1.0", "Version: 0.9", 1)
        found = analyse(SOURCES, packages)
        assert found["gone"]["version"] == "1.0"
        assert found["gone"]["architectures"] == ["amd64", "arm64"]
        assert found["gone"]["rdepends"][0]["architectures"] == ["amd64", "arm64"]

    def test_a_dependency_the_binary_does_not_satisfy_does_not_hold_it(self):
        """Removing it changes nothing: the dependency is unmet already."""
        found = analyse(SOURCES, both("gone (>= 5)"))
        assert found["gone"]["removable"]

    def test_build_dependencies_count(self):
        sources = SOURCES + "\nPackage: builder\nBinary: b\nVersion: 1\n"
        sources += "Build-Depends-Arch: gone (>= 1)\n"
        packages = {a: GONE.format(arch=a) for a in ("amd64", "arm64")}
        found = analyse(sources, packages)
        assert found["gone"]["build_rdepends"] == [
            {"package": "builder", "component": "main"}
        ]
        assert "removable" not in found["gone"]

    def test_a_binary_at_its_sources_current_version_is_not_nbs(self):
        """archive-cruft-check sets these aside: the source still builds it."""
        packages = {"amd64": "Package: odd\nVersion: 2.0\nSource: lib\n"}
        assert analyse(SOURCES, packages) == {}

    def test_what_needs_it_is_collected_for_ownership(self):
        signal = next(
            s
            for s in NbsIngester(
                TextFetcher(SOURCES, both("gone")), series=SERIES
            ).run()
        )
        assert referenced_packages(signal) == {"user"}


class TestSecondReviewOn26:
    """Copilot's second review on #26."""

    def test_an_alternative_restricted_elsewhere_does_not_satisfy(self):
        """Build-Depends: gone [arm64] | libnew [amd64] needs gone on arm64,
        whatever amd64 publishes."""
        sources = SOURCES + "\nPackage: builder\nBinary: b\nVersion: 1\n"
        sources += "Build-Depends: gone [arm64] | libnew [amd64]\n"
        packages = {a: GONE.format(arch=a) + "\n" + new(a) for a in ("amd64", "arm64")}
        found = analyse(sources, packages)
        assert found["gone"]["build_rdepends"] == [
            {"package": "builder", "component": "main"}
        ]
        assert "removable" not in found["gone"]

    def test_a_restriction_that_does_not_apply_holds_nothing(self):
        sources = SOURCES + "\nPackage: builder\nBinary: b\nVersion: 1\n"
        sources += "Build-Depends: gone [riscv64]\n"
        packages = {a: GONE.format(arch=a) for a in ("amd64", "arm64")}
        assert analyse(sources, packages)["gone"]["removable"]

    def test_the_only_provider_of_a_name_is_held_by_it(self):
        provides = {
            a: GONE.format(arch=a)
            + "Provides: virtual-api (= 1)\n\n"
            + user("virtual-api (>= 1)", a)
            for a in ("amd64", "arm64")
        }
        found = analyse(SOURCES, provides)
        [rdep] = found["gone"]["rdepends"]
        assert rdep["package"] == "user"
        assert "removable" not in found["gone"]

    def test_another_provider_frees_it(self):
        packages = {
            a: GONE.format(arch=a)
            + "Provides: virtual-api\n\n"
            + user("virtual-api", a)
            + "\n"
            + new(a, provides="virtual-api")
            for a in ("amd64", "arm64")
        }
        assert analyse(SOURCES, packages)["gone"]["removable"]
