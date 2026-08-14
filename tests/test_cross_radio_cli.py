"""The cross-radio review has to be one command the operator can run.

The analysis is only useful if it can be pasted into a discussion, so the
command prints the formatted text by default and the whole report as JSON on
request — and an empty corpus prints the sentence that says so rather than a
wall of zeroes, because that is the state the operator will see for the first
hours after a re-score.

Neither door prints a verdict on the coincidence model's independence
assumption without the negative controls that say whether that verdict is
capable of failing, so both are checked here.
"""
import json

from leo_tracker.radio import cli
from leo_tracker.radio.beacon.cross_radio import (CONSISTENT_VERDICT,
                                                  CROSS_RADIO_SCHEMA)


def test_the_review_command_prints_text_over_an_empty_corpus(tmp_path, capsys):
    corpus = tmp_path / "surveys" / "corpus"
    corpus.mkdir(parents=True)

    assert cli.main(["starlink-cross-radio", "review", str(tmp_path)]) == 0

    printed = capsys.readouterr().out
    assert "no paired sweep" in printed.lower()
    assert "no injection" in printed.lower()
    # A corpus with nothing in it cannot have run a negative control, so it
    # cannot have validated the model's independence assumption either.
    assert CONSISTENT_VERDICT not in printed


def test_the_review_command_can_emit_the_whole_report_as_json(tmp_path, capsys):
    corpus = tmp_path / "corpus"
    corpus.mkdir(parents=True)

    assert cli.main(["starlink-cross-radio", "review", str(tmp_path),
                     "--corpus-root", str(corpus), "--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["schema"] == CROSS_RADIO_SCHEMA
    assert report["pairs"]["joined"] == 0
    # The rate every threshold was calibrated at travels in the document, so a
    # report pasted somewhere cannot be read against the wrong one.
    assert report["false_alarm_rate"] == 0.01
    # And so does the verdict on the consistency check, with the controls it
    # was drawn from. A JSON reader that only saw the f spread would be back to
    # reading a number that the negative controls reproduce on data the model
    # cannot fit.
    assert [control["name"] for control in report["occupancy"]["controls"]] == [
        "scrambled", "shifted +2"]
    assert report["occupancy"]["consistency"]["certified"] is False
