#!/usr/bin/env python3
"""
Screenshot function for opening files with appropriate applications and capturing screenshots
"""

import os
import subprocess
import time
import threading
from pathlib import Path
import pyautogui
from PIL import Image
import psutil
import sys
import numpy as np

_keepalive_stop = threading.Event()

def _keepalive_worker(interval: int = 20):
    """
    Background thread: every `interval` seconds simulate a harmless Shift keypress
    and a tiny mouse nudge (+1/-1 px) to reset the idle timer and prevent screen blanking.
    Requires xdotool (sudo apt install xdotool).
    """
    while not _keepalive_stop.wait(timeout=interval):
        try:
            # Nudge mouse by +1 then immediately back to avoid visible movement
            subprocess.run(['xdotool', 'mousemove_relative', '--', '1', '0'],
                           check=False, capture_output=True)
            subprocess.run(['xdotool', 'mousemove_relative', '--', '-1', '0'],
                           check=False, capture_output=True)
            # Also send a harmless keypress as a belt-and-braces reset
            subprocess.run(['xdotool', 'key', 'shift'],
                           check=False, capture_output=True)
        except FileNotFoundError:
            pass  # xdotool not installed — silent no-op

def start_keepalive(interval: int = 20):
    """Start the keep-alive background thread."""
    _keepalive_stop.clear()
    t = threading.Thread(target=_keepalive_worker, args=(interval,), daemon=True)
    t.start()
    return t

def stop_keepalive():
    """Signal the keep-alive thread to stop and wait for it to exit."""
    _keepalive_stop.set()


def get_application_for_file(file_path):
    """Determine the appropriate application to open a file based on its extension"""
    file_extension = os.path.splitext(file_path)[1].lower()
    
    # Application mapping for different file types
    app_mapping = {
        '.pdf': 'evince',  # PDF viewer
        '.docx': 'libreoffice',  # Document editor
        '.jpg': 'eog',  # Image viewer
        '.jpeg': 'eog',
        '.png': 'eog',
        '.gif': 'eog',
        '.heic': 'eog',
        '.doc': 'libreoffice',
        '.txt': 'gedit',  # Text editor
        '.rtf': 'libreoffice'
    }
    
    # Try specific application first, fall back to xdg-open
    app = app_mapping.get(file_extension, 'xdg-open')
    return app

def wait_for_application_window(app_name, timeout=25):
    """Wait for application window to appear and return window info"""
    start_time = time.time()
    
    while time.time() - start_time < timeout:
        # Get all running processes
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                if app_name.lower() in proc.info['name'].lower():
                    # Application is running, wait a bit more for window to appear
                    time.sleep(4)
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        
        time.sleep(1)
    
    return False

def close_application(app_name):
    """Close the application by killing its process"""
    try:
        # Special handling for LibreOffice which has multiple processes
        if app_name.lower() == 'libreoffice':
            # Kill all LibreOffice related processes
            libreoffice_processes = ['soffice', 'soffice.bin', 'libreoffice', 'libreoffice-calc', 
                                   'libreoffice-writer', 'libreoffice-impress', 'libreoffice-draw']
            
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    proc_name = proc.info['name'].lower()
                    proc_cmdline = ' '.join(proc.info.get('cmdline', [])).lower() if proc.info.get('cmdline') else ''
                    
                    # Check if this is a LibreOffice process
                    if any(libre_name in proc_name for libre_name in ['soffice', 'libreoffice']) or \
                       any(libre_name in proc_cmdline for libre_name in ['soffice', 'libreoffice']):
                        proc.kill()
                        print(f"Closed LibreOffice process: {proc.info['name']}")
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass
        else:
            # Standard process closing for other applications
            for proc in psutil.process_iter(['pid', 'name']):
                try:
                    if app_name.lower() in proc.info['name'].lower():
                        proc.kill()
                        print(f"Closed application: {proc.info['name']}")
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass
    except Exception as e:
        print(f"Error closing application: {e}")

def is_image_black(image, threshold=10):
    """
    Check if an image is predominantly black (likely a failed screenshot)
    
    Args:
        image: PIL Image object
        threshold: Pixel value threshold below which is considered black (0-255)
    
    Returns:
        bool: True if image is predominantly black, False otherwise
    """
    # Convert to numpy array for faster processing
    img_array = np.array(image)
    
    # Calculate mean pixel value across all channels
    mean_value = np.mean(img_array)
    
    # If mean value is very low, image is likely black
    return mean_value < threshold

