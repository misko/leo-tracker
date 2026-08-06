"""Four C-clips in a cross on a central pedestal. One printed part.

Each clamp is tilted TILT deg outward with its mouth facing outward. All four
forward ends and the cross web's top face are cut by ONE horizontal plane, so
the assembly has a single flush face. Printed on it, every surface is within
TILT deg of vertical -> no supports.

The web's outer ends are trimmed to the clamps' actual outer surfaces (found by
ray marching), so each arm cradles its clamp instead of stabbing into it.

The centre drops into a hollow pedestal that reaches below the LNBF tails and
flares into a foot, so the assembly stands on itself. Because the boresights
tilt outward, the LNBF bodies lean *inward* going down and converge on the
centre -- that convergence, not the clamps, is what sets the arm length.
"""
import math, struct
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

# ---- clamp (see clamp.py / lnbf-clamp.md) ----------------------------
R_BORE   = 20.2      # bore radius, for a 40.0 neck
T_ROOT   = 6.0       # wall at bottom dead centre
T_TIP    = 3.5       # wall at the tips
LEN_MAX  = 42.0      # longest grip. MUST be <= usable neck length
LAND_Y   = -17.2     # flat land matching the neck's moulded flat
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
WEB_T    = 12.0      # cross thickness below the flush face
PED_D    = 50.0      # pedestal outside diameter
PED_WALL = 6.0       # pedestal wall
FOOT_D   = 90.0      # flared foot diameter
FOOT_CLR = 25.0      # LNBF tail to ground. The flare occupies all of it, so
                     # this and FOOT_D are linked by the 45 deg overhang limit:
                     # FOOT_D <= PED_D + 2*FOOT_CLR
EMBED    = 0.8       # how far the web buries into the clamp wall (wall >= 6)

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
    p = math.radians(phi_deg)
    s = abs(phi_deg + 90.0)
    t = T_ROOT - (T_ROOT - T_TIP) * min(s / abs(phi_a + 90.0), 1.0)
    r_in = R_BORE
    sp = math.sin(p)
    if sp < -1e-9:
        r_in = min(r_in, LAND_Y / sp)
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

r_aft = R_ARM - H * math.tan(tr)                   # bore axis at the clamp aft
r_tail = r_aft - BODY_L * ST                       # ... and at the LNBF tail
z_tail = -BODY_L * CT
Z_BASE = z_tail - FOOT_CLR                         # foot sits below the tails
FLARE = math.degrees(math.atan2((FOOT_D - PED_D) / 2, FOOT_CLR))

gap_adj = r_tail * math.sqrt(2) - BODY_D
gap_ped = r_tail - BODY_D / 2 - PED_D / 2
print(f"grip {min(zi.min(), zo.min()):.1f} to {max(zi.max(), zo.max()):.1f} mm, "
      f"flush plane z={H:.1f}, bore-axis spacing {R_ARM*math.sqrt(2):.0f} mm")
print(f"LNBF tail at r={r_tail:.1f} z={z_tail:.1f}")
print(f"  clearance LNBF to LNBF (adjacent) {gap_adj:+.1f} mm")
print(f"  clearance LNBF to pedestal       {gap_ped:+.1f} mm")
if min(gap_adj, gap_ped) < 5.0:
    print("  *** TIGHT - raise R_ARM or shrink PED_D ***")
print(f"pedestal {H - Z_BASE:.0f} mm tall, foot Ø{FOOT_D:.0f} at z={Z_BASE:.1f}, "
      f"{FOOT_CLR:.0f} mm under the tails")
print(f"  foot flare {FLARE:.1f}° from vertical"
      + ("  *** OVER 45° - needs supports ***" if FLARE > 45 else "  (supportless)"))


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
    f.append(q(P(0, vs[0], ws[0]), P(0, vs[-1], ws[0]),
               P(0, vs[-1], ws[-1]), P(0, vs[0], ws[-1])))
    return [np.array(x) for x in f]


def revolve(rz_pairs, n=72, flip=False):
    """Surface of revolution through a list of (r, z), as quads."""
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


ri_ped, ri_foot = PED_D / 2 - PED_WALL, FOOT_D / 2 - PED_WALL
pedestal = (
    revolve([(PED_D/2, H), (PED_D/2, Z_BASE + FOOT_CLR), (FOOT_D/2, Z_BASE)]) +
    revolve([(ri_foot, Z_BASE), (ri_ped, Z_BASE + FOOT_CLR),
             (ri_ped, H - WEB_T)], flip=True) +
    revolve([(ri_foot, Z_BASE), (FOOT_D/2, Z_BASE)], flip=True) +
    revolve([(0.0, H), (PED_D/2, H)], flip=True) +
    revolve([(0.0, H - WEB_T), (ri_ped, H - WEB_T)])
)

arms = [web_arm(a, M, o) for a, (M, o) in zip(AZ, XF)]
allf = [f for c in clamps for f in c] + [x for a in arms for x in a] + pedestal

ap = np.vstack(allf)
print(f"envelope {ap[:,0].max()-ap[:,0].min():.0f} x {ap[:,1].max()-ap[:,1].min():.0f}"
      f" x {ap[:,2].max()-ap[:,2].min():.0f} mm  "
      f"({(ap[:,0].max()-ap[:,0].min())/math.sqrt(2)+FOOT_D/2:.0f} mm bed if rotated 45°)")

FLIP = np.diag([1.0, -1.0, -1.0])
printo = [f @ FLIP.T for f in allf]
dz = min(f[:, 2].min() for f in printo)
printo = [f - np.array([0, 0, dz]) for f in printo]
print(f"print height {max(f[:,2].max() for f in printo):.0f} mm")

ring = np.vstack([outer, inner[::-1]])
area = 0.5 * abs(np.dot(ring[:, 0], np.roll(ring[:, 1], -1)) -
                 np.dot(ring[:, 1], np.roll(ring[:, 0], -1)))
vol = (4 * area * float(np.mean(np.concatenate([zi, zo])))
       + (2 * WEB_W * 2 * R_ARM - WEB_W ** 2) * WEB_T
       + math.pi * ((PED_D/2)**2 - ri_ped**2) * (H - WEB_T - Z_BASE)) / 1000.0
print(f"solid volume ~{vol:.0f} cm3; expect ~{vol*1.24*0.5:.0f}-{vol*1.24*0.65:.0f} g "
      f"and 14-20 h at 40% infill")


def write_stl(name, polys):
    tris = []
    for f in polys:
        tris.append((f[0], f[1], f[2])); tris.append((f[0], f[2], f[3]))
    with open(name, "wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", len(tris)))
        for a, b, c in tris:
            n = np.cross(b - a, c - a); ln = np.linalg.norm(n)
            n = n / ln if ln > 1e-12 else np.zeros(3)
            fh.write(struct.pack("<12fH", *n, *a, *b, *c, 0))
    print(f"wrote {name} ({len(tris)} triangles)")


write_stl("quad-clamp-print.stl", printo)
write_stl("quad-clamp-installed.stl", allf)

# ---- render ----------------------------------------------------------
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
