import json, glob, os, sys

target = sys.argv[1]
files = sorted(glob.glob('/home/node/.openclaw/agents/discord-friend/sessions/*.trajectory.jsonl'), key=os.path.getmtime)
matches = [f for f in files if target in f]
f = matches[-1]
print('file:', f)
with open(f) as fh:
    lines = list(fh)
ps = [json.loads(ln) for ln in lines if json.loads(ln).get('type') == 'prompt.submitted']
last = ps[-1].get('data', {})
sp = last.get('systemPrompt', '')
prompt = last.get('prompt', '')
# find tool listing section
idx_tool = sp.find('## Tooling')
idx_cron = sp.find('cron')
idx_workspace = sp.find('## Workspace')
print(f'sp len: {len(sp)}')
print(f'cron at: {idx_cron}')
print(f'## Tooling at: {idx_tool}')
print('--- Tool section (Tooling -> Workspace) ---')
end = idx_workspace if idx_workspace > idx_tool else idx_tool + 4000
print(sp[idx_tool:end][:3500])
print('--- prompt head ---')
print((prompt or '')[:1200])
