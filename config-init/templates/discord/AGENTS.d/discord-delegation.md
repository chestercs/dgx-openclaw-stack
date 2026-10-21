## Delegation — sessions_spawn

Work too big for one turn goes to a sub-agent, started in the SAME turn you announce it: text alone stops when the turn ends.
- Long work, a coding project, "dolgozz rajta egy napig" → `sessions_spawn {"thread":true,"taskName":"<short-kebab-name>","cleanup":"keep","context":"isolated","task":"…"}`. It gets its own thread; one task = one thread.
- A hard task you want back now → `sessions_spawn {"context":"isolated","task":"…"}`, then `sessions_yield`; the result arrives as the next message: summarise it. Never poll with sessions_list.
- The `task` is all the child knows: the goal, the inputs, what "done" means, the limits, and the reply language ("Az eredményt magyarul írd.").
- Status → `/subagents list`. No runId means no background work; never call it done before the child does.
- A simple 1–2 tool request stays with you.
