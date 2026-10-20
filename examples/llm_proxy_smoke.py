"""Run through proxy_entrypoint in a container; no provider credential needed here."""
import json
import os
from urllib.request import Request, urlopen

for stream in (False, True):
    payload = {'model': 'must-be-overridden', 'messages': [{'role': 'user', 'content': 'Reply with exactly OK.'}],
               'max_tokens': 128, 'thinking': {'type': 'disabled'}, 'stream': stream}
    request = Request(os.environ['OPENAI_BASE_URL'] + '/chat/completions', data=json.dumps(payload).encode(),
                      headers={'Authorization': 'Bearer ' + os.environ['OPENAI_API_KEY'], 'Content-Type': 'application/json'})
    with urlopen(request, timeout=120) as response:
        raw = response.read().decode()
        if stream:
            chunks = [json.loads(line[5:].strip()) for line in raw.splitlines() if line.startswith('data:') and line[5:].strip() != '[DONE]']
            text = ''.join(c.get('delta', {}).get('content') or '' for chunk in chunks for c in chunk.get('choices', []))
            assert '[DONE]' in raw and text.strip() == 'OK', raw
        else:
            data = json.loads(raw)
            text = data['choices'][0]['message']['content']
            assert text.strip() == 'OK', data
        print(json.dumps({'stream': stream, 'response': text, 'http_status': response.status}), flush=True)
