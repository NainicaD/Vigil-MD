# Hand-label guide (Day 1, N6)

**Owner:** Nandita. **Rules come from** [`SAME_DEVICE_DEFINITION.md`](SAME_DEVICE_DEFINITION.md) sections 1 to 3.
This guide only says how to apply them to the sheet. If the two ever disagree, the definition wins,
and the disagreement is reported so one of them is fixed.

**Sheet:** `labels/hand_label_sample_v1.csv`, made by `pipeline/s02_build_hand_label_sheet.py`. Batch 1 is 150
MAUDE rows from the test split. Do not sort or delete rows in the file you hand back; filter views are fine.

---

## 1. What one question looks like

Every MAUDE row (one device in one adverse-event report) appears as **1 + k sheet rows**, all with the
same `maude_row_id`:

| `cand_rank` | What it is |
|---|---|
| `0` | The statement **"none of these candidates is the same device"**. Present for every MAUDE row. |
| `1` to `3` | GUDID candidates, best rule first (`cand_rule`). There may be none. |

You label **every** row of the question, including rank 0.

The left part of each row is the MAUDE side (`maude_*`), the right part the candidate (`cand_*`).
The candidate is one GUDID record standing for its whole model group (§1 "model level"):
`cand_n_dis_for_model` says how many DIs share its labeler and model key.

**Helper columns are hints, not answers:** `cand_match_kind`, `cand_is_distinctive`,
`cand_has_suffix_sibling`, `cand_raw_identifier_equal`, `cand_rule`, `hard_case_reason`. They come from
the same rules as the definition, so they are usually right, but you decide. If a hint is clearly wrong,
say so in `notes`. That is how we find bugs in the rules.

**Do not use** `cand_split`, `meta_sample_weight` or `stratum_id` to decide anything. `maude_udi_di_raw`
(filled only when the report carries a UDI that is not a GUDID primary DI) **is** evidence: if it matches
the candidate's package or secondary DI on AccessGUDID, that is strong support for MATCH.

## 2. The columns you fill in

| Column | Values |
|---|---|
| `label` | `MATCH`, `NON_MATCH` or `INSUFFICIENT_EVIDENCE`. Nothing else: the validator rejects any other value. |
| `labeler` | Your first name, lower case (`hitika`, `nandita`). |
| `labeled_at` | Date, `YYYY-MM-DD`. |
| `confidence` | `high`, `medium` or `low` (see §5). |
| `rule_cited` | The edge case(s) that decided it: `E1` to `E11`, several separated by `;`. Use `DEF` when only the §1 definition was needed. |
| `notes` | Free text. Required when `confidence` is `low`, when you overrule a hint, or when you used AccessGUDID or openFDA to decide. |

## 3. Decision tree for a candidate row (`cand_rank` 1 to 3)

Work through it top to bottom and stop at the first answer.

1. **Does the MAUDE row have a usable identifier?** A model or catalog number that is not empty and not a
   placeholder (`NA`, `N/A`, `NI`, `UNK`, `UNKNOWN`, `0000`, `XXX`, or under 2 characters) (E6).
   - **No** → only the name or brand can agree, and a name alone is never MATCH →
     **INSUFFICIENT_EVIDENCE**, unless the labelers clearly differ and nothing agrees → **NON_MATCH**.
2. **Do the identifiers agree?** Compare every MAUDE identifier with every candidate identifier, model
   and catalog in both directions (E7). Formatting does not count: `IP-200` = `IP200`,
   `5925-040-12` = `592504012` (E2). A key that appears as a whole word inside the other field counts
   (`GF-UCT180` inside `OLYMPUS GF TYPE UCT180`).
   - **They conflict** (different model, no relation) → **NON_MATCH**. Go to step 5 only to check the
     labeler; a conflict is NON_MATCH either way.
   - **Only a prefix relation**, and MAUDE gives the shorter stem (`GP200` vs a candidate `GP200S`,
     and GUDID has no plain `GP200`) → **INSUFFICIENT_EVIDENCE** (E3b).
   - **They agree** → step 3.
3. **Is there a more exact sibling?** If GUDID has a record equal to the MAUDE identifier, and this
   candidate is only that plus 1 to 3 characters (`GP200` in MAUDE, candidate `GP200S`), this candidate
   is **NON_MATCH** (E3a). `cand_has_suffix_sibling = TRUE` is the hint. A size or pack variant of the
   **same** model (same model key, different DI) is not a sibling: it is the same device (§1).
4. **Is the agreeing identifier distinctive?** A model key shared by more than 20 DIs of the labeler is
   a family or release label, not a model (`REV01`, `Initial Release`, `Perfusion Pack`)
   (`cand_is_distinctive = FALSE`) (E4).
   - **Not distinctive**, and no catalog number agrees → **INSUFFICIENT_EVIDENCE**.
   - **Not distinctive**, but a catalog number agrees → continue.
5. **Is it the same labeler family?** Same normalized name, or a known alias or acquisition
   (`carefusion sd` / `carefusion 303`, `abbott medical` / `st. jude medical`,
   `medtronic puerto rico operations` / `medtronic`) (E5).
   - **Yes** → **MATCH**.
   - **Names differ with no known alias** → **MATCH** only if the identifier is distinctive **and** the
     product codes agree; otherwise **INSUFFICIENT_EVIDENCE**.
6. **Kit or component?** (E9) If MAUDE's identifier names a component and the candidate is that
   component → MATCH. A kit DI and a component DI with different identifiers → **NON_MATCH**.
   The word "system" in a MAUDE device name does not by itself mean a kit.

Product codes are soft evidence (E8): a mismatch alone is never NON_MATCH, and a match alone is never
MATCH.

