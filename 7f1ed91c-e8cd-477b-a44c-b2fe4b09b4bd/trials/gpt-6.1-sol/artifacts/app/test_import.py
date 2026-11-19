import os,secrets,urllib.request,urllib.error,concurrent.futures,unittest
from db import DB
from app import digest
class ImportFlow(unittest.TestCase):
 def test_atomic_duplicate_concurrent(self):
  token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32);prefix='test-'+secrets.token_hex(8);filename='../'+prefix+'<unsafe>.csv';key=digest(token)
  with DB() as d:
   d.q('INSERT INTO lb_sessions(hash,user_id,csrf) VALUES($1,1,$2)',key,csrf)
   def upload(rows):
    boundary='LotbookBoundary'+secrets.token_hex(8)
    csv='Export\nAccount Number: NW-4821-3377\nTrade Date,Settlement Date,Activity,Symbol,Description,Quantity,Price,Commission,Net Amount,Currency,Reference\n'+rows
    body=(f'--{boundary}\r\nContent-Disposition: form-data; name="csrf"\r\n\r\n{csrf}\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: text/csv\r\n\r\n{csv}\r\n--{boundary}--\r\n').encode()
    req=urllib.request.Request(os.environ['PUBLIC_BASE_URL'].rstrip('/')+'/accounts/1/imports',body,headers={'Cookie':'lotbook='+token,'Content-Type':'multipart/form-data; boundary='+boundary})
    try:r=urllib.request.urlopen(req,timeout=55)
    except urllib.error.HTTPError as ex:r=ex
    return r.status,r.read().decode()
   try:
    row=f'01/04/2027,,BUY,AURQ,<script>alert(1)</script>,5,,,(-50.00),USD,{prefix}\n'.replace('(-50.00)','(50.00)')
    status,body=upload(row+row);self.assertEqual(status,200);self.assertIn('data-imported-count="1"',body);self.assertIn('data-duplicate-count="1"',body)
    status,body=upload(row+row);self.assertEqual(status,200);self.assertIn('data-imported-count="0"',body);self.assertIn('data-duplicate-count="2"',body)
    status,body=upload(row.replace('(50.00)','(51.00)'));self.assertEqual(status,422);self.assertIn('data-error-line="4"',body)
    row2=row.replace(prefix,prefix+'-new');invalid=row.replace(prefix,prefix+'-bad').replace(',5,,,',',-5,,,');status,body=upload(row2+invalid);self.assertEqual(status,422);self.assertIn('data-error-line="5"',body);self.assertFalse(d.one('SELECT 1 FROM lb_transactions WHERE ref=$1',prefix+'-new'))
    with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(upload,[row2,row2]))
    self.assertEqual([s for s,b in results],[200,200]);self.assertEqual(d.one('SELECT count(*) n FROM lb_transactions WHERE ref=$1',prefix+'-new')['n'],'1');self.assertEqual(sum('data-imported-count="1"' in b for s,b in results),1)
    req=urllib.request.Request(os.environ['PUBLIC_BASE_URL'].rstrip('/')+'/accounts/1',headers={'Cookie':'lotbook='+token});body=urllib.request.urlopen(req).read().decode();self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;',body);self.assertNotIn('<script>alert(1)</script>',body)
   finally:
    d.q('DELETE FROM lb_transactions WHERE ref LIKE $1',prefix+'%');d.q('DELETE FROM lb_imports WHERE filename=$1',filename);d.q('DELETE FROM lb_sessions WHERE hash=$1',key)
if __name__=='__main__':unittest.main()
