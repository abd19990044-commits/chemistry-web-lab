import fitz

doc = fitz.open("orca_manual_6_1_0.pdf")

# PyMuPDF can get the document outline / TOC directly!
toc = doc.get_toc()
print(f"Total TOC items: {len(toc)}")

with open("scratch_toc.txt", "w", encoding="utf-8") as f:
    for lvl, title, page in toc:
        f.write(f"{'  ' * (lvl - 1)}[Level {lvl}] {title} -> Page {page}\n")

print("TOC written to scratch_toc.txt. First 40 entries:")
for lvl, title, page in toc[:40]:
    print(f"{'  ' * (lvl - 1)}[L{lvl}] {title} (p. {page})")
