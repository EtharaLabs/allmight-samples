import ctypes as C, os, json
p=C.CDLL(os.path.join(os.path.dirname(__file__),'lib/libpq.so.5'))
def sig(n,ret,args):
 f=getattr(p,n);f.restype=ret;f.argtypes=args;return f
connect=sig('PQconnectdb',C.c_void_p,[C.c_char_p]);status=sig('PQstatus',C.c_int,[C.c_void_p]);finish=sig('PQfinish',None,[C.c_void_p]);execute=sig('PQexecParams',C.c_void_p,[C.c_void_p,C.c_char_p,C.c_int,C.c_void_p,C.POINTER(C.c_char_p),C.c_void_p,C.c_void_p,C.c_int]);rs=sig('PQresultStatus',C.c_int,[C.c_void_p]);nt=sig('PQntuples',C.c_int,[C.c_void_p]);nf=sig('PQnfields',C.c_int,[C.c_void_p]);name=sig('PQfname',C.c_char_p,[C.c_void_p,C.c_int]);value=sig('PQgetvalue',C.c_char_p,[C.c_void_p,C.c_int,C.c_int]);null=sig('PQgetisnull',C.c_int,[C.c_void_p,C.c_int,C.c_int]);clear=sig('PQclear',None,[C.c_void_p])
class DB:
 def __init__(self):
  self.c=connect((os.environ['DATABASE_URL']+('&' if '?' in os.environ['DATABASE_URL'] else '?')+'connect_timeout=2').encode())
  if status(self.c):finish(self.c);raise RuntimeError('Database unavailable')
  self.q("SET client_min_messages='error'")
  self.q("SET statement_timeout='4000ms'")
 def q(self,sql,*args):
  vals=(C.c_char_p*len(args))(*[None if x is None else str(x).encode() for x in args]);r=execute(self.c,sql.encode(),len(args),None,vals,None,None,0)
  if not r:raise RuntimeError('Database operation failed')
  try:
   if rs(r) not in (1,2):raise RuntimeError('Database operation failed')
   cols=[name(r,j).decode() for j in range(nf(r))]
   return [{k:None if null(r,i,j) else value(r,i,j).decode() for j,k in enumerate(cols)} for i in range(nt(r))]
  finally:clear(r)
 def one(self,sql,*a):
  r=self.q(sql,*a);return r[0] if r else None
 def close(self):finish(self.c)
 def __enter__(self):return self
 def __exit__(self,*a):self.close()
def migrate():
 with DB() as d:
  for sql in open(os.path.join(os.path.dirname(__file__),'schema.sql')).read().split(';'):
   if sql.strip():d.q(sql)
