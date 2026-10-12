"""Atrapa uslugi EASW ("SportsWorld") -- tyle, ile potrzeba, zeby klient FIFA 17 uznal ja za dzialajaca.

Dlaczego: po udanym createGame ekran hosta wchodzi w poczekalnie (ekrany 0x64/0x63), a UI od razu odpala trzy operacje
SportsWorld (ConnectToSportsWorld, LoadSportsWorldUsers = lista znajomych EASW, LoadSportsWorldUserPresence). Serwer
nie podawal adresow uslugi, wiec wszystkie konczyly sie bledem ("ServerError SportsWorld disconnect"), a po ~1,3 s UI
wracal do menu (0x74 -> 0x65). Lista znajomych SportsWorld to zarazem lista, z ktorej prawdziwe UI wybiera
zapraszanego -- bez niej skrypt UI nigdy nie wola SendGameInvite.

Wszystko ponizej wynika z dezasemblacji EBOOT (moduly 0x308ebc-0x3152a0; RE w README, "SportsWorld"):

* Klient czyta z konfiguracji (fetchClientConfig, ten sam sklep co FUT_RS4_BASE_URL) klucze OSDK_EASW_AUTH_URL,
  OSDK_EASW_REQ_URL, _MEDIA_URL, _EVENT_URL (domyslnie = REQ), _GF_FILE_URL (kazdy max 63 znaki!) oraz
  OSDK_EASW_ALLOWED_LOCALES. Bez tego ostatniego domyslna wartosc "----" odrzuca KAZDA lokalizacje i operacja
  Connect konczy sie bledem, zanim wyslemy cokolwiek (0x30905c).
* Connect: POST <AUTH>/authenticationPS3?version=v2&locale=<xxXX>&skuid=<n> z biletem PSN w tresci. Klient czyta z
  NAGLOWKOW odpowiedzi (0x30f3a0): "EASW-Token: " (wymagany, niepusty = sukces), "EASW-Session: ",
  "EASW-Nucleus-Persona: " i "EASW-Userid: " (liczba dziesietna; trafia do sciezek jako %u). Wyszukiwanie jest
  rozrozniajace wielkosc liter, koniec wartosci = CR. Naglowki odpowiedzi czytane sa do bufora 0x300 B.
* Lista znajomych: GET <REQ>/personas/<uid>/buddies/<sku>;full -> XML, w ktorym DirtySDK XmlFind szuka sciezki
  "buddies.buddylist.buddy"; z kazdego elementu brany jest <handle> (nazwa persony, max 16 znakow) i szukany
  w menedzerze uzytkownikow (0x311db0).
* Obecnosc: GET <REQ>/personas/<uid>/sku/<sku>/onlineStatus?handles=a,b -> XML "addressbook.buddy" z <handle> i
  <presence> (tekst rozny od "OFFLINE" = online) (0x31388c).
* Pozostale operacje (profil, awatary, nagrody, zdarzenia...) dostaja 200 z pustym XML-em i sa NAGRYWANE (plik
  pow_*.txt w logs/captures), zeby przy nastepnym tescie zobaczyc, o co jeszcze klient pyta.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
import urllib.parse
from typing import Callable, Optional

from . import ids

BASE_PATH = "/easw"
# Wartosc OSDK_EASW_ALLOWED_LOCALES: lista (po przecinku) czteroznakowych masek; '*' pasuje do dowolnego znaku.
ALLOWED_LOCALES = "****"
MAX_URL_LEN = 63                         # bufory URL w kliencie maja 0x40 bajtow

# Ustawiane przez blaze.py: funkcja () -> [{"name", "peer"}] polaczonych graczy (peer = adres IP klienta jako tekst).
PLAYERS_FN: Optional[Callable[[], list]] = None

_LOCK = threading.Lock()
_SEEN: dict = {}                         # nazwa -> czas ostatniego uwierzytelnienia / zobaczenia (znajomi EASW)
_SESSIONS: dict = {}                     # token -> nazwa persony
_PEER_LAST: dict = {}                    # adres IP -> ostatnia persona uwierzytelniona z tego adresu


def config_items(host: str, port: int) -> list:
    """Klucze fetchClientConfig dla klienta. Adres bez konczacego '/' (klient dokleja "/sciezka")."""
    base = f"http://{host}:{port}{BASE_PATH}"
    assert len(base) <= MAX_URL_LEN, base
    return [
        ("OSDK_EASW_AUTH_URL", base),
        ("OSDK_EASW_REQ_URL", base),
        ("OSDK_EASW_MEDIA_URL", base),
        ("OSDK_EASW_EVENT_URL", base),
        ("OSDK_EASW_GF_FILE_URL", base),
        ("OSDK_EASW_ALLOWED_LOCALES", ALLOWED_LOCALES),
    ]


def reset_state() -> None:
    with _LOCK:
        _SEEN.clear()
        _SESSIONS.clear()
        _PEER_LAST.clear()


def _online_players() -> list:
    try:
        return list(PLAYERS_FN()) if PLAYERS_FN else []
    except Exception:
        return []


def _remember(name: str) -> None:
    with _LOCK:
        _SEEN[name] = time.time()


def _name_for_uid(uid: int) -> Optional[str]:
    with _LOCK:
        names = set(_SEEN)
    names.update(p["name"] for p in _online_players())
    for n in names:
        if ids.uid_for(n) == uid:
            return n
    return None


def identify_player(peer: str, body: bytes = b"") -> Optional[str]:
    """Kto laczy sie z atrapa? 1) nazwa persony wystepujaca w bilecie PSN (RPCS3 wpisuje tam jawny Online ID),
    2) jedyny polaczony gracz z tego adresu IP, 3) ostatnia persona z tego adresu, 4) jedyny polaczony gracz."""
    players = _online_players()
    names = [p["name"] for p in players]
    for n in sorted(names, key=len, reverse=True):
        if n.encode("utf-8") in body:
            return n
    same = [p["name"] for p in players if p.get("peer") == peer]
    if len(same) == 1:
        return same[0]
    with _LOCK:
        last = _PEER_LAST.get(peer)
    if last:
        return last
    if len(names) == 1:
        return names[0]
    return same[0] if same else (names[0] if names else None)


def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _response(status: int, headers: list, body: bytes = b"", ctype: str = "text/xml"):
    return status, [("Content-Type", ctype)] + headers, body


def auth_response(peer: str, query: dict, body: bytes):
    """POST /authenticationPS3 -> 200 + naglowki EASW-* (patrz doc modulu)."""
    name = identify_player(peer, body)
    if name is None:
        return _response(401, [], b"<error>unknown player</error>"), None
    uid = ids.uid_for(name)
    token = hashlib.sha1(f"easw-token:{name}:{int(time.time() // 3600)}".encode()).hexdigest()
    session = hashlib.sha1(f"easw-session:{name}".encode()).hexdigest()[:32]
    with _LOCK:
        _SEEN[name] = time.time()
        _SESSIONS[token] = name
        _PEER_LAST[peer] = name
    headers = [("EASW-Token", token), ("EASW-Session", session),
               ("EASW-Nucleus-Persona", str(ids.persona_id_for(name))), ("EASW-Userid", str(uid))]
    payload = b"<authentication><result>OK</result></authentication>"
    return _response(200, headers, payload), name


def buddies_xml(me: Optional[str]) -> bytes:
    """Lista znajomych EASW = pozostali gracze, ktorych serwer zna (polaczeni teraz albo uwierzytelnieni od startu)."""
    with _LOCK:
        names = set(_SEEN)
    names.update(p["name"] for p in _online_players())
    names.discard(me or "")
    rows = "".join(f"<buddy><handle>{_xml_escape(n)}</handle></buddy>" for n in sorted(names))
    return f"<buddies><buddylist>{rows}</buddylist></buddies>".encode("utf-8")


def presence_xml(handles: list) -> bytes:
    online = {p["name"] for p in _online_players()}
    rows = "".join(
        f"<buddy><handle>{_xml_escape(h)}</handle><presence>{'ONLINE' if h in online else 'OFFLINE'}</presence></buddy>"
        for h in handles)
    return f"<addressbook>{rows}</addressbook>".encode("utf-8")


_RE_BUDDIES = re.compile(r"^/personas/(\d+)/buddies/")
_RE_PRESENCE = re.compile(r"^/personas/(\d+)/sku/[^/]+/onlineStatus$")


def handle_request(method: str, target: str, headers: dict, body: bytes, peer: str):
    """-> (status, [(naglowek, wartosc)], tresc, opis_do_logu). `target` = sciezka z zapytaniem, zaczyna sie od BASE_PATH."""
    parsed = urllib.parse.urlsplit(target)
    path = parsed.path[len(BASE_PATH):] if parsed.path.startswith(BASE_PATH) else parsed.path
    query = urllib.parse.parse_qs(parsed.query)
    if path.startswith("/authentication"):
        (status, hdrs, payload), name = auth_response(peer, query, body)
        return status, hdrs, payload, f"EASW auth -> {status} persona={name!r}"
    m = _RE_BUDDIES.match(path)
    if m:
        me = _name_for_uid(int(m.group(1)))
        payload = buddies_xml(me)
        return 200, [("Content-Type", "text/xml")], payload, f"EASW znajomi dla {me!r}: {payload[:200]!r}"
    m = _RE_PRESENCE.match(path.split(";", 1)[0])
    if m:
        raw = query.get("handles", [""])[0]
        handles = [h for h in raw.split(",") if h]
        payload = presence_xml(handles)
        return 200, [("Content-Type", "text/xml")], payload, f"EASW obecnosc {handles}: {payload[:200]!r}"
    return 200, [("Content-Type", "text/xml")], b"<response></response>", \
        f"EASW (nieobslugiwane, nagrane) {method} {path}"


def render_http(status: int, headers: list, payload: bytes) -> bytes:
    reason = {200: "OK", 401: "Unauthorized", 404: "Not Found"}.get(status, "OK")
    out = [f"HTTP/1.1 {status} {reason}"]
    names = {h.lower() for h, _ in headers}
    out += [f"{k}: {v}" for k, v in headers]
    if "content-length" not in names:
        out.append(f"Content-Length: {len(payload)}")
    out.append("Connection: close")
    return ("\r\n".join(out) + "\r\n\r\n").encode("latin-1") + payload
