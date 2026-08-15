"""Import a sibling extractor that was renamed when it was filed here.

The extractors are prefixed by the group they feed -- ``heatmaps-pipeline-``,
``firerate-pipeline-`` and so on -- so a reader can see at a glance which
figures share a cache. Their *import sites* were never updated to match, and a
hyphen is not a legal module name anyway, so nine scripts in this directory have
been committed with ``import hcore`` against a file called
``heatmaps-pipeline-hcore.py``. Every one of them fails at line one.

That matters more than a broken script usually would, because the provenance
section of this report promises each figure ships with the code that produced
it, so that any number can be re-derived or contradicted. A script that cannot
be imported does not keep that promise.

    from _pipeline import load
    hcore = load("hcore")

The loaded module is registered in :data:`sys.modules` under its bare name, so a
dependency that itself does ``import extract_heatmaps`` resolves too, without
that file needing to change.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _prefix_of(path: Path) -> str | None:
    """The pipeline group a script belongs to, from its own filename.

    ``heatmaps-pipeline-drift.py`` belongs to ``heatmaps-pipeline-``. Scripts
    that are figures rather than extractors have no prefix and get ``None``.
    """
    stem = path.name
    marker = "-pipeline-"
    return stem[:stem.index(marker) + len(marker)] if marker in stem else None


def _resolve(name: str, caller: Path | None) -> Path:
    """Which file is ``name``, refusing to guess when more than one could be.

    Two groups both ship a ``snapshot.py``, and picking between them
    alphabetically loaded the wrong one -- quietly, with no error and a figure
    built on another group's census. So a caller inside a group is served that
    group's copy, and anything still ambiguous is an error rather than a
    coin flip.
    """
    exact = HERE / f"{name}.py"
    if exact.exists():
        return exact

    prefixed = sorted(HERE.glob(f"*-{name}.py"))
    if not prefixed:
        raise ModuleNotFoundError(
            f"no extractor for {name!r} in {HERE}; looked for {name}.py and "
            f"*-{name}.py. If it lives with another report, copy it here -- "
            f"this directory is meant to stand on its own.")
    if len(prefixed) == 1:
        return prefixed[0]

    group = _prefix_of(caller) if caller is not None else None
    if group is not None:
        same = [p for p in prefixed if p.name.startswith(group)]
        if len(same) == 1:
            return same[0]

    raise ModuleNotFoundError(
        f"{name!r} is ambiguous in {HERE}: "
        f"{', '.join(p.name for p in prefixed)}. "
        f"Caller {caller.name if caller else 'unknown'} is not in a group that "
        f"picks one, so name the file explicitly rather than letting this guess.")


def load(name: str):
    """The extractor module called ``name``, whatever it is filed as here."""
    if name in sys.modules:
        return sys.modules[name]

    # Whoever called us, so a group's script gets its own group's copy.
    frame = sys._getframe(1)
    caller = Path(frame.f_code.co_filename).resolve()
    target = _resolve(name, caller)

    spec = importlib.util.spec_from_file_location(name, target)
    module = importlib.util.module_from_spec(spec)
    # Registered before execution, so a dependency cycle resolves to the
    # partially-initialised module rather than re-entering this loader.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        del sys.modules[name]
        raise
    return module
