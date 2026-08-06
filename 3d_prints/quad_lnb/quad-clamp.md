# Quad LNBF mount — four C-clips on a flush cross, one printed part

Four of the [C-clips](lnbf-clamp.md) at 90°, each tilted 27° outward with its
mouth facing outward, unioned by a cross web whose top face is coplanar with all
four trimmed clamp faces. The centre drops into a hollow pedestal that reaches
below the LNBF tails and flares into a foot, so the whole thing stands on itself
with the LNBFs off the ground. One flat plane on top; printed on it, nothing
needs support.

![six views of the assembly](quad-clamp-views.png)

## Numbers

| Feature | Value |
| --- | --- |
| Clamps | 4 × C-clip, Ø40.4 bore, 37 mm mouth, 241° wrap |
| Tilt | 27° from zenith, boresight 63° elevation |
| Arm radius | 105 mm, centre to bore axis in the flush plane |
| Bore-axis spacing | 148 mm adjacent, 210 mm opposite |
| Cross web | 24 mm wide × 12 mm thick |
| Pedestal | Ø50 × 5 mm wall, 180 mm tall, flaring to a Ø160 foot |
| Grip length | 22.5 mm at the arm root to 42 mm at the mouth side |
| Envelope | 237 × 237 × 180 mm (197 mm bed rotated 45°) |
| Ground clearance | 90 mm under the LNBF tails |
| Tip angle | 27.6° |
| Print | 180 mm tall, ~180–240 g, 18–24 h |

`assembly.py` writes both printable files: `quad-clamp-print.stl`, oriented on
its flush face, and `clamp-fit-test.stl`, a 12 mm slice of one clamp for
checking the fit before committing to the long print. Both come from the same
`profile()` and the same constants, so the coupon cannot drift from the part.

## Why the mouths face outward

They have to, given a cross. The mouth is a 119° gap; a web arm arriving from
the centre would land in that gap and touch nothing. With the mouths outward the
arm meets the **closed back** of each C — the solid, thickest part of the ring,
6 mm of wall — which is both the only place worth bonding to and the natural
place for the load to enter. It also means each LNBF is seated from outside the
array with nothing in the way.

This flips the single-clamp convention, where the mouth went uphill so gravity
would push the neck away from the opening. That was worth 0.8 N against ~45 N of
ejection resistance and does not survive contact with a real constraint.

## The cradle

Each web arm's outer end is trimmed to the clamp's **actual outer surface**,
found by ray-marching the solid rather than by eye. The contact radius runs from
about 40.4 mm at the centre of the arm out to 47.3 mm at its edges and top, so
the end face is a genuine concave saddle that wraps the clamp instead of butting
a flat face against a curve. Two consequences:

- The arm never crosses into the bore. It stops at the outer surface and then
  buries **0.8 mm** into a 6 mm wall, which is enough for the slicer to fuse the
  two solids and nowhere near enough to break through.
- The interface carries load in bearing across a curved patch rather than on a
  line contact, which is what you want at the one joint that sees the LNBF's
  full cantilever.

## The arm length is set by the LNBFs, not the clamps

The boresights tilt outward, so each LNBF body leans **inward** as it goes down
and the four tails converge on the centre. That convergence — not clamp
clearance — is what sizes the cross.

An LNBF tail sits 58 mm below its clamp's aft face and 30 mm further inboard.
With 75 mm arms that puts the tails at r = 29 mm, where **adjacent bodies
overlap each other by 9 mm**: the mount could not have been assembled. At 105 mm
they land at r = 59 mm:

| Clearance | Value |
| --- | --- |
| LNBF to adjacent LNBF | +34 mm |
| LNBF to the Ø50 pedestal | +9 mm |

The pedestal is now the binding constraint, not the neighbouring LNBFs. Both
numbers come straight from `BODY_D` and `BODY_L` at the top of `assembly.py`,
which are **assumptions about your LNBF** — measure them and re-run before
printing, because they move `R_ARM` directly.

## Standing on the pedestal

The centre drops 180 mm to a hollow Ø50 column with a Ø160 flared foot,
reaching 90 mm below the lowest LNBF. Three details make it work:

- **The top face stays flat all the way across.** The cross does not slope down
  to the centre — it keeps a flush top and gets *deeper*. Sloping arms would put
  their upper surfaces in overhang once the part is flipped for printing; a flat
  top with a deeper centre prints as a slab with a column rising out of it.
