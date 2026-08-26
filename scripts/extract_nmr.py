import fitz
import sys

sys.stdout.reconfigure(encoding='utf-8')
doc = fitz.open("orca_manual_6_1_0.pdf")

print("=== NMR IN ORCA 6.1 (Pages 1032-1038) ===")
for p in range(1032, 1039):
    print(f"\n--- PAGE {p} ---")
    print(doc[p - 1].get_text())
