"""Resolve every number in the report to a committed sidecar.

This report was patched eight times, and an audit of the eighth pass found two
dozen places where the prose no longer matched the artefact it cited, plus eight
claims whose numbers appeared in no committed file at all. Prose does not stay
in step with data by discipline alone, so it is no longer asked to. Every figure
in the generated report is a reference into a sidecar, resolved when the document
is built, and a reference that will not resolve stops the build instead of
reaching a reader.

Two kinds of fact exist. A *direct* fact is a JSON Pointer into one sidecar, and
carries no arithmetic: what the figure plotted is what the sentence says. A
*derived* fact names its inputs and the expression over them, so a number the
prose computes -- a difference, a ratio, a percentage lost -- is still traceable
to the sidecars it came from, and still recomputed on every build rather than
frozen at whatever it was when someone typed it.
"""

from __future__ import annotations

import ast
import json
import operator
from pathlib import Path

MINUS = "−"          # the report sets minus signs as U+2212, not hyphen
LNB_LO_HZ = 9.75e9        # every ppm figure in the report is against this LO


class FactError(Exception):
    """A reference that will not resolve. Always fatal; never warned past."""


def _unescape(token: str) -> str:
    # RFC 6901: ~1 is '/', ~0 is '~', and in that order or '~01' decodes wrong.
    return token.replace("~1", "/").replace("~0", "~")


def json_pointer(document: object, pointer: str) -> object:
    """Look up an RFC 6901 pointer, naming what was missing when it fails.

    A KeyError carrying only the leaf is useless when a sidecar has 1,200 leaves,
    so every failure reports the path that worked and what was available there.
    """
    if pointer in ("", "/") and not isinstance(document, (dict, list)):
        return document
    if not pointer.startswith("/"):
        raise FactError(f"pointer must start with '/': {pointer!r}")
    node = document
    walked = ""
    for token in pointer.split("/")[1:]:
        key = _unescape(token)
        walked += "/" + token
        if isinstance(node, list):
            try:
                node = node[int(key)]
            except (ValueError, IndexError):
                raise FactError(
                    f"{walked}: no index {key!r} in a list of {len(node)}") from None
        elif isinstance(node, dict):
            if key not in node:
                near = ", ".join(sorted(node)[:8])
                raise FactError(
                    f"{walked}: no key {key!r}; available here: {near}") from None
            node = node[key]
        else:
            raise FactError(f"{walked}: {type(node).__name__} is not indexable")
    return node


