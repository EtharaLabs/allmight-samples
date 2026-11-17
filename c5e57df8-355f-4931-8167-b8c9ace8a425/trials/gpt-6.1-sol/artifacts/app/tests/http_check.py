import sys,os,json,time,secrets,urllib.request,urllib.error,urllib.parse,re
sys.path.insert(0,os.path.dirname(os.path.dirname(__file__)));import app
base=os.environ['PUBLIC_BASE_URL'];db=app.DB();db.q('BEGIN');s=json.loads(db.q('select data from pp_state where id=1 for update')[0]['data']);users=db.q('select * from users where active');tokens={}
for role in ['buyer','finance','supervisor','picker']:
 u=next(u for u in users if u['role']==role);token=secrets.token_urlsafe(32);sid=app.digest(token);s['sessions'][sid]={'uid':u['id'],'csrf':'verify-http'};tokens[role]=(token,sid)
db.q('update pp_state set data='+app.sql(json.dumps(s))+'::jsonb where id=1');db.q('COMMIT')
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*a,**kw):return None
op=urllib.request.build_opener(NoRedirect)
def req(path,role=None,data=None):
 headers={'Cookie':'pickpath='+tokens[role][0]} if role else {}
 r=urllib.request.Request(base+path,data=urllib.parse.urlencode(data).encode() if data is not None else None,headers=headers)
 try:
  with op.open(r,timeout=10) as response:return response.status,response.read().decode(),response.headers
 except urllib.error.HTTPError as e:return e.code,e.read().decode(),e.headers
try:
 assert req('/healthz')[0]==200
 assert req('/orders')[0]==303
 for role in tokens:
  for path in ['/','/profile']:
   status,body,h=req(path,role);assert status==200,(role,path,status);assert 'data-action="signout"' in body;assert 'data-csrf' in body
 assert req('/prices','buyer')[0]==403
 assert req('/users/new','supervisor')[0]==403
 assert req('/waves','buyer')[0]==403
 assert req('/profile','buyer',{'full_name':'bad'})[0]==403
 assert req('/api/v1/orders/123')[0]==401
 assert req('/webhooks/payments',data={})[0]==400
 assert req('/account/api-keys/999/revoke','buyer')[0]==405
 for path,role in [('/reports/on-hand.csv','finance'),('/reports/valuation.csv?as_of=2026-11-17T00:00:00Z','finance'),('/orders/export.csv','buyer')]:
  start=time.monotonic();status,body,h=req(path,role);duration=time.monotonic()-start;assert status==200,(path,status);assert '\r\n' in body;print(path,round(duration,3),'seconds',len(body),'bytes')
 assert req('/reports/valuation.csv?as_of=2026-11-17','finance')[0]==400
 print('PASS: HTTP health, auth redirects, role permissions, CSRF rejection, signature rejection, invalid API key, report validation and timing')
finally:
 db.q('BEGIN');s=json.loads(db.q('select data from pp_state where id=1 for update')[0]['data'])
 for token,sid in tokens.values():s['sessions'].pop(sid,None)
 db.q('update pp_state set data='+app.sql(json.dumps(s))+'::jsonb where id=1');db.q('COMMIT');db.close()
