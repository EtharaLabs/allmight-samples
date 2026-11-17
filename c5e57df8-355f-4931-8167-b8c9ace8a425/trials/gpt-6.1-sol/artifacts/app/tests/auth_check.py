import sys,os,json,re
sys.path.insert(0,os.path.dirname(os.path.dirname(__file__)));import app
D=app.DB();D.q('BEGIN')
try:
 s=json.loads(D.q('select data from pp_state where id=1 for update')[0]['data']);c=app.Context(D,s);u=next(u for u in c.users.values() if u['active'] and u['role']=='buyer')
 def handler(path,data=None):
  x=app.Handler.__new__(app.Handler);x.c=c;x.u=None;x.pathonly=path;x.d=data or {};x.post=data is not None;x.csrf='csrf';x.sid='anonymous';x.query={};x.result=None;s['sessions']['anonymous']={'uid':None,'csrf':'csrf'};return x
 x=handler('/password-reset',{'email':'  '+u['email'].upper()+'  '});x.auth();assert len(c.after_mail)==1;token=c.after_mail[-1]['body'].split('/')[-1];assert token not in json.dumps(s)
 x=handler('/password-reset',{'email':u['email']});x.auth();newtoken=c.after_mail[-1]['body'].split('/')[-1]
 try:handler('/password-reset/'+token).auth();raise AssertionError('replaced token accepted')
 except app.Fail as e:assert e.code==410
 try:handler('/password-reset/unknown').auth();raise AssertionError('unknown token accepted')
 except app.Fail as e:assert e.code==404
 x=handler('/password-reset/'+newtoken,{'password':'verification-password-123'});x.auth();assert newtoken not in json.dumps(s);assert 'verification-password-123' not in json.dumps(s)
 try:handler('/password-reset/'+newtoken,{'password':'other-password-123'}).auth();raise AssertionError('used token accepted')
 except app.Fail as e:assert e.code==410
 for nxt,expected in [('https://example.test','/'),('//example.test','/'),('/\\example.test','/'),('/orders','/orders')]:
  x=handler('/signin',{'email':' '+u['email'].upper()+' ','password':'verification-password-123'});x.query={'next':[nxt]};x.auth();assert x.result[3]==expected;assert 'anonymous' not in s['sessions'];assert x.cookie and 'HttpOnly' in x.cookie and 'SameSite=Lax' in x.cookie
 x=handler('/account/api-keys',{'label':'Test key'});x.u=u;x.dispatch();body=x.result[0];key=re.search(r'data-api-key="([^"]+)"',body)[1];assert key not in json.dumps(s);assert any(app.matches(key,k['hash']) for k in s['keys'].values())
 print('PASS: reset normalization, token hashing, single-use/replaced links, password hashing, session rotation, safe redirects, one-time API keys')
finally:D.q('ROLLBACK');D.close()
