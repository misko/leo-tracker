"""The cross-radio review has to be one command the operator can run.

The analysis is only useful if it can be pasted into a discussion, so the
command prints the formatted text by default and the whole report as JSON on
request — and an empty corpus prints the sentence that says so rather than a
wall of zeroes, because that is the state the operator will see for the first
hours after a re-score.
"""
import json

from leo_tracker.radio import cli
from leo_tracker.radio.beacon.cross_radio import CROSS_RADIO_SCHEMA


def test_the_review_command_prints_text_over_an_empty_corpus(tmp_path, capsys):
    corpus = tmp_path / "surveys" / "corpus"
    corpus.mkdir(parents=True)

    assert cli.main(["starlink-cross-radio", "review", str(tmp_path)]) == 0

    printed = capsys.readouterr().out
    assert "no paired sweep" in printed.lower()
    assert "no injection" in printed.lower()


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
