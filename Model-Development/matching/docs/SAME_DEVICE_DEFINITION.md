# What counts as the "same device"

**Status:** proposed, for team sign-off (Day 1 of the 3-day model plan).
**Owner:** Hitika. **Data:** `vigil_md_prod` snapshot of 2026-11-06.
**Evidence:** every number below comes from a query in [`../sql/same_device/`](../sql/same_device/), and the
results are in [`../reports/same_device/RESULTS.md`](../reports/same_device/RESULTS.md). The query is cited as `Q05` and so on.

Linking a MAUDE device to the wrong GUDID device gives it another device's recall history. The definition
decides what a correct link is. That one definition sets:
- the positive class for training labels
- the hand-labeled MATCH / NON_MATCH / INSUFFICIENT_EVIDENCE rules
- what "99% precision" is measured against

---

## 1. Definition

> A MAUDE device row (`mdr_report_key`, `device_seq`) and a GUDID record (primary DI) are the
> **same device** when they name the **same model or version, made by the same labeler or its
> corporate family**. The test is this: the GUDID record's regulatory and recall history applies
> to the device in the report.

In practice, all three of these must hold for **MATCH**:

1. **Same labeler family.** The normalized manufacturer names are equal, or the pair is in the
   alias table (§4). If the names differ and there is no alias entry, a MATCH needs a
   distinctive identifier agreement *and* product-code agreement.
2. **A distinctive identifier agrees.** After normalization, a MAUDE identifier (model *or*
   catalog number) equals a GUDID identifier (model *or* catalog number). It may also appear in
   the other field as a whole token. An identifier is *distinctive* if it is not a placeholder
   and the model key is shared by at most 20 GUDID DIs of that labeler (§2, E4).
3. **No conflicting identifier.** No other GUDID record of the same labeler matches the MAUDE
   identifier more exactly. For example, if MAUDE says GP200S and GUDID has a GP200S, then the
   pair with GP200 is NON_MATCH (E3).

**Granularity: model level, not DI level (decision for sign-off).** Size, length, quantity and
packaging variants of one model count as the same device. A link to any DI in that model group
is correct.
- **Why:** 86.5% of GUDID DIs are the only DI for their (labeler, model) (Q05), so the two
  levels agree for most devices.
- **Why:** For the other 13.5%, MAUDE rarely carries enough detail (size, pack count) to choose
  one DI, and recalls are normally issued per model or catalog list.
- **Strict DI level is the alternative.** It would turn most no-UDI rows with a multi-DI model
  into INSUFFICIENT_EVIDENCE.

## 2. Label rules (hand-labeled set and error tagging)

| Label | When |
|---|---|
| **MATCH** | All three conditions in §1 hold. |
| **NON_MATCH** | Both sides have usable identifiers and they conflict: different model, a suffix or revision variant (E3), or a kit vs. a component with different identifiers (E9). Also NON_MATCH: labeler families clearly differ and no identifier agrees. |
| **INSUFFICIENT_EVIDENCE** | MAUDE has no usable identifier (missing or placeholder, E6) and only the brand or device name agrees. Also: the only identifier is non-distinctive (E4) and no catalog number resolves it, or the only relation is a prefix (E3b). |

A device-name match alone is never MATCH: device names are generic ("Medline Industries, Inc.",
"Kyocera Medical Technologies, Inc." in Q05). A product-code mismatch alone is never NON_MATCH
(E8).

## 3. Edge cases

### E1. Same primary DI → MATCH (with a noise filter for training)
- **Evidence:**
  - Every GUDID primary DI is unique in prod: 5,258,174 DIs, 0 with more than one record (Q04).
  - Of 10.82M UDI-linked MAUDE rows, 96.35% are consistent with the link: an identifier agrees
    (91.9%), the identifiers relate by prefix or containment (3.2%), or MAUDE has no usable model
    number to compare (1.2%) (Q08).
  - **3.65% (394,870 rows) truly conflict.**
- **Conflict examples (Q08 `conflict_examples`):**
  - MDR 6595018: MAUDE "RESTORE", model `37711` vs. GUDID `97714` ("RestoreSensor SureScan").
  - MDR 50665629: Zoll LifeVest *system* vs. GUDID *electrode belt*.
- **Rule:** in the hand-labeled set a matching primary DI is MATCH. **For training labels**,
  rows in `conflict` are dropped from the positives. They are not flipped to negatives; they are
  kept as a review sample. Many are component vs. system reports, where the reporter entered the
  UDI of a different part.

### E2. Formatting-only difference (IP-200 vs IP200, `OLYMPUS CV-190` vs `CV-190`) → MATCH
- **Evidence:**
  - Inside GUDID, only 0.15% of model keys (6,918 of 4.73M) have more than one raw spelling
    within a labeler (Q06).
  - Nearly all are the same product: `5925-040-12` / `592504012` (OrbusNeich), `CL2078-BLUE` /
    `CL2078BLUE` (Steris).
  - On UDI-linked rows, 0.95% relate only by *containment* (a manufacturer prefix or a name
    around the model), e.g. MAUDE `GF-UCT180` vs. GUDID `OLYMPUS GF TYPE UCT180` (Q08).
