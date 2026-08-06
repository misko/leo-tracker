# Quad LNBF mount — four C-clips on a flush cross, one printed part

Four of the [C-clips](lnbf-clamp.md) at 90°, each tilted 27° outward with its
mouth facing outward, unioned by a cross web whose top face is coplanar with all
four trimmed clamp faces. The whole assembly has one flat plane; printed on it,
nothing needs support.

![six views of the assembly](quad-clamp-views.png)

## Numbers

| Feature | Value |
| --- | --- |
| Clamps | 4 × C-clip, Ø40.4 bore, 37 mm mouth, 241° wrap |
| Tilt | 27° from zenith, boresight 63° elevation |
| Arm radius | 75 mm, centre to bore axis in the flush plane |
| Bore-axis spacing | 106 mm adjacent, 150 mm opposite |
| Cross web | 24 mm wide × 12 mm thick |
| Grip length | 22.5 mm at the arm root to 42 mm at the mouth side |
| Envelope | 177 × 177 × 37 mm |
| Print | 37.4 mm tall, 177 × 177 bed, ~105–130 g, 8–12 h |

`quad-clamp-print.stl` is oriented on its flush face — slice as-is.
`quad-clamp-installed.stl` is the same solid at its service attitude.
`assembly.py` builds both from the constants at the top.

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
  footprint — no brim needed here, unlike the single clamp.
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
- Adjacent bore axes are 106 mm apart, at the low end of the 10–15 cm the brief
  asked for. Apertures diverge above the clamps, so the real spacing is wider —
  raise `R_ARM` if you want more, at the cost of bed size and web deflection.
