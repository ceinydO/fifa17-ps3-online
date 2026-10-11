#!/usr/bin/env python3
"""Generator lat RPCS3 ("znacznikow") do sledzenia, ktore funkcje FIFA 17 sa faktycznie wywolywane.

Po co: logi serwera widza tylko ruch Blaze. To, czy gra po createGame w ogole probuje wyslac zaproszenie (NP),
oraz jakie zdarzenia UI (EVENT_*) odpalaja sie u hosta i u kolegi, jest widoczne tylko po stronie gry.
Lata dopisuje do RPCS3.log linie typu

    cellRtc: cellRtcSetTime_t(pDateTime=*0xd012cba8, iTime=0xfa1200010000a1b2)

(RPCS3 loguje argumenty kazdego wywolania HLE). Gra sama wola cellRtcSetTime_t z normalnym czasem
(iTime=0x5884....), wiec nasze znaczniki (zaczynaja sie od 0xfa) sa latwe do odroznienia.

Kodowanie iTime:  0xFA | id(8 bitow) | k(16 bitow) | wartosc(32 bity)
  id = numer znacznika (tabela TARGETS), k = ktory argument (0 = r3, 1 = r4, 2 = r5, 3 = LR wywolujacego, 4 = r6),
  wartosc = jego dolne 32 bity. LR to adres powrotu = "kto nas wolal" (tools/decode_trace.py zamienia go na funkcje).
Dla znacznika zdarzen (id 0x01) logowany jest r4 (k=1) = wskaznik na nazwe zdarzenia (decode_trace zamienia go na
tekst "EVENT_...") oraz LR (k=3) = kto zglosil zdarzenie.

Runda 3 (2026-10-12): oprocz wejsc w funkcje (T+4 = `bl _savegpr`) sa tez znaczniki "w srodku funkcji" (tabela MIDS):
podmieniamy jedna instrukcje A na `bl stub`; stub zachowuje WSZYSTKIE rejestry, loguje r3/r4/r5, wykonuje podmieniona
instrukcje i wraca na A+4 (dozwolone tylko tam, gdzie LR jest martwy, tj. tuz przed `bdnzctrl`/`bl`).

Mechanizm (bezpieczny dla rekompilatora PPU RPCS3): wszystkie obserwowane funkcje maja na poczatku
    mflr r0 ; bl _savegpr_N
Podmieniamy TYLKO drugie slowo (bl _savegpr_N -> bl S_k). S_k zapisuje LR, wola wspolna procedure `common`
(zachowuje WSZYSTKIE rejestry r0-r12, f1-f13, LR, CR, CTR), potem przywraca LR i robi `b _savegpr_N`, wiec dla
oryginalnej funkcji nic sie nie zmienia. Kod i stuby leza w nieuzywanym, wyzerowanym obszarze segmentu kodu.

Drugi, OPCJONALNY wpis w tym samym pliku ("eksperyment") wymusza po udanym createGame wywolanie skryptu UI
`OnOnlineCreateSession("EVENT_CREATEGAME_SUCCESS")` -- dokladnie to, co zrobilby FSM, gdyby listener wyslal
EVENT_CREATEGAME_SUCCESS (patrz EXPERIMENT_* ponizej). Jest niezalezny od znacznikow (osobny wpis w Patch Managerze).

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

# (id, nazwa, adres funkcji T, adres pomocnika H z "bl H" w T+4, tryb = klucz MODES: 3 = r3,r4,r5,LR; 5 = r3,r4,r5,r6,LR;
#  1 = r4; 6 = r4,LR; 7 = r5 (nazwa), pierwsze dwa argumenty wariadyczne, LR)
TARGETS = [
    (0x01, "EVT  post_event(this, nazwa_zdarzenia, dane)", 0x015FFB68, 0x01955538, 6),
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
    # --- runda 3: kto wola createGame i jaki listener dostaje wynik; wywolania silnik -> skrypty UI ---
    (0x71, "UI skrypt: wywolanie funkcji 'Handle_<nazwa>' (napisy) (38aca4)", 0x0038ACA4, 0x01955538, 7),
    (0x72, "UI skrypt: wywolanie funkcji 'Handle_<nazwa>' (obiekt) (38b110)", 0x0038B110, 0x01955538, 7),
    (0x73, "gses: wynik operacji 2df808", 0x002DF808, 0x01955538, 3),
    (0x74, "gses: wynik operacji 2df978", 0x002DF978, 0x01955540, 3),
    (0x75, "gses: wynik operacji 2dfb20", 0x002DFB20, 0x01955540, 3),
    (0x76, "EVENT_GAME_INVITE_RECEIVED: nadawca zdarzenia (1649774)", 0x01649774, 0x01955544, 3),
    (0x77, "listener zaproszen: USER_INVITE sukces (16495b4)", 0x016495B4, 0x01955540, 3),
    (0x78, "listener zaproszen: USER_INVITE porazka (164964c)", 0x0164964C, 0x01955540, 3),
    (0x79, "EVENT_MATCHUP_SUCCESS: nadawca zdarzenia (1625504)", 0x01625504, 0x0195553C, 3),
    (0x7A, "inny listener createGame (1624b68)", 0x01624B68, 0x0195553C, 3),
    (0x7B, "inny listener createGame (1624c14)", 0x01624C14, 0x0195553C, 3),
    (0x7C, "inny listener createGame (1624cf0)", 0x01624CF0, 0x01955544, 3),
    (0x7D, "GameSessionManager vt+0x10 (28fdfc)", 0x0028FDFC, 0x01955520, 3),
    (0x7E, "GameSessionManager JoinGame? (290b18)", 0x00290B18, 0x0195552C, 3),
    (0x7F, "GameSessionManager JoinGame? (290dd8)", 0x00290DD8, 0x0195552C, 3),
]

# Znaczniki w srodku funkcji: (id, nazwa, adres instrukcji A, jej slowo, tryb).  Podmieniana instrukcja musi byc
# wykonywalna poza kontekstem (bez rozgalezien wzgledem PC) i LR musi byc w A martwy.
MIDS = [
    (0x70, "createGame (GSMR) tuz przed `mtctr r5`: r3=listener, r4=sesja, r5=KOD listenera, ktory zaraz zostanie wywolany",
     0x002DEF74, 0x7CA903A6, 3),                                  # mtctr r5 (a potem bdnzctrl @ 2def78)
]


# ---------------------------------------------------------------- mini asembler (tylko uzywane instrukcje)
# ---------------------------------------------------------------- EKSPERYMENT: wymus OnOnlineCreateSession
EXP_NAME = "FIFA eksperyment - wymus OnOnlineCreateSession po createGame"
EXP_AT = 0x002DEF7C                  # `li r3,4` tuz po wywolaniu listenera w callbacku createGame (2dee08)
EXP_DISPLACED = 0x38600004
EXP_FN_SCRIPT_MGR, EXP_FN_FMT, EXP_FN_CALL_SCRIPT = 0x0038A5AC, 0x00287C5C, 0x0038ADE4
EXP_STR_FN, EXP_STR_EVT = 0x01F00A6C, 0x01EFFC8C        # "OnOnlineCreateSession", "EVENT_CREATEGAME_SUCCESS"


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
BCTRL = 0x4E800421


def li(rd, imm): return addi(rd, 0, imm)


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
ARG_OFF = {0: OFF_R3, 1: OFF_R3 + 8, 2: OFF_R3 + 16,          # r3, r4, r5
           3: OFF_R0,                                          # LR wywolujacego (po `mflr r0` w prologu)
           4: OFF_R3 + 24}                                     # r6
# k = 5, 6: slowo spod adresu r6+4 / r6+12 (r6 = wskaznik na tablice argumentow wariadycznych, 8-bajtowe sloty
# z 32-bitowym wskaznikiem w dolnej polowie) -- uzywane tylko przez funkcje 'Handle_<nazwa>' (38aca4, 38b110)
DEREF = {5: 4, 6: 12}
# tryb -> ktore argumenty logowac (w tej kolejnosci)
MODES = {1: (1,), 3: (0, 1, 2, 3), 5: (0, 1, 2, 4, 3), 6: (1, 3), 7: (2, 5, 6, 3)}


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
        if k in DEREF:
            emit(ld(8, ARG_OFF[4], 1))                    # r6
            emit(lwz(8, DEREF[k], 8))
        else:
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


MID_FRAME = 0x30


def build_mid_stub(base, marker_id, common, displaced):
    """Stub dla znacznika w srodku funkcji. Wejscie przez `bl` z A (LR = A+4). r11/r12 sa nietykalne dla oryginalu,
    wiec zapisujemy je na krotkiej ramce, a po `common` przywracamy. Podmieniona instrukcja jest wykonywana na koncu."""
    code = [stdu(1, -MID_FRAME, 1), std(11, 0x20, 1), std(12, 0x28, 1),
            lis(11, 0xFA00 | marker_id), mflr(12), 0, mtlr(12),
            ld(11, 0x20, 1), ld(12, 0x28, 1), addi(1, 1, MID_FRAME), displaced, BLR]
    code[5] = branch(base + 20, common, True)
    return code


EXP_FRAME = 0xC0


def build_exp_stub(base):
    """Odtwarza to, co FSM (4d0aa4) robi po EVENT_CREATEGAME_SUCCESS: OnOnlineCreateSession("EVENT_CREATEGAME_SUCCESS",
    <id sesji jako tekst>). Wejscie przez `bl` z EXP_AT (po powrocie z listenera rejestry ulotne sa martwe, r28 = sesja)."""
    c = []
    e = c.append
    e(stdu(1, -EXP_FRAME, 1))
    e(mflr(0)); e(std(0, EXP_FRAME + 0x10, 1))
    e(addi(3, 28, 8))                       # r3 = sesja + 8
    e(lwz(4, 8, 28)); e(lwz(4, 0, 4)); e(lwz(4, 0, 4))    # kod slotu 0 vtable podobiektu
    e(mtctr(4)); e(BCTRL)                   # r3 = id gry
    e(addi(4, 1, 0x70)); e(li(5, 10))
    e(0); idx_fmt = len(c) - 1
    e(std(3, 0xB0, 1))                      # wskaznik na tekst
    e(0); idx_mgr = len(c) - 1
    e(li(4, 0))
    e(lis(5, EXP_STR_FN >> 16)); e(ori(5, 5, EXP_STR_FN & 0xFFFF))
    e(lis(6, EXP_STR_EVT >> 16)); e(ori(6, 6, EXP_STR_EVT & 0xFFFF))
    e(ld(7, 0xB0, 1)); e(li(8, 0))
    e(0); idx_call = len(c) - 1
    e(ld(0, EXP_FRAME + 0x10, 1)); e(mtlr(0))
    e(addi(1, 1, EXP_FRAME))
    e(EXP_DISPLACED); e(BLR)
    c[idx_fmt] = branch(base + 4 * idx_fmt, EXP_FN_FMT, True)
    c[idx_mgr] = branch(base + 4 * idx_mgr, EXP_FN_SCRIPT_MGR, True)
    c[idx_call] = branch(base + 4 * idx_call, EXP_FN_CALL_SCRIPT, True)
    return c


def build_exp():
    """-> (slowa (adres, slowo), (adres_podmiany, slowo_bl, stub))"""
    words, hooks, (commons, end) = build_all()
    base = (end + 15) & ~15
    sw = build_exp_stub(base)
    assert base + 4 * len(sw) <= CAVE_END, hex(base + 4 * len(sw))
    return [(base + 4 * i, w) for i, w in enumerate(sw)], (EXP_AT, branch(EXP_AT, base, True), base)


def build_all():
    """-> (lista (adres, slowo) kodu w jaskini, lista (adres, slowo, nazwa, id, stub) podmian, (commons, koniec))"""
    words, hooks = [], []
    addr = CAVE
    commons = {}
    for mode in sorted({m for *_, m in TARGETS} | {m for *_, m in MIDS}):
        c = build_common(addr, MODES[mode])
        words += [(addr + 4 * i, w) for i, w in enumerate(c)]
        commons[mode] = addr
        addr = (addr + 4 * len(c) + 15) & ~15
    for mid, name, target, helper, mode in TARGETS:
        stub = addr
        sw = build_stub(stub, mid, commons[mode], helper)
        words += [(stub + 4 * i, w) for i, w in enumerate(sw)]
        hooks.append((target + 4, branch(target + 4, stub, True), name, mid, stub))
        addr = stub + 4 * len(sw)
    for mid, name, at, displaced, mode in MIDS:
        stub = addr
        sw = build_mid_stub(stub, mid, commons[mode], displaced)
        words += [(stub + 4 * i, w) for i, w in enumerate(sw)]
        hooks.append((at, branch(at, stub, True), name, mid, stub))
        addr = stub + 4 * len(sw)
    addr = (addr + 15) & ~15
    assert addr <= CAVE_END, hex(addr)
    return words, hooks, (commons, addr)


def render_yaml():
    words, hooks, (commons, end) = build_all()
    out = []
    w = out.append
    w("# GENEROWANE przez tools/make_trace_patch.py -- nie edytuj recznie.")
    w("# Lata-znacznik RPCS3: wpisuje do RPCS3.log linie 'cellRtc: cellRtcSetTime_t(... iTime=0xfa....)' za kazdym razem,")
    w("# gdy gra wchodzi w wybrana funkcje (lancuch zaproszenia, zdarzenia UI EVENT_*). Dekodowanie: tools/decode_trace.py.")
    w("#")
    w("# Jak uzyc (na OBU komputerach): otworz patch.yml RPCS3 (folder patches), znajdz klucz")
    w("#   %s:" % PPU_HASH)
    w("# (ten sam, pod ktorym masz juz inne patche) i wklej PONIZSZY wpis POD TYM KLUCZEM (z zachowaniem wciec).")
    w("# Potem: Patch Manager -> FIFA 17 -> BLES02233 v.01.00 -> zaznacz 'FIFA trace (zaproszenia i zdarzenia UI)'")
    w("# oraz (tylko jesli chcesz sprobowac) 'FIFA eksperyment - wymus OnOnlineCreateSession po createGame'.")
    w("# Pierwsze uruchomienie po zmianie kompiluje PPU od nowa (kilka minut) -- to normalne.")
    w("#")
    w("# Znaczniki:")
    for mid, name, target, helper, mode in TARGETS:
        w("#   0x%02X  0x%08X  %s" % (mid, target, name))
    for mid, name, at, displaced, mode in MIDS:
        w("#   0x%02X  0x%08X  [w srodku funkcji] %s" % (mid, at, name))
    w("")
    w("  FIFA trace (zaproszenia i zdarzenia UI):")
    w("    Games:")
    w("      FIFA 17:")
    w('        BLES02233: [ "01.00" ]')
    w('    Author: "custom"')
    w('    Notes: "Znaczniki diagnostyczne: log cellRtcSetTime_t z iTime=0xFA... przy wejsciu w funkcje zaproszen / zdarzen UI"')
    w('    Patch Version: "1.0"')
    w("    Patch:")
    w("          # --- podmiana drugiego slowa (bl _savegpr) w funkcjach / jednej instrukcji w srodku funkcji na bl do stuba ---")
    for target4, word, name, mid, stub in hooks:
        w("          - [ be32, 0x%08X, 0x%08X ]   # %s -> stub 0x%08X" % (target4, word, name, stub))
    w("          # --- kod w jaskini 0x%08X..0x%08X ---" % (CAVE, end))
    for a, wd in words:
        w("          - [ be32, 0x%08X, 0x%08X ]" % (a, wd))
    exp_words, (exp_at, exp_bl, exp_stub) = build_exp()
    w("")
    w("  %s:" % EXP_NAME)
    w("    Games:")
    w("      FIFA 17:")
    w('        BLES02233: [ "01.00" ]')
    w('    Author: "custom"')
    w('    Notes: "Po udanym createGame (callback 2dee08) wywoluje skrypt UI OnOnlineCreateSession(EVENT_CREATEGAME_SUCCESS) -- to, czego brakuje, bo listener nie wysyla zdarzenia"')
    w('    Patch Version: "1.0"')
    w("    Patch:")
    w("          - [ be32, 0x%08X, 0x%08X ]   # 2def7c: li r3,4 -> bl stub 0x%08X" % (exp_at, exp_bl, exp_stub))
    for a, wd in exp_words:
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
        elif op == 19 and ((w >> 1) & 0x3FF) == 528:         # bctr / bctrl
            if w & 1: self.lr = nxt
            self.pc = self.ctr & ~3
            return
        else:
            raise RuntimeError("nieobslugiwany opcode %d @0x%x (0x%08x)" % (op, self.pc, w))
        self.pc = nxt


def _fresh_emu(words, rng):
    e = Emu()
    for a, wd in words:
        e.w(a, struct.pack(">I", wd))
    for n in range(3, 13):
        e.r[n] = rng.getrandbits(64)
    for n in range(1, 14):
        e.f[n] = rng.getrandbits(64)
    e.ctr = rng.getrandbits(64)
    e.cr = rng.getrandbits(32)
    e.r[1] = 0xD0010000 - 0x200
    return e


def _make_rtc(logged, rng):
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
    return rtc


def _want_log(mid, mode, regs, lr_value, mem=None):
    """regs = stan rejestrow przed wejsciem; mem = emulator (do odczytu slow spod r6+4/r6+12)."""
    src = {0: regs[3], 1: regs[4], 2: regs[5], 3: lr_value, 4: regs[6]}
    if mem is not None:
        for k, off in DEREF.items():
            src[k] = struct.unpack(">I", mem.rd((regs[6] + off) & 0xFFFFFFFFFFFFFFFF, 4))[0]
    return [((0xFA000000 | (mid << 16) | k) << 32) | (src[k] & 0xFFFFFFFF) for k in MODES[mode]]


def _yaml_check():
    """Wpisy musza byc poprawnym YAML-em (RPCS3 odrzuci caly patch.yml przy bledzie, np. ': ' w nazwie wpisu)."""
    try:
        import yaml
    except Exception:
        print("PyYAML niedostepny -- pomijam sprawdzenie skladni YAML")
        return True
    doc = yaml.safe_load("%s:\n%s" % (PPU_HASH, render_yaml()))
    entries = doc[PPU_HASH]
    problems = []
    for name in ("FIFA trace (zaproszenia i zdarzenia UI)", EXP_NAME):
        e = entries.get(name)
        if not e or not e.get("Patch") or e["Games"]["FIFA 17"]["BLES02233"] != ["01.00"]:
            problems.append(name)
    for e in entries.values():
        for row in e["Patch"]:
            if row[0] != "be32" or not isinstance(row[1], int) or not isinstance(row[2], int):
                problems.append("wiersz %r" % (row,))
                break
    print("%-4s skladnia YAML i struktura wpisow %s" % ("OK" if not problems else "BLAD", ",".join(problems)))
    return not problems


def _exp_selftest():
    """Eksperyment: kolejnosc i argumenty wywolan oraz zachowanie rejestrow nieulotnych (r14-r31, f14-f31, cr2-4)."""
    rng = random.Random(77)
    exp_words, (at, bl, stub) = build_exp()
    e = _fresh_emu(exp_words, rng)
    e.w(at, struct.pack(">I", bl))
    sess, vptr, opd, code = 0x10D9E310, 0x20000000, 0x20000100, 0x20000200
    e.w(sess + 8, struct.pack(">I", vptr))
    e.w(vptr, struct.pack(">I", opd))
    e.w(opd, struct.pack(">I", code))
    calls = []

    def hook(name, ret):
        def f(emu):
            calls.append((name, [emu.r[n] & 0xFFFFFFFF for n in range(3, 9)]))
            emu.r[3] = ret
            for n in list(range(4, 13)) + [0]:           # funkcje wolane niszcza rejestry ulotne
                emu.r[n] = rng.getrandbits(64)
            emu.pc = emu.lr
        return f
    e.hooks[code] = hook("id_gry", 0x1234)
    e.hooks[EXP_FN_FMT] = hook("fmt", 0xD0AAAA00)
    e.hooks[EXP_FN_SCRIPT_MGR] = hook("mgr", 0x0BADF00D)
    e.hooks[EXP_FN_CALL_SCRIPT] = hook("wywolaj_skrypt", 0)
    e.r[28] = sess
    for n in list(range(14, 28)) + [29, 30, 31]:
        e.r[n] = rng.getrandbits(64)
    before = (list(e.r), list(e.f), e.cr)
    e.lr = at + 4
    e.pc = stub
    e.hooks[at + 4] = lambda emu: (_ for _ in ()).throw(StopIteration)
    try:
        e.run(at + 4)
    except StopIteration:
        pass
    problems = []
    names = [c[0] for c in calls]
    if names != ["id_gry", "fmt", "mgr", "wywolaj_skrypt"]:
        problems.append("kolejnosc %s" % names)
    else:
        if calls[0][1][0] != sess + 8: problems.append("r3(id)")
        if calls[1][1][:3] != [0x1234, (before[0][1] - EXP_FRAME + 0x70) & 0xFFFFFFFF, 10]: problems.append("fmt(args)")
        a = calls[3][1]
        if a != [0x0BADF00D, 0, EXP_STR_FN, EXP_STR_EVT, 0xD0AAAA00, 0]: problems.append("skrypt(args)=%s" % a)
    if e.pc != at + 4: problems.append("pc=%x" % e.pc)
    if e.r[3] != 4: problems.append("r3 (podmieniona instrukcja)")
    if e.r[1] != before[0][1]: problems.append("r1")
    for n in list(range(14, 32)) + [2]:
        if e.r[n] != before[0][n]: problems.append("r%d" % n)
    if e.cr != before[2]: problems.append("cr")
    print("%-4s eksperyment: OnOnlineCreateSession (stub 0x%08X, %d slow) %s" % (
        "OK" if not problems else "BLAD", stub, len(exp_words), ",".join(problems)))
    return not problems


def selftest():
    words, hooks, (commons, end) = build_all()
    exp_words, (exp_at, exp_bl, _stub) = build_exp()
    ok = _capstone_check(words + [(a, w) for a, w, *_ in hooks] + exp_words + [(exp_at, exp_bl)])
    rng = random.Random(1234)
    for mid, name, target, helper, mode in TARGETS:
        logged = []
        e = _fresh_emu(words, rng)
        hook = [h for h in hooks if h[0] == target + 4][0]
        e.w(target + 4, struct.pack(">I", hook[1]))
        e.hooks[RTC_STUB] = _make_rtc(logged, rng)
        e.hooks[helper] = lambda emu: (_ for _ in ()).throw(StopIteration)
        for off in DEREF.values():
            e.w((e.r[6] + off) & 0xFFFFFFFFFFFFFFFF, struct.pack(">I", rng.getrandbits(32)))
        caller_lr = 0x00123450
        e.lr = caller_lr
        e.r[0] = caller_lr                    # wykonane przez 'mflr r0' w funkcji
        e.r[11] = rng.getrandbits(64)
        e.r[12] = rng.getrandbits(64)
        before = (list(e.r), list(e.f), e.ctr, e.cr)
        e.lr = target + 8                     # 'bl S' z T+4
        e.pc = hook[4]
        # wejscie w stub S_k odbywa sie przez 'bl' z T+4 -- symulujemy skok bezposrednio
        try:
            e.run(helper)
        except StopIteration:
            pass
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
        want = _want_log(mid, mode, before[0], caller_lr, e)
        if logged != want:
            problems.append("log %s != %s" % ([hex(x) for x in logged], [hex(x) for x in want]))
        print("%-4s 0x%02X 0x%08X %-52s %s" % ("OK" if not problems else "BLAD", mid, target, name[:52],
                                              ",".join(problems)))
        ok = ok and not problems
    for mid, name, at, displaced, mode in MIDS:
        logged = []
        e = _fresh_emu(words, rng)
        hook = [h for h in hooks if h[0] == at][0]
        e.w(at, struct.pack(">I", hook[1]))
        e.hooks[RTC_STUB] = _make_rtc(logged, rng)
        for n in (0, 11, 12):
            e.r[n] = rng.getrandbits(64)
        e.lr = rng.getrandbits(32) & ~3          # LR sprzed `bl` (martwy -- po `bl` bedzie A+4)
        e.hooks[at + 4] = lambda emu: (_ for _ in ()).throw(StopIteration)
        before = (list(e.r), list(e.f), e.ctr, e.cr)
        # wzorzec: wykonaj ta sama podmieniona instrukcje na kopii stanu
        ref = _fresh_emu([], rng)
        ref.r, ref.f, ref.ctr, ref.cr = list(before[0]), list(before[1]), before[2], before[3]
        ref.w(at, struct.pack(">I", displaced))
        ref.pc = at
        ref.step()
        e.lr = at + 4                            # skutek `bl stub` z A
        e.pc = hook[4]
        try:
            e.run(at + 4)
        except StopIteration:
            pass
        problems = []
        if e.pc != at + 4: problems.append("pc=%x" % e.pc)
        for n in range(0, 32):
            if e.r[n] != ref.r[n]: problems.append("r%d" % n)
        for n in range(1, 14):
            if e.f[n] != before[1][n]: problems.append("f%d" % n)
        if e.ctr != ref.ctr: problems.append("ctr")
        if e.cr != before[3]: problems.append("cr")
        want = _want_log(mid, mode, before[0], before[0][0], e)
        if logged != want:
            problems.append("log %s != %s" % ([hex(x) for x in logged], [hex(x) for x in want]))
        print("%-4s 0x%02X 0x%08X %-52s %s" % ("OK" if not problems else "BLAD", mid, at, "[srodek] " + name[:43],
                                              ",".join(problems)))
        ok = ok and not problems
    ok = _exp_selftest() and ok
    ok = _yaml_check() and ok
    exp_end = exp_words[-1][0] + 4
    print("jaskinia: 0x%08X..0x%08X (%d bajtow, limit %d)" % (CAVE, exp_end, exp_end - CAVE, CAVE_END - CAVE))
    return ok


def verify_elf(path):
    """Sprawdza na prawdziwym EBOOT.ELF: kazdy cel zaczyna sie od `mflr r0` i ma w T+4 `bl H` z H z tabeli;
    w kazdym znaczniku w srodku funkcji pod adresem A stoi dokladnie podmieniana instrukcja."""
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
    for mid, name, at, displaced, mode in MIDS:
        if u32(at) != displaced:
            bad += 1
            print("NIEZGODNE 0x%02X %s 0x%08X: jest %08X, oczekiwano %08X" % (mid, name, at, u32(at), displaced))
    if u32(EXP_AT) != EXP_DISPLACED:
        bad += 1
        print("NIEZGODNE eksperyment 0x%08X: jest %08X, oczekiwano %08X" % (EXP_AT, u32(EXP_AT), EXP_DISPLACED))
    for a, w in build_all()[0] + build_exp()[0]:      # jaskinia musi byc pusta (zera) w oryginale
        if u32(a) != 0:
            bad += 1
            print("JASKINIA NIE JEST PUSTA @0x%08X: %08X" % (a, u32(a)))
            break
    print("sprawdzono %d celow + %d znacznikow w srodku na %s, niezgodnych: %d" % (len(TARGETS), len(MIDS), path, bad))
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
