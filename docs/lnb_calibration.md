# LNB local-oscillator calibration

Two LNBs feeding one Pluto do not share a frequency reference. Each has its own
PLL, and different models land on different actual local oscillators even when
both are labelled 9750 MHz. The two receivers on a Pluto share a single tuner,
so one tuning cannot centre both, and a beacon that sits outside the
acquisition search on one port can never be confirmed by the pair.

This is not a hypothetical. On this installation R2A1 and R2A2 disagree by
**435 kHz**, which put R2A1 outside the ±300 kHz coarse search for most of a
pass and left that radio identifying **zero** satellites while its sibling
identified dozens.

## What is measurable, and what is not

Each receiver reports the frequency at which the edge pilot correlates best:

```
observed = Doppler + LNB_LO_error + Pluto_LO_error + satellite_error - tuned_IF
```

Subtracting the two receivers of one radio cancels everything except the term
of interest:

| term | cancels? | why |
|---|---|---|
| Doppler | yes | both ports observe the same satellite from the same place |
| Pluto LO error | yes | the two receivers share one tuner |
| satellite transmit error | yes | one transmitter |
| **LNB LO difference** | **no** | independent references, this is the measurement |

So the **difference between receivers is a clean, reference-free measurement of
the LNB mismatch**. No test source, no GPS reference, no known-frequency
injection is required: the satellite is the reference, and the subtraction
removes everything it cannot pin down.

The corollary matters as much. **Absolute LO error is not measurable this way.**
A drift common to both LNBs, or drift of the Pluto TCXO, cancels in exactly the
same subtraction and is indistinguishable from Doppler. Recovering absolute
error needs TLE-predicted Doppler as an external reference, which exists only
for tracks that already qualified. For centring the acquisition search, the
differential is the quantity that matters and absolute error is irrelevant.

Per-port medians taken from each port's own detections are **not** a substitute.
A port whose offset exceeds the search range only detects when Doppler happens
to pull it inside, so its observed median is biased toward the search boundary.
R2A1 reads +349 kHz that way against a true +436 kHz, with its p10 piled against
the +300 kHz edge. Always calibrate from the paired difference.

## Why a mismatch costs coverage

Both receivers see the same Doppler at any instant, and Ku-band LEO spans about
±250 kHz. A receiver can only acquire while its own observed offset lies inside
the ±300 kHz coarse search (`pilots.py`, `acquisition.py`). Dual-RX
confirmation needs both inside simultaneously, so the usable Doppler window is
the intersection.

With a 435 kHz mismatch:

| strategy | offset applied | dual-RX Doppler coverage |
|---|---|---|
| no correction | none | 115 kHz, **23%** |
| shift the common tuning by half the mismatch | −218 / +218 kHz | 165 kHz, **33%** |
| **per-receiver search centring** | each port searched about its own offset | 500 kHz, **100%** |

Shifting the common tuning only makes the loss symmetric. The intersection
width is set by `2 x search - mismatch` no matter where it is placed, so it
cannot exceed 165 kHz while the mismatch stands. **Centring each receiver's own
search is the fix**; the receivers are acquired independently already, so each
can be given its own search centre without touching the tuner.

## Protocol

Run weekly, and **whenever an LNB is swapped, moved, or replaced with a
different model** — a swap invalidates the stored value immediately, while
ordinary ageing does not.

1. Collect recent narrow reports for the radio, at least a few hundred probes
   with `candidate` true on both receivers.
2. For each dual candidate take the signed difference of
   `receivers[i].acquisition.exact_match.frequency_offset_hz` between receiver 0
   and receiver 1.
3. Take the median. Record p10 and p90 as well; a spread much beyond 10 kHz
   means something other than a static LO difference is moving.
4. Store per radio, against the receiver labels in effect. The labels encode
   physical position (`lnb-a` is R1A1, `lnb-c` is R2A1), so a stored value is
   only valid while nothing has been unplugged.
5. Apply as a per-receiver acquisition search centre.

## Cadence, and why hourly is wrong

Measured over 13 hours on this installation:

| radio | median mismatch | p10 | p90 | spread |
|---|---|---|---|---|
| pluto-19f2 (R2A1 − R2A2) | +435,329 Hz | 431,685 | 439,798 | 8.1 kHz |
| pluto-5d4d (R1A1 − R1A2) | +5,857 Hz | 261 | 8,777 | 8.5 kHz |

The hourly medians wander around a stable centre rather than trending, almost
certainly diurnal thermal behaviour, so the error does not accumulate. Against
a 300 kHz search half-width the ~8 kHz wander is roughly a tenth of the
available margin, so correcting it hourly would chase noise.

**Weekly is ample. What justifies an out-of-cycle recalibration is an event, not
elapsed time:**

- an LNB swapped, moved between ports, or replaced with a different model
- a change of band (a universal LNB's 10600 MHz high-band reference is a
  different oscillator with its own error)
- an alert, below

## Monitoring

Recalibrating on a timer will not catch a cable swapped between ports, which
changes the mismatch instantly. Compare each measurement against the stored
value and raise an alert when it moves by more than about **20 kHz** — twice the
observed thermal wander, so ordinary behaviour is silent while a swapped
connector or a failing PLL is caught the same day.

A sign change in the mismatch means the ports have been exchanged. Re-label
rather than re-calibrate, or the position names stop describing the hardware.

## Current values

Measured 2026-08-11 from 460 and 3,722 dual candidates respectively:

| radio | ports | mismatch | note |
|---|---|---|---|
| pluto-19f2 | R2A1 − R2A2 | **+435,329 Hz** | R2A1 reads high; outside the search untreated |
| pluto-5d4d | R1A1 − R1A2 | **+5,857 Hz** | inside every gate, no action needed |

R1A1 and R1A2 both sit near −190 kHz in absolute terms. That is common-mode and
is the shared Pluto TCXO, not the LNBs; their genuine disagreement is the
5.9 kHz differential. Reading the absolute figure as an LNB fault would send
you after the wrong component.
