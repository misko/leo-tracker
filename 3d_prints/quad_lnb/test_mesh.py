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

# Probe points in PRINT coordinates: bed at z=0, part rising to z~180.
# The flush face is the first layer, so installed z = H - print z.
SOLID_POINTS = {
    "rod centre, inside the cap": (0.0, 0.0, 5.0),
    "rod on a 45 degree diagonal": (12.0, 12.0, 5.0),
    "rod directly under an arm": (15.0, 0.0, 5.0),
    "arm/rod junction": (22.0, 0.0, 8.0),
    "mid arm": (60.0, 0.0, 5.0),
    "arm where it buries into the clamp": (79.0, 0.0, 5.0),
    "rod wall well below the cap": (22.5, 0.0, 40.0),
    "foot flare wall": (76.0, 0.0, 177.0),
    "rod core, once hollow": (0.0, 0.0, 100.0),
    "foot core, once hollow": (60.0, 0.0, 175.0),
}

# Points that must be EMPTY. If these read solid the mesh is inverted.
VOID_POINTS = {
    "inside a clamp bore": (100.0, 0.0, 10.0),
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


def test_no_degenerate_faces(mesh):
    areas = mesh.area_faces
    assert (areas > 1e-9).all(), (
        f"{int((areas <= 1e-9).sum())} zero-area triangles, typically from "
        f"revolving a profile point that sits on the axis"
    )


def test_volume_is_plausible(mesh):
    """A union is smaller than the sum of its overlapping parts.

    The pedestal is modelled solid, so this is the whole swept volume -- what
    it weighs is the slicer's infill setting, not a property of the mesh.
    """
    v = mesh.volume / 1000.0
    assert 850.0 < v < 1050.0, f"volume {v:.0f} cm3 outside the expected range"


@pytest.mark.parametrize("name", list(SOLID_POINTS))
def test_points_that_must_be_solid(mesh, name):
    """The gaps the eye sees in the rod are these points reading as empty."""
    p = np.array([SOLID_POINTS[name]])
    assert bool(mesh.contains(p)[0]), f"{name} is not solid: {SOLID_POINTS[name]}"


@pytest.mark.parametrize("name", list(VOID_POINTS))
def test_points_that_must_be_empty(mesh, name):
    p = np.array([VOID_POINTS[name]])
    assert not bool(mesh.contains(p)[0]), f"{name} is solid but should be void"


def test_even_odd_slicer_agrees(mesh):
    """Model the fill rule directly: cast a ray, count crossings.

    Some slicers union overlapping shells, others use even-odd parity. On a
    single closed shell the two agree, which is the whole point of unioning.
    """
    up = np.array([[0.0, 0.0, 1.0]])
    bad = []
    for name, p in SOLID_POINTS.items():
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
