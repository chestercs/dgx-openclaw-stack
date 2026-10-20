# people-memory — nightly persona update

Run by the `people-memory-nightly` job; the job message gives the guild ID (`all` = every server the bot is in). Steps in EXACTLY this order:

1. `exec`: `node /opt/people-digest.mjs collect --guild <GUILD_ID>{{DIGEST_FLAGS}}` — the messages of the past day, per author (2-10 min, wait for it). With several servers each line starts with `[#server/channel]`.
2. `read`: `people/MEMORY.md` — the current file.
3. Per author, decide whether they stated a NEW, LASTING fact ABOUT THEMSELVES. Belongs here: job/studies, hobbies, games they play, pets, city, family role (e.g. has a child), own project, favourite things, a nickname they accept, a running joke.
   🚨 NOT here: politics, religion, health, sexuality, finances, conflicts/insults, a one-off mood, ironic exaggeration, claims about someone else, exact address/phone/employer name. Unsure → leave it out. Max 3 new lines per person per night.
   - **Style:** one `- (stílus) …` line per person who wrote enough to show a pattern (roughly 10+ messages): how they talk and what tone works with them — humour (szárkasztikus, abszurd, szóviccek…), register (laza, káromkodós, udvarias), short quips or long explanations, emoji/GIF use, what they like to banter about, how they talk to the bot. Describe a pattern seen in SEVERAL messages, never one message. No verdicts on character or intelligence, no psychologising, nothing from the 🚨 list. Max ~25 words. An existing `(stílus)` line is REPLACED only when the new picture clearly differs; never a second one. It does not count toward the 3 lines.
   - `## (bot) …` sections are other bots (e.g. a Markov chat bot): NEVER a person, NEVER a `##` section of their own, no facts from their text. Use them only as context for what the humans said to them (e.g. a running joke with the bot).
   - `## Voice-aktivitás` is the voice log of that day: record at most a lasting HABIT (e.g. "Esténként 21 után szokott voice-ban lenni."), and only if the person's existing lines or several entries back it. Never record single join/leave times.
4. `write`: the COMPLETE new file into `people/MEMORY.next.md` (NOT into MEMORY.md):
   - every existing section and line STAYS; you may only replace an outdated or contradicted line; NEVER change a "végleges szabály" line;
   - an `(ideiglenes, <date>)` line: DELETE it if the date is older than 2 days or the person has said something else since; otherwise keep it;
   - empty or duplicate `##` headers (the same name/ID twice) → merge them into one;
   - a new person → a new `## Name (<@ID>)` section at the end of the file, the ID from the digest header; add a missing `(<@ID>)` to an existing header;
   - one fact = one short line, written in Hungarian, in the third person (e.g. "Szegeden lakik."); keep the whole file under 8000 characters.
5. `exec`: `node /opt/people-digest.mjs apply` → `APPLIED` = done. `REJECTED: <reason>` → fix the proposal by the reason, write it again, run it again (max 2 attempts). Nothing new: `node /opt/people-digest.mjs apply --no-change`.
6. Your reply is one line: how many people got new lines. Send NO message to anyone.
