#!/usr/bin/env python3
"""
Batch receipt extraction — no human-in-the-loop.

Processes all receipts in a directory using one model (default: Qwen2-VL-7B),
writes results to a CSV file, and skips entries that already exist in the CSV.
Low-confidence results (any of store_name / date / total missing) are flagged
with status "auto-low-confidence" and the source column carries a [LOW] marker.

Each receipt image is displayed alongside extracted data while processing,
just like the interactive pipeline. Items are extracted from the model; if the
model returns none, a best-effort guess is made from the store name and total.

Usage examples:
  python batch_receipts.py                          # defaults
  python batch_receipts.py --model gemini
  python batch_receipts.py --list
  python batch_receipts.py ./Receipt_2021/output_screenshot results/out.csv --model paddleocr
"""

import argparse
import csv
import json
import sys
import textwrap
import warnings
from dataclasses import asdict
from pathlib import Path
from typing import Optional

warnings.filterwarnings("ignore", message="No ccache found")
warnings.filterwarnings("ignore", category=UserWarning, module="requests")

import matplotlib.gridspec as gridspec
import matplotlib.pyplot as plt
from PIL import Image

# ── import shared definitions from the baseline pipeline ──────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from receipt_pipeline import (
    Receipt,
    CSV_FIELDS,
    DEFAULT_IMAGE_DIR,
    CSV_PATH,
    JSON_PATH,
    extract_paddleocr,
    extract_donut,
    extract_qwen,
    extract_gemini,
)

# ── constants ─────────────────────────────────────────────────────────────────
VALID_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".gif"}

MODELS = {
    "paddleocr": ("PaddleOCR",        extract_paddleocr),
    "donut":     ("Donut",            extract_donut),
    "qwen":      ("Qwen2-VL-7B",      extract_qwen),
    "gemini":    ("Gemini-2.0-Flash", extract_gemini),
}
DEFAULT_MODEL = "qwen"


# ── item guessing ─────────────────────────────────────────────────────────────
# Rough category → typical line items used when the model returns nothing.
_STORE_HINTS = {
    "costco":       ["Costco merchandise"],
    "walmart":      ["Walmart merchandise"],
    "target":       ["Target merchandise"],
    "amazon":       ["Amazon purchase"],
    "whole foods":  ["Grocery items"],
    "trader joe":   ["Grocery items"],
    "safeway":      ["Grocery items"],
    "kroger":       ["Grocery items"],
    "ralphs":       ["Grocery items"],
    "vons":         ["Grocery items"],
    "cvs":          ["CVS items"],
    "walgreens":    ["Walgreens items"],
    "home depot":   ["Home improvement items"],
    "lowes":        ["Home improvement items"],
    "lowe's":       ["Home improvement items"],
    "best buy":     ["Electronics purchase"],
    "staples":      ["Office supplies"],
    "uhaul":        ["U-Haul rental"],
    "u-haul":       ["U-Haul rental"],
    "restaurant":   ["Food & beverage"],
    "cafe":         ["Food & beverage"],
    "pizza":        ["Pizza order"],
    "sushi":        ["Restaurant meal"],
    "hotel":        ["Hotel stay"],
    "uber":         ["Ride or delivery"],
    "lyft":         ["Ride share"],
}


def _fill_items_fallback(r: Receipt) -> Receipt:
    """
    If the model returned no items, construct a best-effort guess.
    Guessed items are marked with guessed=True so the display can flag them.
    """
    if r.items:
        return r

    store_lower = (r.store_name or "").lower()

    # Try to match a known store keyword
    guessed_names = []
    for keyword, hints in _STORE_HINTS.items():
        if keyword in store_lower:
            guessed_names = hints
            break

    # Fallback: use a generic description derived from the store name
    if not guessed_names:
        label = f"Purchase at {r.store_name}" if r.store_name else "Purchase (store unknown)"
        guessed_names = [label]

    price = r.total or ""
    r.items = [{"name": name, "price": price, "guessed": True} for name in guessed_names]
    return r


# ── display ───────────────────────────────────────────────────────────────────
_fig = _ax_img = _ax_txt = None


