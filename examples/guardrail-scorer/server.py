"""Local fixture artifact server; deploy without credentials or writable mounts."""
import os
from fastapi import FastAPI, HTTPException
from scorer import score

app=FastAPI(title='Mock Git scorer artifact')

@app.get('/health')
def health():
    return {'commit':os.environ['SCORER_COMMIT']}

@app.post('/score')
def evaluate(value:dict):
    import json
    if len(json.dumps(value).encode())>1000000:raise HTTPException(413,'Evidence too large')
    try:return {'commit':os.environ['SCORER_COMMIT'],'result':score(value)}
    except (ValueError,KeyError,TypeError):raise HTTPException(422,'Invalid scan evidence') from None
