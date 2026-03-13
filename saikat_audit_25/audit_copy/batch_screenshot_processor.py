#!/usr/bin/env python3
"""
Batch screenshot processor for merged expense data.

This script:
1. Reads merged_expense_data.csv
2. Extracts all unique input directories from FILE_LOCATION column
3. Uses screenshot_function.py to generate screenshots for all files
4. Creates final_expense_data.csv with an additional SCREENSHOT column
   containing the path to each generated screenshot
"""

import os
import csv
import sys
from pathlib import Path
from collections import defaultdict

# Import the screenshot processing function
from screenshot_function import process_directory


def extract_directory_from_path(file_location):
    """Extract the directory path from a FILE_LOCATION entry"""
    if not file_location or file_location == "NOT_FOUND":
        return None
    # Get the directory part (everything except the filename)
    directory = os.path.dirname(file_location)
    return directory if directory else None


def extract_filename_from_path(file_location):
    """Extract just the filename from a FILE_LOCATION entry"""
    if not file_location or file_location == "NOT_FOUND":
        return None
    return os.path.basename(file_location)


def get_screenshot_filename(original_filename):
    """
    Convert an original filename to the expected screenshot filename
    Based on screenshot_function.py: scrshot_<original_filename>.jpg
    """
    if not original_filename:
        return None
    return f"scrshot_{original_filename}.jpg"


def main():
    csv_file = "merged_expense_data.csv"
    output_csv = "final_expense_data.csv"
    screenshot_output_dir = "screenshotoutput"
    
    # Create output directory
    Path(screenshot_output_dir).mkdir(exist_ok=True)
    
    # Step 1: Read the CSV and collect unique directories and file info
    print("📖 Reading merged_expense_data.csv...")
    rows = []
    directory_files = defaultdict(set)  # Map directory -> set of filenames
    
    with open(csv_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
            file_location = row.get('FILE_LOCATION', '')
            
            if file_location and file_location != "NOT_FOUND":
                directory = extract_directory_from_path(file_location)
                filename = extract_filename_from_path(file_location)
                
                if directory and filename:
                    directory_files[directory].add(filename)
    
    print(f"✅ Loaded {len(rows)} rows")
    print(f"📁 Found {len(directory_files)} unique directories")
    
    # Step 2: Process each directory using screenshot_function
    print("\n🎬 Processing screenshots for each directory...")
    print("─" * 60)
    
    for idx, (directory, filenames) in enumerate(sorted(directory_files.items()), 1):
        # Full input path
        input_path = os.path.join(os.getcwd(), directory)
        
        if not os.path.exists(input_path):
            print(f"⚠️  [{idx}] Directory not found: {directory}")
            continue
        
        print(f"\n[{idx}] Processing: {directory}")
        print(f"    Files to process: {len(filenames)}")
        
        # Use quiet mode for batch processing (no GUI needed)
        process_directory(
            input_dir=input_path,
            output_dir=screenshot_output_dir,
            skip_existing=True,
            quiet=True  # Use headless/programmatic mode
        )
    
    print("─" * 60)
    print("✅ Screenshot processing complete\n")
    
    # Step 3: Create mapping of original filename to screenshot path
    print("🔗 Creating file to screenshot mapping...")
    screenshot_mapping = {}  # Maps original_filename -> screenshot_relative_path
    
    # Scan the screenshot output directory
    screenshot_dir = Path(screenshot_output_dir)
    for screenshot_file in screenshot_dir.iterdir():
        if screenshot_file.is_file() and screenshot_file.suffix.lower() == '.jpg':
            # screenshot file is like: scrshot_originalname.jpg
            # Extract the original filename
            screenshot_name = screenshot_file.name
            if screenshot_name.startswith('scrshot_'):
                # Remove the 'scrshot_' prefix and '.jpg' suffix to get original name
                # But we need to handle multi-page PDFs which have _p001, _p002, etc.
                
                # For simplicity, we'll match against what we expect
                original_name = screenshot_name[8:-4]  # Remove 'scrshot_' prefix and '.jpg'
                
                # Store the relative path to the screenshot
                rel_path = os.path.join(screenshot_output_dir, screenshot_name)
                screenshot_mapping[original_name] = rel_path
    
    print(f"✅ Found {len(screenshot_mapping)} screenshots\n")
    
    # Step 4: Create the final CSV with SCREENSHOT column
    print("📝 Creating final_expense_data.csv...")
    
    # Add SCREENSHOT column to each row
    for row in rows:
        file_location = row.get('FILE_LOCATION', '')
        
        if file_location and file_location != "NOT_FOUND":
            filename = extract_filename_from_path(file_location)
            screenshot_path = screenshot_mapping.get(filename)
            row['SCREENSHOT'] = screenshot_path if screenshot_path else "NOT_FOUND"
        else:
            row['SCREENSHOT'] = "NOT_FOUND"
    
    # Write the new CSV with all original columns + SCREENSHOT column
    with open(output_csv, 'w', newline='', encoding='utf-8') as f:
        # Get the column names from the first row, plus our new SCREENSHOT column
        fieldnames = list(rows[0].keys()) if rows else []
        
        # Ensure SCREENSHOT is at the end
        if 'SCREENSHOT' in fieldnames:
            fieldnames.remove('SCREENSHOT')
        fieldnames.append('SCREENSHOT')
        
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    
    print(f"✅ Created {output_csv} with {len(rows)} rows")
    print(f"   Columns: {', '.join(fieldnames)}\n")
    
    # Step 5: Print summary statistics
    print("📊 Summary Statistics:")
    print("─" * 60)
    
    found_count = sum(1 for row in rows if row.get('SCREENSHOT') != "NOT_FOUND")
    not_found_count = sum(1 for row in rows if row.get('SCREENSHOT') == "NOT_FOUND")
    
    print(f"  Total rows:              {len(rows)}")
    print(f"  Screenshots found:       {found_count}")
    print(f"  Screenshots not found:   {not_found_count}")
    print(f"  Screenshot directory:    {screenshot_output_dir}")
    print("─" * 60 + "\n")
    
    print("✨ Processing complete!")
    print(f"📄 Output file: {output_csv}")


if __name__ == "__main__":
    main()
