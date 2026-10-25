"""Atrapa uslug webowych EASFC (POW) i FUT -- tylko NAGRYWA zadania HTTP.

Z analizy EBOOT (2026-10-09):
* POWService czyta FIFA_POW_URL / FIFA_POW_CONTENT_SERVER_URL (domyslnie http://pas.gt.easfc.ea.com:8094 i
  http://content.lt.easfc.ea.com:8080), ale w RPCS3.log nie ma ani jednego zapytania POW (healthcheck
  "pow/healthcheck/system/all") -- ani DNS, ani polaczenia na te porty. Powod nie jest ustalony.
* Dwa nieudane zapytania DNS tuz po zalogowaniu ('' oraz 'ut') pochodza z modulu FUT: funkcja po
  zalogowaniu (0x4eab24) przekazuje obiektowi FUT adresy z kluczy FUT_RS4_BASE_URL i
  FUTDYNAMICMESSAGES_URL_BASE tylko gdy serwer je poda; bez nich URL = "ut/game/fifa17/..." (host 'ut')
  i "/messages" (host '').
Ta atrapa przyjmuje polaczenia pod pow_port / pow_content_port, zapisuje pelne zadanie (metoda, sciezka,
naglowki, cialo) do logs/captures i odpowiada 200 z pustym JSON-em, zeby poznac protokol. Nie udaje
prawdziwej uslugi.
"""
from __future__ import annotations

import socket

from . import easw
from .config import Config
from .server import Capture


def _read_request(conn: socket.socket, cap: Capture):
    buf = b""
    while b"\r\n\r\n" not in buf and len(buf) < 65536:
        chunk = conn.recv(4096)
        if not chunk:
            return None, b""
        cap.data("C->S", chunk)
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    length = 0
    for line in head.decode("latin-1").split("\r\n")[1:]:
        k, _, v = line.partition(":")
        if k.strip().lower() == "content-length":
            try:
                length = int(v.strip())
            except ValueError:
                length = 0
    body = rest
    while len(body) < length and len(body) < 1_000_000:
        chunk = conn.recv(4096)
        if not chunk:
            break
        cap.data("C->S", chunk)
        body += chunk
    return head.decode("latin-1"), body


class _Quiet:
    """Zastepcza 'Capture' na czas czytania zadania -- decyzje, czy logowac, podejmujemy dopiero po pierwszej linii."""

    def data(self, *_a, **_k):
        pass

    def note(self, *_a, **_k):
        pass

    def close(self):
        pass


# Ile razy widzielismy dane zadanie (metoda + sciezka bez zapytania). Test 2026-10-25: gra odpytuje sklep POW
# (GET /pow/store/game/fifa17/catalog/0/item/list) ~3600 razy w kilka minut, bo dostaje pusty JSON -- logowanie kazdego
# zapytania zalewalo log serwera (2 MB) i tworzylo tysiace plikow w logs/captures. Pelny zapis: pierwsze LOG_FIRST
# wystapienia, potem co LOG_EVERY-te jako jedna linia podsumowania.
LOG_FIRST, LOG_EVERY = 3, 500
_SEEN_REQ: dict = {}


def _should_log(request_line: str):
    parts = request_line.split(" ")
    key = (parts[0], parts[1].split("?", 1)[0]) if len(parts) > 1 else (request_line, "")
    n = _SEEN_REQ.get(key, 0) + 1
    _SEEN_REQ[key] = n
    return n <= LOG_FIRST or n % LOG_EVERY == 0, n


def handle(conn: socket.socket, addr, cfg: Config, label: str = "pow") -> None:
    cap = _Quiet()
    try:
        conn.settimeout(10)
        while True:
            head, body = _read_request(conn, cap)
            if head is None:
                break
            lines = head.split("\r\n")
            loud, count = _should_log(lines[0])
            if loud:
                cap = Capture(cfg, label, addr)
                cap.data("C->S", head.encode("latin-1") + b"\r\n\r\n" + body)
                if count > LOG_FIRST:
                    cap.note(f"{label}: to samo zadanie juz {count} razy (logowane tylko co {LOG_EVERY}.)")
            cap.note(f"{label} zadanie: {lines[0]}")
            for h in lines[1:]:
                name, _, val = h.partition(":")
                cap.note(f"  naglowek {name.strip()}: {val.strip()[:300]}")
            if body:
                cap.note(f"  cialo ({len(body)}B): {body[:600]!r}")
            parts = lines[0].split(" ")
            target = parts[1] if len(parts) > 1 else "/"
            if cfg.serve_easw_config and target.startswith(easw.BASE_PATH):
                # SportsWorld (EASW): prawdziwa atrapa, nie sam rejestrator -- patrz easw.py
                hdrs = {h.split(":", 1)[0].strip().lower(): h.split(":", 1)[1].strip()
                        for h in lines[1:] if ":" in h}
                status, out_headers, payload, what = easw.handle_request(
                    parts[0], target, hdrs, body, addr[0] if addr else "")
                conn.sendall(easw.render_http(status, out_headers, payload))
                cap.note(f"{label} odpowiedz: {what}")
                break
            payload = b"{}"
            resp = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(payload)}\r\n".encode()
                    + b"Connection: close\r\n\r\n" + payload)
            conn.sendall(resp)
            cap.note(f"{label} odpowiedz: 200 OK {{}}")
            break
    except OSError as exc:
        cap.note(f"blad polaczenia {label}: {exc}")
    finally:
        cap.close()
