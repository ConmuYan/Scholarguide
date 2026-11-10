"""Product comparison matrix: strong/medium/weak vs ScholarGuide (blank).
Outputs: reports/product_comparison.md + reports/product_comparison.xlsx
"""
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

DIMS = [
    # (dimension, weight-ish group, ChatGPT, SciSpace, ConnectedPapers, note source)
    ("UI clarity & onboarding (使用感受)", "Strong general UI; chat-first", "Paper-centric UI, feature-rich", "Minimal graph UI, single purpose", ""),
    ("Multi-turn stateful refinement (核心维度)", "Supports follow-ups, keeps context informally", "Chat per-paper; weak cross-task state", "No dialogue; static graph", ""),
    ("Retrieval coverage (sources)", "Broad web + some scholarly", "Scholarly-focused, millions of papers", "Seed-paper graph only", ""),
    ("Evidence grounding (摘要级证据)", "Quotes with links; variable", "Citation-linked answers", "Edge = citation only", ""),
    ("Identity correctness of papers", "Occasional hallucinated refs", "Generally reliable metadata", "Exact paper nodes", ""),
    ("Decision transparency (trace)", "Hidden reasoning", "Partial", "None", ""),
    ("User control (intent, #results, scope)", "Natural language only", "Filters available", "None", ""),
    ("Latency / cost", "Fast; paid tier for search", "Fast; freemium", "Fast; freemium", ""),
    ("Export (BibTeX/APA/Markdown)", "No structured citation export", "Citation export limited", "None", ""),
    ("Language support (中/EN)", "Multilingual", "English-first", "English-only", ""),
]
SCORES = {  # 1-5 preliminary, to be re-scored in the blind test
    "ChatGPT (Search)":    [5, 4, 4, 3, 3, 2, 3, 4, 2, 5],
    "SciSpace (medium)":   [4, 2, 4, 4, 4, 2, 4, 4, 3, 3],
    "Connected Papers (weak)": [4, 1, 2, 2, 5, 1, 1, 5, 1, 2],
}

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Comparison"
hdr_fill = PatternFill("solid", fgColor="1F3864")
blank_fill = PatternFill("solid", fgColor="FFF2CC")
thin = Border(*[Side(style="thin", color="BBBBBB")] * 4)

ws.append(["Dimension"] + list(SCORES.keys()) + ["ScholarGuide (self-eval, to fill)"] + ["Notes"])
for c in ws[1]:
    c.font = Font(bold=True, color="FFFFFF"); c.fill = hdr_fill; c.alignment = Alignment(vertical="center", wrap_text=True)
for (dim, cg, ss, cp, _n), sc in zip(DIMS, zip(*SCORES.values())):
    ws.append([dim, sc[0], sc[1], sc[2], None, f"ChatGPT: {cg} | SciSpace: {ss} | Conn.Papers: {cp}"])
r = len(DIMS) + 2
ws.cell(row=r, column=1, value="Mean").font = Font(bold=True)
for col in (2, 3, 4):
    ws.cell(row=r, column=col, value=f"=ROUND(AVERAGE({chr(64+col)}2:{chr(64+col)}{r-1}),2)")
ws.cell(row=r, column=5, value="=IF(COUNT(E2:E{r-1})=0,\"\",ROUND(AVERAGE(E2:E{r-1}),2))")
for row in ws.iter_rows(min_row=1, max_row=r, max_col=6):
    for c in row:
        c.border = thin
        c.alignment = Alignment(vertical="top", wrap_text=True)
for rr in range(2, r):
    ws.cell(row=rr, column=5).fill = blank_fill
ws.column_dimensions["A"].width = 42
for col in "BCDE":
    ws.column_dimensions[col].width = 22
ws.column_dimensions["F"].width = 70
wb.save("reports/product_comparison.xlsx")

with open("reports/product_comparison.md", "w", encoding="utf-8") as f:
    f.write("# 产品多维度对比（初评，待盲测校准）\n\n")
    f.write("对比对象：**强 = ChatGPT (Search)** · **中 = SciSpace** · **弱 = Connected Papers**；ScholarGuide 列留空，正式分数由问卷盲测产出。\n\n")
    f.write("| 维度 | ChatGPT | SciSpace | Connected Papers | ScholarGuide |\n|---|---|---|---|---|\n")
    for (dim, *_), sc in zip(DIMS, zip(*SCORES.values())):
        f.write(f"| {dim} | {sc[0]} | {sc[1]} | {sc[2]} |  |\n")
    f.write("\n注：初评为桌面研究估计，正式对比需按组内计划做问卷式盲测（产物并置打分）。\n")
print("written reports/product_comparison.xlsx / .md")
