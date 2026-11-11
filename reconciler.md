# 🌌 ICICI-GL Reconciliation Engine: Core Logic & Architecture

Welcome to the technical blueprint of the **ICICI-GL Reconciliation Engine (Pro Edition)**. This high-performance engine is designed for surgical precision and low-latency matching, handling 90,000+ complex transactions, unstructured banking narrations, UPI VPA mask resolution, and multi-layered reference bridging.

---

## 🛠️ 1. High-Level System Architecture

The engine uses a modular **Data-Pipe Architecture** consisting of five specialized stages: Ingestion & Enrichment, Transitive Bridging, Mask-Aware UPI Resolution, Vectorized Tiered Matching, and Double-Entry Persistence.

```mermaid
flowchart TD
    %% Ingestion
    subgraph Ingestion ["1. Ingestion & Pre-Processing"]
        BS["Bank Statement (.psv)"] -->|Parse & Clean| INGEST["Data Ingestion Hub"]
        GL["General Ledger (.xml)"] -->|Parse & Clean| INGEST
        RTGS["RTGS Reports (.xml)"] -->|Bridge Scraped| INGEST
        CMS["CMS Reports (.xls/.tsv)"] -->|Bridge Scraped| INGEST
        UPI["InstaBIZ UPI Reports (.csv/.xls)"] -->|Header Detect & Filter| INGEST
    end

    %% Bridging & Enrichment
    subgraph Bridging ["2. Transitive Bridging & Enrichment"]
        INGEST -->|Extract Refs| EXT["Surgical Regex Extractor"]
        EXT -->|Map CRN/Serials| BRIDGE["RTGS/CMS Bridge Engine"]
        INGEST -->|RRN / Gateway Key| UPI_ENRICH["InstaBIZ UPI Enricher"]
        BRIDGE --> GL_ENRICHED[("Enriched GL DataFrame")]
        UPI_ENRICH --> BANK_ENRICHED[("Enriched Bank DataFrame")]
    end

    %% Matching Engine
    subgraph Engine ["3. Vectorized Multi-Tier Matching Engine"]
        GL_ENRICHED & BANK_ENRICHED --> TIERS["14 Prioritized Matching Tiers"]
        TIERS --> T1["Tier 1 / 1.1 (Exact & CMS Bridge)"]
        TIERS --> T2["Tier 2 / 2.1 / 2.5 / 2.8 (UTR, Fuzzy, Cheque, Year-End)"]
        TIERS --> T3["Tier 3 / 3.2 (Exact Date & Mask-Aware VPA)"]
        TIERS --> T4["Tier 4 / 5 / 6 (Fuzzy Date, Deep Narrative, 1-to-N Groups)"]
        TIERS --> OFFSETS["Internal Ledger Offsets Scanner"]
    end

    %% Persistence & UI
    subgraph Persistence ["4. Persistence, Double-Entry Guard & UI"]
        TIERS --> OUT[("data.json (Reconciliation Cache)")]
        ML["manual_links.json"] -->|Inject Manual Overrides| TIERS
        OUT --> API["Flask REST API"]
        API --> UI["Pro UI (Sender Tracking, Sticky Notes, Glow Guidance)"]
    end
```

---

## 🔍 2. Surgical Reference Ingestion & Extraction

Before any matches are compared, the ingestion pipeline cleans and extracts references from unstructured narrations using an anchored Regex engine with strict digit boundary guards.

### Anchored 12-Digit UTR Guard
```python
# Guarded against concatenated digits and scientific notation
REGEX_12DIGIT = re.compile(r'(?<!\d)\d{12}(?!\d)')
```
- **Digit Boundary Guards**: `(?<!\d)\d{12}(?!\d)` prevents false extractions from longer account numbers, timestamps, or phone numbers.
- **Cheque Sequences**: Supports `_`, `#`, `-`, and CTS clearing markers for 6-to-10 digit cheque numbers.
- **Indic Phonetic Normalization**: Normalizes Malayalam/Indian transliteration variations (`th/t`, `sh/s`, `ee/i`, `oo/u`) to prevent narrative false negatives.

---

## 🌉 3. The Transitive Bridge Mechanism (Solving the Reference Gap)

In enterprise accounting, a GL transaction is frequently logged using an internal Payment Serial Number, whereas the bank statement records the transaction with a Global UTR. They share no direct text alignment. The engine bridges this gap by cross-referencing auxiliary reports:

```
       [ General Ledger Entry ]
                  │
                  ▼ (Has Serial No: 270326100418)
    ┌───────────────────────────┐
    │  A. RTGS XML Outward Rep  │ ───► Maps Serial 270326100418 to CRN: CMS5605778710
    └───────────────────────────┘
                  │
                  ▼ (Matched via CRN)
    ┌───────────────────────────┐
    │   B. CMS Excel Report     │ ───► Maps CRN to Customer Ref: 914549257
    └───────────────────────────┘
                  │
                  ▼ (Bridge Enriches GL Row's References)
        [ Enriched GL References ] ◄─── Contains [270326100418, 914549257]
                  │
                  ├───► [ Perfect Match via Overlapping Ref '914549257'! ]
                  │
        [ Bank Narration Row ] ◄────── Contains CMS/001914073925/914549257
```

- **Inward RTGS CBS Bridging**: For inward remittances lacking outward batch serials, the engine cross-references Inward RTGS reports by `(TO_CBS_ACCNO, amount, date)` tuple to link the bank statement entry directly to the GL account.

---

## 💳 4. Multi-Schema UPI Engine & InstaBIZ Mask-Aware Ingestion

To address the September 2026 NPCI UPI VPA masking mandate (enforced under the Digital Personal Data Protection Act 2023), the engine incorporates a multi-schema parser and automated InstaBIZ collection report ingestion.

### 1. Multi-Schema UPI Parser (`parse_upi_narration`)
Standard ICICI UPI narrations follow a 6-part schema, but remitter banks often send extended 7-part schemas where the payer name precedes the VPA:
- **7-Part Remitter Schema**: `UPI / <Payer Name> / <VPA Handle> / <Marker> / <Remitter Bank> / <12-Digit UTR> / <Gateway Ref>`
- **6-Part ICICI Schema**: `UPI / <12-Digit UTR> / <Remark> / <VPA Handle> / <Remitter Bank> / <Gateway Ref>`
- **Bank Noise Shield**: Strictly excludes bank tokens (`PUNJAB`, `STATE BANK`, `CANARA`, `IDFC`, `FEDERAL`, `UNION`, etc.) from polluting VPA or party fields.

### 2. InstaBIZ Collection Report Ingestion (`load_upi_collection_reports`)
- Scans `Data/<FY>/UPI Collection Report/` for CSV, TSV, XLS, and XLSX exports.
- Dynamically locates table headers (`BANK RRN`, `CUSTOMER VPA`, `PAYER NAME`, `TRANSACTION AMOUNT`, `TRANSACTION STATUS`).
- Filters for successful transactions (`Transaction Status == Success`), safely discarding failed or declined entries.
- Enriches both Bank and GL records with unmasked `Customer VPA` and official `Payer Name` via 12-digit RRN and gateway switch tokens.

### 3. Mask-Aware VPA Matching Engine (`vpa_matches`)
Enables asymmetric matching between unmasked VPAs and masked `XX...` handles when PSP domains match and the unmasked handle ends with the masked tail (tail $\ge 3$ characters):
```python
def vpa_matches(v1: Optional[str], v2: Optional[str]) -> bool:
    # Handles exact matches and asymmetric masked matches
    # e.g., 'SABITHATLY2000@OKSBI' matches 'XX2000@OKSBI'
    ...
```

---

## 📊 5. Deep-Dive Prioritized Matching Tiers Catalog

The engine processes data through a sequence of 14 prioritized filters. Once a transaction is reconciled in a tier, it is removed from the search pool.