- **The column is hollow and open at the bottom**, so it drains, saves ~90 cm³
  of material, and its bore prints as a plain vertical hole with the web slab as
  its floor.
- **The foot flare sits entirely below the LNBF tails**, at 38.7° from
  vertical. Below the tails there is nothing to hit, so the foot can spread;
  above them it could not.

**Height and width fight each other, so size them together.** Ground clearance
raises the centre of gravity one-for-one, while foot diameter only helps as a
ratio, so going taller costs stability faster than going wider buys it back:

| Clearance | Foot | Flare | Tip angle |
| --- | --- | --- | --- |
| 25 mm | Ø90 | 38.7° | 27.1° |
| 90 mm | Ø90 | 24.0° | 16.4° |
| 90 mm | Ø130 | 41.6° | 23.0° |
| **90 mm** | **Ø160** | **42.5°** | **27.6°** |

Ø160 is what holds the original stability at the taller height. `assembly.py`
reports the tip angle every run using the LNBFs as the CG, which is conservative
— the mount's own mass sits lower.

`FLARE_H` is deliberately separate from `FOOT_CLR`. If the flare had to span the
whole 90 mm drop the cone would be enormous; confining it to the bottom 60 mm
keeps the column straight for the rest and holds the flare at 42.5°, inside the
45° overhang limit. The cap is **`FOOT_D ≤ PED_D + 2 × FLARE_H`**, with
`FLARE_H ≤ FOOT_CLR` so the cone stays below the LNBF tails.

## The wall went back to 6 mm

Worth tracking, because it reverses the change made when the single clamp was
lengthened. The trim removes a wedge from whichever side of the clamp sits
highest — and flipping the mouth outward flips which side that is:

| | single clamp, mouth uphill | in the cross, mouth outward |
| --- | --- | --- |
| Long side (42 mm) | land / arm root | mouth / arm tips |
| Short side | mouth tips, 23 mm | **arm root, 22.5 mm** |

Arm stiffness is set at the root. In the single clamp the root was the *long*
side, so the wall had to come down to 5 mm to keep insertion force near 50 N.
Here the root is the *short* side, so the 6 mm wall goes back in and insertion
lands at ~45 N. Same target force, opposite adjustment — driven entirely by
which way the mouth points.

## Printing

Slice `quad-clamp-print.stl` as it comes. **Do not re-orient it.**

- Every surface is within 27° of vertical: the bores are near-vertical channels
  with no ceiling to bridge, the outer walls overhang at most 27°, and the aft
  ends are upward-facing 27° slopes.
- The web becomes a solid 12 mm slab in the first layers, giving a large flat
  footprint — no brim needed here, unlike the single clamp. The pedestal then
  rises as a 100 mm column; check it is not the tallest thin feature your
  printer struggles with before starting a 16-hour job.
- 0.2 mm layers, 5 perimeters, 40% infill. The clamp walls are almost all
  perimeter; the web is where the infill goes.
- PLA is fine for a first article. Outdoors use ASA — PLA softens near 55 °C and
  a part in direct sun gets there.

**The STL is a union of overlapping solids**, not a single manifold shell —
four clamps plus four web arms that interpenetrate at the joins and at the
centre. Every slicer handles this correctly. If you want a watertight manifold
for CAD work, run it through a mesh boolean first.

## Before printing 8–12 hours of it

- **Measure the usable neck** between the feedhorn flange and the LNBF body.
  `LEN_MAX = 42` is an assumption; the clamp is captured axially between those
  two features, so if the neck is shorter the clamp will not fit at all.
- **Print one single clamp first** ([lnbf-clamp.md](lnbf-clamp.md)) and check
  insertion force and that the mouth springs fully back. The mouth width is the
  dimension to tune, and tuning it on a 16 g part beats tuning it on a 120 g one.
- **Measure `BODY_D` and `BODY_L`.** They set the LNBF convergence, which sets
  `R_ARM`, which sets everything else. `assembly.py` prints both clearances on
  every run and warns below 5 mm.
- Adjacent bore axes are 148 mm apart, comfortably inside the 10–15 cm the brief
  asked for, and apertures diverge further above the clamps.
