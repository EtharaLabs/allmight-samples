import ctypes as C, os, json
P=C.CDLL(os.path.join(os.path.dirname(__file__),'lib/libpq.so.5'))
for n,args,ret in [('PQconnectdb',[C.c_char_p],C.c_void_p),('PQstatus',[C.c_void_p],C.c_int),('PQfinish',[C.c_void_p],None),('PQexecParams',[C.c_void_p,C.c_char_p,C.c_int,C.c_void_p,C.POINTER(C.c_char_p),C.c_void_p,C.c_void_p,C.c_int],C.c_void_p),('PQresultStatus',[C.c_void_p],C.c_int),('PQntuples',[C.c_void_p],C.c_int),('PQnfields',[C.c_void_p],C.c_int),('PQfname',[C.c_void_p,C.c_int],C.c_char_p),('PQgetvalue',[C.c_void_p,C.c_int,C.c_int],C.c_char_p),('PQgetisnull',[C.c_void_p,C.c_int,C.c_int],C.c_int),('PQftype',[C.c_void_p,C.c_int],C.c_int),('PQclear',[C.c_void_p],None)]:
 f=getattr(P,n); f.argtypes=args; f.restype=ret
class DB:
 def __init__(self):
  self.c=P.PQconnectdb((os.environ['DATABASE_URL']+('&' if '?' in os.environ['DATABASE_URL'] else '?')+'connect_timeout=3').encode())
  if P.PQstatus(self.c)!=0: self.close(); raise RuntimeError('Database unavailable')
 def close(self):
  if self.c: P.PQfinish(self.c); self.c=None
 def q(self,sql,*params):
  vals=(C.c_char_p*len(params))(*[None if p is None else str(p).encode() for p in params])
  r=P.PQexecParams(self.c,sql.encode(),len(params),None,vals,None,None,0)
  try:
   if P.PQresultStatus(r) not in (1,2): raise ValueError('The request conflicts with existing data or contains invalid values.')
   out=[]
   for i in range(P.PQntuples(r)):
    row={}
    for j in range(P.PQnfields(r)):
     v=None if P.PQgetisnull(r,i,j) else P.PQgetvalue(r,i,j).decode(); typ=P.PQftype(r,j)
     if v is not None:
      if typ in (20,21,23): v=int(v)
      elif typ==16: v=v=='t'
      elif typ in (114,3802): v=json.loads(v)
     row[P.PQfname(r,j).decode()]=v
    out.append(row)
   return out
  finally: P.PQclear(r)
 def one(self,sql,*p):
  rows=self.q(sql,*p); return rows[0] if rows else None
 def __enter__(self): self.q('BEGIN'); return self
 def __exit__(self,t,v,b):
  try: self.q('ROLLBACK' if t else 'COMMIT')
  finally: self.close()
