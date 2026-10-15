#!/usr/bin/env python3
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ddm.i18n import parse_po

DYNAMIC = ["bekliyor", "bağlanıyor", "devralındı", "indiriyor", "bekletiliyor", "yeniden deneniyor", "alan adı"]


def extract():
    ids, plurals = {}, {}
    for path in sorted((ROOT / "ddm").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("_", "N_")
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                target = plurals if node.func.id == "N_" else ids
                target.setdefault(node.args[0].value, f"{path.relative_to(ROOT)}:{node.lineno}")
    for text in DYNAMIC:
        ids.setdefault(text, "dynamic")
    return ids, plurals


def quote(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def update(po):
    ids, plurals = extract()
    catalog, plural = parse_po(po) if po.exists() else ({}, {})
    missing = [i for i in ids if i not in catalog] + [p for p in plurals if p not in plural]
    if not missing:
        return []
    lines = [""]
    for text in missing:
        lines.append(f"#: {ids.get(text) or plurals.get(text)}")
        lines.append(f"msgid {quote(text)}")
        if text in plurals:
            lines += [f"msgid_plural {quote(text)}", 'msgstr[0] ""', 'msgstr[1] ""']
        else:
            lines.append('msgstr ""')
        lines.append("")
    with open(po, "a", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return missing


def check(po):
    ids, plurals = extract()
    catalog, plural = parse_po(po)
    missing = [i for i in ids if i not in catalog] + [p for p in plurals if p not in plural]
    unused = [k for k in catalog if k not in ids] + [k for k in plural if k not in plurals]
    return missing, unused


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    lang = sys.argv[2] if len(sys.argv) > 2 else "en"
    po = ROOT / "ddm" / "locale" / f"{lang}.po"
    if cmd == "update":
        added = update(po)
        print(f"{len(added)} new entries added to {po.relative_to(ROOT)}")
        return 0
    missing, unused = check(po)
    for text in missing:
        print(f"untranslated: {text!r}")
    for text in unused:
        print(f"unused: {text!r}")
    print(f"{po.relative_to(ROOT)}: {len(missing)} untranslated, {len(unused)} unused")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
