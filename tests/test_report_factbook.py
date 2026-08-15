"""The report's numbers must stay attached to the sidecars they came from."""

import json
import sys
from pathlib import Path

import pytest

REPORT_ROOT = Path(__file__).resolve().parent.parent / "reports" / "starlink-detector-evaluation"
sys.path.insert(0, str(REPORT_ROOT / "source"))

from factbook import (FactBook, FactError, evaluate,  # noqa: E402
                      json_pointer)


def book(tmp_path, facts, sidecars=None):
    """A FactBook over a throwaway tree, so tests never depend on the real report."""
    (tmp_path / "source" / "facts").mkdir(parents=True)
    (tmp_path / "source" / "facts" / "test.json").write_text(json.dumps(facts))
    for name, payload in (sidecars or {}).items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
    return FactBook.load(tmp_path)


def test_pointer_walks_dicts_and_lists():
    document = {"a": {"b": [10, {"c": 3}]}}
    assert json_pointer(document, "/a/b/0") == 10
    assert json_pointer(document, "/a/b/1/c") == 3


def test_pointer_unescapes_in_the_right_order():
    # '~01' must decode to '~1', not to '/'. Decoding '~0' last would give '/'.
    assert json_pointer({"~1": 5}, "/~01") == 5
    assert json_pointer({"a/b": 7}, "/a~1b") == 7


def test_pointer_names_what_was_missing():
    with pytest.raises(FactError) as caught:
        json_pointer({"alpha": 1, "beta": 2}, "/gamma")
    assert "gamma" in str(caught.value) and "alpha" in str(caught.value)


def test_missing_key_is_fatal_not_blank(tmp_path):
    facts = {"x": {"src": "s.json", "ptr": "/nope", "fmt": "int"}}
    with pytest.raises(FactError):
        book(tmp_path, facts, {"s.json": {"yes": 1}}).text("x")


def test_missing_sidecar_is_fatal(tmp_path):
    facts = {"x": {"src": "gone.json", "ptr": "/a", "fmt": "int"}}
    with pytest.raises(FactError, match="does not exist"):
        book(tmp_path, facts).text("x")


def test_derived_fact_recomputes_from_its_inputs(tmp_path):
    facts = {
        "measured": {"src": "s.json", "ptr": "/measured", "fmt": "hz"},
        "applied": {"src": "s.json", "ptr": "/applied", "fmt": "hz"},
        "miscentred": {"expr": "measured - applied", "fmt": "signed_hz",
                       "inputs": {"measured": "measured", "applied": "applied"}},
    }
    made = book(tmp_path, facts, {"s.json": {"measured": 424989.96, "applied": 602869.4}})
    assert made.text("miscentred") == "−177,879 Hz"


def test_derived_facts_cannot_call_anything():
    with pytest.raises(FactError, match="Call is not allowed"):
        evaluate("open('x')", {})


def test_derived_facts_reject_unknown_names():
    with pytest.raises(FactError, match="not an input"):
        evaluate("a + b", {"a": 1.0})


def test_cycles_are_reported_not_recursed(tmp_path):
    facts = {
        "a": {"expr": "b + 1", "inputs": {"b": "b"}, "fmt": "f0"},
        "b": {"expr": "a + 1", "inputs": {"a": "a"}, "fmt": "f0"},
    }
    with pytest.raises(FactError, match="cycle"):
        book(tmp_path, facts).value("a")


def test_a_fact_defined_twice_is_refused(tmp_path):
    directory = tmp_path / "source" / "facts"
    directory.mkdir(parents=True)
    (directory / "one.json").write_text(json.dumps({"x": {"src": "s", "ptr": "/a"}}))
    (directory / "two.json").write_text(json.dumps({"x": {"src": "s", "ptr": "/b"}}))
    with pytest.raises(FactError, match="defined twice"):
        FactBook.load(tmp_path)


def test_percentage_format_refuses_an_already_scaled_number(tmp_path):
    # The failure this guards is publishing "2,550%" from a value of 25.5.
    facts = {"x": {"src": "s.json", "ptr": "/v", "fmt": "pct1"}}
    with pytest.raises(FactError, match="expects a fraction"):
        book(tmp_path, facts, {"s.json": {"v": 25.5}}).text("x")


