import fitz
import sys

sys.stdout.reconfigure(encoding='utf-8')
doc = fitz.open("orca_manual_6_1_0.pdf")

print("=== TD-DFT IN ORCA 6.1 (Pages 825-830) ===")
for p in range(825, 831):
    print(f"\n--- PAGE {p} ---")
    print(doc[p - 1].get_text())
