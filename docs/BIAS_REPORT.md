# Bias slice analysis of the PROD tables

Run on 2026-10-26 against `vigil-md-1792696816.vigil_md_prod`. Produced by `scripts/analysis/bias_slices.py`. The results are in `data/interim/bias/`, which is git-ignored, so re-run the script to regenerate them.

Re-run on 2026-10-27 with the same code: identical headline rates (UDI availability 61.9%, primary-DI match 81.2%, MAUDE→GUDID 50.2%). Section 6 (mitigation) was added on 2026-10-27.

## 1. Scope and method

**What this is:** an analysis of what the existing pipeline already produces. It is not a redesign of the pipeline.
- Every query is a read-only `SELECT` on the four PROD tables.
- No RAW JSON was scanned. No table, view or other warehouse object was created. No matching rule was changed.
- Slices use only PROD columns.
- The script enforces this. `assert_read_only` rejects any statement that isn't a SELECT, any DML or DDL keyword, and any dataset other than `vigil_md_prod`. Each query is dry-run first, and the real job runs with `maximum_bytes_billed` set.

**Metrics.** These definitions follow the existing rules exactly.

| Metric | Definition |
|---|---|
| UDI availability | MAUDE device rows with `udi_di IS NOT NULL` ÷ all MAUDE device rows |
| Conditional primary-DI match | Of the rows that have a UDI, the share where `udi_di` exactly equals a GUDID primary DI. This is the rule in `scripts/matching/exact_linking.py`: no TRIM or UPPER, compared against the DISTINCT set of GUDID DIs so the join cannot fan out |
| End-to-end MAUDE→GUDID | Matched rows ÷ all MAUDE rows. This equals availability × match |
| Recall↔enforcement | Share of rows that have a counterpart on `link_key` (`product_res_number` = `recall_number`), measured in each direction |
| Field completeness | Rows where the value is not null and not blank (`TRIM(x) != ''`; for arrays, not empty) ÷ rows. Measured for every business column in `schemas.FLAT_COLUMNS` |

**Slices.**
- **maude:** year (`record_date`), event_type, adverse_event_flag, product_code (top 25 + OTHER), manufacturer (top 25 + OTHER), manufacturer volume tier (top 10 / 11–100 / long tail, ranked by row count in that table)
- **gudid:** publish year, device_record_status, distribution_status, manufacturer, manufacturer tier
- **recall:** year, recall_status, root_cause, product_code, manufacturer tier
- **enforcement:** year, recall_class, status, manufacturer tier

**Statistics.**
- Each slice gets a Wilson 95% CI.
- `gap_pp` is the slice rate minus the overall rate, in percentage points.
- A slice is a **disparity flag** when n ≥ 30, |gap| ≥ 10 pp, and its CI excludes the overall rate.
- `below_four_fifths` marks slices whose rate is below 0.8 × the overall rate. It is not applied to error-rate metrics.
- With millions of rows almost every CI is narrow, so the size of the gap matters more than the flag.

## 2. Data snapshot

| Table | Rows | Newest `last_updated_at` (UTC) |
|---|---:|---|
| maude (device rows) | 21,532,440 | 2026-10-26 21:45 |
| gudid | 5,238,573 | 2026-10-26 05:56 |
| recall | 59,552 | 2026-10-26 00:36 |
| enforcement | 40,299 | 2026-10-26 16:07 |

- The 8 queries scanned 7.6 GB in total. A re-run was served from the BigQuery result cache.
- MAUDE `record_date` covers 2016–2026. Every GUDID row has `device_record_status = Published`.

## 3. Results

### 3.1 Overall

| Metric | Rate |
|---|---:|
| MAUDE UDI availability | **61.9%** |
| Primary-DI match, given a UDI | **81.2%** |
| End-to-end MAUDE→GUDID | **50.2%** |
| Recall→enforcement | **67.7%** |
| Enforcement→recall | **99.995%** (2 unlinked rows) |
| MAUDE `udi_di` outside the 6–23 character length rule | 0.0005% of non-null values |

**Why unmatched UDIs don't match** (`unmatched_udi_shape.csv`, diagnostic only):

