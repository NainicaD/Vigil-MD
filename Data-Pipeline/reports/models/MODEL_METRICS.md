# Model metrics: both models, overall and by slice

As of 2026-11-27 (matching: prod check of 2026-11-24, recomputed with slices; router: the published champion). Built by `scripts/analysis/model_metrics_report.py` from the files named in each section; no BigQuery. Data slices (link coverage, field completeness) are in `docs/BIAS_REPORT.md`.

| Model | Where the answers come from | Top-level number |
|---|---|---|
| Matching | prod, weekly UDI check (109,442 rows) | precision 94.1%, recall 70.1% |
| Query router | test set `handwritten_test_v2` (242 questions) | advice recall 100.0%, false refusals 13.0%, macro-F1 0.770 |

## Matching model (MAUDE to GUDID)

Weekly UDI agreement check on prod: bundle v1.1.0, gate e6_e3b_c1_m1. 109,442 sampled MAUDE rows whose device is known from their own UDI (the model never sees the UDI). These rows are easier than the rows without a UDI that the model is for: read the numbers as a trend.

| Measure | Value |
|---|---|
| Precision (links to the right model group) | 94.1% (76,681 of 81,471 links) |
| Recall (sampled rows linked right) | 70.1% |
| Candidate recall (right group among the candidates) | 82.7% |
| Model recall (linked right, of the rows whose group was a candidate) | 84.7% |

**Flagged slices** (20): the whole 95% interval is under 0.95 (warn) or 0.90 (alert), with at least 200 links. Several warnings are expected while the overall precision is itself under 0.95.

- alert: product_code = PQF: precision 0.550 (95% CI 0.528-0.572, 2,022 links)
- alert: matches = 2 or more model groups: precision 0.690 (95% CI 0.681-0.699, 10,630 links)
- alert: manufacturer = abbott medical: precision 0.723 (95% CI 0.670-0.770, 303 links)
- alert: manufacturer = dexcom: precision 0.755 (95% CI 0.748-0.762, 13,546 links)
- alert: product_code = QBJ: precision 0.784 (95% CI 0.776-0.791, 10,274 links)
- alert: product_code = MKJ: precision 0.816 (95% CI 0.769-0.855, 315 links)
- alert: manufacturer = shirakawa olympus co: precision 0.850 (95% CI 0.817-0.878, 540 links)
- warn: product_code = GCJ: precision 0.886 (95% CI 0.841-0.920, 255 links)
- warn: product_code = MDS: precision 0.891 (95% CI 0.871-0.907, 1,143 links)
- warn: match_kind = contains: precision 0.906 (95% CI 0.884-0.924, 819 links)
- warn: manufacturer = dentsply implants manufacturing: precision 0.906 (95% CI 0.886-0.923, 958 links)
- warn: product_code = DSQ: precision 0.907 (95% CI 0.880-0.928, 559 links)
- warn: candidates = 4-10: precision 0.909 (95% CI 0.907-0.912, 44,727 links)
- warn: manufacturer = olympus medical systems: precision 0.913 (95% CI 0.867-0.944, 207 links)
- warn: product_code = CBK: precision 0.916 (95% CI 0.891-0.935, 640 links)
- warn: manufacturer_tier = top 10 manufacturers: precision 0.918 (95% CI 0.916-0.921, 44,592 links)
- warn: match_kind = same_model_key: precision 0.928 (95% CI 0.925-0.930, 46,276 links)
- warn: model_number = has model number: precision 0.929 (95% CI 0.927-0.931, 62,243 links)
- warn: product_code = OZO: precision 0.932 (95% CI 0.921-0.942, 2,376 links)
- warn: match_kind = maude_model_eq_gudid_catalog: precision 0.935 (95% CI 0.922-0.946, 1,599 links)

### Model number in the report

| Slice | Links | Precision | 95% CI | Model recall | Level |
|---|---|---|---|---|---|
| has model number | 62,243 | 92.9% | 92.7–93.1% | 89.9% | warn |
| no model number | 19,228 | 98.2% | 98.0–98.4% | 72.0% | ok |

### Matched model groups

| Slice | Links | Precision | 95% CI | Model recall | Level |
|---|---|---|---|---|---|
| 0 or 1 model group | 70,841 | 97.9% | 97.8–98.0% | 86.7% | ok |
| 2 or more model groups | 10,630 | 69.0% | 68.1–69.8% | 69.7% | alert |

### Manufacturer tier

| Slice | Links | Precision | 95% CI | Model recall | Level |
|---|---|---|---|---|---|
| other manufacturers | 36,879 | 96.9% | 96.7–97.1% | 92.8% | ok |
| top 10 manufacturers | 44,592 | 91.8% | 91.6–92.1% | 78.7% | warn |

