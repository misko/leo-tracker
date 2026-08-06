"""C-clip LNBF clamp: extended, flat-trimmed for supportless printing.

Base frame: bore axis along +Z, mouth at +Y, flat land at -Y.
Installed:  rotated TILT deg about +X, so the boresight sits TILT deg off
            zenith, the mouth goes uphill and the land goes downhill.
Trim:       the forward end is cut by a horizontal plane (in the installed
            attitude), giving one flat face. Printed on that face, every
            surface is within TILT deg of vertical, so nothing needs support.
"""
import math
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

# ---- parameters (mm) -------------------------------------------------
R_BORE   = 20.2      # bore radius, for a 40.0 neck
T_ROOT   = 5.0       # wall at bottom dead centre
T_TIP    = 3.0       # wall at the tips
LEN_MAX  = 42.0      # longest grip, at the land side. MUST be <= usable neck
LAND_Y   = -17.2     # flat land, matching the neck's moulded flat
PHI_CON  = 23.7      # constriction: gap here is 37.0 mm
CHAM_DEG = 7.0       # chamfer angular span
CHAM_R   = 2.5       # chamfer radial rise
TILT     = 27.0      # boresight tilt from zenith, per the 25-30 deg brief
PHI_TIP  = PHI_CON + CHAM_DEG
SWEEP    = 180.0 + 2 * PHI_TIP
N        = 240

phi_a, phi_b = PHI_TIP, PHI_TIP - SWEEP
tr = math.radians(TILT)
ROT = np.array([[1, 0, 0],
                [0, math.cos(tr), -math.sin(tr)],
                [0, math.sin(tr),  math.cos(tr)]])
FLIP = np.diag([1.0, -1.0, -1.0])


def profile(phi_deg):
    """Inner and outer radius of the 2D section at angle phi (deg from +X)."""
    p = math.radians(phi_deg)
    s = abs(phi_deg + 90.0)
    arm = abs(phi_a + 90.0)
    t = T_ROOT - (T_ROOT - T_TIP) * min(s / arm, 1.0)
    r_in = R_BORE
    sp = math.sin(p)
    if sp < -1e-9:                                # flat land at the bottom
        r_in = min(r_in, LAND_Y / sp)
    a = min(abs(phi_deg - phi_a), abs(phi_deg - phi_b))
    if a < CHAM_DEG:                              # lead-in chamfer at the tips
        r_in += CHAM_R * (1.0 - a / CHAM_DEG)
    return r_in, R_BORE + t


phis = np.linspace(phi_a, phi_b, N)
inner, outer = [], []
for f in phis:
    ri, ro = profile(f)
    c, s = math.cos(math.radians(f)), math.sin(math.radians(f))
    inner.append((ri * c, ri * s))
    outer.append((ro * c, ro * s))
inner, outer = np.array(inner), np.array(outer)

# trim plane: horizontal in the installed attitude, placed so the longest
# grip (at the most negative y, the land side) comes out at LEN_MAX
H = outer[:, 1].min() * math.sin(tr) + LEN_MAX * math.cos(tr)
zf = lambda y: (H - y * math.sin(tr)) / math.cos(tr)
zi, zo = zf(inner[:, 1]), zf(outer[:, 1])

gap = 2 * min(profile(f)[0] * math.cos(math.radians(f)) for f in phis if f > 0)
print(f"wrap {SWEEP:.1f} deg, tips {90-PHI_TIP:.1f} deg from top")
print(f"mouth {gap:.2f} mm, entry flare "
      f"{2*profile(phi_a)[0]*math.cos(math.radians(phi_a)):.2f} mm")
print(f"grip length {min(zi.min(), zo.min()):.1f} to {max(zi.max(), zo.max()):.1f} mm")

ring = np.vstack([outer, inner[::-1]])
area = 0.5 * abs(np.dot(ring[:, 0], np.roll(ring[:, 1], -1)) -
                 np.dot(ring[:, 1], np.roll(ring[:, 0], -1)))
mean_len = float(np.mean(np.concatenate([zi, zo])))
vol = area * mean_len / 1000.0
print(f"section {area:.0f} mm2, volume ~{vol:.1f} cm3, PLA ~{vol*1.24*0.92:.0f} g")


def q(a, b, c, d):
    return [a, b, c, d]


faces = []
for k in range(N - 1):
    ox, oy = outer[k]; ox2, oy2 = outer[k + 1]
    ix, iy = inner[k]; ix2, iy2 = inner[k + 1]
    faces.append(q((ox, oy, 0), (ox2, oy2, 0), (ox2, oy2, zo[k+1]), (ox, oy, zo[k])))
    faces.append(q((ix, iy, zi[k]), (ix2, iy2, zi[k+1]), (ix2, iy2, 0), (ix, iy, 0)))
    faces.append(q((ix, iy, zi[k]), (ox, oy, zo[k]),
                   (ox2, oy2, zo[k+1]), (ix2, iy2, zi[k+1])))          # trim face
    faces.append(q((ix2, iy2, 0), (ox2, oy2, 0), (ox, oy, 0), (ix, iy, 0)))
for k, fwd in ((0, True), (N - 1, False)):
    ix, iy = inner[k]; ox, oy = outer[k]
    f = q((ix, iy, 0), (ox, oy, 0), (ox, oy, zo[k]), (ix, iy, zi[k]))
    faces.append(f if fwd else f[::-1])
