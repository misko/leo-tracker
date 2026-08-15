## Executive summary

Eight algorithms were built to decide whether a Starlink downlink channel is
lit, by looking for the eight known pilot subcarriers at the channel's band
edge. To rank them you need ground truth. Nothing is injected at this site, so
there is none, and the substitute chosen was two independent radios watching the
same channel at the same instant: if both fire more often than chance, something
was there, and a coincidence model turns the three counted rates into a sky
occupancy `f` and a per-chain detection probability `d`.

Two radios were driven from one process with a barrier at every tuning. {{exec_paired_sweeps}}
paired sweeps were captured, shipped byte-verified, imported, and scored. That
part works and is documented in
[section 3](#3-the-apparatus-two-radios-one-instant).

The method it was built to serve does not. The validation offered for the
coincidence model was that the eight algorithms agree on `f`. Run the identical
estimator on two joins where the model is **definitionally false** — radio B
taken two instants later, and radio A joined to a different sweep entirely — and
the eight agree at least as tightly: spread {{exec_scrambled_spread}} on the scrambled join against
{{exec_real_spread}} on the real one, each at or below its own sampling noise. The controls
are not demonstrably *tighter* — they are indistinguishable, which is all the
argument needs. Under the model's own separation rule no control separates from
the real join. The check has no failure mode
([section 6](#6-did-it-work-the-negative-controls)).

The reason is measurable. Over {{exec_observations}} observations every pair of the eight
detectors makes the same fire / no-fire call at phi {{exec_phi_min}}–{{exec_phi_max}}. They are one
statistic counted eight times, and near-duplicates are obliged to return
near-identical `f` whatever they are fed
([section 7](#7-why-it-failed-the-detectors-are-near-duplicates)).

What survives is observational rather than causal. A channel's own two edges
agree at phi {{exec_same_channel_phi_min}}–{{exec_same_channel_phi_max}}, far above any two different channels at {{exec_cross_channel_phi_min}}–{{exec_cross_channel_phi_max}} once
the acquisition arm is controlled — and that weaker cross-channel term *rises*
with channel separation rather than staying flat, which no uniform common mode
produces and which this report does not explain. Agreement between two chains
varies with receiver configuration, but the design is not factorial and cannot
say whether receiver, radio, edge, water or timing is responsible; the earlier
claim that the LNB was the entire cause is withdrawn in
[section 8](#8-what-the-sky-looks-like). The instrument yields two associations
worth acting on: a scoring-pipeline detection cliff near {{exec_cliff_low_khz}}–{{exec_cliff_high_khz}} kHz of corrected
offset, and a large positive centre correction on one port without which a
working receiver reads as deaf ([section 9](#9-what-survives-the-instrument)) —
though the specific {{exec_lnbc_applied_figure}} value is itself wrong by {{exec_lnbc_miscentred_khz}} kHz, as
[section 16](#16-the-150-khz-measured-four-oscillators-and-what-it-costs) measures.

**Since this report was first written, ground truth arrived.** A cabled
loopback on two bench radios injects the repository's own pilot waveform at a
known amplitude, occupancy and carrier offset, which converts the central
questions from inference into measurement.
[Section 11](#11-ground-truth-at-last-measured-detection-probability) reports
it: the measured detector ranking is uncorrelated with both the model's ranking
(rho {{exec_rho_measured_vs_model}}) and the fire-count ranking ({{exec_rho_measured_vs_fire}}); the coincidence estimator
brackets a known `f` at moderate and high occupancy but reads **low at low
occupancy**, while its diagnostic refuses to certify at any level — so the solver
partly works and the check does not, and nothing here validates the pooled model
on the heterogeneous sky corpus; the {{exec_cliff_low_khz}}–{{exec_cliff_high_khz}} kHz cliff does not exist against a *known* offset,
falling instead at the {{exec_bank_edge_khz}} kHz bank edge; and the thresholds are calibrated on
truly empty input.

**And the instrument itself is losing detections right now.**
[Section 16](#16-the-150-khz-measured-four-oscillators-and-what-it-costs)
measures each receiver's absolute carrier centre against two independent
populations, finds four independent oscillator errors between {{exec_ppm_min}} and
{{exec_ppm_max}} ppm — normal hardware, never measured — and shows that correcting each one
separately moves the pooled sky window from a centroid of {{exec_before_centroid}} kHz onto
**{{exec_after_centroid}} kHz** in an out-of-sample test. Against the calibration in force today all
four ports are miscentred: three by {{exec_lnbd_miscentred_khz}}–{{exec_lnbc_miscentred_khz}} kHz, which a controlled contrast
prices at **{{exec_lost_three_ports}} of detections lost**, and `lnb-a` by {{exec_lnba_miscentred_khz}} kHz, priced at
**{{exec_lost_lnba}}**. The daily differential calibration cannot measure any of this, and as
written it would erase the fix.

Every `d` in the sections below is a **model output**, never a measurement. It is
inferred from a model whose own consistency check is the one shown above to be
incapable of failing. That caveat is repeated wherever a `d` appears, because it
is load-bearing every time.
