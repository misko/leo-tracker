# Koolertron 10 MHz Reference Setup

> **Status: not the deployed configuration.** No shared 10 MHz reference is
> in the signal chain as of 2026-08-12; every LNB free-runs on its own
> crystal. This is a build guide for a setup that has not been adopted, kept
> for when a disciplined reference is fitted. Nothing here describes how the
> current measurements were taken — see `lnb-swap-20260812.md` and
> `epochs.json` for that.
>
> The "both LNBs share much of this error" claim under *Precision limitation*
> is contradicted by measurement on the free-running hardware: lnb-c and
> lnb-d sat 410.7 kHz apart. Curiously lnb-a and lnb-b sat only 9.5 ± 9.0 kHz
> apart, which is closer than two independent crystals ought to land and is
> not yet explained.

This guide configures the Koolertron 15 MHz dual-channel DDS signal
generator as the shared 10 MHz reference for two Orbital Research 5400X
LNBs.

The product listing most likely corresponds to the `GH-CJDS66-A` or
`JDS6600-15M` family. Koolertron sold visually similar 200 MSa/s and
266 MSa/s revisions, so confirm the exact model on the rear or bottom label.
The controls relevant to this setup are substantially the same.

Reference manual:
[JDS6600 operating manual](https://ktek.jp/dds-jds6600/jds6600_quick_guide.pdf)

## Required settings

| Setting | Value |
| --- | --- |
| Output channel | CH1 |
| Waveform | Sine |
| Frequency | `10.000000 MHz` |
| Amplitude | `1.000 Vpp` |
| DC offset | `0.000 V` |
| Phase | `0.0 degrees` |
| Duty | `50.0%` (ignored for a sine wave) |
| Sweep | Off |
| Burst | Off |
| Modulation | Off |
| CH2 output | Off |

Do not use the generator's TTL output. Use the analog CH1 BNC output and
50-ohm coax.

## Front-panel configuration

1. Disconnect the generator output and turn on the generator.
2. Return to the normal waveform screen, rather than Measure or Modulation.
3. Select CH1.
4. Press the waveform control, normally labeled `WAVE`, and select `Sine`.
5. Press `FREQ`. Use the cursor control and knob to enter `10.000000 MHz`.
   A long press on `FREQ` changes the frequency unit on this instrument
   family.
6. Press `AMPL` and enter `1.000 Vpp`.
7. Press `OFFS` and enter `0.000 V`. Holding `OFFS` generally resets the
   offset to zero.
8. Leave `DUTY` at its default `50.0%`. Duty cycle does not affect a sine
   wave; it is relevant only to square and pulse waveforms.
9. Confirm that sweep, burst, and modulation are all disabled.
10. Confirm that CH2 is off.
11. Leave CH1 off until the reference network is connected.

## Reference wiring

Use one generator output and the ZSC-3-1+ so both LNBs receive the same
reference waveform:

```text
Koolertron CH1
      |
      v
ZSC-3-1+ port S
  +---+---+
  |   |   |
  1   2   3
  |   |   +-- 50-ohm BNC terminator
  |   +------ Z4BT B REF
  +---------- Z4BT A REF
```

The remaining connections are:

```text
13 V ---------------- Z4BT A DC
5400X A IF connector - Z4BT A COM
Pluto RX0 ------------ Z4BT A RF

13 V ---------------- Z4BT B DC
5400X B IF connector - Z4BT B COM
Pluto RX1 ------------ Z4BT B RF
```

Never connect a Z4BT COM port directly to the Pluto because COM carries DC,
10 MHz reference, and L-band IF. The Pluto connects only to the Z4BT RF port.

## Reference-level budget

The 5400X requires a 10 MHz input between approximately -8 and 0 dBm. The
reference path has approximately:

- 4.8 dB ideal three-way division loss in the ZSC-3-1+
- about 0.4 dB additional ZSC insertion loss around 10 MHz
- about 0.5 dB Z4BT reference-path insertion loss
- a small amount of cable loss

A `1.000 Vpp` generator setting is deliberately tolerant of the generator's
load-display convention. Depending on whether the displayed voltage describes
the open-circuit or 50-ohm loaded amplitude, the generator supplies roughly
-2 to +4 dBm. Each LNB should therefore receive roughly -8 to -2 dBm.

## Verification with an oscilloscope

If an oscilloscope is available, measure CH1 using a real 50-ohm input or a
50-ohm feed-through terminator. Confirm:

- frequency is 10 MHz
- waveform is sinusoidal
- DC offset is approximately zero
- amplitude is approximately 1.0 Vpp

A normal 1-megohm oscilloscope input without a 50-ohm terminator may show
approximately twice the voltage that the matched reference network receives.

## Power sequence

Start the system in this order:

```text
Connect reference network
Enable Koolertron CH1 output
Enable 13 V LNB power
Start Pluto capture
```

Stop it in reverse order:

```text
Stop Pluto capture
Disable 13 V LNB power
Disable Koolertron CH1 output
```

## Precision limitation

The generator family specifies approximately +/-20 ppm absolute frequency
accuracy and +/-1 ppm stability over three hours. A 20 ppm error at 10 MHz is
200 Hz; multiplication to the LNB's approximately 10 GHz local oscillator can
produce about 200 kHz of absolute LO error. Both LNBs will share much of this
error, which is useful for initial dual-receiver Doppler experiments, but this
generator is not the final reference for precise positioning or phase work.

Replace it with a low-phase-noise 10 MHz GPSDO or disciplined OCXO when moving
from acquisition experiments to precision Doppler and PNT measurements.

## Component references

- [Orbital Research 5400X datasheet](https://orbitalresearch.net/wp-content/uploads/2019/12/Orbital-5400X-Enhanced-External-Reference-Ku-Band-LNB-web-1912.pdf)
- [Mini-Circuits Z4BT-2R15GW+ datasheet](https://www.minicircuits.com/pdfs/Z4BT-2R15GW%2B.pdf)
- [Mini-Circuits ZSC-3-1+ datasheet](https://www.minicircuits.com/pdfs/ZSC-3-1.pdf)
