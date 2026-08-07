"""Mesh integrity tests for the printable STLs.

These exist because a mesh can look perfect in a 3D viewer and still slice with
holes. A viewer draws triangles; a slicer decides what is *inside*, and for that
it needs a single closed shell with consistent winding. Overlapping shells,
inverted normals and zero-area faces all render fine and print wrong.

    uv run --with numpy --with trimesh --with manifold3d --with rtree \
           --with pytest pytest test_mesh.py -v
"""
import math
import numpy as np
import pytest
import trimesh

PRINT_STL = "quad-clamp-print.stl"
COUPON_STL = "clamp-fit-test.stl"

# Probe points are derived from the mesh bounds rather than hardcoded, so they
# survive changes to LEN_MAX, FOOT_CLR and friends. Print coordinates: bed at
# z=0, foot at the top.
def solid_points(m):
    zmax = m.bounds[1][2]
    xmax = m.bounds[1][0]
    return {
        "rod centre, inside the cap": (0.0, 0.0, 3.0),
        "rod on a 45 degree diagonal": (12.0, 12.0, 3.0),
        "rod directly under an arm": (15.0, 0.0, 3.0),
        "mid arm": (60.0, 0.0, 3.0),
        # The clamp's inboard back only exists above H - WEB_T_MAX, so probe it
        # inside the web depth; higher up the section is aft-face wedge.
        "arm where it buries into the clamp": (xmax - 48.0, 0.0, 3.0),
        "rod core, mid column": (0.0, 0.0, zmax * 0.5),
        "foot core": (15.0, 0.0, zmax - 8.0),
        "foot, out near the rim": (60.0, 0.0, zmax - 8.0),
    }


def void_points(m):
    zmax = m.bounds[1][2]
    return {
        "inside a clamp bore": (100.0, 0.0, 8.0),
        "above the foot entirely": (0.0, 0.0, zmax + 20.0),
        "outside the part entirely": (200.0, 200.0, 50.0),
    }


@pytest.fixture(scope="module")
def mesh():
    return trimesh.load(PRINT_STL)


def test_single_body(mesh):
    """A slicer that sees several shells has to guess how they combine."""
    assert mesh.body_count == 1, (
        f"{mesh.body_count} separate shells: the clamps, web arms and pedestal "
        f"were emitted as overlapping solids instead of being unioned"
    )


def test_watertight(mesh):
    assert mesh.is_watertight, "open edges: inside/outside is undefined"


def test_winding_is_outward(mesh):
    """Inverted normals turn a solid into a hole."""
    assert mesh.volume > 0, (
        f"signed volume {mesh.volume/1000:.1f} cm3 is negative, so at least one "
        f"shell has its faces wound inside-out"
    )


def test_degenerate_faces_are_negligible(mesh):
    """A zero-area face contributes no segment to any layer, so it is harmless
    on its own -- what matters is that the mesh stays watertight around it.
    The threshold is a smell check: revolving profile points that sit on the
    axis used to produce 144 of these, and a boolean union leaves the odd
    sliver at a corner.
    """
    n = int((mesh.area_faces <= 1e-9).sum())
    assert n <= 0.005 * len(mesh.faces), (
        f"{n} zero-area triangles out of {len(mesh.faces)} - too many to be "
        f"boolean slivers, check for profile points on the axis"
    )
    assert mesh.is_watertight, "degenerate faces have opened the mesh"


def test_volume_is_plausible(mesh):
    """A union is smaller than the sum of its overlapping parts.

    The pedestal is modelled solid, so this is the whole swept volume -- what
    it weighs is the slicer's infill setting, not a property of the mesh.
    """
    v = mesh.volume / 1000.0
    assert 700.0 < v < 1050.0, f"volume {v:.0f} cm3 outside the expected range"


def test_points_that_must_be_solid(mesh):
    """The gaps the eye sees in the rod are these points reading as empty."""
    bad = [f"{n} at {p}" for n, p in solid_points(mesh).items()
           if not bool(mesh.contains(np.array([p]))[0])]
    assert not bad, "not solid: " + "; ".join(bad)


def test_points_that_must_be_empty(mesh):
    bad = [f"{n} at {p}" for n, p in void_points(mesh).items()
           if bool(mesh.contains(np.array([p]))[0])]
    assert not bad, "solid but should be void: " + "; ".join(bad)


def test_even_odd_slicer_agrees(mesh):
    """Model the fill rule directly: cast a ray, count crossings.

    Some slicers union overlapping shells, others use even-odd parity. On a
    single closed shell the two agree, which is the whole point of unioning.
    """
    # An oblique direction on purpose: a straight +Z ray from a probe sitting
    # on y=0 travels along mesh grid edges and double-counts coincident
    # triangles, which looks like a defect and is not one.
    d = np.array([0.11, 0.19, 1.0])
    up = np.array([d / np.linalg.norm(d)])
    bad = []
    for name, p in solid_points(mesh).items():
        n = len(mesh.ray.intersects_location([np.array(p)], up)[0])
        if n % 2 == 0:
            bad.append(f"{name} ({n} crossings)")
    assert not bad, "even-odd parity reads these as holes: " + "; ".join(bad)


def test_coupon_is_also_clean():
    c = trimesh.load(COUPON_STL)
    assert c.body_count == 1 and c.is_watertight and c.volume > 0, (
        f"coupon: bodies={c.body_count} watertight={c.is_watertight} "
        f"volume={c.volume/1000:.2f} cm3"
    )
