import json

from leo_tracker.replay_cli import main
from leo_tracker.radio.beacon.replay import DEFAULT_REPLAY_ID, PLAN_SCHEMA


def test_replay_status_cli_reads_versioned_plan(tmp_path, capsys):
    plan = (tmp_path / "reports" / "replays" / DEFAULT_REPLAY_ID / "plan.json")
    plan.parent.mkdir(parents=True)
    plan.write_text(json.dumps({"schema": PLAN_SCHEMA, "replay_id": DEFAULT_REPLAY_ID,
                                "job_count": 0, "jobs": []}))

    assert main(["status", str(tmp_path)]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["job_count"] == 0
    assert result["remaining_count"] == 0
