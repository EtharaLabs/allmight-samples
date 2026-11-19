import os,threading,secrets,unittest,json,urllib.request,urllib.error,urllib.parse,concurrent.futures
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from db import DB
from app import Handler,digest
from engine import Engine
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*a):return None
class LockFlow(unittest.TestCase):
 def test_concurrent_freeze_adjustments_mail(self):
  sent=[]
  class CaptureDB(DB):
   def q(self,sql,*a):
    if sql.startswith('INSERT INTO lb_mail('):sent.append(a);return []
    return super().q(sql,*a)
  token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32);key=digest(token);txids=[]
  server=ThreadingHTTPServer(('127.0.0.1',0),Handler);base='http://127.0.0.1:'+str(server.server_port);threading.Thread(target=server.serve_forever,daemon=True).start()
  def lock(y):
   req=urllib.request.Request(base+f'/households/1/reports/{y}/lock',urllib.parse.urlencode({'csrf':csrf}).encode(),headers={'Cookie':'lotbook='+token})
   try:r=urllib.request.build_opener(NoRedirect()).open(req,timeout=55)
   except urllib.error.HTTPError as ex:r=ex
   status=r.status;r.read();return status
  with DB() as d:
   if d.one('SELECT 1 FROM lb_locks WHERE household_id=1 AND year>2023'):self.skipTest('Household already has new locked years')
   d.q('INSERT INTO lb_sessions(hash,user_id,csrf) VALUES($1,1,$2)',key,csrf)
   try:
    with patch('app.DB',CaptureDB):
     with concurrent.futures.ThreadPoolExecutor(2) as pool:statuses=list(pool.map(lock,[2024,2024]))
     self.assertEqual(sorted(statuses),[303,409]);self.assertEqual(len(sent),3)
     for recipient,name,subject,body in sent:
      self.assertIn('Year-end summary 2024',subject);self.assertTrue(subject.endswith('locked by Priya Raman'))
      for label in ['Short-term gains:','Long-term gains:','Wash-sale loss disallowed:','Dividends:','Adjustments for earlier years:']:self.assertIn(label,body)
     en=Engine(d,1).run();before=en.report(2024)[:2];baseadj=sum(v[6] for v in en.report(2025)[2].values())
     def dividend():
      tid=int(d.one('SELECT greatest((SELECT max(id) FROM legacy_transactions),coalesce((SELECT max(id) FROM lb_transactions),0))+1 AS id')['id']);txids.append(tid);d.q("INSERT INTO lb_transactions(id,account_id,security_id,kind,trade_date,amount) VALUES($1,1,2,'dividend','2024-11-15',12.34)",tid)
     dividend();en=Engine(d,1).run();self.assertEqual(en.report(2024)[:2],before);self.assertEqual(sum(v[6] for v in en.report(2025)[2].values()),baseadj+1234)
     self.assertEqual(lock(2025),303);self.assertEqual(len(sent),6)
     dividend();en=Engine(d,1).run();self.assertEqual(sum(v[6] for v in en.report(2026)[2].values()),1234);self.assertEqual(lock(2025),409);self.assertEqual(len(sent),6)
   finally:
    for tid in txids:d.q('DELETE FROM lb_transactions WHERE id=$1',tid)
    d.q('DELETE FROM lb_locks WHERE household_id=1 AND year IN (2024,2025)');d.q('DELETE FROM lb_sessions WHERE hash=$1',key);server.shutdown();server.server_close()
if __name__=='__main__':unittest.main()
