#!/usr/bin/env python3
"""Web search quality bench: is the expected fact in the top N results?

Each case is a fact question with a regex the answer must match somewhere in
the titles, URLs or snippets of the top results. Three sets of 20 (10
Hungarian, 10 English): BENCH_SET=1 (default), 2 or 3. Run it on the Docker
host; it calls the containers by name.

  scripts/websearch-bench.py searxng            # through the gateway, as the bot sees it
  scripts/websearch-bench.py parallel-free      # any provider from `openclaw infer web providers`
  scripts/websearch-bench.py yahoo 5 searx      # one SearxNG engine, raw JSON
  scripts/websearch-bench.py default 5 searx hu # all engines, Hungarian cases only

Env for the searx mode: SEARX_URL (default http://searxng:8080), SEARX_LANG,
SEARX_CATS. A regex miss on a correct page happens (the snippet lacks the
number), so read the MISS lines, not only the score.
"""
import json, os, re, subprocess, sys, time, urllib.parse

CASES = [
  # (lang, query, regex that a good top-5 must contain)
  ("hu", "autó tengeralattjáró ütközés", r"1961|Collision_between_a_car_and_a_submarine|tengeralattjáró.{0,40}autó|autó.{0,40}tengeralattjáró"),
  ("en", "car collided with submarine", r"1961|Collision_between_a_car_and_a_submarine"),
  ("hu", "Kékestető tévétorony magassága", r"180"),
  ("hu", "Magyarország legmagasabb hegye", r"Kékes|1014"),
  ("hu", "Petőfi Sándor születési helye", r"Kiskőrös"),
  ("hu", "Szent István halálának éve", r"1038"),
  ("hu", "Duna hossza km", r"2850|2 850|2,850|2860"),
  ("hu", "Debrecen polgármestere", r"Papp László"),
  ("hu", "Valheim Ashlands frissítés megjelenése", r"2024|május|May"),
  ("hu", "ki nyerte a 2024-es Forma-1 világbajnokságot", r"Verstappen"),
  ("hu", "mi az a MeshCore LoRa", r"[Mm]esh[Cc]ore"),
  ("en", "Python 3.13 release date", r"October 7, 2024|2024-10-07|7 October 2024|Oct(ober)? 7"),
  ("en", "Mount Everest height meters", r"8,?849|8848"),
  ("en", "docker compose depends_on service_healthy", r"service_healthy"),
  ("en", "SearXNG enable json format settings.yml", r"formats|json"),
  ("en", "Valheim Ashlands release date", r"May 14, 2024|14 May 2024|2024-05-14"),
  ("en", "who won 2024 Formula 1 world championship", r"Verstappen"),
  ("en", "NVIDIA DGX Spark GB10 memory", r"128 ?GB"),
  ("en", "Kursk submarine disaster year", r"2000"),
  ("en", "population of Hungary 2024", r"9[.,]5|9[.,]6|9 5\d\d|9 6\d\d|9,5\d\d|9,6\d\d|million"),
]

CASES2 = [
  ("hu", "Rubik Ernő találmánya", r"[Kk]ocka|[Cc]ube"),
  ("hu", "Balaton legmélyebb pontja", r"12|Tihany"),
  ("hu", "hány lakosa van Szegednek", r"1[56]d ?d{3}|16d|15d"),
  ("hu", "Ferencváros hány bajnoki címet nyert", r"3[0-6]"),
  ("hu", "hogyan kell bejelenteni lakcímváltozást", r"[Kk]ormányablak|lakcím|ügyfélkapu|magyarorszag.hu"),
  ("hu", "Nobel-díjas magyar Karikó Katalin díj éve", r"2023"),
  ("hu", "Mátyás király születésének éve", r"1443"),
  ("hu", "Linux kernel aktuális stabil verzió", r"6.d+"),
  ("hu", "mennyi a magyar minimálbér 2025", r"290 ?800|290.800|290800"),
  ("hu", "Zelda Breath of the Wild megjelenés éve", r"2017"),
  ("en", "who invented the Rubik's cube", r"Rubik|Ern"),
  ("en", "Lake Balaton maximum depth", r"12"),
  ("en", "Ferencvaros number of league titles", r"3[0-6]"),
  ("en", "Katalin Kariko Nobel prize year", r"2023"),
  ("en", "Rust programming language creator", r"Graydon Hoare|Mozilla"),
  ("en", "vLLM NVFP4 quantization support", r"NVFP4|FP4|nvfp4"),
  ("en", "how to resize a partition with parted", r"resizepart|parted"),
  ("en", "Factorio Space Age release date", r"October 21, 2024|21 October 2024|2024-10-21|Oct 21"),
  ("en", "Hungarian Grand Prix circuit name", r"Hungaroring"),
  ("en", "OpenClaw AI agent gateway", r"[Oo]pen[Cc]law"),
]

