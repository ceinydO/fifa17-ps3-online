# Plan testu (nastepna sesja)

## TEST RUNDA 3 (2026-10-12) -- zrob to najpierw
Wynik rundy 2 (README, "Latest session: 2026-10-12"): host woluje `createGame` z innego miejsca niz adaptor UI
CreateGame, listener wyniku nie wysyla `EVENT_CREATEGAME_SUCCESS`, wiec UI nigdy nie dochodzi do wyslania zaproszenia.
Ta runda pokazuje, KTO woluje `createGame` i jaki kod dostaje wynik, a opcjonalny eksperyment wymusza brakujace
wywolanie skryptu UI (`OnOnlineCreateSession`).
1. **Oba komputery:** podmien `patches\patch.yml` w folderze RPCS3 plikiem od Claude'a (ten sam plik dla obu; stary
   zapisz jako `patch.yml.bak`). Plik lezy w podfolderze `patches` folderu z `rpcs3.exe` (host: `C:\rcps3\patches\patch.yml`).
   (Gdy masz git: `python tools\install_trace_patch.py "C:\rcps3"` robi to samo.)
2. W RPCS3: Manage -> Patch Manager -> FIFA 17 -> BLES02233 v01.00 -> zaznacz **oba** wpisy: **FIFA trace (zaproszenia i
   zdarzenia UI)** i **FIFA eksperyment - wymus OnOnlineCreateSession po createGame**. Pierwszy start gry kompiluje PPU
   od nowa (kilka minut) -- to normalne. Kolega moze miec zaznaczony tylko "FIFA trace", ale oba tez nie szkodza.
3. Host: `.\update_and_run.ps1` (serwer przez caly test). Obaj: uruchom FIFA 17, wejdz w Online Friendlies i **zostan
   online obaj przez caly test**.
4. Host: zaproszenie kolegi (Play Match), poczekaj 30 s. Jesli RPCS3 pokaze okno wyboru znajomego / wiadomosci --
   wybierz `odyniec1` i zatwierdz. Ponow raz. **Zapisz jednym zdaniem, co widac na ekranie hosta i kolegi** (czy cos sie
   pojawia, czy ekran sie zmienia, czy "please wait" znika).
5. Zamknij gre normalnie, **skopiuj `RPCS3.log` zaraz po tescie** (obaj), host: `collect_logs.ps1`. Wyslij zip + oba logi.
Jesli gra wywala sie/zawiesza przy zaproszeniu: zapisz to, zamknij, odznacz "FIFA eksperyment" i powtorz (znaczniki
zostaja) -- logi z obu podejsc sa cenne.
Odczyt: `python tools/decode_trace.py RPCS3.log --elf EBOOT.ELF` -- linie `0x70 createGame -> ... WOLA KOD <adres>`,
`UI-SKRYPT <nazwa>(...) <- <kto wolal>` i `<- LR (fn START)` przy znacznikach.

## TEST Z ZNACZNIKAMI (2026-10-11 wieczor) -- wykonany (runda 1-2), zostaje jako opis procedury
Cel: dowiedziec sie, w ktorym miejscu FIFA zatrzymuje wysylanie zaproszenia (README, "Latest session: 2026-10-11 wieczor").
Drabinka NotifyGameSetup jest zrobiona: wszystkie 5 poziomow dochodzi do `finalizeGameCreation` bez crasha, serwer zostaje
na poziomie 5.
1. **Oba komputery:** `git pull`. Host: `.\update_and_run.ps1` (serwer ma dzialac przez caly test).
2. **Oba komputery, jednorazowo:** zainstaluj latke-znacznik (dopisuje wpis do `patches\patch.yml` RPCS3):
   `python tools\install_trace_patch.py "C:\sciezka\do\rpcs3"` (folder z rpcs3.exe), potem w RPCS3
   Manage -> Patch Manager -> FIFA 17 -> BLES02233 v01.00 -> zaznacz **FIFA trace (zaproszenia i zdarzenia UI)**.
   Pierwsze uruchomienie gry po tej zmianie kompiluje PPU od nowa (kilka minut) -- to normalne.
   Jesli gra nie dojdzie do menu glownego / wywala sie przy starcie: odznacz te latke i daj znac (nic wiecej nie rob).
