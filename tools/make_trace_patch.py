#!/usr/bin/env python3
"""Generator lat RPCS3 ("znacznikow") do sledzenia, ktore funkcje FIFA 17 sa faktycznie wywolywane.

Po co: logi serwera widza tylko ruch Blaze. To, czy gra po createGame w ogole probuje wyslac zaproszenie (NP),
oraz jakie zdarzenia UI (EVENT_*) odpalaja sie u hosta i u kolegi, jest widoczne tylko po stronie gry.
Lata dopisuje do RPCS3.log linie typu

    cellRtc: cellRtcSetTime_t(pDateTime=*0xd012cba8, iTime=0xfa1200010000a1b2)

(RPCS3 loguje argumenty kazdego wywolania HLE). Gra sama wola cellRtcSetTime_t z normalnym czasem
(iTime=0x5884....), wiec nasze znaczniki (zaczynaja sie od 0xfa) sa latwe do odroznienia.

Kodowanie iTime:  0xFA | id(8 bitow) | k(16 bitow) | wartosc(32 bity)
  id = numer znacznika (tabela TARGETS), k = ktory argument (0 = r3, 1 = r4, 2 = r5), wartosc = jego dolne 32 bity.
Dla znacznika zdarzen (id 0x01) logowany jest tylko r4 (k=1) = wskaznik na nazwe zdarzenia (tools/decode_trace.py
zamienia go na tekst "EVENT_...").

Mechanizm (bezpieczny dla rekompilatora PPU RPCS3): wszystkie obserwowane funkcje maja na poczatku
    mflr r0 ; bl _savegpr_N
Podmieniamy TYLKO drugie slowo (bl _savegpr_N -> bl S_k). S_k zapisuje LR, wola wspolna procedure `common`
(zachowuje WSZYSTKIE rejestry r0-r12, f1-f13, LR, CR, CTR), potem przywraca LR i robi `b _savegpr_N`, wiec dla
oryginalnej funkcji nic sie nie zmienia. Kod i stuby leza w nieuzywanym, wyzerowanym obszarze segmentu kodu.

Uzycie:
    python tools/make_trace_patch.py            # zapisuje tools/rpcs3_patch_trace_invites.yml
    python tools/make_trace_patch.py --selftest # sprawdza kodowanie (capstone, jesli jest) i semantyke (emulator)
    python tools/make_trace_patch.py --check    # porownuje plik z generowanym (do selftestu repo)
    python tools/make_trace_patch.py --verify-elf EBOOT.ELF   # sprawdza cele na prawdziwym EBOOT (mflr + bl H)
"""
from __future__ import annotations

import os
import random
import struct
import sys

CAVE = 0x0211D810                 # wyzerowany obszar w segmencie kodu EBOOT (0x9f0 bajtow wolnych)
CAVE_END = 0x0211E200
RTC_STUB = 0x01EA2160             # import cellRtcSetTime_t (patrz RPCS3.log: "cellRtc import ... -> 0x1ea2160")
PPU_HASH = "PPU-1243af2b292938038cdc3696755e82477122db3b"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rpcs3_patch_trace_invites.yml")

