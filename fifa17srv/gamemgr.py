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
# Test na zywo 2026-10-09/10 pokazal: po przebudowie GameManager klient hosta NIE wysyla ani updateMeshConnection,
# ani finalizeGameCreation (ostatni test na zywo ze starym przebiegiem, commit 2c9842c: wysylal oba od razu po
# setupie, a potem szedl dalej -- lookup kolegi, statystyki). Dekompilacja (handler NotifyGameSetup 0xc6e0b8,
# onNetworkCreated 0xc6a92c -> 0xc6a4e0 -> finalize 0xc6a204) nie wskazuje jednej przyczyny, wiec serwer potrafi
# wejsc po "drabince" od kształtu BAJT W BAJT jak stary (poziom 1) do obecnego docelowego (poziom 6), dodajac
# po jednej zmianie na poziom. Tryb auto (gm_variant=0): kolejny createGame probuje nowszy poziom, jesli
# poprzedni doszedl do finalizeGameCreation hosta; po porazce wraca do najnowszego dzialajacego.
LEVELS = {
    1: {"name": "1-stary-ksztalt", "state": STATE_PRE_GAME, "host_state": PLAYER_CONNECTED,
        "invitee_in_setup": True, "followups": True, "reason": "legacy", "new_players": False, "new_game": False,
        "user_added": False,
        "about": "bajt w bajt jak stary przebieg: PRE_GAME, obaj gracze w setupie, stare pola, REAS z tagiem FIFA 14"},
    2: {"name": "2-nowi-gracze", "state": STATE_PRE_GAME, "host_state": PLAYER_CONNECTED,
        "invitee_in_setup": True, "followups": True, "reason": "legacy", "new_players": True, "new_game": False,
        "about": "poziom 1 + nowe pola graczy w rosterze (CONG, CSID, DSUI, EXBL, LOC, NASP, PATT, TIME, UUID) "
                 "+ wzajemne NotifyUserAdded"},
    3: {"name": "3-nowa-gra", "state": STATE_PRE_GAME, "host_state": PLAYER_CONNECTED,
        "invitee_in_setup": True, "followups": True, "reason": "legacy", "new_players": True, "new_game": True,
        "about": "poziom 2 + nowe pola gry (GPVH, SEED, UUID, MNCP, PSAS, pelny HostInfo, MACI w adresach)"},
    4: {"name": "4-nowy-REAS", "state": STATE_PRE_GAME, "host_state": PLAYER_CONNECTED,
        "invitee_in_setup": True, "followups": True, "reason": "new", "new_players": True, "new_game": True,
        "about": "poziom 3 + REAS wg FIFA 17 (host DLSC/CREATE, zaproszony IJGS)"},
    5: {"name": "5-zaproszony-po-finalize", "state": STATE_PRE_GAME, "host_state": PLAYER_CONNECTED,
        "invitee_in_setup": False, "followups": True, "reason": "new", "new_players": True, "new_game": True,
        "about": "poziom 4 + host sam w pierwszym setupie, zaproszony dopiero po finalizeGameCreation"},
    6: {"name": "6-initializing", "state": STATE_INITIALIZING, "host_state": PLAYER_CONNECTED,
        "invitee_in_setup": False, "followups": False, "reason": "new", "new_players": True, "new_game": True,
        "about": "poziom 5 + gra startuje w INITIALIZING (PRE_GAME po finalize), bez follow-upow -- docelowy"},
}
MAX_LEVEL = max(LEVELS)
_VARIANT_LOCK = threading.Lock()


def _read_variant_state(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, AttributeError):
        return None


def _write_variant_state(path, data) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except (OSError, AttributeError):
        pass


def choose_variant(cfg):
    """Wybiera poziom dla nowego createGame -> (numer | None, uzasadnienie). None = pojedyncze przelaczniki
    (gm_variant = -1). Auto (0): poziom 1 na start; poprzedni poziom OK -> nastepny wyzszy (o ile nie byl juz
    nieudany), nie OK -> najnowszy dzialajacy (lub, gdy zaden nie dzialal, kolejny wyzszy). Stan w pliku
    state/gm_variant.json: {"level", "result": pending|ok|fail, "good": [...], "bad": [...]}."""
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
        if level not in LEVELS:
            number, why = 1, "pierwsza proba -- zaczynamy od ksztaltu jak stary, dzialajacy przebieg"
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
                number, why = best, f"poziom {level} NIE doszedl do finalizeGameCreation -- wraca do {best}"
            else:
                number = level + 1 if level < MAX_LEVEL else 1
                why = f"poziom {level} NIE doszedl do finalizeGameCreation, zaden nie dziala -- probuje {number}"
        if path is not None:
            _write_variant_state(path, {"level": number, "result": "pending",
                                        "good": sorted(good), "bad": sorted(bad)})
        return number, why


def _set_result(cfg, number, result: str) -> None:
    path = getattr(cfg, "gm_variant_path", None)
    if path is None or number not in LEVELS or getattr(cfg, "gm_variant", 0) != 0:
        return
    with _VARIANT_LOCK:
        st = _read_variant_state(path)
        if st and st.get("level") == number and st.get("result") == "pending":
            st["result"] = result
            _write_variant_state(path, st)


def mark_finalized(cfg, number) -> None:
    """Host wyslal finalizeGameCreation -- ten poziom 'dziala'."""
    _set_result(cfg, number, "ok")