def test_minus_signs_match_the_report_typography(tmp_path):
    facts = {"x": {"src": "s.json", "ptr": "/v", "fmt": "signed_khz1"}}
    assert book(tmp_path, facts, {"s.json": {"v": -177879.0}}).text("x") == "−177.9 kHz"


def test_thousands_separators(tmp_path):
    facts = {"x": {"src": "s.json", "ptr": "/v", "fmt": "int"}}
    assert book(tmp_path, facts, {"s.json": {"v": 1234567}}).text("x") == "1,234,567"


def test_unknown_format_is_named(tmp_path):
    facts = {"x": {"src": "s.json", "ptr": "/v", "fmt": "furlongs"}}
    with pytest.raises(FactError, match="unknown format"):
        book(tmp_path, facts, {"s.json": {"v": 1}}).text("x")


def test_provenance_reaches_through_a_derived_fact(tmp_path):
    facts = {
        "a": {"src": "s.json", "ptr": "/a", "fmt": "f0"},
        "b": {"src": "s.json", "ptr": "/b", "fmt": "f0"},
        "gap": {"expr": "a - b", "inputs": {"a": "a", "b": "b"}, "fmt": "f0"},
    }
    record = book(tmp_path, facts, {"s.json": {"a": 5, "b": 2}}).provenance("gap")
    assert record["expr"] == "a - b"
    assert record["inputs"]["a"]["src"] == "s.json"
    assert record["inputs"]["a"]["ptr"] == "/a"


def test_uncited_facts_are_reported(tmp_path):
    facts = {"used": {"src": "s.json", "ptr": "/v", "fmt": "f0"},
             "spare": {"src": "s.json", "ptr": "/v", "fmt": "f0"}}
    made = book(tmp_path, facts, {"s.json": {"v": 1}})
    made.text("used")
    assert made.unused() == ["spare"]


def test_the_published_report_still_matches_its_sources():
    """REPORT.md is output, and this is what makes that true rather than aspirational.

    The document is committed so it renders on the forge like any other file,
    which also means it can be hand-edited -- and hand-editing numbers next to
    data nobody re-read is exactly what put two dozen contradictions in it. A
    sidecar regenerated with different values does the same thing from the other
    side. Either way the rendered file and its sources stop agreeing, and this
    fails instead of leaving it for a reader to notice.
    """
    import subprocess

    done = subprocess.run(
        [sys.executable, str(REPORT_ROOT / "source" / "build.py"), "check"],
        capture_output=True, text=True, cwd=REPORT_ROOT)
    assert done.returncode == 0, (
        "REPORT.md has drifted from source/sections and source/facts.\n"
        "Rebuild it with: python source/build.py build\n\n"
        + done.stdout[-4000:] + done.stderr[-2000:])


def test_a_fact_reading_a_superseded_key_must_say_so():
    """The failure this catches resolved fine and was still wrong.

    Several sidecars deliberately keep what was published before a population
    changed, so the report can show the old number beside the new one. That is
    legitimate and the report does it in a dozen places. What is not legitimate
    is reaching into one by accident: a figure caption described 3,774 units and
    three receivers while the figure beside it plotted 5,032 and four, because
    the caption's facts pointed at the preserved-published block. Every
    reference resolved, so the build was happy and the drift gate saw nothing.

    Requiring a note turns citing a superseded value into a decision. It does
    not stop anyone doing it.
    """
    book = FactBook.load(REPORT_ROOT)
    undeclared = sorted(name for name, note in book.historical().items() if not note)
    assert not undeclared, (
        "these facts read a preserved-historical sidecar key without saying so:\n  "
        + "\n  ".join(undeclared)
        + "\nAdd a `note` saying why the superseded value is the one wanted.")


def test_the_historical_marker_actually_matches_something(tmp_path):
    # A guard whose pattern matched nothing would pass forever in silence.
    facts = {"old": {"src": "s.json", "ptr": "/published_with_x/v", "fmt": "f0"},
             "new": {"src": "s.json", "ptr": "/v", "fmt": "f0"}}
    made = book(tmp_path, facts, {"s.json": {"v": 1, "published_with_x": {"v": 2}}})
    assert set(made.historical()) == {"old"}
