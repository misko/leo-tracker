"""Render the report from its section templates and its sidecars.

    python source/build.py build     # write REPORT.md and the fact manifest
    python source/build.py check     # fail if the committed report has drifted

`check` is the point of the exercise. REPORT.md stays committed so it renders on
the forge like any other document, but it is output: if someone edits a number in
the rendered file, or a sidecar is regenerated with different values, the two
stop agreeing and this says so instead of leaving it for a reader to find.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from factbook import FactBook, FactError          # noqa: E402

REPORT_ROOT = Path(__file__).resolve().parent.parent
REFERENCE = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")

BANNER = """<!-- GENERATED FILE -- do not edit.

    Sections live in source/sections/*.md and every number is a reference into a
    committed sidecar, resolved by source/build.py.  Edit the templates or the
    facts, then re-run:  python source/build.py build
    To check this file still matches its sources:  python source/build.py check
-->
"""


def section_files(root: Path, complete: bool = True) -> list[Path]:
    """The section templates, in document order, checked against the manifest.

    REPORT.md is the published document, and a build that silently emitted only
    the sections converted so far would replace it with a fragment -- which is
    exactly what happened once. MANIFEST names every section the finished report
    must contain, so a partial conversion fails loudly instead.
    """
    directory = root / "source" / "sections"
    files = sorted(directory.glob("*.md"))
    if not files:
        raise FactError(f"no section templates under {directory}")

    manifest = directory / "MANIFEST"
    if manifest.exists():
        expected = [line.strip() for line in manifest.read_text().splitlines()
                    if line.strip() and not line.startswith("#")]
        present = {path.name for path in files}
        missing = [name for name in expected if name not in present]
        if missing and complete:
            raise FactError(
                f"{len(missing)} section(s) named in MANIFEST are not yet "
                f"converted: {', '.join(missing)}\n"
                f"  build a preview instead: python source/build.py build "
                f"--out source/preview.md")
        unlisted = sorted(present - set(expected))
        if unlisted:
            raise FactError(f"section(s) not named in MANIFEST: {', '.join(unlisted)}")
        return [directory / name for name in expected if name in present]
    return files


def render(root: Path, complete: bool = True) -> tuple[str, FactBook]:
    """Substitute every reference, collecting failures rather than stopping at one.

    Reporting one broken reference per run would make converting a 1,900-line
    document a build-fix-build loop of a few dozen iterations, so every failure in
    the pass is gathered and raised together.
    """
    book = FactBook.load(root)
    problems: list[str] = []
    pieces: list[str] = []

    for path in section_files(root, complete):
        text = path.read_text()

        def substitute(match: re.Match) -> str:
            name = match.group(1)
            try:
                return book.text(name)
            except FactError as exc:
                line = text.count("\n", 0, match.start()) + 1
                problems.append(f"{path.name}:{line}: {exc}")
                return match.group(0)

        pieces.append(REFERENCE.sub(substitute, text))

    if problems:
        raise FactError("unresolved references:\n  " + "\n  ".join(problems))

    # A blank line between sections, not a bare newline: a heading that
    # butts against the previous paragraph is only a heading by the
    # grace of the renderer, and this document is read on more than one.
    body = "\n\n".join(piece.strip("\n") for piece in pieces) + "\n"
    return BANNER + "\n" + body, book


def manifest(book: FactBook) -> dict:
    return {
        "schema": "report-facts/1",
        "fact_count": len(book.facts),
        "cited": sorted(book.used),
        "uncited": book.unused(),
        "facts": {name: book.provenance(name) for name in sorted(book.used)},
    }


def command_build(root: Path, out: Path | None) -> int:
    # A preview is allowed to be partial; only REPORT.md must be the whole report.
    text, book = render(root, complete=out is None)
    target = out or (root / "REPORT.md")
    target.write_text(text)
    if out is None:
        (root / "source" / "facts-manifest.json").write_text(
            json.dumps(manifest(book), indent=2, sort_keys=True) + "\n")
    stale = book.unused()
    print(f"wrote {target.name}  ({text.count(chr(10)) + 1} lines, "
          f"{len(book.used)} facts cited of {len(book.facts)} defined)")
    if stale:
        print(f"note: {len(stale)} fact(s) defined but never cited: "
              f"{', '.join(stale[:10])}{' ...' if len(stale) > 10 else ''}")
    return 0


def command_check(root: Path) -> int:
    text, book = render(root)
    current = (root / "REPORT.md").read_text()
    if current == text:
        print(f"REPORT.md matches its sources ({len(book.used)} facts cited)")
        return 0
    diff = difflib.unified_diff(current.splitlines(True), text.splitlines(True),
                                "REPORT.md (committed)", "REPORT.md (rebuilt)",
                                n=1)
    sys.stdout.writelines(diff)
    print("\nREPORT.md has drifted from its sources; run: python source/build.py build")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "check"))
    parser.add_argument("--root", type=Path, default=REPORT_ROOT)
    parser.add_argument("--out", type=Path, default=None,
                        help="write here instead of REPORT.md; use while "
                             "converting, so a partial report never lands")
    args = parser.parse_args(argv)
    try:
        if args.action == "build":
            return command_build(args.root, args.out)
        return command_check(args.root)
    except FactError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
