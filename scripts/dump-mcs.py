import json, sys
p = sys.argv[1]
lines = open(p).readlines()
mcs = [json.loads(l) for l in lines if json.loads(l).get('type') == 'model.completed']
print('total model.completed:', len(mcs))
for m in mcs:
    ts = str(m.get('ts',''))[:19]
    d = m.get('data',{})
    ats = d.get('assistantTexts',[])
    snippet = ats[0][:160] if ats else '<none>'
    print(f'  [{ts}] {snippet!r}')
