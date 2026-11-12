from evaluation_backend.aspects import snapshot_scorers, aspect_results
from evaluation_backend.storage.service import EvaluationStorageService
from memory_storage import Memory


def test_snapshot_missing_scores_and_mock_are_separate():
    service=EvaluationStorageService(Memory()); s=service.storage
    spec={'version':1,'name':'Risk','criteria':{'aspect':'safety','requirements':'Match label'}}
    s.put('scorers','p','risk',{'versions':[spec]})
    bindings=snapshot_scorers(service,'p',['risk','tone'],{'evaluation':{'scorers':{'risk':{'version':'1'}}}})
    assert bindings[0]['aspect']=='safety' and bindings[1]['aspect']=='unclassified'
    s.put('scorers','p','risk',{'versions':[]})
    assert bindings[0]['definition']==spec
    s.put('run-contexts','p','r',{'datasets':[{'dataset_id':'d','dataset_version':1,'sample_count':4,'scorer_bindings':bindings}], 'feature_modes':{'scoring_enabled':True}})
    for i,scores in enumerate([[{'scorerId':'risk','passed':True,'source':'real'}],[],[{'scorerId':'risk','passed':True,'source':'mock'}]]):
        s.put('samples','p',str(i),{'evaluation_id':'r','dataset_id':'d','status':'PASS','scores':scores})
    report=aspect_results(service.storage.get('run-contexts','p','r'), service.report_samples('p','r'))
    real=next(g for g in report['aspects'] if (g['aspect'],g['source'])==('safety','real'))
    assert real['evaluated']==2 and real['passed']==1 and real['missing']==1 and real['pass_rate']==.5 and real['pending']==1
    mock=next(g for g in report['aspects'] if (g['aspect'],g['source'])==('safety','mock'))
    assert mock['passed']==1


def test_same_aspect_is_scored_separately_per_dataset():
    service=EvaluationStorageService(Memory());store=service.storage
    binding={'id':'risk','aspect':'safety','name':'Risk','version':'1'}
    store.put('run-contexts','p','r',{'datasets':[{'dataset_id':d,'dataset_version':1,'sample_count':1,'scorer_bindings':[binding]} for d in ('a','b')]})
    for d,passed in [('a',True),('b',False)]:
        store.put('samples','p',d,{'evaluation_id':'r','dataset_id':d,'status':'PASS' if passed else 'FAIL','scores':[{'scorerId':'risk','passed':passed,'source':'real'}]})
    result=aspect_results(service.storage.get('run-contexts','p','r'), service.report_samples('p','r'))
    assert {g['dataset_id']:g['pass_rate'] for g in result['dataset_aspects']}=={'a':1,'b':0}
    assert result['aspects'][0]['pass_rate']==.5