def prevent_screensaver_during_screenshot():
    """Prevent screen saver during individual screenshot capture"""
    try:
        # Method 1: Use Gio for GNOME environments (more reliable)
        try:
            from gi.repository import Gio
            
            # Disable screen blanking
            session_settings = Gio.Settings.new("org.gnome.desktop.session")
            session_settings.set_uint("idle-delay", 0)
            
            # Disable lock screen
            screensaver_settings = Gio.Settings.new("org.gnome.desktop.screensaver")
            screensaver_settings.set_boolean("lock-enabled", False)
            
            # Optional: prevent suspend on AC power
            power_settings = Gio.Settings.new("org.gnome.settings-daemon.plugins.power")
            power_settings.set_string("sleep-inactive-ac-type", "nothing")
            
        except ImportError:
            # Method 2: Fallback to xset for non-GNOME environments
            subprocess.run(['xset', 's', 'off'], check=False, capture_output=True)
            subprocess.run(['xset', 's', 'noblank'], check=False, capture_output=True)
            subprocess.run(['xset', 'dpms', '0', '0'], check=False, capture_output=True)
            
    except Exception:
        pass

def restore_screensaver_after_screenshot():
    """Restore screen saver after screenshot capture"""
    try:
        # Restore default screen saver settings
        subprocess.run(['xset', 's', 'on'], check=False, capture_output=True)
        subprocess.run(['xset', 's', 'blank'], check=False, capture_output=True)
        subprocess.run(['xset', 'dpms', '5', '10', '15'], check=False, capture_output=True)
    except Exception:
        pass

def _cleanup_libreoffice_locks(file_path: str):
    """
    Remove LibreOffice lock files for the given document before launching.
    Lock files are named  .~lock.<filename>#  in the same directory.
    Also clears the user recovery directory so no restore dialog appears.
    """
    p = Path(file_path)
    lock_file = p.parent / f".~lock.{p.name}#"
    if lock_file.exists():
        try:
            lock_file.unlink()
            print(f"  Removed stale lock file: {lock_file.name}")
        except Exception as e:
            print(f"  Warning: could not remove lock file {lock_file}: {e}")

    # Clear the LibreOffice crash-recovery folder so the restore dialog is suppressed
    recovery_dir = Path.home() / ".config" / "libreoffice" / "4" / "user" / "backup"
    if recovery_dir.exists():
        for f in recovery_dir.glob("*"):
            try:
                f.unlink()
            except Exception:
                pass


