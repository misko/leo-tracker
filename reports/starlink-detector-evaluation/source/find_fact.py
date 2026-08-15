"""Search every committed sidecar for a number the prose claims.

Converting the report means answering one question a few hundred times: this
sentence says 25.5%, so where did that come from? Doing it by eye is how the
report drifted in the first place, so it is done mechanically.

    python source/find_fact.py 424990          # a plain value
    python source/find_fact.py 0.255 --pct     # also try it as a percentage
    python source/find_fact.py 605521 --tol 2  # allow rounding slack
    python source/find_fact.py --key miscentring   # search key names instead

Prints one line per hit as  <sidecar>  <json-pointer>  <value>, which is exactly
what a fact definition needs. No hits is a real answer: it means the number is
in no committed artefact, and the claim resting on it has to be recomputed or
withdrawn.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

FIGURES = Path(__file__).resolve().parent.parent / "figures"


def escape(token: str) -> str:
    return str(token).replace("~", "~0").replace("/", "~1")


def leaves(node, pointer=""):
    """Every scalar in a document, with the pointer that reaches it.

    Lists are yielded whole as well as elementwise: confidence intervals live in
    the sidecars as two-element lists and the report quotes them as a pair, so a
    search for the pair has to be able to hit the list itself.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            yield from leaves(value, f"{pointer}/{escape(key)}")
    elif isinstance(node, list):
        yield pointer, node
        for index, value in enumerate(node):
            yield from leaves(value, f"{pointer}/{index}")
    else:
        yield pointer, node


def sidecars(root: Path):
    for path in sorted(root.rglob("*.json")):
        try:
            yield path, json.loads(path.read_text())
        except ValueError:
            continue


def close(value, target: float, tolerance: float) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if target == 0:
        return abs(value) <= tolerance
    return abs(value - target) <= max(tolerance, abs(target) * 1e-9)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("value", nargs="?", type=float)
    parser.add_argument("--key", help="substring match on key names instead of values")
    parser.add_argument("--tol", type=float, default=0.51,
                        help="absolute tolerance; default catches display rounding")
    parser.add_argument("--pct", action="store_true",
                        help="also try the value scaled by 1/100 and by 100")
    parser.add_argument("--units", action="store_true",
                        help="also try the value scaled by 1000 and by 1/1000")
    parser.add_argument("--root", type=Path, default=FIGURES)
    args = parser.parse_args(argv)

    if args.key is None and args.value is None:
        parser.error("give a value, or --key")

    # Each target carries the factor it was scaled by, so the tolerance scales
    # with it. Without that, dividing a five-digit count by 1000 to try it as kHz
    # leaves a tolerance of half a unit against a target of 17.1, which matches
    # every unrelated percentage in the corpus.
    targets = []
    if args.value is not None:
        targets = [(args.value, 1.0, "")]
        if args.pct:
            targets += [(args.value / 100.0, 0.01, " (as a fraction)"),
                        (args.value * 100.0, 100.0, " (as a percentage)")]
        if args.units:
            # A number quoted in kHz usually lives in the sidecar in Hz.
            targets += [(args.value * 1000.0, 1000.0, " (as kHz->Hz)"),
                        (args.value / 1000.0, 0.001, " (as Hz->kHz)")]

    hits = 0
    for path, document in sidecars(args.root):
        relative = path.relative_to(args.root.parent)
        for pointer, value in leaves(document):
            if args.key is not None:
                if args.key.lower() in pointer.lower():
                    print(f"{relative}  {pointer}  {value!r}")
                    hits += 1
                continue
            for target, factor, label in targets:
                if close(value, target, args.tol * factor):
                    print(f"{relative}  {pointer}  {value!r}{label}")
                    hits += 1
                    break

    if not hits:
        print("no committed sidecar holds this value -- the claim resting on it "
              "is unsupported and must be recomputed or withdrawn")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
