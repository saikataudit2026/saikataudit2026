# Installation

## Requirements

- Python 3.11 or 3.12
- Linux (recommended — `bitsandbytes` quantization is Linux/CUDA only)
- NVIDIA GPU with ≥ 4 GB VRAM for Qwen2-VL-7B at 4-bit, ≥ 7 GB at 8-bit
  - CPU-only mode is available via `--no-gpu` but is very slow
- LibreOffice (for converting Word/RTF receipts to PDF before screenshotting)

---

## 1. Clone the repo

```bash
git clone <repo-url>
cd <repo-dir>
```

---

## 2. Create a virtual environment

```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

---

## 3. Install PyTorch with CUDA

Install the CUDA-enabled PyTorch wheel **before** running `pip install -r requirements.txt`.
Check your CUDA version with `nvidia-smi`, then pick the matching command from
[pytorch.org/get-started](https://pytorch.org/get-started/locally/).

Example for CUDA 12.1:
```bash
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
```

---

## 4. Install Python dependencies

```bash
pip install -r requirements.txt
```

> **Note on PaddleOCR:** The default `paddlepaddle` package is CPU-only.
> For GPU inference install `paddlepaddle-gpu` instead:
> ```bash
> pip install paddlepaddle-gpu -i https://www.paddlepaddle.org.cn/packages/stable/cu118/
> pip install paddleocr
> ```

---

## 5. Install LibreOffice (for Word/RTF receipts)

```bash
# Debian / Ubuntu
sudo apt install libreoffice

# Fedora / RHEL
sudo dnf install libreoffice
```

Only needed if your `Receipts/` folders contain `.docx`, `.doc`, or `.rtf` files.

---

## 6. Download model weights

Models are downloaded automatically on first use and cached in `~/.cache/huggingface/`.
Disk space requirements:

| Model | Size on disk |
|---|---|
| Qwen2-VL-7B-Instruct | ~15 GB |
| Donut (mychen76/invoices-and-receipts_donut_v1) | ~1.5 GB |
| PaddleOCR | ~200 MB |

Gemini uses the cloud API — no local weights needed.

---

## 7. Set the Gemini API key (optional)

Only required if you use `--model gemini`.

```bash
export GOOGLE_API_KEY="your-key-here"
```

Or add it to a `.env` file and source it before running.

---

## 8. Verify the installation

```bash
# Smoke-test PaddleOCR (CPU, no VRAM)
python batch_receipts.py --debug <path-to-any-receipt-image> --model paddleocr

# Smoke-test Qwen at 4-bit
python batch_receipts.py --debug <path-to-any-receipt-image> --model qwen --quant 4bit
```

---

## Files to check in to git

The following files belong in version control.
Do **not** commit the data directories, generated CSVs, or model weights.

```
# Source scripts
screenshot_function.py
create_expense_mapping.py
combine_excel_files.py
merge_receipt_data.py
batch_screenshot_processor.py
receipt_pipeline.py
batch_receipts.py
build_audit.py

# Docs
README.md
INSTALL.md
requirements.txt
.gitignore
```

A minimal `.gitignore` is shown in the next section.

---

## .gitignore

```gitignore
# Virtual environment
.venv/
__pycache__/
*.pyc

# Generated data files — re-created by running the pipeline
expense_receipt_mapping.csv
combined_expense_data.csv
merged_expense_data.csv
final_expense_data.csv
saikat_audit_details.csv
results/
screenshotoutput/

# Source data — large, not for version control
Expense details/
Receipt_2021/

# Model cache
*.bin
*.safetensors
modelfile

# Misc
*.jpg
*.png
x_p00*.jpg
```

> Adjust the gitignore to suit your project — if `events.csv` is hand-crafted
> and not regenerated, keep it in git.
