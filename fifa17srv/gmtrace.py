"""Dziennik prob GameManager (logs/gm_attempts.log) -- jeden plik, ktory wystarczy wyslac do analizy.

Dla kazdego createGame zapisuje: poziom drabinki i powod wyboru, pelne zadanie createGame, KAZDE powiadomienie GameManager
wyslane przez serwer (rozkodowane TDF, nie tylko rozmiar), KAZDE zadanie klientow w oknie ~45 s po createGame (z czasem
wzgledem createGame w ms) oraz wynik (finalizeGameCreation OK / watchdog / sondy). Dzieki temu z jednego testu widac, na co
klient hosta zareagowal i kiedy -- bez przegladania calych logow polaczen.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from . import tdf

WINDOW_SECONDS = 45.0

_LOCK = threading.Lock()
_PATH: Path | None = None
_T0 = 0.0
_UNTIL = 0.0
_GID = 0
_SEEN: dict = {}


def configure(path) -> None:
    global _PATH
    _PATH = Path(path)
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def _write(text: str) -> None:
    if _PATH is None:
        return
    try:
        with open(_PATH, "a", encoding="utf-8") as f:
            f.write(text.rstrip("\n") + "\n")
    except OSError:
        pass


def _stamp() -> str:
    now = time.time()
    return f"{time.strftime('%H:%M:%S', time.localtime(now))}.{int((now % 1) * 1000):03d} +{time.monotonic() - _T0:6.3f}s"


def _indent(text: str, pad: str = "      ") -> str:
    return "\n".join(pad + line for line in text.split("\n"))


def begin(host: str, gid: int, level, name: str, why: str, about: str, req_fields) -> None:
    global _T0, _UNTIL, _GID, _SEEN
    with _LOCK:
        _T0 = time.monotonic()
        _UNTIL = _T0 + WINDOW_SECONDS
        _GID = gid
        _SEEN = {}
        _write("\n" + "=" * 100)
        _write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  createGame GID={gid} od {host!r}")
        _write(f"POZIOM {level} ({name}): {about}")
        _write(f"powod wyboru: {why}")
        try:
            _write("zadanie createGame:\n" + _indent(tdf.pretty(req_fields)))
        except Exception as exc:                      # diagnostyka nie moze zepsuc obslugi
            _write(f"(nie udalo sie rozkodowac zadania: {exc})")


def server(target: str, label: str, frame: bytes) -> None:
    """Powiadomienie/odpowiedz wyslana przez serwer (ramka z naglowkiem 16 B)."""
    with _LOCK:
        if time.monotonic() > _UNTIL:
            return
        try:
            body = tdf.pretty(tdf.decode(frame[16:]))
        except Exception as exc:
            body = f"(nie udalo sie rozkodowac: {exc})"
        _write(f"{_stamp()}  S->C {target!r}: {label} [{len(frame) - 16}B]\n{_indent(body)}")


def client(who: str, component: int, command: int, msg_num: int, msg_type: int, fields) -> None:
    """Zadanie klienta (kazdy gracz) w oknie po createGame; pingi ramkowe pomijane."""
    if msg_type == 4:
        return
    with _LOCK:
        if time.monotonic() > _UNTIL:
            return
        key = (who, component, command)
        _SEEN[key] = _SEEN.get(key, 0) + 1
        try:
            body = tdf.pretty(fields) if fields else ""
        except Exception:
            body = ""
        text = f"{_stamp()}  C->S {who!r}: komp=0x{component:04X} cmd=0x{command:04X} msg={msg_num}"
        _write(text + ("\n" + _indent(body) if body else ""))


def note(text: str) -> None:
    with _LOCK:
        if _T0:
            _write(f"{_stamp()}  {text}")
        else:
            _write(text)


def outcome(gid: int, text: str) -> None:
    """Wynik proby + zestawienie, jakie zadania (komponent/komenda) zobaczylismy od kogo."""
    with _LOCK:
        if gid != _GID:
            return
        lines = [f"{_stamp()}  WYNIK GID={gid}: {text}"]
        if _SEEN:
            lines.append("      zadania klientow od createGame (kto, komponent/komenda: ile razy):")
            for (who, comp, cmd), n in sorted(_SEEN.items()):
                lines.append(f"        {who!r}: 0x{comp:04X}/0x{cmd:04X} x{n}")
        else:
            lines.append("      od createGame klienci nie wyslali ZADNEGO zadania")
        _write("\n".join(lines))
