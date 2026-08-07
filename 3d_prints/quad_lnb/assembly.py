"""Four C-clips in a cross on a central pedestal. One printed part.

Each clamp is tilted TILT deg outward with its mouth facing outward. All four
forward ends and the cross web's top face are cut by ONE horizontal plane, so
the assembly has a single flush face. Printed on it, every surface is within
TILT deg of vertical -> no supports.

The web's outer ends are trimmed to the clamps' actual outer surfaces (found by
ray marching), so each arm cradles its clamp instead of stabbing into it.

The centre drops into a solid pedestal that reaches below the LNBF tails and
flares into a foot, so the assembly stands on itself. The pedestal is modelled
solid rather than walled: how dense it actually prints is the slicer's infill
setting, not something baked into the geometry. Because the boresights
tilt outward, the LNBF bodies lean *inward* going down and converge on the
centre -- that convergence, not the clamps, is what sets the arm length.
"""
import math
import numpy as np
import trimesh
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

# ---- clamp (see clamp.py / lnbf-clamp.md) ----------------------------
NECK_D   = 40.0      # measured neck diameter
TIGHTEN  = 0.5       # diametral, taken off the as-tested coupon bore. The
                     # whole internal profile shrinks together, so the mouth
                     # follows -- which is right if the coupon was loose
                     # because of printer offset, since that offset applies to
                     # the bore and the mouth alike.
R_BORE   = 20.2 - TIGHTEN / 2
T_ROOT   = 6.0       # wall at bottom dead centre
T_TIP    = 3.5       # wall at the tips
LEN_MAX  = 28.0      # clamp width along the neck, at its longest. MUST be <=
                     # usable neck length. The flush trim always costs
                     # (profile height * tan TILT) = 19.3 mm of variation, so
                     # the narrow side lands at LEN_MAX - 19.3, and WEB_T_MAX
                     # falls with it -- see the note in quad-clamp.md.
LAND_OFF = 17.2 - TIGHTEN / 2   # neck is 37.2 mm flat-to-opposite-arc, so
                                # its flat sits 17.2 mm off the axis
LAND_AT  = -55.0     # clock angle of the land, and so of the neck's flat and
                     # its connector. -90 puts the connector straight inboard;
                     # every degree added swings it 1 deg away from the mast.
                     # Moves the land ONLY -- the mouth stays outboard so the
                     # web keeps landing on the clamp's stiff back.
PHI_CON  = 23.7
CHAM_DEG = 7.0
CHAM_R   = 2.5
TILT     = 27.0      # boresight tilt from zenith

# ---- the LNBF itself. MEASURE THESE - they set R_ARM -----------------
BODY_D   = 50.0      # body diameter behind the neck
BODY_L   = 65.0      # clamp aft face to back of connector, along the axis
HORN_D   = 66.0      # feedhorn diameter (render only)

# ---- assembly --------------------------------------------------------
R_ARM    = 105.0     # centre to bore axis, in the flush plane
WEB_W    = 24.0      # cross width = half the clamp's 48.7 mm width
WEB_T    =  7.5      # cross thickness below the flush face. Capped by
                     # WEB_T_MAX, which is what LEN_MAX leaves of the clamp's
                     # inboard back. At LEN_MAX 28 that ceiling is 7.8 mm.
PED_D    = 50.0      # pedestal outside diameter (solid - set density with
                     # the slicer's infill, not by hollowing the model)
FOOT_CLR = 90.0      # LNBF tail to ground: room for an F connector and boot
                     # (~40 mm) plus a drip loop (~50 mm)
FLARE_H  = 60.0      # flare height. Kept separate from FOOT_CLR so the column
                     # can be tall without the cone growing with it.
FOOT_D   = 160.0     # foot diameter. The 45 deg overhang limit caps this at
                     # FOOT_D <= PED_D + 2*FLARE_H, and FLARE_H <= FOOT_CLR
EMBED    = 0.8       # how far the web buries into the clamp wall (wall >= 6)
TEST_D   = 12.0      # fit-test coupon depth: a short slice of one clamp

PHI_TIP  = PHI_CON + CHAM_DEG
SWEEP    = 180.0 + 2 * PHI_TIP
N        = 168

phi_a, phi_b = PHI_TIP, PHI_TIP - SWEEP
tr = math.radians(TILT)
ST, CT = math.sin(tr), math.cos(tr)


