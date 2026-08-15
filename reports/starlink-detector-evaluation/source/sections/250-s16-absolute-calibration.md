## 16. The −150 kHz, measured: four oscillators, and what it costs

[Section 12](#12-what-the-cliff-actually-is) argued that the −150 kHz window
centre is each LNB's own local-oscillator error rather than one shared tuning
mistake, and [section 14](#14-the-dead-port-and-the-stale-calibration-are-one-fault)
measured one of those errors the hard way, on `lnb-a`. Neither produced what the
pipeline actually needs: a number per receiver. This section measures all eight
— four ports either side of the LNB swap — checks them out of sample, and prices
what leaving them unmeasured is costing right now.

### 16a. Four independent constants, not one shared error

Two populations can measure an absolute centre, and they are independent of each
other. The **live narrow reports** come from a ±350 kHz search that *is* centred
on each receiver, and are read with the repository's own `_paired_differences`.
The **survey re-scoring** of the corpus uses a fixed ±700 kHz bank about raw zero
and ignores `receiver_centers` entirely. Where the sync corpus also carries a
port it is a third. The bracket below is the spread across those populations,
which is far wider than any of their statistical intervals and is therefore the
honest uncertainty.

| Receiver | Epoch | Absolute centre | Spread across populations | n | ppm at 9.75 GHz |
|---|---|---:|---|---:|---:|
| `lnb-a` (5d4d rx0) | gen1 | **{{s16a_lnba_gen1_centre}}** | {{s16a_lnba_gen1_spread}} | {{s16a_lnba_gen1_n}} | {{s16a_lnba_gen1_ppm}} |
| `lnb-a` | gen2 | **{{s16a_lnba_gen2_centre}}** | *single population* | {{s16a_lnba_gen2_n}} | {{s16a_lnba_gen2_ppm}} |
| `lnb-b` (5d4d rx1) | gen1 | **{{s16a_lnbb_gen1_centre}}** | {{s16a_lnbb_gen1_spread}} | {{s16a_lnbb_gen1_n}} | {{s16a_lnbb_gen1_ppm}} |
| `lnb-b` | gen2 | **{{s16a_lnbb_gen2_centre}}** | {{s16a_lnbb_gen2_spread}} | {{s16a_lnbb_gen2_n}} | {{s16a_lnbb_gen2_ppm}} |
| `lnb-c` (19f2 rx0) | gen1 | **{{s16a_lnbc_gen1_centre}}** | {{s16a_lnbc_gen1_spread}} | {{s16a_lnbc_gen1_n}} | {{s16a_lnbc_gen1_ppm}} |
| `lnb-c` | gen2 | **{{s16a_lnbc_gen2_centre}}** | {{s16a_lnbc_gen2_spread}} | {{s16a_lnbc_gen2_n}} | {{s16a_lnbc_gen2_ppm}} |
| `lnb-d` (19f2 rx1) | gen1 | **{{s16a_lnbd_gen1_centre}}** | {{s16a_lnbd_gen1_spread}} | {{s16a_lnbd_gen1_n}} | {{s16a_lnbd_gen1_ppm}} |
| `lnb-d` | gen2 | **{{s16a_lnbd_gen2_centre}}** | {{s16a_lnbd_gen2_spread}} | {{s16a_lnbd_gen2_n}} | {{s16a_lnbd_gen2_ppm}} |

**Read the `n` column carefully.** It counts every observation examined for that
port-epoch, not every observation that entered the consensus. `lnb-a` gen2 is the
case where those differ and it matters: of its {{s16a_lnba_gen2_n}} observations
only the sync corpus arm is usable, because the live search is blind there and
the survey arm fires at 5.0%. That row rests on **one** population, so treat it
as ±40 kHz. Every other row draws on {{s16a_lnbb_gen1_npop}} or more.

**{{s16a_lnbb_gen2_ppm}} to {{s16a_lnbc_gen2_ppm}} ppm.** That is inside an
ordinary consumer LNB specification. None of this is a fault; it is four
oscillators behaving normally and a pipeline that never asked them where they
were.

The swap acts as its own control. `lnb-b`, on the radio nobody touched, moves
**{{s16a_lnbb_move}}** across the boundary. `lnb-a`, on that same untouched
radio, moves **{{s16a_lnba_move}}** — the fault of
[section 14](#14-the-dead-port-and-the-stale-calibration-are-one-fault), now
visible on an absolute axis. And of the two LNBs that were physically replaced,
`lnb-d` moves **{{s16a_lnbd_move}}** while `lnb-c` moves only
**{{s16a_lnbc_move}}**: a replacement part is a new draw from the same
distribution, not necessarily a different one.

![Absolute per-receiver centres, and the corrected sky window](figures/absolute-centres.png)

### 16b. The correction survives being tested out of sample

A centre fitted to a population and then measured on that same population proves
nothing. Three tests of increasing strength:

| Test | Centroid of the sky window | Fraction negative | n |
|---|---:|---:|---:|
| The axis in use today | **{{s16b_before_centroid}} kHz** | {{s16b_before_negative}} | {{s16b_before_n}} |
| Half-sample — centres from odd sweeps, measured on even | −4.8 … +9.9 kHz per port | — | — |
| **Out of sample** — centres from the *live* reports, applied to the *survey* re-scoring | **{{s16b_after_centroid}} kHz** | {{s16b_after_negative}} | {{s16b_after_n}} |

The third row shares no detector, no arm and no fitted quantity with what it is
scored on. The window moves onto zero, and the one-sided distribution that
[section 12](#12-what-the-cliff-actually-is) found becomes symmetric. Per port
the out-of-sample residuals span −33.7 to +17.0 kHz. The single exception is
`lnb-a` gen2 at −92.5 kHz, whose corpus detections are censored at the bank edge.

That one is not waiting on a tool, and earlier drafts of this report said it was
— they prescribed an `lo_sweep`, which exists nowhere in this repository. What
`lnb-a` gen2 is waiting on is **data**: the method used here already measures an
absolute centre, and it fails on that port only because its single usable
population is too thin. More post-swap survey sweeps make its corpus arm usable,
and the measurement then follows from the pipeline in
[`figures/abscal-pipeline-abscal.py`](figures/abscal-pipeline-abscal.py) with
nothing new written.

**But the differential and the absolute centre are not interchangeable, and this
report should not pretend they are.** Differencing two absolute centres does not
reproduce the measured `rx0 − rx1`. On a *common* population — both receivers
scored on the same dual-candidate checks — the differential is reproduced to
1 kHz. The gap appears only when marginal means from *different* detection sets
are subtracted, because a differential is exact at an instant while a port's
absolute centre is a mean over the sky that port happens to detect, and two ports
do not detect the same sky.

**The consequence is a floor of roughly 20–40 kHz on any absolute per-receiver
number, which more data does not remove.** Search-window truncation was ruled out
as the cause: inverting the truncated mean against an empirical detected-Doppler
density leaves every uncensored estimate unchanged.

### 16c. Miscentring costs detections, and this is measured, not modelled

Twice, one port's applied search centre moved while its partner's did not. The
partner is a time control, so the ratio of the two ports' candidate rates
isolates the effect of miscentring from everything else that changed.

| Contrast | Miscentring | Control | Detections lost |
|---|---:|---|---:|
| `lnb-c` gen2, one applied centre to another | {{s16c_lnbc_gen2_miscentring}} | `{{s16c_lnbc_gen2_control}}` | **{{s16c_lnbc_gen2_lost}}** |
| `lnb-c` gen1, corrected against uncorrected | {{s16c_lnbc_gen1_miscentring}} | `{{s16c_lnbc_gen1_control}}` | **{{s16c_lnbc_gen1_lost}}** |
| `lnb-a` across its LO move | {{s16c_lnba_epochmove_miscentring}} | `{{s16c_lnba_epochmove_control}}` | **{{s16c_lnba_epochmove_lost}}** |

A hard ±350 kHz window would predict near-zero loss at 150 kHz, since the
detected-Doppler density barely reaches ±200 kHz. It does not: the loss is a soft
roll-off of the timing stage and the subband filter, which is precisely why it
had to be measured rather than reasoned about.

**Against the calibration in force today, all four ports are miscentred.** The
live artifact — created {{s16d_artifact_utc}} from
{{s16d_reports_examined}} reports, and snapshotted into this report's figures
directory because a timer rewrites it — resolves through `receiver_centers()` to:

| Port | Applied today | Measured | Miscentred by |
|---|---:|---:|---:|
| `lnb-c` | {{s16d_lnbc_applied}} | {{s16a_lnbc_gen2_centre}} | **{{s16d_lnbc_miscentred}}** |
| `lnb-b` | {{s16d_lnbb_applied}} | {{s16a_lnbb_gen2_centre}} | **{{s16d_lnbb_miscentred}}** |
| `lnb-d` | {{s16d_lnbd_applied}} | {{s16a_lnbd_gen2_centre}} | **{{s16d_lnbd_miscentred}}** |
| `lnb-a` | {{s16d_lnba_applied}} | {{s16a_lnba_gen2_centre}} | **{{s16d_lnba_miscentred}}** |

Two of these are **directly measured** and two are **not**, and the difference
matters:

| Port | Basis |
|---|---|
| `lnb-c` | **measured** — its miscentring sits on top of the {{s16c_lnbc_gen2_miscentring}} contrast above, loss ≈ {{s16c_lnbc_gen2_lost}} |
| `lnb-a` | **measured** — sits on the {{s16c_lnba_epochmove_miscentring}} contrast, loss ≈ {{s16c_lnba_epochmove_lost}} |
| `lnb-b` | *estimated* from the measured miscentring response; no contrast exists at its offset |
| `lnb-d` | *estimated* likewise |

`lnb-b` and `lnb-d` fall between the first contrast and zero, so their loss is
read off a response curve rather than off a control. Calling all four "roughly a
quarter" would present an interpolation as a measurement, which is the habit
this report has been correcting throughout.

The survey and corpus paths do not lose detections to this, because their coarse
bank is fixed about raw zero and is never steered by `receiver_centers`. That
detection cost lands entirely on the live dwell path and everything downstream
of it.

### 16c-bis. One radio's agreement check is switched off, and the corpus records it

`survey_scoring` does read `receiver_centers`. Not to place its search — but to
form `bias = centers[0] − centers[1]` for `cross_receiver_checks`, which marks a
pair as agreeing only when `|cfo_difference − bias|` is within
`CROSS_RECEIVER_CFO_HZ`, a {{s16e_gate}} Hz gate. That check needs only the
*difference*, which is the one thing a differential calibration genuinely
establishes, and its docstring says so. The design is sound.

What defeats it is that the artifact carries **one differential per radio and no
epoch**, while the differential moved by hundreds of kilohertz when the LNBs were
swapped. Reading the bias actually applied out of {{s16e_sampled}} scored
sidecars — not out of any calibration file, because the scoring host's artifact
turns out not to be the one on the share:

| Radio | Bias applied | Measured differential | Residual | Checks | Agree | Rate |
|---|---:|---:|---:|---:|---:|---:|
| `pluto-19f2` | {{s16e_19f2_applied}} | {{s16e_19f2_measured}} | {{s16e_19f2_residual}} | {{s16e_19f2_checks}} | {{s16e_19f2_agree}} | **{{s16e_19f2_rate}}** |
| `pluto-5d4d` | {{s16e_5d4d_applied}} | {{s16e_5d4d_measured}} | **{{s16e_5d4d_residual}}** | {{s16e_5d4d_checks}} | {{s16e_5d4d_agree}} | **{{s16e_5d4d_rate}}** |

`pluto-19f2` happens to be carrying a bias correct to 1.4 kHz, comfortably inside
the gate, and agrees on {{s16e_19f2_rate}} of its checks. `pluto-5d4d` is
carrying its **pre-swap** differential, wrong by {{s16e_5d4d_residual}} Hz — 38
times the gate — and agrees on {{s16e_5d4d_agree}} of {{s16e_5d4d_checks}}
checks. A factor of {{s16e_ratio}} between two radios watching the same sky
through the same code.

**That {{s16e_5d4d_rate}} is not a measurement of the sky.** No real coincidence
can pass a gate offset by half a megahertz, so what survives is whatever the
false-alarm floor happens to be. On this radio the check is switched off, and
nothing in the sidecar says so — `agrees: false` looks identical whether the sky
was empty or the bias was wrong.

![Agreement rate against the bias each radio was scored with](figures/cross-receiver-bias.png)

This is not a historical note: the census moved from {{s16e_census_before}} to
{{s16e_census_after}} scored entries while this audit was reading it. Every
sidecar written from here on records the same epoch-blind bias. The fix is the
one [16d](#16d-what-to-write-and-the-trap-waiting-for-whoever-writes-it) asks
for, with an epoch bound added — and it needs no re-scoring, because each check
stores `bias_hz` and `cfo_difference_hz` alongside its verdict, so `agrees` can
be recomputed offline once the right differential is known. Until then, **no
`agrees` field on `pluto-5d4d` should be read as evidence about the sky.**

### 16d. What to write, and the trap waiting for whoever writes it

`receiver_centers()` returns `measured_centers_hz` verbatim when present. The
pair below is pinned so that `rx0 − rx1` equals the measured differential —
`survey_scoring.cross_receiver_checks` gates agreement at
`CROSS_RECEIVER_CFO_HZ = 15,000` Hz on exactly that difference — and then slid
bodily so the unavoidable residual is halved between the two ports instead of
dumped on one.

| Radio | `measured_centers_hz` | Residual per port |
|---|---|---:|
| `pluto-19f2` | `{{s16d_pluto19f2_recommended}}` | ∓{{s16d_pluto19f2_residual}} |
| `pluto-5d4d` | `{{s16d_pluto5d4d_recommended}}` | ∓{{s16d_pluto5d4d_residual}} |

Four things the daily timer cannot do, and one it will actively undo:

1. **It never produces an absolute number.** `measure_mismatch` differences the
   two ports and its docstring says the absolute "is not recoverable here and is
   not needed". It is needed — a per-radio differential leaves both ports free to
   sit 150 kHz off *together*, which is exactly what `lnb-b` and `lnb-d` do.
2. **It cannot find an offset outside its own search**, for the self-sealing
   reason given in [section 14](#14-the-dead-port-and-the-stale-calibration-are-one-fault).
3. **It is currently returning an epoch-blind mixture**, averaging gen1's and
   gen2's differentials together inside one {{s16d_reports_examined}}-report
   window.
4. **It will erase the fix.** `command_starlink_lnb_calibration` builds its
   artifact from `measure_mismatch` alone and `write_calibration` replaces the
   file wholesale; `measured_centers_hz` is read by `lnb_calibration.py` and
   written nowhere. The snapshot confirms it: neither radio carries the field
   today. `leo-tracker-lnb-calibration.timer` next fires
   **{{s16d_timer_next}}** with `--apply`, so writing the values without first
   teaching the command to carry them forward from `previous`, or masking the
   timer, buys less than a day.
