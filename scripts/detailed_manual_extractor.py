import fitz
import sys

sys.stdout.reconfigure(encoding='utf-8')
doc = fitz.open("orca_manual_6_1_0.pdf")

def print_section(title, start_page, end_page):
    print(f"\n{'='*50}\nSECTION: {title} (Pages {start_page}-{end_page})\n{'='*50}")
    for p in range(start_page, end_page + 1):
        print(f"\n--- Page {p} ---")
        lines = doc[p - 1].get_text().split("\n")
        print("\n".join(lines[:60]))

# 1. Solvation (CPCM & SMD)
print_section("Implicit Solvation Models (CPCM & SMD)", 144, 158)

# 2. Composite methods
print_section("Composite Methods (3c)", 275, 278)

# 3. Dispersion corrections
print_section("Dispersion Corrections", 252, 258)

# 4. TD-DFT and NMR
print_section("TD-DFT", 825, 828)
print_section("NMR", 1032, 1035)

# 5. Frequency & Thermochemistry
print_section("Frequency & Thermochemistry", 670, 676)
