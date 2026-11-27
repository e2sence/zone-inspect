# Zone Inspect — Roadmap

State as of 2026-11-27. The earlier plan (March 2026) was written before Recipe
Studio; most of it is done, in a different shape than planned. This file lists
what exists, what is next, and what was deliberately postponed.

## Done

### Recipes and Recipe Studio
Replaced hand-tuned templates. A recipe is prepared from sample photos and
published to the line by a lead.

- Recipe versions (draft / active / archived); old templates migrated with their history.
- Good and defect samples captured from a phone; defect labelling.
- Publish gates: at least 15 good samples per zone from 2 or more phones,
  2 defect samples per zone, 1 per critical subzone. A recipe that misses a
  defect sample is not published unless the miss is explicitly accepted.
- Photo quality check (sharpness, exposure, resolution) for the reference and for every sample.
- Operators see published recipes only; strictness comes from the recipe.

### Calibration from samples
Was "Calibration Mode" in the old plan (phases 1–3 and auto-sensitivity).

- A model of normal variation per zone, built from the good samples.
- OK limit per zone, set from the good samples and checked against the defect samples; presets.
- Suspect good samples are pointed out.
- Viewpoint is measured relative to the good samples; a live hint on the phone
  viewfinder keeps the operator within it.

### Phone inspection
- A zone is checked as soon as its photo is uploaded; the camera stays open between shots.
- Barcodes are read on the phone.

### Line-system gate
For a line system (MES) that cannot call the API but can run a program with the
scanned serial number.

- `GET /api/v1/verdict?serial=…` — verdict of the latest inspection of a board.
- `integrations/windows/zi_check.py` — prints the verdict and a link to the
  result, ends with `EXIT WITH ERRORLEVEL==N`. Python 3.7+, no dependencies.
- `/r/<result_id>` — a read-only page of one result.

### Production
- Several stations at once: 8 web threads, at most 2 zone checks at a time.
- Deterministic matching, cached reference features.
- `run-deploy.sh`: copies only changed tracked files, never deletes, backs up,
  waits for the health check, can roll back.

## Next

| # | Item | Why | Size |
|---|------|-----|------|
| 1 | Quality gate on the operator's photo | A blurred handheld shot gives false defects | Small |
| 2 | Line-system gate: first stations | Proves the integration on real hardware | Small |
| 3 | Zone health hints in Recipe Studio | Catches zones that cannot be inspected reliably before samples are collected | Medium |

### 1. Quality gate on the operator's photo
The quality check already runs for references and samples, but not for the photo
an operator takes during inspection.

- Run the same check when an inspection photo is uploaded.
- Show green / yellow / red on the phone before the verdict; red asks to retake.
- Decide whether red blocks the check or only warns.

### 2. Line-system gate: first stations
Deployed, tested against a stub and a test database, not yet run on a station.

- Run on a real station (Windows, Python 3.7); check root certificates there.
- Confirm that the serial scanned by the line system equals the code Zone Inspect
  reads from the board. If not, add a normalisation rule (prefix, separators).
- Results saved by an admin have no group, so a key bound to a group does not
  see them. Decide whether that is right or whether such results need a group.

### 3. Zone health hints in Recipe Studio
Was "Template Health Score". Today the only hint is a warning for features
smaller than 48 px.

- Low texture: a zone with a uniform surface gives unstable scores.
- Heavy overlap between zones.
- Zone too large or too small for what it contains.

## Later

### Backbone: EfficientNetV2-S instead of EfficientNet-B4
Expected to be 30–40% faster on CPU. Postponed: calibration models are built
from B4 features, so every published recipe would have to be recalibrated and
re-checked against its defect samples. Worth doing when check time becomes the
bottleneck, and with a migration plan for existing recipes.

### Calibration confidence in the API
Return how well a recipe is calibrated (`calibration_confidence`) with results,
for line systems that want to weigh a verdict. No request for it so far.

## Superseded

- **Template badge (calibrated / stale / not calibrated)** — replaced by recipe
  status and publish gates: an uncalibrated recipe cannot reach the line.
- **Manual sensitivity tuning** — replaced by the OK limit per zone from calibration.
