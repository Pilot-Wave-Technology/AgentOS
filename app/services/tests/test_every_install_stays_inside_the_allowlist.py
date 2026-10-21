"""B6 -- the allowlist becomes measured, and stays measured.

Every entry on that list was added because something failed, and each
was first read as something else: node-gyp's headers as a broken build,
the credential broker on `.run.app` as "an authentication failure rather
than a blocked host". A list grown that way has two invisible defects --
hosts an install reaches that are not on it, and entries nothing has
reached in months.

Nothing here talks to a cluster. What is tested is the machinery that
turns a squid log plus the deployed manifest into those two answers,
including the parts most likely to be wrong in the PERMISSIVE direction:
a domain rule that matches more than squid would, a denial counted as a
success, a second replica's log silently dropped.
"""

from __future__ import annotations

from app.services.egress_audit import accesslog, allowlist, report

LOG = """\
1726900000.123 145 10.0.0.5 TCP_TUNNEL/200 8123 CONNECT static.crates.io:443 - ORIGINAL_DST/1.2.3.4 -
1726900001.000 2 10.0.0.5 TCP_DENIED/403 3941 CONNECT telemetry.example.com:443 - HIER_NONE/- text/html
1726900002.000 9 10.0.0.5 TCP_MISS/200 500 GET http://deb.debian.org/debian/x - ORIGINAL_DST/1.2.3.4 -
"""


# -- reading the deployed list, not a copy of it -----------------------

def test_the_list_is_read_from_the_manifest_that_deploys_it():
    """A second copy anywhere goes stale silently and then reports a
    correct configuration as a defect."""
    acls = allowlist.load()
    assert "full_hosts" in acls and "store_hosts" in acls
    assert ".crates.io" in acls["full_hosts"]


def test_only_dstdomain_lines_are_parsed():
    """A regex that quietly matched `dstdom_regex` would report hosts as
    allowed that squid matches by a different rule entirely."""
    got = allowlist.parse(
        "acl a dstdomain .one.test\n"
        "acl b dstdom_regex ^.*two\\.test$\n"
        "acl c dst 10.0.0.0/8\n")
    assert got == {"a": {".one.test"}}


# -- matching, in the direction that matters ---------------------------

def test_a_leading_dot_covers_the_domain_and_its_subdomains():
    allowed = {".crates.io"}
    assert allowlist.covers(allowed, "crates.io") == ".crates.io"
    assert allowlist.covers(allowed, "static.crates.io") == ".crates.io"


def test_a_leading_dot_does_not_cover_a_lookalike():
    """`evilcrates.io` ends with `crates.io` as a STRING and is a
    different domain. Getting this wrong reports a real hole as
    covered."""
    assert allowlist.covers({".crates.io"}, "evilcrates.io") == ""
    assert allowlist.covers({".crates.io"}, "crates.io.evil.test") == ""


def test_a_bare_entry_matches_exactly_and_no_further():
    allowed = {"registry.test"}
    assert allowlist.covers(allowed, "registry.test") == "registry.test"
    assert allowlist.covers(allowed, "sub.registry.test") == ""


def test_matching_ignores_case_and_a_trailing_dot():
    assert allowlist.covers({".pypi.org"}, "Files.PyPI.org.") == ".pypi.org"


# -- reading what squid actually saw -----------------------------------

def test_both_connect_and_plain_http_yield_a_host():
    got = accesslog.parse(LOG)
    assert set(got) == {"static.crates.io", "telemetry.example.com",
                        "deb.debian.org"}


def test_a_denial_is_kept_and_counted_as_one():
    """It is the more useful half: a TCP_DENIED on a host nobody
    allowlisted is the failure a story is about to report as something
    else, visible before anybody hits it."""
    got = accesslog.parse(LOG)
    assert got["telemetry.example.com"] == {"allowed": 0, "denied": 1}
    assert got["static.crates.io"] == {"allowed": 1, "denied": 0}