def mark_failed(cfg, number) -> None:
    """Minal czas na finalizeGameCreation hosta -- ten poziom nie dziala."""
    _set_result(cfg, number, "fail")


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
    """Blaze::GameManager::HostInfo {CONG, CSID, HPID, HSES, HSLT}; stary ksztalt (poziom 1-2): tylko {HPID, HSLT}."""
    uid = info["uid"]
    if not new:
        return [("HPID", tdf.VARINT, uid), ("HSLT", tdf.VARINT, 0)]
    return _S([("CONG", tdf.VARINT, ids.connection_group_id_for(info["name"])), ("CSID", tdf.VARINT, 0),
               ("HPID", tdf.VARINT, uid), ("HSES", tdf.VARINT, uid), ("HSLT", tdf.VARINT, 0)])


def legacy_network_address(ip: int, port: int):
    """NetworkAddress jak w starym przebiegu: IpPairAddress {EXIP{IP,PORT}, INIP{IP,PORT}} bez MACI."""
    ep = [("IP  ", tdf.VARINT, ip), ("PORT", tdf.VARINT, port)]
    return (2, ("VALU", tdf.STRUCT, [("EXIP", tdf.STRUCT, ep), ("INIP", tdf.STRUCT, ep)]))


def player_entry(info: dict, gid: int, slot: int, state: int, team_index: int = 0, new: bool = True):
    """Blaze::GameManager::ReplicatedGamePlayer (pola z refleksji EBOOT); new=False -> stary, krotszy zestaw pol."""
    ip, port, maci = peer_endpoint(info)
    if not new:
        return _S([
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
    return _S([
        ("CONG", tdf.VARINT, ids.connection_group_id_for(info["name"])),
        ("CSID", tdf.VARINT, 0),
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


def game_data(game: dict, host: dict, state: int):
    """Blaze::GameManager::ReplicatedGameData -- wylacznie pola, ktore istnieja w FIFA 17 (poziom 1-2: zestaw
    ze starego przebiegu, razem z nieistniejacym HSES, ktory klient ignoruje)."""
    ip, port, maci = peer_endpoint(host)
    echo = game["echo"]
    new = game.get("shape", {}).get("new_game", True)
    hinfo = host_info(host, new)
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
    newp = game.get("shape", {}).get("new_players", True)
    roster = [player_entry(players[n], game["id"], slot, game["pstate"][n], min(slot, 1), newp)
              for slot, n in enumerate(names)]
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
def create_game(cfg, host: str, req_fields, others, lookup):
    """Obsluga GameManager::createGame. Zwraca (gid, pola_odpowiedzi, [Out...]). Ksztalt pierwszego setupu
    zalezy od poziomu drabinki (LEVELS); poziom i jego uzasadnienie zostaja w grze ("variant", "variant_why")."""
    gmcd = _field(req_fields, "GMCD", []) or []
    cmgd = _field(req_fields, "CMGD", []) or []
    number, why = choose_variant(cfg)
    eff = effective_settings(cfg, number)
    with GAMES_LOCK:
        gid = _next_game_id[0]
        _next_game_id[0] += 1
        version = _field(cmgd, "GVER", "") or ""
        game = {
            "id": gid, "host": host,
            "name": _field(gmcd, "GNAM", "") or f"{host}'s game",
            "version": version, "proto_hash": zlib.crc32(version.encode("utf-8")) or 1,
            "max_players": _field(gmcd, "PMAX", 2) or 2, "min_players": _field(gmcd, "PMIN", 1) or 1,
            "topology": _field(gmcd, "NTOP", 130) or 130,
            "seed": random.getrandbits(31), "uuid": f"fifa17-{gid:08x}-{random.getrandbits(32):08x}",
            "state": eff["state"],
            "players": [host], "pending": [] if eff["invitee_in_setup"] else list(others),
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
            outs = [(host, f"NotifyGameSetup [0x0004::0x0014] (host, {reason_label})",
                     notify_game_setup(game, players, [host], host_reason, game["state"]))]
        if eff["followups"]:
            # jak w starym przebiegu: po setupie stany graczy i gry jeszcze raz jako osobne powiadomienia
            snap = _snapshot(lookup, game["players"])
            for target in game["players"]:
                for n in game["players"]:
                    outs.append((target, f"NotifyGamePlayerStateChange {n}={game['pstate'][n]}",
                                 notify_player_state_change(gid, snap[n]["uid"], game["pstate"][n])))
                outs.append((target, f"NotifyGameStateChange {game['state']}",
                             notify_game_state_change(gid, game["state"])))
        return gid, create_game_response_fields(gid), outs


def _join_players(cfg, game: dict, joiners, lookup, send_platform_host: bool = True,
                  context="indirect") -> list:
    """Dolacza `joiners` do gry: dolaczajacy dostaje NotifyGameSetup, pozostali NotifyPlayerJoining."""
    outs = []
    for j in joiners:
        if j in game["players"]:
            continue
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
        if cfg.gm_send_player_joining:
            entry = player_entry(players[j], game["id"], game["players"].index(j), game["pstate"][j],
                                 min(game["players"].index(j), 1), game.get("shape", {}).get("new_players", True))
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
        players = _snapshot(lookup, game["players"])
        host_uid = players[game["host"]]["uid"]
        outs = []
        for n in game["players"]:
            outs.append((n, "NotifyPlatformHostInitialized", notify_platform_host_initialized(gid, host_uid)))
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
        for gid in [g["id"] for g in GAMES.values() if name in g["players"]]:
            uid = ids.uid_for(name)
            outs.extend(remove_player(cfg, name, [("GID ", tdf.VARINT, gid), ("PID ", tdf.VARINT, uid),
                                                  ("REAS", tdf.VARINT, 1)], lookup))
    return [o for o in outs if o[0] != name]
