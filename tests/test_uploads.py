import io,json
import pytest
from evaluation_backend.uploads import parse_upload, UploadStore, UploadError

SCHEMA={'name':'Guardrail','objective':'Test','metadata':{},'scorerIds':[], 'version':1,'publishedAt':'2026-10-24T00:00:00Z',
 'fields':[{'name':'messages','type':'json','role':'input','required':True},{'name':'has_issue','type':'boolean','role':'reference','required':True}], 'rows':[]}
CSV=b'messages,has_issue\n"[{""role"":""user"",""content"":""hello""}]",false\n'


def test_csv_and_xlsx_decode_same_typed_rows():
    import openpyxl
    parsed=parse_upload(CSV,'cases.csv',SCHEMA)
    assert parsed==[{'messages':[{'role':'user','content':'hello'}],'has_issue':False}]
    w=openpyxl.Workbook();w.active.append(['messages','has_issue']);w.active.append([json.dumps(parsed[0]['messages']),False]);b=io.BytesIO();w.save(b)
    assert parse_upload(b.getvalue(),'cases.xlsx',SCHEMA)==parsed


@pytest.mark.parametrize('data',[b'messages,has_issue,extra\na,false,x',b'messages,messages\na,a',b'messages,has_issue\n[],false',b'messages,has_issue\nCANARY,maybe',b'messages\na',b'messages,has_issue\n'])
def test_invalid_schema_never_echoes_sensitive_cells(data):
    with pytest.raises(UploadError) as error: parse_upload(data,'cases.csv',SCHEMA)
    assert 'CANARY' not in str(error.value)


def test_formula_and_nested_message_shape_rejected():
    import openpyxl
    w=openpyxl.Workbook();w.active.append(['messages','has_issue']);w.active.append(['=A3',False]);b=io.BytesIO();w.save(b)
    with pytest.raises(UploadError):parse_upload(b.getvalue(),'cases.xlsx',SCHEMA)
    with pytest.raises(UploadError):parse_upload(b'messages,has_issue\n"[{""role"":""user""}]",false','cases.csv',SCHEMA)


def test_upload_is_project_and_schema_scoped_single_run_and_deletable(tmp_path):
    store=UploadStore(tmp_path)
    u=store.create('p','d',1,SCHEMA,parse_upload(CSV,'cases.csv',SCHEMA))
    with pytest.raises(UploadError):store.claim('other',u['id'],'d',1,'eval-1','exp-1')
    with pytest.raises(UploadError):store.claim('p',u['id'],'d',2,'eval-1','exp-1')
    store.claim('p',u['id'],'d',1,'eval-1','exp-1')
    store.claim('p',u['id'],'d',1,'eval-1','exp-1')
    with pytest.raises(UploadError):store.claim('p',u['id'],'d',1,'eval-2','exp-2')
    assert store.read('p',u['id'],'exp-1')['rows'][0]['has_issue'] is False
    store.delete('p',u['id'])
    with pytest.raises(UploadError):store.read('p',u['id'],'exp-1')
    assert not list(tmp_path.glob('*.json'))


def test_expired_unbound_upload_is_deleted(tmp_path):
    store=UploadStore(tmp_path,ttl=0)
    u=store.create('p','d',1,SCHEMA,parse_upload(CSV,'cases.csv',SCHEMA))
    store.reconcile(lambda _:False)
    with pytest.raises(UploadError):store.claim('p',u['id'],'d',1,'eval-1','exp-1')
    assert not list(tmp_path.glob('*.json'))
