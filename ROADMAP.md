# PCB Zone Check -- Road Map

## 1. Quality Gate (pre-inspection photo check)

Before running inspection -- evaluate the incoming photo and warn/reject if quality is low.

**Metrics:**
- **Sharpness** -- Laplacian variance. Blurry phone photo = false defects
- **Brightness / contrast** -- mean + stddev. Too dark or overexposed = noise
- **Alignment score** -- homography reprojection error (anchors already exist)
- **Zone resolution** -- if zone crop < N px after homography, model can't work reliably

**UX:**
- Green / yellow / red indicator per photo BEFORE operator makes a decision
- Yellow = warning (proceed with caution), Red = reject (re-take photo)

**Effort:** ~100 lines backend, minor frontend changes

---

## 2. Template Health Score

When saving/editing a template -- evaluate how "inspectable" it is.

**Per-zone metrics:**
- **Texture entropy** -- zones with uniform surface (low gradient) give random results
- **Zone size** -- too large = noise, too small = not enough context
- **Zone overlap** -- heavily overlapping zones produce correlated results
- **Reference image quality** -- same sharpness/brightness metrics as Quality Gate

**UX:**
- Score per zone in template editor
- Recommendations: "Zone 3: low texture, consider splitting" or "Reference: low sharpness, re-capture"

**Effort:** moderate, reuses Quality Gate metrics

---

## 3. Calibration Mode

A calibration workflow for existing templates. Lead calibrates before deploying a template to production.

### Phase 1 -- Known Good (golden sample)

Lead takes a **known good board** and photographs it 5-10 times:
- From different phones
- With slightly different lighting
- With small shift/rotation

System runs inspection on each shot and collects **per-zone statistics:**
- SSIM mean / std / min / max
- defect_pct mean / std / min / max
- Alignment reprojection error
- Photo sharpness (Laplacian variance)

**Result: per-zone stability card**
```
Zone 1 "IC U5":  stable    sigma=0.02  recommended_threshold=0.35
Zone 2 "Cap C3": unstable  sigma=0.28  WARNING: high variance
Zone 3 "Conn":   stable    sigma=0.05  recommended_threshold=0.42
```

### Phase 2 -- Known Bad (defect sample, optional)

Lead takes a board with a **known defect** (or marks with tape/marker). Runs inspection. System checks:
- Did the model detect the defect in the correct zone?
- With what score?
- What margin above threshold?

**Result: detection confidence**
```
Zone 2 "Cap C3": defect detected, score=0.87, margin=+0.37  OK
Zone 5 "R12":    defect NOT detected, score=0.41             FAIL
```

### Phase 3 -- Calibration Report

Everything saved into the template as a `calibration` object:

```json
{
  "calibrated_at": "2026-03-19T...",
  "calibrated_by": "lead for line1",
  "good_samples": 8,
  "bad_samples": 2,
  "zones": [
    {
      "label": "IC U5",
      "stability": "stable",
      "sigma": 0.02,
      "recommended_sensitivity": 0.35,
      "detection_verified": true,
      "margin": 0.37
    }
  ],
  "overall_confidence": 0.94
}
```

**Template badge in UI:**
- Green: calibrated, confidence > 0.85
- Yellow: calibrated, but has unstable zones
- Red: not calibrated
- Grey: calibration stale (template changed after calibration)

### What calibration gives:

1. **Lead** understands: "this template is reliable" vs "this zone needs rework"
2. **Operator** sees: "template is calibrated" -- trust in results
3. **System** can auto-set optimal sensitivity instead of manual tuning
4. **v1 API** can return `calibration_confidence` in results for MES integration

---

## Implementation order

| # | Feature             | Depends on | Priority |
|---|---------------------|------------|----------|
| 1 | Quality Gate        | --         | High     |
| 2 | Template Health     | 1 (reuses) | Medium   |
| 3 | Calibration Phase 1 | 1, 2       | Medium   |
| 4 | Calibration Phase 2 | 3          | Low      |
| 5 | Calibration Report  | 3          | Medium   |
| 6 | Auto-sensitivity    | 3          | Low      |
