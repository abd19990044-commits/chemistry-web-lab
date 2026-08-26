import fitz

doc = fitz.open("orca_manual_6_1_0.pdf")

def get_page_text(pnum):
    # pnum is 1-indexed
    return doc[pnum - 1].get_text()

pages_to_check = {
    "Highlights": [11, 12, 13, 35, 36],
    "Simple_Input_Keywords": [59, 60],
    "Parallel_Memory": [60, 61, 62, 63],
    "Solvation_CPCM_SMD": [144, 145, 152, 156, 157, 168, 169],
    "Dispersion": [252, 253, 254],
    "Composite_3c": [275, 276, 277, 278],
    "RI_AutoAux": [97, 98, 99, 100],
    "X2C_Relativistic": [140, 141],
    "Vibrational_Freq": [670, 671, 675, 676],
    "TDDFT": [825, 826, 827],
    "NMR_EPR": [1032, 1033]
}

with open("scratch_manual_analysis.txt", "w", encoding="utf-8") as out:
    for category, pages in pages_to_check.items():
        out.write(f"\n{'='*30}\nCATEGORY: {category}\n{'='*30}\n")
        for p in pages:
            out.write(f"\n--- Page {p} ---\n")
            out.write(get_page_text(p))

print("Extracted detailed manual pages to scratch_manual_analysis.txt")
