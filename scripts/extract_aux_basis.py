import fitz
import sys

sys.stdout.reconfigure(encoding='utf-8')
doc = fitz.open("orca_manual_6_1_0.pdf")

print("=== AUXILIARY BASIS SETS IN ORCA 6.1 (Pages 97-102) ===")
for p in range(97, 103):
    print(f"\n--- PAGE {p} ---")
    print(doc[p - 1].get_text())