- **Rule:** the model key is `UPPER`, with spaces and `- _ . /` stripped. A key that appears as a
  whole token inside the other field also counts.
- **Known exception:** Sontec `142-200` and `14-2200` are two different forceps with different
  product codes, yet they collapse to the same key (Q06). The hyphen position carried meaning.
  The shared feature function therefore keeps a `raw_identifier_equal` flag next to the
  normalized match, so the model can tell these apart.

### E3. Suffix or revision variant (GP200 vs GP200S) → NON_MATCH
- **Evidence:**
  - GUDID has **516,170** pairs of model keys within one labeler where one key is the other plus
    1-3 characters, across 3,506 labelers (Q07).
  - `S` alone accounts for 55,396 pairs.
  - Examples: Globus `394.126C` / `394.126CS`, Medline `DYNJ85712` / `DYNJ85712A`, Juzo
    `…FL14 II` / `…FL14 III`.
  - These are separately registered devices.
- **Rule (E3a):** if GUDID has a record that equals the MAUDE identifier exactly, every suffix
  sibling of it is NON_MATCH.
- **E3b, a bare prefix:** MAUDE gives the stem (`GP200`) and GUDID has only longer variants.
  This case is **INSUFFICIENT_EVIDENCE**, not NON_MATCH: 2.3% of UDI-confirmed links relate only
  by prefix (Q08).
- This is the conflicting-identifier feature in the plan. Q07 is also a ready source of hard
  negatives.

### E4. Same model, many DIs → MATCH at model level, unless the "model" is not an identifier
- **Evidence (Q05):** 13.5% of GUDID DIs share their (labeler, model key) with other DIs:
  - 9.6% are in groups of 2-5
  - 1.6% are in groups of more than 100
- **Small groups** are size or pack variants with the same catalog stem (Q05 `typical`).
- **The largest groups are not models at all.** The labeler put a family, revision or release
  label in the model field:
  - `REV01`: Brosmed, 3,956 DIs
  - `Initial Release`: Merit, 1,401 DIs
  - `Perfusion Pack`: LivaNova, 5,006 DIs
  - `COLOR`: Aidite
- **Rule:** a model key shared by **more than 20 DIs** of one labeler is *non-distinctive*. It
  cannot support a MATCH alone; the catalog number must agree. Without a catalog number the pair
  is INSUFFICIENT_EVIDENCE. The threshold of 20 is the 6-20 / 21-100 bucket edge in Q05 and is
  open for sign-off.

### E5. Manufacturer alias or acquisition → MATCH when an identifier agrees
- **Evidence:** on UDI-confirmed links, **43.8%** of rows have different normalized manufacturer
  names (Q10). There are 5,484 distinct name pairs. The top ones:
  - `carefusion sd` → `carefusion 303` (1.29M rows)
  - `medtronic puerto rico operations` → `medtronic minimed`
  - `abbott medical` → `st. jude medical` (acquisition)
  - `smiths medical asd` → `icu medical` (acquisition)
  - `zimmer dental` → `zimvie us corp`
- **Rule:** exact manufacturer equality cannot be required. An alias table is built from
  UDI-linked name pairs (Q10 `top_pairs`), **from the train split only**, so test devices do not
  leak through it. An alias plus an identifier agreement is MATCH.

### E6. Placeholder or missing identifier → treat as missing
- **Evidence:**
  - Placeholders among the most common MAUDE model values: `NA` (525,440 rows), `N/A`
    (255,429), `NI` (56,066), `UNK` (30,680) (Q03).
  - Catalog numbers show the same pattern, plus `UNKNOWN` (87,538).
  - **36.7% of the no-UDI pool has no usable model number (Q14).**
  - GUDID uses placeholders too: Heron's model `N/A` covers 8 DIs (Q06).