# (id, nazwa, adres funkcji T, adres pomocnika H z "bl H" w T+4, tryb: 3 = loguj r3,r4,r5; 1 = tylko r4)
TARGETS = [
    (0x01, "EVT  post_event(this, nazwa_zdarzenia, dane)", 0x015FFB68, 0x01955538, 1),
    (0x10, "adaptor SendGameInvite (UI -> C++)", 0x016491B4, 0x01955530, 3),
    (0x11, "adaptor SendMultipleGameInvites (UI -> C++)", 0x01649264, 0x01955524, 3),
    (0x12, "ginv::SendGameInvite", 0x00301AD4, 0x01955528, 3),
    (0x13, "Invitations op ctor (SendGameInvite)", 0x003019A4, 0x0195552C, 3),
    (0x14, "puid: LookupPlatformUserId start", 0x002CD938, 0x01955530, 3),
    (0x15, "puid: wynik wyszukiwania (30262c)", 0x0030262C, 0x0195552C, 3),
    (0x16, "NP: ostatni krok przed sceNpBasicSendMessageGui (302454)", 0x00302454, 0x01955538, 3),
    (0x17, "ginv: rejestracja modulu zaproszen (302184)", 0x00302184, 0x01955538, 3),
    (0x18, "SendPartyGameInvites", 0x00301C88, 0x01955538, 3),
    (0x20, "createGame: callback sukcesu 'GSMR' (2dee08)", 0x002DEE08, 0x0195552C, 3),
    (0x30, "NP: handler zdarzen basic (zaproszenia/wiadomosci)", 0x00AE0C04, 0x01955534, 3),
    (0x31, "NP: callback managera", 0x00ADF1DC, 0x01955538, 3),
    (0x40, "SportsWorld: ConnectToSportsWorld", 0x00309388, 0x01955538, 3),
    (0x41, "SportsWorld: LoadSportsWorldUserPresence", 0x0030F2F0, 0x01955534, 3),
    (0x42, "SportsWorld: SendFriendsToSportsWorld", 0x0030F068, 0x0195553C, 3),
    (0x43, "SportsWorld: LoadSportsWorldUsers", 0x00311FDC, 0x01955538, 3),
    (0x44, "SportsWorld: SendMatchupStatsToSportsWorld", 0x0030E498, 0x0195551C, 3),
    # --- runda 2: dlaczego po createGame nie ma EVENT_CREATEGAME_SUCCESS ---
    (0x50, "OSDK trace(kategoria 4cc, zdarzenie 4cc)", 0x002E8448, 0x01955538, 3),
    (0x51, "GameSessionManager::createGame(parametry, listener)", 0x0028EFA4, 0x01955510, 3),
    (0x52, "CreateGame op: konstruktor", 0x0028E208, 0x01955518, 3),
    (0x53, "adaptor UI CreateGame (1624180)", 0x01624180, 0x01955538, 3),
    (0x54, "GameSession: konstruktor", 0x002891E4, 0x01955534, 3),
    (0x55, "gses::setCurrentSession", 0x002911B8, 0x01955538, 3),
    (0x56, "SetPlayerAttribute op: utworzenie (po createGame)", 0x0028A384, 0x01955534, 3),
    (0x57, "createGame: koniec po SetPlayerAttribute (2deb88)", 0x002DEB88, 0x01955540, 3),
    (0x58, "createGame: porazka (2ded74)", 0x002DED74, 0x01955540, 3),
    (0x59, "listener createGame: SUKCES (1624e1c)", 0x01624E1C, 0x0195553C, 3),
    (0x5A, "listener createGame: PORAZKA (1624e9c)", 0x01624E9C, 0x01955544, 3),
    (0x5B, "Blaze: handler NotifyGameSetup", 0x00C6E0B8, 0x0195551C, 3),
    (0x5C, "Blaze: onNetworkCreated (c6a4e0)", 0x00C6A4E0, 0x01955530, 3),
    (0x5D, "Blaze: wysylka finalizeGameCreation (c6a204)", 0x00C6A204, 0x0195553C, 3),
    (0x5E, "Blaze: odpowiedz na finalizeGameCreation (c6bf74)", 0x00C6BF74, 0x0195553C, 3),
    (0x5F, "FSM poczekalni: start (4d087c)", 0x004D087C, 0x01955538, 3),
    (0x60, "FSM poczekalni: przerwanie/timeout (4cf708)", 0x004CF708, 0x01955538, 3),
    (0x61, "FSM poczekalni: konsument zdarzen (4d0aa4)", 0x004D0AA4, 0x01955514, 3),
    (0x62, "FSM poczekalni: 4cf59c", 0x004CF59C, 0x01955530, 3),
    (0x63, "FifaOnline::StartInviteFlowPrep (4cf1f8)", 0x004CF1F8, 0x01955538, 3),
    (0x64, "GameInviteScreenAbort (4cf2f8)", 0x004CF2F8, 0x0195553C, 3),
]


