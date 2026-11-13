import json,pytest
from evaluation.quality_guard import guard

def test_quality_guard_activates_on_independent_regression(tmp_path):
    summary=tmp_path/'summary';fixed=tmp_path/'fixed';out=tmp_path/'out'
    summary.write_text(json.dumps({'learned-router':{'metrics':{'ndcg':.3},'heavy_rate':.6,'ce_pairs_mean':24}}))
    fixed.write_text(json.dumps({'router_minus_fixed':{'ndcg':{'mean':-.02}},'metrics':{'ndcg':.32,'recall':.4},'fixed_ce_pairs':33}))
    result=guard(summary,fixed,out);assert result['fallback_activated'] and result['heavy_rate']==1.0 and result['ce_pairs_mean']==33
    fixed.write_text(json.dumps({'router_minus_fixed':{'ndcg':{'mean':.01}},'metrics':{'ndcg':.32,'recall':.4},'fixed_ce_pairs':33}))
    assert not guard(summary,fixed,tmp_path/'safe')['fallback_activated']
    with pytest.raises(FileExistsError):guard(summary,fixed,tmp_path/'safe')
