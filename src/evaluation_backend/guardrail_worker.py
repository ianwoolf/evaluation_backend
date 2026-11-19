"""Direct Scan execution with runner-local pinned Git scoring."""
import json
import os
import time
from urllib.parse import urlsplit
import httpx
from evaluation_backend import tracing
from evaluation_backend.observability import correlation_headers
from evaluation_backend.storage.client import StorageClient
from evaluation_backend.guardrail_contracts import ClassificationResult, GuardrailRequestResult
from evaluation_backend.observability import configure_logging, event, shutdown_logging


def request_error_reason(error, *, has_token):
    # HTTP exception text can contain credentials, response headers or payloads.
    if isinstance(error, httpx.HTTPStatusError):
        return f'Guardrail returned HTTP {error.response.status_code}'
    if isinstance(error, json.JSONDecodeError):
        return f'Invalid JSON response at line {error.lineno}, column {error.colno}'
    if isinstance(error, httpx.LocalProtocolError) and str(error).startswith('Illegal header value'):
        return 'Invalid Authorization header: empty Bearer token' if not has_token else 'Invalid HTTP header value'
    if isinstance(error, httpx.ConnectError):
        message = str(error).lower()
        for marker, reason in (
            ('certificate_verify_failed', 'TLS certificate verification failed'),
            ('name or service not known', 'DNS resolution failed'),
            ('temporary failure in name resolution', 'Temporary DNS resolution failure'),
            ('connection refused', 'Guardrail connection refused'),
            ('network is unreachable', 'Guardrail network unreachable'),
        ):
            if marker in message:
                return reason
    return f'Guardrail request failed: {type(error).__name__}'


