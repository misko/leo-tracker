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
