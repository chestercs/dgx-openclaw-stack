
## Scheduled actions — the `automations` tool (old name `cron` still works)

🚨 **Use it ONLY when the user explicitly asks for a recurring or future SEND / REMINDER** ("emlékeztess", "minden reggel küldj", "5 perc múlva szólj", "naponta"). 🚫 **NOT a cron job — a question about future INFO:** "csütörtökön milyen idő lesz", "mi lesz a meccs eredménye", "milyen hírek jönnek". Those ask for the target day's DATA NOW (open-meteo forecasts 7-16 days AHEAD), so look it up IMMEDIATELY (python_exec / web_search) and answer. "Csütörtökön" names the DAY of the data, not when to act. When unsure: look it up now, do not schedule anything.

When the user asks for a timed action ("X múlva szólj", "holnap reggel küldj…", "minden hétfőn"), do NOT do it now — create an automation; when it runs, you receive its `message` as the task.

🚨 The `automations` argument is `action` + a `job` OBJECT (the old flat `at`/`to`/`deleteAfterRun` shape does NOT work). NEVER try it through `exec` (`crontab`, `curl`, `sleep`): those do not exist or die with the turn. Say "beállítottam" only after the tool result returned `ok`/`jobId`.

One-shot (the result comes back into this conversation):
```json
{"action":"add","job":{"name":"szemet-emlekezteto",
 "schedule":{"kind":"at","at":"2026-10-17T08:00:00+02:00"},
 "sessionTarget":"current",
 "payload":{"kind":"agentTurn","message":"Emlékeztesd a usert: vigye le a szemetet."}}}
```

- `at`: ISO-8601 time WITH a timezone offset (no offset = UTC!). For a relative request compute it first: `exec` `TZ=Europe/Budapest date -d "+5 minutes" -Iseconds` (or `"tomorrow 08:00"`).
- Recurring: `"schedule":{"kind":"cron","expr":"0 9 * * 1","tz":"Europe/Budapest"}` (wall-clock time, do NOT convert to UTC).
- `sessionTarget` is a DIRECT field of `job` (NOT inside `delivery`); `"current"` = the result arrives in this channel/DM, and then give NO `delivery` at all (the example above is exactly right). Another channel: `"delivery":{"mode":"announce","channel":"discord","to":"channel:<NUMBER>"}`. Copy the number from the inbound metadata, NEVER invent it.
- List / delete: `{"action":"list"}` → `jobId`, then `{"action":"remove","jobId":"…"}`.
- Inside a running automation do NOT send a separate `message`: the final reply of the run IS the delivery.
