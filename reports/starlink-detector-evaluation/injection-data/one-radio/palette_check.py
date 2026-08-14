"""Six-checks validator for the categorical palette, in Python (no node here).

sRGB -> linear -> OKLab for the distances; Vienot/Brettel LMS projection for the
protan/deutan/tritan simulations.  Lines use the ADJACENT pairlist.
"""
from __future__ import annotations

import numpy as np

PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
           "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
SURFACE_LIGHT = "#fcfcfb"

_M1 = np.array([[0.4122214708, 0.5363325363, 0.0514459929],
                [0.2119034982, 0.6806995451, 0.1073969566],
                [0.0883024619, 0.2817188376, 0.6299787005]])
_M2 = np.array([[0.2104542553, 0.7936177850, -0.0040720468],
                [1.9779984951, -2.4285922050, 0.4505937099],
                [0.0259040371, 0.7827717662, -0.8086757660]])
# Hunt-Pointer-Estevez / Vienot LMS
_RGB2LMS = np.array([[17.8824, 43.5161, 4.11935],
                     [3.45565, 27.1554, 3.86714],
                     [0.0299566, 0.184309, 1.46709]])
_LMS2RGB = np.linalg.inv(_RGB2LMS)
_SIM = {
    "protan": np.array([[0, 2.02344, -2.52581], [0, 1, 0], [0, 0, 1]]),
    "deutan": np.array([[1, 0, 0], [0.494207, 0, 1.24827], [0, 0, 1]]),
    "tritan": np.array([[1, 0, 0], [0, 1, 0], [-0.395913, 0.801109, 0]]),
}


def hex_rgb(value: str) -> np.ndarray:
    v = value.lstrip("#")
    return np.array([int(v[i:i + 2], 16) / 255 for i in (0, 2, 4)])


def to_linear(c: np.ndarray) -> np.ndarray:
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def oklab(rgb: np.ndarray) -> np.ndarray:
    lms = _M1 @ to_linear(rgb)
    return _M2 @ np.cbrt(np.maximum(lms, 0.0))


def simulate(rgb: np.ndarray, kind: str) -> np.ndarray:
    lms = _RGB2LMS @ rgb
    return np.clip(_LMS2RGB @ (_SIM[kind] @ lms), 0, 1)


def de(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.linalg.norm(oklab(a) - oklab(b)) * 100)


def relative_luminance(rgb: np.ndarray) -> float:
    return float(np.dot(to_linear(rgb), [0.2126, 0.7152, 0.0722]))


def contrast(a: np.ndarray, b: np.ndarray) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def report() -> dict:
    cols = [hex_rgb(h) for h in PALETTE]
    surface = hex_rgb(SURFACE_LIGHT)
    labs = [oklab(c) for c in cols]
    out = {"lightness_L": [round(float(l[0]), 3) for l in labs],
           "chroma": [round(float(np.hypot(l[1], l[2])), 4) for l in labs],
           "contrast_on_surface": [round(contrast(c, surface), 2) for c in cols]}
    adjacent = [(i, i + 1) for i in range(len(cols) - 1)]
    normal = [(PALETTE[i], PALETTE[j], round(de(cols[i], cols[j]), 1))
              for i, j in adjacent]
    out["adjacent_normal_dE"] = normal
    out["worst_adjacent_normal_dE"] = min(v for *_, v in normal)
    worst_cvd = []
    for kind in _SIM:
        vals = [(PALETTE[i], PALETTE[j],
                 round(de(simulate(cols[i], kind), simulate(cols[j], kind)), 1))
                for i, j in adjacent]
        worst_cvd.append((kind, min(v for *_, v in vals)))
        out[f"adjacent_{kind}_dE"] = vals
    out["worst_adjacent_cvd_dE"] = min(v for _, v in worst_cvd)
    out["worst_cvd_by_kind"] = dict(worst_cvd)
    out["verdict"] = {
        "normal_floor_15": out["worst_adjacent_normal_dE"] >= 15,
        "cvd_target_8": out["worst_adjacent_cvd_dE"] >= 8,
        "relief_rule_needed": [h for h, c in zip(PALETTE, out["contrast_on_surface"])
                               if c < 3.0]}
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(report(), indent=1))