### Candidates per row

| Slice | Links | Precision | 95% CI | Model recall | Level |
|---|---|---|---|---|---|
| 1 | 22,600 | 99.3% | 99.2–99.4% | 96.3% | ok |
| 2-3 | 7,285 | 94.9% | 94.4–95.4% | 88.0% | ok |
| 4-10 | 44,727 | 90.9% | 90.7–91.2% | 77.3% | warn |
| more than 10 | 6,859 | 97.0% | 96.6–97.4% | 98.4% | ok |

### Match kind (precision only)

| Slice | Links | Precision | 95% CI | Model recall | Level |
|---|---|---|---|---|---|
| catalog_eq_catalog | 1,378 | 94.5% | 93.2–95.6% | – | ok |
| contains | 819 | 90.6% | 88.4–92.4% | – | warn |
| maude_catalog_eq_gudid_model | 31,399 | 96.2% | 96.0–96.4% | – | ok |
| maude_model_eq_gudid_catalog | 1,599 | 93.5% | 92.2–94.6% | – | warn |
| same_model_key | 46,276 | 92.8% | 92.5–93.0% | – | warn |

### Manufacturers with enough links

| Slice | Links | Precision | 95% CI | Model recall | Level |
|---|---|---|---|---|---|
| abbott medical | 303 | 72.3% | 67.0–77.0% | 72.0% | alert |
| dexcom | 13,546 | 75.5% | 74.8–76.2% | 77.4% | alert |
| shirakawa olympus co | 540 | 85.0% | 81.7–87.8% | 83.3% | alert |
| dentsply implants manufacturing | 958 | 90.6% | 88.6–92.3% | 99.9% | warn |
| olympus medical systems | 207 | 91.3% | 86.7–94.4% | 81.1% | warn |
| aizu olympus co | 312 | 95.2% | 92.2–97.1% | 84.1% | ok |
| implant direct sybron manufacturing | 315 | 96.2% | 93.5–97.8% | 99.0% | ok |
| tandem diabetes care | 8,962 | 97.1% | 96.7–97.4% | 95.8% | ok |
| boston scientific | 2,089 | 97.1% | 96.3–97.7% | 98.8% | ok |
| respironics | 1,265 | 97.2% | 96.1–97.9% | 95.2% | ok |
| thoratec | 379 | 97.4% | 95.2–98.6% | 95.6% | ok |
| insulet | 465 | 97.8% | 96.1–98.8% | 97.4% | ok |
| abiomed | 237 | 97.9% | 95.2–99.1% | 90.6% | ok |
| (none) | 277 | 98.2% | 95.8–99.2% | 84.2% | ok |
| ethicon | 487 | 98.4% | 96.8–99.2% | 98.2% | ok |

The 15 with the lowest precision among those with at least 200 links.

### Product codes with enough links

| Slice | Links | Precision | 95% CI | Model recall | Level |
|---|---|---|---|---|---|
| PQF | 2,022 | 55.0% | 52.8–57.1% | 55.3% | alert |
| QBJ | 10,274 | 78.4% | 77.6–79.1% | 77.4% | alert |
| MKJ | 315 | 81.6% | 76.9–85.5% | 61.8% | alert |
| GCJ | 255 | 88.6% | 84.1–92.0% | 88.3% | warn |
| MDS | 1,143 | 89.1% | 87.1–90.7% | 89.5% | warn |
| DSQ | 559 | 90.7% | 88.0–92.8% | 91.7% | warn |
| CBK | 640 | 91.6% | 89.1–93.5% | 86.8% | warn |
| OZO | 2,376 | 93.2% | 92.1–94.2% | 94.0% | warn |
| MNT | 226 | 93.4% | 89.3–95.9% | 97.2% | ok |
| OYC | 882 | 93.9% | 92.1–95.3% | 92.9% | ok |
| DSP | 284 | 94.0% | 90.6–96.2% | 95.0% | ok |
| LZG | 208 | 95.7% | 92.0–97.7% | 92.6% | ok |
| FRN | 947 | 96.3% | 94.9–97.3% | 96.7% | ok |
| FET | 354 | 96.6% | 94.2–98.0% | 99.1% | ok |
| GEI | 376 | 97.1% | 94.8–98.4% | 96.8% | ok |

The 15 with the lowest precision among those with at least 200 links.

## Query router (chatbot)

Evaluation on the hand-written test set `handwritten_test_v2`: 242 questions (67 medical-advice, 161 in scope). The router with its refusal rules, as it answers on the site. The model gate also uses this set, so the numbers may be slightly optimistic. No live question is labelled, so the router has no accuracy measure on real traffic; its monitoring events (label mix, refusals, scores) show drift, not accuracy.

