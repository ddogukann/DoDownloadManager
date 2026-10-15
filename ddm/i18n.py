import ast
import json
import os
from pathlib import Path

LOCALE_DIR = Path(__file__).resolve().parent / "locale"
LANGUAGES = [("tr", "Türkçe"), ("en", "English")]
CODES = [code for code, _name in LANGUAGES]

LANG = "tr"
_catalog = {}
_plural = {}


def system_language():
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        val = os.environ.get("DDM_SYSTEM_LANGUAGE", "") if var == "LANGUAGE" else os.environ.get(var, "")
        if not val:
            continue
        first = val.split(":")[0].lower()
        if first in ("c", "posix") or first.startswith(("c.", "c_")):
            continue
        return next((code for code in CODES if first.startswith(code)), "en")
    return "en"


def stored_language():
    from .config import SETTINGS_PATH
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            value = json.load(f).get("language", "auto")
    except (OSError, ValueError, AttributeError):
        value = "auto"
    return value if value == "auto" or value in CODES else "auto"


def resolve(choice):
    return system_language() if choice in (None, "", "auto") else choice


def _unquote(chunk):
    try:
        return ast.literal_eval(chunk)
    except (ValueError, SyntaxError):
        return chunk.strip('"')


def parse_po(path):
    catalog, plural = {}, {}
    entry, key = {}, None

    def flush():
        msgid = entry.get("msgid")
        if msgid:
            if "msgid_plural" in entry:
                forms = [entry.get(f"msgstr[{i}]", "") for i in range(2)]
                if all(forms):
                    plural[msgid] = forms
            elif entry.get("msgstr"):
                catalog[msgid] = entry["msgstr"]

    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                if line == "" and entry:
                    flush()
                    entry, key = {}, None
                continue
            if line.startswith('"'):
                if key:
                    entry[key] += _unquote(line)
                continue
            word, _, rest = line.partition(" ")
            if word in ("msgid", "msgid_plural", "msgstr", "msgctxt") or word.startswith("msgstr["):
                if word == "msgid" and "msgid" in entry:
                    flush()
                    entry = {}
                key = word
                entry[key] = _unquote(rest)
    if entry:
        flush()
    return catalog, plural


def setup(choice=None):
    global LANG, _catalog, _plural
    os.environ.setdefault("DDM_SYSTEM_LANGUAGE", os.environ.get("LANGUAGE", ""))
    LANG = resolve(stored_language() if choice is None else choice)
    _catalog, _plural = {}, {}
    if LANG != "tr":
        po = LOCALE_DIR / f"{LANG}.po"
        if po.exists():
            _catalog, _plural = parse_po(po)
    os.environ["LANGUAGE"] = LANG
    return LANG


def _(text):
    if LANG == "tr" or not text:
        return text
    return _catalog.get(text, text)


def N_(text, n):
    if LANG == "tr":
        return text
    forms = _plural.get(text)
    if forms:
        return forms[0] if n == 1 else forms[1]
    return _catalog.get(text, text)


def pct(value):
    return f"%{value}" if LANG == "tr" else f"{value}%"


def decimal(text):
    return text.replace(".", ",") if LANG == "tr" else text
