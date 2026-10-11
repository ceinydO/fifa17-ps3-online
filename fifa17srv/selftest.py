"""Self-test: proves the plumbing works using synthetic clients. Does NOT need FIFA 17.

It cannot prove the game behaves the same way -- that is what the real captures are for.
"""
from __future__ import annotations

import json
import logging
import re
import socket
import ssl
import struct
import tempfile
import time
from pathlib import Path

from . import probe, redirector, tdf
from .certs import ensure_certs
from .config import Config
from .server import Server, make_tls_context
from .tls_hello import analyze_client_hello, describe

results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail and not ok else ""))


def _latest(logdir: Path, prefix: str, suffix: str = ".txt") -> str:
    files = sorted(p for p in logdir.glob(f"{prefix}_*{suffix}") if not p.name.endswith("_c2s.bin"))
    return files[-1].read_text(encoding="utf-8") if files else ""


def _any_log(logdir: Path, prefix: str, needle: str) -> bool:
    """True if any capture log with this prefix contains needle (connections finish in any order)."""
    return any(needle in p.read_text(encoding="utf-8")
               for p in logdir.glob(f"{prefix}_*.txt"))


def _client_ctx(ca: Path, legacy: bool = False) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(str(ca))
    if legacy:
        ctx.set_ciphers("ALL:@SECLEVEL=0")
        ctx.minimum_version = ssl.TLSVersion.TLSv1
        ctx.maximum_version = ssl.TLSVersion.TLSv1
    return ctx


