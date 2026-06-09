# HEARTBEAT — autonóm folyamatos haladás (discord-friend)

Ez a fájl a discord-friend agent workspace-ében (`workspace-discord/HEARTBEAT.md`) él.
A heartbeat (0-24, 30 percenként, izolált friss session) beolvassa és követi.

tasks:

- name: advance-active-project
  interval: 30m
  prompt: |
    Van-e AKTÍV fejlesztési projekt befejezetlen TODO-val? Forrás: a legutóbbi canvas-beli
    projekt (`/home/node/.openclaw/canvas/<projekt>/`) docs/spec/TODO fájljai + `memory/roadmap.md`.
    HA van befejezetlen lépés:
      1. Csináld meg a KÖVETKEZŐ EGY, kicsi, ellenőrizhető lépést (kód / doc / asset).
      2. Kódprojektnél: buildeld / ellenőrizd a lépést; commitold a `python_sandbox__git_push`-sal
         (`force=true` ha a remote divergál, vagy új repo-név).
      3. Jelöld a lépést késznek a projekt saját TODO-jában.
      4. Fűzz EGY rövid sort a `memory/heartbeat-progress.md`-hez: a mai dátum + mit csináltál + mi a következő lépés.
    SZABÁLYOK: EGY lépés / heartbeat (ne csinálj többet egy ciklusban). SOHA ne találj ki munkát —
    csak valódi, dokumentált TODO-n haladj. Kis, visszafordítható lépések (a git a védőháló).
    HA nincs befejezetlen task, VAGY blokkolva vagy, VAGY user-döntés kell:
    válaszolj `HEARTBEAT_OK` (ne spamelj, ne ígérj jövőbeli/háttér-munkát a következő heartbeaten túl).