# ---------------------------------------------------------------- mini asembler (tylko uzywane instrukcje)
def _ds(op, rs, ra, d, xo):
    return (op << 26) | (rs << 21) | (ra << 16) | (d & 0xFFFC) | xo


def std(rs, d, ra): return _ds(62, rs, ra, d, 0)
def stdu(rs, d, ra): return _ds(62, rs, ra, d, 1)
def ld(rd, d, ra): return _ds(58, rd, ra, d, 0)
def lwz(rd, d, ra): return (32 << 26) | (rd << 21) | (ra << 16) | (d & 0xFFFF)
def stfd(fr, d, ra): return (54 << 26) | (fr << 21) | (ra << 16) | (d & 0xFFFF)
def lfd(fr, d, ra): return (50 << 26) | (fr << 21) | (ra << 16) | (d & 0xFFFF)
def addi(rd, ra, imm): return (14 << 26) | (rd << 21) | (ra << 16) | (imm & 0xFFFF)
def addis(rd, ra, imm): return (15 << 26) | (rd << 21) | (ra << 16) | (imm & 0xFFFF)
def lis(rd, imm): return addis(rd, 0, imm)
def ori(ra, rs, imm): return (24 << 26) | (rs << 21) | (ra << 16) | (imm & 0xFFFF)
def or_(ra, rs, rb): return (31 << 26) | (rs << 21) | (ra << 16) | (rb << 11) | (444 << 1)
def mflr(rd): return 0x7C0802A6 | (rd << 21)
def mtlr(rs): return 0x7C0803A6 | (rs << 21)
def mfcr(rd): return 0x7C000026 | (rd << 21)
def mtcrf_all(rs): return 0x7C000120 | (rs << 21) | (0xFF << 12)
def mfctr(rd): return 0x7C0902A6 | (rd << 21)
def mtctr(rs): return 0x7C0903A6 | (rs << 21)
BLR = 0x4E800020


def _md(rs, ra, sh, mbme, xo):
    return (30 << 26) | (rs << 21) | (ra << 16) | ((sh & 31) << 11) | ((mbme & 31) << 6) | \
           (((mbme >> 5) & 1) << 5) | (xo << 2) | (((sh >> 5) & 1) << 1)


def sldi(ra, rs, n): return _md(rs, ra, n, 63 - n, 1)           # rldicr ra,rs,n,63-n
def clrldi(ra, rs, n): return _md(rs, ra, 0, n, 0)              # rldicl ra,rs,0,n


def branch(src, dst, link):
    off = dst - src
    assert off % 4 == 0 and -(1 << 25) <= off < (1 << 25), (hex(src), hex(dst))
    return (18 << 26) | (off & 0x03FFFFFC) | (1 if link else 0)


# ---------------------------------------------------------------- budowa kodu
FRAME = 0x1C0
OFF_R0, OFF_R3 = 0x70, 0x78         # r3..r12 w 0x78 + 8*(n-3)
OFF_CR, OFF_CTR, OFF_F1, OFF_BUF = 0xC8, 0xD0, 0xD8, 0x140
OFF_LR = FRAME + 0x10
ARG_OFF = {0: OFF_R3, 1: OFF_R3 + 8, 2: OFF_R3 + 16}          # r3, r4, r5


