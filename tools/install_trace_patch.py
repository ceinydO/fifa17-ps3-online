#!/usr/bin/env python3
"""Dopisuje (albo usuwa) latke-znacznik 'FIFA trace' do patch.yml RPCS3 -- zeby nie wklejac recznie.

    python tools/install_trace_patch.py "C:\\rcps3"            # dopisz / zaktualizuj
    python tools/install_trace_patch.py "C:\\rcps3" --remove    # usun

Argument to folder z rpcs3.exe (tam jest podfolder patches\\patch.yml). Tworzy kopie patch.yml.bak.
Po dopisaniu: RPCS3 -> Manage -> Patch Manager -> FIFA 17 -> BLES02233 -> zaznacz
"FIFA trace (zaproszenia i zdarzenia UI)" i uruchom gre od nowa. Pierwsze uruchomienie kompiluje PPU (kilka minut).
"""
from __future__ import annotations

import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "rpcs3_patch_trace_invites.yml")
KEY = "PPU-1243af2b292938038cdc3696755e82477122db3b"
NAME = "FIFA trace (zaproszenia i zdarzenia UI):"


def entry_lines() -> list[str]:
    lines = open(SRC, encoding="utf-8").read().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("  " + NAME))
    return lines[start:]


def strip_entry(lines: list[str]) -> list[str]:
    """Usuwa istniejacy wpis 'FIFA trace' (od jego naglowka do nastepnego klucza na tym samym lub wyzszym poziomie)."""
    out, skipping = [], False
    for ln in lines:
        body = ln.rstrip("\r\n")
        if body.startswith("  " + NAME):
            skipping = True
            continue
        if skipping:
            indent = len(body) - len(body.lstrip(" "))
            if body.strip() and not body.lstrip().startswith("#") and indent <= 2:
                skipping = False
            else:
                continue
        out.append(ln)
    return out


def install(path: str, remove: bool = False) -> str:
    raw = open(path, "rb").read().decode("utf-8")
    nl = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.splitlines(keepends=True)
    lines = strip_entry(lines)
    if not remove:
        new = [ln + nl for ln in entry_lines()]
        idx = next((i for i, ln in enumerate(lines) if ln.rstrip("\r\n") == KEY + ":"), None)
        if idx is None:
            if lines and not lines[-1].endswith(("\n", "\r\n")):
                lines[-1] += nl
            lines += [KEY + ":" + nl] + new
        else:
            lines[idx + 1:idx + 1] = new
    return "".join(lines)


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    base = args[0]
    path = base if base.lower().endswith(".yml") else os.path.join(base, "patches", "patch.yml")
    if not os.path.exists(path):
        print("Nie znaleziono %s -- podaj folder z rpcs3.exe (powinien miec podfolder patches)." % path)
        return 1
    text = install(path, remove="--remove" in argv)
    shutil.copyfile(path, path + ".bak")
    with open(path, "wb") as fh:
        fh.write(text.encode("utf-8"))
    print(("Usunieto wpis z " if "--remove" in argv else "Dopisano wpis do ") + path + "  (kopia: patch.yml.bak)")
    if "--remove" not in argv:
        print("Teraz: RPCS3 -> Manage -> Patch Manager -> FIFA 17 -> BLES02233 v01.00 -> zaznacz '%s'" % NAME.rstrip(":"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
