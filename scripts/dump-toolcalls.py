import json, sys, os

p = sys.argv[1] if len(sys.argv) > 1 else '/home/node/.openclaw/agents/discord-friend/sessions/cf74e2a5-6071-4001-a386-cfc8b764163b.trajectory.jsonl'
lines = open(p).readlines()
mcs = [json.loads(l) for l in lines if json.loads(l).get('type') == 'model.completed']
n = int(sys.argv[2]) if len(sys.argv) > 2 else 4
for i, mc in enumerate(mcs[-n:]):
    d = mc.get('data', {})
    ms = d.get('messagesSnapshot', [])
    ts = mc.get('ts', '')[:19]
    print(f'--- session #{len(mcs)-n+i+1} @ {ts} (last 4 messages) ---')
    for m in ms[-4:]:
        role = m.get('role')
        c = m.get('content')
        if isinstance(c, list):
            for part in c[:5]:
                t = part.get('type')
                if t == 'toolCall':
                    name = part.get('name')
                    args = json.dumps(part.get('arguments'))
                    print(f'  [{role}] toolCall name={name} args={args[:300]}')
                elif t == 'text':
                    txt = part.get('text', '')
                    print(f'  [{role}] text: {txt[:300]}')
                else:
                    print(f'  [{role}] {t}: {str(part)[:250]}')
        else:
            print(f'  [{role}] {str(c)[:250]}')
