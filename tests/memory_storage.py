import copy
from evaluation_backend.storage import NotFound, Conflict

class Memory:
    def __init__(self):self.data={}
    def get(self,c,p,i):
        if (c,p,i) not in self.data:raise NotFound('Missing')
        return copy.deepcopy(self.data[c,p,i])
    def put(self,c,p,i,v,create=False,revision=None):
        if create and (c,p,i) in self.data:raise Conflict('Exists')
        current=self.data.get((c,p,i))
        if revision is not None and (not current or current['_revision'] != revision):raise Conflict('Stale revision')
        seq=int(current['_revision'].split(':')[0])+1 if current else 1
        self.data[c,p,i]={**copy.deepcopy(v),'_revision':f'{seq}:1'}
        return self.get(c,p,i)
    def search(self,c,p,filters=None,**kw):
        rows=[v for (cc,pp,_),v in self.data.items() if (cc,pp)==(c,p) and all(v.get(k)==x for k,x in (filters or {}).items())]
        return {'items':copy.deepcopy(rows),'total':len(rows)}

