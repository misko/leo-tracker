# The interim capture pipeline

These are the scripts that actually produced the synchronised paired-scan
corpus — 7,054 sweep attempts, roughly 350 GB, captured 2026-08-14T00:03Z to
14:49Z. They ran from `/mnt/leo-nvme/leo-tracker/bin/` on the capture host and
were untracked until now, which meant the code behind the dataset existed in
exactly one place, on one NVMe, with no history.

They are **not** the repository's implementation. `src/leo_tracker/radio/beacon/
synchronised_scan.py` is the proper version and differs in ways that matter:

- The repo version **meets the barrier on frequency**, retuning before the
  rendezvous, so its recorded skew is a real sample-start offset. These scripts
  stamp skew at **barrier release**, before each thread writes its LO — which is
  why every skew figure in the reports is a lower bound, and why it is blind to
  the geometry it is used to stratify on.
- The repo version carries schema versioning, error handling and tests. These
  are 286 lines with none of that.

They are committed here as the provenance of the corpus, not as code to develop.
If the capture path is revived, the right move is to run the repository version
and let the record schema say so — not to extend these.

| File | Role |
|---|---|
| `synccollect.py` | the collector: both radios, one process, one thread each, `threading.Barrier` per tuning, 12-arm draw, 90/10 arm pairing, per-radio edge order |
| `syncdrain.sh` | batched rsync to the share with byte-size verification; holds back the newest two directories so it can never race the collector |
| `importsync.py` | sweep directories into survey-corpus entries |
| `leo-sync-*.service`, `.timer` | the units that ran them |

One bug is worth remembering, because it cost 893 sweeps. The collector's
reopen path ran `del ctx[name]` before re-opening, so once a reopen failed the
key was gone and every later attempt raised on the `del` before reaching the
retry. One USB dropout at 03:24Z therefore produced 892 further self-inflicted
failures until 05:07Z. It is fixed here with `ctx.pop(name, None)`.

## The 1.25 MS/s arm ran through a filter nobody recorded

On 2026-08-15, after both radios were unplugged and re-attached, every draw at
1.25 MS/s failed with `EINVAL` on the write of `sampling_frequency`, while every
other rate worked. Both parts reported `filter_fir_config = FIR Rx: 0,0 Tx: 0,0`
— no FIR loaded — and refused 2,083,333 Hz as well as 1,250,000 Hz.

That is the AD9361's bare minimum sample rate, 25 MHz / 12 ≈ 2.083 MS/s. Going
below it requires a decimating FIR in the receive path. So the 1.25 MS/s arm of
the original corpus **only ever worked because some earlier tool had left an FIR
loaded in the part**, and re-plugging the radios cleared it.

Two consequences, and the second is the one that matters for analysis:

1. The arm is not reproducible from this repository. Nothing here loads an FIR,
   so a fresh capture host cannot recreate that configuration.
2. **The 1.25 MS/s captures went through a different receive filter chain from
   every other arm, and its coefficients were never recorded.** The corpus has
   no FIR readback — `collect_radio` writes `sampling_frequency` and
   `rf_bandwidth` and reads back neither, and this collector records neither.
   So the arm the detector report already treats as weakest, on the grounds that
   a 1.875 MHz pilot band cannot fit in a 1.25 MHz capture, carries a second and
   independent reason to distrust it: an undocumented decimation filter sat in
   front of it.

The 12-hour run started 2026-08-15T03:49:59Z drops the rate rather than loading
an FIR of unknown provenance, via `SYNC_SKIP_RATES_HZ=1250000`. Resurrecting the
arm by loading *some* FIR would not reproduce the original one, and would change
the receive response that the arm exists to measure.

## Environment the paced run added

| Variable | Default | Why |
|---|---|---|
| `SYNC_SWEEP_PERIOD_S` | `0` | Minimum seconds between sweep starts. Unthrottled this writes ~186 GB/h once each sweep's two corpus copies are counted; a long run needs to trade cadence for span, because coverage across the clock is what the corpus lacks. |
| `SYNC_MIN_FREE_BYTES` | `0` | Stop when the share falls below this. The failure guarded is a full share, which takes the drain, the import and the scoring host down together. |
| `SYNC_FREE_CHECK` | `/mnt/qnap01/mouse9911` | Where to measure free space: where the sweeps end up, not where they start. |
| `SYNC_SKIP_RATES_HZ` | *(empty)* | Sample rates to leave out of the arm draw. |