| Tier | Precedence Status | Window / Tolerances | Algorithmic Rules & Business Logic |
| :--- | :--- | :--- | :--- |
| **Tier 0** | `MANUAL_MATCH` | Dynamic | User overrides from `manual_links.json`. Supports N-to-N manual matching. |
| **Tier 1** | `TIER1_PERFECT` | Exact Date (+0 days)<br>Exact Amount | Same Date, Amount, and direct UTR/Reference. Sign-sensitive. |
| **Tier 1.1**| `TIER1.1_TRANSITIVE_CMS` | Exact Date (+0 days)<br>Exact Amount | Bridged matches via RTGS + CMS reports. Bypasses the Hard Conflict Guard. |
| **Tier 2** | `TIER2_UTR_MATCH` | ±45-Day Window<br>Exact Amount | UTR/Reference match with ±45-day safety window filter. |
| **Tier 2.1**| `TIER2.1_FUZZY_UTR` | ±3-Day Window<br>Exact Amount | Levenshtein distance 1 on 11–13 digit references for single-character typos/OCR slips. |
| **Tier 2.5**| `TIER2.5_CHEQUE_MATCH`| ±3-Day Window<br>Exact Amount | Matches exact 6-10 digit cheque numbers extracted from narration. |
| **Tier 2.8**| `TIER2.8_MARCH31_ADJUST`| Fiscal Year Window<br>Exact Amount | Matches March 31 GL adjustment entries against earlier bank entries with identical UTR/Cheque. |
| **Tier 3** | `TIER3_EXACT_DATE` | Exact Date (±1-day grace)<br>±₹0.50 Paisa Tolerance | Same Date and Amount with narrative compatibility check. Includes overnight clearing grace. |
| **Tier 3.2**| `TIER3.2_UPI_VPA_MATCH`| ±5-Day Window<br>Exact Amount | Matches transactions sharing verified identical customer VPAs or mask-resolved handles. |
| **Tier 4** | `TIER4_FUZZY_WINDOW` | ±5-Day Window<br>±₹0.50 Paisa Tolerance | Same amount with relaxed date boundaries. Requires narrative word intersection. |
| **Tier 5** | `TIER5_DEEP_NARRATION`| ±15-Day Window<br>Exact Amount | Fuzzy narrative matching via `difflib.SequenceMatcher` with threshold set to **0.72**. |
| **Tier 6** | `TIER6_GROUP_MATCH` | Same Date / Near Range<br>Exact Sum Balance | One-to-Many grouping: single Bank credit matching the sum of multiple GL debits. |
| **Tier 6b**| `TIER6B_GROUP_MATCH_REV`| Same Date / Near Range<br>Exact Sum Balance | Many-to-One grouping: multiple Bank entries matching a single aggregated GL entry. |
| **Offsets**| `INTERNAL_OFFSET` | O(1) Hash-Bucket Scan | Reversal and contra entries within the same ledger (`REVERSAL`, `BOUNCE`, `FAILED`, `RETURNED`). |

---

## 🛡️ 6. Hard Conflict Guard with Transitive Exception

To prevent false auto-links, a **Hard Conflict Guard** checks all candidate matches in the vectorized UTR loops:
- **Conflict Guard Rule**: If a GL entry has a 12-digit reference and a Bank entry has a different 12-digit reference, they are flagged as a mismatch and blocked.
- **Transitive Exception**: Validated CMS-bridged transactions mathematically expect different 12-digit references (GL payment serial vs. Bank batch ref). When auxiliary reports link the two, the Conflict Guard is safely bypassed.

---

## 🏛️ 7. Core Accounting, Persistence & Double-Entry Guards

Reconciliation state changes are strictly governed by double-entry ledger constraints:
- **Sign Sensitivity**: Bank DEBIT (Cash Outflow) matches GL CREDIT (Asset Reduction); Bank CREDIT (Cash Inflow) matches GL DEBIT (Asset Increase).
- **Double-Entry Manual Unlink Guard**: The unlinking API enforces structural validation. Any partial unlink that leaves orphaned links is completely dissolved, preventing unbalanced ledger states.
- **Atomic File Storage (`storage.py`)**: All saves use PID/TID-tagged temporary files (`path.pid.tid.tmp`), atomic replace, and Windows exponential backoff retries. Corrupt active files are automatically recovered from verified `.bak` backups.
- **Persistent UPI Knowledge Base (`Data/upi_knowledge_base.json`)**: Persistent cross-FY database mapping customer UPI handles to verified payer names and transaction counts.
- **Enterprise Sticky Notes System**: 7 note types (`NOTE`, `WARNING`, `ACTION`, `AUDIT`, `TAX`, `QUERY`, `WRONG_NARRATION`), severity scoring (`critical` to `info`), open/resolved lifecycle tracking, and bulk annotation via `POST /api/sticky-notes/batch`.

---

*Last Audit Date: November 5, 2026*  
*Audit Status: Full FY26-27 Ingestion (April–October) Validated. 100% Pass across all 55 tests. Production Ready.*
