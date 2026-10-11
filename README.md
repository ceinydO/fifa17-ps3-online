# fifa17-friendlies

A from-scratch, MIT-licensed **server emulator for FIFA 17's EA Blaze online servers**, which EA
shut down. The concrete target being tested is the **PS3 build of FIFA 17, run under the RPCS3
emulator**, but nothing here is PS3-only by design (redirector/Blaze/QoS/Nucleus/telemetry are all
generic Blaze SDK 15.1.x infrastructure).

Not affiliated with, endorsed by, or connected to Electronic Arts. This is a preservation /
reverse-engineering project: **you need your own legitimate copy of FIFA 17**. This project does
**not** bypass DRM, Denuvo, or ownership/game-copy verification, does **not** help obtain pirated
game or firmware copies, and must not contain or distribute any EA files, assets, keys, or the
game itself.

> **Read this file before starting a new session on this project.** It is written to be the single
> source of truth for what is confirmed, what is a guess, and what is still broken — so a fresh
> session (human or Claude) does not have to re-derive it from scratch.

## Quick orientation (2026-10-12)

**Goal:** two real players (two PCs, RPCS3, Radmin VPN) play an *Online Friendlies* match: the host invites a
friend, the friend sees the invitation, accepts, both end up in the same Blaze game and exchange P2P traffic.

**Where we are:**
- Login, persona lookup, entitlements, messaging, stats and the whole host-side `createGame` handshake work
  (`createGame` -> `NotifyGameSetup` -> `updateMeshConnection` -> `finalizeGameCreation` all complete, no crash, on
  all five "host-only" setup levels, see below).
- **The invitation is still never sent.** The host's game does not call `sceNpBasicSendMessageGui` (the NP call that
  delivers the invitation to the friend), so the friend sees nothing. A trace patch for RPCS3 (below) showed that the
  game finishes `createGame` internally ("GSMR" success callback) but never raises `EVENT_CREATEGAME_SUCCESS`, which
  the UI needs before it asks the invitation module to send anything.
- Round 2 of the trace patch has been run with both players online (2026-10-12): see "Latest session: 2026-10-12"
  right below. Short version: the game calls `createGame` from **some other code path than the CreateGame UI adaptor**,
  so the listener that would post `EVENT_CREATEGAME_SUCCESS` is never involved. Next step: **round 3** (below) tells
  who calls `createGame` and which listener receives the result, and includes an optional experiment that forces the
  missing UI callback.

**Useful commands:** server `powershell -ExecutionPolicy Bypass -File .\update_and_run.ps1` (host only);
logs for analysis `powershell -ExecutionPolicy Bypass -File .\collect_logs.ps1` (zip) + `RPCS3.log` of both players;
selftest `python -m fifa17srv selftest`.

## Latest session: 2026-10-12 (trace patch round 2 with both players online)

**Setup.** Host (`odyniec`, `C:\rcps3`) and friend (`odyniec1`) both ran RPCS3 with the round-2 patch (396 patch words
applied on both), both logged in to RPCN, both online for the whole ~5.5 min. The host pressed "invite" six times.
Server log: six `createGame` requests (GID 1..6), each answered at level 5 of the host-only ladder and completed by
`updateMeshConnection` + `finalizeGameCreation` (the watchdog confirms), no errors.

**RPCN is fine.** Both RPCS3 instances are `PSN status: RPCN` and logged in; they are RPCN friends of each other (host log:
`basic_event: event:0/9/1 from odyniec1`, friend log: `event:9/1 from odyniec`). Presence travels in both directions
(host 55 bytes at login and 64 bytes at ~300 s, friend 64 bytes at ~206 s). The friend's game polls
`sceNpBasicGetFriendPresenceByIndex(0)` every 10 s. The friend received **no** `INCOMING_INVITATION` (5),
`MESSAGE` (2) or `SEND_INVITATION_RESULT` (13): the host never called `sceNpBasicSendMessage*`, so the problem is
entirely on the host side, not in RPCN or on the friend's PC.

**Host-side chain, identical on all six attempts** (decoded with `tools/decode_trace.py`):
`OSDK SCRN/PUEN` (busy popup) -> `0x51 GameSessionManager::createGame` (r3 = manager `0x10F65E90`, r4 = params on the
FEThread stack `0xD012D6A0`, r5 = **listener `0x1106B288`**) -> `0x52` op ctor -> `0x5B` NotifyGameSetup handler ->
`0x5C` onNetworkCreated -> `0x5D` finalizeGameCreation sent -> `0x5E` response -> `0x20` GSMR success callback ->
`0x54` GameSession ctor -> `0x55` setCurrentSession -> `OSDK GSMR/SUCC` -> `EVENT_NETWORK_END` -> `SCRN/PUEX` (the popup
closes ~0.1 s after the request) -> SportsWorld loads (`LoadSportsWorldUserPresence`, `LoadSportsWorldUsers`).
On the Blaze side the only follow-ups are `lookupUsersByPersonaNames(['odyniec1'])` and
`Stats::getStatsByGroupAsync` for the group `MyFriendlies`.

