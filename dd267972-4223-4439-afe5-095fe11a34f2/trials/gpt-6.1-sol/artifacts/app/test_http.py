"""Integration checks against the configured database; cleans up its own fixture rows."""
import os,time,app, threading, urllib.request, urllib.parse, urllib.error, http.cookiejar, re, json, secrets
from concurrent.futures import ThreadPoolExecutor
from db import DB
from datetime import date
sent=[]
app.sendmail=lambda *args:sent.append(args)
server=app.ThreadingHTTPServer(('127.0.0.1',0),app.App)
threading.Thread(target=server.serve_forever,daemon=True).start()
base='http://127.0.0.1:'+str(server.server_port)
class Client:
 def __init__(self):
  self.jar=http.cookiejar.CookieJar();self.o=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
 def req(self,path,data=None,headers=None,raw=None):
  b=raw if raw is not None else urllib.parse.urlencode(data).encode() if data is not None else None
  try:r=self.o.open(urllib.request.Request(base+path,b,headers or {}))
  except urllib.error.HTTPError as e:r=e
  return r.code,r.read().decode(),r.headers,r.url
 def csrf(self,path):
  r=self.req(path);assert r[0]==200,r;return re.search(r'name="csrf" data-csrf value="([^"]+)"',r[1])[1]
 def post(self,path,data,page=None):return self.req(path,dict(data,csrf=self.csrf(page or path)))