def rz(d):
    c, s = math.cos(math.radians(d)), math.sin(math.radians(d))
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def ry(d):
    c, s = math.cos(math.radians(d)), math.sin(math.radians(d))
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def profile(phi_deg):
    s = abs(phi_deg + 90.0)
    t = T_ROOT - (T_ROOT - T_TIP) * min(s / abs(phi_a + 90.0), 1.0)
    r_in = R_BORE
    c = math.cos(math.radians(phi_deg - LAND_AT))
    if c > 1e-9:                                   # flat land, facing LAND_AT
        r_in = min(r_in, LAND_OFF / c)
    a = min(abs(phi_deg - phi_a), abs(phi_deg - phi_b))
    if a < CHAM_DEG:
        r_in += CHAM_R * (1.0 - a / CHAM_DEG)
    return r_in, R_BORE + t


phis = np.linspace(phi_a, phi_b, N)
inner = np.array([[profile(f)[0] * math.cos(math.radians(f)),
                   profile(f)[0] * math.sin(math.radians(f))] for f in phis])
outer = np.array([[profile(f)[1] * math.cos(math.radians(f)),
                   profile(f)[1] * math.sin(math.radians(f))] for f in phis])

# Rz(-90) puts the mouth on +X; Ry(TILT) tips the bore axis toward +X, carrying
# the mouth outboard and the closed back inboard, so the web lands on material.
M0 = ry(TILT) @ rz(-90)
H = LEN_MAX * CT - outer[:, 1].max() * ST          # the flush plane
zf = lambda y: (H + y * ST) / CT
zi, zo = zf(inner[:, 1]), zf(outer[:, 1])
OFF0 = np.array([R_ARM - H * math.tan(tr), 0.0, 0.0])

# The clamp's inboard back is bounded below by its aft face, a plane normal to
# the tilted bore axis. Below that world height the back simply does not exist,
# so the web has nothing to reach: this is the deepest the joint can ever be.
WEB_T_MAX = H + outer[:, 1].min() * ST
if WEB_T > WEB_T_MAX:
    raise SystemExit(f"WEB_T {WEB_T} exceeds {WEB_T_MAX:.1f} mm - the web would "
                     f"hang past the clamp's back and float")

r_aft = R_ARM - H * math.tan(tr)                   # bore axis at the clamp aft
r_tail = r_aft - BODY_L * ST                       # ... and at the LNBF tail
z_tail = -BODY_L * CT
Z_BASE = z_tail - FOOT_CLR                         # foot sits below the tails
FLARE = math.degrees(math.atan2((FOOT_D - PED_D) / 2, FLARE_H))

gap_adj = r_tail * math.sqrt(2) - BODY_D
gap_ped = r_tail - BODY_D / 2 - PED_D / 2
mouth = 2 * min(profile(f)[0] * math.cos(math.radians(f)) for f in phis if f > 0)
print(f"bore Ø{2*R_BORE:.2f} on a Ø{NECK_D:.1f} neck "
      f"({2*R_BORE-NECK_D:+.2f} mm diametral), mouth {mouth:.2f} mm")
print(f"  capture {(NECK_D-mouth)/2:.2f} mm per side = how far each arm flexes")
print(f"grip {min(zi.min(), zo.min()):.1f} to {max(zi.max(), zo.max()):.1f} mm, "
      f"flush plane z={H:.1f}, bore-axis spacing {R_ARM*math.sqrt(2):.0f} mm")
print(f"web {WEB_T:.1f} mm deep of a possible {WEB_T_MAX:.1f} mm "
      f"({100*WEB_T/WEB_T_MAX:.0f}% of the clamp's inboard back)")
bearing = LAND_AT - 90.0                           # land relative to outboard
print(f"land/connector {abs(bearing):.0f}° from outboard, "
      f"{180-abs(bearing):.0f}° from inboard (LAND_AT={LAND_AT:.0f})")
print(f"LNBF tail at r={r_tail:.1f} z={z_tail:.1f}")
print(f"  clearance LNBF to LNBF (adjacent) {gap_adj:+.1f} mm")
print(f"  clearance LNBF to pedestal       {gap_ped:+.1f} mm")
if min(gap_adj, gap_ped) < 5.0:
    print("  *** TIGHT - raise R_ARM or shrink PED_D ***")
print(f"pedestal {H - Z_BASE:.0f} mm tall, foot Ø{FOOT_D:.0f} at z={Z_BASE:.1f}, "
      f"{FOOT_CLR:.0f} mm under the tails")