3. Host: Online Friendlies -> zaproszenie kolegi, poczekaj ~20 s, ponow 2-3 razy. Kolega patrzy, czy cos sie pojawia.
   Zapisz jednym zdaniem, **co widac na ekranie hosta** po zaproszeniu i po ilu sekundach to znika.
4. Zamknij gre w RPCS3 normalnie (X okna gry), **skopiuj `RPCS3.log` zaraz po tescie** (host i kolega), potem na hoscie
   `powershell -ExecutionPolicy Bypass -File .\collect_logs.ps1` i wyslij zip + oba `RPCS3.log`.
Wyniki czyta sie komenda `python tools/decode_trace.py RPCS3.log --elf EBOOT.ELF` (linie `EVT ...` = zdarzenia UI,
`0x12 ginv::SendGameInvite` = lancuch zaproszenia, `NP sceNp...` = wywolania NP obok).

1. Oba komputery: `git pull`, uruchom serwer jak dotad. `config.json` ma zawierac tylko
   `{"idle_timeout":600,"bind_address":"0.0.0.0","blaze_advertise_host":"<twoj adres Radmin>"}`.
2. Zrob zaproszenie jak zwykle (host zaprasza kolege, "Online Friendlies").
3. Wyslij: log RPCS3 hosta i kolegi + log serwera (`logs/captures`).

## Jesli host nadal wisi na "Sending match invite..."
Zmieniaj po jednej rzeczy w `config.json` (sprawdz po kazdej zmianie):
- `"gm_faithful_flow": false` -- stary przebieg (obaj gracze od razu w NotifyGameSetup).
- `"gm_deferred_pregame": false` -- stary wariant (PRE_GAME od razu).
- `"gm_initial_player_state": 4` -- gracze od razu "polaczeni".
- `"gm_send_player_joining": false` -- bez NotifyPlayerJoining.
- `"gm_followups": false` -- bez zadnych dodatkowych powiadomien.

## Diagnostyka sieci (jesli nadal nic)
Wireshark na adapterze Radmin, filtr `udp.port == 3659 || udp.port == 9999`:
czy host/kolega w ogole wysyla pakiety do siebie po NotifyGameSetup?

## Baner "EAS FC servers are unavailable" (test 2026-10-09)
1. `git pull` + `.\update_and_run.ps1`. W bannerze startowym powinna byc linia `web (POW/FUT): ...:8094`.
   Zezwol Windows Firewall na port 8094 (i 8080, 6776 jesli kolega sie laczy).
2. Wejdz do menu glownego. W logu RPCS3 szukaj `DnsHook: DNS query for ut` -- po tej zmianie ma zniknac
   (zamiast tego `Attempting to connect on <adres Radmin>:8094`). Zapytanie z pusta nazwa moze zostac.
3. Sprawdz, czy baner sie zmienil. Przeslij: log serwera i pliki `logs/captures/pow_*.txt` (co FUT zadal).
4. Opcjonalnie (tylko kosmetyka): patch `tools/rpcs3_patch_easfc_banner.yml` -- instrukcja w pliku.
   Wylaczenie nowych kluczy w serwerze: `"serve_fut_config": false`.

## Play Season (test po zmianie `serve_seasonal_stats`)
1. `git pull` + `.\update_and_run.ps1`, wejdz w Play Season i odczekaj ~20 s.
2. Wyslij log serwera od "H2HSeasonalPlay": w logu ma byc `StatGroupResponse ... [70 deskryptorow statystyk sezonu]`
   i potem NOWE zadania (kolejna grupa statystyk albo `fetchClientConfig CFID='FIFA_H2H_SEASONALPLAY'`) -- to znaczy, ze ruszylo.
3. Jesli nadal cisza: `logs/captures/blaze_*.txt` + log RPCS3 (od momentu klikniecia).

