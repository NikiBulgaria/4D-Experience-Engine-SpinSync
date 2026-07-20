"""verify.py — static integrity check for the SpinSync codebase.

Catches the bug classes that only show up at runtime, without needing a display:

  1. self._something referenced but never defined  (this crashed startup once,
     after an edit deleted six EspTab methods in one go)
  2. pyqtSignal(...).emit() called with the wrong number of arguments
  3. cross-module attribute access on a known project class that does not exist

Run:  python3 verify.py
"""

from __future__ import annotations

import ast
import pathlib
import sys
from collections import defaultdict

MODULES = ["config.py", "esp_link.py", "rng.py", "wheel_widget.py",
           "game_engine.py", "show_window.py", "widgets.py", "test_panel.py",
           "question_editor.py", "settings_window.py", "main.py"]


def is_project_name(name: str) -> bool:
    """Project members are snake_case or _private; Qt's are camelCase."""
    if name.startswith("__"):
        return False
    stripped = name.lstrip("_")
    return not any(ch.isupper() for ch in stripped)


def has_foreign_base(info, classes: dict) -> bool:
    """True when the class inherits from something outside this project (Qt),
    whose member list we cannot see."""
    return any(base not in classes for base in info.bases)


class ClassInfo:
    def __init__(self, name: str, module: str):
        self.name = name
        self.module = module
        self.methods: set = set()
        self.attrs: set = set()
        self.signals: dict = {}
        self.bases: list = []

    @property
    def members(self) -> set:
        return self.methods | self.attrs | set(self.signals)


def collect(path: pathlib.Path, classes: dict):
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        info = ClassInfo(node.name, path.name)
        info.bases = [b.id for b in node.bases if isinstance(b, ast.Name)]

        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                info.methods.add(item.name)
            elif isinstance(item, ast.Assign):
                for target in item.targets:
                    if not isinstance(target, ast.Name):
                        continue
                    info.attrs.add(target.id)
                    call = item.value
                    if (isinstance(call, ast.Call)
                            and isinstance(call.func, ast.Name)
                            and call.func.id == "pyqtSignal"):
                        info.signals[target.id] = len(call.args)
            elif isinstance(item, ast.AnnAssign):
                # dataclass field:  ip: str = "192.168.1.50"
                if isinstance(item.target, ast.Name):
                    info.attrs.add(item.target.id)

        for sub in ast.walk(node):
            if isinstance(sub, (ast.Assign, ast.AnnAssign)):
                targets = (sub.targets if isinstance(sub, ast.Assign)
                           else [sub.target])
                for t in targets:
                    if (isinstance(t, ast.Attribute)
                            and isinstance(t.value, ast.Name)
                            and t.value.id == "self"):
                        info.attrs.add(t.attr)
            elif isinstance(sub, ast.For):
                if (isinstance(sub.target, ast.Attribute)
                        and isinstance(sub.target.value, ast.Name)
                        and sub.target.value.id == "self"):
                    info.attrs.add(sub.target.attr)
        classes[node.name] = (info, node)


def check_members(classes: dict) -> list:
    problems = []
    for name, (info, node) in classes.items():
        known = set(info.members)
        for base in info.bases:
            if base in classes:
                known |= classes[base][0].members
        for sub in ast.walk(node):
            if not (isinstance(sub, ast.Attribute)
                    and isinstance(sub.value, ast.Name)
                    and sub.value.id == "self"):
                continue
            if not isinstance(sub.ctx, ast.Load):
                continue
            attr = sub.attr
            if attr in known or not is_project_name(attr):
                continue
            # A Qt subclass inherits hundreds of members we cannot enumerate,
            # so there we only trust the project's own "_private" convention.
            if has_foreign_base(info, classes) and not attr.startswith("_"):
                continue
            problems.append(f"{info.module}:{sub.lineno}  {name}.self.{attr} "
                            f"is used but never defined")
    return problems


