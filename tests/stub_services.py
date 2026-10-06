"""Synthetic ASR/OpenAI-compatible fixture. Never use this as a transcription service."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import time
import os

counts={'asr':0,'llm':0,'proxy':0}
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def reply(self,value):
        raw=json.dumps(value,ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    def do_GET(self):
        if self.path=='/counts': return self.reply(counts)
        if self.path=='/api/tags': return self.reply({'models':[{'name':os.environ.get('STUB_MODEL','synthetic-model'),'digest':'b'*64},{'name':'qwen3.5:9b','digest':os.environ.get('STUB_BASE_DIGEST','a'*64)}]})
        self.reply({'status':'ok','version':'synthetic','models':[]})
    def do_POST(self):
        body=self.rfile.read(int(self.headers.get('Content-Length','0')))
        if self.path.startswith('http'): counts['proxy']+=1
        if self.path=='/api/show': return self.reply({'parameters':os.environ.get('STUB_PARAMETERS','')})
        if self.path.startswith('/asr'):
            counts['asr']+=1
            return self.reply({'text':'Синтетическая проверка публикации расшифровки. Система сохраняет результат в заметке.', 'language':'ru','segments':[{'start':0,'end':1,'speaker':'SPEAKER_00','text':'Синтетическая проверка публикации расшифровки. Система сохраняет результат в заметке.'}], 'speaker_embeddings':{}})
        if self.path.startswith('/v1/chat/completions'):
            counts['llm']+=1
            return self.reply({'id':'synthetic','object':'chat.completion','created':int(time.time()),'model':'synthetic-model','choices':[{'index':0,'message':{'role':'assistant','content':'Синтетическое резюме: проверена публикация локальной расшифровки.'},'finish_reason':'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':10,'total_tokens':20}})
        self.reply({'ok':True})

if __name__=='__main__': ThreadingHTTPServer(('0.0.0.0',9000),Handler).serve_forever()