faces = [np.array(f) for f in faces]

installed = [f @ ROT.T for f in faces]
printo = [f @ FLIP.T for f in installed]
dz = min(f[:, 2].min() for f in printo)
printo = [f - np.array([0, 0, dz]) for f in printo]
print(f"print height {max(f[:,2].max() for f in printo):.1f} mm, "
      f"first-layer footprint {area/math.cos(tr):.0f} mm2")


# No STL output: the shipped parts both come from assembly.py. This script
# exists to document and render the clamp geometry. Note its walls (5.0/3.0)
# differ from the assembly's (6.0/3.5) because the trim direction flips which
# side of the clamp is the arm root -- see quad-clamp.md.

# ---- render ----------------------------------------------------------
BASE = np.array([0.560, 0.530, 0.900])
TEAL = np.array([0.114, 0.620, 0.459])
LIGHT = np.array([0.45, -0.55, 0.70]); LIGHT /= np.linalg.norm(LIGHT)


def shaded(polys, base=BASE):
    cols = []
    for f in polys:
        n = np.cross(f[1] - f[0], f[2] - f[0]); ln = np.linalg.norm(n)
        n = n / ln if ln > 1e-12 else np.array([0, 0, 1.0])
        sh = 0.46 + 0.54 * max(0.0, float(n @ LIGHT))
        cols.append(np.clip(base * sh + 0.16 * sh, 0, 1))
    return cols


def draw(ax, polys, el, az, title, ground=None, extra=None, pad=1.1):
    if ground is not None:
        ax.add_collection3d(Poly3DCollection(
            [np.array([(-46, -46, ground), (46, -46, ground),
                       (46, 46, ground), (-46, 46, ground)])],
            facecolors=[(0.88, 0.88, 0.90)], edgecolors="none", alpha=0.5))
    if extra:
        extra(ax)
    c = shaded(polys)
    ax.add_collection3d(Poly3DCollection(polys, facecolors=c, edgecolors=c,
                                         linewidths=0.3))
    ap = np.vstack(polys); ctr = (ap.max(0) + ap.min(0)) / 2
    sp = (ap.max(0) - ap.min(0)).max() / 2 * pad
    ax.set_xlim(ctr[0]-sp, ctr[0]+sp); ax.set_ylim(ctr[1]-sp, ctr[1]+sp)
    ax.set_zlim(ctr[2]-sp, ctr[2]+sp)
    ax.set_box_aspect((1, 1, 1)); ax.view_init(elev=el, azim=az)
    ax.set_axis_off(); ax.set_title(title, fontsize=12, color="#333333", pad=-2)


fig = plt.figure(figsize=(15, 9.6), dpi=110)
for i, (t, el, az) in enumerate([
        ("isometric", 26, -55), ("looking down bore", 88, -90),
        ("side on, trimmed end up", 8, 0), ("from behind", 28, 125),
        ("mouth from above", 50, -90), ("trimmed face", -40, -55)], 1):
    draw(fig.add_subplot(2, 3, i, projection="3d"), faces, el, az, t)
fig.suptitle(f"LNBF C-clip  ·  Ø40.4 bore · 37 mm mouth · 241° wrap · "
             f"grip {min(zi.min(), zo.min()):.0f}–{LEN_MAX:.0f} mm, flat-trimmed",
             fontsize=14, color="#222222", y=0.965)
fig.tight_layout(rect=[0, 0, 1, 0.94])
fig.savefig("lnbf-clamp-views.png", facecolor="white")
print("wrote lnbf-clamp-views.png")

bs = np.array([0.0, -math.sin(tr), math.cos(tr)])
gi = min(f[:, 2].min() for f in installed)


def rays(ax):
    ax.plot([0, 0], [0, 0], [gi, 76], ls=(0, (4, 4)), color="#888780", lw=1.1)
    ax.plot(*[[0, 76 * bs[k]] for k in range(3)], color="#D85A30", lw=1.8)
    ax.plot(*zip(*[(0, -50*math.sin(a), 50*math.cos(a))
                   for a in np.linspace(0, tr, 24)]), color="#888780", lw=1.0)
    ax.text(0, -16, 56, f"{TILT:.0f}°", color="#333333", fontsize=14)


fig2 = plt.figure(figsize=(14.5, 4.6), dpi=110)
draw(fig2.add_subplot(1, 4, 1, projection="3d"), installed, 4, 0,
     "installed, along the tilt plane", ground=gi, extra=rays, pad=1.4)
draw(fig2.add_subplot(1, 4, 2, projection="3d"), installed, 22, -58,
     "installed, isometric", ground=gi, extra=rays, pad=1.4)
draw(fig2.add_subplot(1, 4, 3, projection="3d"), installed, 63, -90,
     "down the boresight", ground=gi, pad=1.25)
draw(fig2.add_subplot(1, 4, 4, projection="3d"), printo, 12, -60,
     "print orientation, flat face on bed", ground=0.0, pad=1.25)
fig2.suptitle("Bore axis 27° from zenith · forward end trimmed flat · "
              "prints on that face, no supports",
              fontsize=13, color="#222222", y=0.97)
fig2.tight_layout(rect=[0, 0, 1, 0.90])
fig2.savefig("lnbf-clamp-tilt.png", facecolor="white")
print("wrote lnbf-clamp-tilt.png")
