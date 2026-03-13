#!/usr/bin/env python3
"""
Script to merge combined_expense_data.csv with missing entries from expense_receipt_mapping.csv
Finds all entries in expense_receipt_mapping.csv that don't exist in combined_expense_data.csv
and appends them with only SUBDIR, REFERENCE_TO_CSV, and FILE_LOCATION columns filled.
"""

import csv
from pathlib import Path

def main():
    # File paths
    combined_file = 'combined_expense_data.csv'
    mapping_file = 'expense_receipt_mapping.csv'
    output_file = 'merged_expense_data.csv'
    
    # Step 1: Read all FILE_LOCATION values from combined_expense_data.csv
    existing_locations = set()
    with open(combined_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            file_loc = row.get('FILE_LOCATION', '').strip()
            if file_loc and file_loc != 'NOT_FOUND':
                existing_locations.add(file_loc)
    
    print(f"Found {len(existing_locations)} unique file locations in combined_expense_data.csv")
    
    # Step 2: Read expense_receipt_mapping.csv and find missing entries
    missing_entries = []
    with open(mapping_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            receipt_path = row.get('Receipt_Full_Path', '').strip()
            
            # Check if this path is not in the existing locations
            if receipt_path and receipt_path not in existing_locations:
                missing_entries.append({
                    'Event': row.get('Event', ''),
                    'Excel_Filename': row.get('Excel_Filename', ''),
                    'Receipt_Full_Path': receipt_path
                })
    
    print(f"Found {len(missing_entries)} missing file locations in expense_receipt_mapping.csv")
    
    # Step 3: Read combined_expense_data.csv header to maintain structure
    with open(combined_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        columns = reader.fieldnames
    
    # Note: The original file has "REFERNCE_TO_CSV" (typo), we maintain it to preserve structure
    print(f"\nColumns in combined_expense_data.csv: {columns}")
    
    # Step 4: Write the merged output
    with open(output_file, 'w', newline='', encoding='utf-8') as outf:
        writer = csv.DictWriter(outf, fieldnames=columns)
        
        # Write header
        writer.writeheader()
        
        # Write all rows from combined_expense_data.csv
        with open(combined_file, 'r', encoding='utf-8') as inf:
            reader = csv.DictReader(inf)
            for row in reader:
                writer.writerow(row)
        
        # Write rows for missing entries with only specific columns filled
        for entry in missing_entries:
            new_row = {}
            for col in columns:
                if col == 'SUBDIR':
                    new_row[col] = entry['Event']
                elif col == 'REFERNCE_TO_CSV':  # Note: keeping the typo from original
                    new_row[col] = entry['Excel_Filename']
                elif col == 'FILE_LOCATION':
                    new_row[col] = entry['Receipt_Full_Path']
                else:
                    new_row[col] = ''
            writer.writerow(new_row)
    
    print(f"\n✓ Successfully created {output_file}")
    print(f"  - Original rows from combined_expense_data.csv: {len(list(open(combined_file).readlines())) - 1}")
    print(f"  - New rows appended from mapping file: {len(missing_entries)}")
    
    # Verify the output
    with open(output_file, 'r', encoding='utf-8') as f:
        output_lines = len(f.readlines())
    print(f"  - Total rows in merged file: {output_lines - 1} (plus 1 header)")

if __name__ == '__main__':
    main()
