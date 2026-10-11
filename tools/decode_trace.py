#!/usr/bin/env python3
"""Dekoduje znaczniki z RPCS3.log zapisane przez late z tools/rpcs3_patch_trace_invites.yml.

    python tools/decode_trace.py RPCS3.log [--elf EBOOT.ELF] [--all]

Wypisuje chronologicznie: kiedy (czas emulatora i, jesli jest naglowek, zegar scienny), w ktorym watku i ktora
funkcja zostala wywolana, z argumentami r3/r4/r5. Dla zdarzen UI (znacznik 0x01) pokazuje nazwe EVENT_*
(wymaga --elf = EBOOT.ELF z tej samej wersji gry; bez tego wypisuje sam adres napisu).
Zwykle zdarzenia sa domyslnie pomijane, jesli wystepuja bardzo czesto -- --all wypisuje wszystkie.
Dodatkowo pokazuje wywolania NP (sceNpBasic*), zeby bylo widac je obok znacznikow.
"""
from __future__ import annotations

import datetime
import os
import re
import struct
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_trace_patch import TARGETS      # noqa: E402

NAMES = {t[0]: t[1] for t in TARGETS}
LINE = re.compile(r"^\S+\s+(\d+):(\d\d):(\d\d)\.(\d+)\s+(?:\{([^}]*)\}\s+)?(.*)$")
RTC = re.compile(r"cellRtcSetTime_t\(pDateTime=\*0x[0-9a-fA-F]+, iTime=0x([0-9a-fA-F]+)\)")
NP = re.compile(r"(sceNp(?:Basic|Lookup|Manager)\w*)\(([^)]*)\)")
HDR = re.compile(r"Current Time:\s*(\S+)")


class Elf:
    def __init__(self, path):
        self.d = open(path, "rb").read()
        phoff = struct.unpack(">Q", self.d[32:40])[0]
        n = struct.unpack(">H", self.d[56:58])[0]
        self.segs = []
        for i in range(n):
            o = phoff + i * 56
            t, fl, off, va, pa, fs, ms = struct.unpack(">IIQQQQQ", self.d[o:o + 48])
            if t == 1 and fs:
                self.segs.append((va, off, fs))

    def cstr(self, va, maxlen=96):
        for v, o, fs in self.segs:
            if v <= va < v + fs:
                raw = self.d[o + va - v:o + va - v + maxlen]
                return raw.split(b"\0", 1)[0].decode("latin-1")
        return None


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    show_all = "--all" in argv
    elf = None
    if "--elf" in argv:
        elf = Elf(argv[argv.index("--elf") + 1])
        args = [a for a in args if a != argv[argv.index("--elf") + 1]]
    if not args:
        print(__doc__)
        return 2
    wall0 = None
    events = []
    pending = {}
    for raw in open(args[0], encoding="utf-8", errors="replace"):
        if wall0 is None:
            m = HDR.search(raw)
            if m:
                try:
                    wall0 = datetime.datetime.fromisoformat(m.group(1))
                except ValueError:
                    pass
        m = LINE.match(raw.rstrip("\n"))
        if not m:
            continue
        h, mi, s, frac, thread, msg = m.groups()
        t = int(h) * 3600 + int(mi) * 60 + int(s) + float("0." + frac)
        r = RTC.search(msg)
        if r:
            v = int(r.group(1), 16)
            if (v >> 56) == 0xFA:
                mid, k, val = (v >> 48) & 0xFF, (v >> 32) & 0xFFFF, v & 0xFFFFFFFF
                key = (thread, mid)
                ev = pending.get(key)
                if ev is None or k in ev["args"]:
                    ev = {"t": t, "thread": thread, "id": mid, "args": {}}
                    pending[key] = ev
                    events.append(ev)
                ev["args"][k] = val
            continue
        n = NP.search(msg)
        if n:
            events.append({"t": t, "thread": thread, "np": n.group(1), "text": n.group(2)[:90]})
    evt_counts = Counter()
    for ev in events:
        if ev.get("id") == 1:
            name = elf.cstr(ev["args"].get(1, 0)) if elf else None
            ev["evname"] = name or "0x%08X" % ev["args"].get(1, 0)
            evt_counts[ev["evname"]] += 1
    noisy = {n for n, c in evt_counts.items() if c > 40 and not show_all}
    print("# znacznikow: %d, wywolan NP: %d" % (sum(1 for e in events if "id" in e), sum(1 for e in events if "np" in e)))
    if noisy:
        print("# pomijam bardzo czeste zdarzenia (uzyj --all): %s" % ", ".join("%s x%d" % (n, evt_counts[n]) for n in sorted(noisy)))
    for ev in events:
        ts = ev["t"]
        wall = ""
        if wall0:
            wall = (wall0 + datetime.timedelta(seconds=ts)).strftime("%H:%M:%S") + " "
        mt = re.search(r"Thread \(([^)]*)\)", ev["thread"] or "")
        th = mt.group(1) if mt else (ev["thread"] or "")
        if "np" in ev:
            print("%s%9.3f %-14s NP   %s(%s)" % (wall, ts, th[:14], ev["np"], ev["text"]))
            continue
        if ev["id"] == 1:
            if ev["evname"] in noisy:
                continue
            print("%s%9.3f %-14s EVT  %s" % (wall, ts, th[:14], ev["evname"]))
            continue
        a = ev["args"]
        print("%s%9.3f %-14s 0x%02X %-52s r3=%08X r4=%08X r5=%08X" % (
            wall, ts, th[:14], ev["id"], NAMES.get(ev["id"], "?"), a.get(0, 0), a.get(1, 0), a.get(2, 0)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