def _gamemgr_checks() -> None:
    """GameManager: przebieg create -> mesh -> finalize -> dolaczenie na syntetycznych graczach (bez gniazd)."""
    from . import gamemgr, ids, gmtrace

    def T(raw):                                   # {tag: wartosc} z ramki (naglowek 16 B)
        return dict((t.rstrip(), v) for t, _t, v in tdf.decode(raw[16:]))

    def sub(fields):
        return dict((t.rstrip(), v) for t, _t, v in fields)

    def setups(outs, who):
        return [o for o in outs if o[0] == who and o[2][8:10] == (0x14).to_bytes(2, "big")]

    gamemgr.reset()
    reg = {
        "host": {"identity": ("host", 1, b"h" * 36), "ip": 1, "port": 3659, "peer_ip": 0x0A000001, "maci": 5},
        "guest": {"identity": ("guest", 2, b"g" * 36), "ip": 2, "port": 3659, "peer_ip": 0x0A000002, "maci": 6},
    }
    lookup = reg.get
    req = [("CMGD", tdf.STRUCT, [("GVER", tdf.STRING, "qa-only")]),
           ("GMCD", tdf.STRUCT, [("NTOP", tdf.VARINT, 130), ("PMAX", tdf.VARINT, 2), ("GSET", tdf.VARINT, 1060)]),
           ("GTYP", tdf.STRING, "gameType20")]
    hcong = ids.connection_group_id_for("host")
    gcong = ids.connection_group_id_for("guest")

    # --- poziom 1 (host sam, stary ksztalt): setup tylko dla hosta, host bez CONG; kolega dolacza przez joinGame
    cfg1 = Config(state_dir=tempfile.mkdtemp(prefix="fifa17srv_gm1_"), gm_variant=1)
    gid, _r, outs = gamemgr.create_game(cfg1, "host", req, ["guest"], lookup)
    check("poziom 1: setup dostaje tylko host", {o[0] for o in outs} == {"host"} and len(setups(outs, "host")) == 1)
    st = T(setups(outs, "host")[0][2])
    game1, pros1 = sub(st["GAME"]), st["PROS"][1]
    p0 = sub(pros1[0])
    check("poziom 1: PRE_GAME, w rosterze tylko host (STAT=4) bez CONG, REAS jak FIFA 14 (VALU)",
          game1["GSTA"] == gamemgr.STATE_PRE_GAME and len(pros1) == 1 and p0["STAT"] == 4 and "CONG" not in p0
          and st["REAS"][0] == 0 and st["REAS"][1][0] == "VALU")
    check("poziom 1: THST/PHST bez CONG (stary ksztalt)", "CONG" not in sub(game1["THST"])
          and "CONG" not in sub(game1["PHST"]) and sub(game1["THST"])["HPID"] == ids.uid_for("host"))
    check("poziom 1: follow-upy dla hosta (stan gracza + stan gry)",
          sum(1 for o in outs if o[1].startswith("NotifyGamePlayerStateChange")) == 1
          and sum(1 for o in outs if o[1].startswith("NotifyGameStateChange")) == 1)
    fin = gamemgr.finalize_game(cfg1, "host", [("GID ", tdf.VARINT, gid)], lookup)
    check("poziom 1: finalizeGameCreation nie dosyla kolegi do gry sam z siebie",
          not setups(fin, "guest") and gamemgr.attempt_info(gid)[2] is True)
    jr, jouts = gamemgr.join_game(cfg1, "guest", [("GID ", tdf.VARINT, gid)], lookup)
    gs = setups(jouts, "guest")
    check("joinGame kolegi: dostaje NotifyGameSetup, a host NotifyPlayerJoining", len(gs) == 1
          and any(o[0] == "host" and o[1] == "NotifyPlayerJoining" for o in jouts))
    gst = T(gs[0][2])
    gp = [sub(x) for x in gst["PROS"][1]]
    check("dolaczajacy: roster = host bez CONG + kolega z CONG (rozne klucze punktow koncowych), kolega w CONNECTING",
          len(gp) == 2 and "CONG" not in gp[0] and gp[1]["CONG"] == gcong and gp[1]["STAT"] == 2
          and gst["REAS"][1][0] == "DLSC" and gst["REAS"][1][2] == [("DCTX", tdf.VARINT, 1)])
    mesh = gamemgr.update_mesh_connection(
        cfg1, "guest", [("GID ", tdf.VARINT, gid), ("STAT", tdf.VARINT, 2),
                        ("TCG ", tdf.OBJID, ids.connection_group_objid_for("host"))], lookup)
    check("mesh kolegi konczy dolaczanie gracza", any(o[1].startswith("NotifyPlayerJoinCompleted guest") for o in mesh))

    # --- poziom 2: host + zarezerwowany kolega (STAT=0) z PLJD; kolega nie dostaje nic do joinGame
    gamemgr.reset()
    cfgR = Config(state_dir=tempfile.mkdtemp(prefix="fifa17srv_gmR_"), gm_variant=2)
    req_r = req + [("PLJD", tdf.STRUCT, [("BTPL", tdf.OBJID, (30722, 2, 1)), ("DFRL", tdf.STRING, ""),
                   ("GENT", tdf.VARINT, 0),
                   ("PLDL", tdf.LIST, (tdf.STRUCT, [[("IREP", tdf.VARINT, 0), ("RLNM", tdf.STRING, ""),
                                                     ("USID", tdf.STRUCT, [("NAME", tdf.STRING, "guest")])]])),
                   ("SLOT", tdf.VARINT, 1)])]
    gidR, _r, outsR = gamemgr.create_game(cfgR, "host", tdf.decode(tdf.encode(req_r)), ["guest", "zbednik"], lookup)
    stR = T(setups(outsR, "host")[0][2])
    prR = [sub(x) for x in stR["PROS"][1]]
    check("poziom 2: w rosterze hosta jest zarezerwowany kolega (STAT=0, z CONG), nikt inny, kolega bez setupu",
          len(prR) == 2 and prR[0]["STAT"] == 4 and prR[1]["STAT"] == 0 and prR[1]["NAME"] == "guest"
          and prR[1]["CONG"] == gcong and "CONG" not in prR[0] and not setups(outsR, "guest")
          and {o[0] for o in outsR} == {"host"})
    jrR, joR = gamemgr.join_game(cfgR, "guest", [("GID ", tdf.VARINT, gidR)], lookup)
    check("poziom 2: joinGame zarezerwowanego -> host dostaje zmiane stanu kolegi (nie PlayerJoining), kolega setup",
          len(setups(joR, "guest")) == 1
          and any(o[0] == "host" and o[1].startswith("NotifyGamePlayerStateChange guest=2") for o in joR)
          and not any(o[1] == "NotifyPlayerJoining" for o in joR))

    # --- poziom 3: INITIALIZING + spojny CONG hosta (roster == THST == PHST), bez follow-upow
    gamemgr.reset()
    cfg2 = Config(state_dir=tempfile.mkdtemp(prefix="fifa17srv_gm2_"), gm_variant=3)
    gid2, _r, outs2 = gamemgr.create_game(cfg2, "host", req, ["guest"], lookup)
    st2 = T(setups(outs2, "host")[0][2])
    g2, p2 = sub(st2["GAME"]), sub(st2["PROS"][1][0])
    check("poziom 3: INITIALIZING, CONG hosta jednakowy w rosterze, THST i PHST (= klucz punktu koncowego hosta)",
          g2["GSTA"] == gamemgr.STATE_INITIALIZING and p2["CONG"] == hcong == sub(g2["THST"])["CONG"]
          == sub(g2["PHST"])["CONG"] and sub(g2["THST"])["HPID"] == ids.uid_for("host") and p2["STAT"] == 4)
    check("poziom 3: bez follow-upow", [o[1] for o in outs2 if not o[1].startswith("NotifyGameSetup")] == [])
    fin2 = gamemgr.finalize_game(cfg2, "host", [("GID ", tdf.VARINT, gid2)], lookup)
    check("poziom 3: po finalize gra przechodzi INITIALIZING -> PRE_GAME",
          any(o[1] == "NotifyGameStateChange PRE_GAME" for o in fin2))

    # --- poziom 4: host w setupie CONNECTING, po finalize serwer oglasza go jako CONNECTED
    gamemgr.reset()
    cfg3 = Config(state_dir=tempfile.mkdtemp(prefix="fifa17srv_gm3_"), gm_variant=4)
    gid3, _r, outs3 = gamemgr.create_game(cfg3, "host", req, ["guest"], lookup)
    check("poziom 4: host w setupie ma STAT=2", sub(T(setups(outs3, "host")[0][2])["PROS"][1][0])["STAT"] == 2)
    fin3 = gamemgr.finalize_game(cfg3, "host", [("GID ", tdf.VARINT, gid3)], lookup)
    check("poziom 4: finalize oglasza hosta jako CONNECTED i JoinCompleted",
          any(o[1].startswith("NotifyGamePlayerStateChange CONNECTED host") for o in fin3)
          and any(o[1].startswith("NotifyPlayerJoinCompleted host") for o in fin3))

    # --- poziom 5 (docelowy): nowe pola, REAS DLSC/CREATE dla hosta
    gamemgr.reset()
    cfg4 = Config(state_dir=tempfile.mkdtemp(prefix="fifa17srv_gm4_"), gm_variant=5)
    gid4, _r, outs4 = gamemgr.create_game(cfg4, "host", req, ["guest"], lookup)
    st4 = T(setups(outs4, "host")[0][2])
    p4 = sub(st4["PROS"][1][0])
    check("poziom 5: REAS = DLSC{DCTX=0}, roster z nowymi polami (CONG, TIME, UUID...)",
          st4["REAS"][0] == 0 and st4["REAS"][1][0] == "DLSC" and st4["REAS"][1][2] == [("DCTX", tdf.VARINT, 0)]
          and {"CONG", "TIME", "UUID", "LOC", "NASP"} <= set(p4) and p4["CONG"] == hcong)
    j4 = gamemgr.finalize_game(cfg4, "host", [("GID ", tdf.VARINT, gid4)], lookup)
    jr4, jo4 = gamemgr.join_game(cfg4, "guest", [("GID ", tdf.VARINT, gid4)], lookup)
    gs4 = T(setups(jo4, "guest")[0][2])
    check("poziom 5: dolaczajacy dostaje DLSC/JOIN i unikalny CONG",
          gs4["REAS"][1][0] == "DLSC" and sub(gs4["PROS"][1][1])["CONG"] == gcong != hcong)

    # --- drabinka (auto): start od 1; po sukcesie wyzej; po pierwszej porazce raz cel; potem najlepszy dzialajacy
    gamemgr.reset()
    cfgA = Config(state_dir=tempfile.mkdtemp(prefix="fifa17srv_gmauto_"))
    a1, *_ = gamemgr.create_game(cfgA, "host", req, ["guest"], lookup)
    check("auto: pierwsza proba = poziom 1", gamemgr.attempt_info(a1)[:2] == (1, "1-host-sam-stary"))
    gamemgr.finalize_game(cfgA, "host", [("GID ", tdf.VARINT, a1)], lookup)
    a2, *_ = gamemgr.create_game(cfgA, "host", req, ["guest"], lookup)
    check("auto: po sukcesie poziomu 1 -> poziom 2", gamemgr.attempt_info(a2)[0] == 2)
    wd = gamemgr.watchdog_expired(cfgA, a2)
    check("watchdog: brak finalize usuwa gre i wysyla NotifyGameRemoved do graczy",
          {o[0] for o in wd} == {"host"} and gamemgr.attempt_info(a2) is None)
    a3, *_ = gamemgr.create_game(cfgA, "host", req, ["guest"], lookup)
    check("auto: po pierwszej porazce (poziom 2) sprawdza raz od razu docelowy poziom 5",
          gamemgr.attempt_info(a3)[0] == gamemgr.MAX_LEVEL == 5)
    gamemgr.watchdog_expired(cfgA, a3)
    a4, *_ = gamemgr.create_game(cfgA, "host", req, ["guest"], lookup)
    check("auto: po porazce celu wraca do najnowszego dzialajacego (1)", gamemgr.attempt_info(a4)[0] == 1)

    # --- sondy + dziennik prob
    trace_path = Path(tempfile.mkdtemp(prefix="fifa17srv_gmtrace_")) / "gm_attempts.log"
    gmtrace.configure(trace_path)
    gmtrace.begin("host", a4, 1, "test", "powod", "opis", req)
    pr = [gamemgr.probe_frames(cfgA, a4, n, lookup) for n in (1, 2, 3)]
    check("sondy 1-3 daja powiadomienia dla hosta (PlatformHostInitialized; stan+JoinCompleted; GameStateChange)",
          [len(x) for x in pr] == [1, 2, 1] and all(o[0] == "host" for x in pr for o in x))
    gmtrace.server("host", pr[0][0][1], pr[0][0][2])
    gmtrace.client("host", 4, 0x1D, 7, 0, [("GID ", tdf.VARINT, a4)])
    gmtrace.outcome(a4, "test")
    text = trace_path.read_text(encoding="utf-8")
    check("dziennik prob zawiera rozkodowane powiadomienie, zadanie klienta i zestawienie",
          "SONDA 1" in text and "komp=0x0004 cmd=0x001D" in text and "0x0004/0x001D x1" in text, text[-300:])
    gmtrace._UNTIL = 0.0                                     # okno 45 s minelo
    gmtrace.client("guest", 4, 0x09, 8, 0, [("GID ", tdf.VARINT, a4)])
    gmtrace.client("guest", 0x7802, 0x32, 9, 0, [])
    text = trace_path.read_text(encoding="utf-8")
    check("po oknie 45 s nadal logowany jest joinGame (GameManager), a inne zadania nie",
          "komp=0x0004 cmd=0x0009" in text and "komp=0x7802 cmd=0x0032" not in text)
    gamemgr.update_mesh_connection(cfgA, "host", [("GID ", tdf.VARINT, a4), ("STAT", tdf.VARINT, 2)], lookup)
    check("po updateMeshConnection hosta sondy sie wylaczaja", gamemgr.host_reacted(a4)
          and gamemgr.probe_frames(cfgA, a4, 1, lookup) == [])
    gamemgr.reset()

    # --- nowy createGame usuwa stara gre hosta (NotifyGameRemoved przed nowym setupem)
    cfgS = Config(state_dir=tempfile.mkdtemp(prefix="fifa17srv_gmstale_"), gm_variant=1)
    s1, *_ = gamemgr.create_game(cfgS, "host", req, ["guest"], lookup)
    gamemgr.join_game(cfgS, "guest", [("GID ", tdf.VARINT, s1)], lookup)
    s2, _r, os2 = gamemgr.create_game(cfgS, "host", req, ["guest"], lookup)
    removed = [o for o in os2 if o[1].startswith("NotifyGameRemoved")]
    first_setup = next(i for i, o in enumerate(os2) if o[1].startswith("NotifyGameSetup"))
    check("nowy createGame usuwa stara gre hosta: NotifyGameRemoved dla kazdego czlonka, przed setupem nowej",
          {o[0] for o in removed} == {"host", "guest"}
          and all(i < first_setup for i, o in enumerate(os2) if o[1].startswith("NotifyGameRemoved"))
          and gamemgr.attempt_info(s1) is None and gamemgr.attempt_info(s2) is not None)
    gamemgr.reset()

    # --- rozlaczenie klienta hosta w trakcie proby (crash/zamkniecie) NIE jest porazka ksztaltu: ten sam poziom wraca
    cfgC = Config(state_dir=tempfile.mkdtemp(prefix="fifa17srv_gmcrash_"))
    c1, *_ = gamemgr.create_game(cfgC, "host", req, ["guest"], lookup)
    gamemgr.finalize_game(cfgC, "host", [("GID ", tdf.VARINT, c1)], lookup)         # poziom 1 OK
    c2, *_ = gamemgr.create_game(cfgC, "host", req, ["guest"], lookup)               # poziom 2
    check("po sukcesie poziomu 1 jest poziom 2", gamemgr.attempt_info(c2)[0] == 2)
    gamemgr.watchdog_expired(cfgC, c2)                                               # 14 s ciszy -> "fail"
    gamemgr.on_disconnect(cfgC, "host", lookup)                                      # ... a potem okno hosta zamkniete
    c3, *_ = gamemgr.create_game(cfgC, "host", req, ["guest"], lookup)
    check("rozlaczenie hosta po werdykcie watchdoga to crash, nie porazka: poziom 2 jest powtorzony",
          gamemgr.attempt_info(c3)[0] == 2 and "rozlaczeniem" in gamemgr.GAMES[c3]["variant_why"],
          gamemgr.GAMES[c3]["variant_why"])
    gamemgr.on_disconnect(cfgC, "host", lookup)                                      # drugi crash tego samego poziomu
    c4, *_ = gamemgr.create_game(cfgC, "host", req, ["guest"], lookup)
    check("po drugim rozlaczeniu na tym samym poziomie uznajemy go za nieudany (i sprawdzamy cel)",
          gamemgr.attempt_info(c4)[0] == gamemgr.MAX_LEVEL and 2 in json.loads(cfgC.gm_variant_path.read_text())["bad"],
          gamemgr.GAMES[c4]["variant_why"])
    # plik stanu z innej wersji formatu jest ignorowany
    cfgC.gm_variant_path.write_text('{"level": 2, "result": "fail", "good": [1], "bad": []}', encoding="utf-8")
    c5, *_ = gamemgr.create_game(cfgC, "host", req, ["guest"], lookup)
    check("stary plik stanu drabinki (bez wersji) jest ignorowany -- start od poziomu 1",
          gamemgr.attempt_info(c5)[0] == 1)
    gamemgr.reset()


