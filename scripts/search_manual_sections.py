import re

with open("scratch_toc.txt", "r", encoding="utf-8") as f:
    lines = f.readlines()

topics = [
    "General Keyword List", "Simple Input", "Structure of an Input File",
    "Density Functional Theory", "Composite", "Dispersion", "Solvation", "CPCM", "SMD",
    "Relativistic", "X2C", "ZORA", "Basis Sets", "Auxiliary", "AutoAux",
    "Geometry Optimization", "Frequency", "Vibrational", "Thermochemistry",
    "TD-DFT", "Excited", "NMR", "EPR", "Parallel", "Memory", "MaxCore", "MaxDisk",
    "Using Old ORCA Inputs", "ORCA 6.1 Highlights", "Compound"
]

print("=== RELEVANT TOC SECTIONS ===")
for t in topics:
    matches = [l.strip() for l in lines if re.search(r"\b" + re.escape(t) + r"\b", l, re.IGNORECASE)]
    if matches:
        print(f"\n--- Topic: {t} ({len(matches)} matches) ---")
        for m in matches[:6]:
            print("  ", m)
