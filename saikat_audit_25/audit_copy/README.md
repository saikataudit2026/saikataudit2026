# Receipt Audit Pipeline

This repo processes expense receipts for audit. The pipeline extracts data from Excel expense sheets, matches receipts to entries, generates screenshots, and runs AI-based receipt extraction.

---

## Pipeline Overview

```
Expense details/          Receipt files/
        │                       │
        ▼                       │
create_expense_mapping.py ──────┘
        │
        ▼
expense_receipt_mapping.csv
        │
        ├──────────────────────────────────────┐
        ▼                                      │
combine_excel_files.py                         │
        │                                      │
        ▼                                      │
combined_expense_data.csv                      │
        │                                      │
        ▼                                      │
merge_receipt_data.py ◄────────────────────────┘
        │
        ▼
merged_expense_data.csv
        │
        ▼
batch_screenshot_processor.py       ← also calls screenshot_function.py internally
        │                                      │
        ├──────────────────────────────────────┘
        │  converts PDFs/docs/images → .jpg
        ▼
screenshotoutput/          (screenshots of every receipt file)
        │
        │  (adds SCREENSHOT column pointing into screenshotoutput/)
        ▼
final_expense_data.csv   (created by batch_screenshot_processor.py)
        │
        ▼
batch_receipts.py  (or receipt_pipeline.py)
        │
        ▼
results/extracted.csv
results/extracted_full.json
        │
        │  (joined with final_expense_data.csv on screenshot filename)
        ▼
build_audit.py
        │
        ▼
saikat_audit_details.csv   ← FINAL AUDIT OUTPUT
```

---

## Why So Many Scripts? The Data Organization Problem

The pipeline exists because the source data comes from two separate, imperfectly-aligned sources that need to be reconciled.

### The source data structure

Each event lives under `Expense details/<Event>/` and contains two things:

```
Expense details/
  Annual Gala/
    Annual Gala/              ← nested folder
      Expenses.xlsx           ← the accounting log (one row per transaction)
      Receipts/               ← the actual receipt files (PDFs, images, etc.)
        receipt_costco.pdf
        hotel_invoice.jpg
        ...
```

- **The Excel file** is the accounting record. Each row is one expense transaction. It has a `Receipt` column that contains the filename of the supporting receipt.
- **The Receipts folder** is the evidence. It contains the actual files, but their names don't always exactly match what's typed in the Excel `Receipt` column.

### The two sources of truth don't line up perfectly

This is the core problem that forces multiple steps:

| Situation | Cause | Handled by |
|---|---|---|
| Excel row references a receipt with a slightly different filename | Manual data entry, typos | Fuzzy matching in `combine_excel_files.py` |
| A receipt file exists in `Receipts/` but no Excel row points to it | Receipt was filed but not logged, or logged under a different name | `merge_receipt_data.py` appends these as sparse rows |
| An Excel row references a receipt file that doesn't exist on disk | File was lost, renamed, or never submitted | Marked `NOT_FOUND` in `FILE_LOCATION` |
| Receipt files are PDFs, Word docs, or images | Mixed formats from multiple submitters | `screenshot_function.py` normalizes all to `.jpg` |

### What each intermediate file actually represents

| File | Unit of a row | Coverage |
|---|---|---|
| `expense_receipt_mapping.csv` | One receipt **file** that physically exists on disk | All receipt files found in all `Receipts/` folders — regardless of whether any Excel row references them |
| `combined_expense_data.csv` | One **Excel expense row** (a transaction) | All accounting entries from all Excel files — with `FILE_LOCATION` filled where a receipt file was matched, `NOT_FOUND` otherwise |
| `merged_expense_data.csv` | Either an Excel row or an orphaned receipt file | Union of both: every Excel entry + every receipt file not claimed by any Excel row |
| `final_expense_data.csv` | Same as merged | Same as merged, but with a `SCREENSHOT` column added. `FILE_LOCATION` → original file (PDF/docx/image); `SCREENSHOT` → the `.jpg` that the AI can actually read. These differ because the AI only handles images. |

### Why not just do it in one script?

The split is intentional:

1. `create_expense_mapping.py` — pure file discovery, no matching logic. Produces a stable index of what files exist.
2. `combine_excel_files.py` — pure accounting extraction. Works from the Excel side, fuzzy-matches to the index.
3. `merge_receipt_data.py` — gap analysis. Identifies receipt files that slipped through the fuzzy match and ensures nothing is silently dropped from the audit.
4. `batch_screenshot_processor.py` — format normalization. Converts all receipt formats to images the AI can read.
5. `batch_receipts.py` / `receipt_pipeline.py` — AI extraction. Reads the normalized images and extracts structured fields.

Each step can be rerun independently if its inputs change, and each intermediate file is a useful audit artifact on its own.

---

## Execution Sequence