def _init_figure():
    global _fig, _ax_img, _ax_txt
    plt.ion()
    _fig = plt.figure(figsize=(15, 8))
    gs = gridspec.GridSpec(1, 2, figure=_fig, width_ratios=[1, 1], wspace=0.04)
    _ax_img = _fig.add_subplot(gs[0])
    _ax_txt = _fig.add_subplot(gs[1])
    _ax_img.axis("off")
    _ax_txt.axis("off")


def _fmt_result(r: Receipt, idx: int, total: int, status: str) -> str:
    low = "low-confidence" in status
    flag = "  [LOW CONFIDENCE]" if low else ""
    sep = "─" * 44
    lines = [
        f"  [{idx}/{total}]  {r.source}{flag}",
        sep,
        f"  Store :  {r.store_name or '(not found)'}",
        f"  Date  :  {r.date       or '(not found)'}",
        f"  Total :  {r.total      or '(not found)'}",
        sep,
    ]
    if r.items:
        any_guessed = any(it.get("guessed") for it in r.items)
        header = f"  Items ({len(r.items)})" + ("  [guessed]" if any_guessed else "") + ":"
        lines.append(header)
        for it in r.items[:14]:
            name  = textwrap.shorten(it.get("name", ""), width=28, placeholder="…")
            price = it.get("price", "")
            g     = " *" if it.get("guessed") else ""
            lines.append(f"    •{g} {name:<28}  {price}")
        if len(r.items) > 14:
            lines.append(f"    … and {len(r.items) - 14} more")
    else:
        lines.append("  Items :  (none extracted)")
    lines.append(sep)
    return "\n".join(lines)


def _show(img_path: str, r: Receipt, idx: int, total: int, status: str):
    global _fig, _ax_img, _ax_txt
    if _fig is None or not plt.fignum_exists(_fig.number):
        _init_figure()

    _fig.suptitle(
        f"Batch  [{idx}/{total}]  —  {Path(img_path).name}",
        fontsize=9, y=0.99,
    )

    _ax_img.cla()
    _ax_img.imshow(Image.open(img_path))
    _ax_img.axis("off")

    _ax_txt.cla()
    _ax_txt.axis("off")
    txt = _fmt_result(r, idx, total, status)
    low = "low-confidence" in status
    color = "#f39c12" if low else "#2ecc71"   # orange=low-conf, green=good
    _ax_txt.text(
        0.03, 0.97, txt,
        transform=_ax_txt.transAxes,
        fontsize=8.5, verticalalignment="top", fontfamily="monospace",
        bbox=dict(boxstyle="round,pad=0.6", facecolor=color, alpha=0.18),
    )

    _fig.canvas.draw()
    _fig.canvas.flush_events()
    plt.pause(0.05)


# ── CSV / JSON helpers ────────────────────────────────────────────────────────
def _load_csv(csv_path: Path) -> dict:
    """Return {filename: row_dict} for every row in csv_path."""
    state: dict = {}
    if csv_path.exists():
        with open(csv_path, newline="") as f:
            for row in csv.DictReader(f):
                for field in CSV_FIELDS:
                    row.setdefault(field, "")
                state[row["filename"]] = row
    return state


def _write_csv(state: dict, csv_path: Path):
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(state.values())


def _upsert_json(r: Receipt, status: str, model_name: str, json_path: Path):
    """Persist full receipt data (including items) to the JSON sidecar."""
    existing: dict = {}
    if json_path.exists():
        try:
            for entry in json.loads(json_path.read_text()):
                existing[entry["filename"]] = entry
        except (json.JSONDecodeError, KeyError):
            pass
    entry = asdict(r)
    entry["status"] = status
    entry["models_tried"] = [model_name]
    existing[r.filename] = entry
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(list(existing.values()), indent=2))


