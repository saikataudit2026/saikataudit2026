#!/usr/bin/env python3
"""
Receipt extraction pipeline — human-in-the-loop, one model per run.

Each run tries ONE model per receipt (the next untried one), gets human
feedback, then moves on to the next receipt.  Rejected models are recorded
in the CSV so the next run automatically picks up with the next model.
This keeps only one model loaded at a time and avoids OOM.

Model order (simplest → most powerful):
  1. PaddleOCR + regex      (CPU, no VRAM)
  2. Donut mychen76          (GPU, ~500 MB VRAM)
  3. Qwen2-VL-7B             (GPU, ~14 GB VRAM)
  4. Google Gemini Flash     (cloud, free tier ~1500 req/day)
  5. Manual entry            (always available as fallback)

Navigation:
  [a] accept          save result, move to next receipt
  [e] edit            fix fields then save
  [n] next run        reject this model, record in CSV, try next model next run
  [m] manual          type all fields by hand
  [s] skip            permanently skip this receipt
  [q] quit            save progress and exit

CSV columns: filename, store_name, date, total, source, status, models_tried
  status       : accepted | pending | skipped
  models_tried : comma-separated list of rejected models, e.g. "PaddleOCR,Donut"

Set GEMINI_API_KEY env var (or place key in ~/.gemini_api_key) to enable Gemini.
"""

import argparse
import csv
import json
import os
import re
import sys
import textwrap
import time
import warnings
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

# Suppress noisy but harmless version-mismatch warnings from paddle and requests
warnings.filterwarnings("ignore", message="No ccache found")
warnings.filterwarnings("ignore", category=UserWarning, module="requests")

import matplotlib.gridspec as gridspec
import matplotlib.pyplot as plt
import torch
from PIL import Image

# ── paths ─────────────────────────────────────────────────────────────────────
DEFAULT_IMAGE_DIR = "./Receipt_2021/output_screenshot"
RESULTS_DIR = Path("./results")

DONUT_DIR = os.path.expanduser(
    "~/.cache/huggingface/hub"
    "/models--mychen76--invoice-and-receipts_donut_v1"
    "/snapshots/74debbfa6b7e3c534093bfc438ff9fc5c0aa8e1d"
)
QWEN_DIR = os.path.expanduser(
    "~/.cache/huggingface/hub"
    "/models--Qwen--Qwen2-VL-7B-Instruct"
    "/snapshots/eed13092ef92e448dd6875b2a00151bd3f7db0ac"
)

RESULTS_DIR.mkdir(parents=True, exist_ok=True)
CSV_PATH  = RESULTS_DIR / "extracted.csv"
JSON_PATH = RESULTS_DIR / "extracted_full.json"

CSV_FIELDS = [
    "store_name", "date", "total",
    "saikat_event",
    "purpose", "auditor_comments",
    "items",
    "filename", "source", "status", "models_tried",
]

# ── result dataclass ──────────────────────────────────────────────────────────
@dataclass
class Receipt:
    filename:          str  = ""
    store_name:        str  = ""
    date:              str  = ""
    total:             str  = ""
    items:             list = field(default_factory=list)
    source:            str  = ""
    skipped:           bool = False
    purpose:           str  = ""
    auditor_comments:  str  = ""
    raw_output:        str  = ""   # full model output — stored in JSON sidecar for debugging


# ── helpers ───────────────────────────────────────────────────────────────────
def _amount_re():
    return re.compile(r'\$?\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})|\d+\.\d{2})')

def _date_re():
    return re.compile(
        r'(\d{4}-\d{2}-\d{2}'
        r'|\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}'
        r'|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\.?\s+\d{1,2},?\s*\d{4}'
        r'|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4})',
        re.IGNORECASE,
    )

