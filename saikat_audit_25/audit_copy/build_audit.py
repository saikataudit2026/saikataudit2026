#!/usr/bin/env python3
"""
Merge final_expense_data.csv with AI-extracted results/extracted.csv
into saikat_audit_details.csv.

For each row:
  - Adds AI-extracted columns (ai_store_name, ai_date, ai_total, ai_items, ai_event, ai_purpose)
  - Sets auditor_comments to:
      "Amount matched"  — receipt found, AI total agrees with Excel Amount
      "Needs review"    — receipt missing, no Excel record, screenshot failed,
                          AI did not process the receipt, or amounts differ

Join key: SCREENSHOT basename (e.g. scrshot_foo.pdf.jpg) matched against
          the filename column in extracted.csv.
          Multi-receipt images (filename contains ::N) are matched on the
          base part before the separator.
"""

import csv
import re
from pathlib import Path


# ── amount parsing ────────────────────────────────────────────────────────────
def _parse_amount(value: str) -> float | None:
    """Strip currency symbols/commas and return float, or None if unparseable."""
    if not value:
        return None
    cleaned = re.sub(r"[$,\s]", "", value.strip())
    try:
        return float(cleaned)
    except ValueError:
        return None


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    final_csv     = "final_expense_data.csv"
    extracted_csv = "results/extracted.csv"
    output_csv    = "saikat_audit_details.csv"

    # ── load AI data keyed by screenshot basename ─────────────────────────────
    # filename in extracted.csv is like "scrshot_foo.pdf.jpg" or
    # "scrshot_foo.jpg::2" for multi-receipt images.
    # We build two lookups:
    #   exact_key  → the full basename (handles single-receipt files)
    #   base_key   → the part before "::" (handles multi-receipt images)
    ai_by_exact: dict[str, list[dict]] = {}
    ai_by_base:  dict[str, list[dict]] = {}

    with open(extracted_csv, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            raw  = row.get("filename", "")
            base = Path(raw.split("::")[0]).name   # strip ::N suffix, take basename
            full = Path(raw).name                  # full basename including ::N

            ai_by_exact.setdefault(full, []).append(row)
            ai_by_base.setdefault(base, []).append(row)

    print(f"Loaded {sum(len(v) for v in ai_by_exact.values())} AI-extracted entries")

    # ── process final_expense_data.csv ────────────────────────────────────────
    rows_out: list[dict] = []
    input_columns: list[str] = []

    with open(final_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        input_columns = list(reader.fieldnames or [])

        for row in reader:
            screenshot    = row.get("SCREENSHOT", "").strip()
            file_location = row.get("FILE_LOCATION", "").strip()
            excel_amount  = row.get("Amount", "").strip()

            # A row has an Excel record if Date or Detail is populated
            # (orphaned receipt rows from merge_receipt_data.py leave these blank)
            has_excel_record = bool(
                row.get("Date", "").strip() or row.get("Detail", "").strip()
            )

            ai_store = ai_date = ai_total = ai_items = ai_event = ai_purpose = ""
            auditor_comments = ""

            if not file_location or file_location == "NOT_FOUND":
                auditor_comments = "Needs review"          # no receipt file on disk

            elif not screenshot or screenshot == "NOT_FOUND":
                auditor_comments = "Needs review"          # screenshot generation failed

            else:
                scr_key = Path(screenshot).name            # e.g. scrshot_foo.pdf.jpg

                # Try exact match first, then base match
                ai_rows = ai_by_exact.get(scr_key) or ai_by_base.get(scr_key)

                if not ai_rows:
                    auditor_comments = "Needs review"      # AI never processed this receipt
                else:
                    # Populate AI columns regardless of whether an Excel record exists
                    ai = ai_rows[0]
                    ai_store   = ai.get("store_name", "")
                    ai_date    = ai.get("date", "")
                    ai_total   = ai.get("total", "")
                    ai_items   = ai.get("items", "")
                    ai_event   = ai.get("saikat_event", "")
                    ai_purpose = ai.get("purpose", "")

                    if not has_excel_record:
                        auditor_comments = "Needs review"  # orphaned receipt, no Excel entry to compare

                    else:
                        excel_amt = _parse_amount(excel_amount)
                        ai_amt    = _parse_amount(ai_total)

                        if excel_amt is None or ai_amt is None:
                            auditor_comments = "Needs review"  # one or both amounts missing/unreadable
                        elif abs(excel_amt - ai_amt) <= 1.00:  # $1 tolerance
                            auditor_comments = "Amount matched"
                        else:
                            auditor_comments = "Needs review"  # amounts differ

            out = dict(row)
            out["ai_store_name"] = ai_store
            out["ai_date"]       = ai_date
            out["ai_total"]      = ai_total
            out["ai_items"]      = ai_items
            out["ai_event"]      = ai_event
            out["ai_purpose"]    = ai_purpose
            out["auditor_comments"] = auditor_comments
            rows_out.append(out)

    # ── write output ──────────────────────────────────────────────────────────
    if not rows_out:
        print("No rows to write.")
        return

    ai_cols       = ["ai_store_name", "ai_date", "ai_total", "ai_items",
                     "ai_event", "ai_purpose"]
    all_fieldnames = input_columns + ai_cols + ["auditor_comments"]

    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=all_fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows_out)

    # ── summary ───────────────────────────────────────────────────────────────
    matched      = sum(1 for r in rows_out if r["auditor_comments"] == "Amount matched")
    needs_review = sum(1 for r in rows_out if r["auditor_comments"] == "Needs review")

    print(f"\n{'─'*56}")
    print(f"  Output         : {output_csv}")
    print(f"  Total rows     : {len(rows_out)}")
    print(f"  Amount matched : {matched}")
    print(f"  Needs review   : {needs_review}")
    print(f"{'─'*56}\n")


if __name__ == "__main__":
    main()