| Shape | Unmatched rows | Would match after normalization |
|---|---:|---:|
| well-formed, not in GUDID | 2,507,324 | 0 |
| unexpected characters | 838 | 0 |
| `(01)` GS1 prefix | 67 | 0 |
| longer than 23 | 37 | 0 |
| shorter than 6 | 29 | 0 |
| `01` + 14 digits, no parentheses | 19 | 19 |
| lower case | 1 | 1 |

- **99.96%** of the 2.51M unmatched UDIs are well-formed DIs that simply aren't among GUDID's primary DIs.
- Normalization (trim, upper-case, strip the GS1 prefix) would recover **20 rows**.
- So the non-match is **not** a formatting problem. It most likely means MAUDE reports a DI that GUDID PROD doesn't store: a secondary, package or unit-of-use DI, or a device missing from the GUDID snapshot. PROD can't tell these apart (see limitation L2).

### 3.2 MAUDE→GUDID by slice

The end-to-end rate splits into availability × match, so each gap can be traced to missing UDIs or to failed matches.

| Slice | n | Availability | Match | End-to-end |
|---|---:|---:|---:|---:|
| **overall** | 21,532,440 | 61.9% | 81.2% | 50.2% |
| event_type Injury | 7,876,204 | 59.9% | 89.7% | 53.7% |
| event_type Malfunction | 13,525,710 | 63.2% | 76.4% | 48.3% |
| event_type **Death** | 119,082 | **44.9%** | 85.9% | **38.6%** |
| event_type Other / null / "No answer provided" | 11,444 | 0.6–7.2% | — | 0.6–4.5% |
| tier top 10 | 11,259,006 | 71.1% | 78.9% | 56.1% |
| tier 11–100 | 6,969,796 | 53.3% | 85.0% | 45.3% |
| tier **long tail** | 3,203,006 | **48.4%** | 84.0% | **40.7%** |
| manufacturer (null) | 100,632 | 46.3% | 74.6% | 34.5% |
| year **2016** | 868,529 | **14.2%** | 81.5% | **11.5%** |
| year 2017 | 938,324 | 35.9% | 85.6% | 30.7% |
| year 2022 (highest) | 2,954,699 | 73.6% | 86.5% | 63.7% |
| year **2026** | 2,924,614 | 56.5% | **68.1%** | **38.5%** |

**Lowest primary-DI match**, top-25 manufacturers and product codes, n ≥ 19k each:

| Slice | Match |
|---|---:|
| Insulet | 17.0% |
| Medtronic MiniMed | 24.4% |
| Abbott Diabetes Care | 28.1% |
| Medtronic Puerto Rico Operations | 34.6% |
| Nobel Biocare USA | 63.4% |
| Respironics | 66.5% |
| Product code LZG | 13.7% |
| Product code OZP | 22.3% |
| Product code OYC | 24.4% |
| Product code QLG | 27.7% |
| Product code BZD | 57.0% |

Several top manufacturers match at 99–100%, for example Straumann, CareFusion SD, and JJGC.

**Lowest UDI availability:**

| Slice | Availability |
|---|---:|
| Medtronic MiniMed | 7.4% |
| Product code PZE | 13.4% |
| Product code NBW | 18.7% |
| Allergan (Costa Rica) | 30.9% |

### 3.3 Recall↔enforcement by slice

| Slice | n | Recall→enforcement |
|---|---:|---:|
| overall | 59,552 | 67.7% |
| recall year 2002–2011 | 17,694 | **0.0%** |
| recall year 2012 | 2,724 | 48.7% |
| recall year 2013–2026 | 25,558 | 96.7–100% |
| recall year unknown (no `record_date`) | 13,576 | 99.4% |
| recall_status Terminated | 44,686 | 57.1% |
| recall_status Open, Classified | 14,424 | 99.4% |
| root_cause "Other" / "PMA" / "Pending" | 8,928 / 109 / 344 | 11.0% / 5.5% / 18.6% |

Enforcement→recall is about 100% in every slice: by class, status, year and manufacturer tier.

### 3.4 Field completeness

**Least complete fields overall:**

| Field | Complete |
|---|---:|
| maude `lot_number` | 56.2% |
| gudid `catalog_number` | 57.5% |
| maude `udi_di` | 61.9% |
| maude `model_number_raw` | 73.3% |
| recall `record_date` | 77.2% |
| maude `catalog_number` | 85.5% |

