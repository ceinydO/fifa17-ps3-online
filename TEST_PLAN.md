# Plan testu (nastepna sesja)

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

## Drabinka poziomow NotifyGameSetup (test po 2026-10-10)
Cel: znalezc najnowszy ksztalt pierwszego setupu, po ktorym klient hosta dochodzi do `finalizeGameCreation`
(patrz README, "Test 2026-10-10" i "drabinka poziomow").
1. Host i kolega: `git pull`. Host uruchamia `.\update_and_run.ps1` (serwer ma dzialac przez wszystkie proby).
   Opcjonalnie skasuj `state/gm_variant.json`, zeby zaczac od poziomu 1.
2. Host: Online Friendlies -> zaproszenie kolegi. Poczekaj ~15 s. W logu serwera: `POZIOM n (...)`, potem
   `finalizeGameCreation od ... SUKCES poziomu n` albo `WATCHDOG ... BRAK finalizeGameCreation`.
3. Po sukcesie ekran hosta wroci do poprzedniego widoku (jak dawniej) -- zrob zaproszenie JESZCZE RAZ: serwer
   najpierw usunie stara gre (`NotifyGameRemoved` dla hosta i kolegi), potem sprobuje poziom wyzej. Po porazce
   (watchdog usuwa gre) tez po prostu ponow; serwer wroci do dzialajacego poziomu.
   Jesli emulator hosta sie zawiesi/wywali (w logu RPCS3 `Access violation` / `Emulation has been frozen`), zamknij
   tylko emulator hosta (serwer zostaje), uruchom go ponownie i ponow -- to rozlaczenie NIE liczy sie jako porazka
   poziomu, ten sam poziom zostanie powtorzony (po drugim takim rozlaczeniu na jednym poziomie uznany za nieudany).
   Zapisz, co WIDAC na ekranie hosta i kolegi po kazdej probie (czy jest "please wait", blad, powrot do menu,
   okno zaproszenia).
4. Powtarzaj, az poziom 6 przejdzie albo zobaczysz, od ktorego poziomu przestaje dzialac.
5. Po tescie (serwer moze dzialac dalej): `powershell -ExecutionPolicy Bypass -File .\collect_logs.ps1` -- tworzy
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
Wymuszenie poziomu: `"gm_variant": 4` w `config.json` (1-6); `-1` = pojedyncze przelaczniki `gm_*`.