```bash
# Step 1 — Create receipt-to-event mapping
python create_expense_mapping.py

# Step 2 — Combine all Excel expense sheets into one CSV
python combine_excel_files.py

# Step 3 — Merge in any receipts missing from combined data
python merge_receipt_data.py

# Step 4 — Generate screenshots for all receipt files
python batch_screenshot_processor.py

# Step 5 — Extract structured data from receipt screenshots (pick one):

# Option A: Fully automated (no human review)
python batch_receipts.py -I screenshotoutput/ --quant 4bit --max 0 --events events.csv

# Option B: Interactive with human review
python receipt_pipeline.py --events events.csv

# Step 6 — Build final audit CSV with amount comparison
python build_audit.py
```

---

## Scripts

### `create_expense_mapping.py`

Creates a flat CSV mapping every receipt file to its event and Excel source.

**Reads:**
- `Expense details/<Event>/<nested>/*.xlsx` — expense Excel files
- `Expense details/<Event>/Receipts/` — receipt files per event

**Writes:**
- `expense_receipt_mapping.csv`

| Column | Description |
|---|---|
| `Event` | Event folder name |
| `Excel_Filename` | Source Excel file |
| `Total_Excel_Entries` | Row count in that Excel |
| `Receipt_Filename` | Receipt file name |
| `Receipt_Full_Path` | Full path to receipt file |

```bash
python create_expense_mapping.py
```

---

### `combine_excel_files.py`

Reads every Excel file in `Expense details/`, extracts expense rows, and fuzzy-matches the `Receipt` column to entries in `expense_receipt_mapping.csv`.

**Reads:**
- `Expense details/<Event>/<nested>/*.xlsx`
- `expense_receipt_mapping.csv`

**Writes:**
- `combined_expense_data.csv`

| Column | Description |
|---|---|
| `Date`, `Detail`, `Type`, `Amount`, `Owner`, `Receipt` | From Excel |
| `XLS_REFERENCE` | `EventFolder/ExcelFile.xlsx` |
| `REFERENCE_TO_CSV` | `ExcelFile.xlsx:Row_N` (row in mapping CSV) |
| `FILE_LOCATION` | Full path to matched receipt file |

```bash
python combine_excel_files.py
```

---

### `merge_receipt_data.py`

Finds receipts in `expense_receipt_mapping.csv` that were not matched in `combined_expense_data.csv` and appends them as new rows.

**Reads:**
- `combined_expense_data.csv`
- `expense_receipt_mapping.csv`

**Writes:**
- `merged_expense_data.csv`

```bash
python merge_receipt_data.py
```

---

### `batch_screenshot_processor.py`

Produces `final_expense_data.csv` — the last intermediate file before AI extraction.

**Why this step exists:** `FILE_LOCATION` in `merged_expense_data.csv` points to the original receipt files, which can be PDFs, Word docs, HEIC images, or other formats. The AI extraction scripts (`batch_receipts.py`, `receipt_pipeline.py`) only work on `.jpg` images. This script converts everything to a consistent image format and records where each screenshot landed.

**What it does:**
1. Reads all unique directories from the `FILE_LOCATION` column in `merged_expense_data.csv`
2. Calls `screenshot_function.process_directory()` on each directory (headless mode, skips existing) — outputs `screenshotoutput/scrshot_<filename>.jpg`
3. Scans `screenshotoutput/` and builds a map: `original_filename → screenshot_path`
4. Writes `final_expense_data.csv` = every row from `merged_expense_data.csv` + a new `SCREENSHOT` column

A row gets `SCREENSHOT = NOT_FOUND` if its `FILE_LOCATION` was `NOT_FOUND` or if screenshot generation failed.

**Reads:**
- `merged_expense_data.csv` (uses `FILE_LOCATION` column)

**Writes:**
- `screenshotoutput/scrshot_<filename>.jpg` — one `.jpg` per receipt file
- `final_expense_data.csv` — all merged columns + `SCREENSHOT` (path to the `.jpg`)

```bash
python batch_screenshot_processor.py
```

---

### `screenshot_function.py`

Library used by `batch_screenshot_processor.py`. Can also be run standalone to convert PDFs, Word docs, and images to `.jpg` screenshots.

Supports: `.pdf`, `.docx`, `.doc`, `.jpg`, `.jpeg`, `.png`, `.gif`, `.heic`, `.txt`, `.rtf`

Multi-page PDFs produce `scrshot_<name>_p001.jpg`, `scrshot_<name>_p002.jpg`, etc.

```bash
# Single file
python screenshot_function.py --input document.pdf --output screenshot.jpg

# Entire directory (headless, skips existing)
python screenshot_function.py --input-dir ./receipts --output-dir ./screenshots --quiet

# Re-process everything
python screenshot_function.py --input-dir ./receipts --output-dir ./screenshots --quiet --no-skip
```

