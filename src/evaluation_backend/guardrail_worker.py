"""Direct Scan execution with runner-local pinned Git scoring."""
import os
import time
import httpx
from evaluation_backend import tracing
from evaluation_backend.observability import correlation_headers
from evaluation_backend.storage.client import StorageClient
from evaluation_backend.guardrail_contracts import ClassificationResult, GuardrailRequestResult
from evaluation_backend.observability import configure_logging, event, shutdown_logging


def run(client=None):
    tracing.configure()
    client = client or StorageClient()
    ident = os.environ['EXPERIMENT_ID']
    payload = client.run_input(ident)
    detail = client.call('GET', 'guardrail-detail/'+ident)
    contents, bindings = payload['datasets'], payload['guardrail_bindings']
    summary = {'dataset_id': contents[0]['dataset_id'], 'dataset_version': contents[0]['version'],
               'items':0,'errors':0,'status':'RUNNING','dataset_results':[]}
    for content in contents:
        summary['dataset_results'].append({'dataset_id':content['dataset_id'],'dataset_version':content['version'],
            'name':content['name'],'sample_count':len(content['rows']),'items':0,'errors':0,'status':'QUEUED'})
    client.experiment(ident,summary)
    with httpx.Client(timeout=30,trust_env=False) as http:
        for di, content in enumerate(contents):
            binding = next(b for b in bindings if b['dataset_id']==content['dataset_id'])
            progress = summary['dataset_results'][di]; progress['status']='RUNNING'
            for ri,row in enumerate(content['rows']):
                sample_id=f'dataset-{di:03d}-sample-{ri:08d}'
                with tracing.span('evaluation.request',{'evaluation.sample_id':sample_id,'evaluation.dataset_id':content['dataset_id'],'evaluation.binding_id':binding['binding_id']}) as request_span:
                    started=time.perf_counter();stage='target'
                    label=binding['label_mapping'][str(row['label'])]
                    g={'request_id':sample_id,'row_id':str(ri),'binding_ids':[binding['binding_id']],
                       'strategy':row['strategy'],'label':label,'status':'ERROR','sent':False,
                       'guardrail_latency_ms':0,'scorer_latency_ms':0,'total_latency_ms':0,
                       'cost':{'amount':0,'currency':'USD','source':'mock','policy_version':'mock-v1'},'classifications':[]}
                    sample={'id':sample_id,'dataset_id':content['dataset_id'],'dataset_version':content['version'],
                        'input':{'input':row['input']},'reference':{'label':row['label'],'strategy':row['strategy']},
                        'output':{},'status':'ERROR','scores':[],'trace':[],'costUsd':0,'latencyMs':0,'error':None}
                    event('guardrail.sample.started',run_id=ident,sample_id=sample_id)
                    try:
                        before=time.perf_counter();g['sent']=True;g['cost']['amount']=.001
                        try:
                            with tracing.span('guardrail.scan',{'http.request.method':'POST','evaluation.sample_id':sample_id,'evaluation.cost.amount':.001,'evaluation.cost.currency':'USD','evaluation.cost.source':'mock'},client=True) as http_span:
                                from urllib.parse import urlsplit
                                endpoint=urlsplit(detail['endpoint'])
                                http_span.set_attribute('server.address',endpoint.hostname or '')
                                http_span.set_attribute('url.path',endpoint.path)
                                body={'input':row['input'],'verbose':True,'flagOnly':False}
                                tracing.payload(http_span,'input',body)
                                response=http.post(detail['endpoint'],headers={'Authorization':'Bearer '+detail['token'],**correlation_headers()},json=body)
                                http_span.set_attribute('http.response.status_code',response.status_code)
                                try: tracing.payload(http_span,'output',response.json())
                                except ValueError: tracing.payload(http_span,'output',response.text)
                                if response.status_code>=400:
                                    from opentelemetry.trace import Status,StatusCode
                                    http_span.set_status(Status(StatusCode.ERROR,'HTTP '+str(response.status_code)))
                        finally:g['guardrail_latency_ms']=(time.perf_counter()-before)*1000
                        g['http_status']=response.status_code
                        response.raise_for_status();stage='response';sample['output']=response.json()
                        stage='scorer';before=time.perf_counter()
                        try:
                            from evaluation_backend.git_scorers import execute
                            value={'result':execute(binding['scorer_execution'],{'input':row['input'],'label':label,
                                'strategy':row['strategy'],'output':sample['output']})}
                            classification=ClassificationResult(binding_id=binding['binding_id'],source='mock',**value['result'])
                            g['classifications']=[classification.model_dump()];g['status']='COMPLETED'
                        finally:g['scorer_latency_ms']=(time.perf_counter()-before)*1000
                        sample['status']='PASS' if classification.outcome in ('TP','TN') else 'FAIL'
                    except Exception as error:
                        g.update(error_stage=stage,error_code=type(error).__name__)
                        sample['error']=f'{stage} execution failed'
                        from opentelemetry.trace import Status,StatusCode
                        request_span.set_status(Status(StatusCode.ERROR,type(error).__name__))
                        progress['errors']+=1;summary['errors']+=1
                        event('guardrail.sample.failed',level='ERROR',run_id=ident,sample_id=sample_id,error=error)
                    g['total_latency_ms']=(time.perf_counter()-started)*1000
                    sample.update(guardrail_result=GuardrailRequestResult.model_validate(g).model_dump(),
                        latencyMs=g['total_latency_ms'],costUsd=g['cost']['amount'])
                client.sample(ident,sample)
                progress['items']+=1;summary['items']+=1
                client.experiment(ident,summary)
                event('guardrail.sample.completed',run_id=ident,sample_id=sample_id,status=sample['status'])
            progress['status']='FAILED' if progress['errors'] else 'COMPLETED'
        summary['status']='FAILED' if summary['errors'] else 'COMPLETED'
        client.experiment(ident,summary)
    return summary


if __name__=='__main__':
    import json
    configure_logging('guardrail-worker')
    try:
        result=run();print(json.dumps({**result,'evaluation_id':os.environ['EXPERIMENT_ID']}))
        code=1 if result['errors'] else 0
    except Exception as error:
        event('guardrail.worker.failed',level='ERROR',error=error);code=1
    finally:
        tracing.shutdown()
        shutdown_logging()
    raise SystemExit(code)
