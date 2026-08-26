import fitz
import sys
import re

sys.stdout.reconfigure(encoding='utf-8')
doc = fitz.open("orca_manual_6_1_0.pdf")

def search_phrase(phrase, max_hits=5):
    print(f"\n==========================================")
    print(f"SEARCH FOR: '{phrase}'")
    print(f"==========================================")
    hits = 0
    for i, page in enumerate(doc):
        t = page.get_text()
        if re.search(r"\b" + re.escape(phrase) + r"\b", t, re.IGNORECASE):
            print(f"\n--- Page {i+1} ---")
            lines = t.split("\n")
            for j, l in enumerate(lines):
                if re.search(r"\b" + re.escape(phrase) + r"\b", l, re.IGNORECASE):
                    start = max(0, j - 3)
                    end = min(len(lines), j + 6)
                    print("\n".join(lines[start:end]))
                    print("---")
            hits += 1
            if hits >= max_hits:
                break

# Let's check SMD syntax in ORCA 6
search_phrase("! CPCM(Water)")
search_phrase("! SMD")
search_phrase("SMDSolvent")
search_phrase("CPCM(SMD)")
search_phrase("r2SCAN-3c")
search_phrase("wB97X-3c")
search_phrase("wB97X-D4")
search_phrase("AutoAux")
search_phrase("%eprnmr")
search_phrase("! OptTS")
search_phrase("! NumFreq")