def run_selftest() -> int:
    logging.getLogger("fifa17srv").setLevel(logging.WARNING)
    tmp = Path(tempfile.mkdtemp(prefix="fifa17srv_test_"))
    cfg = Config(cert_dir=str(tmp / "certs"), log_dir=str(tmp / "logs"), idle_timeout=2.0)
    paths = ensure_certs(cfg)
    ctx = make_tls_context(cfg)
    check("certificates generated", all(p.exists() for p in paths.values()))

    psrv = Server("probe", "127.0.0.1", 0, lambda c, a: probe.handle(c, a, cfg, ctx)).start()
    cfg.blaze_port = psrv.port
    rsrv = Server("redir", "127.0.0.1", 0, lambda c, a: redirector.handle(c, a, cfg, ctx)).start()
    logs = cfg.log_dir_path

    # 1. HTTPS redirector request, verified against our CA
    body = b'<?xml version="1.0"?><serverinstancerequest><name>fifa-2017-pc</name></serverinstancerequest>'
    req = (b"POST /redirector/getServerInstance HTTP/1.1\r\nHost: " + cfg.redirector_host.encode()
           + b"\r\nContent-Type: application/xml\r\nContent-Length: " + str(len(body)).encode()
           + b"\r\n\r\n" + body)
    with socket.create_connection(("127.0.0.1", rsrv.port), timeout=5) as raw:
        with _client_ctx(paths["ca"]).wrap_socket(raw, server_hostname=cfg.redirector_host) as tls:
            tls.sendall(req)
            resp = b""
            while True:
                chunk = tls.recv(4096)
                if not chunk:
                    break
                resp += chunk
    text = resp.decode()
    check("redirector answers over TLS", text.startswith("HTTP/1.1 200"), text[:80])
    check("redirector points at probe port", f"<port>{psrv.port}</port>" in text, text)
    check("redirector encodes 127.0.0.1 as integer", "<ip>2130706433</ip>" in text)
    check("redirector honours secure=0", "<secure>0</secure>" in text)
    time.sleep(0.3)
    rlog = _latest(logs, "redirector")
    check("redirector capture has ClientHello analysis", "ClientHello" in rlog and "cipher suites" in rlog, rlog[:300])
    check("redirector capture has request body", "serverinstancerequest" in rlog)

    # 2. plaintext client on the main port (what we hope the game does with secure=0)
    payload = bytes.fromhex("00000042") + b"\x00\x09PreAuth-test" + bytes(range(32))
    with socket.create_connection(("127.0.0.1", psrv.port), timeout=5) as s:
        s.sendall(payload)
        time.sleep(0.3)
    time.sleep(0.5)
    bins = sorted(logs.glob("blaze_*_c2s.bin"))
    plain_ok = any(b.read_bytes() == payload for b in bins)
    check("probe captures plaintext bytes exactly", plain_ok)
    check("probe detects plaintext", _any_log(logs, "blaze", "plaintext connection"))

    # 3. TLS client on the main port (secure=1 case)
    payload2 = b"\x00\x00\x00\x10" + b"tls-side-payload"
    with socket.create_connection(("127.0.0.1", psrv.port), timeout=5) as raw:
        with _client_ctx(paths["ca"]).wrap_socket(raw, server_hostname=cfg.redirector_host) as tls:
            tls.sendall(payload2)
            time.sleep(0.3)
    time.sleep(0.5)
    check("probe captures bytes through TLS", any(b.read_bytes() == payload2 for b in logs.glob("blaze_*_c2s.bin")))
    check("probe logs TLS handshake OK", _any_log(logs, "blaze", "TLS handshake OK"))

    # 4. legacy TLS 1.0 client (skipped if this OpenSSL cannot do it)
    try:
        payload3 = b"legacy-tls10-client"
        with socket.create_connection(("127.0.0.1", psrv.port), timeout=5) as raw:
            with _client_ctx(paths["ca"], legacy=True).wrap_socket(raw, server_hostname=cfg.redirector_host) as tls:
                ver = tls.version()
                tls.sendall(payload3)
                time.sleep(0.3)
        time.sleep(0.5)
        check(f"legacy {ver} handshake accepted", any(b.read_bytes() == payload3 for b in logs.glob("blaze_*_c2s.bin")))
    except (ssl.SSLError, ValueError, OSError) as exc:
        print(f"[SKIP] legacy TLS 1.0 client not possible on this system ({exc})")

    # 5. a client that rejects our cert: server must log the failure, not crash
    with socket.create_connection(("127.0.0.1", psrv.port), timeout=5) as raw:
        strict = ssl.create_default_context()  # does not trust our CA
        try:
            strict.wrap_socket(raw, server_hostname=cfg.redirector_host)
        except ssl.SSLError:
            pass
    time.sleep(0.5)
    check("failed handshake is logged", _any_log(logs, "blaze", "TLS handshake FAILED"))
    check("failed handshake gets a diagnostic hint", _any_log(logs, "blaze", "HINT: client rejected our certificate"))
    with socket.create_connection(("127.0.0.1", psrv.port), timeout=5) as s:
        s.sendall(b"still alive")
        time.sleep(0.3)
    time.sleep(0.4)
    check("server survives bad client", any(b.read_bytes() == b"still alive" for b in logs.glob("blaze_*_c2s.bin")))

    check("client alert is decoded in the log", _any_log(logs, "blaze", "Alert(fatal, unknown_ca)"))

    # 5b. a client that vanishes (TCP reset) right after our certificate, like a client that
    #     does not trust the CA. The log must say so explicitly.
    hello_ctx = ssl.create_default_context()
    hello_ctx.check_hostname = False
    hello_ctx.verify_mode = ssl.CERT_NONE
    hello_ctx.maximum_version = ssl.TLSVersion.TLSv1_2   # like the game: readable server flight
    inc, out = ssl.MemoryBIO(), ssl.MemoryBIO()
    obj = hello_ctx.wrap_bio(inc, out, server_hostname=cfg.redirector_host)
    try:
        obj.do_handshake()
    except ssl.SSLWantReadError:
        pass
    raw_hello = out.read()
    s = socket.create_connection(("127.0.0.1", psrv.port), timeout=5)
    s.sendall(raw_hello)
    s.settimeout(3)
    got = b""
    try:
        while b"\x0e\x00\x00\x00" not in got:      # ServerHelloDone
            piece = s.recv(65536)
            if not piece:
                break
            got += piece
    except socket.timeout:
        pass
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    s.close()                                          # sends RST
    time.sleep(0.6)
    check("server flight seen by client", len(got) > 500)
    check("abrupt reset after certificate is diagnosed",
          _any_log(logs, "blaze", "dropped the connection right after our ServerHelloDone"))
    check("server flight is logged", _any_log(logs, "blaze", "S->C [handshake]") and _any_log(logs, "blaze", "ServerHelloDone"))

    # 5c. SHA-1 signed certificates (built by hand) must also work end to end
    cfg_sha1 = Config(cert_dir=str(tmp / "certs_sha1"), log_dir=str(tmp / "logs_sha1"), cert_sig_hash="sha1")
    paths_sha1 = ensure_certs(cfg_sha1)
    ctx_sha1 = make_tls_context(cfg_sha1)
    ssrv = Server("probe-sha1", "127.0.0.1", 0, lambda c, a: probe.handle(c, a, cfg_sha1, ctx_sha1)).start()
    cctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    cctx.set_ciphers("ALL:@SECLEVEL=0")
    cctx.load_verify_locations(str(paths_sha1["ca"]))
    try:
        with socket.create_connection(("127.0.0.1", ssrv.port), timeout=5) as raw:
            with cctx.wrap_socket(raw, server_hostname=cfg_sha1.redirector_host) as tls:
                tls.sendall(b"sha1-cert-client")
                time.sleep(0.3)
        time.sleep(0.5)
        check("SHA-1 signed certificate is accepted by a verifying client",
              any(b.read_bytes() == b"sha1-cert-client" for b in cfg_sha1.log_dir_path.glob("blaze_*_c2s.bin")))
    except ssl.SSLError as exc:
        check("SHA-1 signed certificate is accepted by a verifying client", False, str(exc))
    ssrv.stop()

    # 6. ClientHello parser on a real hello produced by Python's ssl
    a, b = socket.socketpair()
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    inc, out = ssl.MemoryBIO(), ssl.MemoryBIO()
    obj = c.wrap_bio(inc, out, server_hostname="example.test")
    try:
        obj.do_handshake()
    except ssl.SSLWantReadError:
        pass
    hello = out.read()
    info = analyze_client_hello(hello)
    check("ClientHello parser reads a real hello", info is not None and len(info.cipher_suites) > 3
          and info.sni == "example.test", describe(info))
    a.close(); b.close()

    # 7. TDF codec round trip
    sample = [
        ("NAME", tdf.STRING, "hello"),
        ("NUM", tdf.VARINT, 300000),
        ("ZERO", tdf.VARINT, 0),
        ("BLOB", tdf.BLOB, b"\x01\x02\x03"),
        ("SUB", tdf.STRUCT, [("A1", tdf.VARINT, 5), ("B2", tdf.STRING, "x")]),
        ("LST", tdf.LIST, (tdf.STRING, ["a", "bb"])),
        ("SLST", tdf.LIST, (tdf.STRUCT, [[("Q", tdf.VARINT, 1)], [("Q", tdf.VARINT, 2)]])),
        ("MAP", tdf.MAP, (tdf.STRING, tdf.VARINT, [("k", 1), ("j", 2)])),
        ("UNI", tdf.UNION, (2, ("INNR", tdf.VARINT, 9))),
        ("UNS", tdf.UNION, (tdf.UNION_UNSET, None)),
        ("ILST", tdf.INTLIST, [1, 2, 1000]),
        ("OTYP", tdf.OBJTYPE, (30722, 1)),
        ("OID", tdf.OBJID, (1, 2, 3)),
        ("FLT", tdf.FLOAT, 1.5),
    ]
    encoded = tdf.encode(sample)
    decoded = tdf.decode(encoded)
    check("TDF encode/decode round trip", decoded == sample, str(decoded)[:200])
    check("TDF varint edge cases", all(
        tdf.Reader(tdf.enc_varint(v)).varint() == v for v in (0, 1, 63, 64, 127, 128, 8191, 8192, 2**32, 2**63)))
    check("TDF tag round trip", all(tdf.decode_tag(tdf.encode_tag(t)) == t for t in ("NAME", "ZZZZ", "AB", "X1", "1234")))
    fields, reached, err = tdf.decode_partial(b"\xff\xff\xff\xff")
    check("TDF decoder rejects garbage without crashing", err is not None and fields == [])

    _gamemgr_checks()

    psrv.stop()
    rsrv.stop()
    passed, total = sum(results), len(results)
    print(f"\n{passed}/{total} checks passed  (temp dir: {tmp})")
    return 0 if passed == total else 1