def take_screenshot_of_file(file_path, output_path="temp_screenshot.jpg",
                             batch_mode: bool = False):
    """
    Open a file with appropriate application and take a screenshot

    Args:
        file_path (str):    Path to the file to open
        output_path (str):  Path where to save the screenshot
        batch_mode (bool):  When True, apply extra hardening for LibreOffice:
                            clean lock/recovery files and pass --norestore so
                            the crash-recovery dialog never blocks the loop.

    Returns:
        bool: True if successful, False otherwise
    """
    if not os.path.exists(file_path):
        print(f"File not found: {file_path}")
        return False

    # Prevent screen saver during screenshot
    #prevent_screensaver_during_screenshot()

    # Get appropriate application for the file
    app = get_application_for_file(file_path)
    print(f"Opening {file_path} with {app}")

    is_libreoffice = app.lower() == 'libreoffice'

    # In batch mode, clean stale LibreOffice artefacts before every launch
    if batch_mode and is_libreoffice:
        _cleanup_libreoffice_locks(file_path)

    # LibreOffice's actual process names are soffice / soffice.bin, not 'libreoffice'
    wait_name = 'soffice' if is_libreoffice else app.split('/')[-1]

    max_retries = 3
    retry_count = 0

    while retry_count < max_retries:
        try:
            # Launch the application with the file
            if is_libreoffice and batch_mode:
                # --norestore : suppress the crash-recovery dialog
                # --nofirststartwizard : skip any first-run wizard
                process = subprocess.Popen(
                    [app, '--norestore', '--nofirststartwizard', file_path]
                )
            else:
                process = subprocess.Popen([app, file_path])
            
            # Wait for application to start
            print("Waiting for application to load...")
            if wait_for_application_window(wait_name, timeout=15):
                print("Application loaded successfully")
                
                # Wait a bit more for the content to render, especially for complex documents
                if app.lower() in ['libreoffice', 'evince']:
                    time.sleep(4)  # Wait for document viewers
                else:
                    time.sleep(3)  # Wait for other applications
                
                # Take screenshot of the application window only
                print("Taking screenshot...")
                
                # For Linux, we'll use a different approach to capture the active window
                # First, try to get the active window using wmctrl (if available)
                try:
                    # Get the active window ID
                    result = subprocess.run(['xdotool', 'getactivewindow'], 
                                          capture_output=True, text=True, timeout=10)
                    if result.returncode == 0:
                        window_id = result.stdout.strip()
                        
                        # Get window geometry
                        geom_result = subprocess.run(['xdotool', 'getwindowgeometry', '--shell', window_id], 
                                                   capture_output=True, text=True, timeout=10)
                        if geom_result.returncode == 0:
                            # Parse geometry output
                            lines = geom_result.stdout.strip().split('\n')
                            x = y = width = height = 0
                            for line in lines:
                                if line.startswith('X='):
                                    x = int(line.split('=')[1])
                                elif line.startswith('Y='):
                                    y = int(line.split('=')[1])
                                elif line.startswith('WIDTH='):
                                    width = int(line.split('=')[1])
                                elif line.startswith('HEIGHT='):
                                    height = int(line.split('=')[1])
                            
                            if width > 0 and height > 0:
                                # Capture only the application window
                                screenshot = pyautogui.screenshot(region=(x, y, width, height))
                                print(f"Captured active window: ({width}x{height}) at ({x},{y})")
                            else:
                                raise ValueError("Invalid window geometry")
                        else:
                            raise ValueError("Could not get window geometry")
                    else:
                        raise ValueError("Could not get active window")
                except (subprocess.TimeoutExpired, subprocess.CalledProcessError, ValueError, FileNotFoundError) as e:
                    # Fallback to full screen if xdotool is not available or fails
                    print(f"Could not capture window ({e}), capturing full screen as fallback")
                    screenshot = pyautogui.screenshot()
                
                # Save as JPG
                screenshot.save(output_path, 'JPEG', quality=95)
                print(f"Screenshot saved to: {output_path}")
                
                # Check if the screenshot is black/blank
                if is_image_black(screenshot, threshold=20):
                    retry_count += 1
                    print(f"⚠️  Screenshot appears to be black/blank (attempt {retry_count}/{max_retries})")
                    print("   Retrying after a short delay...")
                    
                    # Close the application before retrying
                    close_application(app.split('/')[-1])
                    time.sleep(3) # Wait before retrying
                    
                    if retry_count >= max_retries:
                        print("❌ Maximum retry attempts reached. Screenshot may be black.")
                        return False
                    else:
                        continue  # Retry the screenshot
                else:
                    print("✅ Screenshot validation passed (not black)")
                
                # Close the application
                close_application(app.split('/')[-1])
                
                # Add a small delay to ensure the application has time to close
                time.sleep(3)
                
                return True
            else:
                print(f"Failed to launch {app} or window didn't appear")
                # Try to close any running instance
                close_application(app.split('/')[-1])
                return False
                
        except Exception as e:
            print(f"Error taking screenshot: {e}")
            # Try to close any running instance
            close_application(app.split('/')[-1])
            return False
    
    return False

SUPPORTED_EXTENSIONS = {'.pdf', '.docx', '.doc', '.jpg', '.jpeg', '.png', '.gif', '.heic', '.txt', '.rtf'}

# Rendered page-by-page via pymupdf / LibreOffice headless (quiet mode only)
_MULTIPAGE_EXTENSIONS = {'.pdf', '.docx', '.doc', '.rtf', '.txt'}

# Converted directly by PIL in quiet mode (no GUI needed)
# Note: .heic requires `pip install pillow-heif` + registering the plugin
_DIRECT_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.heic'}


def _copy_image_direct(src_path: Path, output_file: Path) -> bool:
    """Convert an image to JPG using PIL — no GUI, works with read-only source files."""
    try:
        if src_path.suffix.lower() == '.heic':
            try:
                from pillow_heif import register_heif_opener
                register_heif_opener()
            except ImportError:
                print("      HEIC support requires pillow-heif:  pip install pillow-heif")
                return False
        img = Image.open(str(src_path)).convert('RGB')
        img.save(str(output_file), 'JPEG', quality=95)
        return True
    except Exception as e:
        print(f"      PIL conversion failed: {e}")
        return False


