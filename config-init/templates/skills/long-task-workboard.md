## Long work on a workboard card

For multi-hour / multi-step trackable work (the user may ask for it: "kövessük kártyán") call the workboard TOOLS (`/workboard …` typed in chat does NOT run — that is the user's slash command):
- `workboard_create` — a card with title + description; can be bound to an agent.
- `workboard_dispatch` — ready cards start as subagent worker runs; the card stores the runId, the session key and the worker log.
- `workboard_list` / `workboard_read` — status; `workboard_complete` — mark done.
- Completion notices are push-based (announce / heartbeat wake) — do NOT poll in a loop.
- For "1 napos" work: card + dispatch + give the card ID back to the user — the status can be checked with it any time, and the work runs past the end of the turn (a worker run, not your session).