**Never entered, not even once in six attempts:**
- `0x53` = the CreateGame UI adaptor `0x1624180` (so `createGame` is **not** called from it; the only function that
  posts `EVENT_CREATEGAME_SUCCESS` is the adaptor's listener `0x1624e1c`, by static xref the only one),
- `0x59`/`0x5A` (that listener's success/failure), `0x56`/`0x57`/`0x58` (SetPlayerAttribute path),
- the whole match-up FSM (`0x5F`, `0x60`, `0x62`, `0x63`, `0x64`) - its tick `0x4d0aa4` runs but never gets a relevant event,
- every invitation function (`0x10`-`0x16`, `0x18`).

**Conclusion.** `createGame` reaches the success callback and calls `listener->vt[0]`, but the listener in `r5`
(`0x1106B288`) is not the CreateGame adaptor's, so nothing posts `EVENT_CREATEGAME_SUCCESS`, the UI script callback
`OnOnlineCreateSession` (called by the FSM `0x4d0aa4` on that event) never runs, and the UI never reaches the point where
it asks the invitation module (`SendGameInvite` -> `sceNpBasicSendMessageGui`) to send the invitation. The server
cannot influence this: the success branch (`op+0x2c` list empty) does not depend on any Blaze payload. Whatever creates
the game is some other caller that we have not identified yet (the `Handle_<name>` script-call trace and the caller
addresses of round 3 answer this).

**Server-side facts worth keeping.** The `createGame` request carries **no invitee**: `PLDL` contains only the host,
`GTYP 'gameType20'`, `OSDK_gameMode 20`, `PMAX 2`, `PMIN 1`, `PRES 1`, `GSET 1060`, `fifaMatchupHash`, `OSDK_coop 1`.
The server's "invited friend" is its own bookkeeping (the other logged-in player), not data from the request.

**Round 3 of the trace patch (generated, selftested, not yet run on the PCs).** See "Trace patch" below. New in round 3:
every marker also logs the **caller's LR**; a mid-function marker `0x70` in the createGame success callback logs the
listener, the session and the **code address of the listener's `vt[0]`**; the two script-call functions `0x38aca4` /
`0x38b110` (`Handle_<name>`, called with names like `ShowBusyIndicator`, `GameInviteValidation`, `GameInviteJoin`,
`OnOnlineCreateSession`, ...) log the script function and its first two arguments; plus the invite receive path on the
friend's PC (`EVENT_GAME_INVITE_RECEIVED`, USER_INVITE listeners), other GameSessionManager entry points and other
createGame listeners. The patch file has a second, optional entry, **"FIFA eksperyment - wymus OnOnlineCreateSession
po createGame"**: right after the listener returns it calls the UI script exactly like the FSM would
(`OnOnlineCreateSession("EVENT_CREATEGAME_SUCCESS", <game id>)`). If the UI is waiting for that callback, the invitation
chain (and RPCS3's friend-selection dialog for `sceNpBasicSendMessageGui`) should start.

## Latest session: 2026-10-11, late evening (trace patch, round 1 worked in real RPCS3)

**What the host's `RPCS3.log` (with the round-1 trace patch) showed.** The patch was applied (`PAT: Applied patch ...
FIFA trace ... (<- 270)`), the game booted normally and every marker line was readable
(`python tools/decode_trace.py RPCS3.log --elf EBOOT.ELF`). The friend was online for the first `createGame` and
had left by the second one.
- Module `ginv` (the invitation module, hook `0x302184`) **is registered right after login**, so the invitation path
  exists at runtime.
- Each `createGame` runs the game-side result callback `0x2dee08` ("GSMR", `r4 = 0` = success) ~25 ms after the request
  started, i.e. right after the Blaze `finalizeGameCreation` round trip.
- **After that nothing happens.** No `EVENT_CREATEGAME_SUCCESS` (posted by the UI listener `0x1624e1c`), no
  `EVENT_GAME_SESSION_CHANGED`, no `EVENT_CREATEGAME_FAILURE`, and **none** of the invitation functions
  (`0x16491b4`/`0x1649264` adaptors, `0x301ad4`, `0x3019a4`, `0x2cd938`, `0x30262c`, `0x302454`) is ever entered.
  Only 22 `EVENT_*` posts occurred in the whole 135 s session (`EVENT_NETWORK_START/END`, `LOGIN_*`, `UNSUSPEND*`,
  `BOOT_LOGIN_SUCCESS`), so the UI event system is lightly used.
- SportsWorld (EASW web service) operations fire constantly (`ConnectToSportsWorld`, `LoadSportsWorldUserPresence`,
  `LoadSportsWorldUsers`, `SendFriendsToSportsWorld`), each ending in the `ServerError ... disconnect` telemetry event
  (the server does not provide `OSDK_EASW_*_URL`). Whether this blocks the invitation is **unknown**.

**Reading of the createGame completion path (static, decompiled).** `0x2dee08` (op class at `0x28e208`, created by
`GameSessionManager::createGame` `0x28efa4`, listener = the UI adaptor sub-object `this+0xc` set in `0x1624180`):
on success it builds the `GameSession` (`0x2891e4`), calls `gses::setCurrentSession` (`0x2911b8`), then
- if the op's pending player-attribute list (`op+0x2c`, 32-byte entries) is **empty**: logs `GSMR/SUCC` through the OSDK
  trace function `0x2e8448` and calls the listener's `vt[0]` -> `0x1624e1c` -> `EVENT_CREATEGAME_SUCCESS`;
- otherwise: creates a `SetPlayerAttribute` op (`0x28a384`) and only completes (`0x2deb88`) after it finishes.
Since no Blaze `setPlayerAttributes` request was ever seen on the wire, either that op never runs/never sends, or the
listener call is never made. Round 2 hooks exactly these points.

## Trace patch for RPCS3 (diagnostic tool, works)

The server cannot see what the game does *inside* the emulator. `tools/make_trace_patch.py` generates an RPCS3 PPU
patch (`tools/rpcs3_patch_trace_invites.yml`) that logs when selected game functions are entered:
- Every hooked function starts with `mflr r0 ; bl _savegpr_N`. The patch replaces only the second word with a `bl` to a
  tiny stub in an unused, zero-filled area of the code segment (`0x0211D810`). The stub saves LR in `r12`, calls a
  common routine that preserves **all** of r0-r12, f1-f13, LR, CR and CTR, restores LR and tail-jumps to the original
  `_savegpr_N`, so the hooked function behaves exactly as before.
- The common routine logs through `cellRtcSetTime_t(pDateTime, iTime)`, which RPCS3 prints with its arguments:
  `iTime = 0xFA | id(8 bit) | k(16 bit) | value(32 bit)` where `k` is the argument index (0 = r3, 1 = r4, 2 = r5). The
  game itself calls the same function with ordinary timestamps (`0x5884....`), so the markers are easy to spot.
- `tools/decode_trace.py RPCS3.log [--elf EBOOT.ELF] [--all]` prints a chronological table (markers, event names
  resolved from the ELF, and the surrounding `sceNp*` calls). `tools/install_trace_patch.py <rpcs3 folder>` adds or
  removes the entry in `patches/patch.yml` (idempotent, keeps CRLF, writes a `.bak`).
- `python tools/make_trace_patch.py --selftest` checks the encoding with capstone and the register-preservation and
  logging semantics with a small built-in PPC emulator (mutation-tested; part of the repo selftest);
  `--verify-elf EBOOT.ELF` checks every hook target on the real binary (first word `mflr r0`, second word `bl` to the
  helper listed in the table); `--check` verifies that the committed YAML is up to date.
- **Round 1** (18 hooks: invitation chain, `post_event` = every `EVENT_*`, NP handlers, SportsWorld ops) was verified
  in real RPCS3 (see above). **Round 2** (39 hooks) adds: the OSDK trace function `0x2e8448` (prints `GSMR/SUCC`,
  `GSMR/FAIL`, ... as 4CC pairs), `GameSessionManager::createGame`, the op constructor, `GameSession` ctor,
  `setCurrentSession`, `SetPlayerAttribute`, the UI listener success/failure handlers, the Blaze
  `NotifyGameSetup`/`onNetworkCreated`/`finalizeGameCreation` handlers and the match-up FSM (`0x4d087c`, `0x4cf708`,
  `0x4d0aa4`, ...).
- **Round 3** (54 hooks + 1 mid-function marker + 1 optional experiment): every marker also logs the caller's LR
  (`k = 3`; r0 holds LR after the prologue's `mflr r0`), `decode_trace.py --elf` maps it to the calling function.
  Markers `0x71`/`0x72` hook the script-call bodies `0x38aca4`/`0x38b110` and additionally dereference the variadic
  argument array (`k = 5, 6`: words at `r6+4` / `r6+12`), so the script function name and its first two string/object
  arguments are readable. The **mid-function marker** `0x70` (table `MIDS`) replaces one instruction (`mtctr r5` at
  `0x2def74`) by a `bl` to a stub that preserves every register (including r11/r12 on a small extra frame), logs
  r3/r4/r5 and executes the displaced instruction; it is only allowed where LR is dead. The **experiment** entry
  (`EXP_*` constants) replaces `li r3,4` at `0x2def7c` by a `bl` to a 27-word stub (frame `0xC0`, volatile registers
  only, r28 = session): `id = (session+8)->vt[0]()`, `text = fmt(id, buf, 10)` (`0x287c5c`),
  `mgr = 0x38a5ac()`, `0x38ade4(mgr, 0, "OnOnlineCreateSession", "EVENT_CREATEGAME_SUCCESS", text, 0)`. The selftest
  emulates it with fake callees and checks call order, arguments and preservation of r1/r2/r14-r31/cr.
  `install_trace_patch.py` installs both entries (`--no-experiment` skips the second).
  Free cave space left: 0x94 bytes (`0x0211E16C`..`0x0211E200`).
- Install on **both** PCs (the friend's side shows whether anything arrives). If the game does not start after enabling
  it, untick the patch in RPCS3's Patch Manager. The first launch after a patch change recompiles the PPU (minutes).

## Session 2026-10-11 (evening): host-only ladder, everything reaches `finalizeGameCreation`

**Test (`logs/gm_attempts.log` 13:34-13:38; host `odyniec`, friend `odyniec1`):**
- 8 x `createGame`, levels 1-5 (host-only): **every one reached `finalizeGameCreation`, zero crashes** (the b015730 fix
  works). `state/gm_variant.json` ends at level 5, all levels "good". On levels 3-5 (INITIALIZING) the host moves the
  game to PRE_GAME itself with `advanceGameState` (cmd 0x0003) - normal behaviour.
- After `finalizeGameCreation` the host always did the same, on every level: ~0.7 s later
  `lookupUsersByPersonaNames(['odyniec1'])`, ~0.8 s `Stats::getStatsByGroupAsync` for group `MyFriendlies` with its OWN
  EID, then **silence** (nothing on Blaze until the next manual attempt ~30 s later). The friend sends nothing (no
  `joinGame`).
- **Host RPCS3: zero `sceNpBasicSendMessageGui` calls** and no `sceNpLookup*` besides start-up initialisation
  (`sceNpLookupInit/CreateTitleCtx`). The friend only sees `basic_event: event:1` (PRESENCE) and `event:0` (OFFLINE),
  never `event:5`/`event:2`.
- **Telemetry** (now readable: `logs/captures/telemetry_*.txt`, the content is plain text after TLS decryption). The only
  events are `client_bugsentry` -> `ServerError`, `server_name:"SportsWorld"`, `server_type:"https"`,
  `errid:"disconnect"`, repeated on EVERY `lookupUsersByPersonaNames` (host and friend, also after `createGame`).
  SportsWorld = the EASW web service (config keys `OSDK_EASW_AUTH_URL/REQ_URL/MEDIA_URL/EVENT_URL/GF_FILE_URL`, module
  `0x30eb10`, op `ConnectToSportsWorld` `0x309388`, `LoadSportsWorldUserPresence` `0x30f2f0`, `SendFriendsToSportsWorld`
  `0x30f068`). The server does not provide these keys, so every request ends in "disconnect". (Cosmetic: `pid` in the
  telemetry is the same for both players because `postAuth.UROP.UID` gets the host's constant.)

**Invitation chain on PS3 (static analysis; the flag `UNK_025fbcd0` is only read, never written, = 0 => NP path):**
Flash UI -> adaptor `OSDK_GameInvitationAdaptor` (`0x16491b4` SendGameInvite / `0x1649264` SendMultipleGameInvites;
**they silently do nothing when module `ginv` is not registered**) -> module `ginv` (`0x301ad4`, registered by
`0x302184` when `Invitations[5]==1`) -> op `SendGameInvite` (`0x3019a4`) -> manager `puid` (`0x2cd938`, op
"LookupPlatformUserId": first the local friend list `0x2f8b50`/`0x27ea00`, only then `sceNpLookupNpIdAsync`) -> callback
`0x30262c` -> `0x302454` (needs a current session from `gses` vt[0x24], otherwise returns error 4 without touching NP)
-> `sceNpBasicSendMessageGui`. The only other caller of that import is `0x2f95c0` (a different one-recipient
operation); `sceNpBasicSendMessage/Attachment` are never called.

**Server changes of this session:** `MAX_PREGAME_TIMEOUT=120` in the client config (`OSDK_CORE_DEFAULTS`; the match-up
FSM `0x4d087c` reads it, default 10 s - the host used to return to the menu after ~14 s, before a friend could accept an
invitation). The telemetry stub now reads the whole request body (Content-Length) and logs it to the session log
(`telemetry tresc: ...`).

## Session 2026-10-11 (earlier): ladder test, crash analysis, the "isHost" model

**Test (two runs with the ladder; server logs + RPCS3 of host `odyniec` and friend `odyniec1`):**
- **Level 1 (byte-for-byte the old flow) completed on BOTH clients**: host and friend both sent `updateMeshConnection`
  (STAT=2) and `finalizeGameCreation` within 0.2 s of the setup; both bound UDP 3659/9999. Then the host did a lookup of
  `odyniec1` + `MyFriendlies` stats (`EID=[0]`) and, ~13.7-13.9 s after the setup, `sceNpBasicSetPresence` (64 B) - the
  friend then receives `basic_event: event:1 (PRESENCE)`. No `sceNpBasicSendMessageGui` in any of the ~40 collected RPCS3
  logs.
- **The second `createGame` (level 2) crashed the host's emulator in BOTH runs** (also after adding removal of the old
  game): `VM: Access violation reading location 0x0`, `FEThread`, PC `0x288ae0`, `Emulation has been frozen!`. Stack:
  NotifyGameSetup handler `0xc6e0b8` -> `0xc6dad8` -> network start `0xcb98e8` -> `onNetworkCreated 0xc6a92c` ->
  `0xc6a4e0` -> job-end callback `0xc71c44` -> `0x2dee08` (`GSMR SUCC`: creates the `GameSession` `0x2891e4` and
  stores it in `gses`) -> `onPlayerAdded 0x288f8c` -> `0x288e58` -> `0x288a84`, which dereferences the result of
  `0x2887d4` ("current game" from `gses`, or `gsmp` if missing) == NULL. The first hypothesis (a stale game GID=1 left in
  the client) was **wrong**: `NotifyGameRemoved` before the setup changed nothing. The real cause is level 2 itself:
  **the client did not consider anyone to be the host.**
- **How the client decides who is the host (decompiled; `Game` ctor `0xc58c3c`, `addPlayer 0xc5844c`, `0xc6a4e0`):**
  `isHost` (`Game::vt[0x64]`, `0xc57318`) = `Game+0x3ac == Game+0x42c`, i.e. the host's network endpoint == MY endpoint.
  Endpoints live in the map `mGameEndpointMap` keyed by CONG (`Game::vt[0x88] = 0xc573a8`).
  * `Game+0x42c` (mine) = the endpoint of my own roster player (key = its CONG) or, in state INITIALIZING, the endpoint
    built from `THST {CONG, CSID, HNET}` when my BlazeId == `THST.HPID`.
  * `Game+0x3ac` (host): in state INITIALIZING/0x84/0x85 = that endpoint from `THST`; in any other state (e.g. PRE_GAME) =
    the endpoint with key **`DHST.CONG`** (field `dedicatedServerHostInfo`; absent => key 0).
  * `GAME` layout: three consecutive `HostInfo` structs (size 0x38; +0x10 HPID, +0x20 HSLT, +0x28 CONG, +0x30 CSID) at
    `GAME+0xe0` = THST, `+0x118` = DHST, `+0x150` = PHST.
  * Old flow: nobody had a CONG (all key 0), so endpoint 0 was both "host" and "mine" on EVERY client - the friend also
    thought it was the host and also sent `finalizeGameCreation` (hence no invitation step).
  * Level 2 still had no CONG in THST/DHST but non-zero CONGs in the roster => `isHost=false` everywhere => no
    `updateMeshConnection`/`finalizeGameCreation`; `0xc6a4e0` finishes the createGame job immediately (callback `0x2dee08`),
    the session constructor (`0x2891e4`) calls `onPlayerAdded` for the local player in state 4, and `0x2887d4` returns NULL
    => crash. With STAT=2 in the setup that call does not happen (so a joining player should have STAT=2).
  * The rebuilt flow 504829f (INITIALIZING, consistent CONG) did not crash but stood still for 30 s (createGame job never
    ended; compare 13.9 s on level 1) - cause still unknown (we do not know whether the "network created" callback fired).
- **Invitation**: PS3 uses the NP path (`UNK_025fbcd0 == 0` -> op `SendGameInvite` -> `sceNpBasicSendMessageGui`,
  `0x301ad4`/`0x302454`), started by the UI (Flash script) after `EVENT_CREATEGAME_SUCCESS` (`0x1624e1c`) - the host never
  got there. In the old flow the friend was immediately in the setup as CONNECTED and had its own game, which may have
  disabled the invitation step.

**Host-only ladder (`gamemgr.LEVELS`)** - only the host gets a setup; the friend joins later through `joinGame` (then gets
a `NotifyGameSetup` with CONG and STAT=2, the host gets a state change / `NotifyPlayerJoining`):
1. `1-host-sam-stary` (host alone, old shape) - PRE_GAME, host without CONG (key 0 = host, as it used to work), old fields, REAS as in FIFA 14;
2. `2-host-i-zarezerwowany-kolega` (host + reserved friend) - like 1 + the friend from `PLJD` in the host's roster as RESERVED (STAT=0, with CONG);
3. `3-host-sam-initializing` (host alone, INITIALIZING) - INITIALIZING + consistent host CONG (roster = THST = PHST), PRE_GAME after finalize;
4. `4-host-laczy-sie` (host is "connecting") - like 3 + host STAT=2 in the setup, after finalize the server announces it as CONNECTED;
5. `5-docelowy` (target shape) - like 4 + reserved friend + new player and game fields + REAS DLSC/CREATE.
Auto mode: start at 1; after a host `finalizeGameCreation` the next `createGame` takes the next level; after the first
failure the target level (5) is tried once, then the best working one; a client disconnect during an attempt (crash) does
not count as a failure, the level is repeated (twice). Before a new game the server removes the host's old games
(`NotifyGameRemoved`). The state file is version 3 (`state/gm_variant.json`). Pin a level with `"gm_variant": N` (1-5) in
`config.json`; `-1` switches to the individual `gm_*` toggles. Player ids are now < 2^31 (`ids.uid_for`: 1.1e9..2.0e9) -
host `odyniec` used to get uid 2 999 187 369 while the old working flow only used values < 2^31.

**Diagnostics for a single test:** `logs/session_*.log` (whole console log), `logs/gm_attempts.log` (attempt journal: every
GameManager notification decoded, every client request with a millisecond timestamp, result; GameManager and Messaging
requests are logged without time limit because the friend's `joinGame` can arrive a minute after `createGame`), probes at
3/6/9 s if the host stays silent (`gm_probes`), capture flush every 2 s, `collect_logs.ps1` zips everything.

## Session 2026-10-09 (evening): first live test after the GameManager rebuild

**Result (host `odyniec` + friend `odyniec1`, Radmin, server logs and both RPCS3 logs):**
- Login, persona lookup, `listEntitlements`, `Messaging::fetchMessages/getMessages` and Stats work on both.
- The host sent `createGame` (PLJD: its own CGID `(30722,2,uid)` echoed correctly, the friend reserved with a unique uid)
  -> the server answered `CreateGameResponse GID=1` + `NotifyGameSetup` (DLSC/CREATE, GSTA=1, host STAT=2). The host's
  client then bound UDP **3659 and 9999** (it started creating the game network) but sent **neither
  `updateMeshConnection` nor `finalizeGameCreation`** and never called `sceNpBasicSendMessageGui`; the user closed the
  emulator after about a minute. The friend received nothing (an invitee joins only after finalize).
- **This was a REGRESSION against the last live test (commit 2c9842c)** where the same chain reached
  `finalizeGameCreation`. The old setup had GSTA=130 (PRE_GAME), both players with STAT=4, REAS unset (wrong tag VALU =
  empty union) and separate `GamePlayerStateChange` x2 + `GameStateChange`. The new one had GSTA=1, only the host with
  STAT=2, REAS=DLSC and no follow-ups.

**What decompilation established (addresses in the FIFA 17 PS3 EBOOT):**
- `0xc6e0b8` = NotifyGameSetup handler: finds the createGame job by (user index, GID), creates the `Game` (`0xc69ed0` ->
  ctor `0xc58c3c`), then `0xc6dad8` -> `0xc60028` starts the game network. The network result returns to `0xc6a92c`
  (onNetworkCreated) -> `0xc6a4e0`, which for the host sends `finalizeGameCreation` (`0xc6a204`) and for a joiner
  `updateMeshConnection STAT=2` (`0xc59f14`). The host sends no `updateMeshConnection`.
- The finalize reply (`0xc6bf74`) completes the createGame job with success when `0xc5b8c0` returns true (for unset REAS and
  DLSC with DCTX!=3 - it does). Only then can the front end send the invitation.
- Sending the invitation (`0x302454`) needs a current session in the `gses` component; without it it returns an error
  without calling `sceNpBasicSendMessageGui`. FIFA sets the host's game id in the INITIALIZING handler, so the old flow
  (straight to PRE_GAME) could finish createGame but not reach the invitation.
- `isHost` in `Game` = (BlazeId from THST/PHST.HPID == BlazeId of the logged-in user from the local player list);
  `vtable[0x28]` of the game returns the topology host id (`Game+0x1b8`) and the NotifyGameSetup handler returns without
  doing anything when that is 0. (Refined on 2026-10-11, see the isHost model above.)
- Observation: the header `Easw-Session-Data-Nucleus-Id` (POW, `0x51c1e4`) carried the FRIEND's id (2028699422) on the
  host and its own on the friend. FIFA takes it from a different user container than the local player list (hypothesis:
  first/minimum id). It does not affect GameManager, but remember it for FUT/POW.

**Test 2026-10-10 (RPCS3 logs of host and friend, no server log):** still no connection. The host log has one UDP 3659/9999
`bind` pair (one `createGame`), then nothing - no `sceNpBasicSendMessageGui`; the friend had no bind of 9999, so no setup
(an invitee gets its setup only after the host's `finalizeGameCreation`). `NPHandler: basic_event: event:0` is RPCS3's
OFFLINE of a friend (1 = presence, 5 = invitation), not a message. The RPCS3 logs of the old (2026-10-08) and new runs are
almost identical - only the server log decides. **From the archived server log (old flow, 2026-10-08 22:02 UTC):** after the
setup the host sent `updateMeshConnection` (STAT=2, TCG=(30722,2,0)) and `finalizeGameCreation` IMMEDIATELY, then the front end
looked up the friend and the `MyFriendlies` stats and returned to the previous screen without an invitation (so createGame
succeeded).

**Fixes after the first ladder run (unverified on a live client at the time):** (1) every new host `createGame` first removes
the host's earlier games (`NotifyGameRemoved` to all members, before the new setup; `gamemgr._retire_stale_locked`);
(2) a host client disconnect during an attempt (crash / closed window, also within 120 s after the watchdog verdict) does NOT
count as a level failure - the same level is repeated, only the second such disconnect marks it as failed
(`result: crash`, `crashes` in `state/gm_variant.json`); (3) the state file is versioned - an old "level 2 failed" result is
ignored; (4) watchdog = 14 s.

## Session 2026-10-09: decompiler analysis, GameManager rebuild

Decompiling the EBOOT (Ghidra headless) exposed concrete errors in our GameManager replies; all fixed in code
(`gamemgr.py`, `ids.py`, `messaging.py`), UNVERIFIED on a live client at the time:
- **REAS union (GameSetupReason)**: in FIFA 17 the members have different tags (DLSC, IJGS, IMSC, MMSC, RDSC) and the wire
  number is the position in the table sorted by tag (DLSC=0, IJGS=1, IMSC=2, MMSC=3, RDSC=4). The union decoder (`0xcd0bc8`)
  leaves the union EMPTY on a tag mismatch. We used to send tag VALU (FIFA 14) and number 2 (= IndirectMatchmaking!) to the
  invitee. Now the host gets DLSC{DCTX=0} and the invitee IJGS{RPVC=0}.
- **CONG (connection group id)**: the client keeps network endpoints under `ReplicatedGamePlayer.CONG` and does NOT connect to a
  player whose CONG equals its own (`0xc5e22c`, `0xc5e958`, `0xc5eaec`). We had CONG=0 for everybody => no UDP between
  players. Now every player has a unique CONG (= its BlazeId), identical in the roster, in `HostInfo
  {CONG,CSID,HPID,HSES,HSLT}` and in `UserAuthenticated.CGID` (ObjectId (30722,2,id)).
- **Unique player ids** (`ids.py`) instead of one shared `LOCAL_USER_ID`, with a per-client view.
- `TimeValue` (TIME) fields have TDF type 11; `HSES` removed from `ReplicatedGameData`; `GPVH/SEED/UUID/MNCP/PSAS` added.
- On the host the createGame success arrives only with the REPLY to `finalizeGameCreation` (`0xc6bf74`) - the server answers
  empty without an error (OK). Then the front end sends the invitation through NP: `gses` must hold a current game session,
  then `sceNpBasicSendMessageGui` with the game id (`0x302454`).
- New: `Authentication::listEntitlements` (ACTIVE per group), Messaging (mailbox), removePlayer/destroyGame/joinGame,
  NotifyUserAdded for the players of a game. Toggles in `config.py` (`gm_*`, `serve_entitlements`, `serve_messaging`).

## Session 2026-10-08 (read this first for the history)

**Goal:** get two real players (two PCs, RPCS3, Radmin VPN) past "Sending match invite and creating a
game session. Please wait..." in Online Friendlies and into a match. **Not achieved yet** — the
host still waits after `createGame`. Everything below is pushed to `main`; none of the newest
server changes had been run on a live client yet (only a local two-client simulation).

**Verified this session**
- Client crash after a populated `lookupUsersByPersonaNames` reply was caused by a wrong `UserData`
  shape (fixed: `{EDAT{ADDR unset}, FLGS, USER{...}}`).
- After `NotifyGameSetup` both clients bind UDP 3659/9999 and send `updateMeshConnection`
  (`STAT=2`) + `finalizeGameCreation`; no peer UDP traffic was observed and the host never proceeds.
- TDF member lists were read from the reflection tables in `EBOOT.ELF` (tool: `tdfauto.py`, not in
  repo), which showed several shapes differed from the FIFA 14 definitions: `NotifyPlatformHostInitialized`
  = {GID, PHID, PHST}, `NotifyPlayerJoinCompleted` = {GID, PID, TIME}, `ReplicatedGamePlayer` has
  EXBL/LOC/NASP/PATT/TIME, `ReplicatedGameData` has no `HSES`.
- The client's Game class expects the topology host (`THST.HPID`) to be present in the roster under
  the same PID, and treats players joining a game that is not yet in PRE_GAME as deferred.

**Changes pushed (all unverified on a live client, each has a `config.json` switch — see
`TEST_PLAN.md`, which is in Polish)**
- `gm_deferred_pregame` (default on): game starts in INITIALIZING(1); PRE_GAME(130) is sent only after
  the host's `finalizeGameCreation`.
- `gm_faithful_flow` (default on): host gets `NotifyGameSetup` alone; the invitee gets its setup
  (plus `NotifyPlatformHostInitialized`) only after the host's `finalizeGameCreation`, and the host
  gets `NotifyPlayerJoining`.
- Corrected notification shapes (above) and richer roster players.
- `advanceGameState` / `setGameAttributes` / `setPlayerAttributes` are acknowledged and broadcast to
  all players of the game.

**Still unknown:** what exactly makes the client raise `EVENT_CREATEGAME_SUCCESS` for the host, and
how the game sends the actual invitation to the friend (see the 2026-10-11 sessions above for the
current state).

### Registration on every start - cause and fix (2026-10-08)
From a capture: after login the client sends `Util::userSettingsLoad` (0x0009/0x000A) with key `FirstTimeFlag`; an
empty reply means "first login" -> welcome / opt-in screen (`localizeStrings`, `getEmailOptInSettings` 0x0001/0x00F2,
`getAccount`, `updateAccount` 0x0014 with OPT1/OPT3), and at the end the client saves `userSettingsSave` (0x000B)
`FirstTimeFlag='0'`. The server ignored the save, so the registration came back every time. Now
`fifa17srv/usersettings.py` stores the settings in `state/user_settings.json` (key = persona name) and returns them in
Load/LoadAll. Disable with `"persist_user_settings": false`. After the first registration later runs should skip it.
(This does not fix the "EA servers not available" bar in the corner of the menu.)

### Banner "...servers are unavailable at this time" - hypothesis and experiment (2026-10-08)
Screenshot: the top of the screen scrolls "...rs are unavailable at this time. Please try again late(r)", and the Catalogue
tile says "Connect to the EAS FC servers". From the EBOOT: `POWService` (class at 0x2314c0, tick 0x236178) is responsible - an
EASFC/EASW service over HTTP (headers `EASW-Session`, `EASW-Token`, `EASW-Nucleus-Persona`). Its address comes from the
server config keys `FIFA_POW_URL` / `FIFA_POW_CONTENT_SERVER_URL` (code 0x232550); when they are missing the address is empty -
exactly what RPCS3 shows as `DnsHook: DNS query for ` (empty name) after login. The server now adds these keys to the
fetchClientConfig reply and has an HTTP stub on ports 8094 / 8080 (`pow_stub.py`) that only RECORDS requests
(logs/captures/pow_*.txt). It is not a real service yet - the goal is to learn the protocol. Disable with
`"serve_pow_config": false`. **Note (2026-10-09): the hypothesis "empty POW address = empty DNS name" was not confirmed - see
the section below.** (The CONF map in fetchClientConfig is now sorted by key.)

### EAS FC banner - new findings (2026-10-08, evening)
The test with `FIFA_POW_URL` in the config did NOT change the banner and the client did not connect to ports 8094/8080 (log:
still `DnsHook: DNS query for ` with an empty name), so POW was not the cause. After a while the banner changes by itself
from "The EAS FC servers are unavailable..." to "PRESS THE START BUTTON TO RE-CONNECT" - with the Blaze connection open (RPCS3
closes it only after START is pressed). Findings from the EBOOT:
- `Util` command 8 is **postAuth** (request {DSUI, MAC, UDID}, reply {TELE, TICK, UROP}); the server used to answer empty,
  so the client never got the address of the **ticker** server (top bar) or of telemetry. Command 5 is getTelemetryServer.
  Both are served now (values from the working FIFA 14 server Impulsum14).
- Component **0x08C9 is OSDKSettings** (fetchSettings = 1, fetchSettingsGroups = 2), not "Seasons".
- The client config module ("scfg") holds the groups OSDK_CORE/CLIENT/NUCLEUS/WEBOFFER/ABUSE_REPORTING/TICKER from
  fetchClientConfig. Keys OSDK_PEERBUFFERSIZE, OSDK_MAXGAMES, OSDK_MATCHUP_TIMEOUT, ... were added.
- A TCP stub on port 6776 (ticker) records what the client sends (logs/captures/ticker_*.txt).
Toggles: `serve_post_auth`, `serve_osdk_core_defaults`, `serve_pow_config`.

### EAS FC banner - EBOOT analysis and the `postAuth` log (2026-10-09)
Test of commit `47a874d` (postAuth + ticker + OSDK keys): banner **unchanged**, the client **did not connect** to port 6776
(ticker) or 8094/8080 (POW). The "ticker" and "POW URL" hypotheses are ruled out as the cause of the banner.

**Confirmed in the EBOOT (code, not guesses):**
- The corner banner is the `POWService` (EASFC) state machine. Function `0x15cc554` (SetStatus) picks the text from the table
  {`TXT_EASFC_SERVER_ERROR`, `TXT_EASFC_PLEASE_SIGN_IN`, `TXT_EASFC_RECONNECTING`}; the subtitle `TXT_EASFC_RECONNECT_PROMPT`
  ("PRESS START TO RE-CONNECT") appears only when the flag `POW+0x2670` is set - the POW tick (0x236178) sets it after
  `POW_RECONNECT_TIMER_MS` (default 5000) **and only when the POW state == 2**. This matches the observation "first
  'unavailable', after a while 'press start'".
- POW state (`+0x80`) = 2 is set only by the handler `0x239a7c` (vtable slot 0x214), called with (type=1, reason in {1,2,7})
  **when POW is "online"** (state 1 + `POW_IS_ON` + the flag `+0x84` set by `PowBlazeConnected`, called from the post-login
  function 0x4eab24). The UI handler `0x15ccbf8` has the same filter. **Who sends this event is not known yet** (the main
  unknown).
- START calls `POWService::PowReconnect` (message "connecting to EA SPORTS Football Club").
- The RPCS3 log contains **no** POW request at all (neither DNS `pas.gt.easfc.ea.com` nor a connection to our 8094) -
  `pow/healthcheck/system/all` is not sent. Reason unknown.
- Two failing DNS lookups after login: `ut` comes from the FUT module (code 0x51d3f4 builds `<FUT_RS4_BASE_URL>` +
  `"ut/game/fifa17/"`, and the base is set in 0x4eab24 **only if the server provides the key** `FUT_RS4_BASE_URL`; without it the
  URL starts with `ut/...` and the host is `ut`). The lookup with an **empty** host name happens right after the directories
  `imgAssets/banners/fifahub` and `.../easfcnewsalerts` are created - POW code creates them (0x2309dc / 0x230a0c), so it is
  probably POW downloading banners/news with an empty URL (not confirmed, it is not `FIFA_POW_URL`).
- Defaults of `getString` for a missing key are copied into the buffer (proof: `PIN_SERVER` -> `pin-river.data.ea.com` works on the
  default value).

**Server change (unverified on a live client):** `serve_fut_config` (on by default) adds `FUT_RS4_BASE_URL`
(`http://<host>:8094/fut/rs4/`, must end with `/`) and `FUTDYNAMICMESSAGES_URL_BASE` to fetchClientConfig; the HTTP stub at
`pow_port` records the requests (logs/captures/pow_*.txt). Expected effect: the `ut` lookup becomes a real connection to
our port 8094 and we will see what FUT requests. Whether the banner disappears is unknown.

**Cosmetic workaround (optional RPCS3 patch):** `tools/rpcs3_patch_easfc_banner.yml` - one word, `blr` at the entry of
`SetStatus` (0x015CC554), so the banner is never set. Paste the entry under the same `PPU-1243af2b...` key in `patch.yml`
next to the other patches and enable it in the Patch Manager. If an empty bar remains after the patch, say so (next step: also
disable the widget refresh). It neither changes nor fixes the POW service itself.

### Play Season: empty screen - season data (2026-10-09, after the `5aa470a` test)
Test: banner unchanged (POW still no requests), but the `ut` lookup disappeared and the client requested
`GET /fut/rs4/ut/game/fifa17/user/accountinfo` (FUT, header `Easw-Session-Data-Nucleus-Id`) - the stub answered `{}`.
Play Season = empty stadium + banner; in the server log silence after `getStatsByGroupAsync` for `H2HSeasonalPlay`.
From the EBOOT (confirmed in code): the season screen is a sequence of requests (function 0x6f95c8): [0] `H2HSeasonalPlay`
stats, [1] `H2HPreviousSeasonalPlay`, ..., [3] the `FIFA_H2H_SEASONALPLAY` configuration. Function 0x5086a8 sends the event
`FE::FIFA::SeasonalPlayDownloadSuccess` **separately for every entity row** of the Stats result and assembles the values from
~65 statistics by NAME (0x507a84: seasons, curDivision, prevDivision, maxDivision, rankingPoints, seasonWins, ...). Our replies had
empty lists (no `StatGroupResponse.STAT` descriptors and no row in `KeyScopedStatValues.STS.STAT`), so the event was never sent
and the screen waited. **Change (unverified on a client):** `serve_seasonal_stats` (on by default) - for the groups
`H2HSeasonalPlay`, `H2HPreviousSeasonalPlay`, `CoopSeasonalPlay_StatGroup` the server returns the descriptors (70 names) and one row
for the player's EID (values "0", divisions = "10"; ETYP=(30722,1) - guessed). Next step: watch the server log - if the client
requests another group (`H2HPreviousSeasonalPlay`) or `fetchClientConfig` with `CFID=FIFA_H2H_SEASONALPLAY` (keys NUM_DIV,
TOP_DIV, DIV_%d_NAME, PTS_WINS... from 0x4efe18), they have to be served. Disable with `"serve_seasonal_stats": false`.
The "PRESS START" banner is separate (POW state); patch: tools/rpcs3_patch_easfc_banner.yml.

Older notes that used to be appended at the end of this file are now in "Appendix: older GameManager notes" at the bottom.

## History (older sessions, kept for reference)

Everything from here down is older material. The sessions above are newer and win wherever they disagree with it
(several early hypotheses below were later disproved - each such place says so).

## Current status (as of 2026-09-28)

The full PS3 client login handshake **works end-to-end**: redirector → TLS → PreAuth → Ping →
fetchClientConfig → fake Nucleus OAuth → Blaze login → account/session notifications → main Online
menu is reachable and playable-looking (Continue → checkbox → OK gets you into the FIFA17 Online
menu with PLAY SEASON / ONLINE FRIENDLIES / CURRENT SEASON / TROPHY ROOM / PLAY CUP MATCH tiles).

**Play Cup Match / Play Season** are still stuck (see "Current blocker" below) — that investigation
is unchanged from 2026-09-25 and still needs the Ghidra work described there.

**Online Friendlies got significantly further this session (2026-09-26), with two real players
over Radmin VPN** — see "Online Friendlies: GameManager `createGame`" below for the full writeup.
Short version: two separate RPCS3 instances (different physical PCs, connected via Radmin VPN) can
both log in, see each other in the friends list (RCPN-backed presence, not our fake Blaze), and
open Match Settings. Clicking **"PLAY MATCH"** sends `GameManager::createGame` (component `0x0004`),
which the server previously left completely unhandled (empty Reply) — now implemented (see below).
**Still unresolved:** even with a correct `CreateGameResponse` + `NotifyGameSetup` reply, the client
does not visibly leave the "Sending match invite and creating a game session. Please wait..."
screen in solo testing (host only, no second player actually connected) — current best theory is
that the wait screen is *also* gated on a real RPCN friend-presence check (`sceNpBasicGetFriendPresenceByIndex`,
which the client polls roughly once per second in a background thread) succeeding for the invited
friend, which is **not** something our fake Blaze server can influence — untested with both players
online simultaneously.

## Architecture

```
FIFA17 (PS3, via RPCS3)
   |
   |--TLS--> redirector (fake, HTTPS) --------- winter15.gosredirector.ea.com, answers
   |                                             POST /redirector/getServerInstance with our
   |                                             blaze host:port
   |
   |--plaintext or TLS--> blaze (main game server, port 10051) -- PreAuth, Ping, fetchClientConfig,
   |                                             Authentication, UserSessions, Stats, ... (see below)
   |
   |--TLS--> qos (fake, HTTP-over-TLS, port 17502) -- NAT type / firewall / connectivity probing
   |
   |--plaintext--> nucleus (fake, HTTP, port 8081) -- fake OAuth: GET /connect/auth always succeeds
   |                                             with a 302 to a fixed FAKE_AUTH_CODE
   |
   \--TLS--> telemetry (fake, port 443) -- accepts rl.data.ea.com / pin-river.data.ea.com
                                            connections and immediately closes them (204/RST),
                                            just so the client doesn't hang retrying
```

All of this is driven by editing the PS3's hosts resolution (via `tools/install_redirector.ps1`,
which patches RPCS3's Windows hosts file) so `winter15.gosredirector.ea.com` and the telemetry
hostnames resolve to `127.0.0.1`.

## Blaze/Fire2 wire protocol (confirmed, not guessed)

### 16-byte frame header

Reconstructed from `grid-leak/blaze` (an open-source Blaze server for Mirror's Edge Catalyst, same
2016 Blaze SDK 15.1.x family — its advertised `SVER "Blaze 15.1.1.0.5"` is a near-exact match for
FIFA17's `BSDK "15.1.1.0.0"` from its CINF), and verified byte-for-byte against real FIFA17 PS3
captures:

```
offset  0-3   payload_size   u32 BE
offset  4-5   metadata_size  u16 BE  (always 0 in our captures -- no metadata section)
offset  6-7   component      u16 BE
offset  8-9   command        u16 BE
offset 10-12  msg_num        3 bytes BE (NOT 2 -- grows with each request in the same session)
offset 13     msg_type << 5  (Message=0, Reply=1, Notification=2, ErrorReply=3, Ping=4, PingReply=5)
offset 14     options        always 0 in our captures
offset 15     reserved       always 0 in our captures
```

A Reply has the **exact same** component/command/msg_num as the request it answers; only
`msg_type` changes to `Reply(1)`. A Notification is server-initiated (no matching request),
`msg_type=Notification(2)`, and its own independent msg_num sequence.

### TDF (Trusted Data Format) tag/value encoding

- Tag = 4 ASCII chars, 6 bits each (`code = char - 0x20`), packed big-endian into **3 bytes**.
  Implemented in `tdf.py`'s `encode_tag()` / equivalent decode.
- Field on the wire = 3-byte tag + 1 type byte + type-specific value encoding.
- Wire types (index = the byte written, in `tdf.py` order):
  `VARINT=0, STRING=1, BLOB=2, STRUCT=3, LIST=4, MAP=5, UNION=6, INTLIST=7, OBJTYPE=8, OBJID=9, FLOAT=10, TIME=11`.
  (Blaze TDF has no separate "bool" wire type — booleans are VARINT 0/1.)
- STRUCT ends with a `0x00` terminator byte after its fields.
- LIST = 1 byte subtype + varint count + that many encoded values.
- MAP = 1 byte key-type + 1 byte value-type + varint count + that many (key,value) pairs — the
  value type byte is written even when the map is empty (count=0), so an "empty map" reply still
  needs the correct key/value type bytes, not just a zero count.

Full codec: `fifa17srv/tdf.py`. `_enc_value()` / `encode_field()` / `encode()` are the ones that
matter; read them directly rather than trusting this summary if something looks off on the wire.

## What's implemented in `fifa17srv/blaze.py`, component by component

Legend: **[CONFIRMED]** = shape taken directly from EBOOT reflection data or matches a real
reference implementation 1:1. **[HYPOTHESIS]** = best guess from field names/types only, not
verified against any known implementation. **[UNHANDLED]** = falls through to the generic
empty-Reply fallback (`0` TDF fields) — this is very likely *wrong* for any command that isn't
purely a fire-and-forget notification, but the client has tolerated it so far for everything below
except possibly the current blocker.

- **`0x0009` Util** (`UTIL_COMPONENT`)
  - `0x0007` PreAuth **[CONFIRMED]** → `PreAuthResponse` (bisected field-by-field via
    `config.preauth_groups`; client only accepts CIDS as a type-4 LIST, not type-7 INTLIST)
  - `0x0002` Ping **[CONFIRMED]** → `PingResponse`
  - `0x0001` fetchClientConfig **[CONFIRMED]** → empty-ish typed reply per `CFID`
    (`OSDK_CORE`, `OSDK_CLIENT`, `OSDK_NUCLEUS`, `OSDK_WEBOFFER`, `OSDK_ABUSE_REPORTING`,
    `IdentityParams`, `OSDK_TICKER`, `OSDK_ROSTER` all observed)
  - `0x0004` (`LANG`, `LSID` = list of localization string ids, e.g.
    `SDB_ORIGIN_ACCT_WELCOME_BACK_HEADER`) **[UNHANDLED]** — looks like a batched localized-string
    fetch (welcome-back dialog, opt-in dialog copy). Client proceeds fine with an empty reply, so
    it's probably just falling back to baked-in English strings.
  - `0x0005` (`CMAC`, `SNAM`) **[UNHANDLED]** — unknown, sent once right after login with a MAC
    address and empty `SNAM`.
  - `0x0008` (`DSUI`, `MAC`, `UDID`) **[UNHANDLED]** — unknown, looks like device/session info.
  - `0x000A` (`KEY`, `UID`) **[UNHANDLED]** — looks like "get user setting by key". Observed keys:
    `FirstTimeFlag`, `AchievementCache`. Empty reply tolerated so far, but a real implementation
    should probably return the value or a proper NOT_FOUND, not zero fields — candidate suspect if
    Seasons-related state is fetched this way somewhere.
  - `0x000B` (`DATA`, `KEY`, `UID`) **[UNHANDLED]** — "set user setting by key", mirrors `0x000A`.
  - `0x000C` (no fields) **[UNHANDLED]** — unknown.

- **`0x0001` Authentication** (`AUTH_COMPONENT`)
  - `0x000A` login **[CONFIRMED]** → `LoginResponse` (bisected via `config.login_groups`)
  - `0x001E` getAccount **[HYPOTHESIS]** → `GetAccountResponse` — 16-field shape read from EBOOT's
    reflection table (function `0x00C75874`), but no code building a *real* response was found
    near it, only reflection metadata, so the field values themselves are guessed.
  - `0x0014` (`CPWD`, `CTRY`, `DOB`, `LANG`, `MAIL`, `OPT1`, `OPT3`, `PASS`, `PRNT`) **[UNHANDLED]**
    — looks like account creation / parental-consent data, all empty strings in the observed
    capture (opt-in dialog path). Client proceeds fine.
  - `0x0020` (`BUID`, `EPSN`, `EPSZ`, `FLAG`, `GNLS` = list of game names e.g. `FIFA17PS3BoxContent`,
    `FIFA17PS3`, `FIFA16PS3`, `FIFAWC14PS3`) **[UNHANDLED]** — looks like an entitlement/ownership
    check across a family of related titles. Client proceeds fine with an empty reply.
  - `0x00F2` (`CTRY`, `PTFM`) **[UNHANDLED]** — small country/platform check, sent a few times.

- **`0x0007` Stats** (`STATS_COMPONENT`) — standard EA Blaze SDK component, not FIFA-specific.
  Confirmed against `Mk0M/Impulsum14` (github.com/Mk0M/Impulsum14), an open-source Blaze backend
  for FIFA 14 PC on an older Blaze 13 SDK but the *same* SDK family/TDF layout — used as the
  "Rosetta stone" for tag names and command numbers that can't be derived from EBOOT alone.
  - `0x0004` getStatGroup **[CONFIRMED shape]** → `StatGroupResponse` (`CNAM/DESC/ETYP/KSUM/META/
    NAME/STAT`), real group name (`NAME`, e.g. `H2HSeasonalPlay`) echoed back, everything else
    empty since we have no real stat definitions.
  - `0x000F` getKeyScopesMap **[CONFIRMED shape]** → `KeyScopes { KSIT: map<string, KeyScopeItem> }`,
    sent as an empty map. **Added 2026-09-24** — previously fell through to the fully-empty
    fallback (not even a `KSIT` field), which is almost certainly wrong for a typed response.
    Confirmed on the wire afterwards, but did **not** fix the Cup Match freeze.
  - `0x0010` getStatsByGroupAsync **[CONFIRMED pattern]** → empty `Reply`, then a separate
    `GetStatsAsyncNotification` (`0x0007`/`0x0032`, `msg_type=Notification`) carrying
    `KeyScopedStatValues { GRNM, KEY, LAST, STS{AGGR,STAT}, VID }` with the real group name (tracked
    server-side across the connection as `last_stat_group`, since this request's own `NAME` field
    arrives empty) and empty stat lists. This two-step Reply-then-Notification pattern is how
    Impulsum14's server implements this exact RPC (`NotifyGetStatsAsyncNotificationAsync`).

- **`0x000F`** (component identity **not confirmed** — candidate: Association Lists, a stock Blaze
  component in other SDK versions)
  - `0x0002` (`FLAG`, `MGID`, `PIDX`, `PSIZ`, `SMSK`, `SORT`, `SRCE` objid, `STAT`, `TARG` objid,
    `TYPE`) **[UNHANDLED]** — `TYPE` decodes as a packed 4-char string: `1919905645 = "room"`. Looks
    like "get/list association list of type ROOM" (friends list, room list, or similar). Worth
    identifying properly — this and `0x08C9` below are the two unexplained components seen in the
    observed flow.
  - `0x0005` (same struct shape, `FLAG=0`, no readable `TYPE`) **[UNHANDLED]**.

- **`0x000A`** (component identity not confirmed) — `0x0001`, zero-field request, **[UNHANDLED]**.

- **`0x0015`** (component identity not confirmed) — `0x000A` (`UPDT` varint), **[UNHANDLED]**.

- **`0x08C9`** (component identity **not confirmed**, likely FIFA-specific since it's not in
  Impulsum14/any generic Blaze SDK component list) — `0x0001` and `0x0002`, both zero-payload
  requests, both **[UNHANDLED]**. Sent right after `getAccount` succeeds, *before* the persona
  lookup. Not yet identified via EBOOT static analysis — good candidate to check next, since it's
  FIFA-specific and untouched so far.

- **`0x7802` UserSessions** (`USER_SESSIONS_COMPONENT`)
  - `0x0032` lookupUsersByPersonaNames → sends **only** `ULST = LIST<UserData>` (tag name
    `ULST` confirmed via Impulsum14's `UserDataResponse.cs`; the `UserData` struct shape
    `EXBB/EXID/ID/NAME/NASP/FLGS` read directly from EBOOT reflection data next to
    `UserIdentification`'s). **Update 2026-09-26:** used to also send a shotgun fallback of
    `USER/VALU/DATA/LIST` tags with the older `UserIdentification` shape "just in case" (left
    over from before `ULST` was confirmed). In the two-RPCS3-instance multiplayer test this
    caused a deterministic PPU access violation on the client's `FEThread`, always at the exact
    same address (`0x2ef598`, `lwz r3,0x90(r31)` — reading a fixed field offset off a
    null/bad object pointer), reproduced across 4 separate runs regardless of what content we put
    in `EXBB`/`EXID`. The crash only appeared once the response started carrying another player's
    (non-self) identity data, so the redundant `USER/VALU/DATA/LIST` fields — never needed once
    `ULST` was confirmed — are the prime suspect: some reflection/decoder path in the client likely
    picked one of them up as a different, unintended structure. Removed the shotgun fallback
    entirely; not yet re-verified live.
  - `0x0014` updateNetworkInfo **[CONFIRMED pattern]** → empty `Reply` + `UserSessionExtendedDataUpdate`
    notification (`0x0001`). Observed **twice** per connection: once right after login with
    placeholder NAT info (`NATT=5`), once later after QoS probing on port 17502 with real results
    (`NATT=1` in the last capture).
  - Notifications, all **[CONFIRMED]** patterns (sent unprompted after login):
    `UserAuthenticated` (`0x0008`), `NotifyUserAdded` (`0x0002`), `UserUpdated` (`0x0005`),
    `UserSessionExtendedDataUpdate` (`0x0001`).

## Playing with a second player (setup guide, verified 2026-09-28)

Everything below was needed to get two real RPCS3 instances on two physical PCs (connected over a
VPN such as Radmin) to both log in to one `fifa17srv` instance. `<HOST_VPN_IP>` is the VPN address
of the machine running the server.

**Host:** `config.json` with `{"bind_address": "0.0.0.0", "blaze_advertise_host": "<HOST_VPN_IP>"}`,
then `update_and_run.ps1`. Only the host runs the server.

**Every client (including the host):**
1. **RPCS3 → right-click game → Configuration → Network → "IP/Hosts switches"** must map the three
   EA hostnames. This field is RPCS3's own resolver and **takes precedence over the Windows hosts
   file**. A stale `winter15.gosredirector.ea.com=127.0.0.1` here silently sends the client to
   itself. On the second player it must look like:
   `winter15.gosredirector.ea.com=<HOST_VPN_IP>&&rl.data.ea.com=<HOST_VPN_IP>&&pin-river.data.ea.com=<HOST_VPN_IP>`
2. **Nucleus OAuth is hard-wired to `127.0.0.1:8081`.** The client opens that connection with no DNS
   lookup at all (confirmed in RPCS3.log: no `DnsHook` line, direct `Attempting to connect on
   127.0.0.1:8081`), and changing the `redirect_uri` we send in the `IdentityParams` config does not
   change it. On any machine that is **not** the host the connection is refused, the client never
   reaches our fake Nucleus, never sends the login request, and ends up on the "EA servers are not
   available" screen. Fix on the second player (admin PowerShell):
   `netsh interface portproxy add v4tov4 listenaddress=127.0.0.1 listenport=8081 connectaddress=<HOST_VPN_IP> connectport=8081`
   (undo with `netsh interface portproxy delete v4tov4 listenaddress=127.0.0.1 listenport=8081`).
3. Restart the game after changing any of this. RPCN sign-in in RPCS3 is separate from all of the
   above and goes over the normal internet, not the VPN.

**Debugging tip:** the per-connection capture files in `logs/captures/` and RPCS3's own
`RPCS3.log` are both needed; the combined console output interleaves lines from concurrent
connections (an apparent "S->C 21 bytes" after an invite was just another connection's write).

## Status after two-player testing (2026-09-28) — where this project stopped

**Works with two real players:** redirector, PreAuth, fake Nucleus, login, user/session
notifications, Stats group burst, and `GameManager::createGame` — both players' `createGame`
requests are parsed and answered, and `NotifyGameSetup` is written to the other player's live TCP
connection without error.

**Does not work:** the invited player never sees the invite and the host stays on "Sending match
invite and creating a game session. Please wait...". Both clients also show the red "PRESS THE START
BUTTON TO RE-CONNECT" banner and **repeatedly perform full reconnects (redirector → login from
scratch) every ~50–90 s** — in the server captures and in the client's RPCS3.log (`bind 0.0.0.0:3659`,
`ECONNRESET`/`ENOTCONN`, `sceNpLookupInit`/`Term` cycles). This is most likely the same unresolved
client-side watchdog described under "Current blocker" and probably explains the lost invite.
**Hypotheses tested on 2026-10-08 (both players connected, one click on "Play Match"):**
1. *Fixed client-side reconnect timer* — **refuted**: an idle client keeps one Blaze connection for 2+ min
   (only 20 s frame pings, no new redirector/login), in both server captures and RPCS3.log.
2. *Invite lost on a stale `_PLAYERS` entry after a reconnect* — **refuted**: with a stable connection per
   player the registry held both players (`['odyniec', 'odyniec1']`), `createGame` wrote the 399-byte
   `NotifyGameSetup` to the invitee's live socket without error, nobody reconnected before or after, yet the
   host stayed on "Sending match invite..." and the invitee saw nothing. (The unexplained "S->C 21 bytes" line
   is just the 5-byte-payload `CreateGameResponse` frame, logged after the handler's own notes.)
   A real race was fixed anyway: a closing old connection could unregister a newer connection of the same
   player (now compares stream identity).

**What the clients' RPCS3.log shows around the click:** the host calls `sceNpBasicSetPresence` with a
**64-byte** payload when it enters the Play Match flow (~19 s *before* `createGame`); the invitee's RPCS3
receives it as `NPHandler: basic_event: event:1, from:<host>, size:64`, then sets its own 20-byte presence.
No `sceNpBasicSendMessage`/invite calls appear at all. After `createGame` the host sends nothing further
(no presence update, no new Blaze request) — it is waiting for something we do not send. `Messaging`
(component `0x000F`) requests seen so far are `fetchMessages`/`getMessages` of type `'room'` (chat rooms), not
invites. Working theory: the invite is carried by the RPCN presence data (which should receive the game id
after `CreateGameResponse`), and the host client stalls before updating it because some expected Blaze
`GameManager` notification (e.g. `NotifyGameStateChange` / `NotifyGamePlayerStateChange`, or a different
`NotifyGameSetup` shape/context) is missing or wrong. Next steps: experiment with those notifications, and
dump the 64-byte presence payload (RPCS3 debugger on `sceNpBasicSetPresence`) to see what it encodes.

Note on command IDs: Impulsum14's component/command numbering (FIFA 14 / older Blaze) does **not**
match FIFA 17 (e.g. Authentication login is `0x000A` on the wire, not `0x28`), so unknown commands
such as `0x0001/0x0046` cannot be named from it.

Contributions and pointers are welcome, in particular from anyone who has traced the client's
reconnect state machine or knows the FIFA 17 / Blaze 15.1 command tables.

## Two-player status, 2026-10-08 (what is verified)

Verified with two RPCS3 instances over Radmin VPN (host log + friend log + server log):

* Both clients log in and stay connected; no reconnect loops.
* **`lookupUsersByPersonaNames` crash fixed.** The reply element type is
  `UserData = {EDAT, FLGS, USER{UserIdentification}}` (Impulsum14 `UserData.cs`, and the
  reflection table in the executable). The old flat `{EXBB,EXID,ID,NAME,NASP,FLGS}` reply left the
  client's user-name buffer empty; the lookup helper (0x2f12bc) then returned NULL and the caller
  (0x2ef520) dereferenced it (`Access violation reading location 0x90` at 0x2ef598). Do not set
  `lookup_users_empty_reply` any more.
* The client uses our lookup result as the player reserved in `createGame` (`PLJD.PLDL`).
* `createGame` arrives, we answer + push `NotifyGameSetup` (+ player/game state notifications)
  to both players. **Still open:** after that the host client sends nothing more and makes no
  `sceNp` call (no `sceNpBasicSendMessage`), it only polls friend presence every 10 s; the friend
  client does not react. So some event the host waits for ("Sending match invite and creating a
  game session") is still missing or malformed. Candidates: `NotifyGameSetup` field content,
  mesh/network setup (`updateMeshConnection`, `NotifyPlayerJoinCompleted`), or invite delivery.
* Roster `PID` must be the BlazeId (= `UserIdentification.ID`), not the persona id (fixed).

Update (later the same evening, verified in server + RPCS3 logs):

* `NotifyGameSetup` now carries host info (`THST`/`PHST`/`HSES`/`ADMN`) and echoes the client's
  `createGame` attributes; rosters are built per viewer (a client knows itself as `LOCAL_USER_ID`).
* After the setup both clients open UDP 3659 / 9999 and send `updateMeshConnection` (0x1D, `STAT=2`)
  and `finalizeGameCreation` (0x0F). We answer with `ACTIVE_CONNECTED` + `NotifyPlayerJoinCompleted`
  + `NotifyPlatformHostInitialized`. Still **no UDP traffic between the clients** and no UI progress.
* Rosters used loopback/LAN addresses; they now use the peer's Radmin IP (the address the client
  connects to the server from).
* Experiment (`gm_initial_player_state`, default 2): players start as `ACTIVE_CONNECTING` so the
  SDK has a reason to open peer connections; plus `NotifyPlayerJoining` to the host.

Next step needs the client's `GameManager` notification handlers disassembled (string
cross-references into the executable did not resolve with simple TOC scans).

## Online Friendlies: GameManager `createGame` (2026-09-26)

**Setup used for this testing:** the user has a friend with their own physical PC and their own
legitimate FIFA 17 disc/ISO. Both machines run RPCS3 and connect over **Radmin VPN** (already
installed; host VPN IP written below as `<HOST_VPN_IP>`) instead of a VM — an earlier attempt to run two RPCS3
instances on one machine inside a VirtualBox VM was abandoned after VirtualBox's virtual GPU could
not create the OpenGL context RPCS3 needs (`Failed to create OpenGL context`, then a full VM freeze
even with 3D Acceleration + `VBoxSVGA` enabled). Both players' clients connect to the **same**
`fifa17srv` instance (run by the host, `blaze_advertise_host` set to the Radmin VPN IP so the
friend's hosts-file redirect reaches it).

**Confirmed working, end-to-end, with two distinct real clients:** full Blaze handshake for both
players (redirector → PreAuth → login → UserSessions → Stats), each player's client shows the other
in the "Online Friendlies" friends list (this friends-list presence comes from **RCPN**, i.e. the
real PS3 online emulation layer, not from anything our Blaze server sends), "Current Season" stats
screen renders for both. Clicking a friend → "PLAY MATCH" opens Match Settings (Half Length, etc.)
and an all-time stats comparison screen; clicking through that sends the request described below.

### Real `createGame` request shape (confirmed on the wire, differs from Impulsum14)

`GameManager::createGame` is component `0x0004`, command `0x0001` (confirmed via Impulsum14's
`Components/GameManagerBase.json`: component Id 4, method Id 1, request `CreateGameRequest`,
response `CreateGameResponse`). **The real FIFA17 request does *not* match Impulsum14's
`CreateGameRequest.cs` field layout** (that reference is FIFA 14 PC, an older Blaze 13 SDK build —
plausibly a different SDK minor version for this particular request shape). The real wire capture
looks like this instead (top-level fields, abbreviated):

```
CMGD <struct>            -- NOT in Impulsum14's CreateGameRequest.cs at all
  GGTY <varint>
  GVER <string>           -- e.g. 'qa-only' -- this is GameProtocolVersionString, NOT top-level VSTR
  OSID <varint>
  PNET <union>            -- host's own NetworkAddress (IpPairAddress, disc=2)
  XNET <union disc=127>   -- unset
GCTR <string>
GMCD <struct>             -- NOT in Impulsum14's CreateGameRequest.cs at all
  ATTR <map string->string>   -- OSDK_gameMode, fifaHalfLength, fifaMatchupHash, etc.
  CRIT <map string->string>
  GMRG <varint>
  GNAM <string>            -- game name -- lives HERE, not top-level
  GSET <varint>            -- bitflag combination (observed 1060), not a simple enum
  NTOP <varint>            -- network topology -- lives HERE, not top-level
  PMAX <varint>             -- max players -- lives HERE, not top-level
  PMIN <varint>
  PRES <varint>
  QCAP <varint>
  RNFO <struct>
  STMN <string>
  VOIP <varint>
GTYP <string>              -- e.g. 'gameType20'
GURL <string>
NRES <varint>
PCAP <list of varint>
PGID <string>
PGSC <blob>
PLJD <struct>              -- NOT in Impulsum14's CreateGameRequest.cs; carries ONLY the HOST's
  BTPL <objid>              own UserIdentification (a self-entry, not the invited friend's identity)
  DFRL <string>
  GENT <varint>
  PLDL <list of struct>     -- one element observed: {IREP, RLNM, USID: UserIdentification}
  SLOT <varint>
  TID <varint>
  TIDX <varint>
TIDS <list of varint>
```

**Important, don't re-derive this the hard way:** `PLJD.PLDL` carries only the *creator's own*
identity, not the invited friend's — so this request does **not** tell the server who to invite.
Whatever decides the invite target must happen client-side before this request is even sent (most
likely from the friends-list/RPCN selection the player made on the "PLAY MATCH" screen), and the
server has no way to read it off this wire message.

### Server implementation added this session

`fifa17srv/blaze.py` now implements the `GameManager::createGame` handler (previously fell through
to the generic empty-Reply fallback, which is the confirmed root cause of the earlier hang on
"Sending match invite and creating a game session. Please wait..."):

- **`CreateGameResponse {GID}`** — the *only* field, confirmed 1:1 against Impulsum14's
  `CreateGameResponse.cs` (a `UInt32`).
- **`NotifyGameSetup {GAME, PROS, QUEU, REAS}`** (component `0x0004`, notification id `0x0014`/20)
  — shape confirmed against Impulsum14's `NotifyGameSetup.cs`, `ReplicatedGameData.cs`,
  `ReplicatedGamePlayer.cs`, `GameSetupReason.cs`, `NetworkAddress.cs`/`IpPairAddress.cs`/
  `IpAddress.cs`. `REAS` disc=0 (`DatalessSetupContext`) is sent to the game's creator; disc=2
  (`IndirectJoinGameSetupContext`) to anyone invited without having called `createGame`/`joinGame`
  themselves — these are Blaze-protocol-valid union variants, the *choice* between them for a given
  recipient is server-side game-flow logic, not a wire-format guess.
- **Cross-connection player registry** (`_PLAYERS`, a module-level dict keyed by persona name,
  protected by `_PLAYERS_LOCK`): each connection registers itself on login (storing its `stream`,
  a per-connection `send_lock`, and the IP/port harvested from `UserSessions::updateNetworkInfo`).
  This is new — until this session, `blaze.py`'s `handle()` had **zero shared state across
  connections**, so one player's `createGame` had no way to reach another player's live connection
  thread to push a notification. `_send_frame()` looks up a persona name and pushes a frame from
  any thread, using that connection's own `send_lock` so pushed notifications never interleave with
  that connection's own replies mid-frame.
- **Who gets invited, since the request itself doesn't say:** the server invites *every other
  currently-logged-in player* — in this project's actual usage (exactly two real people, host +
  one RPCN friend) that's always exactly one person. This is a deliberate, documented server-side
  design choice for this project's known 2-player use case, not a guess about what the protocol
  field layout means.
- `UserSessions::updateNetworkInfo`'s handler now also extracts the client's own `IP` (not just
  `PORT`/`MACI` as before) from the `INIP` sub-struct and stores it in the player registry, so the
  `ReplicatedGamePlayer`/`ReplicatedGameData` sent in `NotifyGameSetup` can carry real (Radmin VPN)
  IP:port pairs for P2P, not just loopback placeholders.

**Tested live (solo, host only — see "Still needs testing with both players online" below):** the
server correctly builds and sends `CreateGameResponse GID=1` followed by `NotifyGameSetup` (host,
`DatalessSetupContext`, roster of 1) — confirmed byte-correct via the server's own capture log and
a local `tdf.decode`/`pretty` round-trip test. Because only the host was connected to the fake
Blaze server during this test (the friend wasn't running FIFA17 at the time), the "invite the other
player" branch never exercised — the log correctly showed
`UWAGA: brak innych zalogowanych graczy do zaproszenia do gry <id>` ("no other logged-in players to
invite to game `<id>`").

**Even with this fix, the client did not visibly leave the "Please wait..." screen in this solo
test.** Two live tests (with slightly different `NotifyGameSetup` payload sizes as the field-parsing
was corrected mid-session) both showed the same result: the wait screen stayed up indefinitely after
the server's response, per the user's direct confirmation ("gra dziala normalnie caly czas na oknie
sending game invite" — the game just keeps sitting on that screen).

### New theory: RPCN friend-presence polling, not (only) a Blaze issue

Grepping the RPCS3 native log (`RPCS3.log`) from the same test run shows the client's `sceNp` layer
calling, roughly once per second in a background thread (correlated with the periodic `PERF: CPU
Usage` log lines):

```
sceNp: sceNpBasicGetFriendPresenceByIndex(index=0, user=*0x14a2af08, pres=*0x14a30368, options=0)
```

This is a **real RPCN (PS3 online emulation) presence check** for the friend at index 0 — separate
infrastructure entirely from our fake Blaze server, which cannot see or influence it. Since the
friend wasn't actually connected to RPCN during this specific solo test, this check most likely
keeps reporting "offline"/no data. **Working theory, not yet confirmed:** the "Sending match invite
and creating a game session" screen may be gated on *both* (a) a correct Blaze
`createGame`/`NotifyGameSetup` round-trip (now implemented) *and* (b) RPCN confirming the invited
friend is actually online and reachable — which is entirely outside this project's fake-Blaze-server
control and can only be satisfied by the friend genuinely being online via RPCN at the same time.

**This has not been tested with both players simultaneously online yet** (the friend wasn't
available during this session's live tests). That is the concrete next step before drawing further
conclusions about whether the `createGame`/`NotifyGameSetup` implementation actually unblocks the
flow — solo testing can only prove the server-side code runs without crashing/error, not that it's
sufficient.

**Next steps, in order:**
1. Test with both players online simultaneously (both in RPCN, both connected to the same
   `fifa17srv` instance) and watch whether the invited player's client receives
   `NotifyGameSetup (zaproszony, IndirectJoinGameSetupContext)` and whether either client leaves the
   "Please wait" screen.
2. If it's still stuck with both players online, the RPCN-presence theory above is falsified (or at
   least insufficient) and the investigation goes back to the Blaze wire level — e.g. checking
   whether the client expects `GameManager::joinGame` to be called somehow, or a different
   `NotifyGameSetup`/`GameSetupReason` variant, or additional fields in `ReplicatedGameData` we
   currently omit (only a subset of `ReplicatedGameData.cs`'s ~34 fields are populated today).
3. If the friend's presence really is the gate, there is likely nothing to fix in `fifa17srv`
   itself for this specific screen — it would mean this part of the flow was already correctly
   implemented and just needs a genuinely-online second player to proceed, at which point the next
   question becomes what happens *after* both clients leave this screen (probably `joinGame`, then
   real P2P connection setup using the `PNET`/`ReplicatedGameData.HNET` addresses already being
   exchanged).

## Current blocker: "Loading Seasons information..." freeze

**Repro:** log in (Continue → tick checkbox → OK) → FIFA17 Online menu → **Play Cup Match** →
stadium loading screen → stuck forever on "Loading Seasons information...". (**PLAY SEASON**
instead shows a blank screen — a different, also-unfixed failure mode, not yet captured/analyzed
separately.)

**What the wire capture shows, most recent run (2026-09-24, after the `getKeyScopesMap` fix):**
everything from login through the full Stats burst completes cleanly, in order, with correct data
and no error/disconnect:

1. `lookupUsersByPersonaNames` (`0x7802/0x0032`) → our UserData reply
2. `Stats::getStatGroup` (`0x0007/0x0004`, `NAME='H2HSeasonalPlay'`) → `StatGroupResponse` with the
   real group name echoed
3. `Stats::getKeyScopesMap` (`0x0007/0x000F`) → `KeyScopes{KSIT: {}}`
4. `Stats::getStatsByGroupAsync` (`0x0007/0x0010`) → empty `Reply` + `GetStatsAsyncNotification`
   with the real group name, empty stat lists, `LAST=1`
5. Two rounds of QoS HTTP probing on port 17502 (`/qos/qos`, `/qos/firewall`, `/qos/firetype`) —
   all `200 OK`
6. `UserSessions::updateNetworkInfo` (`0x7802/0x0014`) sent a **second** time by the client, now
   with real NAT-detection results — answered with empty `Reply` + `UserSessionExtendedDataUpdate`
   notification

**After step 6, the client sends nothing further at all.** This is the key fact: it is not that a
later request gets mishandled — the client goes completely silent while the loading spinner keeps
spinning. That points to one of:

- **(a)** the client waiting on some other async Notification we never send, from a component
  whose role is still unidentified (`0x08C9` and `0x000F` above are the prime suspects, since
  they're the only pieces of the observed flow that are still a complete black box);
- **(b)** a client-side check against the *content* of one of our replies failing silently — e.g.
  something subtly wrong in `GetStatsAsyncNotification`'s byte layout that isn't visible just from
  the Python-level field list (Impulsum14 itself has never been observed on real FIFA17 wire
  traffic for this exact packet, only cross-referenced structurally);
- **(c)** the freeze has nothing to do with Stats at all, and is really about `0x08C9`/`0x000F`
  getting empty fallback replies earlier in the flow.

**Update 2026-09-24, second data point:** captured the "Play Cup Match" path again (same server
run, `getKeyScopesMap` fix in place). The Blaze wire trace is byte-for-byte identical in shape to
the first capture above. New information this time came from two places that aren't Blaze traffic
at all:

- **The stadium loading screen shows a banner: "⊗ PRESS THE START BUTTON TO RE-CONNECT"** (red
  no-globe icon, top right). This is the first concrete UI-level signal we've gotten — the client
  itself believes it has lost its online connection, it's not just a spinner with nothing behind
  it.
- **The raw Blaze TCP connection is provably still alive and healthy** the whole time: frame-level
  pings (`component=0x0000 command=0x0000 msg_type=4`, i.e. `Ping`/`PingReply` at the transport
  level, distinct from `Util::ping`) keep arriving every ~20s and get answered normally, for over
  a minute after the client goes silent at the application level. So this is **not** a dropped
  socket or a raw timeout — whatever decides to show "RE-CONNECT" is a higher-level client-side
  check.
- **`GameManager` (the standard Blaze component for creating/joining a game session, `0x0004` in
  most SDK versions) never appears anywhere in any capture we have**, including this one. Every
  other component in the observed flow up to this point (Util `0x0009`, Auth `0x0001`, Stats
  `0x0007`, UserSessions `0x7802`, the two unidentified `0x000F`/`0x08C9`) has shown up at least
  once. If Cup Match needs to create or join a game session, the client should eventually send
  *something* to a GameManager-shaped component — it never does, which means whatever it's
  waiting on happens **before** it would even attempt that.
- The RPCS3 native log around the same time shows the client's PSN layer doing an unrelated
  `sceNpCommerce2` store/DLC product-info check (`GetProductInfoListStart`, 26 product IDs) that
  fails with `SCE_NP_COMMERCE2_ERROR_INVALID_ARGUMENT` — this is PS3's real Store API, not Blaze,
  and RPCS3 marks its handler `TODO` (unimplemented stub). Almost certainly unrelated to the
  freeze (a real PS3 would talk to Sony's actual store for this), but noted here in case it turns
  out not to be.

**Working theory, unconfirmed:** the "RE-CONNECT" banner is most likely the client's own
watchdog/precondition check for entering online play — something it expects to have (a piece of
`UserSessionExtendedDataUpdate` state, a Notification from a component we haven't identified, or
a QoS/NAT result it doesn't like) never arrives or never satisfies it, so after some internal
timeout it gives up and shows the generic "lost connection" UI rather than proceeding to whatever
would call GameManager. This is different from "the server sent a malformed reply" — it looks more
like "the client is waiting for something the server never sends at all."

**Update 2026-09-24, EBOOT static analysis of GameManager (component `0x0004`, confirmed via
Impulsum14's `GameManagerBase.json`):** `find_requests.py` (backward-scans for `li r5,imm` /
`li r6,imm` right before each call to the request-send function `0x00CFAD54`) found **190 total
call sites in the whole EBOOT, and NOT ONE of them has a resolvable r5 (component)** — this
compiler doesn't load the component id via an immediate right before the call, for any component,
so this heuristic can't be used to prove or disprove a specific component is called. It's still
useful for r6 (command): clusters of known commands appear together (e.g. `0x01CA2E4C`-`0x01CA338C`
contains commands `0x0004/0x000F/0x0010`, matching Stats' `getStatGroup/getKeyScopesMap/
getStatsByGroupAsync` — the RPC stub functions for a component appear to be laid out contiguously
in the binary), but no such cluster of GameManager-shaped command values (createGame=1, joinGame,
etc., per Impulsum14) was found anywhere in the 190 call sites.

Confirmed via `find_str_refs.py` that GameManager **is** compiled into the client: literal strings
`createGame`, `joinGame`, `NotifyGameSetup`, `cancelMatchmaking`, `startMatchmaking`, etc. all
exist in EBOOT (`0x01FA90B8` onward), and `0x00C92400`-`0x00C92600` is a `getCommandName()`-style
switch (command number in r3 → name string) covering GameManager's full ~40-command RPC set. This
proves the component's code exists and isn't dead-code-eliminated, but a name lookup table doesn't
prove where (or whether) the client actually *calls* `createGame`/`joinGame` — that would require
finding callers of this command-name function or tracing the actual RPC dispatch, neither done yet.

**Tried and disproven:** changed `qos.py`'s `/qos/firetype` response from `2` to `0` (Open vs.
Strict in typical EA/DirtySDK NAT-type numbering), on the theory that the client refuses to attempt
P2P after concluding it has a bad NAT type. Confirmed the change **does** reach the client and
changes its own computed `NATT` value in the next `updateNetworkInfo` (was `1`, became `4`) — so
the QoS responses genuinely feed into the client's NAT-type math — but the Cup Match freeze was
**unaffected**: same "Loading Seasons information..." hang, no GameManager traffic, connection
otherwise unchanged. Don't retry this specific value swap; if NAT-type theories come up again,
the fact that `NATT` shifted 1→4 for a firetype 2→0 change is worth using to reverse-engineer what
the field actually encodes before guessing another value blindly.

**Tried and disproven:** `build_ext_data_update()` used to send `ADDR` as `UNSET` in the
`UserSessionExtendedDataUpdate` notification that follows `updateNetworkInfo`, even though the
client's own `updateNetworkInfo` request always shows `EXIP` as all-zero (it doesn't know its own
external address and appears to be asking for it back). Changed it to send a real
`NetworkAddress::IpPairAddress` (union disc=2, tag `VALU`, fields `EXIP`/`INIP`/`MACI` — shape
confirmed on the wire from the client's own request) echoing back the client's reported local
`INIP`/`MACI`/`PORT` as both `EXIP` and `INIP` (everything is loopback here, so "external" ==
"internal"). **This worked exactly as designed and is worth keeping**: the very next
`updateNetworkInfo` the client sends shows `EXIP: IP=127.0.0.1, PORT=3659` — the client accepted
and echoed back exactly the address we gave it. But the Cup Match freeze was **still unaffected**:
identical "Loading Seasons information..." hang immediately after. So a missing/unset session
address was not the (sole) gate either. Keep this fix (it's more correct than `UNSET` regardless,
and future components may depend on it), but stop looking at `UserSessionExtendedData`'s `ADDR`
field as the blocker.

**Update 2026-09-24, third data point + component IDs resolved, BPS/QDAT fix tried:** user
clarified the "freeze" is actually a **persistent spinning loading icon in the corner of the
screen**, not a hard hang — raw frame-level pings keep succeeding indefinitely, confirming the
client is alive and just idling/waiting, not crashed. Checked the RPCS3 native log (`sceNp*`
calls) around and after the freeze point: **no `sceNpMatching2`/signaling calls at all** — only
trophies, the known/unrelated `sceNpCommerce2` store-check failures, and a periodic
`sceNpBasicSetPresence` heartbeat. This rules out "client is stuck waiting on native PSN
matchmaking" as a cause — the blocker is confirmed to be Blaze-level, not RPCS3/PSN-level.

Resolved two previously-unidentified component IDs against Impulsum14
(`Components/*ComponentBase.json`, cross-checked against `PostLoginComponents.cs`, a working
reference server's post-login handler set):
- `0x000A` (10 decimal) = **CensusData** (`SubscribeToCensusDataAsync`) — our empty-Reply fallback
  is *correct* (Impulsum14's reference handler also just returns `EmptyMessage`).
- `0x000F` (15 decimal) = **Messaging** (`fetchMessages`=2, `getMessages`=5, confirmed by command
  numbers matching exactly) — NOT "Rooms" as previously guessed (that guess conflated hex/decimal).
  Empty-Reply fallback is correct here too (reference handler returns `EmptyMessage`).
- `0x0015` (21 decimal) = **Rooms** (`selectViewUpdates`=10, matches the observed `UPDT=1` field
  from `SelectViewUpdatesRequest.cs`) — empty-Reply fallback is *also correct*
  (`RoomsComponent.SelectViewUpdatesAsync` in the reference returns `EmptyMessage` too).
  So the earlier "`TYPE=room`" lead on component `0x000F` was based on a hex/decimal mixup and
  is a dead end; Rooms subscription itself needs no reply body.

None of these three were the blocker. But comparing our `updateNetworkInfo` handling against
`UserSessionExtendedData`'s full field list (`Address.cs` set: `ADDR`/`BPS`/`CMAP`/`CTY`/`CVAR`/
`DMAP`/`HWFG`/`PSLM`/`QDAT`/`UATT`/`ULST`) turned up a real gap: the client's second
`updateNetworkInfo` sends us `NLMP` (its own measured ping-site latencies, e.g. `'ea-sjc':
1601961990`) and `NQOS` (`DBPS`/`NATT`/`UBPS`, self-computed), but our
`UserSessionExtendedDataUpdate` notification only ever echoed `ADDR` — never `BPS`
(`BestPingSiteAlias`) or `QDAT` (`Util::NetworkQosData`). EA Sports' "connecting" spinner is
plausibly waiting on the server to confirm the best ping site / finalize QoS before it can
dismiss. **Tried (2026-09-24, not yet verified live):** `blaze.py`'s `updateNetworkInfo` handler
now parses `NLMP`/`NQOS` from the client's request and `build_ext_data_update()` echoes back
`BPS` (lowest-latency key from `NLMP`) and `QDAT` (mirroring the client's own `DBPS`/`NATT`/
`UBPS`) in the `UserSessionExtendedDataUpdate` notification. Unit-tested the encoder locally
(round-trips through `tdf.decode`/`pretty` correctly, shape matches `UserSessionExtendedData.cs`
+ `NetworkQosData.cs`). **Tested live: disproven.** The notification correctly grew from 24B to
128B (confirming `BPS`/`QDAT` were actually sent, `BPS='ea-sjc'`, `QDAT` mirroring the client's
own NATT=4/DBPS=0/UBPS=0), but the client's behavior was byte-for-byte identical to before: same
silence immediately after `updateNetworkInfo #2`/msg_num=40, same raw-ping-only tail. Keep the
BPS/QDAT fields (more correct regardless, matches the confirmed TDF shape) but this is not the
gate either.

Also ran `disasm_range.py` over `0x00C92300`-`0x00C92600`, confirming the GameManager
`getCommandName()`-style dispatch is a big `cmpwi`/`beq` chain covering the full command set
(`addAdminPlayer` through `setGameEntryCriteria` and beyond) with each branch loading a literal
string pointer — this is purely a name-lookup table (probably for logging/asserts), not evidence
of the RPC path being invoked. No new leads from this.

**Tried and inconclusive: literal search for component `0x08C9` as an `li`/`ori` immediate.**
Computed the PPU opcodes for `li rX,0x8C9` / `ori rX,r0,0x8C9` for X=3..10 and scanned the raw
EBOOT.ELF bytes. Important gotcha: raw file offsets are NOT the same as the virtual addresses
`disasm_range.py` takes — this EBOOT's first (and largest) `PT_LOAD` segment has
`vaddr - offset = 0x10000`, so a raw byte-search hit at file offset `X` corresponds to VA
`X + 0x10000` (confirmed via the ELF program headers: `phoff=0x40`, `phentsize=56`, `phnum=8`,
two real `PT_LOAD` segments both with that same `0x10000` delta, plus three degenerate
zero-size `PT_LOAD` entries to ignore). After correcting for this, found one genuine `li r4,
0x8C9` at VA `0x002991E0`, but it's followed by `bl 0x01A148A0` — not the known Blaze send
function (`0x00CFAD54`) — so it doesn't look related. Two other raw-offset hits
(`0x0030AA08`/`0x0030AAEC` → corrected VA `0x0030BA08`/`0x0030BAEC`) turned out to be false
positives on reinspection (the bytes at the corrected VA are not `38 80 08 C9` at all — likely a
bug in the ad-hoc scan script from this session, not re-derived). **Do not trust file-offset hits
from a quick byte-scan without applying the `+0x10000` VA correction and verifying with
`disasm_range.py` before acting on them.** This technique did not find component `0x08C9`'s
load site; abandon it in favor of a different approach (e.g. `find_str_refs.py`-style TOC/pointer
reference tracing, or capturing a different client flow that might reference it more directly).

**Next investigation ideas, in rough priority order:**

1. Find the actual **caller(s)** of the GameManager `getCommandName()` function at
   `0x00C92400`-`0x00C926C0ish` (its entry is somewhere before `0x00C92400`, likely right after
   the `cmpwi`/`beq` chain starts — hasn't been located precisely yet). Whatever calls this to
   build a display/log string for a *specific* command number would show us which command (if any)
   the client is actually working with when it decides to give up and show "RE-CONNECT" — this is
   more promising than guessing at r5 values that `find_requests.py` can't resolve here.
2. Identify the real `GameManager`-equivalent component in this SDK/build and what it expects to
   receive *unprompted* (server-initiated) versus what it expects the client to send — check
   whether Impulsum14 has a server-side "auto-invite to game" or "session ready" notification that
   fires without a matching client request, since the client here never asks for one.
3. Identify component `0x08C9` (2249 decimal) in EBOOT via static analysis (string/reflection-table
   cross-reference, the same method used to find the `Stats` component and `UserData` shape this
   session). It's FIFA-specific so it won't be in Impulsum14 — needs direct EBOOT work. Given it
   fires right after login and before persona lookup, it's plausibly session/entitlement setup
   that later steps depend on.
4. Capture and analyze the **PLAY SEASON → blank screen** path separately — a blank screen implies
   the client got further than a network stall (it's rendering *something*, just wrong/empty),
   which might be a more tractable lead than the frozen spinner.
5. Hex-dump-verify `GetStatsAsyncNotification`'s actual bytes on the wire against `tdf.py`'s
   encoder output line by line, rather than trusting the Python field-list source.

**Update 2026-09-24 (new session, PS3-side/binary-patch angle instead of Blaze-wire angle):**
this update is about the *exact same* "RE-CONNECT"/frozen-spinner blocker described above, but
approached from RPCS3-native-log + PS3-binary-patching instead of Blaze wire captures. Both symptom
descriptions ("Loading Seasons information...", "PRESS THE START BUTTON TO RE-CONNECT") are the
same underlying freeze; Play Cup Match and Play Season both exhibit it.

**Critical infrastructure discovery: how this user's RPCS3 actually boots the game.** The user's
RPCS3 does **not** boot `C:\fifa17-ps3\EBOOT.ELF` directly — it boots from an **ISO**
(`C:/rcps3/games/FIFA 17 (Europe) (En,Fr,De,Es,It,Nl,Pt,Sv,No,Da,Pl,Ru).iso`), with
`Elf path: /dev_bdvd/PS3_GAME/USRDIR/EBOOT.BIN` inside it (confirmed via `Elf path:` /
`Booting from gamelist...` lines in `RPCS3.log`). **Any direct byte-patch to the standalone
`C:\fifa17-ps3\EBOOT.ELF` file has zero effect on actual gameplay** — this was tried and confirmed
useless this session (a verified-correct byte patch produced no behavior change at all). Disc
serial (confirmed via `Serial:` lines in `RPCS3.log`, do not confuse with the `UP0006-BLUS31543_00`
/`UP0006-BLUS31593_00` strings seen in NP ticket/commerce calls, which are unrelated EA network
service IDs): **BLES02233**, version **01.00** (European edition).

**The correct way to apply a binary patch: RPCS3's Patch Manager (`patch.yml`).** Location on this
user's machine: `C:\rcps3\patches\patch.yml` (NOT `C:\rcps3\patch.yml` — confirmed by the user).
RPCS3 applies patches from this file to the executable **in memory at runtime**, keyed by PPU
executable hash + game title + serial + version, independent of which file on disk actually gets
booted — this is why it works where the direct file patch didn't. Confirmed working syntax (there
were already two working patches in the file under the same key when this session started, used as
the template):
```yaml
Version: 1.2
PPU-1243af2b292938038cdc3696755e82477122db3b:
  <Patch Name>:
    Games:
      FIFA 17:
        BLES02233: [ "01.00" ]
    Author: "custom"
    Notes: "..."
    Patch Version: "1.0"
    Patch:
          - [ be32, 0x<VA_hex>, 0x<value_hex> ]
```
Addresses are **VAs** (virtual addresses), same addressing scheme as everywhere else in this
project's disassembly work — not raw file offsets. New patches must be inserted as a new key under
the *same* `PPU-<hash>:` parent key (regex-insert before an existing sibling patch entry works
fine), and get picked up automatically by RPCS3's Patch Manager GUI (`FIFA 17 > BLES02233 v.01.00`)
without needing a restart — just enable the new patch's checkbox and relaunch the game.

**Patch #1 applied and confirmed effective: `NEW_THREAD_FOR_FE_INIT_STAGE3` default-value flip.**
Function `0x005C2BB8`-`0x005C2D44` gates creation of the "FIFA FE Second Initial Thread" (whose
endless recreation loop was the symptom investigated in earlier sessions) behind a config flag
whose *default* value (used when the key isn't found in an internal typed config store) is set by
`li r5, 1` (`38 A0 00 01`) at VA `0x005C2C10`. Patched via `patch.yml`:
```yaml
  Disable NEW_THREAD_FOR_FE_INIT_STAGE3 default (force off):
    Games:
      FIFA 17:
        BLES02233: [ "01.00" ]
    Author: "custom"
    Notes: "li r5,1 -> li r5,0 at 0x005C2C10, forces NEW_THREAD_FOR_FE_INIT_STAGE3 default to off"
    Patch Version: "1.0"
    Patch:
          - [ be32, 0x005C2C10, 0x38A00000 ]
```
**Confirmed effective**: after enabling this patch, `"FIFA FE Second Initial Thread"` no longer
appears **anywhere** in `RPCS3.log` during a full Cup Match/Play Season test — the first time in
the whole multi-session debugging effort this specific thread-creation loop has been eliminated.
**However, the on-screen symptom is unchanged** ("same as before" per user) — a *different*
mechanism has taken over the freeze.

**New blocker mechanism found: repeating "MoviePlayer2 Decode Thread" creation, NOT an actual
movie player.** There are two distinct things sharing this thread name in the log, easy to
conflate:
- A **real, legitimate** MoviePlayer2 thread (id `0x1000027`) plays the boot intro
  (`/dev_bdvd/PS3_GAME/USRDIR/data/movies/bootflowintro_MARKER.vp6`), reads real file data over
  ~3 seconds, and exits cleanly — log even shows `"PS3Shutdown, Movie player complete"` at the very
  end. **This is not the bug.**
- Starting **~2 seconds after** `cellGameDataCheck(): directory '/dev_hdd0/game/BLUS31543' not
  found` fails (this specific error has appeared in every test run all session, previously
  deprioritized), `FEThread` begins creating a new thread **every ~10.00-10.02 seconds, on the
  clock**, until the user closes the game window. These loop-threads do **no file I/O at all** —
  they're created, immediately do one `sys_mutex_unlock`, and exit in under 1ms. The
  "MoviePlayer2 Decode Thread" name on these is almost certainly a stale/reused thread-name-pointer
  artifact in RPCS3's thread-naming, not an actual video decoder — this is a generic polling/retry
  loop, structurally identical in behavior (fixed-interval, no-op, endless) to the now-fixed
  `FIFA FE Second Initial Thread` loop, just via a different code path.
- **Ruled out: not waiting on the network.** The `fifa17srv` console log for the same run shows the
  client closing its own Blaze TCP connection (`client closed the connection`) only ~19s after the
  last frame-level ping, with no further requests sent. RPCS3-log-side `sys_net_bnet_*` activity in
  the loop's time window is just polling an already-idle/closed socket. So whatever the 10-second
  loop is waiting for, it is **not** an unanswered Blaze request — it's a purely local/PS3-side
  wait (or a fixed timeout unrelated to any reply).
- **Tried, not yet verified live:** built and installed a synthetic `PARAM.SFO` at
  `C:\rcps3\dev_hdd0\game\BLUS31543\PARAM.SFO` (that directory existed on disk already but was
  completely empty — that's why `cellGameDataCheck` reported "not found"). Minimal PSF with keys
  `APP_VER=01.00`, `ATTRIBUTE=0` (int32), `CATEGORY=HG`, `PARENTAL_LEVEL=0` (int32),
  `TITLE=FIFA 17`, `TITLE_ID=BLUS31543`, `VERSION=01.00`. **`CATEGORY=HG` is a guess** — PS3
  category codes for this kind of supplementary/HDD game-data folder could plausibly also be `GD`;
  if this fix has no effect, try `GD` next before abandoning the idea. Built via a PowerShell script
  in this session's transcript (constructs the PSF header/index-table/key-table/data-table
  programmatically with correct offset math — regenerate from that script rather than hand-editing
  bytes if the category needs to change). **Not yet retested against a fresh RPCS3.log at the time
  of this README update** — next session should check whether (a) the "not found" message is gone
  and (b) the 10-second retry loop stopped.
- Given the loop isn't network-related, if the PARAM.SFO fix doesn't resolve it, the next lead
  should probably be identifying what specifically `FEThread` is polling for every 10 seconds —
  i.e., finding the actual calling function around the thread-creation call site (`func=*0xd9018`
  is a generic thread-trampoline, not useful on its own) via `find_callers.py` against whatever
  function issues these `_sys_ppu_thread_create` calls from `FEThread`, once that call site's VA is
  identified from a fresh disassembly pass.

**Update 2026-09-25 (later in the same session): the "10-second loop" is NOT a fixed-interval
timer — it's a reconnect cycle, and it correlates directly with the client's own Blaze
reconnection attempts.** Listing every `"MoviePlayer2 Decode Thread"` creation across a *full* log
(not just a narrow window) showed irregular gaps: 14.7s → 31.9s → 10.0s → **4 minutes 55 seconds**.
The user confirmed they were sitting completely idle on the frozen Play Season screen (with music
still playing) for the entire run — no input, no navigation. So the irregular timing isn't
UI-navigation-driven either.

Cross-referencing against the matching `fifa17srv` console log (wall-clock) for the same run
revealed the real cause: **the client closes and fully reopens its own Blaze connection
periodically, doing a complete redirector→login→QoS handshake from scratch each time**:
- Connection #1: opened, alive ~59s, closed.
- Gap: 4 min 46s.
- Connection #2: fully reconnects (fresh redirector/PreAuth/login/QoS sequence), alive with real
  traffic ~8s, one more frame-level ping 35s later, then closes after another ~5 min 05s of
  silence.

This **4:46 gap between the server-side close and reconnect** lines up almost exactly with the
**4:55 gap** between the 3rd and 4th `MoviePlayer2 Decode Thread` creations in the RPCS3-side
timeline for the same run. This confirms the thread-creation loop is not a fixed poll timer or
generic engine thread pool — **it's tied to the client's own automatic Blaze-reconnect cycle**,
and the `0xA46528` status-check function found via the live debugger (see above) is plausibly part
of that reconnect state machine after all.

**This is the key strategic finding of this whole session**: the client performs a **complete,
successful** Blaze handshake (redirector, PreAuth, login, full Stats burst, QoS, updateNetworkInfo)
on every single reconnect attempt — our server implementation is not rejecting it or erroring out.
It just never receives whatever it's actually waiting for to leave the frozen screen, so after a
timeout it gives up, waits several minutes, and tries the whole handshake again from scratch. This
**fully validates the original Blaze-level analysis** in the main "Current blocker" section above
(component `0x08C9` never identified, GameManager never invoked, the "waiting for a Notification
we never send" theory) and **invalidates** this session's PS3-side detour as the fix — the
PARAM.SFO/patch.yml/GameDataCheck work was not wasted (it correctly eliminated one real bug, the
old `FIFA FE Second Initial Thread` loop, and produced reusable tooling/technique), but the actual
blocker lives in the Blaze protocol layer, not in PS3-side binary patches or game-data files.
**Next session should return to the Blaze-level investigation ideas listed above** (identifying
component `0x08C9`, finding GameManager's real callers, comparing `GetStatsAsyncNotification`
byte-for-byte) rather than continuing PS3-side static/live disassembly of the reconnect-loop code
— that path has now told us what it can (confirms a reconnect cycle exists and roughly how long it
waits) without revealing what network-level piece is actually missing.

**Update 2026-09-25: PARAM.SFO fix retested live — RULES OUT the BLUS31543/GameDataCheck theory
entirely.** With the synthetic `PARAM.SFO` in place (RPCS3's game list now shows a second row,
`FIFA 17 / BLUS31543 / HDD Game / 01.00 / 248.00 B`, confirming the PSF parses correctly), a fresh
Play Season test shows:

- `cellGameDataCheck(type=3, dirName="BLUS31543", ...)` now fails with a **real, specific** error
  instead of "not found": `'cellGameDataCheck' failed with 0x8002cb05 : psf::error='OK',
  type='3' CATEGORY='HG'`. This confirms the PSF is read successfully, but **`CATEGORY=HG` is the
  wrong category for whatever `type=3` expects** (our earlier guess was wrong; `GD` or another code
  may be correct for type 3 specifically — not yet tried).
- The client **immediately retries with `type=2`** on the same `dirName`, and **that one succeeds**
  — category `HG` is apparently correct for `type=2` (plausibly `CELL_GAME_GAMETYPE_HDD`). It then
  proceeds to `cellGameContentPermit` and `sys_fs_opendir("/dev_hdd0/game/BLUS31543/USRDIR/dime")`
  (a `USRDIR/dime` subfolder we never created — this whole exchange looks like a DIME
  [likely some kind of live-content/roster-update check] probe, most likely unrelated to Play
  Season/Cup Match itself).
- **Despite `type=2` now succeeding, the 10-second "MoviePlayer2 Decode Thread" retry loop occurred
  completely unchanged** — same fixed ~10.00-10.02s cadence (5 iterations logged before the user
  closed the game), same no-op thread bodies, and **the user confirmed "w grze się nic nie
  zmieniło" (nothing changed on screen)**. This is strong evidence the loop was **never actually
  gated by the BLUS31543 GameDataCheck outcome** — the correlation observed in the previous test
  was coincidental timing, not causal. **Do not keep chasing this lead**; the PARAM.SFO
  fix can stay in place (it's more correct than an empty directory regardless, and may matter for
  other things later), but it is confirmed **not** the fix for the freeze.
- This pushes the investigation back to the already-documented Blaze-level "RE-CONNECT" blocker
  (see the main "Current blocker" section above) — the 10s loop is most plausibly the client's own
  reconnect-retry/watchdog mechanism for the same freeze, just observed from the PS3-native-log
  side instead of the Blaze-wire side. **Next concrete step, not yet tried:** use **RPCS3's
  built-in debugger** (View/Tools → Debugger in the RPCS3 GUI) to pause emulation right as `FEThread`
  creates one of these loop threads and read its **live call stack** — this gives the actual
  calling function's return address directly, without needing to guess or brute-force scan for it
  statically (raw `sys_ppu_thread_create` syscalls don't log an `LR:`/`HLE:` caller annotation the
  way `cellGame`/`cellSysmodule` HLE-wrapped calls do, so static `find_callers.py`-style tracing
  has no good starting VA for this specific loop yet — the debugger sidesteps that entirely).

## Reverse-engineering toolchain

The PS3 EBOOT.ELF static-analysis scripts (`find_str_refs.py`, `find_cmd_consts.py`,
`find_cmd_names.py`, `find_requests.py`, `disasm_range.py`, `dump_words.py`,
`find_tdf_members.py`, ...) used to derive the confirmed facts above live in `fifa17srv/`. They
operate on an `EBOOT.ELF` you extract yourself from **your own** legitimate copy of the game — the
ELF is **not** in this repository and must never be committed. Each tool prints its own usage on
`--help` or bad args.

- `find_callers.py <eboot.elf> <target_hex_va>`: dependency-free (no capstone), finds all `bl`/`b`
  instructions in `.text` whose branch target resolves to the given VA, by parsing ELF64 PT_LOAD
  program headers for VA↔file-offset mapping and decoding just the PPC primary-opcode-18 branch
  instruction. Useful for "who calls this function" when a targeted xref search is needed and
  heavier disassembly tooling is unavailable/slow. Confirmed working: found 9 callers of the config
  lookup function `0x143C4B8`; found 0 callers of `0x143C9D8` (a setter only reached indirectly via
  function pointer/vtable — a dead end for this specific technique, not a bug in the script).

**PS3 ELF VA↔file-offset mapping, important recurring gotcha:** this EBOOT's first (and largest)
`PT_LOAD` segment has `vaddr - file_offset = 0x10000` (confirmed via program headers: `phoff=0x40`,
`phentsize=56`, `phnum=8`, two real `PT_LOAD` segments sharing that same `0x10000` delta, plus
three degenerate zero-size `PT_LOAD` entries to ignore). A raw file-offset hit from a byte-level
scan is **not** the same as the VA that `disasm_range.py`/patch.yml/etc. expect — always add
`0x10000` and verify with `disasm_range.py` before trusting or acting on a raw-offset search hit.
(This bit this project once already: two apparent `li r4, 0x8C9` hits at raw offsets
`0x0030AA08`/`0x0030AAEC` turned out to be false positives on reinspection after correction.)

Known paths on the user's Windows machine (confirmed, not guessed -- ask the user to re-confirm
if a command against one of these fails, since setups can change):
- RE tool scripts: `C:\fifa17-friendlies-git\fifa17srv\` (run scripts from here, e.g.
  `python find_requests.py ...`)
- EBOOT.ELF (standalone file, disassembly target — **but NOT what RPCS3 actually boots**, see
  below): `C:\fifa17-ps3\EBOOT.ELF`
- **What RPCS3 actually boots**: an ISO,
  `C:/rcps3/games/FIFA 17 (Europe) (En,Fr,De,Es,It,Nl,Pt,Sv,No,Da,Pl,Ru).iso`, with
  `Elf path: /dev_bdvd/PS3_GAME/USRDIR/EBOOT.BIN` inside it. A byte-patch to the standalone
  `C:\fifa17-ps3\EBOOT.ELF` above has **no effect on gameplay** — confirmed this session. Use
  RPCS3's Patch Manager (`patch.yml`, below) for any patch meant to actually change behavior.
- RPCS3 install root: `C:\rcps3` (note the transposed letters, that's the real path). Native/HLE
  log: `C:\rcps3\log\RPCS3.log` (several MB, grep it rather than pasting whole). TTY log (game's
  own stdout/stderr, much smaller): `C:\rcps3\log\TTY.log`.
- RPCS3 Patch Manager database: `C:\rcps3\patches\patch.yml` (**not** `C:\rcps3\patch.yml` — a
  previous session guessed wrong, user corrected it). Applies `be32` (and other) in-memory patches
  keyed by PPU hash + game/serial/version, regardless of which file RPCS3 actually booted from.
  See the "Patch Manager" writeup in the "Current blocker" section above for exact working YAML
  syntax and a confirmed-effective example patch.
- RPCS3 patch-manager config (separate from `patches/`): `C:\rcps3\config`
- RPCS3 virtual filesystem root: `C:\rcps3\dev_hdd0`. Game-data-check directories live under
  `C:\rcps3\dev_hdd0\game\<DIRNAME>\` (e.g. `BLUS31543` — already existed as an empty dir this
  session, which is why `cellGameDataCheck` reported "not found"; needs a valid `PARAM.SFO` inside
  to be recognized). Savedata (useful as a real PARAM.SFO reference/template for the PSF binary
  format) lives under `C:\rcps3\dev_hdd0\home\00000001\savedata\<TITLE_ID+NN>\PARAM.SFO`, e.g.
  `C:\rcps3\dev_hdd0\home\00000001\savedata\BLES022330000\PARAM.SFO`.
- Confirmed disc serial: **BLES02233** (European edition), version **01.00** — found via `Serial:`
  lines in `RPCS3.log`. Do not confuse with `UP0006-BLUS31543_00`/`UP0006-BLUS31593_00` strings
  seen in NP ticket/commerce API calls elsewhere in the log — those are unrelated internal EA
  network service identifiers, not the disc serial.

Reference implementation used for cross-checking tag names / command numbers / TDF field layouts:
`github.com/Mk0M/Impulsum14` — an open-source, from-scratch C# Blaze backend for **FIFA 14 PC**
(older Blaze 13 SDK, same TDF/SDK family). Treat it as a strong hint for *shape*, not as ground
truth for FIFA 17 specifically — SDK versions drift, and nothing in it has ever been confirmed
against real FIFA 17 wire traffic.

### Ghidra setup (started 2026-09-25, pivot away from ad-hoc byte-scanning scripts)

**Why:** the hand-rolled Python byte-scanning scripts above (`find_callers.py`, raw `li`/`ori`
immediate scans, `cmpwi`/`beq` chain heuristics for "dispatch tables") produced **two confirmed
false positives in a single session** — a `cmpwi`/`beq` chain at `0x00C92380`-`0x00C92420` that
looked exactly like a command-dispatch switch was actually a generic ASCII character
classifier/string parser unrelated to GameManager or any Blaze component. Static analysis without
real cross-references (xrefs) is too unreliable for finding "who calls this" — every "next step"
in the Current Blocker section above that says "find the real caller of X" needs a proper
disassembler with xrefs, not more grep.

**Already installed** on the user's machine: Ghidra 12.1.3 PUBLIC at
`C:\fifa17-ps3\ghidra\ghidra_12.1.3_PUBLIC` (Java: Eclipse Adoptium JDK 21 at
`C:\Program Files\Eclipse Adoptium\jdk-21.0.12.101-hotspot`, already working, no need to reinstall).

**Import gotcha, already solved — do not redo this investigation:** the user's *first* import
attempt (a file plainly named `EBOOT.ELF` inside the Ghidra project) was done **without picking a
Language**, so the whole file sat as undefined `??` bytes with 0 functions found — completely
useless. The fix was a **second, correct import**, named `v2_EBOOT.ELF` to avoid clashing with the
broken one, explicitly selecting language **`PowerPC:BE:64:default:default`** (64-bit big-endian
PowerPC w/ Altivec — matches the PS3 Cell PPU). **If starting fresh or re-importing, always
explicitly set this language — do not let the importer leave it blank.** The broken plain
`EBOOT.ELF` entry is still sitting in the project and should probably be deleted at some point, but
hasn't caused any problems being left there.

**Ghidra project location:**
- Project directory: `C:\fifa17-ps3\ghidra_projects`
- Project name: `FIFA17` (so the `.gpr`/`.rep` are `C:\fifa17-ps3\ghidra_projects\FIFA17.gpr` etc.)
- The correctly-imported program inside it: `v2_EBOOT.ELF` (at project root `/`)
- File stats from the import summary: **40,732,646 bytes** (~40.7 MB), 34 memory blocks, 72,997
  defined data items, 0 functions (not yet analyzed at import time — see below).

**Auto-analysis: kicked off headless, running overnight, status unknown as of this README update.**
GUI-based analysis was abandoned as unpredictable/unable-to-show-real-progress for a binary this
size (this bit the project on a previous, separate Ghidra attempt described by the user as
"mieliło" for an indeterminate time with no way to tell if it would ever finish). Instead, used
Ghidra's **headless analyzer**, which is more reliable for large unattended runs. Two gotchas hit
and fixed while setting this up, worth knowing before running it again:
1. `-analysisTimeoutPerFile 0` does **NOT** mean "unlimited" — it is taken literally as a 0-second
   timeout, so analysis aborts instantly (`"Analysis timed out at 0 seconds"`). Use a large explicit
   value instead, e.g. `86400` (24 hours).
2. The project must **not** be open in the Ghidra GUI at the same time (file lock) — close the GUI
   entirely (File → Exit Ghidra) before running headless against the same project.

The exact command used (run from `C:\fifa17-ps3\ghidra\ghidra_12.1.3_PUBLIC\support`, in a
PowerShell session with `$env:MAXMEM = "24G"` set beforehand — user has 32GB RAM, left 8GB for the
OS):
```powershell
cd C:\fifa17-ps3\ghidra\ghidra_12.1.3_PUBLIC\support
$env:MAXMEM = "24G"
.\analyzeHeadless.bat "C:\fifa17-ps3\ghidra_projects" FIFA17 -process v2_EBOOT.ELF -analysisTimeoutPerFile 86400
```
Confirmed this actually started real analysis (log showed `ANALYZING all memory and code:
/v2_EBOOT.ELF` followed by a couple of harmless `ERROR Invalid GIF data at ...` lines from the
Embedded Media analyzer misfiring on some non-GIF data — not a real problem, analysis continues
past those). Left running unattended overnight in a PowerShell window the user was told not to
close. **Next session must first check whether this finished successfully** (console should show
a final `INFO  REPORT: Save succeeded for processed file: /v2_EBOOT.ELF` with no accompanying
timeout error) before doing anything else — if it's still running, either wait for it or (if it
looks stuck/crashed) re-launch the same command, which should resume/redo cleanly since headless
re-processing an already-partially-analyzed file is safe.

**UPDATE 2026-09-25 evening — the `v2_EBOOT.ELF` headless run above used the WRONG language and
should be considered abandoned/superseded.** After ~20h of the headless run showing essentially no
progress signal (only two harmless GIF errors in the log the entire time, though CPU usage
confirmed it was genuinely computing the whole time, ~100% of one core, never stuck/deadlocked), a
community source (a PS3 reverse-engineering video, project name "Ratchet-RE") was found showing
that PS3 EBOOT.ELF files should use language **`PowerPC:BE:64:A2ALT-32addr`** (shown in Ghidra's
language picker as "PowerISA-Altivec-64-32addr", 32-bit addressing, big endian) — **not**
`PowerPC:BE:64:default:default` which was used for `v2_EBOOT.ELF`. This matches a known open Ghidra
bug (NationalSecurityAgency/ghidra#570): generic PPC64 language specs don't correctly process
TOC/OPD/GOT for ELFs using PPC64 with 32-bit addressing, which is exactly the PS3 Cell PPU ABI.

Killed the 20h-in `v2_EBOOT.ELF` headless process (`Stop-Process -Id <pid> -Force`) and re-imported
the same `EBOOT.ELF` as a **new** program named `v3_EBOOT.ELF` in the same `FIFA17` project, this
time explicitly picking language `PowerPC:BE:64:A2ALT-32addr`. Ran analysis **from the GUI this
time** (not headless) with `Decompiler Switch Analysis` and `Decompiler Parameter ID` both
unchecked in the Analysis Options dialog (both are known slow analyzers for large binaries per
community reports). Result: **analysis completed in ~28 minutes** (started 21:57, done by ~22:25),
found **53,160 functions** and **145,671 defined data items**. The decompiler window confirmed
real, sane pseudo-C output (e.g. `.opd.FUN_00010200` calling `FUN_00027f90()` and `FUN_00028000()`),
unlike the old profile which likely would have produced garbage/oversized functions from
misinterpreted 64-bit-addressing pointers (plausible explanation for why the old run was so slow:
Ghidra's decompiler has a known bug — NationalSecurityAgency/ghidra#4558 — where it can stall for
hours on a single malformed/oversized function).

**Conclusion for future sessions: use `v3_EBOOT.ELF` (language `PowerPC:BE:64:A2ALT-32addr`) as the
canonical analyzed program going forward, not `v2_EBOOT.ELF`.** The `v2_EBOOT.ELF` program (wrong
language) and the original blank `EBOOT.ELF` (no language) can both be considered dead ends, kept
in the project only for reference/comparison, not for further investigation. If Ghidra analysis
ever needs to be redone from scratch on this binary, always pick `PowerPC:BE:64:A2ALT-32addr`
explicitly, and prefer GUI analysis with Decompiler Switch Analysis/Decompiler Parameter ID
disabled over headless — it's fast enough now (minutes, not hours) that headless's unattended
overnight approach is no longer necessary.

**Once analysis is confirmed complete, the concrete next steps are** (do these instead of any more
manual byte-scanning, and do them against `v3_EBOOT.ELF`, not `v2_EBOOT.ELF`):
1. Open the project in the Ghidra GUI (double-click `v3_EBOOT.ELF` in the `FIFA17` project — this
   should show real disassembly and decompiled pseudo-C).
2. Find the confirmed Blaze request-send function `0x00CFAD54` and use Ghidra's **"Find References
   to"** (right-click the function, or place cursor on its entry and check the XREF panel) to get
   the complete, real list of callers — filter for any that set up `component=0x0004` (GameManager)
   before calling it, since GameManager traffic has never been observed on the wire in any capture
   this project has taken.
3. Search Program Strings (Search → For Strings) for `createGame`, `joinGame`, `NotifyGameSetup`,
   `startMatchmaking` (already confirmed present in the binary via `find_str_refs.py` in an earlier
   session, at `0x01FA90B8` onward) and use each string's XREF panel to find what actually
   references them — this replaces the abandoned "find `getCommandName` dispatch caller" static
   heuristic that gave a false positive.
4. Re-investigate component `0x08C9` (2249 decimal): the only concrete lead so far is a real `li r4,
   0x8C9` at VA `0x002991E0` feeding into a binary-search-over-sorted-array call at `0x01A148A0`
   (confirmed generic `lower_bound`-style helper, not the Blaze send function) — with Ghidra's
   decompiler on that surrounding function (starts around `0x00299180`), read the actual pseudo-C
   to see what the binary-search result (a found "component descriptor" struct pointer) is used
   for; earlier manual tracing suggested a field at `+4` might be a name-string pointer but this
   needs the decompiler/live debugger to actually resolve the runtime pointer value, not more
   static guessing.
5. Once real xrefs are available, revisit whether the `0xA46528`/`0xA465D8` functions found via the
   RPCS3 live debugger this session (a generic-looking "check status==2, notify, clear dirty flag"
   pattern, and a list-iterating watchdog calling it) are actually part of Blaze's client-side
   reconnect state machine, now that they can be traced properly instead of guessed from a single
   live-debugger snapshot.

## Quick start (Windows 10/11)

1. Install Python 3.11+ and this folder somewhere convenient.
2. Open **PowerShell as Administrator** in the project folder (one-time setup):
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\tools\install_redirector.ps1
   ```
   (adds one hosts line and creates `certs/`; it does *not* touch your certificate store)
3. Every time you want to test: `powershell -ExecutionPolicy Bypass -File .\update_and_run.ps1`
   (pulls the latest server code and starts it in one step — see below).
4. Start FIFA 17 (via RPCS3) and go to an online mode.
5. Look in `logs/captures/` and paste the server's console log back for analysis.
6. When permanently done, undo the redirector changes:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\tools\remove_redirector.ps1
   ```

`update_and_run.ps1` (repo root) just does `git pull` of the current branch followed by
`python -m fifa17srv run`, run from the repo root via `$PSScriptRoot`. It exists because
`logs/`, `certs/`, and `config.json` are all gitignored, so there is never anything local worth
preserving — no need for `git add`/`commit`/`push` before pulling.

## Playing with a friend

See "Playing with a second player (setup guide)" above for the full procedure. In short: the host runs the server
(`update_and_run.ps1`) bound to `0.0.0.0` with `blaze_advertise_host` in `config.json` set to its VPN (Radmin) address; the
friend does **not** run the server and does not need git - only RPCS3 configured to resolve
`winter15.gosredirector.ea.com` to the host, the same three RPCS3 patches as the host (CA swap, NEW_THREAD_FOR_FE_INIT_STAGE3,
Nucleus URL) and, for the diagnostic rounds, the trace patch (a ready-made `patch.yml` can simply be copied over the friend's
`patches/patch.yml`).

## Configuration

Create `config.json` next to this file to override any field of `fifa17srv/config.py` (the
dataclass there is the source of truth — read it directly, it's short and every field has a
comment explaining what it's for and why it exists), e.g.

```json
{ "bind_address": "0.0.0.0", "blaze_advertise_host": "26.1.2.3", "blaze_secure": false }
```

## Layout

```
fifa17srv/redirector.py      fake redirector (HTTPS)
fifa17srv/blaze.py           main Blaze protocol handler -- most protocol logic lives here
fifa17srv/gamemgr.py         GameManager (0x0004): game state, host-only setup ladder, watchdog, probes
fifa17srv/gmtrace.py         per-attempt GameManager journal (logs/gm_attempts.log)
fifa17srv/ids.py             player ids (one source of truth for the whole server)
fifa17srv/messaging.py       Messaging (0x000F) mailbox
fifa17srv/usersettings.py    persistent Util::userSettings* (state/user_settings.json)
fifa17srv/nucleus.py         fake Nucleus (console login, hands out an auth code)
fifa17srv/qos.py             local QoS server
fifa17srv/telemetry_stub.py  EA telemetry stub (TLS + HTTP 200, logs the request body)
fifa17srv/pow_stub.py        POW (EASFC) / FUT web stub -- only records requests
fifa17srv/tcp_record.py      recording TCP stub (ticker)
fifa17srv/probe.py           capture probe for the main Blaze port
fifa17srv/server.py          threaded server, capture logs, TLS-or-plaintext negotiation
fifa17srv/tls_hello.py       ClientHello parser (explains handshake failures)
fifa17srv/tdf.py             TDF codec
fifa17srv/analyze.py         capture inspection helper
fifa17srv/certs.py           throwaway local CA + server cert
fifa17srv/config.py          Config dataclass, all tunables with inline comments
fifa17srv/selftest.py        synthetic-client self-test (python -m fifa17srv selftest)
fifa17srv/find_*.py, dump_*.py, disasm_range.py   ad-hoc EBOOT reverse-engineering helpers (comments partly Polish)
tools/*.ps1                  hosts/CA install and removal (Windows, run as admin)
tools/make_trace_patch.py    generator + self-test of the RPCS3 trace patch (tools/rpcs3_patch_trace_invites.yml)
tools/install_trace_patch.py add/remove the trace patch in RPCS3's patches/patch.yml
tools/decode_trace.py        decode trace markers from RPCS3.log
tools/rpcs3_patch_easfc_banner.yml   optional cosmetic RPCS3 patch (hides the EAS FC corner banner)
update_and_run.ps1           one-command pull + run for repeat testing
collect_logs.ps1             zip server logs, journal, state and config for analysis
TEST_PLAN.md                 step-by-step test procedure (in Polish)
```

## Security notes

* `-InstallCA` makes Windows trust a CA whose private key lives in `certs/`. Use it only if
  needed, keep `certs/` private, and run `remove_redirector.ps1` afterwards.
* The server is meant for localhost or a trusted friend. Do not expose it to the open internet.
* Do not download "FIFA private server" builds from random sites; build from source.

## Roadmap

1. ~~Capture redirector + first Blaze packets.~~ Done.
2. ~~Confirm frame format; implement PreAuth / Ping / Authentication responses.~~ Done.
3. ~~Get the client to a logged-in main menu with a local fake profile.~~ Done.
4. ~~Make the host-side `GameManager::createGame` handshake complete without crashing.~~ Done (host-only setup ladder,
   all five levels reach `finalizeGameCreation`).
5. **Current:** make the host's game actually send the invitation (`EVENT_CREATEGAME_SUCCESS` -> `SendGameInvite` ->
   `sceNpBasicSendMessageGui`). Use trace patch round 2 to find where the createGame completion path stops, then fix the
   server reply (or work around it).
6. Friend side: accept the invitation (`joinGame`), real P2P address exchange over UDP 3659/9999, play a match.
7. Get past the Online menu for Cup Match / Season (blank screen / "Loading Seasons information..." - see
   "Current blocker" below). Unrelated to the Online Friendlies progress above.
8. Docs of the protocol (clean-room notes, no EA code).

## Related work

Other people are reviving old FIFA online modes (mostly Ultimate Team); this project focuses on
friendlies. Contributions and shared captures are welcome. License: MIT.

## Appendix: older GameManager notes (2026-10-08, translated from Polish)

### Experiment `gm_deferred_pregame` (2026-10-08, unverified)

Hypothesis: in `NotifyGameSetup` the game should have GSTA=INITIALIZING(1), and only after `finalizeGameCreation` does the
server send NotifyGameStateChange(PRE_GAME=130). On by default; `"gm_deferred_pregame": false` in config.json restores
the old behaviour (PRE_GAME at once). EBOOT analysis: the createGame success (`EVENT_CREATEGAME_SUCCESS`) is raised by the
OSDK listener (0x1624e1c) called from the GameManagerAPI layer; the exact condition for the call had not been established yet
(see the 2026-10-11 late-evening notes for the current understanding).

### Corrected notification shapes from the EBOOT reflection (2026-10-08)

From the TDF class tables in EBOOT.ELF (verified): `NotifyPlatformHostInitialized` = {GID, **PHID**} (earlier wrongly PHST
from Impulsum14), `NotifyPlayerJoinCompleted` = {GID, PID, **TIME**}, `NotifyGamePlayerStateChange` = {GID, PID, STAT},
`NotifyGameStateChange` = {GID, GSTA}, `NotifyPlayerJoining` = {GID, PDAT, QOST}. `NotifyGameSetup` has the fields GAME,
LFPJ, MNAM, PROS, QOSS, QOSV, QUEU, REAS, TELM. Tool: `tdfauto.py` (not in the repo).

`NotifyPlatformHostInitialized` = {GID, PHID, PHST(slot)} (3 fields). `ReplicatedGamePlayer` according to the reflection: CSID,
DSUI, EXBL, EXID, GID, JFPS, JVMM, LOC, NAME, NASP, PATT, PID, PNET, PSET, RCRE, ROLE, SID, SLOT, STAT, TIDX, TIME, UGID, UID,
UUID - EXBL (NpId), LOC, NASP, PATT, TIME were added to the roster. `ReplicatedGameData`: ADMN APRS ATTR CAP COID CRIT CTIM DHST
DNET ESNM GGTY GID GMRG GNAM GPVH GSET GSID GSTA GTYP GURL HNET MATR MCAP MNCP NPSI NQOS NRES NTOP PGID PGSR PHST PRES PSAS QCAP
RNFO SCID SEED STMN THST TIDS UUID VOIP VSTR XNNC XSES (the HSES we used to send does not exist there).

Added (unverified on a live client at the time): the GameManager requests advanceGameState (3), setGameAttributes (7),
setPlayerAttributes (8) are acknowledged with an empty reply and broadcast to all players of the game as
NotifyGameStateChange (100) / NotifyGameAttribChange (80) / NotifyPlayerAttribChange (90).
EBOOT analysis: FIFA code (0xc49180..) switches UI variables according to the game state: INITIALIZING(1) sets 'gsid' (game id)
for the host, PRE_GAME(130) sets 'peer'=1 - so the client expects the transition 1 -> 130, which supports the
gm_deferred_pregame hypothesis.

### The `gm_faithful_flow` flow (2026-10-08, on by default at the time, unverified)
createGame: the host gets NotifyGameSetup only with itself (state CONNECTED, GSTA=INITIALIZING). After its
finalizeGameCreation the server sends the host PRE_GAME + PlatformHostInitialized, and the invitee a NotifyGameSetup
(PRE_GAME, roster host+invitee, IndirectJoin) + PlatformHostInitialized; the host gets NotifyPlayerJoining. Then as before:
updateMeshConnection STAT=2 -> PlayerStateChange(4) + JoinCompleted. (Superseded by the 2026-10-11 host-only ladder.)

### Conclusions from a further EBOOT analysis (2026-10-08)
- The constructor of class Game (0xc58c3c) in state INITIALIZING(1), POST_GAME(132) and MIGRATING(133) creates an extra
  "platform host" object from PHST/THST, and in PRE_GAME(130) it looks the topology host up in the roster by THST.HPID (so the
  host MUST be in PROS under the same PID as THST.HPID - it is in our reply).
- `isHost` = (topology host == local player); the local player is bound by the BlazeId of the logged-in user, so the host's PID
  as seen by the host must equal its own BlazeId (LOCAL_USER_ID). (Refined on 2026-10-11: the real comparison is between
  network endpoints keyed by CONG, see "the isHost model".)
- FIFA code (0xc4917x) sets the variable 'gsid' (game id) only in the INITIALIZING handler of the host, so skipping that state
  (old variant: PRE_GAME at once) might have broken something.
- Players joining a game that is not yet in PRE_GAME..POST_GAME go to `Game::mDeferredJoiningPlayerMap` (deferred) - that is
  why an invitee only gets its setup after the transition to PRE_GAME.
