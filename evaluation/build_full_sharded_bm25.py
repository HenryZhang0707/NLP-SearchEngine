import subprocess,time
from pathlib import Path
src=Path('/root/autodl-tmp/dureader-full-shards-v1/corpus100k'); out=Path('/root/autodl-tmp/dureader-full-shards-v1/index100k'); py='/root/autodl-tmp/NLP-SearchEngine/.venv/bin/python'; env={'PYTHONPATH':'/root/autodl-tmp/NLP-SearchEngine-router'}
import os; env={**os.environ,**env}
parts=sorted(src.glob('part-*'))
for i,p in enumerate(parts):
 d=out/p.name
 if (d/'manifest.json').exists(): print('skip',i,p.name,flush=True);continue
 t=time.time(); print('build',i+1,'/',len(parts),p.name,flush=True)
 r=subprocess.run([py,'-u','-m','offline.build','--corpus',str(p),'--out',str(d),'--dataset',f'dureader-full-shard-{i:03d}','--k1','1.2','--b','0.75'],env=env)
 if r.returncode: raise SystemExit(r.returncode)
 print('done',i,p.name,'seconds',round(time.time()-t,1),flush=True)