def build_common(base, args):
    """Zwraca liste slow procedury `common`. Wejscie: r11 = znacznik (lis 0xFAid), reszta rejestrow = jak w funkcji."""
    code = []
    emit = code.append
    emit(stdu(1, -FRAME, 1))
    emit(std(0, OFF_R0, 1))
    for n in range(3, 13):
        emit(std(n, OFF_R3 + 8 * (n - 3), 1))
    emit(mflr(0)); emit(std(0, OFF_LR, 1))
    emit(mfcr(0)); emit(std(0, OFF_CR, 1))
    emit(mfctr(0)); emit(std(0, OFF_CTR, 1))
    for f in range(1, 14):
        emit(stfd(f, OFF_F1 + 8 * (f - 1), 1))
    for k in args:
        emit(ld(10, OFF_R3 + 8 * (11 - 3), 1))        # r11 (zachowane) = baza znacznika
        emit(clrldi(10, 10, 32))
        emit(ori(9, 10, k))
        emit(sldi(9, 9, 32))
        emit(ld(8, ARG_OFF[k], 1))
        emit(clrldi(8, 8, 32))
        emit(or_(4, 9, 8))
        emit(addi(3, 1, OFF_BUF))
        here = base + 4 * len(code)
        emit(branch(here, RTC_STUB, True))
    for f in range(1, 14):
        emit(lfd(f, OFF_F1 + 8 * (f - 1), 1))
    emit(ld(0, OFF_CR, 1)); emit(mtcrf_all(0))
    emit(ld(0, OFF_CTR, 1)); emit(mtctr(0))
    emit(ld(0, OFF_LR, 1)); emit(mtlr(0))
    for n in range(3, 13):
        emit(ld(n, OFF_R3 + 8 * (n - 3), 1))
    emit(ld(0, OFF_R0, 1))
    emit(addi(1, 1, FRAME))
    emit(BLR)
    return code


def build_stub(base, marker_id, common, helper):
    code = [lis(11, 0xFA00 | marker_id), mflr(12), 0, mtlr(12), 0]
    code[2] = branch(base + 8, common, True)
    code[4] = branch(base + 16, helper, False)
    return code


def build_all():
    """-> (lista (adres, slowo) kodu w jaskini, lista (adres, slowo) podmian wejsc funkcji, mapa opisow)"""
    words, hooks, notes = [], [], {}
    addr = CAVE
    common3 = addr
    c3 = build_common(common3, (0, 1, 2))
    words += [(common3 + 4 * i, w) for i, w in enumerate(c3)]
    addr = common3 + 4 * len(c3)
    addr = (addr + 15) & ~15
    common1 = addr
    c1 = build_common(common1, (1,))
    words += [(common1 + 4 * i, w) for i, w in enumerate(c1)]
    addr = common1 + 4 * len(c1)
    addr = (addr + 15) & ~15
    for mid, name, target, helper, mode in TARGETS:
        stub = addr
        sw = build_stub(stub, mid, common3 if mode == 3 else common1, helper)
        words += [(stub + 4 * i, w) for i, w in enumerate(sw)]
        hooks.append((target + 4, branch(target + 4, stub, True), name, mid, stub))
        addr = (stub + 4 * len(sw) + 15) & ~15
    assert addr <= CAVE_END, hex(addr)
    return words, hooks, (common3, common1, addr)