ids=[];device=None;runs=[];seed=secrets.token_urlsafe(32);prefix='integration-'+secrets.token_hex(6);admin=Client();user=Client()
try:
 with DB() as d:d.q('INSERT INTO sb_sessions(hash,employee_id,csrf) VALUES($1,1,$2)',app.digest(seed),prefix)
 admin.jar.set_cookie(http.cookiejar.Cookie(0,'sb_session',seed,None,False,'127.0.0.1',False,False,'/',True,False,None,True,None,None,{}))
 r=admin.post('/admin/employees/new',{'email':prefix+'@example.test','full_name':'=Test, "Cook"','location_id':1,'manager_id':1,'role':'employee','job_code':'COOK','rate_cents':2000,'effective_from':'2026-09-20'})
 assert r[0]==200,r;eid=int(re.search(r'data-employee-id="(\d+)"',r[1])[1]);ids.append(eid)
 # Reset response is identical for unknown and known, and the captured email is addressed by name.
 r1=user.post('/password-reset',{'email':'  '+prefix.upper()+'@EXAMPLE.TEST '});r2=user.post('/password-reset',{'email':'missing@example.test'})
 assert r1[:2]==r2[:2] and len(sent)==1;assert '=Test' in sent[-1][0]
 token=re.search(r'/password-reset/(\S+)',sent[-1][2])[1]
 assert user.post('/password-reset/'+token,{'password':'test-password-123'})[0]==200
 assert user.req('/password-reset/'+token)[0]==400
 # Session rotation, safe redirects, and CSRF rejection.
 oldcookie=[c.value for c in user.jar if c.name=='sb_session'][0]
 r=user.post('/signin',{'email':prefix+'@example.test','password':'test-password-123','next':'//evil.test'})
 assert r[0]==200 and r[3].endswith('/timesheet'),r
 assert [c.value for c in user.jar if c.name=='sb_session'][0]!=oldcookie
 assert user.req('/profile',{'full_name':'Oops'})[0]==403
 assert user.req('/payroll')[0]==403
 assert user.post('/profile',{'full_name':'<script>alert(1)</script>','role':'payroll_admin'})[0]==200
 with DB() as d:assert d.one('SELECT role FROM employees WHERE id=$1',eid)['role']=='employee'
 # Clock and ordered punches, duplicates, instant collision and key revocation.
 r=admin.post('/admin/devices',{'location_id':1,'label':prefix});device=int(re.search(r'data-device-id="(\d+)"',r[1])[1]);key=re.search(r'data-device-key="([^"]+)"',r[1])[1]
 headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'}
 punches=[{'punch_id':prefix+'-in','employee_id':eid,'kind':'in','at':'2026-09-21T08:00:00-07:00','job_code':'COOK'},{'punch_id':prefix+'-out','employee_id':eid,'kind':'out','at':'2026-09-21T17:00:00-07:00'}]
 def punch(rows):return user.req('/api/punches',headers=headers,raw=json.dumps({'punches':rows}).encode())
 assert [x['status'] for x in json.loads(punch(punches)[1])['results']]==['accepted','accepted']
 assert json.loads(punch(punches)[1])['results'][0]['status']=='duplicate'
 different=dict(punches[0],at='2026-09-21T08:06:00-07:00');assert json.loads(punch([different])[1])['results'][0]['status']=='rejected'
 r=user.req('/timesheet?week=2026-09-20');assert 'data-reg-hours="8.0"' in r[1] and 'data-ot-hours="1.0"' in r[1]
 assert user.req(f'/employees/{eid}/weeks/2026-09-20/approve',{'csrf':user.csrf('/profile')})[0]==403
 r=admin.post(f'/employees/{eid}/weeks/2026-09-20/approve',{},f'/employees/{eid}/timesheet?week=2026-09-20');assert 'data-status="approved"' in r[1],r
 # Draft and stale detection after a rate append.
 r=admin.post('/payroll/runs',{'period_start':'2026-09-20'},'/payroll');assert r[0]==200,r
 rid=int(re.search(r'data-run-id="(\d+)"',r[1])[1]);runs.append(rid);v=re.search(r'data-version="(\d+)"',r[1])[1]
 assert 'data-gross-cents="21000"' in r[1]
 r=admin.post(f'/admin/employees/{eid}/rates',{'job_code':'COOK','rate_cents':2100,'effective_from':'2026-09-21'},f'/admin/employees/{eid}');assert r[0]==200,r
 assert 'data-status="pending"' in user.req('/timesheet?week=2026-09-20')[1]
 r=admin.post(f'/payroll/runs/{rid}/finalize',{'version':v},f'/payroll/runs/{rid}');assert r[0]==409,r
 admin.post(f'/employees/{eid}/weeks/2026-09-20/approve',{},f'/employees/{eid}/timesheet?week=2026-09-20')
 r=admin.post('/payroll/runs',{'period_start':'2026-09-20'},'/payroll');v=re.search(r'data-version="(\d+)"',r[1])[1]
 assert 'data-gross-cents="22050"' in r[1]
 r=admin.post(f'/payroll/runs/{rid}/finalize',{'version':v},f'/payroll/runs/{rid}');assert 'data-run-status="finalized"' in r[1],r
 assert admin.post(f'/payroll/runs/{rid}/finalize',{'version':v},f'/payroll/runs/{rid}')[0]==409
 assert admin.post('/payroll/runs',{'period_start':'2026-09-20'},'/payroll')[0]==409
 csv=admin.req(f'/payroll/runs/{rid}/export.csv');assert '\r\n' in csv[1] and '&lt;' not in csv[1] and '<script>' in csv[1]
 with DB() as d:assert len(d.q('SELECT * FROM sb_outbox WHERE run_id=$1',rid))==1
 # Earlier finalized pay is adjusted only after the changed week is approved again.
 admin.post(f'/admin/employees/{eid}/rates',{'job_code':'COOK','rate_cents':2400,'effective_from':'2026-09-20'},f'/admin/employees/{eid}')
 # This earlier effective date is superseded by 2100 on September 21, so pay is unchanged.
 assert 'data-status="approved"' in user.req('/timesheet?week=2026-09-20')[1]
 admin.post(f'/admin/employees/{eid}/rates',{'job_code':'COOK','rate_cents':2400,'effective_from':'2026-09-21'},f'/admin/employees/{eid}')
 # The duplicate effective date is refused; an earlier job date does not silently overwrite a rate.
 with DB() as d:assert d.one('SELECT rate_cents FROM job_rates WHERE employee_id=$1 AND effective_from=$2',eid,'2026-09-21')['rate_cents']=='2100'
 # Add later-in-the-week work and race the same punch ID.
 row={'punch_id':prefix+'-race','employee_id':eid,'kind':'in','at':'2026-09-22T08:00:00-07:00','job_code':'COOK'}
 with ThreadPoolExecutor(2) as pool:
  results=list(pool.map(lambda _:json.loads(punch([row])[1])['results'][0]['status'],range(2)))
 assert sorted(results)==['accepted','duplicate'],results
 punch([{'punch_id':prefix+'-race-out','employee_id':eid,'kind':'out','at':'2026-09-22T10:00:00-07:00'}])
 assert 'data-status="pending"' in user.req('/timesheet?week=2026-09-20')[1]
 admin.post(f'/employees/{eid}/weeks/2026-09-20/approve',{},f'/employees/{eid}/timesheet?week=2026-09-20')
 r=admin.post('/payroll/runs',{'period_start':'2026-10-04'},'/payroll');rid2=int(re.search(r'data-run-id="(\d+)"',r[1])[1]);runs.append(rid2)
 assert 'data-gross-cents="4200"' in r[1] and 'Earlier week 2026-09-20' in r[1],r
 version=re.search(r'data-version="(\d+)"',r[1])[1];csrf=admin.csrf(f'/payroll/runs/{rid2}')
 cookie=[c.value for c in admin.jar if c.name=='sb_session'][0]
 def finalize(_):return Client().req(f'/payroll/runs/{rid2}/finalize',{'version':version,'csrf':csrf},headers={'Cookie':'sb_session='+cookie})[0]
 with ThreadPoolExecutor(2) as pool:codes=list(pool.map(finalize,range(2)))
 assert sorted(codes)==[200,409],codes
 # Revocation, signout replay, and password reset invalidation.
 admin.post(f'/admin/devices/{device}/revoke',{},'/admin/devices');assert punch([])[0]==401
 cookie=[c.value for c in user.jar if c.name=='sb_session'][0];user.post('/signout',{},'/profile')
 replay=Client();r=replay.req('/profile',headers={'Cookie':'sb_session='+cookie});assert r[3].split('?')[0].endswith('/signin')
 user.post('/password-reset',{'email':prefix+'@example.test'});oldtoken=re.search(r'/password-reset/(\S+)',sent[-1][2])[1]
 user.post('/password-reset',{'email':prefix+'@example.test'});newtoken=re.search(r'/password-reset/(\S+)',sent[-1][2])[1]
 assert user.req('/password-reset/'+oldtoken)[0]==400
 user.post('/signin',{'email':prefix+'@example.test','password':'test-password-123'})
 assert user.req('/profile')[0]==200
 visitor=Client();visitor.post('/password-reset/'+newtoken,{'password':'new-test-password-123'})
 assert user.req('/profile')[3].split('?')[0].endswith('/signin')
 for _ in range(5):visitor.post('/signin',{'email':prefix+'@example.test','password':'wrong-password'})
 assert 'temporarily locked' in visitor.post('/signin',{'email':prefix+'@example.test','password':'new-test-password-123'})[1]
 assert visitor.req('/password-reset/unknown-token')[0]==404
 original_url=os.environ['DATABASE_URL'];os.environ['DATABASE_URL']='postgresql://invalid:invalid@127.0.0.1:1/invalid'
 started=time.monotonic()
 try:assert Client().req('/healthz')[0]==503 and time.monotonic()-started<5
 finally:os.environ['DATABASE_URL']=original_url
 assert Client().req('/healthz')[0]==200
 print('HTTP integration checks passed: reset, login, session rotation, CSRF, roles, profile, clocks, punches, approvals, rates, drafts, finalization, export, signout.')
finally:
 server.shutdown()
 with DB() as d:
  for rid in runs:d.q('DELETE FROM sb_outbox WHERE run_id=$1',rid);d.q('DELETE FROM sb_runs WHERE id=$1',rid)
  for eid in ids:
   for table in ['sb_punches','sb_approvals','sb_sessions','sb_resets','sb_auth','job_rates']:d.q('DELETE FROM '+table+' WHERE employee_id=$1',eid)
   d.q('DELETE FROM employees WHERE id=$1',eid)
  if device:d.q('DELETE FROM sb_devices WHERE id=$1',device)
  d.q('DELETE FROM sb_sessions WHERE hash=$1',app.digest(seed))