def _render_pdf_pages(pdf_path: Path, output_dir: Path, base_name: str,
                      skip_existing: bool) -> tuple[int, int, int]:
    """
    Render every page of a PDF to a separate JPG using pymupdf.

    Naming:
      single-page  →  scrshot_<base_name>.jpg          (unchanged, keeps skip logic working)
      multi-page   →  scrshot_<base_name>_p001.jpg, _p002.jpg, …

    Returns:
        (n_ok, n_skipped, n_failed)
    """
    try:
        import fitz  # pymupdf
    except ImportError:
        print("  [pymupdf not installed — pip install pymupdf]  falling back to screenshot")
        return 0, 0, 1

    try:
        doc = fitz.open(str(pdf_path))
    except Exception as e:
        print(f"  Could not open PDF: {e}")
        return 0, 0, 1

    n_pages = len(doc)
    n_ok = n_skip = n_fail = 0

    for i in range(n_pages):
        if n_pages == 1:
            out_name = f"scrshot_{base_name}.jpg"
        else:
            out_name = f"scrshot_{base_name}_p{i + 1:03d}.jpg"
        out_file = output_dir / out_name

        if skip_existing and out_file.exists():
            print(f"      page {i+1}/{n_pages}: {out_name}  → already exists, skipping")
            n_skip += 1
            continue

        try:
            page = doc[i]
            # 2× zoom → ~150 dpi effective, good balance of quality vs file size
            mat = fitz.Matrix(2.0, 2.0)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            pix.save(str(out_file))
            print(f"      page {i+1}/{n_pages}: {out_name}  ✅")
            n_ok += 1
        except Exception as e:
            print(f"      page {i+1}/{n_pages}: FAILED ({e})")
            n_fail += 1

    doc.close()
    return n_ok, n_skip, n_fail


def _render_pdf_single(pdf_path: Path, output_file: Path) -> bool:
    """
    Render all pages of a PDF to JPGs for single-file mode.

    If the PDF has one page, saves to output_file as-is.
    If it has multiple pages, saves to  <stem>_p001<suffix>, <stem>_p002<suffix>, …
    and prints the paths produced.

    Returns True if at least one page was saved successfully.
    """
    try:
        import fitz  # pymupdf
    except ImportError:
        print("  [pymupdf not installed — pip install pymupdf]")
        return False

    try:
        doc = fitz.open(str(pdf_path))
    except Exception as e:
        print(f"  Could not open PDF: {e}")
        return False

    n_pages = len(doc)
    stem    = output_file.stem
    suffix  = output_file.suffix or '.jpg'
    out_dir = output_file.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    mat     = fitz.Matrix(2.0, 2.0)
    n_ok    = 0
    for i in range(n_pages):
        if n_pages == 1:
            dest = output_file
        else:
            dest = out_dir / f"{stem}_p{i + 1:03d}{suffix}"
        try:
            pix = doc[i].get_pixmap(matrix=mat, alpha=False)
            pix.save(str(dest))
            print(f"  page {i+1}/{n_pages} → {dest}  ✅")
            n_ok += 1
        except Exception as e:
            print(f"  page {i+1}/{n_pages} FAILED: {e}")

    doc.close()
    return n_ok > 0


def _convert_to_pdf(src_path: Path, tmp_dir: Path) -> Path | None:
    """
    Convert a Word / RTF document to PDF using LibreOffice headless.
    Returns the path to the generated PDF, or None on failure.
    """
    try:
        result = subprocess.run(
            ['libreoffice', '--headless', '--norestore', '--convert-to', 'pdf',
             '--outdir', str(tmp_dir), str(src_path)],
            capture_output=True, text=True, timeout=120,
        )
        pdf_path = tmp_dir / (src_path.stem + '.pdf')
        if result.returncode == 0 and pdf_path.exists():
            return pdf_path
        print(f"  LibreOffice conversion failed: {result.stderr.strip()}")
        return None
    except Exception as e:
        print(f"  LibreOffice conversion error: {e}")
        return None