print(f"  foot flare {FLARE:.1f}° from vertical over {FLARE_H:.0f} mm"
      + ("  *** OVER 45° - NEEDS SUPPORTS ***" if FLARE > 45 else "  (supportless)"))


def q(a, b, c, d):
    return [a, b, c, d]


def clamp_faces():
    f = []
    for k in range(N - 1):
        ox, oy = outer[k]; ox2, oy2 = outer[k + 1]
        ix, iy = inner[k]; ix2, iy2 = inner[k + 1]
        f.append(q((ox, oy, 0), (ox2, oy2, 0), (ox2, oy2, zo[k+1]), (ox, oy, zo[k])))
        f.append(q((ix, iy, zi[k]), (ix2, iy2, zi[k+1]), (ix2, iy2, 0), (ix, iy, 0)))
        f.append(q((ix, iy, zi[k]), (ox, oy, zo[k]),
                   (ox2, oy2, zo[k+1]), (ix2, iy2, zi[k+1])))
        f.append(q((ix2, iy2, 0), (ox2, oy2, 0), (ox, oy, 0), (ix, iy, 0)))
    for k, fwd in ((0, True), (N - 1, False)):
        ix, iy = inner[k]; ox, oy = outer[k]
        e = q((ix, iy, 0), (ox, oy, 0), (ox, oy, zo[k]), (ix, iy, zi[k]))
        f.append(e if fwd else e[::-1])
    return [np.array(x) for x in f]


CF = clamp_faces()
AZ = [0.0, 90.0, 180.0, 270.0]
XF = [(rz(a) @ M0, rz(a) @ OFF0) for a in AZ]
clamps = [[f @ M.T + o for f in CF] for M, o in XF]


def in_clamp(P, M, o):
    p = M.T @ (P - o)
    r = math.hypot(p[0], p[1])
    if r < 1e-9:
        return False
    d = math.degrees(math.atan2(p[1], p[0]))
    while d > phi_a:
        d -= 360.0
    while d < phi_a - 360.0:
        d += 360.0
    if d < phi_b:
        return False
    ri, ro = profile(d)
    return ri <= r <= ro and 0.0 <= p[2] <= zf(p[1])


def cradle_u(uhat, vhat, v, w, M, o):
    lo, u = None, 0.0
    while u < R_ARM + 45.0:
        if in_clamp(u * uhat + v * vhat + np.array([0, 0, w]), M, o):
            lo = u
            break
        u += 0.25
    if lo is None:
        return None
    a, b = lo - 0.25, lo
    for _ in range(24):
        m = 0.5 * (a + b)
        if in_clamp(m * uhat + v * vhat + np.array([0, 0, w]), M, o):
            b = m
        else:
            a = m
    return b + EMBED


NV, NW = 15, 7
vs = np.linspace(-WEB_W / 2, WEB_W / 2, NV)
ws = np.linspace(H - WEB_T, H, NW)


def web_arm(az, M, o):
    uhat = np.array([math.cos(math.radians(az)), math.sin(math.radians(az)), 0.0])
    vhat = np.array([-uhat[1], uhat[0], 0.0])
    U = np.zeros((NV, NW))
    for i, v in enumerate(vs):
        for j, w in enumerate(ws):
            r = cradle_u(uhat, vhat, v, w, M, o)
            if r is None:
                raise SystemExit(f"web arm at az={az} misses its clamp")
            U[i, j] = r
    P = lambda u, v, w: u * uhat + v * vhat + np.array([0, 0, w])
    f = []
    for i in range(NV - 1):
        for j in range(NW - 1):
            f.append(q(P(U[i, j], vs[i], ws[j]), P(U[i+1, j], vs[i+1], ws[j]),
                       P(U[i+1, j+1], vs[i+1], ws[j+1]), P(U[i, j+1], vs[i], ws[j+1])))
    for i, sgn in ((0, -1), (NV - 1, 1)):
        for j in range(NW - 1):
            e = q(P(0, vs[i], ws[j]), P(U[i, j], vs[i], ws[j]),
                  P(U[i, j+1], vs[i], ws[j+1]), P(0, vs[i], ws[j+1]))
            f.append(e if sgn > 0 else e[::-1])
    for j, sgn in ((0, -1), (NW - 1, 1)):
        for i in range(NV - 1):
            e = q(P(0, vs[i], ws[j]), P(U[i, j], vs[i], ws[j]),
                  P(U[i+1, j], vs[i+1], ws[j]), P(0, vs[i+1], ws[j]))
            f.append(e if sgn > 0 else e[::-1])
    for i in range(NV - 1):                       # inner end cap, subdivided to
        for j in range(NW - 1):                   # match the walls exactly --
            f.append(q(P(0, vs[i], ws[j]),        # a single quad here leaves
                       P(0, vs[i], ws[j+1]),      # T-junctions and the shell
                       P(0, vs[i+1], ws[j+1]),    # never closes
                       P(0, vs[i+1], ws[j])))
    return [np.array(x) for x in f]