All other fields are at least 99.4% complete. Every enforcement field is at least 99.9% complete.

**Notable slice gaps** (full detail in `field_completeness.csv`):
- **MAUDE Death reports:** `udi_di` 45% and `catalog_number` 72%, against 61.9% and 85.5% overall.
- **Lot numbers vary by manufacturer:** about 0% for Respironics, CareFusion SD and CareFusion 303, against 98% for Intuitive Surgical. They also vary by product code: QFG 17%, FRN 5%.
- **MAUDE `event_type` "Other", null or "No answer provided"** (11k rows): poor on almost every field.
- **GUDID `catalog_number`:** 0% for some manufacturers, such as Alphatec Spine, and 100% for others.

## 4. Interpretation

1. **The linked MAUDE→GUDID subset is not representative.** Only half of MAUDE links, and the linked half shifts in composition:
   - **Death reports** are about 23% under-represented: 0.55% of all rows but about 0.43% of linked rows.
   - **Top-10 manufacturers** are over-represented: 52% of all rows, about 58% of linked rows.
   - **Long-tail manufacturers** are under-represented: 15% of all rows, about 12% of linked rows.
   - Any analysis restricted to linked rows (device attributes from GUDID) inherits these skews.
2. **The gaps have different causes:**
   - The *Death*, *long-tail* and *2016–17* gaps come from **UDI availability**, meaning the MAUDE row has no UDI-DI (see L9 on where that field comes from). For these slices the match rate is at or above average. The 2016–17 gap fits the phased UDI compliance dates.
   - The *diabetes-device makers* (Insulet, Abbott Diabetes Care, Medtronic MiniMed) and their product codes have UDIs but **don't match the primary DI**. Since normalization recovers nothing, the likely cause is reports that use non-primary DIs. This is a hypothesis; PROD alone can't verify it.
   - *2026* matches less often (68% vs 81%). This fits devices being reported before they appear in the GUDID snapshot, but that is not verified.
3. **The recall→enforcement gap is mostly a coverage window, not linkage failure.** Recalls before 2012 never link and those after 2013 almost always do, which fits enforcement data starting in 2012. The low rates for "Terminated" status and the "Other"/"PMA"/"Pending" root causes are probably the same age effect, since these are older records. That confounding has not been checked by cross-tabulation.
4. **Field completeness is high in the curated sources** (enforcement and GUDID identifiers) **and uneven in MAUDE.** Lot and model numbers depend heavily on who reports. Any feature built from them will carry manufacturer-specific missingness.

## 5. Limitations

- **L1. Slices that are not available.** These exist only in RAW payloads or are not loaded at all, so they are not analysed:
  - device class
  - medical specialty / review panel
  - country
  - MAUDE report source and reporter type
  - patient sex and age
  - MAUDE `date_of_event`
  - GUDID GMDN terms and premarket submission
