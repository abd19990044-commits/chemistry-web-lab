import fitz
import sys

sys.stdout.reconfigure(encoding='utf-8')
doc = fitz.open("orca_manual_6_1_0.pdf")

print("=== SOLVATION IN ORCA 6.1 (Pages 144-160) ===")
for p in range(144, 161):
    print(f"\n--- PAGE {p} ---")
    print(doc[p - 1].get_text())