def render_yaml():
    words, hooks, (common3, common1, end) = build_all()
    out = []
    w = out.append
    w("# GENEROWANE przez tools/make_trace_patch.py -- nie edytuj recznie.")
    w("# Lata-znacznik RPCS3: wpisuje do RPCS3.log linie 'cellRtc: cellRtcSetTime_t(... iTime=0xfa....)' za kazdym razem,")
    w("# gdy gra wchodzi w wybrana funkcje (lancuch zaproszenia, zdarzenia UI EVENT_*). Dekodowanie: tools/decode_trace.py.")
    w("#")
    w("# Jak uzyc (na OBU komputerach): otworz patch.yml RPCS3 (folder patches), znajdz klucz")
    w("#   %s:" % PPU_HASH)
    w("# (ten sam, pod ktorym masz juz inne patche) i wklej PONIZSZY wpis POD TYM KLUCZEM (z zachowaniem wciec).")
    w("# Potem: Patch Manager -> FIFA 17 -> BLES02233 v.01.00 -> zaznacz 'FIFA trace (zaproszenia i zdarzenia UI)'.")
    w("# Pierwsze uruchomienie po zmianie kompiluje PPU od nowa (kilka minut) -- to normalne.")
    w("#")
    w("# Znaczniki:")
    for mid, name, target, helper, mode in TARGETS:
        w("#   0x%02X  0x%08X  %s" % (mid, target, name))
    w("")
    w("  FIFA trace (zaproszenia i zdarzenia UI):")
    w("    Games:")
    w("      FIFA 17:")
    w('        BLES02233: [ "01.00" ]')
    w('    Author: "custom"')
    w('    Notes: "Znaczniki diagnostyczne: log cellRtcSetTime_t z iTime=0xFA... przy wejsciu w funkcje zaproszen / zdarzen UI"')
    w('    Patch Version: "1.0"')
    w("    Patch:")
    w("          # --- podmiana drugiego slowa (bl _savegpr) w funkcjach na bl do stuba ---")
    for target4, word, name, mid, stub in hooks:
        w("          - [ be32, 0x%08X, 0x%08X ]   # %s -> stub 0x%08X" % (target4, word, name, stub))
    w("          # --- kod w jaskini 0x%08X..0x%08X ---" % (CAVE, end))
    for a, wd in words:
        w("          - [ be32, 0x%08X, 0x%08X ]" % (a, wd))
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- weryfikacja
def _capstone_check(words):
    try:
        from capstone import Cs, CS_ARCH_PPC, CS_MODE_64, CS_MODE_BIG_ENDIAN
    except Exception:
        print("capstone niedostepny -- pomijam weryfikacje kodowania")
        return True
    md = Cs(CS_ARCH_PPC, CS_MODE_64 | CS_MODE_BIG_ENDIAN)
    ok = True
    for a, w in words:
        ins = list(md.disasm(struct.pack(">I", w), a))
        if not ins:
            print("NIE DEKODUJE SIE: 0x%08X: 0x%08X" % (a, w))
            ok = False
    return ok


class Emu:
    """Minimalny emulator PPC64 dla instrukcji uzywanych w patchu (do sprawdzenia semantyki)."""

    M64 = (1 << 64) - 1

    def __init__(self):
        self.mem = {}
        self.r = [0] * 32
        self.f = [0] * 32
        self.lr = self.ctr = self.cr = 0
        self.pc = 0
        self.hooks = {}

    def w(self, a, data):
        for i, b in enumerate(data):
            self.mem[a + i] = b

    def rd(self, a, n):
        return bytes(self.mem.get(a + i, 0) for i in range(n))

    def ld64(self, a): return struct.unpack(">Q", self.rd(a, 8))[0]
    def st64(self, a, v): self.w(a, struct.pack(">Q", v & self.M64))

    def run(self, stop, limit=100000):
        for _ in range(limit):
            if self.pc == stop:
                return
            if self.pc in self.hooks:
                self.hooks[self.pc](self)
                continue
            self.step()
        raise RuntimeError("limit krokow")

    @staticmethod
    def s16(x): return x - 0x10000 if x & 0x8000 else x

    def step(self):
        w = struct.unpack(">I", self.rd(self.pc, 4))[0]
        op = w >> 26
        rd = (w >> 21) & 31
        ra = (w >> 16) & 31
        rb = (w >> 11) & 31
        imm = w & 0xFFFF
        nxt = self.pc + 4
        R, M = self.r, self.M64
        if op == 62:
            d = self.s16(imm & 0xFFFC)
            sub = w & 3
            ea = (R[ra] + d) & M
            self.st64(ea, R[rd])
            if sub == 1:
                R[ra] = ea
        elif op == 58:
            ea = (R[ra] + self.s16(imm & 0xFFFC)) & M
            R[rd] = self.ld64(ea)
        elif op == 32:
            ea = ((R[ra] if ra else 0) + self.s16(imm)) & M
            R[rd] = struct.unpack(">I", self.rd(ea, 4))[0]
        elif op == 54:
            self.st64((R[ra] + self.s16(imm)) & M, self.f[rd])
        elif op == 50:
            self.f[rd] = self.ld64((R[ra] + self.s16(imm)) & M)
        elif op == 14:
            R[rd] = (((R[ra] if ra else 0) + self.s16(imm)) & M)
        elif op == 15:
            R[rd] = ((((R[ra] if ra else 0)) + (self.s16(imm) << 16)) & M)
        elif op == 24:
            R[ra] = R[rd] | imm
        elif op == 31:
            xo = (w >> 1) & 0x3FF
            if xo == 444:                                    # or
                R[ra] = R[rd] | R[rb]
            elif xo == 339:                                  # mfspr
                spr = ((w >> 16) & 31) | (((w >> 11) & 31) << 5)
                R[rd] = {8: self.lr, 9: self.ctr}[spr]
            elif xo == 467:                                  # mtspr
                spr = ((w >> 16) & 31) | (((w >> 11) & 31) << 5)
                if spr == 8: self.lr = R[rd]
                elif spr == 9: self.ctr = R[rd]
                else: raise RuntimeError("mtspr %d" % spr)
            elif xo == 19:                                   # mfcr
                R[rd] = self.cr
            elif xo == 144:                                  # mtcrf
                assert ((w >> 12) & 0xFF) == 0xFF
                self.cr = R[rd] & 0xFFFFFFFF
            else:
                raise RuntimeError("op31 xo=%d @%x" % (xo, self.pc))
        elif op == 30:
            sh = ((w >> 11) & 31) | (((w >> 1) & 1) << 5)
            mb = ((w >> 6) & 31) | (((w >> 5) & 1) << 5)
            xo = (w >> 2) & 7
            v = R[rd]
            rot = ((v << sh) | (v >> (64 - sh))) & M if sh else v
            if xo == 0:                                      # rldicl
                R[ra] = rot & (M >> mb)
            elif xo == 1:                                    # rldicr
                R[ra] = rot & (M << (63 - mb)) & M
            else:
                raise RuntimeError("op30 xo=%d" % xo)
        elif op == 18:
            off = w & 0x03FFFFFC
            if off & 0x2000000: off -= 0x4000000
            if w & 1: self.lr = nxt
            self.pc = (self.pc + off) & M if not (w & 2) else off
            return
        elif op == 19 and ((w >> 1) & 0x3FF) == 16:          # blr
            self.pc = self.lr
            return
        else:
            raise RuntimeError("nieobslugiwany opcode %d @0x%x (0x%08x)" % (op, self.pc, w))
        self.pc = nxt


