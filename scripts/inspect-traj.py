import json, glob, os, sys

mode = sys.argv[1] if len(sys.argv) > 1 else 'list'

files = sorted(glob.glob('/home/node/.openclaw/agents/discord-friend/sessions/*.trajectory.jsonl'), key=os.path.getmtime)

if mode == 'list':
    for f in files[-12:]:
        try:
            with open(f) as fh:
                first = json.loads(fh.readline())
            ts = first.get('ts','')
            sk = first.get('sessionKey','?')
            print(f'{ts}  {os.path.basename(f)}  key={sk}')
        except Exception as e:
            print('err', f, e)
else:
    target = mode
    matches = [f for f in files if target in f]
    if not matches:
        print('not found:', target); sys.exit(1)
    f = matches[-1]
    print('file:', f)
    with open(f) as fh:
        lines = list(fh)
    print('lines:', len(lines))
    for ln in lines:
        try:
            o = json.loads(ln)
        except Exception:
            continue
        typ = o.get('type')
        ts = str(o.get('ts',''))[:19]
        if typ == 'prompt.submitted':
            d = o.get('data',{})
            sp = d.get('systemPrompt','')
            p = (d.get('prompt') or '')[:600]
            cron_in_sp = 'cron' in sp
            print(f'[{ts}] prompt.submitted sp_len={len(sp)} cron_in_sp={cron_in_sp} prompt_head={p!r}')
        elif typ == 'model.completed':
            d = o.get('data',{})
            ats = d.get('assistantTexts',[])
            usage = d.get('usage',{})
            print(f'[{ts}] model.completed aborted={d.get("aborted")} usage={usage} ats=')
            for t in ats:
                print('    ', t[:600])
        elif typ == 'session.ended':
            d = o.get('data',{})
            print(f'[{ts}] session.ended status={d.get("status")} aborted={d.get("aborted")}')
        else:
            print(f'[{ts}] {typ}')
