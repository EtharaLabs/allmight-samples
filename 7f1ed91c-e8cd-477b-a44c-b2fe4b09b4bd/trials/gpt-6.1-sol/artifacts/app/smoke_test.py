import os,secrets,urllib.request,urllib.error,time,re
from db import DB
from app import digest
base=os.environ['PUBLIC_BASE_URL'].rstrip('/')
def req(path,token=None,data=None):
 r=urllib.request.Request(base+path,data=data,headers={'Cookie':'lotbook='+token} if token else {})
 try:
  with urllib.request.urlopen(r,timeout=55) as x:return x.status,x.read().decode(),x.headers
 except urllib.error.HTTPError as x:return x.code,x.read().decode(),x.headers
with DB() as d:
 sessions=[]
 try:
  for uid in (1,9,8,10):
   token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32);key=digest(token);sessions.append(key);d.q('INSERT INTO lb_sessions(hash,user_id,csrf) VALUES($1,$2,$3)',key,uid,csrf)
   paths=['/','/households/1','/households/1/securities','/households/1/corporate-actions','/households/1/transfers','/accounts/1','/accounts/1/lots','/households/1/reports/2023','/households/1/reports/2025','/households/1/reports/2025/summary.csv','/households/1/reports/2025/realized.csv','/households/1/unrealized.csv'] if uid==1 else (['/','/households/2','/accounts/6','/households/2/reports/2025'] if uid==10 else ['/','/households/1','/accounts/1','/households/1/reports/2025'])
   for path in paths:
    t=time.time();status,body,headers=req(path,token);assert status==200,(uid,path,status);assert headers['Cache-Control']=='no-store'
    if uid in (8,9):assert not re.search('data-action="(?:create-account|create-security|import-statement|change-method|lock-year)"',body)
    print('PASS',uid,path,round(time.time()-t,2))
   status,_,_=req('/accounts/1/method',token,b'lot_method=FIFO&effective_from=2025-01-01');assert status==403
   if uid in (9,8):
    status,_,_=req('/households/1/accounts',token,('csrf='+csrf).encode());assert status==403
   status,_,_=req('/households/'+('1' if uid==10 else '3'),token);assert status==403
  print('All HTTP smoke checks passed')
 finally:
  for key in sessions:d.q('DELETE FROM lb_sessions WHERE hash=$1',key)