CASES3 = [
  ("hu", "Budapest Debrecen távolság autóval km", r"2[0-4]d ?km"),
  ("hu", "mohácsi csata éve", r"1526"),
  ("hu", "ki a Magyar Nemzeti Bank elnöke", r"Varga Mihály"),
  ("hu", "melyik évben nyílt meg a 4-es metró Budapesten", r"2014"),
  ("hu", "Revolut leállás", r"Revolut"),
  ("hu", "Wizz Air székhelye", r"Budapest|Jersey"),
  ("hu", "Kossuth-díj 2025 kitüntetettjei", r"Kossuth"),
  ("hu", "hogyan kell Valheimben portált építeni", r"[Pp]ort[aá]l|[Ss]urtling|[Ff]ine wood|finom fa|[Gg]reydwarf"),
  ("hu", "Nemotron nyelvi modell NVIDIA", r"Nemotron"),
  ("hu", "magyar autópálya matrica ára 2025", r"d{1,2} ?d{3} ?(Ft|forint)|matrica"),
  ("en", "GB10 Grace Blackwell CPU cores", r"20[- ]core|20 cores|20 Arm|10 Cortex-X925|X925"),
  ("en", "SearXNG duckduckgo CAPTCHA", r"CAPTCHA|captcha"),
  ("en", "Nemotron 3 Super active parameters", r"12B|12 ?billion|A12B"),
  ("en", "where to find Discord bot token", r"[Dd]eveloper [Pp]ortal"),
  ("en", "vLLM gemma4 tool call parser", r"gemma4|tool[-_ ]call[-_ ]parser"),
  ("en", "Battle of Mohacs year", r"1526"),
  ("en", "Hungarian National Bank governor", r"Varga"),
  ("en", "Budapest metro line 4 opening year", r"2014"),
  ("en", "Valheim how to build a portal", r"[Ss]urtling|[Ff]ine wood|[Gg]reydwarf|[Pp]ortal"),
  ("en", "Wizz Air headquarters", r"Budapest|Jersey|Geneva"),
]

def strip(s):
  return re.sub(r"<<<[^>]*>>>|Source: Web Search\n---\n", " ", s or "")

def cli(provider, q, limit):
  t = time.time()
  p = subprocess.run(["docker", "exec", "openclaw-cli", "openclaw", "infer", "web", "search",
                      "--provider", provider, "--query", q, "--limit", str(limit), "--json"],
                     capture_output=True, text=True, timeout=120)
  dt = time.time() - t
  try:
    d = json.loads(p.stdout[p.stdout.index("{"):])
    res = d["outputs"][0]["result"].get("results") or []
    if not res and d["outputs"][0]["result"].get("content"):
      res = [{"title": "", "url": "", "snippet": d["outputs"][0]["result"]["content"]}]
  except Exception as e:
    return [], dt, f"ERR {p.stderr[-200:] or e}"
  return [{"title": strip(r.get("title")), "url": r.get("url", ""),
           "snippet": strip(r.get("snippet") or r.get("description") or "")} for r in res[:limit]], dt, ""

def searx(engines, q, limit):
  t = time.time()
  params = {"q": q, "format": "json"}
  if engines != "default":
    params["engines"] = engines
  for k, env in (("language", "SEARX_LANG"), ("categories", "SEARX_CATS")):
    if os.environ.get(env):
      params[k] = os.environ[env]
  url = os.environ.get("SEARX_URL", "http://searxng:8080") + "/search?" + urllib.parse.urlencode(params)
  p = subprocess.run(["docker", "exec", "openclaw-cli", "curl", "-sS", "--max-time", "30", url],
                     capture_output=True, text=True, timeout=60)
  dt = time.time() - t
  try:
    d = json.loads(p.stdout)
  except Exception:
    return [], dt, "ERR " + p.stdout[:100]
  bad = ";".join(f"{e[0]}:{e[1][:20]}" for e in d.get("unresponsive_engines", []))
  return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", ""),
           "engines": r.get("engines")} for r in d["results"][:limit]], dt, bad

def main():
  provider = sys.argv[1]
  limit = int(sys.argv[2]) if len(sys.argv) > 2 else 5
  mode = sys.argv[3] if len(sys.argv) > 3 else "cli"
  only = sys.argv[4] if len(sys.argv) > 4 else ""
  hits = {"hu": [0, 0], "en": [0, 0]}
  times = []
  for lang, q, rx in {"2": CASES2, "3": CASES3}.get(os.environ.get("BENCH_SET"), CASES):
    if only and only != lang:
      continue
    res, dt, err = (cli if mode == "cli" else searx)(provider, q, limit)
    times.append(dt)
    blob = " ".join(f"{r['title']} {r['url']} {r['snippet']}" for r in res)
    ok = bool(re.search(rx, blob))
    hits[lang][0] += ok; hits[lang][1] += 1
    top = res[0]["url"][:70] if res else "-"
    print(f"{'OK ' if ok else 'MISS'} {lang} {dt:5.1f}s n={len(res)} {q[:45]:45} | {top} {err}", flush=True)
  tot = sum(h[0] for h in hits.values()), sum(h[1] for h in hits.values())
  times.sort()
  print(f"== {provider} [{mode}] hu {hits['hu'][0]}/{hits['hu'][1]}  en {hits['en'][0]}/{hits['en'][1]}  "
        f"total {tot[0]}/{tot[1]}  median {times[len(times)//2]:.1f}s")

if __name__ == "__main__":
  main()