---

### `batch_receipts.py`

Automated AI extraction of store name, date, total, and line items from receipt screenshots. No human input required. Skips files already in the output CSV.

**Reads:** Receipt images from a directory

**Writes:**
- `results/extracted.csv`
- `results/extracted_full.json`

| Column | Description |
|---|---|
| `store_name`, `date`, `total` | Extracted fields |
| `items` | JSON list of line items |
| `saikat_event` | Nearest event matched by date |
| `purpose` | Heuristic expense category |
| `status` | `auto` or `auto-low-confidence` |
| `source` | Model used + flags |

```bash
# Basic run — processes first 3 by default
python batch_receipts.py -I screenshotoutput/ --quant 4bit

# Process all receipts
python batch_receipts.py -I screenshotoutput/ --quant 4bit --max 0

# With event matching
python batch_receipts.py -I screenshotoutput/ --quant 4bit --max 0 --events events.csv

# List current results
python batch_receipts.py --list

# Debug a single image (no CSV written)
python batch_receipts.py --debug screenshotoutput/scrshot_receipt.jpg --quant 4bit

# Use a different model
python batch_receipts.py -I screenshotoutput/ --model gemini --max 0
```

**Models:**

| Key | Model | Notes |
|---|---|---|
| `qwen` | Qwen2-VL-7B | Default. ~4 GB VRAM (4bit), ~7 GB (8bit) |
| `gemini` | Gemini-2.0-Flash | Cloud API, needs key |
| `paddleocr` | PaddleOCR + regex | CPU only, fast |
| `donut` | Donut mychen76 | GPU, ~500 MB |

**Quant options for Qwen:** `4bit` (fastest), `8bit` (default, better quality), `none` (bfloat16), `--no-gpu` (CPU only, very slow)

---

### `receipt_pipeline.py`

Interactive version of receipt extraction with human review after each model attempt. Use when you want to validate or correct results as they are extracted.

Commands during review: `[a]ccept`, `[e]dit`, `[n]ext model`, `[m]anual entry`, `[s]kip`, `[q]uit`

**Writes:** Same output format as `batch_receipts.py` (`results/extracted.csv`, `results/extracted_full.json`)

```bash
python receipt_pipeline.py
python receipt_pipeline.py --image-dir ./screenshotoutput --events events.csv
```

---

### `build_audit.py`

Joins `final_expense_data.csv` with `results/extracted.csv` (AI extraction output) and writes the final audit file `saikat_audit_details.csv`.

**What it does:**
- Matches each row in `final_expense_data.csv` to its AI-extracted entry using the `SCREENSHOT` filename as the join key
- Adds AI-extracted columns alongside the original Excel columns
- Compares the Excel `Amount` against the AI-extracted `ai_total` and sets `auditor_comments`

**`auditor_comments` values:**

| Value | When set |
|---|---|
| `Amount matched` | Receipt found, AI total agrees with Excel amount (within $1.00) |
| `Needs review` | Receipt file missing (`FILE_LOCATION = NOT_FOUND`) |
| `Needs review` | No Excel record for this receipt (orphaned file) |
| `Needs review` | Screenshot could not be generated (`SCREENSHOT = NOT_FOUND`) |
| `Needs review` | AI did not process this receipt (not in `extracted.csv`) |
| `Needs review` | AI total differs from Excel amount |
| `Needs review` | Either amount is missing or unreadable |

**Reads:**
- `final_expense_data.csv`
- `results/extracted.csv`

**Writes:**
- `saikat_audit_details.csv` — all columns from both inputs plus `auditor_comments`

**Output columns added:**

| Column | Source |
|---|---|
| `ai_store_name` | AI-extracted store name |
| `ai_date` | AI-extracted date |
| `ai_total` | AI-extracted total amount |
| `ai_items` | AI-extracted line items (JSON) |
| `ai_event` | Event matched by AI from receipt date |
| `ai_purpose` | Expense category guessed by AI |
| `auditor_comments` | `Amount matched` or `Needs review` |

```bash
python build_audit.py
```

---

## Key Files

| File | Description |
|---|---|
| `expense_receipt_mapping.csv` | Receipt → event/Excel mapping (generated by step 1) |
| `combined_expense_data.csv` | Excel rows with matched receipt paths (step 2) |
| `merged_expense_data.csv` | Combined + unmatched receipts appended (step 3) |
| `screenshotoutput/` | Screenshots of all receipt files (step 4) |
| **`final_expense_data.csv`** | **Full expense data with screenshot paths (step 4 output)** |
| `results/extracted.csv` | AI-extracted receipt fields (step 5) |
| `results/extracted_full.json` | Full item-level receipt data (step 5) |
| **`saikat_audit_details.csv`** | **Final audit file: Excel + AI columns + `auditor_comments` (step 6)** |