- **Rule:** keys under 2 characters, or matching the placeholder pattern in the
  [README](../README.md#shared-conventions), are missing on both sides. `NA` = `NA` is never an
  identifier match. If no other identifier exists, the pair is INSUFFICIENT_EVIDENCE.

### E7. Model and catalog fields swapped → MATCH
- **Evidence:** on UDI-confirmed links, model = model holds for only **51.6%** of rows (Q08).
  The other identifier agreements are cross-field:
  - **30.1%**: MAUDE *catalog* = GUDID *model* (e.g. Tandem, MAUDE catalog `1006402` = GUDID
    model)
  - 8.0%: catalog = catalog
  - 2.3%: MAUDE model = GUDID catalog
- **Rule:** compare every MAUDE identifier against every GUDID identifier. A model-only
  comparison would miss about 40% of true links.

### E8. Product-code conflict → soft evidence, not a rule
- **Evidence:** on UDI-confirmed links, product codes agree for 94.7% of rows and differ for
  5.3% (Q12).
- The top disagreements are related codes for one device family: `PQF` vs `MDS` (insulin
  pumps), `QFG` vs `OZO`, `OYC` vs `MDS`.
- **Rule:** a product-code mismatch is a feature. It is NON_MATCH only together with an
  identifier conflict.

### E9. Kit or system vs. component → the identifier decides
- **Evidence:** MAUDE device names say "system", "kit" and so on for 22.3% of UDI-linked rows,
  while GUDID uses those words for 10.4% (Q13). Example: "DEXCOM G6 CONTINUOUS GLUCOSE MONITORING
  SYSTEM" in MAUDE vs. "US G6 Transmitter" in GUDID.
- So "system" in a MAUDE name does not mean the report is about a kit.
- **Rule:** if the MAUDE identifier names the component, MATCH the component. A kit DI and a
  component DI with different identifiers are NON_MATCH (see the LifeVest example in E1).

### E10. MAUDE UDI that is not a GUDID primary DI → not a label; treat as no-UDI
- **Evidence:**
  - **2,503,355** MAUDE rows carry a well-formed UDI that is not any GUDID primary DI (Q09).
  - That is 23.4% of the no-link pool (Q14).
  - Only 20 rows would match after fixing the format.
  - They are most likely package or secondary DIs, which prod GUDID does not keep.
- **Rule:** these rows are not training positives. They go into the no-UDI pool. Loading GUDID
  secondary and package DIs would turn many of them into labels (open question 5).

### E11. Multi-device reports → the pair key includes `device_seq`
- **Evidence:** 0.12% of reports list more than one device; 738 reports list 5 or more (Q13).
- **Rule:** the pair key is (`mdr_report_key`, `device_seq`, GUDID `udi_di`). One report can
  have a different correct link per device.

## 4. Normalization the shared feature function must implement

The Day 1 shared feature function is used by every track.

| Field | Rule | Source |
|---|---|---|
| Manufacturer | `manufacturer_name_norm` from prod (issue #13 fixed), then the train-split alias table | E5 |
| Identifier key | `UPPER(REGEXP_REPLACE(TRIM(x), r'[\s\-_./]', ''))` | E2 |
| Placeholder | key under 2 characters or matches the README pattern → NULL | E6 |
| Identifier match | any of model/catalog × model/catalog, exact key or whole-token containment | E2, E7 |
| Conflicting identifier | GUDID has a sibling key that equals the MAUDE key while this candidate differs only by a 1-3 character suffix | E3 |
| Distinctive | the model key has at most 20 DIs for that labeler in GUDID | E4 |
| Raw equality flag | raw trimmed strings equal (guards the E2 hyphen collisions) | E2 |

## 5. What this means for the rest of Day 1

- **Blocking must not use exact (manufacturer, model).**
  - Of no-UDI rows with a usable model number, **83.3% have no exact (manufacturer, model key)
    candidate in GUDID** (Q14).
  - The reasons are aliases (E5) and identifiers sitting in the catalog field (E7).
  - Block on the alias-mapped labeler plus any identifier key across fields.
- **Training positives:** UDI-linked rows, minus `conflict` (E1) and minus non-distinctive-only
  matches (E4).
- **Hard negatives:**
  - suffix siblings (Q07)
  - same labeler with a different model in the same product code
- **The data is concentrated.**
  - 21.5M MAUDE rows carry only 194,197 distinct UDI-DIs (Q01).
  - Single model values dominate: `8100` (Alaris) has 994K rows and `1002717` (Tandem) has 704K
    (Q03).
  - Split by device (planned), and deduplicate pairs to unique (MAUDE identifier tuple, DI)
    before training and scoring. Otherwise a few devices dominate every metric.
- **Hand-labeled test set strata (Q14)**, all from the no-UDI pool, which is 76.6% no UDI and
  23.4% UDI-not-primary:
  - model number usable or not (63% / 37%)
  - manufacturer size (top 10 = 46%, 11-100 = 36%, smaller = 18%)
  - 0 / 1 / 2+ exact candidates
  - Expect many INSUFFICIENT_EVIDENCE labels from the 37% without a usable model number.
- **Dates:** prod MAUDE covers 2016-01-01 to 2026-09-30 (Q01). There is no pre-2016 slice.

## 6. Open questions for sign-off

1. **Model level vs. DI level** (§1). The recommendation is model level.
2. **Non-distinctive threshold** of 20 DIs per model key (E4).
3. **Alias table:** built automatically from train-split UDI links, with a minimum DI count per
   pair (proposed: at least 3 DIs) to keep one-off mis-entries out.
4. **Hyphen-position collisions** (E2): accept them as a rare risk, with the raw-equality flag as
   the guard.
5. **GUDID secondary and package DIs:** ask the pipeline owners to load them. This could turn up
   to 2.5M no-label rows into UDI-labeled rows (E10).
6. **Who labels and by when** (risk 4 in the plan, deadline D7). The second labeler checks
   agreement on E3b, E4 and E9, the cases most likely to split opinions.