def check_signal_arity(classes: dict) -> list:
    problems = []
    for name, (info, node) in classes.items():
        signals = dict(info.signals)
        for base in info.bases:
            if base in classes:
                signals.update(classes[base][0].signals)
        for sub in ast.walk(node):
            if not (isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "emit"):
                continue
            owner = sub.func.value
            if not (isinstance(owner, ast.Attribute)
                    and isinstance(owner.value, ast.Name)
                    and owner.value.id == "self"):
                continue
            signal = owner.attr
            if signal not in signals:
                continue
            expected, got = signals[signal], len(sub.args)
            if expected != got:
                problems.append(
                    f"{info.module}:{sub.lineno}  {name}.{signal}.emit() passes "
                    f"{got} argument(s) but the signal declares {expected}")
    return problems


def check_cross_module(classes: dict) -> list:
    """Attribute access on an object whose class is obvious from a constructor
    parameter annotation, e.g. `def __init__(self, link: EspLink)`."""
    problems = []
    for name, (info, node) in classes.items():
        bound = {}
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                for arg in item.args.args:
                    ann = arg.annotation
                    if isinstance(ann, ast.Name) and ann.id in classes:
                        bound[arg.arg] = ann.id
                for sub in ast.walk(item):
                    if (isinstance(sub, ast.Assign)
                            and len(sub.targets) == 1
                            and isinstance(sub.targets[0], ast.Attribute)
                            and isinstance(sub.targets[0].value, ast.Name)
                            and sub.targets[0].value.id == "self"
                            and isinstance(sub.value, ast.Name)
                            and sub.value.id in bound):
                        bound["self." + sub.targets[0].attr] = bound[sub.value.id]
        for sub in ast.walk(node):
            if not (isinstance(sub, ast.Attribute)
                    and isinstance(sub.value, ast.Attribute)
                    and isinstance(sub.value.value, ast.Name)
                    and sub.value.value.id == "self"):
                continue
            key = "self." + sub.value.attr
            target = bound.get(key)
            if not target or not is_project_name(sub.attr):
                continue
            other = classes[target][0]
            known = set(other.members)
            for base in other.bases:
                if base in classes:
                    known |= classes[base][0].members
            if sub.attr not in known:
                problems.append(
                    f"{info.module}:{sub.lineno}  {name} uses "
                    f"{key}.{sub.attr}, but {target} has no such member")
    return problems



BUILTINS = set(dir(__builtins__)) | {
    "self", "cls", "__file__", "__name__", "__doc__", "annotations"}


def check_undefined_names(root: pathlib.Path) -> list:
    """Names used at module level that are never imported, defined or built in.

    This is what a missing `from rng import entropy` looks like: it compiles
    fine and only explodes when that line finally runs.
    """
    problems = []
    for module in MODULES:
        path = root / module
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        defined = set(BUILTINS)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    defined.add((a.asname or a.name).split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    defined.add(a.asname or a.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                defined.add(node.id)
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                defined.add(node.name)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                defined.update(node.names)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if node.id not in defined:
                    problems.append(f"{module}:{node.lineno}  name '{node.id}' "
                                    f"is used but never imported or defined")
    return sorted(set(problems))


def main() -> int:
    root = pathlib.Path(__file__).parent
    classes: dict = {}
    for module in MODULES:
        path = root / module
        if path.exists():
            collect(path, classes)

    print(f"Scanned {len(MODULES)} modules, {len(classes)} classes.\n")

    failed = False
    for title, problems in (
            ("Missing members", check_members(classes)),
            ("Signal arity", check_signal_arity(classes)),
            ("Cross-module access", check_cross_module(classes)),
            ("Undefined names", check_undefined_names(root))):
        if problems:
            failed = True
            print(f"[FAIL] {title}")
            for p in problems:
                print("   ", p)
            print()
        else:
            print(f"[ OK ] {title}")

    print("\nRESULT:", "PROBLEMS FOUND" if failed else "CLEAN")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