def selftest():
    words, hooks, (common3, common1, end) = build_all()
    ok = _capstone_check(words + [(a, w) for a, w, *_ in hooks])
    rng = random.Random(1234)
    for mid, name, target, helper, mode in TARGETS:
        e = Emu()
        for a, wd in words:
            e.w(a, struct.pack(">I", wd))
        hook = [h for h in hooks if h[0] == target + 4][0]
        e.w(target + 4, struct.pack(">I", hook[1]))
        logged = []
        stack_top = 0xD0010000

        def rtc(emu):
            buf, itime = emu.r[3], emu.r[4]
            assert emu.rd(buf, 16) is not None
            logged.append(itime)
            # jak prawdziwy import: zapisuje w ramce wolajacego (0x20, 0x28) i niszczy rejestry zmienne
            emu.st64(emu.r[1] + 0x20, emu.lr)
            emu.st64(emu.r[1] + 0x28, 0xBADBADBAD)
            for n in list(range(3, 13)) + [0]:
                emu.r[n] = rng.getrandbits(64)
            for n in range(0, 14):
                emu.f[n] = rng.getrandbits(64)
            emu.ctr = rng.getrandbits(64)
            emu.cr = rng.getrandbits(32)
            emu.pc = emu.lr

        e.hooks[RTC_STUB] = rtc
        e.hooks[helper] = lambda emu: (_ for _ in ()).throw(StopIteration)
        for n in range(3, 11):
            e.r[n] = rng.getrandbits(64)
        e.r[11] = rng.getrandbits(64)
        e.r[12] = rng.getrandbits(64)
        e.r[1] = stack_top - 0x200
        caller_lr = 0x00123450
        e.lr = caller_lr
        e.r[0] = caller_lr                    # wykonane przez 'mflr r0' w funkcji
        for n in range(1, 14):
            e.f[n] = rng.getrandbits(64)
        e.ctr = rng.getrandbits(64)
        e.cr = rng.getrandbits(32)
        before = (list(e.r), list(e.f), e.ctr, e.cr)
        e.lr = target + 8                     # 'bl S' z T+4
        e.pc = [h for h in hooks if h[0] == target + 4][0][4]
        # wejscie w stub S_k odbywa sie przez 'bl' z T+4 -- symulujemy skok bezposrednio
        try:
            e.run(helper)
        except StopIteration:
            pass
        assert e.pc == helper or True
        # stan przy wejsciu do pomocnika musi zgadzac sie z oryginalem (poza r11/r12)
        problems = []
        if e.r[0] != caller_lr: problems.append("r0")
        if e.r[1] != before[0][1]: problems.append("r1")
        for n in range(3, 11):
            if e.r[n] != before[0][n]: problems.append("r%d" % n)
        for n in range(1, 14):
            if e.f[n] != before[1][n]: problems.append("f%d" % n)
        if e.ctr != before[2]: problems.append("ctr")
        if e.cr != before[3]: problems.append("cr")
        if e.lr != target + 8: problems.append("lr=%x" % e.lr)
        args = (before[0][3], before[0][4], before[0][5])
        ks = (1,) if mode == 1 else (0, 1, 2)
        want = [((0xFA000000 | (mid << 16) | k) << 32) | (args[k] & 0xFFFFFFFF) for k in ks]
        if logged != want:
            problems.append("log %s != %s" % ([hex(x) for x in logged], [hex(x) for x in want]))
        print("%-4s 0x%02X 0x%08X %-52s %s" % ("OK" if not problems else "BLAD", mid, target, name,
                                              ",".join(problems)))
        ok = ok and not problems
    print("jaskinia: 0x%08X..0x%08X (%d bajtow, limit %d)" % (CAVE, end, end - CAVE, CAVE_END - CAVE))
    return ok