def revolve_closed(loop, n=72):
    """Revolve a closed (r, z) polygon into one closed shell.

    Segments lying on the axis are skipped and segments touching it emit
    triangles, so no zero-area faces are produced.
    """
    f = []
    for k in range(len(loop)):
        (r0, za), (r1, zb) = loop[k], loop[(k + 1) % len(loop)]
        if r0 < 1e-9 and r1 < 1e-9:
            continue
        for i in range(n):
            a0, a1 = 2*math.pi*i/n, 2*math.pi*(i+1)/n
            p00 = (r0*math.cos(a0), r0*math.sin(a0), za)
            p01 = (r0*math.cos(a1), r0*math.sin(a1), za)
            p11 = (r1*math.cos(a1), r1*math.sin(a1), zb)
            p10 = (r1*math.cos(a0), r1*math.sin(a0), zb)
            if r0 < 1e-9:
                f.append(np.array([p00, p11, p10, p00]))
            elif r1 < 1e-9:
                f.append(np.array([p00, p01, p11, p00]))
            else:
                f.append(np.array([p00, p01, p11, p10]))
    return f


pedestal = revolve_closed([
    (0.0,        H),                       # top face, flush with the cross
    (PED_D / 2,  H),
    (PED_D / 2,  Z_BASE + FLARE_H),        # straight column
    (FOOT_D / 2, Z_BASE),                  # flare out to the foot
    (0.0,        Z_BASE),                  # foot underside, solid across
])

arms = [web_arm(a, M, o) for a, (M, o) in zip(AZ, XF)]
allf = [f for c in clamps for f in c] + [x for a in arms for x in a] + pedestal

ap = np.vstack(allf)
R45 = rz(45)
a45 = np.vstack([f @ R45.T for f in allf])
print(f"envelope {ap[:,0].max()-ap[:,0].min():.0f} x {ap[:,1].max()-ap[:,1].min():.0f}"
      f" x {ap[:,2].max()-ap[:,2].min():.0f} mm  "
      f"({max(a45[:,0].max()-a45[:,0].min(), a45[:,1].max()-a45[:,1].min()):.0f} mm "
      f"bed if rotated 45°)")


def shell(quads, name):
    """Quad soup -> one closed, outward-wound solid.

    fix_normals() is what makes this safe: winding by hand is easy to get
    backwards, and an inverted shell renders identically but slices as a void.
    """
    v, f = [], []
    for qd in quads:
        i = len(v)
        v.extend([tuple(x) for x in qd])
        f += [[i, i + 1, i + 2], [i, i + 2, i + 3]]
    m = trimesh.Trimesh(vertices=np.array(v, float), faces=np.array(f), process=True)
    m.update_faces(m.area_faces > 1e-8)          # drop zero-area triangles
    m.remove_unreferenced_vertices()
    m.fix_normals()
    if not m.is_watertight:
        raise SystemExit(f"{name}: shell is not closed, cannot union")
    if m.volume <= 0:
        raise SystemExit(f"{name}: still inside-out after fix_normals")
    return m


SHELLS = ([shell(c, f"clamp{i}") for i, c in enumerate(clamps)]
          + [shell(a, f"arm{i}") for i, a in enumerate(arms)]
          + [shell(pedestal, "pedestal")])
print(f"unioning {len(SHELLS)} shells "
      f"(sum {sum(m.volume for m in SHELLS)/1000:.0f} cm3 before overlaps merge)")
solid = trimesh.boolean.union(SHELLS, engine="manifold")
solid.fix_normals()
print(f"  -> 1 body={solid.body_count == 1}, watertight={solid.is_watertight}, "
      f"volume {solid.volume/1000:.0f} cm3")
if not (solid.is_watertight and solid.body_count == 1 and solid.volume > 0):
    raise SystemExit("union did not produce a single closed solid")