def execute_case(ident, di, ri, content, binding, row, http, detail):
    sample_id=f'dataset-{di:03d}-sample-{ri:08d}'
    caseid=row.get('caseid') or f'row-{ri+1:08d}'
    with tracing.span('evaluation.request',{'evaluation.sample_id':sample_id,'evaluation.dataset_id':content['dataset_id'],'evaluation.binding_id':binding['binding_id']}) as request_span:
        started=time.perf_counter();stage='target'
        label=binding['label_mapping'][str(row['label'])]
        g={'request_id':sample_id,'row_id':str(ri),'caseid':caseid,'binding_ids':[binding['binding_id']],
           'strategy':row['strategy'],'status':'ERROR','state_type':'error','sent':False,
           'guardrail_latency_ms':0,'scorer_latency_ms':0,'total_latency_ms':0,
           'cost':{'amount':0,'currency':'USD','source':'mock','policy_version':'mock-v1'},'classifications':[]}
        sample={'id':sample_id,'caseid':caseid,'dataset_id':content['dataset_id'],'dataset_version':content['version'],
            'input':{'input':row['input']},'reference':{'label':row['label'],'strategy':row['strategy']},
            'output':{},'status':'ERROR','scores':[],'trace':[],'costUsd':0,'latencyMs':0,'error':None}
        event('guardrail.sample.started',run_id=ident,sample_id=sample_id)
        server_host = None
        target_response={'body':None,'http_code':None,'client_timeout':False,'error':None}
        try:
            before=time.perf_counter();g['sent']=True;g['cost']['amount']=.001
            try:
                with tracing.span('guardrail.scan',{'http.request.method':'POST','evaluation.sample_id':sample_id,'evaluation.cost.amount':.001,'evaluation.cost.currency':'USD','evaluation.cost.source':'mock'},client=True) as http_span:
                    endpoint=urlsplit(detail['endpoint'])
                    server_host = endpoint.hostname
                    http_span.set_attribute('server.address',endpoint.hostname or '')
                    http_span.set_attribute('url.path',endpoint.path)
                    body={'input':row['input'],'verbose':True,'flagOnly':False}
                    tracing.payload(http_span,'input',body)
                    try:
                        response=http.post(detail['endpoint'],headers={'Authorization':'Bearer '+detail['token'],**correlation_headers()},json=body)
                        target_response['http_code']=response.status_code
                        g['http_status']=response.status_code
                        http_span.set_attribute('http.response.status_code',response.status_code)
                        try: target_response['body']=response.json()
                        except ValueError: target_response['body']=response.text
                        tracing.payload(http_span,'output',target_response['body'])
                        if response.status_code>=400:
                            from opentelemetry.trace import Status,StatusCode
                            http_span.set_status(Status(StatusCode.ERROR,'HTTP '+str(response.status_code)))
                    except Exception as error:
                        target_response['client_timeout']=isinstance(error,httpx.TimeoutException)
                        target_response['error']=request_error_reason(error,has_token=bool(detail.get('token')))
            finally:g['guardrail_latency_ms']=(time.perf_counter()-before)*1000
            target_response['elapsed_ms']=g['guardrail_latency_ms']
            body=target_response['body']
            if isinstance(body,dict):sample['output']=body
            elif body is not None:sample['output']={'body':body}
            if isinstance(body,dict) and isinstance(body.get('result'),dict) and isinstance(body['result'].get('outcome'),str):
                g['guardrail_outcome']=body['result']['outcome']
            stage='scorer';before=time.perf_counter()
            try:
                from evaluation_backend.git_scorers import execute
                try:
                    result=execute(binding['scorer_execution'],{'input':row['input'],'label':label,
                        'strategy':row['strategy'],'caseid':caseid,'output':sample['output'],
                        'target_response':target_response})
                except Exception:
                    # Published evaluator versions are immutable. Older scripts do not
                    # understand failed-response envelopes; preserve the target state.
                    code=target_response['http_code']
                    if not (target_response['error'] or code is None or code >= 400):
                        raise
                    state=('timeout' if target_response['client_timeout'] or code in (408,499)
                           else 'rate-limit' if code == 429 else 'error')
                    target_reason=target_response['error'] or f'Guardrail returned HTTP {code}'
                    result={'state_type':state,'reason':target_reason + '; evaluator execution failed'}
                state=result.get('state_type','ok')
                if state not in ('ok','rate-limit','timeout','error'):
                    raise ValueError('Invalid evaluator state_type')
                if state=='ok' and (target_response['error'] or target_response['http_code'] is None or target_response['http_code'] >= 400):
                    raise ValueError('Evaluator marked a failed target request as ok')
                g['state_type']=state
                response_body=target_response['body']
                raw_outcome=response_body.get('result',{}).get('outcome') if isinstance(response_body,dict) and isinstance(response_body.get('result'),dict) else None
                guardrail_outcome=result.get('guardrail_outcome',raw_outcome)
                if guardrail_outcome is not None and not isinstance(guardrail_outcome,str):
                    raise ValueError('Evaluator guardrail_outcome must be text')
                reason=result.get('reason')
                if reason is not None and (not isinstance(reason,str) or len(reason)>8000):
                    raise ValueError('Evaluator reason must be text of at most 8000 characters')
                g['guardrail_outcome']=guardrail_outcome
                g['reason']=reason
                if state=='ok':
                    classification=ClassificationResult(binding_id=binding['binding_id'],source='mock',
                        **{key:result[key] for key in ('expected','predicted','outcome','reason')})
                    if classification.expected != label:
                        raise ValueError('Evaluator expected label differs from dataset label')
                    g['classifications']=[classification.model_dump()];g['status']='COMPLETED'
                    sample['status']='PASS' if classification.outcome in ('TP','TN') else 'FAIL'
                else:
                    g['error_stage']='target';g['error_code']=state
                    g['reason']=g['reason'] or f'Guardrail request {state}'
                    sample['error']=g['reason']
                    from opentelemetry.trace import Status,StatusCode
                    request_span.set_status(Status(StatusCode.ERROR,state))
                    event('guardrail.sample.failed',level='ERROR',run_id=ident,sample_id=sample_id,
                        phase='target',method='POST',server_host=server_host,
                        verify_ssl=detail.get('verify_ssl',True),status_code=g.get('http_status'),
                        duration_ms=round(g['guardrail_latency_ms'],2),error_reason=g['reason'])
            finally:g['scorer_latency_ms']=(time.perf_counter()-before)*1000
        except Exception as error:
            reason=request_error_reason(error,has_token=bool(detail.get('token'))) if stage=='target' else f'Evaluator execution failed: {type(error).__name__}'
            g.update(error_stage=stage,error_code=type(error).__name__,state_type='error',reason=reason,classifications=[])
            sample['error']=reason
            from opentelemetry.trace import Status,StatusCode
            request_span.set_status(Status(StatusCode.ERROR,type(error).__name__))
            request_fields = {}
            if stage == 'target':
                request_fields = {
                    'method': 'POST', 'server_host': server_host,
                    'verify_ssl': detail.get('verify_ssl', True),
                    'status_code': g.get('http_status'), 'duration_ms': round(g['guardrail_latency_ms'], 2),
                    'error_reason': request_error_reason(error, has_token=bool(detail.get('token'))),
                }
            event('guardrail.sample.failed',level='ERROR',run_id=ident,sample_id=sample_id,error=error,phase=stage,**request_fields)
        g['total_latency_ms']=(time.perf_counter()-started)*1000
        sample.update(guardrail_result=GuardrailRequestResult.model_validate(g).model_dump(),
            latencyMs=g['total_latency_ms'],costUsd=g['cost']['amount'])
    return di,sample


