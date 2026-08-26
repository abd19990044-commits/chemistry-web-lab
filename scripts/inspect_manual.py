import fitz  # PyMuPDF
import re
import sys

doc = fitz.open("orca_manual_6_1_0.pdf")
print(f"Total pages: {len(doc)}")

def search_terms(queries, max_results_per_query=4):
    results = {}
    for q in queries:
        results[q] = []
        pattern = re.compile(re.escape(q), re.IGNORECASE)
        for page_num in range(len(doc)):
            page = doc[page_num]
            text = page.get_text()
            if pattern.search(text):
                lines = text.split("\n")
                matched_lines = []
                for i, l in enumerate(lines):
                    if pattern.search(l):
                        start = max(0, i - 2)
                        end = min(len(lines), i + 4)
                        matched_lines.append(f"[p.{page_num+1}] " + " | ".join(lines[start:end]))
                results[q].append((page_num + 1, matched_lines[:2]))
                if len(results[q]) >= max_results_per_query:
                    break
    return results

print("\n--- Searching key sections ---")
q_list = [
    "What is New in ORCA 6",
    "General Structure of an Input File",
    "Simple Input",
    "Composite Electronic Structure Methods",
    "Implicit Solvation Models",
    "AutoAux",
    "CPCM",
    "SMD",
    "%maxdisk",
    "%maxcore",
    "%pal",
    "r2SCAN-3c",
    "wB97X-3c",
    "B97-3c",
    "OptTS",
    "NumFreq",
]

res = search_terms(q_list, max_results_per_query=3)
for q, hits in res.items():
    print(f"\n=== Query: {q} (Hits: {len(hits)}) ===")
    for pnum, snippets in hits:
        print(f"  Page {pnum}:")
        for s in snippets:
            print(f"    {s[:160]}...")
