# Plan: CLEAR: an auditable foundation model for radiology grounded in clinical concepts

- Issue: #2
- Paper: `recommended/2026-08-19/clear/paper.json`
- DOI: https://doi.org/10.1038/s41551-026-01741-4
- Physician focus: whether confounders such as view position (AP/PA) affect AI judgement on our chest X-ray data, and what our CXR demo can reproduce.

## Proposed presentation

Dashboard entry, in this order:

1. **Cohort strip (text + small table)**: 272 studies, AP 88 / PA 184, with abnormal rate per view. Purpose: show first that AP and PA differ in size and case mix, so any gap is read as a possible confound rather than a view effect.
2. **Grouped bar chart: per-label sensitivity, AP vs PA** (x: finding label, y: sensitivity, two bars per label, n printed on each bar, labels with fewer than 5 cases per view greyed out). Grouped bars make the AP/PA gap directly comparable per finding, and the printed n stops the physician over-reading tiny cells.
3. **Forest-style dot plot: AUROC / sensitivity difference (PA minus AP) with bootstrap 95% CI** per label. Shows whether the gap is distinguishable from noise, which bars alone cannot.
4. **Stratified table: view x institution x device**, with counts. Checks whether view is separable from site or device (confound of confound).
5. **Grad-CAM panel (qualitative)**: the existing CXR Lens overlays for matched AP/PA false negatives and false positives, kept out of the repository. This is the closest available stand-in for the paper's concept-level attribution, and it is not the same method.
6. **Text sections**: "What the paper reports", "What our data shows", "What we cannot conclude", each with sources.

## Open questions

- Is the target question "does model performance differ by view" (reproducible here), or "which concepts explain the difference" (needs a concept model such as CLEAR, not our DenseNet)?
- Which labels should be the primary outcome, given many have fewer than 10 cases per view? Suggest Infiltration, Atelectasis and Effusion; pool the rest as "any abnormal".
- Should multi-label cases (50 of 272) count as positive for each label, or be analysed separately?
- Should we adjust for institution, device, sex and age before comparing AP and PA?
- Is a Grad-CAM-based proxy for concept attribution acceptable, or is the physician only interested in the performance-gap audit?
