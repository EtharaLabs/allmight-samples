import ctypes as C, ctypes.util, os, json
from datetime import date, datetime
L=C.CDLL(os.path.join(os.path.dirname(__file__),'libpq.so.5'))
def bind(n,args,ret):
 f=getattr(L,n); f.argtypes=args; f.restype=ret; return f
ptr=C.c_void_p; st=C.c_char_p; I=C.c_int
connect=bind('PQconnectdb',[st],ptr); status=bind('PQstatus',[ptr],I); finish=bind('PQfinish',[ptr],None)
execute=bind('PQexecParams',[ptr,st,I,ptr,C.POINTER(st),ptr,ptr,I],ptr)
rstatus=bind('PQresultStatus',[ptr],I); clear=bind('PQclear',[ptr],None); rows=bind('PQntuples',[ptr],I); cols=bind('PQnfields',[ptr],I); field=bind('PQfname',[ptr,I],st); value=bind('PQgetvalue',[ptr,I,I],st); null=bind('PQgetisnull',[ptr,I,I],I)
class DBError(Exception): pass
class DB:
 def __init__(self):
  self.c=connect((os.environ['DATABASE_URL']+(' ' if '://' not in os.environ['DATABASE_URL'] else ('&' if '?' in os.environ['DATABASE_URL'] else '?'))+'connect_timeout=3').encode())
  if status(self.c): finish(self.c); raise DBError()
 def q(self,sql,*args):
  a=(st*len(args))(*[None if x is None else str(x).encode() for x in args]); r=execute(self.c,sql.encode(),len(args),None,a,None,None,0)
  try:
   if not r or rstatus(r) not in (1,2): raise DBError()
   names=[field(r,j).decode() for j in range(cols(r))]
   return [{n:None if null(r,i,j) else value(r,i,j).decode() for j,n in enumerate(names)} for i in range(rows(r))]
  finally:
   if r: clear(r)
 def close(self): finish(self.c)
 def __enter__(self): return self
 def __exit__(self,t,v,b): self.close()
 def one(self,sql,*a):
  r=self.q(sql,*a); return r[0] if r else None

def init():
 with DB() as d:
  for sql in [
  'CREATE TABLE IF NOT EXISTS lb_auth(user_id integer PRIMARY KEY, password_hash text)',
  'CREATE TABLE IF NOT EXISTS lb_resets(hash text PRIMARY KEY,user_id integer,requested_at timestamptz NOT NULL DEFAULT now(), used boolean NOT NULL DEFAULT false)',
  'CREATE TABLE IF NOT EXISTS lb_sessions(hash text PRIMARY KEY,user_id integer,csrf text NOT NULL)',
  'CREATE TABLE IF NOT EXISTS lb_transactions(id bigint PRIMARY KEY,account_id integer NOT NULL,security_id integer NOT NULL,kind text NOT NULL,trade_date date NOT NULL,quantity numeric,amount numeric,split_new integer,split_old integer,to_account_id integer,ref text,memo text,description text,specific jsonb)',
  'CREATE UNIQUE INDEX IF NOT EXISTS lb_tx_ref ON lb_transactions(account_id,ref) WHERE ref IS NOT NULL',
  'CREATE TABLE IF NOT EXISTS lb_imports(id bigserial PRIMARY KEY,account_id integer,filename text,created_at timestamptz DEFAULT now())',
  'CREATE TABLE IF NOT EXISTS lb_locks(household_id integer,year integer,summary text NOT NULL,realized text NOT NULL,figures jsonb NOT NULL,PRIMARY KEY(household_id,year))',
  'CREATE TABLE IF NOT EXISTS lb_mail(id bigserial PRIMARY KEY,recipient text,recipient_name text,subject text,body text,sent boolean DEFAULT false)',
  'CREATE INDEX IF NOT EXISTS lb_tx_account ON lb_transactions(account_id)']:
   d.q(sql)
