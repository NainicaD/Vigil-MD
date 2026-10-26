# Bias slice analysis of the PROD tables

Run on 2026-10-26 against `vigil-md-1792696816.vigil_md_prod`. Produced by `scripts/analysis/bias_slices.py`. The results are in `data/interim/bias/`, which is git-ignored, so re-run the script to regenerate them.

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
   - The *Death*, *long-tail* and *2016–17* gaps come from **UDI availability**, meaning reporters didn't supply a UDI. For these slices the match rate is at or above average. The 2016–17 gap fits the phased UDI compliance dates.
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

## 6. Proposed mitigations (not implemented)

These follow from the results above. None of them has been built. Items marked † would need RAW access, a new table, or a change to a matching rule, so each needs its own approval.

1. **Don't treat linked-only results as population results.** Report downstream metrics stratified by event_type, manufacturer tier and year. Where linked rows are used for estimates, reweight them by inverse link probability per slice, using the rates in `link_coverage.csv`.
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
