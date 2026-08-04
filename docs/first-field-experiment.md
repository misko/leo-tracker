# First field experiment: predicted pass to measured Doppler

## Purpose

Demonstrate that a blindly extracted RF frequency track follows the geometric
Doppler shape of a predicted LEO pass. This experiment does **not** claim a
position solution.

## Prerequisites

- Surveyed receiver latitude, longitude, altitude, and uncertainty.
- UTC-disciplined host clock with its source and health recorded.
- Fresh TLE/Supplemental GP artifact with retrieval time and SHA-256.
- Documented antenna, LNB/downconverter, bias tee, filtering, attenuation,
  cables, polarization, and nominal LNB LO.
- Pluto+ serial and firmware identity. The SPF direct-USB protocol-v2 RAM image
  may be reused after its normal smoke test; standard USB-IIO is acceptable for
  the first capture.
- Verified Pluto input power is within safe limits.

## Pass selection

Choose at least three passes through the clearest azimuth sector. Prefer peak
elevation above 45 degrees, low TLE age, and no receiver retune during the core
pass. Generate both 10-degree and 25-degree visibility windows.

## Capture protocol

1. Photograph and describe the RF setup and visible obstructions.
2. Run a preflight capture and check noise floor, clipping, dropped samples,
   clock health, disk space, and hardware identity.
3. Start at least five minutes before predicted rise.
4. Keep center frequency, sample rate, bandwidth, gain mode, and antenna fixed.
5. Continue at least five minutes after predicted set.
6. Finalize the immutable IQ artifact and verify its checksum by replay.
7. Record weather, unexpected interference, resets, or setup changes.

## Required controls

- An equal-duration off-pass recording.
- The same observation scored against a time-shifted prediction.
- At least one plausible wrong satellite scored over the same interval.
- Blind ridge extraction performed before using the TLE curve as a constraint.

## Analysis and acceptance

Fit only a constant receiver/LNB frequency offset and linear frequency drift.
Plot the predicted and observed tracks, residuals, SNR, uncertainty, elevation,
and quality flags. Report failures and null captures.

Pass the milestone only after at least three captures meet all of these:

- a continuous, quality-qualified ridge overlaps the predicted pass interval;
- the correct prediction has materially lower held-out RMS than every control;
- results reproduce from raw IQ plus the committed configuration and TLE;
- timing, ephemeris, radio, firmware, and RF-chain provenance are complete.

Phase observations and receiver-position fitting remain gated until this
milestone passes.