def run(client=None):
    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
    import threading
    from evaluation_backend.git_scorers import prepare
    concurrency=int(os.getenv('EVAL_CONCURRENCY','1'))
    if concurrency < 1:
        raise ValueError('EVAL_CONCURRENCY must be a positive integer')
    tracing.configure()
    client=client or StorageClient()
    ident=os.environ['EXPERIMENT_ID']
    payload=client.run_input(ident)
    detail=client.call('GET','guardrail-detail/'+ident)
    contents,bindings=payload['datasets'],payload['guardrail_bindings']
    by_dataset={b['dataset_id']:b for b in bindings}
    # Checkout before concurrent execution; identical pinned artifacts are cached.
    for binding in bindings:prepare(binding['scorer_execution'])
    summary={'dataset_id':contents[0]['dataset_id'],'dataset_version':contents[0]['version'],
        'items':0,'errors':0,'status':'RUNNING','dataset_results':[
            {'dataset_id':c['dataset_id'],'dataset_version':c['version'],'name':c['name'],
             'sample_count':len(c['rows']),'items':0,'errors':0,'status':'QUEUED'} for c in contents]}
    client.experiment(ident,summary)
    local=threading.local();clients=[];lock=threading.Lock()
    def task(di,ri,content,binding,row):
        if not hasattr(local,'http'):
            local.http=httpx.Client(timeout=30,trust_env=False,verify=detail.get('verify_ssl', True))
            with lock:clients.append(local.http)
        di,sample=execute_case(ident,di,ri,content,binding,row,local.http,detail)
        try:
            client.sample(ident,sample)
        except Exception as error:
            event('guardrail.sample.persist_failed',level='ERROR',run_id=ident,sample_id=sample['id'],error=error)
            raise
        return di,sample
    tasks=iter((di,ri,c,by_dataset[c['dataset_id']],row) for di,c in enumerate(contents) for ri,row in enumerate(c['rows']))
    event('guardrail.pool.started',run_id=ident,count=concurrency)
    try:
        with ThreadPoolExecutor(max_workers=concurrency,thread_name_prefix='guardrail-case') as pool:
            pending=set()
            def submit_next():
                try:args=next(tasks)
                except StopIteration:return False
                summary['dataset_results'][args[0]]['status']='RUNNING'
                pending.add(pool.submit(task,*args));return True
            # Each worker persists its own sample before completing the task.
            for _ in range(concurrency*2):
                if not submit_next():break
            while pending:
                completed,_=wait(pending,return_when=FIRST_COMPLETED)
                for future in completed:
                    pending.remove(future)
                    di,sample=future.result()
                    submit_next()
                    # Count only persisted samples; summary mutations stay on this thread.
                    progress=summary['dataset_results'][di]
                    progress['items']+=1;summary['items']+=1
                    if sample['status']=='ERROR':progress['errors']+=1;summary['errors']+=1
                    if progress['items']==progress['sample_count']:
                        progress['status']='COMPLETED'
                    # Samples persist individually; coalesce progress writes to avoid
                    # waiting for an extra ES refresh for every completed case.
                    if summary['items']%concurrency==0:
                        client.experiment(ident,summary)
                    event('guardrail.sample.completed',run_id=ident,sample_id=sample['id'],status=sample['status'])
        # A case error is a persisted outcome. Runner or storage failures still raise.
        summary['status']='COMPLETED'
        client.experiment(ident,summary)
        event('guardrail.pool.completed',run_id=ident,count=summary['items'],status=summary['status'])
        return summary
    finally:
        for http in clients:http.close()


if __name__=='__main__':
    configure_logging('guardrail-worker')
    try:
        result=run();print(json.dumps({**result,'evaluation_id':os.environ['EXPERIMENT_ID']}))
        code=0
    except Exception as error:
        event('guardrail.worker.failed',level='ERROR',error=error);code=1
    finally:
        tracing.shutdown()
        shutdown_logging()
    raise SystemExit(code)