def process_directory(input_dir: str, output_dir: str, skip_existing: bool = True,
                      quiet: bool = False):
    """
    Loop over all supported files in input_dir and save a screenshot of each
    to output_dir as  scrshot_<original_filename>.jpg.

    Args:
        input_dir:      Directory containing receipt files.
        output_dir:     Directory where screenshots will be saved.
        skip_existing:  If True (default), skip files whose output already exists.
        quiet:          If True, use programmatic rendering (PIL / pymupdf /
                        LibreOffice headless) — no GUI, supports multi-page.
                        If False (default), open each file in a viewer and
                        take a single screenshot (visual / fallback mode).
    """
    input_path  = Path(input_dir)
    output_path = Path(output_dir)

    if not input_path.exists():
        print(f"❌ Error: Input directory not found: {input_dir}")
        return

    output_path.mkdir(parents=True, exist_ok=True)

    # Collect supported files, sorted for deterministic order
    files = sorted(
        p for p in input_path.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    )

    if not files:
        print(f"❌ No supported files found in {input_dir}")
        return

    total   = len(files)
    n_ok    = 0
    n_skip  = 0
    n_fail  = 0
    failed  = []

    mode_label = "quiet (programmatic)" if quiet else "visual (GUI)"
    print(f"\nBatch screenshot — {total} file(s) in {input_dir}")
    print(f"Output directory : {output_dir}")
    print(f"Mode             : {mode_label}")
    print(f"Skip existing    : {skip_existing}")
    print()
    if not quiet:
        print("⚠️  NOTE: Automatic screensaver suppression is unreliable on some systems.")
        print("   For best results, please disable screen blanking manually before running:")
        print("     GNOME Settings → Power → Screen Blank → Never")
        print("   or run:  gsettings set org.gnome.desktop.session idle-delay 0")
        print()

    #prevent_screensaver_during_screenshot()
    start_keepalive(interval=20)
    try:
        for idx, src in enumerate(files, 1):
            ext = src.suffix.lower()
            print(f"  [{idx:>3}/{total}] {src.name}")

            out_name = f"scrshot_{src.name}.jpg"
            out_file = output_path / out_name

            if quiet:
                # ── quiet mode: programmatic, no GUI, multi-page for docs ─────
                if ext in _DIRECT_IMAGE_EXTENSIONS:
                    if skip_existing and out_file.exists():
                        print(f"      → already exists, skipping")
                        n_skip += 1
                        continue
                    if _copy_image_direct(src, out_file):
                        print(f"      ✅ saved → {out_name}")
                        n_ok += 1
                    else:
                        print(f"      ❌ failed")
                        n_fail += 1
                        failed.append(src.name)

                elif ext in _MULTIPAGE_EXTENSIONS:
                    pdf_path = src
                    tmp_pdf  = None

                    if ext in {'.docx', '.doc', '.rtf', '.txt'}:
                        print(f"      converting to PDF via LibreOffice headless …")
                        import tempfile, shutil
                        with tempfile.TemporaryDirectory() as tmp_dir:
                            tmp_pdf = _convert_to_pdf(src, Path(tmp_dir))
                            if tmp_pdf is None:
                                print(f"      ❌ conversion failed")
                                n_fail += 1
                                failed.append(src.name)
                                continue
                            kept_pdf = output_path / (src.stem + '_converted.pdf')
                            shutil.copy2(str(tmp_pdf), str(kept_pdf))
                        pdf_path = kept_pdf

                    ok, skip, fail = _render_pdf_pages(pdf_path, output_path, src.name, skip_existing)
                    n_ok   += ok
                    n_skip += skip
                    n_fail += fail
                    if fail:
                        failed.append(src.name)

                    if tmp_pdf is not None and kept_pdf.exists():
                        kept_pdf.unlink()

                else:
                    print(f"      skipped (unsupported in quiet mode)")

            else:
                # ── visual mode: open in GUI, single screenshot per file ───────
                if skip_existing and out_file.exists():
                    print(f"      → already exists, skipping")
                    n_skip += 1
                    continue

                success = take_screenshot_of_file(str(src), str(out_file), batch_mode=True)
                if success:
                    print(f"      ✅ saved → {out_name}")
                    n_ok += 1
                else:
                    print(f"      ❌ failed")
                    n_fail += 1
                    failed.append(src.name)
    finally:
        stop_keepalive()
        #restore_screensaver_after_screenshot()

    print(f"\n{'─'*56}")
    print(f"  Done      : {n_ok}  failed: {n_fail}  skipped: {n_skip}")
    if failed:
        print("  Failed files:")
        for f in failed:
            print(f"    • {f}")
    print(f"{'─'*56}\n")