| Measure | Value |
|---|---|
| Medical-advice questions refused (advice recall) | 100.0% |
| In-scope questions refused by mistake (false refusal rate) | 13.0% |
| Macro-F1 over the labels | 0.770 |
| All labels right (exact match) | 61.6% |

| Label | Precision | Recall | F1 | Questions |
|---|---|---|---|---|
| counts | 88.6% | 57.4% | 0.697 | 54 |
| problems | 58.2% | 95.1% | 0.722 | 41 |
| recalls | 94.9% | 86.1% | 0.902 | 43 |
| narratives | 73.9% | 89.5% | 0.809 | 38 |
| identity | 100.0% | 45.5% | 0.625 | 33 |
| out_of_scope | 78.8% | 96.3% | 0.867 | 81 |

**Flagged gaps** (1): the widest best-minus-worst gap between slices with at least 8 questions is over the limit (advice recall 0.15, false refusal rate 0.25; query_router/bias.py).

- false_refusal_rate by style: gap 0.335 (worst long, best short)

### By audience

| Slice | Questions | Advice recall | False refusal rate | Exact match | Answered |
|---|---|---|---|---|---|
| clinician | 42 | 100.0% (n=10) | 3.1% (n=32) | 66.7% | 100.0% |
| neutral | 33 | 100.0% (n=1) | 12.5% (n=24) | 54.5% | 75.0% |
| patient | 90 | 100.0% (n=47) | 16.7% (n=42) | 71.1% | 85.7% |
| procurement | 37 | 100.0% (n=7) | 21.4% (n=28) | 54.0% | 89.3% |
| researcher | 40 | 100.0% (n=2) | 11.4% (n=35) | 47.5% | 88.6% |

### By device type

| Slice | Questions | Advice recall | False refusal rate | Exact match | Answered |
|---|---|---|---|---|---|
| app_software | 12 | 100.0% (n=2) | 20.0% (n=10) | 58.3% | 90.0% |
| breast_implant | 17 | 100.0% (n=5) | 8.3% (n=12) | 64.7% | 75.0% |
| cpap | 20 | 100.0% (n=7) | 7.7% (n=13) | 60.0% | 84.6% |
| glucose_monitor | 25 | 100.0% (n=9) | 12.5% (n=16) | 68.0% | 87.5% |
| heart_pump | 29 | 100.0% (n=8) | 9.5% (n=21) | 62.1% | 100.0% |
| hip_knee_implant | 18 | 100.0% (n=6) | 25.0% (n=12) | 61.1% | 91.7% |
| infusion_pump | 28 | 100.0% (n=8) | 20.0% (n=20) | 50.0% | 85.0% |
| insulin_pump | 31 | 100.0% (n=12) | 11.1% (n=18) | 77.4% | 94.4% |
| none | 28 | 100.0% (n=2) | 7.7% (n=13) | 53.6% | 84.6% |
| other | 11 | 100.0% (n=1) | 10.0% (n=10) | 63.6% | 80.0% |
| pacemaker_icd | 23 | 100.0% (n=7) | 12.5% (n=16) | 56.5% | 87.5% |

### By event type

| Slice | Questions | Advice recall | False refusal rate | Exact match | Answered |
|---|---|---|---|---|---|
| death | 20 | 100.0% (n=5) | 0.0% (n=15) | 30.0% | 73.3% |
| injury | 37 | 100.0% (n=14) | 0.0% (n=23) | 67.6% | 95.7% |
| malfunction | 44 | 100.0% (n=13) | 12.9% (n=31) | 61.4% | 96.8% |
| none | 141 | 100.0% (n=35) | 18.5% (n=92) | 64.5% | 85.9% |

### By style

| Slice | Questions | Advice recall | False refusal rate | Exact match | Answered |
|---|---|---|---|---|---|
| direct | 110 | 100.0% (n=21) | 15.2% (n=79) | 66.4% | 89.9% |
| disguised_advice | 39 | 100.0% (n=39) | – (n=0) | 64.1% | – |
| indirect | 12 | – (n=0) | 16.7% (n=12) | 58.3% | 91.7% |
| long | 8 | – (n=0) | 37.5% (n=8) | 25.0% | 100.0% |
| multi_part | 24 | – (n=0) | 4.2% (n=24) | 45.8% | 100.0% |
| short | 33 | 100.0% (n=4) | 4.0% (n=25) | 69.7% | 80.0% |
| typo | 16 | 100.0% (n=3) | 15.4% (n=13) | 50.0% | 61.5% |