def parse_receipt_text(text: str) -> Receipt:
    """Extract store_name / date / total / items from raw OCR text."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    amt   = _amount_re()
    dtpat = _date_re()

    # store name — first line with ≥3 letters that isn't obviously metadata
    store_name = ""
    skip_words = {"receipt", "invoice", "screenshot", "filter", "transaction",
                  "statement", "posted", "subtotal", "total", "tax", "change"}
    for line in lines[:8]:
        low = line.lower()
        if any(w in low for w in skip_words):
            continue
        if len(re.findall(r'[A-Za-z]', line)) >= 3:
            store_name = line
            break

    # date — first match anywhere in doc
    date = ""
    for line in lines:
        m = dtpat.search(line)
        if m:
            date = m.group(1)
            break

    # total — last line containing "total" keyword + amount
    total = ""
    for line in reversed(lines):
        if re.search(r'\btotal\b', line, re.IGNORECASE):
            m = amt.search(line)
            if m:
                total = "$" + m.group(1).replace(",", "")
                break
    if not total:
        # fallback: largest dollar amount in document
        candidates = []
        for line in lines:
            m = amt.search(line)
            if m:
                try:
                    candidates.append(float(m.group(1).replace(",", "")))
                except ValueError:
                    pass
        if candidates:
            total = f"${max(candidates):.2f}"

    # items — any line ending with a dollar amount
    items = []
    item_re = re.compile(r'^(.{3,}?)\s+\$?(\d+\.\d{2})\s*[A-Z]?\s*$')
    # only exclude the receipt-level summary lines, keep everything else
    exclude = {"grand total", "balance due", "amount due", "change due",
               "visa tend", "cash tend", "amount tendered"}
    for line in lines:
        m = item_re.match(line)
        if m:
            name = m.group(1).strip()
            if not any(e in name.lower() for e in exclude):
                items.append({"name": name, "price": "$" + m.group(2)})

    return Receipt(store_name=store_name, date=date, total=total, items=items)


# ── extractor 1 : PaddleOCR ───────────────────────────────────────────────────
_paddle_ocr = None

def extract_paddleocr(img_path: str) -> Receipt:
    global _paddle_ocr
    if _paddle_ocr is None:
        from paddleocr import PaddleOCR
        _paddle_ocr = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
    result = _paddle_ocr.ocr(img_path, cls=True)
    lines  = [item[1][0] for page in result for item in page] if result else []
    text   = "\n".join(lines)
    r = parse_receipt_text(text)
    r.source = "PaddleOCR"
    return r


# ── extractor 2 : Donut ───────────────────────────────────────────────────────
_donut_processor = None
_donut_model     = None

def _load_donut():
    global _donut_processor, _donut_model
    if _donut_model is not None:
        return
    # unload Qwen if loaded
    _unload_qwen()
    from transformers import DonutProcessor, VisionEncoderDecoderModel
    print("  Loading Donut …")
    _donut_processor = DonutProcessor.from_pretrained(DONUT_DIR)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype  = torch.float16 if device == "cuda" else torch.float32
    _donut_model = VisionEncoderDecoderModel.from_pretrained(
        DONUT_DIR, torch_dtype=dtype
    ).to(device).eval()
    print(f"  Donut ready on {device}")

def extract_donut(img_path: str) -> Receipt:
    _load_donut()
    device = next(_donut_model.parameters()).device
    image  = Image.open(img_path).convert("RGB")

    pixel_values = _donut_processor(image, return_tensors="pt").pixel_values.to(device)
    if device.type == "cuda":
        pixel_values = pixel_values.half()

    dec_ids = _donut_processor.tokenizer(
        "<s_receipt>", add_special_tokens=False, return_tensors="pt"
    ).input_ids.to(device)

    with torch.no_grad():
        out = _donut_model.generate(
            pixel_values,
            decoder_input_ids=dec_ids,
            max_length=_donut_model.decoder.config.max_position_embeddings,
            pad_token_id=_donut_processor.tokenizer.pad_token_id,
            eos_token_id=_donut_processor.tokenizer.eos_token_id,
            use_cache=True, num_beams=1,
            bad_words_ids=[[_donut_processor.tokenizer.unk_token_id]],
        )

    seq = _donut_processor.batch_decode(out, skip_special_tokens=True)[0]
    try:
        d = _donut_processor.token2json(seq)
    except Exception:
        d = {}

    # map Donut field names → our standard names
    store = d.get("store_name") or d.get("seller") or d.get("header", {}).get("seller", "")
    date  = (d.get("date") or d.get("invoice_date")
             or d.get("header", {}).get("invoice_date", ""))
    total = d.get("total") or d.get("grand_total") or ""
    if total and not total.startswith("$"):
        total = "$" + total

    raw_items = d.get("line_items") or d.get("items") or []
    items = []
    for it in raw_items:
        name  = it.get("item_name") or it.get("item_desc") or ""
        price = it.get("item_value") or it.get("item_gross_worth") or ""
        if name:
            items.append({"name": name, "price": price})

    return Receipt(store_name=store, date=date, total=total,
                   items=items, source="Donut")


# ── extractor 3 : Qwen2-VL-7B ────────────────────────────────────────────────
_qwen_processor = None
_qwen_model     = None
_qwen_quant     = None   # tracks which quant level is currently loaded

def _unload_qwen():
    global _qwen_processor, _qwen_model, _qwen_quant
    if _qwen_model is not None:
        print("  Unloading Qwen from VRAM …")
        del _qwen_model, _qwen_processor
        _qwen_model = _qwen_processor = _qwen_quant = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

def _unload_donut():
    global _donut_processor, _donut_model
    if _donut_model is not None:
        print("  Unloading Donut from VRAM …")
        del _donut_model, _donut_processor
        _donut_model = _donut_processor = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

class _QwenProfiler:
    """
    Hooks into the live Qwen model for real-time progress and a final report.

    Per-layer timing (prefill pass):
      - Wall-clock time per layer (always)
      - CUDA-event GPU-only time per layer (GPU mode only)
      - Overhead % per layer = (wall - GPU) / wall  → PCIe transfer cost
      - Marks which layers are CPU-offloaded

    Phase summary:
      - Visual encoder, prefill total, decode total + tok/s
      - Overall GPU compute % vs non-GPU overhead (GPU mode)
      - CPU mode: wall-clock only — all time is CPU compute + DRAM
    """

    def __init__(self, model, on_cpu: bool = False):
        self.model        = model
        self.on_cpu       = on_cpu   # True → pure CPU run, no CUDA events
        self.hooks        = []
        self._ms          = {}       # phase → elapsed ms
        self._t           = {}       # phase → wall-clock start
        self._prefill_done = False
        self._n_tok       = 0
        self._gen_t0      = None

        # CUDA events for overall generate() GPU time (GPU mode only)
        self.ev_gen_start = None if on_cpu else torch.cuda.Event(enable_timing=True)
        self.ev_gen_end   = None if on_cpu else torch.cuda.Event(enable_timing=True)

        # Locate transformer layers
        layers = self._find_layers(model)
        self.n_layers  = len(layers)

        # Layers whose parameters are on CPU while the primary device is GPU.
        # In pure CPU mode every layer is on CPU by design — that's not "offloading",
        # so we leave the list empty to suppress the misleading PCIe-active message.
        if on_cpu:
            self.cpu_param_layers = []
        else:
            self.cpu_param_layers = [
                i for i, lyr in enumerate(layers)
                if any(p.device.type == "cpu" for p in lyr.parameters())
            ]

        # Per-layer timing storage (prefill pass only)
        self._lyr_wall_pre  = [0.0] * self.n_layers
        self._lyr_wall_post = [0.0] * self.n_layers
        if not on_cpu:
            self._lyr_ev_pre  = [torch.cuda.Event(enable_timing=True) for _ in layers]
            self._lyr_ev_post = [torch.cuda.Event(enable_timing=True) for _ in layers]
        else:
            self._lyr_ev_pre  = None
            self._lyr_ev_post = None

        self._install(layers)

    # ── layer discovery ────────────────────────────────────────────────────────

    @staticmethod
    def _find_layers(model):
        import torch.nn as nn
        for path in ["model.layers", "model.model.layers", "transformer.h",
                     "model.decoder.layers"]:
            obj = model
            try:
                for attr in path.split("."):
                    obj = getattr(obj, attr)
                if isinstance(obj, (nn.ModuleList, list)) and len(obj) > 0:
                    return list(obj)
            except AttributeError:
                continue
        # Fallback: largest ModuleList in the model
        best, best_n = [], 0
        for _, mod in model.named_modules():
            if isinstance(mod, nn.ModuleList) and len(mod) > best_n:
                best, best_n = list(mod), len(mod)
        return best

    # ── sync helper ────────────────────────────────────────────────────────────

    def _sync(self):
        if not self.on_cpu:
            torch.cuda.synchronize()

    # ── hook installation ──────────────────────────────────────────────────────

    def _install(self, layers):
        if hasattr(self.model, "visual"):
            self.hooks += [
                self.model.visual.register_forward_pre_hook(self._pre_visual),
                self.model.visual.register_forward_hook(self._post_visual),
            ]

        if not layers:
            print("  [profiler] WARNING: transformer layers not found — "
                  "layer-level hooks disabled")
            return

        for i, layer in enumerate(layers):
            self.hooks += [
                layer.register_forward_pre_hook(self._make_pre_layer(i)),
                layer.register_forward_hook(self._make_post_layer(i)),
            ]

    # ── hook callbacks ─────────────────────────────────────────────────────────

    def _pre_visual(self, mod, inp):
        self._sync()
        self._t["visual"] = time.perf_counter()
        print("  [visual]   encoding image patches ...", end=" ", flush=True)

    def _post_visual(self, mod, inp, out):
        self._sync()
        ms = (time.perf_counter() - self._t["visual"]) * 1000
        self._ms["visual"] = ms
        print(f"{ms:.0f} ms", flush=True)

    def _make_pre_layer(self, idx):
        def hook(mod, inp):
            if self._prefill_done:
                return
            try:
                hs = inp[0] if isinstance(inp, tuple) else inp
                if not (isinstance(hs, torch.Tensor) and hs.dim() >= 2
                        and hs.shape[1] > 1):
                    return   # decode step (seq_len=1) — skip
            except (IndexError, AttributeError):
                return

            if idx == 0:
                self._sync()
                self._t["prefill"] = time.perf_counter()
                n_cpu = len(self.cpu_param_layers)
                suffix = (f"  ({n_cpu} layers CPU-offloaded — PCIe active)"
                          if n_cpu else "")
                mode   = "CPU" if self.on_cpu else "GPU"
                print(f"  [prefill]  {self.n_layers} layers  [{mode}]{suffix}",
                      end="", flush=True)
            elif idx % 4 == 0:
                print(".", end="", flush=True)

            # Record layer start
            self._lyr_wall_pre[idx] = time.perf_counter()
            if not self.on_cpu:
                self._lyr_ev_pre[idx].record()

        return hook

    def _make_post_layer(self, idx):
        def hook(mod, inp, out):
            if self._prefill_done:
                return
            try:
                hs = inp[0] if isinstance(inp, tuple) else inp
                if not (isinstance(hs, torch.Tensor) and hs.dim() >= 2
                        and hs.shape[1] > 1):
                    return
            except (IndexError, AttributeError):
                return

            # Record layer end
            if not self.on_cpu:
                self._lyr_ev_post[idx].record()
            self._lyr_wall_post[idx] = time.perf_counter()

            if idx == self.n_layers - 1:
                self._sync()
                ms = (time.perf_counter() - self._t.get("prefill",
                       time.perf_counter())) * 1000
                self._ms["prefill"] = ms
                self._prefill_done  = True
                print(f"  {ms:.0f} ms", flush=True)
                print("  [decode]   generating ", end="", flush=True)
                self._gen_t0 = time.perf_counter()

        return hook

    def token_callback(self):
        self._n_tok += 1
        if self._gen_t0 and self._n_tok % 8 == 0:
            elapsed = time.perf_counter() - self._gen_t0
            speed   = self._n_tok / elapsed
            print(f"\r  [decode]   {self._n_tok} tokens  {speed:.1f} tok/s    ",
                  end="", flush=True)

    # ── cleanup & report ───────────────────────────────────────────────────────

    def remove(self):
        for h in self.hooks:
            h.remove()
        self.hooks.clear()

    def report(self, wall_s: float):
        if not self.on_cpu:
            torch.cuda.synchronize()

        # Finalise decode line
        if self._gen_t0:
            decode_s = time.perf_counter() - self._gen_t0
            self._ms["decode"] = decode_s * 1000
            speed = self._n_tok / decode_s if decode_s > 0 else 0
            print(f"\r  [decode]   {self._n_tok} tokens  {speed:.1f} tok/s  done       ",
                  flush=True)

        # Overall GPU time
        if not self.on_cpu and self.ev_gen_start:
            try:
                gpu_ms = self.ev_gen_start.elapsed_time(self.ev_gen_end)
            except RuntimeError:
                gpu_ms = None
        else:
            gpu_ms = None

        vis_ms     = self._ms.get("visual",  0)
        prefill_ms = self._ms.get("prefill", 0)
        decode_ms  = self._ms.get("decode",  0)
        total_ms   = wall_s * 1000

        sep = "─" * 62
        print(f"\n{sep}")
        print(f"  PROFILING REPORT  ({'CPU-only' if self.on_cpu else 'GPU'})")
        print(sep)

        # ── Phase summary ──────────────────────────────────────────────────
        print(f"  Phase summary (wall clock):")
        for label, ms in [("Visual encoder", vis_ms),
                          ("Prefill",         prefill_ms),
                          ("Decode",          decode_ms)]:
            pct = ms / total_ms * 100 if total_ms > 0 else 0
            bar = "█" * int(pct / 2)
            print(f"    {label:<16} {ms:8.1f} ms  {pct:5.1f}%  {bar}")
        print(f"    {'Total':<16} {total_ms:8.1f} ms")

        # ── GPU vs overhead (GPU mode only) ───────────────────────────────
        if gpu_ms is not None:
            overhead_ms  = max(0.0, total_ms - gpu_ms)
            overhead_pct = overhead_ms / total_ms * 100 if total_ms > 0 else 0
            gpu_pct      = gpu_ms / total_ms * 100 if total_ms > 0 else 0
            print(f"\n  Compute vs transfer (overall generate):")
            print(f"    GPU compute      {gpu_ms:8.1f} ms  {gpu_pct:5.1f}%")
            print(f"    Non-GPU overhead {overhead_ms:8.1f} ms  {overhead_pct:5.1f}%"
                  + ("  ← PCIe/CPU stalls" if overhead_pct > 15 else ""))
        else:
            print(f"\n  [CPU mode — all time is CPU compute + DRAM access, no PCIe]")

        # ── Per-layer prefill breakdown ────────────────────────────────────
        if self._prefill_done and self.n_layers > 0:
            print(f"\n  Per-layer timing (prefill pass):")

            if not self.on_cpu and self._lyr_ev_pre:
                # GPU mode: read CUDA events for GPU-only time per layer
                try:
                    torch.cuda.synchronize()
                    gpu_per_layer = [
                        self._lyr_ev_pre[i].elapsed_time(self._lyr_ev_post[i])
                        for i in range(self.n_layers)
                    ]
                except RuntimeError:
                    gpu_per_layer = None
            else:
                gpu_per_layer = None

            wall_per_layer = [
                (self._lyr_wall_post[i] - self._lyr_wall_pre[i]) * 1000
                for i in range(self.n_layers)
            ]

            if gpu_per_layer:
                print(f"    {'Layer':>5}  {'Wall ms':>8}  {'GPU ms':>8}  "
                      f"{'Overhead':>9}  {'Device':<6}")
                print(f"    {'─'*5}  {'─'*8}  {'─'*8}  {'─'*9}  {'─'*6}")
            else:
                print(f"    {'Layer':>5}  {'Wall ms':>8}  {'Device':<6}")
                print(f"    {'─'*5}  {'─'*8}  {'─'*6}")

            for i in range(self.n_layers):
                w   = wall_per_layer[i]
                dev = "CPU" if i in self.cpu_param_layers else ("CPU" if self.on_cpu else "GPU")
                if gpu_per_layer:
                    g    = gpu_per_layer[i]
                    ovhd = max(0.0, w - g)
                    pct  = ovhd / w * 100 if w > 0 else 0
                    flag = "  ← PCIe" if pct > 40 else ""
                    print(f"    {i:>5}  {w:>8.1f}  {g:>8.1f}  {pct:>8.0f}%  {dev}{flag}")
                else:
                    print(f"    {i:>5}  {w:>8.1f}  {dev}")

            # Summary stats
            total_layer_ms = sum(wall_per_layer)
            if wall_per_layer:
                slowest = sorted(enumerate(wall_per_layer), key=lambda x: -x[1])[:3]
                slow_str = ", ".join(f"layer {i} ({ms:.0f}ms)"
                                     for i, ms in slowest)
                print(f"\n    Avg per layer : {total_layer_ms / self.n_layers:.1f} ms")
                print(f"    Slowest layers: {slow_str}")

        # ── Decode throughput ──────────────────────────────────────────────
        if self._n_tok and decode_ms > 0:
            print(f"\n  Decode throughput: {self._n_tok} tokens"
                  f"  @  {self._n_tok / (decode_ms / 1000):.1f} tok/s")

        print(f"{sep}\n")


def _load_qwen(quant: str = "8bit"):
    """Load Qwen2-VL-7B with the specified quantization / device mode.

    quant: "8bit"  — ~7 GB VRAM, excellent quality (default)
           "4bit"  — ~4 GB VRAM, good quality, fastest
           "none"  — ~14 GB VRAM bfloat16, best quality, GPU+CPU auto-split
           "cpu"   — float32 on CPU only, no GPU required, slowest
    """
    global _qwen_processor, _qwen_model, _qwen_quant
    if _qwen_model is not None:
        if _qwen_quant == quant:
            return
        _unload_qwen()
    _unload_donut()
    from transformers import Qwen2VLForConditionalGeneration, AutoProcessor, BitsAndBytesConfig

    if quant == "8bit":
        bnb_cfg    = BitsAndBytesConfig(load_in_8bit=True)
        device_map = "cuda"
        dtype      = None          # BNB controls dtype
    elif quant == "4bit":
        bnb_cfg    = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
        )
        device_map = "cuda"
        dtype      = None
    elif quant == "cpu":           # pure CPU — bfloat16 (~14 GB RAM, half of float32's 28 GB)
        bnb_cfg    = None
        device_map = "cpu"
        dtype      = torch.bfloat16
        print("  WARNING: CPU mode requires ~14 GB RAM for bfloat16 weights.")
    else:  # "none" — bfloat16, let HF decide GPU/CPU split
        bnb_cfg    = None
        device_map = "auto"
        dtype      = None

    print(f"  Loading Qwen2-VL-7B [{quant}] (this takes ~30 s) …")
    _qwen_processor = AutoProcessor.from_pretrained(QWEN_DIR)
    _qwen_model = Qwen2VLForConditionalGeneration.from_pretrained(
        QWEN_DIR,
        quantization_config=bnb_cfg,
        device_map=device_map,
        torch_dtype=dtype,
    ).eval()
    _qwen_quant = quant
    print("  Qwen ready")

_QWEN_PROMPT = """\
Extract receipt data from this image and return ONLY valid JSON, no markdown.