def test_a_port_and_a_userinfo_are_not_part_of_the_host():
    got = accesslog.parse(
        "1 1 c TCP_MISS/200 1 GET http://u@files.pypi.org:8080/x - D/1 -")
    assert "files.pypi.org" in got


def test_a_short_line_is_skipped_rather_than_guessed_at():
    assert accesslog.parse("garbage\n\n1 2 3\n") == {}


# -- the three answers, and which one fails ----------------------------

def test_an_unknown_host_reached_is_the_failure():
    result = report.compare(accesslog.parse(LOG), {".crates.io", ".debian.org"})
    assert set(result["unknown"]) == {"telemetry.example.com"}
    assert report.failed(result)


def test_an_entry_nothing_reached_is_reported_and_not_failed():
    """An entry for a language nothing used this month is not a defect."""
    result = report.compare(
        accesslog.parse(LOG),
        {".crates.io", ".debian.org", ".example.com", ".rubygems.org"})
    assert result["unused"] == [".rubygems.org"]
    assert not report.failed(result), (
        "an unused entry must not fail the check")


def test_a_used_host_names_the_entry_that_admitted_it():
    result = report.compare(accesslog.parse(LOG), {".crates.io", ".debian.org"})
    assert result["used"]["static.crates.io"]["entry"] == ".crates.io"


def test_rust_is_expected_to_start_showing_up():
    """Once cargo is in the image, `.crates.io` stops being unused --
    and if it does not, the registry rows are not running."""
    assert ".crates.io" in allowlist.load()["full_hosts"]
    result = report.compare(accesslog.parse(LOG), allowlist.load()["full_hosts"])
    assert ".crates.io" not in result["unused"]


# -- the report must not overclaim -------------------------------------

def test_the_report_states_what_it_did_not_look_at():
    """"The allowlist is measured" would otherwise be read as "every way
    out of a pod is measured", and a reader who believes that stops
    looking at the five places this cannot see."""
    text = report.render(report.compare({}, {".pypi.org"}))
    assert "What this did not look at" in text
    for keyword in ("DNS", "3307", "NO_PROXY", "replica"):
        assert keyword in text, keyword


def test_the_blind_spots_include_the_one_that_leaves_no_trace():
    """A tool that ignores *_PROXY is DROPPED by default-deny -- never
    logged, never reported, and it hangs."""
    joined = " ".join(report.BLIND_SPOTS)
    assert "ignore *_PROXY" in joined and "HANG" in joined


# -- it recurs, and the runner says how --------------------------------

def test_the_runner_exits_non_zero_on_an_unknown_host(tmp_path):
    import scripts.egress_audit as runner

    log = tmp_path / "access.log"
    log.write_text(LOG, encoding="utf-8")
    manifest = tmp_path / "m.yaml"
    manifest.write_text("    acl full_hosts dstdomain .crates.io .debian.org\n",
                        encoding="utf-8")

    assert runner.main([str(log), "--manifest", str(manifest)]) == 1


def test_the_runner_is_green_when_every_host_is_covered(tmp_path):
    import scripts.egress_audit as runner

    log = tmp_path / "access.log"
    log.write_text(LOG, encoding="utf-8")
    manifest = tmp_path / "m.yaml"
    manifest.write_text(
        "    acl full_hosts dstdomain .crates.io .debian.org .example.com\n",
        encoding="utf-8")

    assert runner.main([str(log), "--manifest", str(manifest)]) == 0


def test_the_runner_says_it_is_a_standing_check_and_how_to_feed_it():
    """A one-off measurement drifts the moment the image or a registry
    row moves, and this phase moves both."""
    import scripts.egress_audit as runner

    doc = runner.__doc__ or ""
    assert "STANDING CHECK" in doc
    assert "EVERY" in doc and "replica" in doc, (
        "one replica's log is half the traffic; the unused half is then "
        "wrong")
    assert "access.log" in doc