- **L2. Primary DI only.** GUDID PROD keeps only the primary DI, so "matches any DI" (secondary, package, unit of use) cannot be tested.
- **L3. No MAUDE/GUDID→recall link.** There is no identifier rule for it. Joining on product_code or manufacturer would be a new matching rule, so it was not measured.
- **L4. Normalization diagnostic only.** MAUDE `udi_di` is unnormalized. The shape breakdown sizes the effect (20 rows) but does not change the rule.
- **L5. Manufacturer names.** `manufacturer_name_norm` has the known `norm_mfr` suffix bugs (`docs/issue.md` #13). One firm can appear under several names, for example "nobel biocare ab" and "nobel biocare ab göteborg", which affects both manufacturer slices and tiers.
- **L6. Bucketing.** Top-25 bucketing hides the long tail inside OTHER. Tiers are ranked within each table.
- **L7. Year meaning differs by table.** MAUDE year is the date received, not the event date. Recall year is `event_date_posted`, and 22.8% of recall rows have none ("unknown").
- **L8. Causes are not verified.** The explanations in section 4 fit the data but were not checked further. Checking them needs RAW or extra sources, which were out of scope.
- **L9. Where the MAUDE UDI-DI comes from.** A 2021 JAMA Internal Medicine article reported that fewer than 21,000 (1.6%) of the roughly 1.2 million device adverse event reports FDA received in 2019 contained a UDI. In PROD, 65.7% of 2019 MAUDE rows have one. FDA added UDI-DI and UDI-Public fields to MAUDE and openFDA in November 2022, but availability shows no jump after 2022 (by `record_date` year: 2016 14.2%, 2017 35.9%, 2018 56.2%, 2019 65.7%, 2020 69.4%, 2021 63.5%, 2022 73.6%, 2023 68.9%, 2024 62.9%, 2025 66.6%, 2026 56.5%). The likely explanation is that the field was also populated for older reports from a source other than the reporter's entry. This cannot be verified from PROD. The skews in sections 3–4 are measured correctly either way; what is uncertain is *why* a row lacks a UDI (the reporter did not give one, or FDA could not fill it in).
- **L10. Residual product-code skew.** After reweighting (section 6.3), insulin-delivery product codes stay under-represented among linked rows. The cell variables do not explain this gap, so reweighting on them cannot fix it.

## 6. Mitigation implemented: reweighting the linked rows

Section 4 found that the MAUDE rows that link to GUDID are not a representative subset of MAUDE. Any analysis restricted to linked rows (for example, one that needs GUDID device attributes) inherits that skew, and so would a matcher trained on the UDI-linked pairs, which are the project's known matches. This section implements proposal 1 of section 7 and measures what it achieves and what it costs. Produced by `scripts/analysis/bias_mitigation.py` on 2026-10-27; results in `reports/bias/`.

### 6.1 Method and why it was chosen

**Inverse probability weighting with weighting cells.** Each linked row gets the weight N/L of its cell, where N is the number of MAUDE rows in the cell and L the number that link. The weighted linked rows then match all MAUDE rows on the cell variables. This is the most common correction for linkage-induced selection bias in linked administrative data (U.S. Census Bureau work on linked survey records), and weighting cells are one of the two standard ways to build such weights, alongside response-propensity models. Cells are simpler and easy to explain with 21.5 million rows.

- **Cells:** event_type × manufacturer_tier × year_group (2016–2017, 2018–2020, 2021–2023, 2024–2026, unknown). A cell with fewer than 30 linked rows uses the next coarser level (drop year_group, then tier). With 2,204 cells, 1,910 kept the full level, 170 used event_type × tier, 112 event_type, and 12 the overall rate.
- **Why these variables:** a weighting variable reduces bias without needlessly increasing variance only if it is associated with both linkage and the analysis outcome (Kreuter et al., 2010). Event type drives both linkage and safety analyses; tier and year drive linkage (sections 3.2 and 4).
- **"Linked" and the tiers** are bias_slices.py's own SQL, imported rather than copied: an exact match on the DISTINCT set of GUDID primary DIs, and top 10 / 11–100 / long tail by row count.
- **Held-out check.** Weighting by a variable restores that variable by construction, so checking it proves nothing. adverse_event_flag and product_code are known for every MAUDE row and are never used for weighting. If the weighted linked rows move toward all rows on them too, that is evidence the correction works beyond the variables it used. This is the test recommended for weighting adjustments: variables available for everyone but not used in the weights (Lohr, Riddles and Morganstein, 2016).
- **Several versions, not one:** unweighted, event_type only, the full cells, and the full cells trimmed at 5× the mean weight. Comparing sets of weights is the recommended way to choose between them, and trimming is the usual way to limit the variance that large weights add, at some cost in bias (Elliott, 2008).
- **Measures:** total variation distance (TVD) between the linked rows' shares and all rows' shares, per variable (0 = identical); Kish's effective sample size (ESS), the number of unweighted rows with the same precision; and the largest weight relative to the mean.

**Why not the fairness libraries' mitigation methods.** AIF360's Reweighing (Kamiran and Calders, 2012) and Fairlearn's mitigation algorithms adjust a classifier's training data or predictions for a label and a protected attribute. Vigil MD has no label or model yet, so they do not apply at this stage. Fairlearn's assessment API does apply (section 6.4).

### 6.2 Results

| | All MAUDE rows | Linked, unweighted | Linked, full weights | Linked, full trimmed |
|---|---:|---:|---:|---:|
| Death reports | 0.55% | 0.42% | 0.55% | 0.54% |
| Top-10 manufacturers | 52.29% | 58.42% | 52.30% | 52.33% |
| Long-tail manufacturers | 14.88% | 12.04% | 14.87% | 14.87% |

TVD from all MAUDE rows (21,532,440 rows; 10,815,717 linked, 50.23%):

| Version | event_type | manufacturer_tier | year_group | **adverse_event_flag (held out)** | **product_code (held out)** | ESS | Largest weight |
|---|---:|---:|---:|---:|---:|---:|---:|
| Unweighted | 0.026 | 0.061 | 0.061 | 0.023 | 0.144 | 100% | 1.0× |
| event_type only | 0.000 | 0.063 | 0.063 | 0.001 | 0.137 | 99.2% | 12.8× |
| Full | 0.000 | 0.000 | 0.000 | 0.0007 | 0.102 | 85.0% | 29.1× |
| Full, trimmed at 5× | 0.000 | 0.001 | 0.001 | 0.0007 | 0.102 | 85.9% | 5.0× |

1. **The correction works on a variable it never saw.** The held-out adverse_event_flag TVD falls by 97% (0.023 → 0.0007).
2. **Event type alone is not enough.** It restores event type but slightly worsens tier and year (0.061 → 0.063). Only the full cells correct all three.
3. **Product code improves only partly** (0.144 → 0.102, a 29% reduction). See 6.3.
4. **Trade-off.** The full weights keep 85% of the effective sample size: the reweighted linked rows carry the precision of about 9.2 million unweighted rows instead of 10.8 million. Trimming at 5× cuts the largest weight from 29× to 5× and gains a little precision (85.9%), at almost no cost in balance (TVD ≤ 0.001 on the weighting variables).
5. **Recommended: the full weights trimmed at 5×.** They are in `reports/bias/mitigation_weights.csv` (column `weight_full_trimmed`), one row per event_type × manufacturer_tier × year_group.

### 6.3 What reweighting cannot fix

Insulin-delivery devices stay under-represented after weighting: product code OZP (automated insulin dosing, single hormonal control; Class III) is 4.19% of MAUDE rows but 1.33% of linked rows before and 1.07% after, and LZG (insulin infusion pump) is 1.47%, 0.23% and 0.23%. FRN (infusion pump) stays over-represented (7.25%, 12.60%, 10.75%). The cell variables do not explain why these devices link poorly, so weighting on event type, tier and year cannot repair them. This is consistent with section 4's finding that diabetes-device makers report UDIs that do not match a primary DI: the cause is the matching, and the fix is proposal 3 (matching against GUDID secondary, package and unit-of-use DIs).

Adding product_code to the weighting cells would force its shares to match, but then nothing would be held out to test the weights, and it would inflate the few insulin-device rows that do link, which may be atypical. The held-out design was kept. Product codes with no linked row at all cover 0.044% of MAUDE rows; no reweighting can represent them.

### 6.4 Fairlearn assessment and cross-check

Fairlearn's `MetricFrame` computes the MAUDE→GUDID link rate per slice for event_type, manufacturer_tier, year_group, adverse_event_flag and product_code, with `difference()` and `ratio()` for each (in `mitigation_summary.json`). The link flag is passed as `y_pred` to `selection_rate`, which ignores `y_true`. Each cell enters as two rows (linked and not linked) weighted by their counts through `sample_params`, so the rates are exact over all 21.5 million rows rather than estimated from a sample.

Cross-check: on the 40 slice values that both report, Fairlearn's rates and bias_slices.py's `link_coverage.csv` differ by at most 5 × 10⁻⁷, the rounding of the CSV. Two independent implementations give the same link rates.

### 6.5 Assumption and limits

- **Linkage is assumed ignorable within a cell:** inside each event_type × tier × year_group cell, whether a row links must not depend on anything else that matters for the analysis. The product-code result (6.3) shows this is only partly true. Variables that are not in PROD (device class, reporter type, patient characteristics; L1) cannot be checked.
- **The weights correct representation, not linkage itself.** Unlinked rows still have no GUDID attributes. The weights make estimates from linked rows represent all MAUDE rows on the cell variables.
- **Use for the matcher.** When training or labelling pairs are drawn from UDI-linked records, sample them with `weight_full_trimmed` (or stratify by the same cells) so the long tail, Death reports and older years are represented as in MAUDE as a whole.

## 7. Further mitigations (proposed, not implemented)

These follow from the results above. Item 1 is now implemented (section 6); the others have not been built. Items marked † would need RAW access, a new table, or a change to a matching rule, so each needs its own approval.

1. **Don't treat linked-only results as population results.** Report downstream metrics stratified by event_type, manufacturer tier and year. Where linked rows are used for estimates, reweight them by inverse link probability per slice, using the rates in `link_coverage.csv`. *Implemented in section 6.*
2. **Keep Death reports in signal work without linking.** For Death and long-tail reports, use MAUDE's own product_code and manufacturer features instead of dropping unlinked rows.
3. **† Test the non-primary-DI hypothesis.** For the diabetes makers, match unmatched UDIs against GUDID secondary, package and unit-of-use DIs. If confirmed, extend the DI set used for matching. This is the biggest recoverable gap; trimming or prefix normalization is not worth doing, since it recovers 20 rows.
4. **Check the 2026 dip.** Re-run this analysis after the next GUDID load. If the 2026 match rate rises, it was snapshot lag.
5. **Label the enforcement coverage window.** Mark recalls before 2012 as "no enforcement source" rather than "unlinked", or restrict recall↔enforcement analyses to 2013 onward.
6. **† Fix `norm_mfr` first** (`docs/issue.md` #13) before drawing conclusions about individual manufacturers.
7. **† Load the FDA product classification table** to unlock the device class and review panel slices.
8. **Make this a recurring check.** Run `bias_slices.py` after each full load and alert when a monitored rate (overall end-to-end, Death availability, long-tail availability) moves by more than 5 pp.

## Reproduce

```bash
python scripts/analysis/bias_slices.py --project vigil-md-1792696816 --print-only     # SQL only
python scripts/analysis/bias_slices.py --project vigil-md-1792696816 --estimate-only  # free dry runs
python scripts/analysis/bias_slices.py --project vigil-md-1792696816 --max-gb 5
```

`completeness_maude` scans about 3.9 GB, which is above the default `--max-gb 2`.

Mitigation (section 6), one read-only query of about 1.3 GB, results in `reports/bias/`:

```bash
python scripts/analysis/bias_mitigation.py --project vigil-md-1792696816 --estimate-only
python scripts/analysis/bias_mitigation.py --project vigil-md-1792696816
# recompute from the saved cell counts, no BigQuery:
python scripts/analysis/bias_mitigation.py --project vigil-md-1792696816 --from-cells data/interim/bias/mitigation_cells.csv
```

Run `bias_slices.py` first: the Fairlearn cross-check reads its `link_coverage.csv`.

## References

- Elliott, M. R. (2008). Model averaging methods for weight trimming. *Journal of Official Statistics*, 24(4), 517–540.
- FDA (November 2022). FDA updates fields available in the Manufacturer and User Facility Device Experience (MAUDE) database and openFDA (UDI-DI and UDI-Public).
- Fairlearn documentation: `fairlearn.metrics.MetricFrame`, advanced usage (per-sample arguments and `selection_rate`).
- Harron, K. et al. (2017). A guide to evaluating linkage quality for the analysis of linked data. *International Journal of Epidemiology*, 46(5), 1699–1710.
- Kamiran, F. and Calders, T. (2012). Data preprocessing techniques for classification without discrimination. *Knowledge and Information Systems*, 33, 1–33.
- Kish, L. (1965). *Survey Sampling*. Wiley (effective sample size).
- Kreuter, F. et al. (2010). Using proxy measures and other correlates of survey outcomes to adjust for non-response. *Journal of the Royal Statistical Society, Series A*, 173(2).
- Lohr, S. L., Riddles, M. K. and Morganstein, D. (2016). Tests for evaluating nonresponse bias in surveys. *Survey Methodology*, 42(2).
- Raze, K., Perales, N. et al. (2024). Non-random assignment of individual identifiers and selection into linked data. FCSM Research and Policy Conference (U.S. Census Bureau).
- Tracking medical devices with the US Food and Drug Administration's Unique Device Identification System (2021). *JAMA Internal Medicine*.