T = np.eye(4)
T[:3, :3] = np.diag([1.0, -1.0, -1.0])           # flush face down onto the bed
printed = solid.copy()
printed.apply_transform(T)
printed.apply_translation([0, 0, -printed.bounds[0][2]])
printed.export("quad-clamp-print.stl")
print(f"wrote quad-clamp-print.stl ({len(printed.faces)} triangles), "
      f"print height {printed.bounds[1][2]:.0f} mm")

vol = solid.volume / 1000.0
# The model is solid, so density is a slicer setting rather than geometry.
# grams ~ volume * infill * PLA density, +15% for perimeters and solid layers.
print(f"volume {vol:.0f} cm3 (modelled solid - set weight with infill):")
for pct in (10, 15, 25, 40):
    g = vol * pct / 100 * 1.24 * 1.15
    print(f"    {pct:2d}% infill -> {g:4.0f} g, roughly {g/11:.0f}-{g/8:.0f} h")

# Tip stability, now that the mount's own mass is worth counting. A solid base
# puts most of that mass low down, which drags the loaded CG toward the foot.
LNBF_G = 180.0
z_lnbf = (LEN_MAX + 34 - BODY_L) / 2 * CT
for pct in (15, 25):
    m_mount = vol * pct / 100 * 1.24 * 1.15
    m_lnbf = 4 * LNBF_G
    cg_z = (m_mount * solid.center_mass[2] + m_lnbf * z_lnbf) / (m_mount + m_lnbf)
    tip = math.degrees(math.atan2(FOOT_D / 2, cg_z - Z_BASE))
    print(f"  at {pct}% infill: mount {m_mount:.0f} g + LNBFs {m_lnbf:.0f} g, "
          f"CG {cg_z - Z_BASE:.0f} mm above the foot, tip angle {tip:.1f}°")


# ---- fit-test coupon: a short straight slice of one clamp ------------
def coupon_faces(depth):
    f = []
    for k in range(N - 1):
        ox, oy = outer[k]; ox2, oy2 = outer[k + 1]
        ix, iy = inner[k]; ix2, iy2 = inner[k + 1]
        f.append(q((ox, oy, 0), (ox2, oy2, 0), (ox2, oy2, depth), (ox, oy, depth)))
        f.append(q((ix, iy, depth), (ix2, iy2, depth), (ix2, iy2, 0), (ix, iy, 0)))
        f.append(q((ix, iy, depth), (ox, oy, depth),
                   (ox2, oy2, depth), (ix2, iy2, depth)))
        f.append(q((ix2, iy2, 0), (ox2, oy2, 0), (ox, oy, 0), (ix, iy, 0)))
    for k, fwd in ((0, True), (N - 1, False)):
        ix, iy = inner[k]; ox, oy = outer[k]
        e = q((ix, iy, 0), (ox, oy, 0), (ox, oy, depth), (ix, iy, depth))
        f.append(e if fwd else e[::-1])
    return [np.array(x) for x in f]


coupon = shell(coupon_faces(TEST_D), "coupon")
coupon.export("clamp-fit-test.stl")
root = min(zi.min(), zo.min())
print(f"wrote clamp-fit-test.stl, {TEST_D:.0f} mm deep, "
      f"{coupon.volume/1000*1.24*0.9:.0f} g, ~15 min")
print(f"  arms are {TEST_D:.0f} mm wide vs {root:.1f} mm at the real root, so the "
      f"full clamp needs about {root/TEST_D:.1f}x the push you feel")

printo = [f @ np.diag([1.0, -1.0, -1.0]).T for f in allf]
dz = min(f[:, 2].min() for f in printo)
printo = [f - np.array([0, 0, dz]) for f in printo]

# ---- render ----------------------------------------------------------
def revolve(rz_pairs, n=72, flip=False):
    """Open surface of revolution. Render only -- never goes into an STL."""
    f = []
    for k in range(len(rz_pairs) - 1):
        (r0, za), (r1, zb) = rz_pairs[k], rz_pairs[k + 1]
        for i in range(n):
            a0, a1 = 2*math.pi*i/n, 2*math.pi*(i+1)/n
            p = [(r0*math.cos(a0), r0*math.sin(a0), za),
                 (r0*math.cos(a1), r0*math.sin(a1), za),
                 (r1*math.cos(a1), r1*math.sin(a1), zb),
                 (r1*math.cos(a0), r1*math.sin(a0), zb)]
            f.append(np.array(p[::-1] if flip else p))
    return f