## Test GameManager po przebudowie (2026-10-09)
Oba komputery: `git pull`, uruchom serwer, zrob zaproszenie Online Friendlies jak zwykle. Przeslij logi serwera
i RPCS3 obu stron. W logu serwera szukaj: `finalizeGameCreation`, `updateMeshConnection ... TCG=`, `NotifyGameSetup`.
W RPCS3 hosta: czy pojawia sie `sceNpBasicSendMessageGui` (zaproszenie) i czy UDP 3659/9999 idzie miedzy PC.
Przelaczniki (po jednym): `gm_fifa17_union_tags`, `gm_indirect_join`, `gm_host_initial_state` (4),
`gm_faithful_flow`, `gm_deferred_pregame`, `serve_entitlements`, `serve_messaging`.

## Drabinka poziomow NotifyGameSetup (test po 2026-10-11)
Cel: znalezc ksztalt setupu, po ktorym host dochodzi do `finalizeGameCreation` ORAZ UI hosta wysyla zaproszenie
(`sceNpBasicSendMessageGui`) -- kolega ma dostac zaproszenie od RPCN. Patrz README, "Test 2026-10-11".
1. Host i kolega: `git pull`. Host uruchamia `.\update_and_run.ps1` (serwer ma dzialac przez wszystkie proby);
   obaj uruchamiaja emulator od nowa i loguja sie do Online Friendlies.
2. Host: Online Friendlies -> zaproszenie kolegi. Poczekaj ~15 s. W logu serwera: `POZIOM n (...)`, potem
   `SUKCES poziomu n` albo `WATCHDOG ... BRAK finalizeGameCreation`. KOLEGA patrzy, czy pojawia sie zaproszenie
   (powiadomienie RPCS3 / ekran FIFA). Zapisz godzine, jesli sie pojawi.
3. Ponow zaproszenie kolejny raz (serwer usunie stara gre i wezmie kolejny poziom: 1 -> 2 -> ... -> 5). Zaproszenie moze
   pojawic sie na dowolnym poziomie; po jego zaakceptowaniu kolega wysle `joinGame` (widac w `gm_attempts.log`).
   Gdy zaproszenie dojdzie, przypnij poziom: w `config.json` `"gm_variant": <numer>` i zrestartuj serwer.
   Jesli emulator hosta sie zawiesi/wywali (w logu RPCS3 `Access violation` / `Emulation has been frozen`), zamknij
   tylko emulator hosta (serwer zostaje), uruchom go ponownie i ponow -- to rozlaczenie NIE liczy sie jako porazka
   poziomu, ten sam poziom zostanie powtorzony (po drugim takim rozlaczeniu na jednym poziomie uznany za nieudany).
   Zapisz, co WIDAC na ekranie hosta i kolegi po kazdej probie (czy jest "please wait", blad, powrot do menu,
   okno zaproszenia) i po ilu sekundach.
4. Po tescie (serwer moze dzialac dalej): `powershell -ExecutionPolicy Bypass -File .\collect_logs.ps1` -- tworzy
   `fifa17_logs_<data>.zip` (logi sesji serwera, `gm_attempts.log`, przechwyty polaczen z 3 godzin, `state\gm_variant.json`,
   `config.json`). Wyslij ten zip + `RPCS3.log` hosta i kolegi (RPCS3.log trzeba skopiowac zaraz po tescie, zanim
   emulator nadpisze go przy nastepnym uruchomieniu).

### Co serwer zapisuje sam (`logs/`)
- `session_<data>.log` -- caly log konsoli (nie trzeba nic wklejac z terminala); na poczatku wersja kodu i config.
- `gm_attempts.log` -- dziennik prob: dla kazdego createGame poziom i powod wyboru, zadanie createGame, KAZDE powiadomienie
  GameManager wyslane przez serwer (rozkodowane), KAZDE zadanie klientow w ciagu 45 s (z czasem w ms od createGame)
  i wynik (`SUKCES poziomu n (po sondzie k)` albo `PORAZKA`, plus zestawienie zadan od kazdego gracza).
- Sondy: gdy host milczy po setupie, po 3/6/9 s serwer wypycha kolejne powiadomienia (PlatformHostInitialized; stan
  gracza CONNECTED + JoinCompleted; GameStateChange) i loguje, po ktorej sondzie klient ruszyl. Wylaczenie:
  `"gm_probes": false`.
Wymuszenie poziomu: `"gm_variant": 3` w `config.json` (1-5); `-1` = pojedyncze przelaczniki `gm_*`.
