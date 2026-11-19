import unittest,threading,urllib.request,urllib.error,urllib.parse,secrets,smtplib
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from app import Handler,digest
from db import DB
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args):return None
class AuthFlow(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);cls.base='http://127.0.0.1:'+str(cls.server.server_port);threading.Thread(target=cls.server.serve_forever,daemon=True).start();cls.sent=[]
 @classmethod
 def tearDownClass(cls):cls.server.shutdown();cls.server.server_close()
 def request(self,path,data=None,cookie=None):
  headers={'Cookie':cookie} if cookie else {};req=urllib.request.Request(self.base+path,None if data is None else urllib.parse.urlencode(data).encode(),headers=headers)
  try:r=urllib.request.build_opener(NoRedirect()).open(req)
  except urllib.error.HTTPError as ex:r=ex
  return r.status,r.read().decode(),r.headers
 def test_reset_signin_sessions(self):
  with DB() as d:
   u=d.one('SELECT * FROM users WHERE id=1');original=d.one('SELECT * FROM lb_auth WHERE user_id=1');oldresets=d.q('SELECT * FROM lb_resets WHERE user_id=1');oldsession={r['hash'] for r in d.q('SELECT hash FROM lb_sessions WHERE user_id=1')};tokens=[]
   def send(s,msg):self.sent.append(msg)
   class SMTP:
    def __init__(s,*a,**k):pass
    def __enter__(s):return s
    def __exit__(s,*a):pass
    send_message=send
   try:
    with patch('app.smtplib.SMTP',SMTP):
     a=self.request('/password-reset',{'email':'unknown@lotbook.example'});self.assertEqual(len(self.sent),0)
     b=self.request('/password-reset',{'email':' \t'+u['email'].strip().upper()+'\r\n '});self.assertEqual(a[0],b[0]);self.assertIn('If that address belongs',b[1]);self.assertEqual(len(self.sent),1)
     import os,re
     link=re.search(re.escape(os.environ['PUBLIC_BASE_URL'].rstrip('/'))+r'(/password-reset/\S+)',self.sent[-1].get_content())[1];tokens.append(link.rsplit('/',1)[1])
     self.assertEqual(self.request(link,{'password':'short'})[0],422)
     self.request('/password-reset',{'email':u['email']});link2=re.search(r'(/password-reset/\S+)',self.sent[-1].get_content())[1];tokens.append(link2.rsplit('/',1)[1]);self.assertIn('no longer valid',self.request(link)[1])
     pw=secrets.token_urlsafe(20);self.assertEqual(self.request(link2,{'password':pw})[0],303);self.assertIn('no longer valid',self.request(link2)[1]);self.assertEqual(self.request('/password-reset/unknown')[0],404)
     for nxt,want in [('/accounts/1?kind=sell','/accounts/1?kind=sell'),('//evil.example','/'),('/\\evil.example','/'),('/\tevil.example','/'),('https:evil.example','/')]:
      status,_,headers=self.request('/signin',{'email':u['email'],'password':pw,'next':nxt});self.assertEqual(status,303);self.assertEqual(headers['Location'],want);cookie=headers['Set-Cookie'].split(';')[0];self.assertIn('HttpOnly',headers['Set-Cookie']);self.assertIn('SameSite=Lax',headers['Set-Cookie'])
     status,body,_=self.request('/',cookie=cookie);self.assertEqual(status,200);csrf=re.search('name="csrf" data-csrf value="([^"]+)',body)[1]
     self.assertEqual(self.request('/signout',{},cookie)[0],403);self.assertEqual(self.request('/signout',{'csrf':csrf},cookie)[0],303);self.assertEqual(self.request('/',cookie=cookie)[0],303)
     status,_,headers=self.request('/signin',{'email':u['email'],'password':pw});cookie=headers['Set-Cookie'].split(';')[0]
     self.request('/password-reset',{'email':u['email']});link3=re.search(r'(/password-reset/\S+)',self.sent[-1].get_content())[1];tokens.append(link3.rsplit('/',1)[1]);self.request(link3,{'password':secrets.token_urlsafe(20)});self.assertEqual(self.request('/',cookie=cookie)[0],303)
   finally:
    if original:d.q('UPDATE lb_auth SET password_hash=$1 WHERE user_id=1',original['password_hash'])
    else:d.q('DELETE FROM lb_auth WHERE user_id=1')
    for token in tokens:d.q('DELETE FROM lb_resets WHERE hash=$1',digest(token))
    for r in oldresets:d.q('UPDATE lb_resets SET used=$1 WHERE hash=$2',r['used'],r['hash'])
    for r in d.q('SELECT hash FROM lb_sessions WHERE user_id=1'):
     if r['hash'] not in oldsession:d.q('DELETE FROM lb_sessions WHERE hash=$1',r['hash'])
if __name__=='__main__':unittest.main()
