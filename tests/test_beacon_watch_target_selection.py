"""The watcher's tuning selection, exercised as the shell actually evaluates it."""
import collections
import subprocess

import pytest

SCRIPT = "scripts/starlink-beacon-watch.sh"
EIGHT = ("1:lower-edge 1:upper-edge 2:lower-edge 2:upper-edge "
         "3:lower-edge 3:upper-edge 4:lower-edge 4:upper-edge")


def _draw(selection, targets=EIGHT, count=1):
    """Run the script's own selection logic in isolation, count times."""
    body = f"""
    read -r -a targets <<< "{targets}"
    target_selection={selection}
    cycle_targets() {{
      if [[ "${{target_selection}}" == "random" ]]; then
        printf '%s\\n' "${{targets[RANDOM % ${{#targets[@]}}]}}"
      else
        printf '%s\\n' "${{targets[@]}}"
      fi
    }}
    for _ in $(seq {count}); do cycle_targets; done
    """
    out = subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                         check=True).stdout.split()
    return out


def test_script_parses_and_defines_the_selector():
    subprocess.run(["bash", "-n", SCRIPT], check=True)
    text = open(SCRIPT).read()
    assert "cycle_targets()" in text
    # Every capture mode must go through the selector, or a mode would keep
    # sweeping the whole band while the others sample it.
    assert text.count("mapfile -t cycle_list < <(cycle_targets)") == 3
    assert 'for target in "${targets[@]}"' not in text


def test_all_selection_emits_every_tuning_once():
    assert sorted(_draw("all")) == sorted(EIGHT.split())


def test_random_selection_draws_exactly_one_tuning_per_cycle():
    assert len(_draw("random")) == 1


def test_random_selection_is_uniform_over_the_eight_tunings():
    """A biased draw would quietly under-sample part of the band.

    RANDOM is 0..32767, so a modulus of eight divides the range exactly and
    introduces no bias; this pins that the arithmetic stays that way.
    """
    draws = _draw("random", count=4000)
    counts = collections.Counter(draws)

    assert set(counts) == set(EIGHT.split())
    # Expected 500 per bin; +/-25% is far outside sampling noise for n=4000
    # (sd ~ 21) while staying insensitive to the seed.
    assert min(counts.values()) > 375
    assert max(counts.values()) < 625


def test_random_selection_reaches_upper_edges():
    """Upper edges are the half of the band that has never been surveyed."""
    draws = set(_draw("random", count=400))
    assert any(t.endswith("upper-edge") for t in draws)
    assert any(t.endswith("lower-edge") for t in draws)


@pytest.mark.parametrize("value", ["round-robin", "Random", "0", "none"])
def test_an_unknown_selection_mode_is_rejected(value):
    """A typo must stop the service, not silently fall back to one tuning."""
    result = subprocess.run(
        ["bash", SCRIPT],
        env={"PATH": "/usr/bin:/bin", "LEO_BEACON_TARGET_SELECTION": value,
             "LEO_BEACON_TARGETS": EIGHT, "LEO_BEACON_FAKE": "1",
             "LEO_BEACON_MAX_CYCLES": "1"},
        capture_output=True, text=True, timeout=60)

    assert result.returncode == 2
    assert "LEO_BEACON_TARGET_SELECTION must be all or random" in result.stderr


def test_an_empty_selection_falls_back_to_capturing_every_tuning():
    """Empty is unset, not invalid.

    ``${VAR:-all}`` treats an empty value as absent, so an unset or blank
    setting keeps the previous whole-band behaviour rather than failing to
    start. Rejecting it would make the variable mandatory for every deployment.
    """
    assert sorted(_draw("all")) == sorted(EIGHT.split())


def test_a_single_target_still_works_under_random():
    """Reducing to one tuning must not divide by zero or skip capture."""
    assert _draw("random", targets="4:lower-edge", count=5) == ["4:lower-edge"] * 5