STEP 1 — Before writing any JSON, scan the ENTIRE image and count how many separate receipts \
or transaction documents are visible. Look in all corners and edges. Each distinct store name \
or document header counts as one receipt.

STEP 2 — Return one JSON object per receipt found.

If there is ONE receipt:
{"store_name":"...","date":"...","total":"...","items":[{"name":"...","price":"..."}]}

If there are MULTIPLE separate receipts in the image, return a JSON array — one object per receipt:
[{"store_name":"...","date":"...","total":"...","items":[...]}, {"store_name":"...","date":"...","total":"...","items":[...]}]

Rules:
- store_name : business or store name (e.g. "Costco Wholesale", "Sprouts Farmers Market")
- date       : transaction date (MM/DD/YYYY preferred)
- total      : final amount paid — look for TOTAL, GRAND TOTAL, AMOUNT DUE, or SUBTOTAL at the bottom.
               For Costco/warehouse receipts the last amount before TAX is typically the subtotal.
- items      : REQUIRED — list EVERY line item with its price. Include subtotal, tax, fees.
               Costco items start with a long item number (e.g. "1234567 CHICKEN BREAST") — include them.
               If this is a bank/card statement with circled rows, list each circled transaction.
               Do NOT leave items empty — make your best effort even if text is unclear.
