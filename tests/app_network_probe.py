"""Executed INSIDE the actual built app image; no external network call permitted."""
import importlib.util
import os
import socket
import sys
import sitecustomize as policy
from openai import OpenAI
from local_entrypoint import validate
import importlib

allowed=dict(os.environ)
for key in ('TEXT_MODEL_BASE_URL','CHAT_MODEL_BASE_URL'):
    for value in ('','https://openrouter.ai/api/v1','http://example.com/v1'):
        invalid=dict(allowed,**{key:value})
        try: validate(invalid)
        except ValueError: pass
        else: raise AssertionError('Invalid endpoint accepted')

calls={'external_resolve':0,'external_connect':0}
old_resolve=policy._resolve
old_connect=policy._connect
def trace_resolve(host,*args,**kwargs):
    if host not in policy._hosts and host not in policy._ips:
        calls['external_resolve']+=1
        raise AssertionError('External DNS attempted')
    return old_resolve(host,*args,**kwargs)
def trace_connect(sock,address):
    if address[0] not in policy._hosts and address[0] not in policy._ips:
        calls['external_connect']+=1
        raise AssertionError('External connection attempted')
    return old_connect(sock,address)
policy._resolve=trace_resolve
policy._connect=trace_connect
for endpoint in ('http://example.com/v1','http://203.0.113.1/v1','http://127.0.0.1:1/v1'):
    client=OpenAI(api_key='synthetic-only',base_url=endpoint,timeout=1,max_retries=0)
    try: client.chat.completions.create(model='synthetic',messages=[{'role':'user','content':'synthetic transcript'}])
    except Exception: pass
    else: raise AssertionError('Unavailable endpoint returned success')
assert calls=={'external_resolve':0,'external_connect':0},calls
# Drive Speakr's own summary/chat request functions with its real config loader.
# This revision stores endpoint configuration in env, not per-user settings.
from src.services import llm
checked=0
for endpoint in (None,'','http://example.com/v1','http://203.0.113.1/v1','http://127.0.0.1:1/v1'):
    for key in ('TEXT_MODEL_BASE_URL','CHAT_MODEL_BASE_URL'):
        if endpoint is None: os.environ.pop(key,None)
        else: os.environ[key]=endpoint
    if endpoint in (None,''):
        os.environ.pop('TEXT_MODEL_API_KEY',None)
        os.environ.pop('CHAT_MODEL_API_KEY',None)
    else:
        os.environ['TEXT_MODEL_API_KEY']='synthetic'
        os.environ['CHAT_MODEL_API_KEY']='synthetic'
    os.environ['LLM_REQUEST_TIMEOUT']='1'
    os.environ['LLM_CONNECT_TIMEOUT']='1'
    os.environ['LLM_MAX_RETRIES']='0'
    llm=importlib.reload(llm)
    for function in (llm.call_llm_completion,llm.call_chat_completion):
        try: function([{'role':'user','content':'synthetic transcript'}],max_tokens=10)
        except Exception: checked+=1
        else: raise AssertionError('Actual Speakr request unexpectedly succeeded')
assert checked==10,checked
assert calls=={'external_resolve':0,'external_connect':0},calls
print('actual Speakr summary/chat negative requests=10; external DNS/connect calls=0')