def main():
    """Main function to demonstrate the screenshot functionality"""
    import argparse

    parser = argparse.ArgumentParser(
        description="Take screenshots of receipt files — single file or whole directory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single file
  python screenshot_function.py --input document.pdf --output my_screenshot.jpg

  # Whole directory  (writes scrshot_<filename>.jpg into output-dir)
  python screenshot_function.py --input-dir ./Receipt_2021 --output-dir ./Receipt_2021/output_screenshot

  # Re-process even if output already exists
  python screenshot_function.py --input-dir ./receipts --output-dir ./screenshots --no-skip
        """
    )

    # ── single-file mode ──────────────────────────────────────────────────────
    parser.add_argument(
        '--input', '-i',
        type=str,
        help='Path to a single file to screenshot.',
    )
    parser.add_argument(
        '--output', '-o',
        type=str,
        default="temp_screenshot.jpg",
        help='Output path for single-file mode (default: temp_screenshot.jpg).',
    )

    # ── directory mode ────────────────────────────────────────────────────────
    parser.add_argument(
        '--input-dir', '-I',
        type=str,
        metavar='DIR',
        help='Input directory: process every supported file inside it.',
    )
    parser.add_argument(
        '--output-dir', '-O',
        type=str,
        metavar='DIR',
        help='Output directory for directory mode (required when --input-dir is used).',
    )
    parser.add_argument(
        '--no-skip',
        action='store_true',
        help='Re-process files even if the output screenshot already exists.',
    )
    parser.add_argument(
        '--quiet', '-q',
        action='store_true',
        help='Quiet mode: render programmatically (PIL / pymupdf / LibreOffice headless). '
             'No GUI, supports multi-page PDFs and documents. '
             'Default: visual mode (opens file in viewer, takes one screenshot per file).',
    )

    args = parser.parse_args()

    # ── directory mode ────────────────────────────────────────────────────────
    if args.input_dir:
        if not args.output_dir:
            parser.error("--output-dir is required when using --input-dir")
        process_directory(args.input_dir, args.output_dir,
                          skip_existing=not args.no_skip, quiet=args.quiet)
        return

    # ── single-file mode ──────────────────────────────────────────────────────
    if args.input:
        input_file = args.input
        if not os.path.exists(input_file):
            print(f"❌ Error: Input file not found: {input_file}")
            return
        ext = os.path.splitext(input_file)[1].lower()
        if ext not in SUPPORTED_EXTENSIONS:
            print(f"❌ Error: Unsupported file type '{ext}'. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
            return
    else:
        # Auto-detect first suitable file in current directory (backward compatibility)
        input_file = None
        for f in sorted(os.listdir(os.getcwd())):
            if os.path.splitext(f)[1].lower() in SUPPORTED_EXTENSIONS:
                input_file = os.path.join(os.getcwd(), f)
                break
        if not input_file:
            print("❌ Error: No supported files found in current directory. "
                  "Use --input or --input-dir.")
            return

    output_file = args.output
    out_dir = os.path.dirname(output_file)
    if out_dir and not os.path.exists(out_dir):
        try:
            os.makedirs(out_dir)
        except Exception as e:
            print(f"❌ Error: Could not create output directory {out_dir}: {e}")
            return

    mode_label = "quiet (programmatic)" if args.quiet else "visual (GUI)"
    print(f"📄 Processing file: {input_file}")
    print(f"💾 Saving to      : {output_file}")
    print(f"🔧 Mode           : {mode_label}")

    src_path = Path(input_file)
    ext      = src_path.suffix.lower()

    if args.quiet:
        # ── quiet mode: programmatic rendering ────────────────────────────────
        if ext in _DIRECT_IMAGE_EXTENSIONS:
            ok = _copy_image_direct(src_path, Path(output_file))
        elif ext == '.pdf':
            ok = _render_pdf_single(src_path, Path(output_file))
        elif ext in {'.docx', '.doc', '.rtf', '.txt'}:
            import tempfile, shutil
            print("  Converting to PDF via LibreOffice headless …")
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_pdf = _convert_to_pdf(src_path, Path(tmp_dir))
                if tmp_pdf is None:
                    print("❌ Conversion failed")
                    return
                kept_pdf = Path(output_file).with_suffix('.pdf')
                shutil.copy2(str(tmp_pdf), str(kept_pdf))
            ok = _render_pdf_single(kept_pdf, Path(output_file))
            kept_pdf.unlink(missing_ok=True)
        else:
            print(f"❌ Unsupported type in quiet mode: {ext}")
            return
        print("✅ Screenshot completed successfully!" if ok else "❌ Screenshot failed")

    else:
        # ── visual mode: GUI viewer, single screenshot (no multi-page) ────────
        if take_screenshot_of_file(input_file, output_file):
            print("✅ Screenshot completed successfully!")
        else:
            print("❌ Screenshot failed")

if __name__ == "__main__":
    main()
