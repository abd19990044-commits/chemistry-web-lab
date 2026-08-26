with open("scratch_manual_analysis.txt", "r", encoding="utf-8") as f:
    text = f.read()

sections = text.split("==============================")
for s in sections:
    if not s.strip():
        continue
    lines = s.strip().split("\n")
    header = lines[0] if len(lines) > 0 else "SECTION"
    print(f"\n{header}")
    content = "\n".join(lines[1:35])
    print(content[:1000])
