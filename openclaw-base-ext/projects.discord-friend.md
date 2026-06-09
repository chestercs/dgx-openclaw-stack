# Projektek — ImbulClaw (2026-06-10)

Három AKTÍV projekt. Mindegyik a `canvas/`-ban (local munkamásolat) ÉS GitHubon van.
🚨 **Minden értelmes coding-lépés után commit+push (`git_push`)** — a `canvas/` törölhető,
a GitHub a védőháló (2026-06-09: egy cleanup törölte a local projekteket; az unpushed kód elveszett).
🚨 A saját hosztolt oldaladat KÖZVETLENÜL nyisd meg `browser`-rel (ne `web_search` → reCAPTCHA).

## 1. cellular-automata — Conway Game of Life (vanilla JS canvas)
- Local: `/home/node/.openclaw/canvas/cellular-automata/`
- GitHub: https://github.com/melytengeribluggyhal/cellular-automata
- Élő: https://sandbox.petyuspolisz.com/cellular-automata/

## 2. maxpayne2-website — Max Payne 2 rajongói oldal (Angular + Tailwind, noir)
- Local: `/home/node/.openclaw/canvas/maxpayne2-website/`
- GitHub: https://github.com/melytengeribluggyhal/max-payne-2-website
- Élő: https://sandbox.petyuspolisz.com/max-payne-2/

## 3. maxpayne2-cpp — Max Payne 2 remake (Unreal Engine 5 + C++)
- Local: `/home/node/.openclaw/canvas/maxpayne2-cpp/`
- GitHub: https://github.com/melytengeribluggyhal/max-payne-2-unreal-cpp
- 🚨 **Ha Kerajoe (vagy bárki) a "max payne PROJEKTET / remake-et / játékot" kérdezi → MINDIG erre (maxpayne2-cpp) gondolj.** (A "max payne OLDAL/website" ≠ ez, az a #2.)

🚨 NINCS más aktív projekt — a régi Angular/Godot/teszt mappák + extra repók TÖRÖLVE.

## Build / hosztolás emlékeztető
- Webes app: build → `canvas/_site/<path>` symlink → a 8095-ös `http.server` szolgálja → `sandbox.petyuspolisz.com/<path>/`. Build után KEMÉNY refresh (cache).
- Angular: `src/index.html` = TEMPLATE (ne hardcode-olj `<script>`/`<link>` asset-taget); Tailwind class-okhoz `tailwind.config.js` + `@tailwind` direktívák kellenek (verify: styles-*.css >5KB).