# ── event matching ────────────────────────────────────────────────────────────
def _load_events(events_path: Path) -> list[dict]:
    """
    Load events from a CSV or JSON file.

    CSV format (header required):
        event_name,date          or     name,date
        Annual Gala,2021-03-15
        ...

    JSON format:
        [{"name": "Annual Gala", "date": "2021-03-15"}, ...]
    """
    suffix = events_path.suffix.lower()
    events = []

    if suffix == ".json":
        data = json.loads(events_path.read_text())
        for item in data:
            name = item.get("event_name") or item.get("name", "")
            date = item.get("date", "")
            if name and date:
                events.append({"name": name, "date": date})

    else:  # treat as CSV
        with open(events_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row.get("event_name") or row.get("name", "")
                date = row.get("date", "")
                if name and date:
                    events.append({"name": name, "date": date})

    # Pre-parse dates; skip entries that can't be parsed
    parsed = []
    for ev in events:
        try:
            from dateutil import parser as dparser
            ev["_dt"] = dparser.parse(ev["date"], dayfirst=False)
            parsed.append(ev)
        except Exception:
            print(f"  [events] Could not parse date '{ev['date']}' for event '{ev['name']}' — skipped")
    return parsed


def _match_event(receipt_date: str, events: list[dict]) -> str:
    """
    Return the name of the event whose date is closest to receipt_date.
    Returns "" if receipt_date is empty or unparseable, or if events is empty.
    """
    if not receipt_date or not events:
        return ""
    try:
        from dateutil import parser as dparser
        rdt = dparser.parse(receipt_date, dayfirst=False)
    except Exception:
        return ""

    best = min(events, key=lambda ev: abs((ev["_dt"] - rdt).total_seconds()))
    return best["name"]


# ── purpose guessing ──────────────────────────────────────────────────────────
# Maps store-type keywords → (category_label, priority).
# Higher priority wins when multiple keywords match.
_STORE_CATEGORIES: list[tuple[list[str], str, int]] = [
    # Home improvement / venue setup
    (["home depot", "lowe's", "lowes", "ace hardware", "menards", "harbor freight",
      "true value"], "Decorating / props", 10),
    # Wholesale / bulk catering
    (["costco", "sam's club", "bj's", "restaurant depot", "smart & final"],
     "Bulk supplies / catering", 10),
    # Grocery / food retail
    (["whole foods", "trader joe", "safeway", "kroger", "ralphs", "vons", "aldi",
      "sprouts", "publix", "heb", "wegmans", "fresh market", "stop & shop",
      "food lion", "winn-dixie", "albertsons", "lucky", "stater bros"],
     "Food & beverages", 9),
    # Restaurants / cafes / takeout
    (["restaurant", "cafe", "coffee", "pizza", "sushi", "grill", "bistro", "diner",
      "kitchen", "thai", "indian", "chinese", "mexican", "bbq", "burger", "subway",
      "chipotle", "mcdonald", "panda express", "in-n-out", "chick-fil",
      "olive garden", "cheesecake factory", "applebee", "ihop", "denny"],
     "Catering / dining", 9),
    # Party & event supplies
    (["party city", "dollar tree", "dollar general", "five below", "party depot",
      "iparty", "shindigz"], "Party supplies / decorations", 10),
    # Electronics / AV
    (["best buy", "apple store", "micro center", "b&h", "adorama", "fry's",
      "newegg", "staples", "office depot", "office max"],
     "AV / tech equipment", 8),
    # Clothing / linens / department stores
    (["macy's", "macys", "nordstrom", "bloomingdale", "target", "marshall",
      "tj maxx", "ross", "burlington", "h&m", "zara", "old navy", "gap", "kohl's"],
     "Attire / table linens", 7),
    # Pharmacy / misc
    (["cvs", "walgreens", "rite aid", "duane reade"],
     "Miscellaneous / first aid", 5),
    # Moving / transport rental
    (["uhaul", "u-haul", "budget truck", "penske", "ryder"],
     "Equipment transport / setup", 8),
    # Hotel / venue
    (["hotel", "marriott", "hilton", "hyatt", "sheraton", "westin", "holiday inn",
      "hampton inn", "embassy suites", "airbnb"],
     "Accommodation / venue", 9),
    # Ride share / delivery
    (["uber", "lyft", "doordash", "grubhub", "instacart", "postmates"],
     "Transportation / delivery", 6),
    # Liquor / wine
    (["liquor", "wine", "spirits", "beer", "total wine", "bevmo", "binny"],
     "Beverages / bar", 10),
    # Walmart — general merchandise, refined by items
    (["walmart"], "General supplies", 6),
    # Amazon — refined by items
    (["amazon"], "General supplies", 5),
]

# Keywords inside item names that refine or override the store-level guess
_ITEM_OVERRIDES: list[tuple[list[str], str]] = [
    (["balloon", "banner", "streamer", "confetti", "decoration", "centerpiece",
      "tablecloth", "napkin", "plate", "cup", "cutlery", "candle"],
     "Party supplies / decorations"),
    (["tent", "canopy", "chair", "table", "folding", "linen", "backdrop",
      "stage", "podium", "sign", "display"], "Venue setup / furnishings"),
    (["food", "grocery", "produce", "meat", "seafood", "dairy", "snack",
      "beverage", "drink", "water", "soda", "juice", "coffee", "tea"],
     "Food & beverages"),
    (["beer", "wine", "liquor", "spirit", "alcohol", "champagne", "prosecco"],
     "Beverages / bar"),
    (["speaker", "microphone", "projector", "screen", "cable", "adapter",
      "camera", "light", "lighting", "led", "bulb", "extension cord"],
     "AV / tech equipment"),
    (["lumber", "paint", "brush", "tool", "drill", "screw", "nail", "wood",
      "foam", "fabric", "glue", "tape", "rope"], "Decorating / props"),
    (["shirt", "jacket", "dress", "suit", "uniform", "badge", "lanyard"],
     "Attire / staff uniforms"),
]


def _guess_purpose(store_name: str, items: list[dict], event_name: str) -> str:
    """
    Heuristically guess the expense purpose from store name, item list, and event.
    Returns a short phrase like "Food & beverages for Summer Picnic",
    or "cannot determine" when no confident guess is possible.
    """
    store_lower = (store_name or "").lower()
    event_suffix = f" for {event_name}" if event_name else ""

    # 1. Try to categorise by store name
    best_category = ""
    best_priority = 0
    for keywords, category, priority in _STORE_CATEGORIES:
        if any(kw in store_lower for kw in keywords):
            if priority > best_priority:
                best_category = category
                best_priority = priority

    # 2. Try to refine/override using item names (higher signal)
    item_text = " ".join(
        (it.get("name") or "").lower() for it in (items or [])
    )
    if item_text.strip():
        for keywords, category in _ITEM_OVERRIDES:
            if any(kw in item_text for kw in keywords):
                # Item override wins if it's at least as specific
                best_category = category
                break  # first matching override wins

    if best_category:
        return best_category + event_suffix

    return "cannot determine"


# ── confidence ────────────────────────────────────────────────────────────────
def _is_low_confidence(r: Receipt) -> bool:
    # Also flag multi-entry images as low-confidence — only first entry was used
    return not r.store_name or not r.date or not r.total or "[MULTI]" in r.source


# ── list view ─────────────────────────────────────────────────────────────────
def print_list(csv_path: Path):
    state = _load_csv(csv_path)
    if not state:
        print(f"\n  No rows in {csv_path} yet.\n")
        return

    rows = list(state.values())
    W_file  = min(max(len(r["filename"])   for r in rows), 44)
    W_store = min(max(len(r["store_name"]) for r in rows), 28)
    W_date  = min(max(len(r["date"])       for r in rows), 12)
    W_total = min(max(len(r["total"])      for r in rows),  9)

    header = (
        f"  {'#':<4} "
        f"{'Filename':<{W_file}}  "
        f"{'Store':<{W_store}}  "
        f"{'Date':<{W_date}}  "
        f"{'Total':>{W_total}}  "
        f"{'Status':<24}  "
        f"Source"
    )
    sep = "  " + "─" * (len(header) - 2)
    print(f"\n{sep}\n{header}\n{sep}")

    for i, (_, row) in enumerate(sorted(state.items()), 1):
        print(
            f"  {i:<4} "
            f"{row['filename'][:W_file]:<{W_file}}  "
            f"{row['store_name'][:W_store]:<{W_store}}  "
            f"{row['date'][:W_date]:<{W_date}}  "
            f"{row['total'][:W_total]:>{W_total}}  "
            f"{row['status']:<24}  "
            f"{row.get('source', '')}"
        )

    print(f"{sep}\n  {len(state)} rows total\n")


# ── batch processing ──────────────────────────────────────────────────────────
def process_batch(image_dir: Path, csv_path: Path, json_path: Path, model_key: str,
                  events: list[dict] | None = None, max_receipts: int = 0,
                  quant: str = "8bit", max_tokens: int = 2048):
    model_name, extractor = MODELS[model_key]

    files = sorted(p for p in image_dir.iterdir() if p.suffix.lower() in VALID_EXT)
    if not files:
        sys.exit(f"No images found in {image_dir}")

    if max_receipts > 0:
        files = files[:max_receipts]

    state   = _load_csv(csv_path)
    total   = len(files)
    def _is_error_entry(row: dict) -> bool:
        return "[ERROR]" in row.get("source", "")

    def _file_done(fname):
        """True if this image file already has at least one non-error row in state."""
        return (fname in state and not _is_error_entry(state[fname])) or \
               (f"{fname}::1" in state and not _is_error_entry(state[f"{fname}::1"]))

    def _file_errored(fname):
        return (fname in state and _is_error_entry(state[fname])) or \
               (f"{fname}::1" in state and _is_error_entry(state[f"{fname}::1"]))

    already  = sum(1 for f in files if _file_done(f.name))
    n_errors = sum(1 for f in files if not _file_done(f.name) and _file_errored(f.name))

    print(f"\nFound {total} receipts in {image_dir}")
    if max_receipts > 0:
        print(f"Limiting : first {max_receipts} (--max 0 to disable)")
    print(f"Model    : {model_name}")
    if model_key == "qwen":
        print(f"Quant    : {quant}")
    print(f"Output   : {csv_path}")
    print(f"Skipping : {already} already in CSV")
    if n_errors:
        print(f"Retrying : {n_errors} previous errors\n")
    else:
        print()

    n_new = n_low = n_err = 0

    for idx, img_path in enumerate(files, 1):
        fname = img_path.name

        if _file_done(fname):
            print(f"  [{idx:>3}/{total}] {fname}  → already in CSV, skipping")
            continue

        retry = _file_errored(fname)
        label = "retrying error" if retry else model_name
        print(f"  [{idx:>3}/{total}] {fname}  → {label} …", end=" ", flush=True)

        try:
            if model_key == "qwen":
                result = extractor(str(img_path), quant=quant, max_new_tokens=max_tokens)
            else:
                result = extractor(str(img_path))
            receipts = result if isinstance(result, list) else [result]
            n_parts  = len(receipts)
            if n_parts > 1:
                print(f"  [{n_parts} receipts found]")
            else:
                print()  # newline after the "→ model …" line

            for part_idx, r in enumerate(receipts, 1):
                key      = fname if n_parts == 1 else f"{fname}::{part_idx}"
                r.filename = key

                r = _fill_items_fallback(r)

                low    = _is_low_confidence(r)
                source = r.source + ("[LOW]" if low else "")
                status = "auto-low-confidence" if low else "auto"
                flag   = "  [LOW CONFIDENCE]" if low else ""

                prefix = f"    [{part_idx}/{n_parts}]" if n_parts > 1 else "         "
                print(
                    f"{prefix}  store={r.store_name or '?':20}  "
                    f"date={r.date or '?':12}  "
                    f"total={r.total or '?'}{flag}"
                )
                if r.items:
                    guessed = all(it.get("guessed") for it in r.items)
                    print(f"{prefix}  items={len(r.items)} ({'guessed' if guessed else 'extracted'})")
                if low:
                    n_low += 1

                matched_event   = _match_event(r.date, events or [])
                guessed_purpose = _guess_purpose(r.store_name, r.items, matched_event)
                state[key] = {
                    "store_name":       r.store_name,
                    "date":             r.date,
                    "items":            json.dumps(r.items) if r.items else "",
                    "total":            r.total,
                    "saikat_event":     matched_event,
                    "purpose":          r.purpose or guessed_purpose,
                    "auditor_comments": r.auditor_comments,
                    "filename":         key,
                    "source":           source,
                    "status":           status,
                    "models_tried":     model_name,
                }
                _upsert_json(r, status, model_name, json_path)

            # Display the first receipt alongside the image
            try:
                _show(str(img_path), receipts[0], idx, total, state[fname if n_parts == 1 else f"{fname}::1"]["status"])
            except Exception:
                pass

            _write_csv(state, csv_path)

        except Exception as ex:
            print(f"FAILED ({ex})")
            r      = Receipt(filename=fname)
            source = f"{model_name}[ERROR]"
            status = "auto-low-confidence"
            state[fname] = {
                "store_name": "", "date": "", "items": "", "total": "",
                "saikat_event": "", "purpose": "", "auditor_comments": "",
                "filename": fname, "source": source, "status": status,
                "models_tried": model_name,
            }
            _write_csv(state, csv_path)
            n_err += 1
            continue

        if not retry:
            n_new += 1

    if _fig is not None and plt.fignum_exists(_fig.number):
        plt.close(_fig)

    print(f"\n{'─'*56}")
    print(f"  Processed      : {n_new} new  (skipped {already} existing)"
          + (f"  |  {n_errors} error(s) retried" if n_errors else ""))
    print(f"  Low confidence : {n_low}")
    if n_err:
        print(f"  Errors         : {n_err}")
    print(f"  CSV            : {csv_path}")
    print(f"  JSON           : {json_path}\n")


# ── debug single-image run ────────────────────────────────────────────────────
def run_debug(img_path: Path, model_key: str, quant: str,
              events: list[dict] | None = None, max_tokens: int = 2048):
    """Extract a single receipt and print results — does not touch any CSV/JSON."""
    model_name, extractor = MODELS[model_key]

    if not img_path.exists():
        sys.exit(f"Image not found: {img_path}")

    print(f"\n{'─'*56}")
    print(f"  DEBUG  mode — no CSV/JSON will be written")
    print(f"  Image  : {img_path}")
    print(f"  Model  : {model_name}")
    if model_key == "qwen":
        print(f"  Quant  : {quant}")
        print(f"  Tokens : max_new_tokens={max_tokens}")
    print(f"{'─'*56}\n")

    import time
    t0 = time.time()

    if model_key == "qwen":
        from receipt_pipeline import extract_qwen as _extract_qwen
        result = _extract_qwen(str(img_path), quant=quant, profile=True, dump_raw=True,
                               max_new_tokens=max_tokens)
    else:
        result = extractor(str(img_path))

    elapsed  = time.time() - t0
    receipts = result if isinstance(result, list) else [result]
    n_parts  = len(receipts)

    if n_parts > 1:
        print(f"\n  *** {n_parts} receipts detected in this image ***")

    for part_idx, r in enumerate(receipts, 1):
        r = _fill_items_fallback(r)
        low    = _is_low_confidence(r)
        status = "auto-low-confidence" if low else "auto"

        if n_parts > 1:
            print(f"\n{'─'*56}")
            print(f"  Receipt {part_idx} of {n_parts}")
        print(f"\n  store      : {r.store_name or '(not found)'}")
        print(f"  date       : {r.date       or '(not found)'}")
        matched = _match_event(r.date, events or [])
        if events:
            print(f"  event      : {matched or '(no match — date missing or unparseable)'}")
        guessed_purpose = _guess_purpose(r.store_name, r.items, matched)
        print(f"  purpose    : {guessed_purpose}")
        print(f"  total      : {r.total      or '(not found)'}")
        if r.items:
            guessed = all(it.get("guessed") for it in r.items)
            label   = "guessed" if guessed else "extracted"
            print(f"  items      : {len(r.items)} ({label})")
            for it in r.items[:10]:
                name  = textwrap.shorten(it.get("name", ""), width=32, placeholder="…")
                price = it.get("price", "")
                g     = " *" if it.get("guessed") else ""
                print(f"    •{g} {name:<32}  {price}")
            if len(r.items) > 10:
                print(f"    … and {len(r.items) - 10} more")
        if low:
            print(f"\n  [LOW CONFIDENCE — one or more fields missing]")

    print(f"\n  inference time : {elapsed:.1f}s")
    print(f"{'─'*56}\n")

    try:
        _show(str(img_path), receipts[0], 1, 1, status)
        input("  Press Enter to close …")
    except Exception:
        pass


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    model_list = "\n".join(
        f"  {k:<12} {v[0]}" + ("  (default)" if k == DEFAULT_MODEL else "")
        for k, v in MODELS.items()
    )

    parser = argparse.ArgumentParser(
        description="Batch receipt extraction — processes all receipts without user input.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Models:\n"
            f"{model_list}\n\n"
            "Status values written to CSV:\n"
            "  auto                  confident extraction (all 3 main fields found)\n"
            "  auto-low-confidence   one or more fields missing — review with audit_receipts\n\n"
            "Items:\n"
            "  Extracted from the model where possible. When the model returns none,\n"
            "  a best-effort item is guessed from the store name and total and marked\n"
            "  with [guessed] in the display. Items are saved to the JSON sidecar file.\n\n"
            "Examples:\n"
            "  python batch_receipts.py\n"
            "  python batch_receipts.py --model gemini\n"
            "  python batch_receipts.py --list\n"
            "  python batch_receipts.py -I ./images -O Results --model paddleocr\n"
        ),
    )
    parser.add_argument(
        "-I", "--input",
        default=DEFAULT_IMAGE_DIR,
        metavar="IMAGE_DIR",
        dest="image_dir",
        help=f"Directory of receipt images (default: {DEFAULT_IMAGE_DIR})",
    )
    parser.add_argument(
        "-O", "--output",
        default=str(CSV_PATH),
        metavar="OUTPUT",
        dest="output_csv",
        help=f"Output CSV file or directory (default: {CSV_PATH}). "
             "If a directory is given, writes extracted.csv inside it.",
    )
    parser.add_argument(
        "--model", "-m",
        default=DEFAULT_MODEL,
        choices=list(MODELS.keys()),
        metavar="MODEL",
        help=f"Extraction model to use (default: {DEFAULT_MODEL}). "
             f"Choices: {', '.join(MODELS.keys())}",
    )
    parser.add_argument(
        "--list", "-l",
        action="store_true",
        help="Print current CSV contents and exit (no processing)",
    )
    parser.add_argument(
        "--debug", "-d",
        metavar="IMAGE",
        help="Debug mode: run extraction on a single image file, no CSV/JSON written",
    )
    parser.add_argument(
        "--quant",
        choices=["8bit", "4bit", "none"],
        default="8bit",
        help="Qwen quantization level (only applies to --model qwen, default: 8bit)",
    )
    parser.add_argument(
        "--no-gpu",
        action="store_true",
        help="Force Qwen to run entirely on CPU (float32, no quantization). "
             "Very slow — useful for profiling baseline or when no GPU is available.",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=3,
        metavar="N",
        help="Stop after processing N receipts (default: 3; 0 = no limit)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=2048,
        metavar="N",
        dest="max_tokens",
        help="Max new tokens for Qwen generation (default: 2048). "
             "Increase for images with many receipts.",
    )
    parser.add_argument(
        "--events",
        metavar="EVENTS_FILE",
        help="CSV or JSON file listing events with names and dates. "
             "Each receipt's date is matched to the closest event and the "
             "saikat_event column is populated automatically. "
             "CSV must have columns: name (or event_name), date. "
             "JSON must be a list of {\"name\": ..., \"date\": ...} objects.",
    )

    args = parser.parse_args()
    csv_path  = Path(args.output_csv)
    # If the path is an existing directory, or has no .csv extension (treat as dir),
    # write the default filename inside it.
    if csv_path.is_dir() or csv_path.suffix.lower() != ".csv":
        csv_path = csv_path / "extracted.csv"
    image_dir = Path(args.image_dir)

    effective_quant = "cpu" if args.no_gpu else args.quant

    events = None
    if args.events:
        events_path = Path(args.events)
        if not events_path.exists():
            sys.exit(f"Events file not found: {events_path}")
        events = _load_events(events_path)
        print(f"Events   : {len(events)} loaded from {events_path}")

    if args.debug:
        run_debug(Path(args.debug), args.model, effective_quant, events=events,
                  max_tokens=args.max_tokens)
        return

    # Derive the JSON sidecar path alongside the CSV (e.g. extracted.csv → extracted_full.json)
    json_path = csv_path.parent / (csv_path.stem + "_full.json")

    if args.list:
        print_list(csv_path)
        return

    if not image_dir.exists():
        sys.exit(f"Image directory not found: {image_dir}")

    process_batch(image_dir, csv_path, json_path, args.model, events=events,
                  max_receipts=args.max, quant=effective_quant, max_tokens=args.max_tokens)


if __name__ == "__main__":
    main()