- Do NOT merge multiple receipts into one object. Each receipt must be a separate entry in the array.
- Do NOT stop early — make sure every receipt you identified in STEP 1 appears in the output.

If any amounts or rows are circled or highlighted, extract those specifically.
"""

def extract_qwen(img_path: str, quant: str = "8bit", profile: bool = False,
                 dump_raw: bool = False,
                 max_new_tokens: int = 2048) -> "Receipt | list[Receipt]":
    global _qwen_model, _qwen_quant
    _load_qwen(quant)
    image = Image.open(img_path).convert("RGB")
    messages = [{"role": "user", "content": [
        {"type": "image"},
        {"type": "text", "text": _QWEN_PROMPT},
    ]}]
    text   = _qwen_processor.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    inputs = _qwen_processor(
        text=[text], images=[image], return_tensors="pt"
    ).to(next(_qwen_model.parameters()).device)

    prof       = _QwenProfiler(_qwen_model, on_cpu=(quant == "cpu")) if profile else None
    gen_kwargs = {"max_new_tokens": max_new_tokens}

    if prof:
        from transformers import LogitsProcessorList, LogitsProcessor as _LP
        class _TokCounter(_LP):
            def __call__(self_, input_ids, scores):
                prof.token_callback()
                return scores
        gen_kwargs["logits_processor"] = LogitsProcessorList([_TokCounter()])
        if prof.ev_gen_start:
            prof.ev_gen_start.record()

    t0 = time.perf_counter()
    try:
        with torch.no_grad():
            out_ids = _qwen_model.generate(**inputs, **gen_kwargs)
        wall_s = time.perf_counter() - t0
        if prof and prof.ev_gen_end:
            prof.ev_gen_end.record()
    except torch.OutOfMemoryError:
        wall_s = time.perf_counter() - t0
        print("\n  [OOM] GPU out of memory — clearing cache and retrying on CPU …", flush=True)
        # Drop GPU tensors and free memory
        del inputs
        import gc; gc.collect()
        torch.cuda.empty_cache()
        # Tear down the GPU model so _load_qwen will reload fresh on CPU
        if prof:
            try: prof.remove()
            except Exception: pass
            prof = None
            gen_kwargs.pop("logits_processor", None)
        _qwen_model = None
        _qwen_quant = None
        _load_qwen("cpu")
        # Re-prepare inputs on CPU (no .to() needed — model is on CPU)
        inputs = _qwen_processor(text=[text], images=[image], return_tensors="pt")
        t0 = time.perf_counter()
        with torch.no_grad():
            out_ids = _qwen_model.generate(**inputs, **gen_kwargs)
        wall_s = time.perf_counter() - t0
        print(f"  [CPU fallback complete — {wall_s:.1f}s]", flush=True)

    trimmed = [o[len(i):] for i, o in zip(inputs.input_ids, out_ids)]
    raw     = _qwen_processor.batch_decode(trimmed, skip_special_tokens=True)[0]

    # strip markdown fences if present
    raw = re.sub(r"```(?:json)?|```", "", raw).strip()

    if dump_raw:
        sep = "─" * 60
        print(f"\n{sep}")
        print("  RAW MODEL OUTPUT:")
        print(sep)
        print(raw)
        print(sep + "\n")

    def _dict_to_receipt_qwen(d: dict, source: str) -> Receipt:
        total = d.get("total", "")
        if total and not str(total).startswith("$"):
            total = "$" + str(total)
        items = [{"name": it.get("name", ""), "price": it.get("price", "")}
                 for it in d.get("items", []) if isinstance(it, dict)]
        return Receipt(store_name=d.get("store_name", ""), date=d.get("date", ""),
                       total=total, items=items, source=source, raw_output=raw)

    try:
        d = json.loads(raw)
        if isinstance(d, list):
            # Model returned multiple receipts — return all of them.
            receipt_dicts = [x for x in d if isinstance(x, dict)]
            if prof:
                prof.report(wall_s)
                prof.remove()
            return [_dict_to_receipt_qwen(rd, "Qwen2-VL-7B[MULTI]")
                    for rd in receipt_dicts]
    except json.JSONDecodeError:
        # Output was likely truncated — extract whatever top-level fields are intact.
        d = {}
        for key in ("store_name", "date", "total"):
            m = re.search(rf'"{key}"\s*:\s*"([^"]*)"', raw)
            if m:
                d[key] = m.group(1)
        items_matches = re.findall(r'\{[^{}]*"name"\s*:[^{}]*\}', raw, re.DOTALL)
        if items_matches:
            parsed_items = []
            for s in items_matches:
                try:
                    parsed_items.append(json.loads(s))
                except json.JSONDecodeError:
                    pass
            if parsed_items:
                d["items"] = parsed_items

    if prof:
        prof.report(wall_s)
        prof.remove()

    return _dict_to_receipt_qwen(d, "Qwen2-VL-7B")


# ── extractor 4 : Gemini Flash ────────────────────────────────────────────────
_gemini_client = None

def _get_gemini_client():
    global _gemini_client
    if _gemini_client is not None:
        return _gemini_client

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        key_file = Path("~/.gemini_api_key").expanduser()
        if key_file.exists():
            api_key = key_file.read_text().strip()
    if not api_key:
        print("\n  Gemini API key not found.")
        print("  Set env var GEMINI_API_KEY  or place key in ~/.gemini_api_key")
        api_key = input("  Paste your Gemini API key (or Enter to skip): ").strip()
    if not api_key:
        return None

    from google import genai
    _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client

_GEMINI_PROMPT = """\
Extract receipt data from this image and return ONLY valid JSON (no markdown).

