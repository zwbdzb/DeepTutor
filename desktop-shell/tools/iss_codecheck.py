"""Lightweight structural check for the [Code] section of an Inno Setup .iss.

NOT a compiler. It only catches the mechanical mistakes that a text edit can
introduce (unbalanced begin/end, `;` right before `else`, unterminated strings
or comments). Comment/string aware so that ExpandConstant('{app}') is not
mistaken for a brace comment.

Usage:
    python tools/iss_codecheck.py build/installer.iss
    python tools/iss_codecheck.py --diff <base.iss> <head.iss>   # compare signatures
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def strip_to_code(text: str) -> tuple[str, list[str]]:
    """Return (code with strings/comments blanked out, problems)."""
    out = []
    problems = []
    i, n = 0, len(text)
    line = 1
    while i < n:
        ch = text[i]
        if ch == "\n":
            line += 1
            out.append("\n")
            i += 1
        elif ch == "'":                                   # string literal, '' = escaped quote
            i += 1
            while i < n:
                if text[i] == "\n":
                    problems.append(f"L{line}: unterminated string")
                    line += 1
                    break
                if text[i] == "'":
                    if i + 1 < n and text[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append(" Str ")
        elif ch == "/" and i + 1 < n and text[i + 1] == "/":   # line comment
            while i < n and text[i] != "\n":
                i += 1
            out.append(" ")
        elif ch == "{":                                   # brace comment
            start = line
            while i < n and text[i] != "}":
                if text[i] == "\n":
                    line += 1
                i += 1
            if i >= n:
                problems.append(f"L{start}: unterminated {{ comment")
            i += 1
            out.append(" ")
        elif ch == "(" and i + 1 < n and text[i + 1] == "*":   # (* comment *)
            start = line
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == ")"):
                if text[i] == "\n":
                    line += 1
                i += 1
            if i + 1 >= n:
                problems.append(f"L{start}: unterminated (* comment")
            i += 2
            out.append(" ")
        else:
            out.append(ch)
            i += 1
    return "".join(out), problems


def code_section(text: str) -> str:
    m = re.search(r"(?im)^\[Code\][ \t]*\r?$", text)
    if not m:
        return ""
    start = m.end()
    nxt = re.compile(r"(?im)^\[[A-Za-z]+\][ \t]*\r?$").search(text, start)
    return text[start: nxt.start() if nxt else len(text)]


def read_text(p: Path) -> str:
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def metrics(code: str) -> dict:
    code, problems = strip_to_code(code)
    low = code.lower()
    return {
        "begin": len(re.findall(r"\bbegin\b", low)),
        "end": len(re.findall(r"\bend\b", low)),
        "if": len(re.findall(r"\bif\b", low)),
        "then": len(re.findall(r"\bthen\b", low)),
        "else": len(re.findall(r"\belse\b", low)),
        "repeat": len(re.findall(r"\brepeat\b", low)),
        "until": len(re.findall(r"\buntil\b", low)),
        "try": len(re.findall(r"\btry\b", low)),
        "finally": len(re.findall(r"\bfinally\b", low)),
        "except": len(re.findall(r"\bexcept\b", low)),
        "semicolon_else": len(re.findall(r";\s*else\b", low)),
        "problems": problems,
    }


def verdict(m: dict, base: dict | None) -> list[str]:
    """With a baseline, judge DELTAS (the edit-induced change), not absolutes.

    Absolute begin/end counts do not match in any real Inno script (procedures
    opened by `begin` are closed by `end;`, but `case`/`record`/`end` inside
    multi-line expressions vary), so only the delta is meaningful.
    """
    errs = []
    if base is None:
        if m["begin"] != m["end"]:
            errs.append(f"[info] begin/end differ absolutely: begin={m['begin']} end={m['end']}")
        if m["if"] != m["then"]:
            errs.append(f"[FAIL] if/then mismatch: if={m['if']} then={m['then']}")
        if m["semicolon_else"]:
            errs.append(f"[FAIL] {m['semicolon_else']}x ';' directly before 'else'")
        return errs

    d_begin = m["begin"] - base["begin"]
    d_end = m["end"] - base["end"]
    d_if = m["if"] - base["if"]
    d_then = m["then"] - base["then"]
    d_try = m["try"] - base["try"]
    d_close = (m["finally"] + m["except"]) - (base["finally"] + base["except"])
    d_repeat = m["repeat"] - base["repeat"]
    d_until = m["until"] - base["until"]

    if d_begin != d_end:
        errs.append(f"[FAIL] begin/end delta unbalanced: begin{d_begin:+d} end{d_end:+d}")
    if d_if != d_then:
        errs.append(f"[FAIL] if/then delta unbalanced: if{d_if:+d} then{d_then:+d}")
    if d_try != d_close:
        errs.append(f"[FAIL] try/finally|except delta unbalanced: try{d_try:+d} close{d_close:+d}")
    if d_repeat != d_until:
        errs.append(f"[FAIL] repeat/until delta unbalanced: repeat{d_repeat:+d} until{d_until:+d}")
    if m["semicolon_else"] > base["semicolon_else"]:
        errs.append(f"[FAIL] new ';else' introduced: {base['semicolon_else']} -> {m['semicolon_else']}")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("iss", nargs="?", default="build/installer.iss")
    ap.add_argument("--baseline", default=None, help="compare against this (e.g. `git show HEAD:...`)")
    args = ap.parse_args()

    p = Path(args.iss)
    if not p.is_file():
        print(f"[error] not found: {p}", file=sys.stderr)
        return 2
    code = code_section(read_text(p))
    if not code.strip():
        print("[error] no [Code] section", file=sys.stderr)
        return 2

    m = metrics(code)
    base = None
    if args.baseline:
        bp = Path(args.baseline)
        base = metrics(code_section(read_text(bp)))

    errs = verdict(m, base)
    fails = [e for e in errs if e.startswith("[FAIL]")]
    print(f"== {p}")
    print("   " + "  ".join(f"{k}={v}" for k, v in m.items() if k not in ("problems",)))
    if base:
        print(f"== baseline {args.baseline}")
        print("   " + "  ".join(f"{k}={v}" for k, v in base.items() if k not in ("problems",)))
    for x in errs:
        print("   " + x)
    if fails:
        return 1
    print("   [OK] no structural regression introduced")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

