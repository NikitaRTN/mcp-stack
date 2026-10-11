import unittest,tempfile,pathlib,sys,types,threading,http.client,json,time,socket,sqlite3,importlib,struct
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
BASE=pathlib.Path(__file__).resolve().parents[1];ROOT=BASE if (BASE/'hub/inspector.py').exists() else BASE/('staged' if (BASE/'staged').exists() else 'build');TMP=tempfile.TemporaryDirectory();DATA=pathlib.Path(TMP.name)
pkg=types.ModuleType('paneltest');pkg.__path__=[str(ROOT/'hub')];sys.modules['paneltest']=pkg
cfg=types.ModuleType('paneltest.config');cfg.DATA=DATA;cfg.load=lambda:{}
services={}
cfg.service=lambda sid,c=None:services.get(sid)
cfg.upstream_of=lambda s:s['upstream'];cfg.split_upstream=lambda u:('127.0.0.1',int(u.split(':')[1]),'/mcp','http')
sys.modules['paneltest.config']=cfg
au=types.ModuleType('paneltest.oauth');au.OAuthError=type('OAuthError',(Exception,),{});sys.modules['paneltest.oauth']=au
# Start from the real pre-tracing schema, not an empty database.
legacy_db=sqlite3.connect(str(DATA/'telemetry.db'))
legacy_db.execute('CREATE TABLE calls (id INTEGER PRIMARY KEY AUTOINCREMENT, service TEXT NOT NULL, method TEXT NOT NULL, tool TEXT, rpc_id TEXT, session TEXT, started REAL NOT NULL, finished REAL, duration_ms REAL, status TEXT NOT NULL DEFAULT "running", error TEXT, args TEXT, req_bytes INTEGER DEFAULT 0, resp_bytes INTEGER DEFAULT 0, client TEXT)'.replace('DEFAULT running',"DEFAULT 'running'"))
legacy_db.execute("INSERT INTO calls(service,method,started,status) VALUES ('archive','archived',1,'ok')")
legacy_db.commit();legacy_db.close()
telemetry=importlib.import_module('paneltest.telemetry');inspector=importlib.import_module('paneltest.inspector');rt=importlib.import_module('paneltest.request_trace')
fixture=[]
class Upstream(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a):pass
 def do_POST(self):
  body=self.rfile.read(int(self.headers.get('Content-Length',0)));q=json.loads(body);method=q.get('method');rid=q.get('id')
  if rid is None:
   time.sleep(.015);self.send_response(202);self.send_header('Content-Length','0');self.end_headers();return
  status=500 if method=='fail' else 200
  answer={'jsonrpc':'2.0','id':rid,'error':{'code':-32000,'message':'Controlled upstream error'}} if status==500 else {'jsonrpc':'2.0','id':rid,'result':{'content':[{'type':'text','text':'Тест: данные получены'}]}}
  payload=json.dumps(answer,ensure_ascii=False).encode()
  if method in ['sse','slow']:
   self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Transfer-Encoding','chunked');self.end_headers()
   packet=b'data: '+payload+b'\n\n';split=[packet[:13],packet[13:31],packet[31:]]
   try:
    for chunk in split:
     time.sleep(.04 if method=='slow' else .008);self.wfile.write(b'%x\r\n'%len(chunk)+chunk+b'\r\n');self.wfile.flush()
    self.wfile.write(b'0\r\n\r\n');self.wfile.flush()
   except OSError:pass
  else:
   time.sleep(.005);self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
 def do_DELETE(self):self.send_response(200);self.send_header('Content-Length','0');self.end_headers()
up=ThreadingHTTPServer(('127.0.0.1',0),Upstream);up.daemon_threads=True;threading.Thread(target=up.serve_forever,daemon=True).start()
services['test']={'id':'test','enabled':True,'kind':'stdio','upstream':'127.0.0.1:'+str(up.server_port)}
server,thread=inspector.serve(0)
def req(method,rid=1):
 c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3);body=json.dumps({'jsonrpc':'2.0','method':method,**({'id':rid} if rid is not None else {}),'params':{'name':'read_file','arguments':{'path':'fixture.txt'}}})
 c.request('POST','/_inspect/test',body,{'Content-Type':'application/json'});r=c.getresponse();raw=r.read();status=r.status;c.close();return status,raw