_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub,
           ast.Mult: operator.mul, ast.Div: operator.truediv,
           ast.Pow: operator.pow}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def evaluate(expression: str, names: dict[str, float]) -> float:
    """Arithmetic over named inputs, and nothing else.

    Derived facts exist so the report can say "25.5% lost" and still be traced to
    the two rates it came from. That only helps if the expression is auditable,
    so this walks the AST and refuses calls, attributes, comprehensions and
    subscripts -- an expression here is arithmetic or it is a build failure.
    """
    def walk(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                return float(node.value)
            raise FactError(f"only numeric constants allowed: {node.value!r}")
        if isinstance(node, ast.Name):
            if node.id not in names:
                raise FactError(f"expression names {node.id!r}, which is not an input")
            return float(names[node.id])
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            return _BINARY[type(node.op)](walk(node.left), walk(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](walk(node.operand))
        raise FactError(
            f"{type(node).__name__} is not allowed in a derived fact expression")

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise FactError(f"cannot parse expression {expression!r}: {exc}") from None
    return walk(tree)


def _group(digits: str) -> str:
    return f"{int(digits):,}"


def _signed(value: float, body: str) -> str:
    # A zero takes no sign: "+0" in an applied-centre column reads as a measured
    # positive offset rather than as the absence of one.
    if float(value) == 0:
        return body
    return (MINUS if value < 0 else "+") + body


FORMATS: dict[str, object] = {}


def _format(name):
    def register(function):
        FORMATS[name] = function
        return function
    return register


@_format("int")
def _fmt_int(value):
    return _group(f"{round(float(value)):.0f}").replace("-", MINUS)


@_format("signed_int")
def _fmt_signed_int(value):
    return _signed(float(value), _group(f"{abs(round(float(value))):.0f}"))


@_format("hz")
def _fmt_hz(value):
    return _fmt_int(value) + " Hz"


@_format("signed_hz")
def _fmt_signed_hz(value):
    return _fmt_signed_int(value) + " Hz"


@_format("khz1")
def _fmt_khz1(value):
    return f"{float(value) / 1000:.1f}".replace("-", MINUS) + " kHz"


@_format("signed_khz1")
def _fmt_signed_khz1(value):
    return _signed(float(value), f"{abs(float(value)) / 1000:.1f}") + " kHz"


@_format("ppm1")
def _fmt_ppm1(value):
    return _signed(float(value), f"{abs(float(value)) / LNB_LO_HZ * 1e6:.1f}")


@_format("pct1")
def _fmt_pct1(value):
    """A fraction in [0, 1] rendered as a percentage.

    Rejecting values above 1 catches the one mistake that matters here: handing
    this an already-percentage number and silently publishing 2,550%.
    """
    number = float(value)
    if not -1.0 <= number <= 1.0:
        raise FactError(f"pct1 expects a fraction, got {number!r}")
    return f"{number * 100:.1f}%".replace("-", MINUS)


@_format("pct2")
def _fmt_pct2(value):
    number = float(value)
    if not -1.0 <= number <= 1.0:
        raise FactError(f"pct2 expects a fraction, got {number!r}")
    return f"{number * 100:.2f}%".replace("-", MINUS)


for _places in (0, 1, 2, 3, 4):
    def _fixed(value, places=_places):
        return f"{float(value):.{places}f}".replace("-", MINUS)

    def _fixed_signed(value, places=_places):
        # For quantities whose sign is the point -- an offset, a bias, a shift --
        # so a positive one reads "+3.5" rather than bare "3.5".
        return _signed(float(value), f"{abs(float(value)):.{places}f}")

    FORMATS[f"f{_places}"] = _fixed
    # Deliberately unscaled, unlike khz1: several sidecars already store kHz, and
    # a formatter that silently divides by 1000 again turns −130.5 into −0.1.
    FORMATS[f"signed_f{_places}"] = _fixed_signed


@_format("raw")
def _fmt_raw(value):
    return str(value)


@_format("ci_int")
def _fmt_ci_int(value):
    low, high = value
    return f"[{_fmt_int(low)}, {_fmt_int(high)}]"


@_format("ci_signed_int")
def _fmt_ci_signed_int(value):
    low, high = value
    return f"[{_fmt_signed_int(low)}, {_fmt_signed_int(high)}]"


@_format("ci_f3")
def _fmt_ci_f3(value):
    low, high = value
    return f"[{FORMATS['f3'](low)}, {FORMATS['f3'](high)}]"


@_format("ci_khz1")
def _fmt_ci_khz1(value):
    low, high = value
    return (f"[{FORMATS['f1'](float(low) / 1000)}, "
            f"{FORMATS['f1'](float(high) / 1000)}] kHz")


class FactBook:
    """The report's numbers, each one still attached to where it came from."""

    def __init__(self, root: Path, facts: dict[str, dict]):
        self.root = Path(root)
        self.facts = facts
        self._sidecars: dict[str, object] = {}
        self._values: dict[str, object] = {}
        self._resolving: list[str] = []
        self.used: set[str] = set()

    @classmethod
    def load(cls, root: Path) -> "FactBook":
        root = Path(root)
        merged: dict[str, dict] = {}
        directory = root / "source" / "facts"
        for path in sorted(directory.glob("*.json")):
            block = json.loads(path.read_text())
            for key, spec in block.items():
                if key in merged:
                    raise FactError(
                        f"fact {key!r} defined twice: {merged[key]['_from']} and {path.name}")
                merged[key] = {**spec, "_from": path.name}
        if not merged:
            raise FactError(f"no fact definitions found under {directory}")
        return cls(root, merged)

    def sidecar(self, relative: str) -> object:
        if relative not in self._sidecars:
            path = self.root / relative
            if not path.exists():
                raise FactError(f"sidecar {relative} does not exist")
            self._sidecars[relative] = json.loads(path.read_text())
        return self._sidecars[relative]

    def value(self, name: str):
        """The raw value behind a fact, resolving derived facts recursively."""
        if name in self._values:
            self.used.add(name)
            return self._values[name]
        if name not in self.facts:
            raise FactError(f"no such fact: {name!r}")
        if name in self._resolving:
            cycle = " -> ".join([*self._resolving, name])
            raise FactError(f"derived facts form a cycle: {cycle}")

        spec = self.facts[name]
        self._resolving.append(name)
        try:
            if "expr" in spec:
                inputs = {alias: float(self.value(ref))
                          for alias, ref in (spec.get("inputs") or {}).items()}
                if not inputs:
                    raise FactError(f"{name}: a derived fact needs inputs")
                result = evaluate(spec["expr"], inputs)
            else:
                if "src" not in spec or "ptr" not in spec:
                    raise FactError(f"{name}: needs either src+ptr or expr+inputs")
                try:
                    result = json_pointer(self.sidecar(spec["src"]), spec["ptr"])
                except FactError as exc:
                    raise FactError(f"{name} -> {spec['src']}{exc}") from None
        finally:
            self._resolving.pop()

        self._values[name] = result
        self.used.add(name)
        return result

    def text(self, name: str) -> str:
        """The formatted string the template asked for."""
        spec = self.facts[name] if name in self.facts else None
        if spec is None:
            raise FactError(f"no such fact: {name!r}")
        style = spec.get("fmt", "raw")
        if style not in FORMATS:
            raise FactError(f"{name}: unknown format {style!r}; "
                            f"known: {', '.join(sorted(FORMATS))}")
        try:
            return FORMATS[style](self.value(name))
        except FactError:
            raise
        except (TypeError, ValueError) as exc:
            raise FactError(f"{name}: format {style!r} rejected the value: {exc}") from None

    def provenance(self, name: str) -> dict:
        """Where a fact came from, for the manifest the report ships with."""
        spec = self.facts[name]
        record = {"fact": name, "defined_in": spec.get("_from"),
                  "format": spec.get("fmt", "raw"), "value": self.value(name)}
        if "expr" in spec:
            record["expr"] = spec["expr"]
            record["inputs"] = {alias: self.provenance(ref)
                                for alias, ref in (spec.get("inputs") or {}).items()}
        else:
            record["src"] = spec["src"]
            record["ptr"] = spec["ptr"]
        if spec.get("note"):
            record["note"] = spec["note"]
        return record

    def unused(self) -> list[str]:
        """Facts defined but never cited.

        Not fatal -- a fact can legitimately outlive the sentence that used it --
        but it is reported, because the usual cause is prose that was deleted
        while the claim it made stayed in the manifest looking supported.
        """
        return sorted(set(self.facts) - self.used)