BASE = np.array([0.560, 0.530, 0.900])
TEAL = np.array([0.114, 0.620, 0.459])
LIGHT = np.array([0.45, -0.55, 0.70]); LIGHT /= np.linalg.norm(LIGHT)


def shaded(polys, base):
    out = []
    for f in polys:
        n = np.cross(f[1] - f[0], f[2] - f[0]); ln = np.linalg.norm(n)
        n = n / ln if ln > 1e-12 else np.array([0, 0, 1.0])
        sh = 0.46 + 0.54 * max(0.0, float(n @ LIGHT))
        out.append(np.clip(base * sh + 0.16 * sh, 0, 1))
    return out


def lnbf(M, o):
    """Stand-in LNBF: body, neck, feedhorn, along the clamp's bore axis."""
    f = (revolve([(BODY_D/2, -BODY_L), (BODY_D/2, -2.0)]) +
         revolve([(0, -BODY_L), (BODY_D/2, -BODY_L)]) +
         revolve([(20.0, -2.0), (20.0, LEN_MAX + 4)]) +
         revolve([(HORN_D/2, LEN_MAX + 4), (HORN_D/2, LEN_MAX + 34)]) +
         revolve([(0, LEN_MAX + 34), (HORN_D/2, LEN_MAX + 34)]))
    off = np.array([BODY_D / 4 * math.cos(math.radians(LAND_AT)),
                    BODY_D / 4 * math.sin(math.radians(LAND_AT)), 0.0])
    f += [x + off for x in                        # F connector, on the flat side
          revolve([(7.0, -BODY_L - 18), (7.0, -BODY_L)]) +
          revolve([(0.0, -BODY_L - 18), (7.0, -BODY_L - 18)])]
    return [x @ M.T + o for x in f]


LN = [x for M, o in XF for x in lnbf(M, o)]


def draw(ax, parts, el, az, title, ground=None):
    if ground is not None:
        s = 175
        ax.add_collection3d(Poly3DCollection(
            [np.array([(-s, -s, ground), (s, -s, ground), (s, s, ground),
                       (-s, s, ground)])], facecolors=[(0.88, 0.88, 0.90)],
            edgecolors="none", alpha=0.5))
    pts = []
    for polys, base, alpha in parts:
        c = shaded(polys, base)
        ax.add_collection3d(Poly3DCollection(polys, facecolors=c, edgecolors=c,
                                             linewidths=0.25, alpha=alpha))
        pts.append(np.vstack(polys))
    a = np.vstack(pts); c = (a.max(0) + a.min(0)) / 2
    sp = (a.max(0) - a.min(0)).max() / 2 * 1.02
    ax.set_xlim(c[0]-sp, c[0]+sp); ax.set_ylim(c[1]-sp, c[1]+sp)
    ax.set_zlim(c[2]-sp, c[2]+sp)
    ax.set_box_aspect((1, 1, 1)); ax.view_init(elev=el, azim=az)
    ax.set_axis_off(); ax.set_title(title, fontsize=12, color="#333333", pad=-6)


P = [(allf, BASE, 1.0)]
PL = [(allf, BASE, 1.0), (LN, TEAL, 0.35)]
fig = plt.figure(figsize=(15, 9.4), dpi=105)
for i, (t, el, az, parts, gr) in enumerate([
        ("standing, with LNBFs", 12, -55, PL, Z_BASE),
        ("side on - foot clears the tails", 2, -90, PL, Z_BASE),
        ("plan view", 86, -90, P, None),
        ("mount alone, isometric", 20, -55, P, Z_BASE),
        ("print orientation, flush face on bed", 20, -55, [(printo, BASE, 1.0)], 0.0),
        ("cradle detail, one junction", 12, 150,
         [(clamps[0] + arms[0], BASE, 1.0)], None)], 1):
    draw(fig.add_subplot(2, 3, i, projection="3d"), parts, el, az, t, ground=gr)
fig.suptitle("Four C-clips at 27° on a flush cross with a dropped pedestal · "
             "one printed part · stands on its own foot",
             fontsize=14, color="#222222", y=0.965)
fig.tight_layout(rect=[0, 0, 1, 0.94])
fig.savefig("quad-clamp-views.png", facecolor="white")
print("wrote quad-clamp-views.png")
