"""Independent audit for the full-corpus cheap-feature quality guard HTTP run."""
import argparse, hashlib, json
from pathlib import Path
import numpy as np
from evaluation.prepare_bm25_router import metrics
from evaluation.train_bm25_router import group_bootstrap
from evaluation.router_metrics import percentile
from evaluation.dureader_policy_features import FrozenLinear
from online.parser import parse


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def load_qrels(path):
    out={}
    for line in Path(path).read_text().splitlines():
        q,_,d,g=line.split(); out.setdefault(q,{})[d]=float(g)
    return out

def audit(folder, qrels_path, models_path, queries_path, max_cost=36.49):
    folder=Path(folder); qrels_path=Path(qrels_path); qrels=load_qrels(qrels_path)
    models=json.loads(Path(models_path).read_text()); gate=FrozenLinear(models['gate']['model'])
    query_rows={r['query_id']:r for r in (json.loads(line) for line in Path(queries_path).open())}
    if len(query_rows)!=808: raise ValueError('query coverage mismatch')
    rows=[json.loads(line) for line in (folder/'requests.jsonl').open()]
    by={(r['query_id'],r['policy']):r for r in rows}
    policies={'quality-guard-40','fixed-pair-budget'}
    if len(rows)!=1616 or set(r['policy'] for r in rows)!=policies:
        raise ValueError('request coverage is not exactly 808 x 2')
    if any((q,p) not in by for q in {r['query_id'] for r in rows} for p in policies):
        raise ValueError('unbalanced query/policy coverage')
    queries=sorted({r['query_id'] for r in rows})
    if len(queries)!=808 or not set(queries)<=set(qrels): raise ValueError('qrels coverage mismatch')
    cheap=[by[q,'quality-guard-40'] for q in queries]
    fixed=[by[q,'fixed-pair-budget'] for q in queries]
    if any(r['routing']['fallback'] if 'routing' in r else False for r in rows): raise ValueError('HTTP fallback occurred')
    if any(r['ce_pairs'] not in (33,40) for r in cheap+fixed): raise ValueError('unexpected CE budget')
    if any(r['ce_pairs'] != (40 if r['gate_heavy'] else 33) for r in cheap): raise ValueError('action/budget mismatch')
    for r in cheap:
        predicted=bool(not parse(query_rows[r['query_id']]['text']).get('explicit_constraints',False) and gate.predict([r['gate_features']])[0] > 0.153)
        if predicted != bool(r['gate_heavy']): raise ValueError('HTTP/offline gate decision mismatch')
    cost=float(np.mean([r['ce_pairs'] for r in cheap])); rate=float(np.mean([r['gate_heavy'] for r in cheap]))
    if cost>max_cost: raise ValueError('cost gate failed')
    result={'queries':len(queries),'heavy_count':int(sum(r['gate_heavy'] for r in cheap)),
            'heavy_rate':rate,'ce_pairs_mean':cost,'requests_sha256':sha(folder/'requests.jsonl'),
            'qrels_sha256':sha(qrels_path)}
    for name in ('ndcg','recall'):
        delta=np.array([metrics(by[q,'quality-guard-40']['results'],qrels[q])[name]-metrics(by[q,'fixed-pair-budget']['results'],qrels[q])[name] for q in queries])
        result[name]={'delta':float(delta.mean()),'grouped_ci95':group_bootstrap(delta,np.asarray(queries))}
        if result[name]['grouped_ci95']['ci95'][0] <= 0: raise ValueError(name+' quality CI does not clear zero')
    result['latency_ms']={p:{'p50':percentile([by[q,p]['client_wall_ms'] for q in queries],.5), 'p95':percentile([by[q,p]['client_wall_ms'] for q in queries],.95)} for p in policies}
    result['passed']=True
    (folder/'audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--folder',type=Path,required=True);p.add_argument('--qrels',type=Path,required=True);p.add_argument('--models',type=Path,required=True);p.add_argument('--queries',type=Path,required=True);p.add_argument('--max-cost',type=float,default=36.49)
    a=p.parse_args(); print(json.dumps(audit(a.folder,a.qrels,a.models,a.queries,a.max_cost),ensure_ascii=False,indent=2))
