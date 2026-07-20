"""Adaptiveness audit: what still refuses to shrink, and where."""
import pathlib, re

FILES = ["settings_window.py", "test_panel.py", "question_editor.py",
         "widgets.py", "show_window.py"]

fixed, tables, scrolls, margins, minw = [], [], [], [], []

for name in FILES:
    p = pathlib.Path(name)
    if not p.exists():
        continue
    text = p.read_text()
    lines = text.splitlines()
    for n, line in enumerate(lines, 1):
        s = line.strip()
        m = re.search(r"setFixedWidth\((\d+)\)", s)
        if m and int(m.group(1)) > 100:
            fixed.append(f"{name}:{n}  {s[:66]}")
        m = re.search(r"setMinimumWidth\((\d+)\)", s)
        if m and int(m.group(1)) >= 200:
            minw.append(f"{name}:{n}  {s[:66]}")
        if re.search(r"setContentsMargins\(0, 0, 0, 0\)", s):
            margins.append(f"{name}:{n}")
    # every QTableWidget should configure resize modes
    n_tables = len(re.findall(r"QTableWidget\(\d", text))
    n_modes = len(re.findall(r"setSectionResizeMode\(", text))
    if n_tables:
        tables.append(f"{name}: {n_tables} table(s), {n_modes} resize-mode call(s)")
    # scroll areas present?
    scrolls.append(f"{name}: {len(re.findall(r'QScrollArea\(', text))} scroll area(s)")

print("=== WIDGETS THAT STILL CANNOT SHRINK (fixed width > 100) ===")
print("\n".join("  " + f for f in fixed) if fixed else "  none")
print("\n=== LARGE MINIMUM WIDTHS (>= 200) ===")
print("\n".join("  " + f for f in minw) if minw else "  none")
print("\n=== TABLE RESIZE POLICY ===")
print("\n".join("  " + t for t in tables))
print("\n=== SCROLL AREAS PER MODULE ===")
print("\n".join("  " + s for s in scrolls))
print(f"\n=== ZERO-MARGIN LAYOUTS: {len(margins)} (fine for nested holders) ===")
