"""Quality guard for a learned CE budget Router."""
import argparse,json
from pathlib import Path

def guard(summary,fixed_control,output,margin=0.0):
    summary=Path(summary);fixed_control=Path(fixed_control);output=Path(output)
    if output.exists():raise FileExistsError(output)
    learned=json.loads(summary.read_text());fixed=json.loads(fixed_control.read_text())
    delta=fixed['router_minus_fixed']['ndcg']['mean'];fallback=delta < -float(margin)
    result={'schema_version':1,'policy':'quality-guarded-fixed-heavy','fallback_activated':fallback,
            'acceptance_margin':float(margin),'learned_router_ndcg':learned['learned-router']['metrics']['ndcg'],
            'fixed_action_ndcg':fixed['metrics']['ndcg'],'learned_minus_fixed_ndcg':delta,
            'fixed_action_recall':fixed['metrics']['recall'],'heavy_rate':1.0 if fallback else learned['learned-router']['heavy_rate'],
            'ce_pairs_mean':fixed['fixed_ce_pairs'] if fallback else learned['learned-router']['ce_pairs_mean'],
            'quality_guard_reason':'independent same-budget quality regression' if fallback else 'no regression',
            'production_default_safe':fallback,'quality_savings_tradeoff':'zero vector/CE savings when fallback activates'}
    output.write_text(json.dumps(result,indent=2)+'\n');return result
if __name__=='__main__':
    p=argparse.ArgumentParser(__doc__)
    for n in ('summary','fixed-control','output'):p.add_argument('--'+n,required=True)
    p.add_argument('--margin',type=float,default=0.0);print(json.dumps(guard(**vars(p.parse_args())),indent=2))
