"""GameManager (komponent 0x0004) -- stan gier i budowa powiadomien.

Ksztalty wiadomosci pochodza z tablic refleksji TDF w EBOOT.ELF FIFA 17 (nazwy pol, tagi, typy), a NIE z
FIFA 14 (Impulsum14), z ktorym FIFA 17 rozni sie w kilku waznych miejscach:

* Unia GameSetupReason (REAS) ma w FIFA 17 ROZNE tagi skladowych (DLSC, IJGS, IMSC, MMSC, RDSC), a numer
  skladowej to jej pozycja w tablicy posortowanej po tagu: 0=DLSC (DatalessSetupContext {DCTX}),
  1=IJGS (IndirectJoinGameSetupContext {GRID, RPVC}), 2=IMSC, 3=MMSC, 4=RDSC. W FIFA 14 wszystkie
  skladowe mialy tag VALU, a IndirectJoin byl numerem 2 -- stary kod wysylal wiec zaproszonemu
  IndirectMatchmaking pod zlym tagiem.
* ReplicatedGamePlayer ma CONG (id grupy polaczen) i CSID; HostInfo to {CONG, CSID, HPID, HSES, HSLT}.
  Klient trzyma "punkty koncowe" sieci pod kluczem CONG: przy CONG=0 u wszystkich dwaj gracze dzielili
  JEDEN punkt koncowy, wiec klient nigdy nie probowal laczyc sie z rowiesnikiem (brak ruchu UDP).
* Pola typu TimeValue (TIME w NotifyPlayerJoinCompleted/ReplicatedGamePlayer) maja typ TDF 11, nie varint.
* ReplicatedGameData nie ma pola HSES, za to ma GPVH (hash wersji protokolu), SEED, UUID, MNCP, PSAS.

Modul nie dotyka gniazd: funkcje zwracaja liste (gracz_docelowy, opis, ramka) do rozeslania przez blaze.py.
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
import zlib

from . import ids, tdf

GM_COMPONENT = 0x0004

# komendy (FIFA 17 -- numeracja zgodna z FIFA 14, potwierdzona na drucie dla 1, 0xB, 0xF, 0x1D)
CMD_CREATE_GAME = 0x0001
CMD_DESTROY_GAME = 0x0002
CMD_ADVANCE_GAME_STATE = 0x0003
CMD_SET_GAME_SETTINGS = 0x0004
CMD_SET_GAME_ATTRIBUTES = 0x0007
CMD_SET_PLAYER_ATTRIBUTES = 0x0008
CMD_JOIN_GAME = 0x0009
CMD_REMOVE_PLAYER = 0x000B
CMD_FINALIZE_GAME_CREATION = 0x000F      # UpdateGameSessionRequest {GID, NPSI, XNNC, XSES}
CMD_UPDATE_MESH_CONNECTION = 0x001D      # {FLGS, GID, QOSI, SCG, STAT, TCG}

# powiadomienia
N_GAME_REMOVED = 0x0010
N_GAME_SETUP = 0x0014
N_PLAYER_JOINING = 0x0015
N_PLAYER_JOIN_COMPLETED = 0x001E
N_PLAYER_REMOVED = 0x0028
N_PLATFORM_HOST_INITIALIZED = 0x0047
N_GAME_ATTRIB_CHANGE = 0x0050
N_PLAYER_ATTRIB_CHANGE = 0x005A
N_GAME_STATE_CHANGE = 0x0064
N_GAME_PLAYER_STATE_CHANGE = 0x0074

# GameState
STATE_INITIALIZING = 1
STATE_PRE_GAME = 130
# PlayerState
PLAYER_RESERVED, PLAYER_QUEUED, PLAYER_CONNECTING, PLAYER_MIGRATING, PLAYER_CONNECTED = 0, 1, 2, 3, 4
# DatalessContext (DCTX)
DCTX_CREATE_GAME = 0
DCTX_JOIN_GAME = 1
# JoinState (JoinGameResponse.JGS)
JOIN_STATE_JOINED = 0

PERSONA_NAMESPACE = "cem_ea_id"
DEFAULT_LOCALE = 1701724754        # 'enBR'
DEFAULT_PING_SITE = "ea-sjc"

GAMES: dict = {}                   # id -> stan gry
GAMES_LOCK = threading.RLock()
_next_game_id = [1]

Out = tuple  # (nazwa_gracza, opis, ramka)

# Ustawiane przez blaze.py: funkcja (identity) -> ramka NotifyUserAdded [0x7802::0x0002]. Klient (menedzer
# uzytkownikow SDK) musi znac gracza z rostera zanim dostanie NotifyGameSetup/NotifyPlayerJoining -- inaczej
# szukanie uzytkownika po BlazeId zwraca NULL (patrz historia crasha lookupUsersByPersonaNames).
USER_ADDED_BUILDER = None

# ------------------------------------------------------------------------------------------------ poziomy
# Wnioski z dekompilacji i testow na zywo 2026-10-10/11 (patrz README, "Test 2026-10-11"):
# * Klient rozpoznaje "jestem hostem" porownaniem PUNKTOW KONCOWYCH sieci: punkt koncowy hosta (Game+0x3ac) ==
#   moj punkt koncowy (Game+0x42c). Punkty koncowe sa kluczowane CONG (id grupy polaczen) gracza z rostera.
#   - Gra startujaca w INITIALIZING: klient sam buduje punkt koncowy hosta z THST {CONG, CSID, HPID} i uznaje sie za
#     hosta, gdy moj BlazeId == THST.HPID oraz CONG hosta w rosterze == THST.CONG.
#   - Gra w innym stanie (PRE_GAME): host = punkt koncowy o kluczu DHST.CONG (brak DHST => klucz 0), czyli host
#     musi miec CONG == 0 (pole nieobecne), a dolaczajacy niezerowy.
#   - Gdy CONG graczy sa niezerowe, a klucz hosta nie pasuje (poziom 2 starej drabinki), NIKT nie jest hostem:
#     klient nie wysyla updateMeshConnection/finalizeGameCreation, konczy zadanie createGame od razu i przy
#     STAT=4 w setupie wywala sie na NULL w tworzeniu sesji (0x288ae0) -- to byl crash z testow 10-11.
#   - Stary przebieg (wszyscy CONG=0) dzialal tylko dlatego, ze KAZDY klient uwazal sie za hosta (kolega tez
#     wysylal finalizeGameCreation) i obaj od razu byli w setupie, wiec zaproszenie nie bylo potrzebne/wysylane.
# * Dlatego poziomy zaczynaja od setupu TYLKO DLA HOSTA (kolega dolacza dopiero przez zaproszenie -> joinGame).
# Tryb auto (gm_variant=0): kolejny createGame probuje nowszy poziom, jesli poprzedni doszedl do finalizeGameCreation
# hosta; po porazce sprawdza raz poziom docelowy, potem wraca do najnowszego dzialajacego.
LEVELS = {
    1: {"name": "1-host-sam-stary", "state": STATE_PRE_GAME, "host_state": PLAYER_CONNECTED,
        "invitee_in_setup": False, "auto_join": False, "reserved": False, "followups": True, "reason": "legacy",
        "new_players": False, "new_game": False, "host_cong": False, "user_added": False,
        "connect_host_on_finalize": False,
        "about": "stary, dzialajacy ksztalt (PRE_GAME, STAT=4, stare pola, REAS jak FIFA 14), ale setup dostaje TYLKO "
                 "host (kolega wchodzi dopiero przez zaproszenie -> joinGame); host bez CONG (jak w starym przebiegu), "
                 "dolaczajacy z CONG"},
    2: {"name": "2-host-i-zarezerwowany-kolega", "state": STATE_PRE_GAME, "host_state": PLAYER_CONNECTED,
        "invitee_in_setup": False, "auto_join": False, "reserved": True, "followups": True, "reason": "legacy",
        "new_players": False, "new_game": False, "host_cong": False, "user_added": True,
        "connect_host_on_finalize": False,
        "about": "poziom 1 + w rosterze hosta jest zapraszany kolega z PLJD jako ZAREZERWOWANY (STAT=0, z CONG); "
                 "kolega nie dostaje nic, dopoki nie wyśle joinGame"},
    3: {"name": "3-host-sam-initializing", "state": STATE_INITIALIZING, "host_state": PLAYER_CONNECTED,
        "invitee_in_setup": False, "auto_join": False, "reserved": False, "followups": False, "reason": "legacy",
        "new_players": False, "new_game": False, "host_cong": True, "user_added": False,
        "connect_host_on_finalize": False,
        "about": "poziom 1 + start w INITIALIZING (klient sam rozpoznaje hosta po THST) i spojny CONG hosta w "
                 "rosterze oraz w THST/PHST; po finalize PRE_GAME"},
    4: {"name": "4-host-laczy-sie", "state": STATE_INITIALIZING, "host_state": PLAYER_CONNECTING,
        "invitee_in_setup": False, "auto_join": False, "reserved": False, "followups": False, "reason": "legacy",
        "new_players": False, "new_game": False, "host_cong": True, "user_added": False,
        "connect_host_on_finalize": True,
        "about": "poziom 3 + host w setupie ma STAT=2 (CONNECTING), a po finalizeGameCreation serwer oglasza go jako "
                 "CONNECTED (NotifyGamePlayerStateChange + PlayerJoinCompleted)"},
    5: {"name": "5-docelowy", "state": STATE_INITIALIZING, "host_state": PLAYER_CONNECTING,
        "invitee_in_setup": False, "auto_join": False, "reserved": True, "followups": False, "reason": "new",
        "new_players": True, "new_game": True, "host_cong": True, "user_added": True,
        "connect_host_on_finalize": True,
        "about": "poziom 4 + zarezerwowany kolega w rosterze + nowe pola graczy i gry wg FIFA 17 (CONG, CSID, DSUI, "
                 "EXBL, LOC, NASP, PATT, TIME, UUID; GPVH, SEED, UUID, MNCP, PSAS, MACI), REAS DLSC/CREATE"},
}
MAX_LEVEL = max(LEVELS)
_VARIANT_LOCK = threading.Lock()
# Wersja formatu pliku stanu drabinki. Wynik zapisany starszym kodem (np. "poziom 2 nieudany" z testu, w ktorym
# klient hosta wywalil sie na starej grze GID=1) nie jest wiarygodny -- plik innej wersji jest ignorowany.
STATE_VERSION = 3
# Ile razy ten sam poziom moze urwac polaczenie klienta hosta (crash/zamkniecie okna w trakcie proby), zanim uznamy
# go za nieudany. Rozlaczenie nie jest dowodem, ze to ksztalt setupu zawinil, wiec pierwsze urwanie jest powtarzane.
MAX_CRASHES_PER_LEVEL = 2
CRASH_WINDOW_SECONDS = 120.0
_LAST_ATTEMPT: dict = {}          # host -> {"level": n, "t0": monotonic, "finalized": bool}


def _read_variant_state(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(data, dict) or data.get("v") != STATE_VERSION:
        return None
    return data


def _write_variant_state(path, data) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(data, v=STATE_VERSION), indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except (OSError, AttributeError):
        pass


def choose_variant(cfg):
    """Wybiera poziom dla nowego createGame -> (numer | None, uzasadnienie). None = pojedyncze przelaczniki
    (gm_variant = -1). Auto (0): poziom 1 na start; poprzedni poziom OK -> nastepny wyzszy (o ile nie byl juz
    nieudany), nie OK -> najnowszy dzialajacy (lub, gdy zaden nie dzialal, kolejny wyzszy). Proba, w ktorej klient
    hosta sie rozlaczyl (crash/zamkniecie okna), nie jest dowodem nieudanego ksztaltu -- ten sam poziom jest
    powtarzany (do MAX_CRASHES_PER_LEVEL razy). Stan w pliku state/gm_variant.json:
    {"v", "level", "result": pending|ok|fail|crash, "good": [...], "bad": [...], "crashes": {"poziom": n}}."""
    forced = getattr(cfg, "gm_variant", 0)
    if forced < 0:
        return None, "gm_variant=-1: pojedyncze przelaczniki gm_*"
    if forced > 0:
        number = forced if forced in LEVELS else MAX_LEVEL
        return number, f"wymuszony w config (gm_variant={forced})"
    path = getattr(cfg, "gm_variant_path", None)
    with _VARIANT_LOCK:
        st = (_read_variant_state(path) if path is not None else None) or {}
        level, result = st.get("level"), st.get("result")
        good, bad = set(st.get("good", [])), set(st.get("bad", []))
        crashes = {int(k): v for k, v in (st.get("crashes") or {}).items()}
        if level not in LEVELS:
            number, why = 1, "pierwsza proba -- zaczynamy od ksztaltu jak stary, dzialajacy przebieg"
        else:
            retry = False
            if result == "crash":
                crashes[level] = crashes.get(level, 0) + 1
                if crashes[level] < MAX_CRASHES_PER_LEVEL:
                    retry = True
                else:
                    result = "fail"
            if retry:
                number = level
                why = (f"poprzednia proba poziomu {level} skonczyla sie rozlaczeniem klienta hosta (crash/zamkniecie "
                       f"gry) -- to nie dowod, ze ksztalt jest zly; powtarzam ten sam poziom "
                       f"({crashes[level]}/{MAX_CRASHES_PER_LEVEL})")
            else:
                if result == "ok":
                    good.add(level)
                    bad.discard(level)
                else:
                    bad.add(level)
                    good.discard(level)
                best = max(good) if good else 0
                if result == "ok":
                    if level + 1 <= MAX_LEVEL and level + 1 not in bad:
                        number, why = level + 1, f"poziom {level} doszedl do finalizeGameCreation -- probuje nowszy"
                    else:
                        number, why = best, f"poziom {level} dziala, nowszego brak/nie dziala -- zostaje {best}"
                elif best:
                    if level != MAX_LEVEL and MAX_LEVEL not in good and MAX_LEVEL not in bad:
                        # poziomy sa kumulatywne, ale pierwsza porazka nie mowi nic o celu -- sprawdzamy go raz od razu
                        number = MAX_LEVEL
                        why = (f"poziom {level} NIE doszedl do finalizeGameCreation -- sprawdzam jeszcze raz od razu "
                               f"docelowy poziom {MAX_LEVEL} (potem wracam do {best})")
                    else:
                        number, why = best, f"poziom {level} NIE doszedl do finalizeGameCreation -- wraca do {best}"
                else:
                    number = level + 1 if level < MAX_LEVEL else 1
                    why = f"poziom {level} NIE doszedl do finalizeGameCreation, zaden nie dziala -- probuje {number}"
        if path is not None:
            _write_variant_state(path, {"level": number, "result": "pending", "good": sorted(good),
                                        "bad": sorted(bad), "crashes": {str(k): v for k, v in sorted(crashes.items())}})
        return number, why


def _set_result(cfg, number, result: str, allowed=("pending",)) -> None:
    path = getattr(cfg, "gm_variant_path", None)
    if path is None or number not in LEVELS or getattr(cfg, "gm_variant", 0) != 0:
        return
    with _VARIANT_LOCK:
        st = _read_variant_state(path)
        if st and st.get("level") == number and st.get("result") in allowed:
            st["result"] = result
            _write_variant_state(path, st)


def mark_finalized(cfg, number) -> None:
    """Host wyslal finalizeGameCreation -- ten poziom 'dziala'."""
    _set_result(cfg, number, "ok")


def mark_failed(cfg, number) -> None:
    """Minal czas na finalizeGameCreation hosta -- ten poziom nie dziala."""
    _set_result(cfg, number, "fail")


def mark_crashed(cfg, number) -> None:
    """Klient hosta rozlaczyl sie w trakcie proby (takze juz po werdykcie watchdoga: zamrozona gra zamykana przez
    uzytkownika). Nie liczy sie jako porazka ksztaltu -- patrz choose_variant."""
    _set_result(cfg, number, "crash", allowed=("pending", "fail"))


def effective_settings(cfg, number):
    """Parametry setupu: z poziomu albo z pojedynczych przelacznikow (number=None)."""
    if number is None:
        return {"name": "przelaczniki", "about": "pojedyncze przelaczniki gm_*",
                "state": STATE_INITIALIZING if cfg.gm_deferred_pregame else STATE_PRE_GAME,
                "host_state": cfg.gm_host_initial_state, "invitee_in_setup": not cfg.gm_faithful_flow,
                "followups": False, "reason": "new", "new_players": True, "new_game": True}
    return dict(LEVELS.get(number, LEVELS[MAX_LEVEL]))


def watchdog_expired(cfg, gid: int):
    """Host nie wyslal finalizeGameCreation w wyznaczonym czasie: poziom uznany za nieudany; opcjonalnie
    (gm_watchdog_remove) gra jest usuwana (NotifyGameRemoved), zeby klient wrocil z ekranu "please wait" bez restartu.
    Zwraca liste Out."""
    with GAMES_LOCK:
        game = GAMES.get(gid)
        if game is None or game.get("finalized"):
            return []
        game["failed"] = True
        mark_failed(cfg, game.get("variant"))
        if not getattr(cfg, "gm_watchdog_remove", True):
            return []
        outs = [(n, "NotifyGameRemoved (watchdog: brak finalizeGameCreation)", notify_game_removed(gid, 0))
                for n in game["players"]]
        del GAMES[gid]
        return outs


# Sondy: jesli klient hosta nie reaguje na setup (ani updateMeshConnection, ani finalizeGameCreation), serwer po kolei
# (co kilka sekund) wypycha powiadomienia, na ktore klient moze czekac, i loguje, ktore z nich go ruszylo.
PROBE_SCHEDULE = ((3.0, 1), (6.0, 2), (9.0, 3))


def host_reacted(gid: int) -> bool:
    with GAMES_LOCK:
        game = GAMES.get(gid)
        return game is None or bool(game.get("finalized") or game.get("mesh_seen"))


def last_probe(gid: int):
    with GAMES_LOCK:
        game = GAMES.get(gid)
        return game.get("last_probe") if game else None


def probe_frames(cfg, gid: int, n: int, lookup) -> list:
    """Sonda n (1..3) dla hosta gry `gid`; pusta lista, gdy gra zniknela albo host juz zareagowal."""
    with GAMES_LOCK:
        game = GAMES.get(gid)
        if game is None or game.get("finalized") or game.get("mesh_seen"):
            return []
        host = game["host"]
        info = _snapshot(lookup, [host])[host]
        game["last_probe"] = n
        if n == 1:
            return [(host, "SONDA 1: NotifyPlatformHostInitialized",
                     notify_platform_host_initialized(gid, info["uid"]))]
        if n == 2:
            game["pstate"][host] = PLAYER_CONNECTED
            return [(host, "SONDA 2: NotifyGamePlayerStateChange CONNECTED",
                     notify_player_state_change(gid, info["uid"], PLAYER_CONNECTED)),
                    (host, "SONDA 2: NotifyPlayerJoinCompleted", notify_player_join_completed(gid, info["uid"]))]
        return [(host, f"SONDA 3: NotifyGameStateChange PRE_GAME (stan gry u serwera: {game['state']})",
                 notify_game_state_change(gid, STATE_PRE_GAME))]


def attempt_info(gid: int):
    """(numer wariantu, nazwa, czy host dotarl do finalizeGameCreation) lub None gdy gry nie ma."""
    with GAMES_LOCK:
        game = GAMES.get(gid)
        if game is None:
            return None
        return game.get("variant"), game.get("variant_name"), bool(game.get("finalized"))


def _user_added(lookup, subject: str):
    if USER_ADDED_BUILDER is None:
        return None
    entry = lookup(subject)
    ident = (entry or {}).get("identity") or (subject, 0, b"")
    return USER_ADDED_BUILDER(ident)


def reset() -> None:
    """Czysci stan (testy)."""
    with GAMES_LOCK:
        GAMES.clear()
        _LAST_ATTEMPT.clear()
        _next_game_id[0] = 1


def _S(fields):
    """Pola struktury TDF musza isc w kolejnosci tagow -- dekoder klienta dopasowuje je sekwencyjnie."""
    return sorted(fields, key=lambda f: f[0])


def _hdr(component: int, command: int, payload: bytes, msg_type: int, msg_num: int = 0) -> bytes:
    return (len(payload).to_bytes(4, "big") + (0).to_bytes(2, "big") + component.to_bytes(2, "big")
            + command.to_bytes(2, "big") + msg_num.to_bytes(3, "big")
            + bytes([(msg_type & 7) << 5]) + bytes([0, 0]) + payload)


def notification(command: int, fields) -> bytes:
    return _hdr(GM_COMPONENT, command, tdf.encode(_S(fields)), 2)


def now_us() -> int:
    """TimeValue Blaze = mikrosekundy od epoki."""
    return int(time.time() * 1_000_000)


# ----------------------------------------------------------------------------------------------- adresy
def ip_endpoint(ip: int, port: int, maci: int):
    return [("IP  ", tdf.VARINT, ip), ("MACI", tdf.VARINT, maci), ("PORT", tdf.VARINT, port)]


def network_address(ip: int, port: int, maci: int = 0):
    """Blaze::NetworkAddress, skladowa 2 = IpPairAddress (ten sam ksztalt, jaki wysyla klient w PNET)."""
    ep = ip_endpoint(ip, port, maci)
    return (2, ("VALU", tdf.STRUCT, [("EXIP", tdf.STRUCT, ep), ("INIP", tdf.STRUCT, ep),
                                     ("MACI", tdf.VARINT, maci)]))


def peer_endpoint(info: dict):
    """(ip, port, maci), pod ktorym INNI gracze maja sie laczyc z tym graczem: adres z jakiego laczy sie z
    serwerem (VPN), bo adresy wewnetrzne/loopback ktore klient podaje w updateNetworkInfo nie sa osiagalne."""
    ip = info.get("peer_ip") or info.get("ip", 0)
    return ip, info.get("port", 0) or 3659, info.get("maci", 0)


# -------------------------------------------------------------------------------------- struktury gry
def host_info(info: dict, new: bool = True):
    """Blaze::GameManager::HostInfo {CONG, CSID, HPID, HSES, HSLT}; bez CONG (new=False): tylko {HPID, HSLT}."""
    uid = info["uid"]
    if not new:
        return [("HPID", tdf.VARINT, uid), ("HSLT", tdf.VARINT, 0)]
    return _S([("CONG", tdf.VARINT, ids.connection_group_id_for(info["name"])), ("CSID", tdf.VARINT, 0),
               ("HPID", tdf.VARINT, uid), ("HSES", tdf.VARINT, uid), ("HSLT", tdf.VARINT, 0)])


def legacy_network_address(ip: int, port: int):
    """NetworkAddress jak w starym przebiegu: IpPairAddress {EXIP{IP,PORT}, INIP{IP,PORT}} bez MACI."""
    ep = [("IP  ", tdf.VARINT, ip), ("PORT", tdf.VARINT, port)]
    return (2, ("VALU", tdf.STRUCT, [("EXIP", tdf.STRUCT, ep), ("INIP", tdf.STRUCT, ep)]))


def player_entry(info: dict, gid: int, slot: int, state: int, team_index: int = 0, new: bool = True,
                 cong=None):
    """Blaze::GameManager::ReplicatedGamePlayer (pola z refleksji EBOOT); new=False -> stary, krotszy zestaw pol.
    cong: czy dodac CONG/CSID (domyslnie tylko w nowym zestawie); klient kluczuje punkty koncowe sieci przez CONG."""
    ip, port, maci = peer_endpoint(info)
    if cong is None:
        cong = new
    cong_fields = ([("CONG", tdf.VARINT, ids.connection_group_id_for(info["name"])), ("CSID", tdf.VARINT, 0)]
                   if cong else [])
    if not new:
        return _S(cong_fields + [
            ("EXID", tdf.VARINT, info["ext"]),
            ("GID ", tdf.VARINT, gid),
            ("NAME", tdf.STRING, info["name"]),
            ("PID ", tdf.VARINT, info["uid"]),
            ("PNET", tdf.UNION, legacy_network_address(ip, port)),
            ("SID ", tdf.VARINT, slot),
            ("SLOT", tdf.VARINT, 0),
            ("STAT", tdf.VARINT, state),
            ("TIDX", tdf.VARINT, team_index),
            ("UID ", tdf.VARINT, info["uid"]),
        ])
    return _S(cong_fields + [
        ("DSUI", tdf.VARINT, 0),
        ("EXBL", tdf.BLOB, info["blob"]),
        ("EXID", tdf.VARINT, info["ext"]),
        ("GID ", tdf.VARINT, gid),
        ("LOC ", tdf.VARINT, DEFAULT_LOCALE),
        ("NAME", tdf.STRING, info["name"]),
        ("NASP", tdf.STRING, PERSONA_NAMESPACE),
        ("PATT", tdf.MAP, (tdf.STRING, tdf.STRING, [])),
        ("PID ", tdf.VARINT, info["uid"]),
        ("PNET", tdf.UNION, network_address(ip, port, maci)),
        ("SID ", tdf.VARINT, slot),
        ("SLOT", tdf.VARINT, 0),
        ("STAT", tdf.VARINT, state),
        ("TIDX", tdf.VARINT, team_index),
        ("TIME", tdf.TIME, now_us()),
        ("UID ", tdf.VARINT, info["uid"]),
        ("UUID", tdf.STRING, ""),
    ])


def roster_entry(game: dict, players: dict, name: str, slot: int):
    """Wpis rostera gracza `name` w grze `game` wg ksztaltu poziomu (CONG hosta zalezy od poziomu)."""
    shape = game.get("shape", {})
    new = shape.get("new_players", True)
    cong = new or shape.get("host_cong", True) or name != game["host"]
    return player_entry(players[name], game["id"], slot, game["pstate"][name], min(slot, 1), new, cong)


def game_data(game: dict, host: dict, state: int):
    """Blaze::GameManager::ReplicatedGameData -- wylacznie pola, ktore istnieja w FIFA 17 (poziom 1-2: zestaw
    ze starego przebiegu, razem z nieistniejacym HSES, ktory klient ignoruje)."""
    ip, port, maci = peer_endpoint(host)
    echo = game["echo"]
    new = game.get("shape", {}).get("new_game", True)
    hinfo = host_info(host, game.get("shape", {}).get("host_cong", new))
    if new:
        fields = [
            ("ADMN", tdf.LIST, (tdf.VARINT, [host["uid"]])),
            ("GID ", tdf.VARINT, game["id"]),
            ("GNAM", tdf.STRING, game["name"]),
            ("GPVH", tdf.VARINT, game["proto_hash"]),
            ("GSET", tdf.VARINT, echo.get("GSET", 0)),
            ("GSTA", tdf.VARINT, state),
            ("GTYP", tdf.STRING, echo.get("GTYP", "gameType0")),
            ("HNET", tdf.LIST, (tdf.UNION, [network_address(ip, port, maci)])),
            ("MCAP", tdf.VARINT, game["max_players"]),
            ("MNCP", tdf.VARINT, game["min_players"]),
            ("NRES", tdf.VARINT, 0),
            ("NTOP", tdf.VARINT, game["topology"]),
            ("PHST", tdf.STRUCT, hinfo),
            ("PRES", tdf.VARINT, echo.get("PRES", 1)),
            ("PSAS", tdf.STRING, DEFAULT_PING_SITE),
            ("QCAP", tdf.VARINT, echo.get("QCAP", 0)),
            ("SEED", tdf.VARINT, game["seed"]),
            ("THST", tdf.STRUCT, hinfo),
            ("UUID", tdf.STRING, game["uuid"]),
            ("VOIP", tdf.VARINT, echo.get("VOIP", 2)),
            ("VSTR", tdf.STRING, game["version"]),
        ]
    else:
        fields = [
            ("ADMN", tdf.LIST, (tdf.VARINT, [host["uid"]])),
            ("GID ", tdf.VARINT, game["id"]),
            ("GNAM", tdf.STRING, game["name"]),
            ("GSET", tdf.VARINT, echo.get("GSET", 0)),
            ("GSTA", tdf.VARINT, state),
            ("GTYP", tdf.STRING, echo.get("GTYP", "gameType0")),
            ("HNET", tdf.LIST, (tdf.UNION, [legacy_network_address(ip, port)])),
            ("HSES", tdf.VARINT, host["uid"]),
            ("MCAP", tdf.VARINT, game["max_players"]),
            ("NRES", tdf.VARINT, 0),
            ("NTOP", tdf.VARINT, game["topology"]),
            ("PHST", tdf.STRUCT, hinfo),
            ("PRES", tdf.VARINT, echo.get("PRES", 1)),
            ("QCAP", tdf.VARINT, echo.get("QCAP", 0)),
            ("THST", tdf.STRUCT, hinfo),
            ("VOIP", tdf.VARINT, echo.get("VOIP", 2)),
            ("VSTR", tdf.STRING, game["version"]),
        ]
    if echo.get("ATTR") is not None:
        fields.append(("ATTR", tdf.MAP, echo["ATTR"]))
    if echo.get("CRIT") is not None:
        fields.append(("CRIT", tdf.MAP, echo["CRIT"]))
    if echo.get("CAP") is not None:
        fields.append(("CAP ", tdf.LIST, echo["CAP"]))
    if echo.get("TIDS") is not None:
        fields.append(("TIDS", tdf.LIST, echo["TIDS"]))
    return _S(fields)


def setup_reason_dataless(cfg, dctx: int):
    """REAS = DatalessSetupContext {DCTX}: dla tworcy gry 0 (CREATE), dla dolaczajacego 1 (JOIN)."""
    if getattr(cfg, "gm_fifa17_union_tags", True):
        return (0, ("DLSC", tdf.STRUCT, [("DCTX", tdf.VARINT, dctx)]))
    return (0, ("VALU", tdf.STRUCT, []))      # stary ksztalt FIFA 14


def setup_reason_indirect_join(cfg):
    """REAS = IndirectJoinGameSetupContext {GRID, RPVC}: gracz wprowadzony do gry przez serwer (bez wlasnego
    createGame/joinGame). Tylko ta (i IndirectMatchmaking) skladowa sprawia, ze SDK klienta bez oczekujacego
    zadania zaklada "zadanie zastepcze" i doprowadza dolaczenie do konca."""
    if getattr(cfg, "gm_fifa17_union_tags", True):
        return (1, ("IJGS", tdf.STRUCT, [("RPVC", tdf.VARINT, 0)]))
    return (2, ("VALU", tdf.STRUCT, []))      # stary ksztalt FIFA 14 (numer 2 to w FIFA 17 IndirectMatchmaking!)


def setup_reason_legacy(disc: int):
    """Stary ksztalt REAS (FIFA 14): skladowa o tagu VALU -- w FIFA 17 tag nie pasuje, wiec klient zostawia unie
    pusta. disc 0 = host (create), 2 = zaproszony (w FIFA 17 to IMSC)."""
    return (disc, ("VALU", tdf.STRUCT, []))


def notify_game_setup(game: dict, players: dict, names, reason, state: int) -> bytes:
    """NotifyGameSetup {GAME, PROS, QUEU, REAS} dla listy `names` w kolejnosci slotow."""
    host = players[game["host"]]
    roster = [roster_entry(game, players, n, slot) for slot, n in enumerate(names)]
    return notification(N_GAME_SETUP, [
        ("GAME", tdf.STRUCT, game_data(game, host, state)),
        ("PROS", tdf.LIST, (tdf.STRUCT, roster)),
        ("QUEU", tdf.LIST, (tdf.STRUCT, [])),
        ("REAS", tdf.UNION, reason),
    ])


def notify_player_joining(gid: int, entry) -> bytes:
    return notification(N_PLAYER_JOINING, [("GID ", tdf.VARINT, gid), ("PDAT", tdf.STRUCT, entry),
                                           ("QOST", tdf.VARINT, 0)])


def notify_player_join_completed(gid: int, uid: int) -> bytes:
    return notification(N_PLAYER_JOIN_COMPLETED, [("GID ", tdf.VARINT, gid), ("PID ", tdf.VARINT, uid),
                                                  ("TIME", tdf.TIME, now_us())])


def notify_platform_host_initialized(gid: int, host_uid: int) -> bytes:
    return notification(N_PLATFORM_HOST_INITIALIZED, [("GID ", tdf.VARINT, gid), ("PHID", tdf.VARINT, host_uid),
                                                      ("PHST", tdf.VARINT, 0)])


def notify_game_state_change(gid: int, state: int) -> bytes:
    return notification(N_GAME_STATE_CHANGE, [("GID ", tdf.VARINT, gid), ("GSTA", tdf.VARINT, state)])


def notify_player_state_change(gid: int, uid: int, state: int) -> bytes:
    return notification(N_GAME_PLAYER_STATE_CHANGE, [("GID ", tdf.VARINT, gid), ("PID ", tdf.VARINT, uid),
                                                     ("STAT", tdf.VARINT, state)])


def notify_player_removed(gid: int, uid: int, reason: int = 0, cntx: int = 0) -> bytes:
    return notification(N_PLAYER_REMOVED, [("CNTX", tdf.VARINT, cntx), ("GID ", tdf.VARINT, gid),
                                           ("LFPJ", tdf.VARINT, 0), ("PID ", tdf.VARINT, uid),
                                           ("REAS", tdf.VARINT, reason)])


def notify_game_removed(gid: int, reason: int = 0) -> bytes:
    return notification(N_GAME_REMOVED, [("GID ", tdf.VARINT, gid), ("REAS", tdf.VARINT, reason)])


def create_game_response_fields(gid: int):
    return [("GID ", tdf.VARINT, gid)]


def join_game_response_fields(gid: int):
    return _S([("GID ", tdf.VARINT, gid), ("JGS ", tdf.VARINT, JOIN_STATE_JOINED)])


# ------------------------------------------------------------------------------------------- pomocnicze
def _field(fields, tag, default=None):
    tag = tag.rstrip()                 # tdf.decode zwraca tagi bez wyrownujacych spacji ("GID", nie "GID ")
    for t, _typ, v in fields:
        if t.rstrip() == tag:
            return v
    return default


def player_info(registry_entry: dict, name: str) -> dict:
    """Ujednolicony opis gracza z wpisu rejestru polaczen (patrz blaze._PLAYERS)."""
    _n, ext, blob = registry_entry.get("identity") or (name, 0, b"")
    return {"name": name, "uid": ids.uid_for(name), "ext": ext, "blob": blob,
            "ip": registry_entry.get("ip", 0), "port": registry_entry.get("port", 0),
            "peer_ip": registry_entry.get("peer_ip", 0), "maci": registry_entry.get("maci", 0)}


def _find_by_uid(game: dict, uid: int):
    for n in game["players"]:
        if ids.uid_for(n) == uid:
            return n
    return None


def _snapshot(lookup, names):
    """{nazwa: info} dla graczy, ktorych znamy; brakujacy gracz (rozlaczony) dostaje opis zastepczy."""
    out = {}
    for n in names:
        entry = lookup(n)
        out[n] = player_info(entry if entry is not None else {}, n)
    return out


# ------------------------------------------------------------------------------------------- createGame
def _retire_stale_locked(name: str) -> list:
    """Usuwa (NotifyGameRemoved do wszystkich czlonkow) kazda wczesniejsza gre, w ktorej jest `name`.
    Test na zywo 2026-10-11: po udanej probie poziomu 1 gra GID=1 zostala na serwerze, wiec klient hosta, dostajac
    setup GID=2, rozbieral GID=1 W TRAKCIE tworzenia GID=2 (removePlayer + updateMeshConnection STAT=0 dla GID=1)
    i padl na odczycie NULL w tworzeniu sesji gry (0x288ae0: brak biezacej gry). Klient trzyma jedna gre naraz,
    wiec przed nowa gra stara musi zniknac osobnym powiadomieniem."""
    outs = []
    for gid in [g["id"] for g in GAMES.values()
                if g["host"] == name or name in g["players"] or name in g.get("pending", [])]:
        game = GAMES.pop(gid)
        for n in game["players"]:
            outs.append((n, f"NotifyGameRemoved GID={gid} (stara gra {name!r}, nowy createGame)",
                         notify_game_removed(gid, 0)))
    return outs


def invitees_from_request(req_fields, others) -> list:
    """Zapraszani z createGame (PLJD.PLDL[*].USID.NAME) ograniczeni do graczy, ktorych serwer zna; gdy zadanie nie
    wymienia nikogo, wszyscy pozostali zalogowani gracze."""
    names = []
    pljd = _field(req_fields, "PLJD", []) or []
    pldl = _field(pljd, "PLDL")
    items = pldl[1] if isinstance(pldl, tuple) and len(pldl) == 2 else []
    for item in items or []:
        usid = _field(item, "USID", []) or []
        nm = _field(usid, "NAME")
        if nm:
            names.append(nm)
    known = [n for n in names if n in others]
    return known if known else list(others)


def create_game(cfg, host: str, req_fields, others, lookup):
    """Obsluga GameManager::createGame. Zwraca (gid, pola_odpowiedzi, [Out...]). Ksztalt pierwszego setupu
    zalezy od poziomu drabinki (LEVELS); poziom i jego uzasadnienie zostaja w grze ("variant", "variant_why")."""
    gmcd = _field(req_fields, "GMCD", []) or []
    cmgd = _field(req_fields, "CMGD", []) or []
    number, why = choose_variant(cfg)
    eff = effective_settings(cfg, number)
    with GAMES_LOCK:
        stale = _retire_stale_locked(host)
        gid = _next_game_id[0]
        _next_game_id[0] += 1
        _LAST_ATTEMPT[host] = {"level": number, "t0": time.monotonic(), "finalized": False}
        version = _field(cmgd, "GVER", "") or ""
        game = {
            "id": gid, "host": host,
            "name": _field(gmcd, "GNAM", "") or f"{host}'s game",
            "version": version, "proto_hash": zlib.crc32(version.encode("utf-8")) or 1,
            "max_players": _field(gmcd, "PMAX", 2) or 2, "min_players": _field(gmcd, "PMIN", 1) or 1,
            "topology": _field(gmcd, "NTOP", 130) or 130,
            "seed": random.getrandbits(31), "uuid": f"fifa17-{gid:08x}-{random.getrandbits(32):08x}",
            "state": eff["state"],
            "players": [host],
            "pending": list(others) if (eff.get("auto_join") and not eff["invitee_in_setup"]) else [],
            "reserved": invitees_from_request(req_fields, others) if eff.get("reserved") else [],
            "pstate": {host: eff["host_state"]}, "completed": set(),
            "variant": number, "variant_name": eff["name"], "variant_why": why, "variant_about": eff["about"],
            "shape": eff, "finalized": False, "failed": False,
            "echo": {"GSET": _field(gmcd, "GSET", 0) or 0, "PRES": _field(gmcd, "PRES", 1) or 1,
                     "VOIP": _field(gmcd, "VOIP", 2) or 2, "QCAP": _field(gmcd, "QCAP", 0) or 0,
                     "GTYP": _field(req_fields, "GTYP", "") or "gameType0",
                     "ATTR": _field(gmcd, "ATTR"), "CRIT": _field(gmcd, "CRIT"),
                     "CAP": _field(req_fields, "PCAP"), "TIDS": _field(req_fields, "TIDS")},
        }
        GAMES[gid] = game
        everybody = [host] + [n for n in others]
        players = _snapshot(lookup, everybody)
        legacy = eff["reason"] == "legacy"
        host_reason = setup_reason_legacy(0) if legacy else setup_reason_dataless(cfg, DCTX_CREATE_GAME)
        reason_label = "REAS jak FIFA 14 (VALU)" if legacy else "DLSC/CREATE"
        if eff["invitee_in_setup"]:
            # stary przebieg: roster od razu pelny (host + zapraszani w stanie hosta), kazdy dostaje swoj setup
            for j in others:
                game["players"].append(j)
                game["pstate"][j] = eff["host_state"]
            guest_reason = setup_reason_legacy(2) if legacy else setup_reason_indirect_join(cfg)
            outs = []
            for me_ in [host] + list(others):
                for other in game["players"]:
                    if other == me_ or not eff.get("user_added", True):
                        continue
                    fr = _user_added(lookup, other)
                    if fr is not None:
                        outs.append((me_, f"NotifyUserAdded {other}", fr))
                is_host = me_ == host
                outs.append((me_, f"NotifyGameSetup [0x0004::0x0014] ({'host' if is_host else 'zaproszony'}, "
                                  f"pelny roster, {reason_label if is_host else 'REAS zaproszonego'})",
                             notify_game_setup(game, players, game["players"],
                                               host_reason if is_host else guest_reason, game["state"])))
        else:
            for r in game["reserved"]:
                game["pstate"][r] = PLAYER_RESERVED
            roster_names = [host] + list(game["reserved"])
            outs = []
            for r in game["reserved"]:
                if eff.get("user_added", True):
                    fr = _user_added(lookup, r)
                    if fr is not None:
                        outs.append((host, f"NotifyUserAdded {r}", fr))
            outs.append((host, f"NotifyGameSetup [0x0004::0x0014] (host, {reason_label}"
                               + (f", zarezerwowani: {game['reserved']}" if game["reserved"] else "") + ")",
                         notify_game_setup(game, players, roster_names, host_reason, game["state"])))
        if eff["followups"]:
            # jak w starym przebiegu: po setupie stany graczy i gry jeszcze raz jako osobne powiadomienia
            snap = _snapshot(lookup, game["players"])
            for target in game["players"]:
                for n in game["players"]:
                    outs.append((target, f"NotifyGamePlayerStateChange {n}={game['pstate'][n]}",
                                 notify_player_state_change(gid, snap[n]["uid"], game["pstate"][n])))
                outs.append((target, f"NotifyGameStateChange {game['state']}",
                             notify_game_state_change(gid, game["state"])))
        return gid, create_game_response_fields(gid), stale + outs


def _join_players(cfg, game: dict, joiners, lookup, send_platform_host: bool = True,
                  context="indirect") -> list:
    """Dolacza `joiners` do gry: dolaczajacy dostaje NotifyGameSetup, pozostali NotifyPlayerJoining."""
    outs = []
    for j in joiners:
        if j in game["players"]:
            continue
        was_reserved = j in game.get("reserved", [])
        if was_reserved:
            game["reserved"].remove(j)
        game["pstate"][j] = cfg.gm_initial_player_state
        game["players"].append(j)
        players = _snapshot(lookup, game["players"])
        # wzajemne NotifyUserAdded: dolaczajacy poznaje graczy gry, a gracze gry poznaja dolaczajacego
        for other in game["players"]:
            if other == j:
                continue
            fr = _user_added(lookup, other)
            if fr is not None:
                outs.append((j, f"NotifyUserAdded {other}", fr))
            fr = _user_added(lookup, j)
            if fr is not None:
                outs.append((other, f"NotifyUserAdded {j}", fr))
        if context == "indirect" and getattr(cfg, "gm_indirect_join", True):
            reason = setup_reason_indirect_join(cfg)
            label = "NotifyGameSetup (zaproszony, IJGS)"
        else:
            reason = setup_reason_dataless(cfg, DCTX_JOIN_GAME)
            label = "NotifyGameSetup (dolaczajacy, DLSC/JOIN)"
        # dolaczajacy dostaje gre w stanie PRE_GAME (SDK odracza graczy dolaczajacych do gry nie w PRE_GAME)
        setup_state = STATE_PRE_GAME if game["state"] == STATE_INITIALIZING else game["state"]
        outs.append((j, label, notify_game_setup(game, players, game["players"], reason, setup_state)))
        if send_platform_host:
            outs.append((j, "NotifyPlatformHostInitialized",
                         notify_platform_host_initialized(game["id"], players[game["host"]]["uid"])))
        if was_reserved:
            # kolega byl juz w rosterze hosta (STAT=0): host dostaje tylko zmiane jego stanu
            for other in game["players"]:
                if other != j:
                    outs.append((other, f"NotifyGamePlayerStateChange {j}={game['pstate'][j]} (rezerwacja zajeta)",
                                 notify_player_state_change(game["id"], players[j]["uid"], game["pstate"][j])))
        elif cfg.gm_send_player_joining:
            entry = roster_entry(game, players, j, game["players"].index(j))
            for other in game["players"]:
                if other != j:
                    outs.append((other, "NotifyPlayerJoining", notify_player_joining(game["id"], entry)))
    return outs


# ----------------------------------------------------------------------- finalizeGameCreation / mesh
def finalize_game(cfg, name: str, req_fields, lookup) -> list:
    """UpdateGameSessionRequest (0x0F): host skonczyl inicjalizacje sieci -> PlatformHostInitialized,
    stan gry INITIALIZING -> PRE_GAME, potem (zaproszeni nie bedacy jeszcze w grze) dolaczaja."""
    gid = _field(req_fields, "GID", 0) or 0
    with GAMES_LOCK:
        game = GAMES.get(gid)
        if game is None:
            return []
        if name == game["host"]:
            game["finalized"] = True
            mark_finalized(cfg, game.get("variant"))
            if name in _LAST_ATTEMPT:
                _LAST_ATTEMPT[name]["finalized"] = True
        players = _snapshot(lookup, game["players"])
        host_uid = players[game["host"]]["uid"]
        outs = []
        for n in game["players"]:
            outs.append((n, "NotifyPlatformHostInitialized", notify_platform_host_initialized(gid, host_uid)))
        host_name = game["host"]
        if (name == host_name and game.get("shape", {}).get("connect_host_on_finalize")
                and game["pstate"].get(host_name) != PLAYER_CONNECTED):
            # host nie wysyla updateMeshConnection (sam jest platforma gry), wiec to finalizeGameCreation jest
            # sygnalem, ze jest polaczony: stan gracza -> CONNECTED + JoinCompleted dla wszystkich w grze
            game["pstate"][host_name] = PLAYER_CONNECTED
            game["completed"].add(host_name)
            for n in game["players"]:
                outs.append((n, f"NotifyGamePlayerStateChange CONNECTED {host_name} (po finalize)",
                             notify_player_state_change(gid, host_uid, PLAYER_CONNECTED)))
                outs.append((n, f"NotifyPlayerJoinCompleted {host_name} (po finalize)",
                             notify_player_join_completed(gid, host_uid)))
        if game["state"] == STATE_INITIALIZING:
            game["state"] = STATE_PRE_GAME
            for n in game["players"]:
                outs.append((n, "NotifyGameStateChange PRE_GAME", notify_game_state_change(gid, STATE_PRE_GAME)))
        pending, game["pending"] = list(game["pending"]), []
        outs.extend(_join_players(cfg, game, pending, lookup, context="indirect"))
        return outs


def update_mesh_connection(cfg, name: str, req_fields, lookup) -> list:
    """updateMeshConnection (0x1D): klient zglasza stan polaczenia z grupa TCG. STAT 2 = polaczony.
    Gracz, ktory zakonczyl dolaczanie, dostaje ACTIVE_CONNECTED + NotifyPlayerJoinCompleted (wszyscy w grze)."""
    gid = _field(req_fields, "GID", 0) or 0
    stat = _field(req_fields, "STAT", 0)
    tcg = _field(req_fields, "TCG", (0, 0, 0)) or (0, 0, 0)
    with GAMES_LOCK:
        game = GAMES.get(gid)
        if game is not None and name == game["host"]:
            game["mesh_seen"] = True              # klient hosta ruszyl (nawet jesli to jeszcze nie finalize)
        if game is None or name not in game["players"] or stat != 2:
            return []
        target = _find_by_uid(game, tcg[2]) if len(tcg) > 2 else None
        candidates = {name} if target in (None, name) else {name, target}
        outs = []
        for n in sorted(candidates, key=game["players"].index):
            if n in game["completed"]:
                continue
            game["completed"].add(n)
            game["pstate"][n] = PLAYER_CONNECTED
            uid = ids.uid_for(n)
            for member in game["players"]:
                outs.append((member, f"NotifyGamePlayerStateChange CONNECTED {n}",
                             notify_player_state_change(gid, uid, PLAYER_CONNECTED)))
                outs.append((member, f"NotifyPlayerJoinCompleted {n}", notify_player_join_completed(gid, uid)))
        return outs


# --------------------------------------------------------------------------------- wychodzenie z gry
def remove_player(cfg, name: str, req_fields, lookup) -> list:
    """removePlayer (0x0B): {BTPL, CNTX, GID, PID, REAS, SCTX}."""
    gid = _field(req_fields, "GID", 0) or 0
    pid = _field(req_fields, "PID", 0)
    reason = _field(req_fields, "REAS", 0) or 0
    cntx = _field(req_fields, "CNTX", 0) or 0
    with GAMES_LOCK:
        game = GAMES.get(gid)
        if game is None:
            return []
        target = _find_by_uid(game, pid) if pid else name
        if target is None:
            target = name
        outs = []
        if target == game["host"]:
            # tworca wyszedl -- bez migracji hosta gra konczy sie dla wszystkich
            for n in game["players"]:
                outs.append((n, "NotifyGameRemoved (host wyszedl)", notify_game_removed(gid, 0)))
            del GAMES[gid]
            return outs
        uid = ids.uid_for(target)
        for n in game["players"]:
            outs.append((n, f"NotifyPlayerRemoved {target}", notify_player_removed(gid, uid, reason, cntx)))
        game["players"].remove(target)
        game["pstate"].pop(target, None)
        game["completed"].discard(target)
        if target in game["pending"]:
            game["pending"].remove(target)
        if not game["players"]:
            del GAMES[gid]
        return outs


def destroy_game(cfg, name: str, req_fields, lookup) -> list:
    gid = _field(req_fields, "GID", 0) or 0
    reason = _field(req_fields, "REAS", 0) or 0
    with GAMES_LOCK:
        game = GAMES.pop(gid, None)
        if game is None:
            return []
        return [(n, "NotifyGameRemoved", notify_game_removed(gid, reason)) for n in game["players"]]


def join_game(cfg, name: str, req_fields, lookup):
    """joinGame (0x09) -- gracz dolacza do istniejacej gry. Zwraca (pola_odpowiedzi, [Out...])."""
    gid = _field(req_fields, "GID", 0) or 0
    with GAMES_LOCK:
        game = GAMES.get(gid)
        if game is None:
            return None, []
        outs = _join_players(cfg, game, [name], lookup, context="join")
        return join_game_response_fields(gid), outs


def broadcast_change(cfg, name: str, command: int, req_fields) -> list:
    """advanceGameState / setGameAttributes / setPlayerAttributes: serwer potwierdza i rozsyla zmiane wszystkim
    graczom gry (ksztalt zadania == ksztalt powiadomienia, jak w FIFA 14)."""
    gid = _field(req_fields, "GID", 0) or 0
    with GAMES_LOCK:
        game = GAMES.get(gid)
        if game is None or not getattr(cfg, "gm_followups", True):
            return []
        outs = []
        if command == CMD_ADVANCE_GAME_STATE:
            state = _field(req_fields, "GSTA", 0)
            game["state"] = state
            frame = notify_game_state_change(gid, state)
            return [(n, "NotifyGameStateChange (advanceGameState)", frame) for n in game["players"]]
        notif = N_GAME_ATTRIB_CHANGE if command == CMD_SET_GAME_ATTRIBUTES else N_PLAYER_ATTRIB_CHANGE
        frame = notification(notif, [(t, typ, v) for t, typ, v in req_fields])
        label = "NotifyGameAttribChange" if command == CMD_SET_GAME_ATTRIBUTES else "NotifyPlayerAttribChange"
        for n in game["players"]:
            outs.append((n, label, frame))
        return outs


def game_of(name: str):
    """Gra, w ktorej jest gracz (lub None)."""
    with GAMES_LOCK:
        for g in GAMES.values():
            if name in g["players"]:
                return g
    return None


def on_disconnect(cfg, name: str, lookup) -> list:
    """Gracz stracil polaczenie z serwerem: wypisz go ze wszystkich gier."""
    outs = []
    with GAMES_LOCK:
        last = _LAST_ATTEMPT.pop(name, None)
        if (last and not last["finalized"] and last["level"] is not None
                and time.monotonic() - last["t0"] < CRASH_WINDOW_SECONDS):
            mark_crashed(cfg, last["level"])
        for gid in [g["id"] for g in GAMES.values() if name in g["players"]]:
            uid = ids.uid_for(name)
            outs.extend(remove_player(cfg, name, [("GID ", tdf.VARINT, gid), ("PID ", tdf.VARINT, uid),
                                                  ("REAS", tdf.VARINT, 1)], lookup))
    return [o for o in outs if o[0] != name]