def verify_elf(path):
    """Sprawdza na prawdziwym EBOOT.ELF, ze kazdy cel zaczyna sie od `mflr r0` i ma w T+4 `bl H` z H z tabeli."""
    d = open(path, "rb").read()
    phoff = struct.unpack(">Q", d[32:40])[0]
    n = struct.unpack(">H", d[56:58])[0]
    segs = []
    for i in range(n):
        o = phoff + i * 56
        t, fl, off, va, pa, fs, ms = struct.unpack(">IIQQQQQ", d[o:o + 48])
        if t == 1 and fs:
            segs.append((va, off, fs))

    def u32(va):
        for v, o, fs in segs:
            if v <= va < v + fs:
                return struct.unpack(">I", d[o + va - v:o + va - v + 4])[0]
        raise ValueError(hex(va))
    bad = 0
    for mid, name, t, h, mode in TARGETS:
        w0, w1 = u32(t), u32(t + 4)
        off = w1 & 0x03FFFFFC
        if off & 0x2000000:
            off -= 0x4000000
        ok = w0 == 0x7C0802A6 and (w1 >> 26) == 18 and (w1 & 3) == 1 and ((t + 4 + off) & 0xFFFFFFFF) == h
        if not ok:
            bad += 1
            print("NIEZGODNE 0x%02X %s 0x%08X: %08X %08X" % (mid, name, t, w0, w1))
    print("sprawdzono %d celow na %s, niezgodnych: %d" % (len(TARGETS), path, bad))
    return bad == 0


def main(argv):
    if "--verify-elf" in argv:
        return 0 if verify_elf(argv[argv.index("--verify-elf") + 1]) else 1
    if "--selftest" in argv:
        return 0 if selftest() else 1
    text = render_yaml()
    if "--check" in argv:
        cur = open(OUT, encoding="utf-8").read() if os.path.exists(OUT) else ""
        if cur != text:
            print("rpcs3_patch_trace_invites.yml nieaktualny -- uruchom tools/make_trace_patch.py")
            return 1
        return 0
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print("zapisano", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
