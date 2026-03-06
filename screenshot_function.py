#!/usr/bin/env python3
"""
Screenshot function for opening files with appropriate applications and capturing screenshots
"""

import os
import subprocess
import time
import pyautogui
from PIL import Image
import psutil
import sys
import numpy as np

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

def take_screenshot_of_file(file_path, output_path="temp_screenshot.jpg"):
    """
    Open a file with appropriate application and take a screenshot
    
    Args:
        file_path (str): Path to the file to open
        output_path (str): Path where to save the screenshot
    
    Returns:
        bool: True if successful, False otherwise
    """
    if not os.path.exists(file_path):
        print(f"File not found: {file_path}")
        return False
    
    # Prevent screen saver during screenshot
    prevent_screensaver_during_screenshot()
    
    # Get appropriate application for the file
    app = get_application_for_file(file_path)
    print(f"Opening {file_path} with {app}")
    
    max_retries = 3
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            # Launch the application with the file
            if app == 'xdg-open':
                process = subprocess.Popen([app, file_path])
            else:
                process = subprocess.Popen([app, file_path])
            
            # Wait for application to start
            print("Waiting for application to load...")
            if wait_for_application_window(app.split('/')[-1], timeout=15):  # Increased timeout
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

def main():
    """Main function to demonstrate the screenshot functionality"""
    import argparse
    
    # Set up argument parser
    parser = argparse.ArgumentParser(
        description="Screenshot function for opening files with appropriate applications and capturing screenshots",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python screenshot_function.py
    Use default behavior (auto-detect first suitable file in current directory)
  
  python screenshot_function.py --input document.pdf
    Take screenshot of specific input file (output auto-generated)
  
  python screenshot_function.py --output my_screenshot.jpg
    Use auto-detected input file, save to specific output location
  
  python screenshot_function.py --input document.pdf --output my_screenshot.jpg
    Take screenshot of specific input file and save to specific output location
  
  python screenshot_function.py -i document.pdf -o my_screenshot.jpg
    Same as above using short options
        """
    )
    
    parser.add_argument(
        '--input', '-i',
        type=str,
        help='Path to the file to take screenshot of (supports .pdf, .docx, .jpg, .jpeg, .png, .gif, .heic, .doc, .txt, .rtf)'
    )
    
    parser.add_argument(
        '--output', '-o',
        type=str,
        default="temp_screenshot.jpg",
        help='Path where to save the screenshot (default: temp_screenshot.jpg)'
    )
    
    args = parser.parse_args()
    
    # Determine input file
    if args.input:
        # Use provided input file
        input_file = args.input
        if not os.path.exists(input_file):
            print(f"❌ Error: Input file not found: {input_file}")
            return
        file_extension = os.path.splitext(input_file)[1].lower()
        supported_extensions = ['.pdf', '.docx', '.jpg', '.jpeg', '.png', '.gif', '.heic', '.doc', '.txt', '.rtf']
        if file_extension not in supported_extensions:
            print(f"❌ Error: Input file must be one of the supported formats: {', '.join(supported_extensions)}")
            print(f"   File provided: {input_file}")
            return
    else:
        # Auto-detect first suitable file in current directory (backward compatibility)
        current_dir = os.getcwd()
        files = os.listdir(current_dir)
        
        input_file = None
        for file in files:
            file_extension = os.path.splitext(file)[1].lower()
            if file_extension in ['.pdf', '.jpg', '.jpeg', '.png']:
                input_file = os.path.join(current_dir, file)
                break
        
        if not input_file:
            print("❌ Error: No suitable test files found in directory")
            return
    
    # Use provided output path
    output_file = args.output
    
    # Ensure output directory exists
    output_dir = os.path.dirname(output_file)
    if output_dir and not os.path.exists(output_dir):
        try:
            os.makedirs(output_dir)
            print(f"Created output directory: {output_dir}")
        except Exception as e:
            print(f"❌ Error: Could not create output directory {output_dir}: {e}")
            return
    
    print(f"📄 Processing file: {input_file}")
    print(f"💾 Screenshot will be saved to: {output_file}")
    
    # Take screenshot
    success = take_screenshot_of_file(input_file, output_file)
    
    if success:
        print("✅ Screenshot function completed successfully!")
    else:
        print("❌ Screenshot function failed")

if __name__ == "__main__":
    main()
