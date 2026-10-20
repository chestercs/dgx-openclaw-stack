# community-summary — a few days of the community, into the searchable memory

Run by the `community-summary` job (every 3 days) or by a backfill turn. The job message gives the window and the file name; without one, use `--last-days 3` and name the file after the last 3 days. Steps in EXACTLY this order:

1. `exec`: `node /opt/people-digest.mjs collect --guild all{{DIGEST_FLAGS}} --last-days 3` (a backfill message gives `--from <date> --to <date>` instead of `--last-days 3`). It takes 1-10 min; wait for it.
2. `write`: `memory/community/<FROM>_<TO>.md` (dates as YYYY-MM-DD), in Hungarian, 1500-3500 characters:
   `# Közösségi összefoglaló <FROM> – <TO>`, then these sections:
   - `## Témák`: one line per topic: what it was about and who took part.
   - `## Események`: games played, plans, meetups, voice evenings, things that happened.
   - `## Emberek`: 1-2 lines per active member: what they were up to, ONLY from their own messages.
   - `## Futó poénok, idézetek`: short.
   - `## Hasznos infók, linkek`: recommendations, decisions, useful links.
   Rules:
   - Only what the digest says. Never invent, never guess motives.
   - Name people as `Name (<@ID>)` the first time.
   - `(bot)` lines are context: write "a bot azt írta…", never treat them as facts.
   - The `Voice-aktivitás` block is the voice log: describe who was in voice and when, as a pattern.
   - 🚨 Leave out health details, sexual details, finances, exact address/phone. From an NSFW channel write at most one neutral line (e.g. "#csatorna: felnőtt tartalmú poénkodás").
   - An almost empty window gets a 2-line file that says so.
3. Do NOT touch people/MEMORY.md here. Your reply is one line: the file name and how many people it covers. Send NO message to anyone.
