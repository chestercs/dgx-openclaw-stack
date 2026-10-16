# HEARTBEAT — opt-in autonóm haladás (discord-friend)

A heartbeat 30 percenként, izolált friss sessionben fut. **Alapállapot: nincs teendő.**
Autonóm munka CSAK akkor van, ha a user kifejezetten kérte, és ez a `memory/active-project.md`
fájlban rögzítve van.

1. Hívj EGY `read`-et: `/home/node/.openclaw/workspace-discord/memory/active-project.md`.
   - Ha a fájl nem létezik, üres, vagy nincs benne `status: active` sor → válaszolj pontosan
     `HEARTBEAT_OK`, és SEMMILYEN más toolt ne hívj. Ne keress, ne listázz mappákat, ne találj ki projektet.
2. Ha `status: active`: a fájl megadja a projekt mappáját (`path:`) és a TODO fájl pontos útvonalát
   (`todo:`). CSAK ezen a kettőn belül dolgozz.
   - Csináld meg a TODO KÖVETKEZŐ EGY kicsi, ellenőrizhető lépését; jelöld késznek a TODO-ban.
   - Kódprojektnél commitold a `python_sandbox__git_push`-sal, ha a fájl `repo:` sort is tartalmaz.
   - Fűzz egy sort a `memory/heartbeat-progress.md`-hez: dátum + mit csináltál + mi a következő lépés.
3. Hibakezelés: `edit` „Could not find the exact text” → `read`-eld be a fájlt, és EGYSZER próbáld újra.
   Ha megint elbukik, vagy hiányzik egy fájl, vagy user-döntés kell: írd az `active-project.md`-be
   `status: blocked` + egy sor indoklás, és válaszolj `HEARTBEAT_OK`. Ugyanazt a hívást SOHA ne ismételd.
4. Ha a TODO-ban nincs több nyitott lépés: `status: done`, és `HEARTBEAT_OK`.

Egy heartbeat = legfeljebb egy lépés. Ne ígérj háttérmunkát, ne küldj üzenetet a usernek, hacsak
az `active-project.md` `notify:` sora kifejezetten kéri.

Bekapcsolás (user kérésére, Discordon): „dolgozz ezen magadtól” → hozd létre a
`memory/active-project.md`-t így:

```
status: active
path: /home/node/.openclaw/canvas/<projekt>/
todo: /home/node/.openclaw/canvas/<projekt>/TODO.md
repo: <github-repo-név, opcionális>
notify: <channel:<id>, opcionális>
```