## 4. The rank-0 row ("none of these is the same device")

Label it **after** the candidate rows of the question:

| Candidate rows | Label the rank-0 row |
|---|---|
| At least one is `MATCH` | `NON_MATCH` (the statement "none of these" is false) |
| None is `MATCH`, the MAUDE row has a usable identifier, and every candidate is `NON_MATCH` (or there are no candidates) | `MATCH` ("none of these" is true) |
| Anything else (some candidate is `INSUFFICIENT_EVIDENCE`, or MAUDE has no usable identifier) | `INSUFFICIENT_EVIDENCE` |

`MATCH` on rank 0 says that **none of the listed candidates** is the device. It does not say the device
is missing from GUDID: the true device may exist under a form no rule found. If you can find it in
AccessGUDID, put its DI and model in `notes`; it is useful for the retrieval track.

Two candidate rows of the same question should not both be `MATCH` unless they are the same model
(different DIs of one model group). If they are, label both `MATCH` and say so in `notes`.

## 5. Confidence

| Value | When |
|---|---|
| `high` | The identifiers settle it, with no hint you disagree with. |
| `medium` | You needed a judgement on one point: an alias you had to look up, a kit wording, a non-distinctive key resolved by catalog. |
| `low` | You are guessing between two labels. Write both in `notes` ("MATCH or IE: ..."). These are reviewed together. |

## 6. Worked examples, one per edge case

Labels are for the candidate row shown. Examples come from `SAME_DEVICE_DEFINITION.md` where it gives
one; rows marked * are illustrations built from its patterns, not real records.

| Case | MAUDE | Candidate | Label | `rule_cited` |
|---|---|---|---|---|
| E1 (UDI) * | carries a UDI that AccessGUDID lists as a package DI of the candidate | same labeler, model agrees | MATCH | `E1` |
| E2 formatting | model `IP-200` | model `IP200`, same labeler | MATCH | `E2` |
| E2 exception | Sontec `142-200` | Sontec `14-2200` (same key after stripping, different product codes) | NON_MATCH; `cand_raw_identifier_equal` is FALSE | `E2` |
| E3a suffix | model `GP200`, and GUDID has a `GP200` | candidate `GP200S` | NON_MATCH | `E3` |
| E3b prefix only | model `GP200`, GUDID has only `GP200S` and `GP200L` | candidate `GP200S` | INSUFFICIENT_EVIDENCE | `E3b` |
| E4 non-distinctive | model `REV01`, no catalog | Brosmed `REV01` (3,956 DIs) | INSUFFICIENT_EVIDENCE | `E4` |
| E4 resolved * | model `REV01`, catalog `BR-2207` | Brosmed `REV01`, catalog `BR2207` | MATCH | `E4;E7` |
| E5 alias * | labeler `carefusion sd`, model `8100` | `carefusion 303`, model `8100` | MATCH | `E5` |
| E6 placeholder * | model `NA`, catalog empty, brand `FOGARTY` | Edwards `FOGARTY` `120403F` | INSUFFICIENT_EVIDENCE | `E6` |
| E7 cross-field | catalog `1006402` (model empty) | Tandem model `1006402` | MATCH | `E7` |
| E8 product code | model agrees, labeler same, codes `PQF` vs `MDS` (both insulin pumps) | | MATCH | `E8` |
| E9 kit | Zoll, MAUDE describes the LifeVest **system**, identifier of the system | candidate is the electrode **belt**, different identifier | NON_MATCH | `E9` |
| E11 multi-device * | report lists 2 devices; this row is `device_seq` 1 | | label only this device; the other is its own row | `E11` |

E10 (a UDI that is not a GUDID primary DI) is why `maude_udi_di_raw` exists: look it up (row 1 of the
table above).

## 7. Tools

- `accessgudid_url` opens the candidate in AccessGUDID: all its DIs (primary, package, secondary),
  catalog numbers and description.
- `openfda_url` opens the MAUDE report in openFDA, including the narrative, which often names the exact
  model or component.
- Use them when the sheet is not enough, and say so in `notes`.

## 8. Process

1. **Pilot (Hitika):** label all 352 rows of batch 1. Stop and review this guide against what came up
   **before** batch 2 is drawn. That is the point of stopping at 150.
2. **Second labeler (Nandita):** label a copy of the **hard** rows only (`is_hard_case = TRUE`, 52 MAUDE
   rows; the plan asks for about 40, E3b, E4 and E9 first) without looking at the first labels. Save it as
   `labels/hand_label_sample_v1.labeler2.csv`, same columns.
3. **Agreement:** Cohen's kappa on `label` over the rows both labeled, overall and for E3b, E4 and E9.
   Every disagreement is discussed; the agreed label goes into the main sheet with the reason in
   `notes`, and anything the guide did not cover goes back into the definition as a clarification.
4. Commit the labeled sheet. It is small public FDA data and must stay diffable, so keep it as CSV.

## 9. How the labels are used (so you know why the columns matter)

- **Precision and recall of a matcher:** candidate rows labeled MATCH or NON_MATCH. INSUFFICIENT_EVIDENCE
  rows are left out of both and reported as a count.
- **Pool-level numbers:** every rate is weighted by `meta_sample_weight` (rows in the stratum / rows
  drawn), because hard cases are over-sampled on purpose. An unweighted rate describes the sample, not
  the no-UDI pool.
- **Retrieval recall:** of the MAUDE rows whose rank-0 row is NON_MATCH (a true candidate exists), the
  share where a method ranks that candidate first or in its top k.
- **Strict test numbers:** filter to `cand_split = test` to measure only devices no training pair used.
