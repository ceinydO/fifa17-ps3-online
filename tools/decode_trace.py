#!/usr/bin/env python3
"""Dekoduje znaczniki z RPCS3.log zapisane przez late z tools/rpcs3_patch_trace_invites.yml.

    python tools/decode_trace.py RPCS3.log [--elf EBOOT.ELF] [--all]

Wypisuje chronologicznie: kiedy (czas emulatora i, jesli jest naglowek, zegar scienny), w ktorym watku i ktora
funkcja zostala wywolana, z argumentami r3/r4/r5. Dla zdarzen UI (znacznik 0x01) pokazuje nazwe EVENT_*
(wymaga --elf = EBOOT.ELF z tej samej wersji gry; bez tego wypisuje sam adres napisu).
Zwykle zdarzenia sa domyslnie pomijane, jesli wystepuja bardzo czesto -- --all wypisuje wszystkie.
Dodatkowo pokazuje wywolania NP (sceNpBasic*), zeby bylo widac je obok znacznikow.
Runda 5: znacznik 0x82 = odbior zaproszenia u kolegi (zdarzenie NP 5/23 -> sceNpBasicRecvMessageCustom; k=1 = wynik, 0 = wybrano
zaproszenie); potem powinny pojawic sie 0x80 (zdarzenie 14), 0x83 (odczyt danych zaproszenia), 0x84 InviteAccepted,
0x85/0x86 GameInviteValidation/GameInviteJoin i 0x7E/0x7F GameSessionManager::joinGame (+ `OSDK GBRS/JOIN`).
Runda 4: znacznik 0x81 = wynik eksperymentu "natywne SendGameInvite" (kto jest pierwszym znajomym RPCN; potem powinny
pojawic sie 0x10 adaptor SendGameInvite, 0x12/0x13/0x14/0x15/0x16 lancuch zaproszenia i `sceNpBasicSendMessageGui`).
Runda 3: przy kazdym znaczniku "<- LR (fn START)" = kto wolal (wymaga --elf); znacznik 0x70 pokazuje, jaki kod
listenera zostal wywolany po udanym createGame; 0x71/0x72 to wywolania funkcji skryptow UI ("Handle_<nazwa>").
"""
from __future__ import annotations

import datetime
import os
import re
import struct
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_trace_patch import TARGETS, MIDS      # noqa: E402

NAMES = {t[0]: t[1] for t in TARGETS}
NAMES.update({t[0]: t[1] for t in MIDS})
NAMES[0x81] = "eksperyment: natywne SendGameInvite"
NAMES[0x82] = "eksperyment: odbior zaproszenia (sceNpBasicRecvMessageCustom)"
MID_IDS = {t[0] for t in MIDS}
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
                txt = raw.split(b"\0", 1)[0]
                if len(txt) >= 3 and all(32 <= c < 127 for c in txt):
                    return txt.decode("latin-1")
                return None
        return None

    def u32(self, va):
        for v, o, fs in self.segs:
            if v <= va < v + fs:
                return struct.unpack(">I", self.d[o + va - v:o + va - v + 4])[0]
        return None

    def func_start(self, va, limit=0x6000):
        """Poczatek funkcji zawierajacej adres va (heurystyka: `mflr r0` + `bl _savegpr` albo `stdu r1; mflr r0`)."""
        x = va & ~3
        while x > va - limit:
            w = self.u32(x)
            if w == 0x7C0802A6:
                nx, pv = self.u32(x + 4), self.u32(x - 4)
                if nx is not None and (nx >> 26) == 18 and (nx & 1):
                    off = nx & 0x03FFFFFC
                    if off & 0x2000000:
                        off -= 0x4000000
                    if 0x1955000 <= ((x + 4 + off) & 0xFFFFFFFF) < 0x1956000:
                        return x
                if pv is not None and (pv & 0xFFFF0000) == 0xF8210000 and (pv & 3) == 1:
                    return x - 4
            elif w is not None and (w & 0xFFFF0000) == 0xF8210000 and (w & 3) == 1 and self.u32(x + 4) == 0x7C0802A6:
                return x
            x -= 4
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

    def who(v):
        """'<- 0xLR (fn START)' -- kto wolal."""
        if not v:
            return ""
        fs = elf.func_start(v) if elf else None
        return " <- %08X%s" % (v, " (fn %08X)" % fs if fs else "")

    def txt(v):
        s_ = elf.cstr(v) if (elf and v) else None
        return '"%s"' % s_ if s_ else "%08X" % (v or 0)

    for ev in events:
        ts = ev["t"]
        wall = ""
        if wall0:
            wall = (wall0 + datetime.timedelta(seconds=ts)).strftime("%H:%M:%S") + " "
        mt = re.search(r"Thread \(([^)]*)\)", ev["thread"] or "")
        th = mt.group(1) if mt else (ev["thread"] or "")
        head = "%s%9.3f %-14s" % (wall, ts, th[:14])
        if "np" in ev:
            print("%s NP   %s(%s)" % (head, ev["np"], ev["text"]))
            continue
        a = ev["args"]
        lr = a.get(3, 0)
        if ev["id"] == 1:
            if ev["evname"] in noisy:
                continue
            print("%s EVT  %s%s" % (head, ev["evname"], who(lr)))
            continue
        if ev["id"] == 0x50:
            def cc(v):
                b = v.to_bytes(4, "big")
                return b.decode("latin-1") if all(32 <= c < 127 for c in b) else "%08X" % v
            print("%s OSDK %s/%s%s" % (head, cc(a.get(0, 0)), cc(a.get(1, 0)), who(lr)))
            continue
        if ev["id"] in (0x71, 0x72):
            nm = elf.cstr(a.get(2, 0)) if elf else None
            print("%s UI-SKRYPT %s(%s, %s)%s" % (head, nm or "%08X" % a.get(2, 0), txt(a.get(5, 0)), txt(a.get(6, 0)),
                                                who(lr)))
            continue
        if ev["id"] == 0x81:
            res = a.get(0, 0)
            nm = b"".join(a.get(k, 0).to_bytes(4, "big") for k in (1, 2)).split(b"\0", 1)[0].decode("latin-1")
            print("%s INVITE-STUB sceNpBasicGetFriendPresenceByIndex(0) -> %s%s  nazwa znajomego='%s'" % (
                head, "OK" if res == 0 else "BLAD 0x%08X" % res, "" if res == 0 else " (brak znajomego/RPCN?)", nm))
            continue
        if ev["id"] == 0x82:
            if 1 in a and 0 not in a:
                res = a[1]
                print("%s RECV-STUB sceNpBasicRecvMessageCustom(INVITE) -> 0x%08X %s" % (
                    head, res, "(zaproszenie wybrane)" if res == 0 else "(anulowano / blad)"))
            else:
                print("%s RECV-STUB zdarzenie NP %d (5=zaproszenie, 23=zaproszenie bootowalne) -> otwieram liste zaproszen" % (
                    head, a.get(0, 0)))
            continue
        if ev["id"] == 0x70:
            code = a.get(2, 0)
            print("%s 0x70 createGame -> listener=%08X sesja=%08X WOLA KOD %08X%s" % (
                head, a.get(0, 0), a.get(1, 0), code, " (fn %08X)" % (elf.func_start(code) or 0) if elf else ""))
            continue
        extra = who(lr) if (3 in a and ev["id"] not in MID_IDS) else ""
        print("%s 0x%02X %-52s r3=%08X r4=%08X r5=%08X%s" % (
            head, ev["id"], NAMES.get(ev["id"], "?"), a.get(0, 0), a.get(1, 0), a.get(2, 0), extra))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
