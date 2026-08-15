"""Every figure ships with code that can actually run.

The detector-evaluation report promises, in its provenance section, that each
PNG ships with the script that produced it so any number can be re-derived or
contradicted. Renaming the extractors to carry their pipeline-group prefix broke
that promise for fourteen scripts at once -- a hyphen is not a legal module
name, so ``import hcore`` against ``heatmaps-pipeline-hcore.py`` fails at line
one -- and nothing noticed, because no test ever imported them.

This does not run the figures. They read tens of gigabytes off a share that is
not present on every machine. It checks the cheap property that was actually
violated: that every module a committed script imports can be found.
"""

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

REPORT = (Path(__file__).resolve().parent.parent / "reports"
          / "starlink-detector-evaluation")
FIGURES = REPORT / "figures"
INJECTION = FIGURES / "injection"

#: Third-party and first-party packages a figure may import. Checked for real
#: rather than assumed, so a missing plotting dependency fails here too.
def _importable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def scripts(directory: Path) -> list[Path]:
    return sorted(p for p in directory.glob("*.py") if not p.name.startswith("__"))


def imported_modules(path: Path) -> set[str]:
    """Top-level module names a script imports, ignoring relative imports."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def search_path(path: Path) -> list[str]:
    """Where this script says its own dependencies live.

    Deliberately mirrors what the script does to ``sys.path`` rather than
    guessing: a figure under ``injection/`` reaches into ``injection-data/`` for
    the harness helper it shares with the records, and a test that quietly added
    every directory would pass while the script still failed.
    """
    roots = [str(path.parent), str(REPORT / "src")]
    for node in ast.walk(ast.parse(path.read_text())):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "insert"):
            continue
        try:
            source = ast.unparse(node.args[1])
        except (IndexError, AttributeError):
            continue
        candidate = source.replace("str(", "").rstrip(")")
        parts = [p.strip().strip('"\'') for p in candidate.split("/")]
        if not parts or not parts[0].startswith("HERE"):
            continue
        base = path.parent
        for _ in range(parts[0].count(".parent")):
            base = base.parent
        for extra in parts[1:]:
            base = base / extra
        roots.append(str(base))
    return roots


@pytest.mark.parametrize(
    "script",
    [pytest.param(p, id=p.name) for p in scripts(FIGURES) + scripts(INJECTION)],
)
def test_every_figure_script_can_resolve_its_imports(script, monkeypatch):
    monkeypatch.syspath_prepend(str(FIGURES))
    for root in search_path(script):
        monkeypatch.syspath_prepend(root)

    unresolved = sorted(name for name in imported_modules(script)
                        if not _importable(name))
    assert not unresolved, (
        f"{script.name} imports {unresolved}, which cannot be found. If it is a "
        f"sibling extractor filed under a pipeline prefix, load it with "
        f"`from _pipeline import load`; if it lives with another report or with "
        f"the injection records, put its directory on sys.path in the script.")


def test_the_pipeline_loader_refuses_to_guess_between_two_groups():
    """Ambiguity has to be an error, because guessing was silent and wrong.

    Two groups each ship a ``snapshot.py``. Resolving alphabetically handed
    ``heatmaps-pipeline-drift.py`` the *carried* group's copy, which would have
    built a figure against another population's census with nothing to show for
    it.
    """
    sys.path.insert(0, str(FIGURES))
    import _pipeline

    assert _pipeline._resolve(
        "snapshot", FIGURES / "heatmaps-pipeline-drift.py"
    ).name == "heatmaps-pipeline-snapshot.py"

    with pytest.raises(ModuleNotFoundError, match="ambiguous"):
        _pipeline._resolve("snapshot", FIGURES / "coincidence-model.py")


def test_a_missing_extractor_names_itself():
    sys.path.insert(0, str(FIGURES))
    import _pipeline

    with pytest.raises(ModuleNotFoundError, match="no extractor for"):
        _pipeline._resolve("not_a_real_extractor", FIGURES / "f-strata.py")