def finished(method):
 end=time.monotonic()+2
 while time.monotonic()<end:
  rows=telemetry.list_calls(include_trace=True)
  row=next((x for x in rows if x['method']==method and x['status']!='running'),None)
  if row:return row
  time.sleep(.005)
 raise AssertionError('No finished row: '+method)
class Tests(unittest.TestCase):
 def test_01_json(self):
  self.assertEqual(req('json')[0],200);row=finished('json');t=row['trace'];self.assertTrue(t['complete']);self.assertEqual(t['scope'],'inspector');self.assertEqual(row['status'],'ok');self.assertEqual(len(t['stages']),8);self.assertTrue(all(s['duration_ms']>=0 for s in t['stages']));self.assertAlmostEqual(sum(s['duration_ms'] for s in t['stages']),t['total_ms'],places=2);fixture.append(row)
 def test_02_sse(self):
  status,body=req('sse',2);self.assertEqual(status,200);self.assertIn('Тест: данные получены',body.decode());row=finished('sse');self.assertEqual(row['status'],'ok');self.assertEqual(row['trace']['transport'],'SSE');self.assertGreater(len(row['trace']['events']),8);self.assertAlmostEqual(sum(s['duration_ms'] for s in row['trace']['stages']),row['trace']['total_ms'],places=2);fixture.append(row)
 def test_03_notification(self):
  self.assertEqual(req('notifications/initialized',None)[0],202);row=finished('notifications/initialized');self.assertGreater(row['duration_ms'],10);self.assertEqual(row['status'],'ok');fixture.append(row)
 def test_04_error(self):
  self.assertEqual(req('fail',4)[0],500);row=finished('fail');self.assertEqual(row['status'],'error');self.assertEqual(row['trace']['failed_stage'],'receive');fixture.append(row)
 def test_05_live(self):
  worker=threading.Thread(target=lambda:req('slow',5));worker.start();deadline=time.monotonic()+1;live=None
  while time.monotonic()<deadline:
   live=next((r for r in telemetry.list_calls(include_trace=True) if r['method']=='slow' and r['status']=='running'),None)
   if live and live['trace']['phase']=='receive':break
   time.sleep(.001)
  self.assertIsNotNone(live);self.assertFalse(live['trace']['complete']);self.assertIsNotNone(live['trace']['phase']);worker.join();fixture.append(finished('slow'))
 def test_06_bounded_events(self):
  t=rt.RequestTrace()
  for _ in range(200):t.enter('receive');t.enter('forward')
  snap=t.snapshot(complete=True);self.assertLessEqual(len(snap['events']),128);self.assertGreater(snap['events_omitted'],0);self.assertAlmostEqual(sum(s['duration_ms'] or 0 for s in snap['stages']),snap['total_ms'],places=2)
 def test_07_old_row_and_detail(self):
  row_id=telemetry.begin('old','tools/call','legacy');telemetry.finish(row_id,'ok');self.assertIsNone(telemetry.get_call(row_id)['trace']);self.assertIsNone(telemetry.get_call(99999999));self.assertNotIn('stages',telemetry.list_calls()[0].get('trace') or {})
 def test_08_history_preserved(self):
  columns={r[1] for r in telemetry.connect().execute('PRAGMA table_info(calls)')};self.assertIn('trace',columns);self.assertGreaterEqual(len(telemetry.list_calls()),6);self.assertEqual(telemetry.get_call(1)['method'],'archived');self.assertIsNone(telemetry.get_call(1)['trace'])
 def test_09_malformed_json_passthrough(self):
  scanner=inspector.SseScanner();self.assertEqual(scanner.feed(b'data: not-json\n\n'),[]);self.assertEqual(scanner.feed(b'data: {"id":2}\n\n'),[{'id':2}])
 def test_10_trace_does_not_include_payload(self):
  row=finished('json');t=json.dumps(row['trace']);self.assertNotIn('fixture.txt',t);self.assertNotIn('Authorization',t);self.assertNotIn('arguments',t)
 def test_11_actual_tools_call(self):
  status,_=req('tools/call',11);self.assertEqual(status,200);row=finished('tools/call');self.assertEqual(row['tool'],'read_file');self.assertEqual(row['status'],'ok');fixture.append(row)

_CLOSED=False
def tearDownModule():
 global _CLOSED
 if _CLOSED:return
 _CLOSED=True
 server.shutdown();up.shutdown();server.server_close();up.server_close();telemetry.connect().close();TMP.cleanup()

if __name__=='__main__':
 result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Tests))
 tearDownModule();sys.exit(0 if result.wasSuccessful() else 1)
