from evaluation_backend.storage.service import EvaluationStorageService
from memory_storage import Memory


def test_archive_is_project_scoped_reversible_and_paginates_active_items():
    service=EvaluationStorageService(Memory())
    for project in ('p','other'):
        for i in range(3):service.storage.put('datasets',project,str(i),{'id':str(i),'versions':[{'version':1}]})
    service.archive('p','datasets','0')
    assert service.page('datasets','p',offset=0,limit=1)['items'][0]['id']=='1'
    assert service.page('datasets','p')['total']==2
    assert service.page('datasets','other')['total']==3
    assert service.storage.get('datasets','p','0')['versions']==[{'version':1}]
    service.archive('p','datasets','0',False)
    assert service.page('datasets','p')['total']==3