STEP 1 — Before writing any JSON, scan the ENTIRE image and count how many separate receipts \
or transaction documents are visible. Look in all corners and edges. Each distinct store name \
or document header counts as one receipt.

STEP 2 — Return one JSON object per receipt found.

If there is ONE receipt:
{"store_name":"...","date":"...","total":"...","items":[{"name":"...","price":"..."}]}

If there are MULTIPLE separate receipts in the image, return a JSON array — one object per receipt:
[{"store_name":"...","date":"...","total":"...","items":[...]}, {"store_name":"...","date":"...","total":"...","items":[...]}]

Rules:
- store_name : business or store name (e.g. "Costco Wholesale", "Sprouts Farmers Market")
- total   : final amount paid — look for TOTAL, GRAND TOTAL, AMOUNT DUE, or SUBTOTAL at the bottom.
            For Costco/warehouse receipts the last amount before TAX is typically the subtotal.
- date    : MM/DD/YYYY format if possible
- items   : REQUIRED — list every line item with name and price. Include subtotal and tax rows.
            Costco items start with a long item number (e.g. "1234567 CHICKEN BREAST") — include them.
            For bank/card statements, list each circled or highlighted transaction as an item.
            Do NOT return an empty items list — always make your best effort.
- Do NOT merge multiple receipts into one object. Each receipt must be a separate entry in the array.
- Do NOT stop early — make sure every receipt you identified in STEP 1 appears in the output.
- If amounts are circled or highlighted, those are the key transactions to extract.
"""

def extract_gemini(img_path: str) -> "Receipt | list[Receipt]":
    client = _get_gemini_client()
    if client is None:
        raise RuntimeError("Gemini API key not available")

    from google.genai import types
    with open(img_path, "rb") as f:
        img_bytes = f.read()

    suffix = Path(img_path).suffix.lower()
    mime   = "image/png" if suffix == ".png" else "image/jpeg"

    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=[
            types.Part.from_bytes(data=img_bytes, mime_type=mime),
            _GEMINI_PROMPT,
        ],
    )
    raw = response.text.strip()
    raw = re.sub(r"```(?:json)?|```", "", raw).strip()

    def _dict_to_receipt_gemini(d: dict, source: str) -> Receipt:
        total = d.get("total", "")
        if total and not str(total).startswith("$"):
            total = "$" + str(total)
        items = [{"name": it.get("name", ""), "price": it.get("price", "")}
                 for it in d.get("items", []) if isinstance(it, dict)]
        return Receipt(store_name=d.get("store_name", ""), date=d.get("date", ""),
                       total=total, items=items, source=source)

    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        d = json.loads(m.group()) if m else {}

    if isinstance(d, list):
        # Model returned multiple receipts — return all of them.
        return [_dict_to_receipt_gemini(rd, "Gemini-2.0-Flash[MULTI]")
                for rd in d if isinstance(rd, dict)]

    return _dict_to_receipt_gemini(d, "Gemini-2.0-Flash")


# ── display ───────────────────────────────────────────────────────────────────
MODEL_COLORS = {
    "PaddleOCR":       "#2ecc71",   # green  — fast/local
    "Donut":           "#f39c12",   # orange — medium
    "Qwen2-VL-7B":     "#e74c3c",   # red    — heavy
    "Gemini-2.0-Flash":"#3498db",   # blue   — cloud
    "Manual":          "#9b59b6",   # purple — human
}

def _fmt_result(r: Receipt, attempt: int, total_models: int) -> str:
    color_tag = f"  [{r.source}  •  attempt {attempt}/{total_models}]"
    sep = "─" * 42
    lines = [
        color_tag,
        sep,
        f"  Store :  {r.store_name or '(not found)'}",
        f"  Date  :  {r.date       or '(not found)'}",
        f"  Total :  {r.total      or '(not found)'}",
        sep,
    ]
    if r.items:
        lines.append(f"  Items ({len(r.items)}):")
        for it in r.items[:12]:
            name  = textwrap.shorten(it.get("name",""), width=28, placeholder="…")
            price = it.get("price","")
            lines.append(f"    • {name:<28}  {price}")
        if len(r.items) > 12:
            lines.append(f"    … and {len(r.items)-12} more")
    else:
        lines.append("  Items :  (none extracted)")
    lines.append(sep)
    lines.append(f"  Purpose  :  {r.purpose or '(to be entered)'}")
    lines.append(f"  Auditor  :  {r.auditor_comments or '(to be entered)'}")
    lines.append(sep)
    lines.append("")
    lines.append("  [a] accept   [e] edit    [t] try next model now")
    lines.append("  [n] next run [m] manual  [s] skip  [q] quit")
    return "\n".join(lines)


_fig = None
_ax_img = None
_ax_txt = None

def _init_figure():
    global _fig, _ax_img, _ax_txt
    plt.ion()
    _fig = plt.figure(figsize=(15, 8))
    gs   = gridspec.GridSpec(1, 2, figure=_fig, width_ratios=[1, 1], wspace=0.04)
    _ax_img = _fig.add_subplot(gs[0])
    _ax_txt = _fig.add_subplot(gs[1])
    _ax_img.axis("off")
    _ax_txt.axis("off")

def show_receipt(img_path: str, r: Receipt, receipt_idx: int,
                 receipt_total: int, model_attempt: int, n_models: int):
    global _fig, _ax_img, _ax_txt
    if _fig is None or not plt.fignum_exists(_fig.number):
        _init_figure()

    fname = Path(img_path).name
    _fig.suptitle(
        f"Receipt {receipt_idx}/{receipt_total}  —  {fname}",
        fontsize=9, y=0.99,
    )

    # image panel
    _ax_img.cla()
    _ax_img.imshow(Image.open(img_path))
    _ax_img.axis("off")

    # text panel
    _ax_txt.cla()
    _ax_txt.axis("off")
    txt   = _fmt_result(r, model_attempt, n_models)
    color = MODEL_COLORS.get(r.source, "#ecf0f1")
    _ax_txt.text(
        0.03, 0.97, txt,
        transform=_ax_txt.transAxes,
        fontsize=8.5, verticalalignment="top", fontfamily="monospace",
        bbox=dict(boxstyle="round,pad=0.6", facecolor=color, alpha=0.18),
    )

    _fig.canvas.draw()
    _fig.canvas.flush_events()
    plt.pause(0.05)


# ── human-in-the-loop edit / manual entry ────────────────────────────────────
def _input_items() -> list:
    """Prompt user to enter items line by line until a blank line."""
    print("    Enter items as  Name  $Price  (blank line to finish):")
    items = []
    while True:
        line = input("      > ").strip()
        if not line:
            break
        m = re.match(r'^(.+?)\s+\$?(\d+\.\d{2})\s*$', line)
        if m:
            items.append({"name": m.group(1).strip(), "price": "$" + m.group(2)})
        else:
            items.append({"name": line, "price": ""})
    return items


def _edit_items(r: Receipt) -> Receipt:
    """Show current items, offer keep / replace / clear."""
    if r.items:
        print(f"\n  Items ({len(r.items)} extracted):")
        for i, it in enumerate(r.items[:15], 1):
            print(f"    {i:>2}. {it.get('name',''):<32}  {it.get('price','')}")
        if len(r.items) > 15:
            print(f"    … and {len(r.items)-15} more")
    else:
        print("\n  Items: (none extracted)")

    while True:
        choice = input("  Items: [Enter]=keep  [r]=replace all  [c]=clear : ").strip().lower()
        if choice in ("", "r", "c"):
            break
        print("  Use Enter, r, or c")

    if choice == "r":
        r.items = _input_items()
    elif choice == "c":
        r.items = []
    return r


def _ask_audit_fields(r: Receipt) -> Receipt:
    """Prompt for mandatory purpose and auditor decision."""
    print()
    while True:
        purpose = input("  Purpose of expense (required): ").strip()
        if purpose:
            break
        print("  Purpose cannot be empty.")
    r.purpose = purpose

    while True:
        decision = input("  Auditor: [a]=Accept  [q]=Have questions : ").strip().lower()
        if decision in ("a", "q"):
            break
        print("  Enter a or q")
    r.auditor_comments = "Accept" if decision == "a" else "Have questions"
    return r


def edit_result(r: Receipt) -> Receipt:
    print("\n  Edit fields — press Enter to keep current value")
    store = input(f"  Store name [{r.store_name}]: ").strip()
    date  = input(f"  Date       [{r.date}]: ").strip()
    total = input(f"  Total      [{r.total}]: ").strip()
    r.store_name = store or r.store_name
    r.date       = date  or r.date
    r.total      = total or r.total
    r.source     = r.source + "+edited"
    r = _edit_items(r)
    return r


def manual_entry(filename: str) -> Receipt:
    print("\n  Manual entry")
    store = input("  Store name : ").strip()
    date  = input("  Date       : ").strip()
    total = input("  Total ($)  : ").strip()
    if total and not total.startswith("$"):
        total = "$" + total
    r = Receipt(filename=filename, store_name=store, date=date,
                total=total, items=[], source="Manual")
    print("  Items (optional):")
    r.items = _input_items()
    return r


# ── results persistence ───────────────────────────────────────────────────────
def _load_state() -> dict:
    """Return {filename: row_dict} for every row currently in the CSV."""
    state = {}
    if CSV_PATH.exists():
        with open(CSV_PATH, newline="") as f:
            for row in csv.DictReader(f):
                state[row["filename"]] = row
    return state

def _write_csv(state: dict):
    """Rewrite the entire CSV from the in-memory state dict."""
    with open(CSV_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(state.values())

def _upsert_json(r: Receipt, status: str, models_tried: list):
    existing = {}
    if JSON_PATH.exists():
        try:
            for entry in json.loads(JSON_PATH.read_text()):
                existing[entry["filename"]] = entry
        except (json.JSONDecodeError, KeyError):
            pass
    entry = asdict(r)
    entry["status"] = status
    entry["models_tried"] = models_tried
    existing[r.filename] = entry
    JSON_PATH.write_text(json.dumps(list(existing.values()), indent=2))

def save_state(r: Receipt, status: str, models_tried: list):
    """Upsert one row in both CSV and JSON."""
    state = _load_state()
    state[r.filename] = {
        "filename":          r.filename,
        "store_name":        r.store_name,
        "date":              r.date,
        "total":             r.total,
        "source":            r.source,
        "status":            status,
        "models_tried":      ",".join(models_tried),
        "purpose":           r.purpose,
        "auditor_comments":  r.auditor_comments,
    }
    _write_csv(state)
    _upsert_json(r, status, models_tried)


# ── pipeline ──────────────────────────────────────────────────────────────────
EXTRACTORS = [
    ("PaddleOCR",        extract_paddleocr),
    ("Donut",            extract_donut),
    ("Qwen2-VL-7B",      extract_qwen),
    ("Gemini-2.0-Flash", extract_gemini),
]
N_MODELS = len(EXTRACTORS)


def _next_model(models_tried: list):
    """Return (name, extractor) for the first model not yet tried, or None."""
    tried = set(models_tried)
    return next(((n, fn) for n, fn in EXTRACTORS if n not in tried), None)


def process_receipt(img_path: str, idx: int, total: int,
                    models_tried: list) -> Optional[dict]:
    """
    Try models one at a time for this receipt.  [t] unloads the current model
    and immediately tries the next one without saving a pending row.

    Returns {"cmd": str, "receipt": Receipt, "models_tried": list}
    where models_tried is the fully updated list for this receipt.
    Returns None when the user presses [q] (caller should exit the main loop).
    """
    fname         = Path(img_path).name
    session_tried = list(models_tried)   # grows as models are rejected in-session

    while True:
        attempt = len(session_tried) + 1
        pending = _next_model(session_tried)

        if pending is None:
            # All models exhausted — fall straight to manual entry
            print(f"\n  [{idx}/{total}] {fname}")
            print(f"  All {N_MODELS} models tried ({', '.join(session_tried)}).")
            print("  Falling back to manual entry.")
            r = manual_entry(fname)
            r = _ask_audit_fields(r)
            return {"cmd": "a", "receipt": r, "models_tried": session_tried}

        name, extractor = pending
        tried_note = (f"  (tried: {', '.join(session_tried)})" if session_tried else "")
        print(f"\n  [{idx}/{total}] {fname}")
        print(f"  Model {attempt}/{N_MODELS}: {name}{tried_note} …", end=" ", flush=True)

        try:
            r = extractor(img_path)
            r.filename = fname
            print("done")
        except Exception as ex:
            print(f"FAILED ({ex})")
            r = Receipt(filename=fname, source=name)

        show_receipt(img_path, r, idx, total, attempt, N_MODELS)
        print(f"  Store: {r.store_name or '?'}  |  Date: {r.date or '?'}  |  Total: {r.total or '?'}")

        while True:
            cmd = input(
                "  > [a]ccept  [e]dit  [t]ry next  [n]ext run  [m]anual  [s]kip  [q]uit : "
            ).strip().lower()
            if cmd in ("a", "e", "t", "n", "m", "s", "q"):
                break
            print("  Unrecognised — use a / e / t / n / m / s / q")

        if cmd == "q":
            if _fig is not None and plt.fignum_exists(_fig.number):
                plt.close(_fig)
            return None

        if cmd == "t":
            # Unload current model and immediately try the next one
            session_tried.append(name)
            _unload_donut()
            _unload_qwen()
            next_pending = _next_model(session_tried)
            if next_pending:
                print(f"  → {name} skipped. Loading {next_pending[0]} …")
            else:
                print(f"  → {name} skipped. No more models — falling back to manual.")
            continue   # re-enter the while loop

        if cmd == "e":
            r = edit_result(r)
            cmd = "a"
        elif cmd == "m":
            r = manual_entry(fname)
            cmd = "a"

        if cmd == "a":
            r = _ask_audit_fields(r)
            updated_tried = session_tried + [name]
            return {"cmd": "a", "receipt": r, "models_tried": updated_tried}

        # cmd in ("n", "s")
        session_tried.append(name)
        return {"cmd": cmd, "receipt": r, "models_tried": session_tried}


# ── list ──────────────────────────────────────────────────────────────────────
def print_list():
    state = _load_state()
    if not state:
        print("\n  No rows in CSV yet.\n")
        return

    # column widths
    W_file  = max(len(r["filename"])   for r in state.values())
    W_store = max(len(r["store_name"]) for r in state.values())
    W_date  = max(len(r["date"])       for r in state.values())
    W_total = max(len(r["total"])      for r in state.values())
    # cap to reasonable maxima
    W_file  = min(W_file,  44)
    W_store = min(W_store, 28)
    W_date  = min(W_date,  12)
    W_total = min(W_total,  9)

    header = (
        f"  {'#':<4} "
        f"{'Filename':<{W_file}}  "
        f"{'Store':<{W_store}}  "
        f"{'Date':<{W_date}}  "
        f"{'Total':>{W_total}}  "
        f"{'Status':<9}  "
        f"Models tried"
    )
    sep = "  " + "─" * (len(header) - 2)

    print(f"\n{sep}")
    print(header)
    print(sep)

    STATUS_MARK = {"accepted": "✓", "pending": "…", "skipped": "–"}
    for i, (fname, row) in enumerate(sorted(state.items()), start=1):
        mark  = STATUS_MARK.get(row["status"], "?")
        fname_t = row["filename"][:W_file]
        store_t = row["store_name"][:W_store]
        date_t  = row["date"][:W_date]
        total_t = row["total"][:W_total]
        tried   = row.get("models_tried", "") or "—"
        print(
            f"  {i:<4} "
            f"{fname_t:<{W_file}}  "
            f"{store_t:<{W_store}}  "
            f"{date_t:<{W_date}}  "
            f"{total_t:>{W_total}}  "
            f"{mark} {row['status']:<8}  "
            f"{tried}"
        )

    print(f"{sep}")
    print(f"  {len(state)} rows total\n")


# ── summary ───────────────────────────────────────────────────────────────────
def print_summary(image_dir: str):
    valid_ext = {".jpg", ".jpeg", ".png", ".bmp", ".gif"}
    all_files = sorted(
        p for p in Path(image_dir).iterdir()
        if p.suffix.lower() in valid_ext
    )
    state    = _load_state()
    accepted = {k: v for k, v in state.items() if v["status"] == "accepted"}
    pending  = {k: v for k, v in state.items() if v["status"] == "pending"}
    skipped  = {k: v for k, v in state.items() if v["status"] == "skipped"}
    not_started = [f for f in all_files if f.name not in state]

    W = 52
    print(f"\n{'━'*W}")
    print(f"  Receipt Processing Summary")
    print(f"{'━'*W}")
    print(f"  Image directory  : {image_dir}")
    print(f"  Results CSV      : {CSV_PATH}")
    print(f"{'─'*W}")
    print(f"  Total files      : {len(all_files)}")
    print(f"  Accepted         : {len(accepted)}")

    src_counts = Counter(v["source"] for v in accepted.values())
    for src, count in sorted(src_counts.items(), key=lambda x: -x[1]):
        print(f"    {'':2}{src:<22} {count:>3}")

    print(f"  Pending          : {len(pending)}  (waiting for next model)")
    print(f"  Skipped          : {len(skipped)}")
    print(f"  Not started      : {len(not_started)}")
    print(f"{'─'*W}")

    if pending:
        print(f"\n  Pending — next model to try:")
        for fname, row in sorted(pending.items()):
            tried_list = [m for m in row.get("models_tried", "").split(",") if m]
            nxt        = _next_model(tried_list)
            next_name  = nxt[0] if nxt else "Manual"
            tried_str  = ", ".join(tried_list) if tried_list else "none"
            print(f"    {fname[:42]:<42}  tried: {tried_str}")
            print(f"    {'':42}  next : {next_name}")

    if not_started:
        print(f"\n  Not started ({len(not_started)}):")
        for f in not_started:
            print(f"    {f.name}")

    print(f"\n{'━'*W}\n")


# ── main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Receipt extraction pipeline — human-in-the-loop"
    )
    parser.add_argument(
        "--image-dir", default=DEFAULT_IMAGE_DIR, metavar="PATH",
        help=f"Directory containing receipt scans (default: {DEFAULT_IMAGE_DIR})",
    )
    parser.add_argument(
        "--summary", action="store_true",
        help="Print a summary of the CSV and exit (no interactive processing)",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="Print all rows currently in the CSV with their values and exit",
    )
    args = parser.parse_args()

    if args.summary:
        print_summary(args.image_dir)
        return

    if args.list:
        print_list()
        return

    valid_ext = {".jpg", ".jpeg", ".png", ".bmp", ".gif"}
    files = sorted(
        p for p in Path(args.image_dir).iterdir()
        if p.suffix.lower() in valid_ext
    )
    if not files:
        sys.exit(f"No images found in {args.image_dir}")

    total = len(files)

    # Startup summary
    state     = _load_state()
    n_done    = sum(1 for r in state.values() if r["status"] in ("accepted", "skipped"))
    n_pending = sum(1 for r in state.values() if r["status"] == "pending")
    n_new     = total - len(state)
    print(f"\nFound {total} receipts  |  {n_done} done  |  {n_pending} pending  |  {n_new} not started")
    print(f"Results → {CSV_PATH}  &  {JSON_PATH}\n")

    for idx, img_path in enumerate(files, start=1):
        # Re-read CSV each iteration so concurrent workers are respected
        row = _load_state().get(img_path.name)

        if row and row["status"] in ("accepted", "skipped"):
            print(f"  [{idx}/{total}] {img_path.name}  — {row['status']}, skipping")
            continue

        # Recover which models have already been tried for this receipt
        models_tried = [m for m in (row or {}).get("models_tried", "").split(",") if m]

        outcome = process_receipt(str(img_path), idx, total, models_tried)

        if outcome is None:           # user pressed [q]
            print("\nSaved progress. Exiting.")
            sys.exit(0)

        cmd          = outcome["cmd"]
        r            = outcome["receipt"]
        updated_tried = outcome["models_tried"]

        if cmd == "a":
            save_state(r, "accepted", updated_tried)
            print(f"  ✓ accepted ({r.source})\n")
        elif cmd == "n":
            # Rejected — save pending so next run tries the next model
            placeholder = Receipt(filename=img_path.name, source="pending")
            save_state(placeholder, "pending", updated_tried)
            next_up = _next_model(updated_tried)
            next_name = next_up[0] if next_up else "Manual"
            tried_str  = ", ".join(updated_tried) if updated_tried else "none"
            print(f"  → Rejected ({tried_str}). Next run will try: {next_name}\n")
        elif cmd == "s":
            placeholder = Receipt(filename=img_path.name, source="skipped", skipped=True)
            save_state(placeholder, "skipped", updated_tried)
            print(f"  → Skipped\n")

    print(f"\nDone. Results in {RESULTS_DIR}/")


if __name__ == "__main__":
    main()
