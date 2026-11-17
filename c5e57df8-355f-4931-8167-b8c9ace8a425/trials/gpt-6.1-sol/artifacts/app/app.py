import os,json,ctypes as C,hashlib,hmac,secrets,time,datetime as dt,threading,traceback,re,html,math,csv,io,smtplib,urllib.request,urllib.parse
from decimal import Decimal,ROUND_HALF_UP
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from http.cookies import SimpleCookie
from email.message import EmailMessage
from email.utils import formataddr
E=os.environ
ROOT=os.path.dirname(__file__)
pq=C.CDLL(ROOT+'/lib/libpq.so.5')
for n,rest,args in [('PQconnectdb',C.c_void_p,[C.c_char_p]),('PQstatus',C.c_int,[C.c_void_p]),('PQexec',C.c_void_p,[C.c_void_p,C.c_char_p]),('PQresultStatus',C.c_int,[C.c_void_p]),('PQntuples',C.c_int,[C.c_void_p]),('PQnfields',C.c_int,[C.c_void_p]),('PQfname',C.c_char_p,[C.c_void_p,C.c_int]),('PQftype',C.c_uint,[C.c_void_p,C.c_int]),('PQgetvalue',C.c_char_p,[C.c_void_p,C.c_int,C.c_int]),('PQgetisnull',C.c_int,[C.c_void_p,C.c_int,C.c_int]),('PQerrorMessage',C.c_char_p,[C.c_void_p]),('PQclear',None,[C.c_void_p]),('PQfinish',None,[C.c_void_p])]:
 f=getattr(pq,n); f.restype=rest; f.argtypes=args
class DB:
 def __init__(self):
  self.c=pq.PQconnectdb((E['DATABASE_URL']+'?connect_timeout=2' if '?' not in E['DATABASE_URL'] else E['DATABASE_URL']+'&connect_timeout=2').encode())
  if pq.PQstatus(self.c): pq.PQfinish(self.c); raise RuntimeError('Database unavailable')
  self.q("SET statement_timeout='3500ms'")
 def q(self,s):
  r=pq.PQexec(self.c,s.encode())
  try:
   if pq.PQresultStatus(r) not in (1,2): raise RuntimeError(pq.PQerrorMessage(self.c).decode())
   cols=[(pq.PQfname(r,j).decode(),pq.PQftype(r,j)) for j in range(pq.PQnfields(r))]; out=[]
   for i in range(pq.PQntuples(r)):
    d={}
    for j,(n,t) in enumerate(cols):
     v=None if pq.PQgetisnull(r,i,j) else pq.PQgetvalue(r,i,j).decode()
     if v is not None:
      if t in (20,21,23): v=int(v)
      elif t==16: v=v=='t'
     d[n]=v
    out.append(d)
   return out
  finally: pq.PQclear(r)
 def close(self): pq.PQfinish(self.c)
def sql(v):
 if v is None:return 'NULL'
 if isinstance(v,bool):return 'true' if v else 'false'
 if isinstance(v,(int,Decimal)):return str(v)
 return "'"+str(v).replace("'","''")+"'"
def insert(db,table,d):return db.q('INSERT INTO '+table+' ('+','.join(d)+') VALUES ('+','.join(sql(v) for v in d.values())+') RETURNING *')[0]
def utc():return dt.datetime.now(dt.timezone.utc)
def stamp(d=None):return (d or utc()).astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
def digest(x):return hashlib.sha256(x.encode()).hexdigest()
def salted(x,s=None):
 s=s or secrets.token_hex(16); return s+':'+hashlib.pbkdf2_hmac('sha256',x.encode(),s.encode(),160000).hex()
def matches(x,h):return hmac.compare_digest(salted(x,h.split(':')[0]),h)
def money(x):return '${:,.2f}'.format(Decimal(x)/100)
def rnd(x):return int(Decimal(x).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
def esc(x):return html.escape(str(x if x is not None else ''),quote=True)
def attrs(**kw):return ' '.join(k.replace('_','-')+'="'+esc(v)+'"' for k,v in kw.items())
class Fail(Exception):
 def __init__(self,msg,code=422):self.msg=msg;self.code=code

def textval(x,lo,hi,newline=False):
 if not isinstance(x,str) or not lo<=len(x)<=hi or any(ord(c)<32 and not(newline and c in '\r\n') for c in x):raise Fail('Please check the text length and remove control characters.')
 return x
def integer(x,lo,hi=10**12):
 if isinstance(x,bool) or not re.fullmatch(r'\d+',str(x)) or not lo<=int(x)<=hi:raise Fail('Enter a whole number between '+str(lo)+' and '+str(hi)+'.')
 return int(x)
def choice(x,values):
 if x not in values:raise Fail('Choose a valid value.')
 return x
def date(x):
 try:
  if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',x):raise ValueError()
  return dt.date.fromisoformat(x)
 except:raise Fail('Enter a valid calendar date.')
def code(x):return str(x).strip().upper()
def provider(path,data=None,key=None):
 req=urllib.request.Request(E['PAYMENTS_API_URL'].rstrip('/')+path,data=urllib.parse.urlencode(data).encode() if data is not None else None,headers={'Idempotency-Key':key} if key else {})
 with urllib.request.urlopen(req,timeout=5) as r:return json.load(r)

def init():
 db=DB();db.q("SET statement_timeout='55000ms'")
 db.q('CREATE TABLE IF NOT EXISTS pp_state (id integer PRIMARY KEY, data jsonb NOT NULL); CREATE TABLE IF NOT EXISTS pp_movements (id bigserial PRIMARY KEY,lot_id integer NOT NULL,kind text NOT NULL,units integer NOT NULL,at timestamptz NOT NULL,legacy_id bigint UNIQUE); CREATE INDEX IF NOT EXISTS pp_mlot ON pp_movements(lot_id); CREATE INDEX IF NOT EXISTS pp_mat ON pp_movements(at);')
 db.q('BEGIN'); db.q('SELECT pg_advisory_xact_lock(710421)')
 if not db.q('SELECT id FROM pp_state WHERE id=1'):
  tz=ZoneInfo(db.q('select timezone from sites order by id limit 1')[0]['timezone'])
  rows=db.q('select * from legacy_movements order by id')
  for start in range(0,len(rows),1000):
   vs=[]
   for r in rows[start:start+1000]:
    at=dt.datetime.fromisoformat(r['local_time']).replace(tzinfo=tz,fold=r['fold']).astimezone(dt.timezone.utc).isoformat()
    vs.append('('+','.join(sql(v) for v in [r['lot_id'],r['kind'],r['units'],at,r['id']])+')')
   db.q('INSERT INTO pp_movements(lot_id,kind,units,at,legacy_id) VALUES '+','.join(vs)+' ON CONFLICT DO NOTHING')
  state={k:{} for k in ['auth','sessions','resets','keys','orders','tasks','waves','shipments','rmas','refunds','holds','low','events']};state['emails']=[];state['seq']=max([r['id'] for r in db.q('select id from legacy_orders')]+[0]);state['counter']=0
  insert(db,'pp_state',{'id':1,'data':json.dumps(state)})
 db.q('COMMIT');db.close()

class Context:
 def __init__(self,db,state):
  self.db=db;self.s=state;self.after_mail=[]
  self.users={r['id']:r for r in db.q('select * from users')}; self.customers={r['id']:r for r in db.q('select * from customers')};self.skus={r['id']:r for r in db.q('select * from skus')};self.bins={r['id']:r for r in db.q('select * from bins')}
  self.tz=ZoneInfo(db.q('select timezone from sites order by id limit 1')[0]['timezone']);self.today=utc().astimezone(self.tz).date()
  self.lots={};self.stock={};self.version={}
 def inventory(self):
  if self.lots:return
  self.lots={r['id']:r for r in self.db.q('select * from lots')}
  for r in self.db.q('select lot_id,sum(units)::bigint as units,count(*)::bigint as version from pp_movements group by lot_id'):self.stock[r['lot_id']]=r['units'];self.version[r['lot_id']]=r['version']
 def uid(self):self.s['counter']+=1;return self.s['counter']
 def mail(self,uid,subject,body):
  u=self.users.get(uid)
  if u:self.s['emails'].append({'name':u['full_name'],'to':u['email'].strip().lower(),'subject':subject,'body':body,'sent':False})
 def movement(self,lid,kind,n):
  insert(self.db,'pp_movements',{'lot_id':lid,'kind':kind,'units':n,'at':utc().isoformat()});self.stock[lid]=self.stock.get(lid,0)+n;self.version[lid]=self.version.get(lid,0)+1
 def tasks(self,o=None):return [t for t in self.s['tasks'].values() if o is None or t['order_id']==o['id']]
 def reserved(self,lid):return sum(t['units'] for t in self.s['tasks'].values() if t['lot_id']==lid and not t.get('shipped'))
 def available(self,l):return self.stock.get(l['id'],0)-self.reserved(l['id'])
 def statuslot(self,l):
  if self.bins[l['bin_id']]['code']=='QA-01':return 'quarantine'
  if l['qa_hold'] or str(l['id']) in self.s['holds']:return 'hold'
  if date(l['expiry_date'])<self.today:return 'expired'
  return 'available'
 def allocatable(self,l):return self.bins[l['bin_id']]['code']!='QA-01' and not l['qa_hold'] and str(l['id']) not in self.s['holds']
 def low(self,sid):
  self.inventory();sk=self.skus[sid]; n=sum(self.available(l) for l in self.lots.values() if l['sku_id']==sid and self.allocatable(l) and date(l['expiry_date'])>=self.today)
  key=str(sid)+':'+str(self.today)
  if n<sk['reorder_point'] and key not in self.s['low']:
   self.s['low'][key]=True
   for u in self.users.values():
    if u['active'] and u['role']=='supervisor':self.mail(u['id'],'Low stock · '+sk['code'],f"Available: {n} units. Reorder point: {sk['reorder_point']}.")
 def alloc(self,o,l,n):
  self.inventory();c=self.customers[o['customer_id']];sk=self.skus[l['sku_id']]
  for lot in sorted(self.lots.values(),key=lambda x:(x['expiry_date'],x['id'])):
   if lot['sku_id']!=sk['id'] or not self.allocatable(lot) or (date(lot['expiry_date'])-date(o['ship_date'])).days<c['min_shelf_life_days']:continue
   av=self.available(lot)
   if not c['allow_split_cases']:av=av//sk['case_pack']*sk['case_pack']
   take=min(n,av)
   if not c['allow_split_cases']:take=take//sk['case_pack']*sk['case_pack']
   if take<=0:continue
   existing=next((t for t in self.tasks(o) if t['line_no']==l['line_no'] and t['lot_id']==lot['id'] and t['status']=='open' and not t['wave_id']),None)
   if existing:existing['units']+=take
   else:
    tid=self.uid();self.s['tasks'][str(tid)]={'id':tid,'order_id':o['id'],'line_no':l['line_no'],'lot_id':lot['id'],'units':take,'status':'open','wave_id':None,'shipped':False}
   n-=take
   if not n:break
  self.low(sk['id']);return n
 def shortage(self,o,l,n):
  left=self.alloc(o,l,n)
  if self.customers[o['customer_id']]['backorders_allowed']:l['backordered']+=left
  else:l['cancelled']+=left
 def fill(self,sid):
  for o in sorted(self.s['orders'].values(),key=lambda x:x['id']):
   if not o.get('paid'):continue
   for l in o['lines']:
    if l['sku_id']==sid and l['backordered']:l['backordered']=self.alloc(o,l,l['backordered'])
   self.update(o)
 def tax(self,o,l,n):return rnd(Decimal(n*l['unit_price_cents'])*Decimal(o['rate'])) if l['taxable'] else 0
 def due(self,o):
  if all(l['qty']==l['cancelled'] for l in o['lines']):return 0
  return sum((l['qty']-l['cancelled'])*l['unit_price_cents']+self.tax(o,l,l['qty']-l['cancelled']) for l in o['lines'])+o['freight_cents']+o['freight_tax_cents']
 def refund(self,o,n,pi=None,reason=None):
  if n<=0:return
  rid=self.uid();self.s['refunds'][str(rid)]={'id':rid,'order_id':o['id'],'amount_cents':n,'status':'pending','pi':pi or o['payment_intent'],'key':secrets.token_hex(24),'provider_id':None,'reason':reason,'attempt':0}
 def update(self,o):
  if o.get('paid'):o['status']='in_fulfilment' if any(l['backordered'] or any(t['units'] and not t['shipped'] for t in self.tasks(o) if t['line_no']==l['line_no']) for l in o['lines']) else ('complete' if any(l['shipped'] for l in o['lines']) else 'cancelled')
 def orderjson(self,o):
  out={k:o[k] for k in ['id','number','status','customer_id','ship_date','po_number','subtotal_cents','tax_cents','freight_cents','freight_tax_cents','total_cents']};out['lines']=[]
  for l in o['lines']:
   ts=[t for t in self.tasks(o) if t['line_no']==l['line_no'] and not t['shipped']];a={}
   for t in ts:a[t['lot_id']]=a.get(t['lot_id'],0)+t['units']
   out['lines'].append({**{k:l[k] for k in ['line_no','qty','unit_price_cents','amount_cents','tax_cents','shipped','backordered','cancelled','returned']},'sku':self.skus[l['sku_id']]['code'],'reserved':sum(a.values()),'allocations':[{'lot_id':i,'units':n} for i,n in sorted(a.items()) if n]})
  out['refunds']=[{k:r[k] for k in ['id','amount_cents','status']} for r in self.s['refunds'].values() if r['order_id']==o['id']]
  out['shipments']=[{k:r[k] for k in ['id','tracking_number','invoice_number','invoice_total_cents']} for r in self.s['shipments'].values() if r['order_id']==o['id']]
  out['credit_notes']=[{'id':r['credit_id'],'rma_id':r['id'],'amount_cents':r['credit']} for r in self.s['rmas'].values() if r['order_id']==o['id'] and r.get('credit_id')];return out
 def createorder(self,u,d):
  if not isinstance(d,dict):raise Fail('Expected a JSON object.',400)
  c=self.customers[u['customer_id']];ship=date(d.get('ship_date',''))
  if not self.today<=ship<=self.today+dt.timedelta(days=365):raise Fail('Ship date must be within the next 365 days.')
  po=textval(d.get('po_number',''),0,40);notes=textval(d.get('notes',''),0,500,True);ls=d.get('lines')
  if not isinstance(ls,list) or not 1<=len(ls)<=12:raise Fail('Include 1 to 12 order lines.')
  rate=self.db.q('select * from tax_rates where state='+sql(c['ship_to_state']))[0];lines=[];seen=set();grams=0;cold=False
  for i,l in enumerate(ls,1):
   if not isinstance(l,dict):raise Fail('Invalid order line.')
   sk=next((s for s in self.skus.values() if s['code']==code(l.get('sku','')) and s['active']),None)
   if not sk or sk['id'] in seen:raise Fail('Choose an active SKU only once per order.')
   seen.add(sk['id']);n=integer(l.get('qty'),1,100000)
   if not c['allow_split_cases'] and n%sk['case_pack']:raise Fail('Quantity must be a whole number of cases.')
   p=self.db.q('select unit_price_cents from price_list where tier='+sql(c['tier'])+' and sku_id='+str(sk['id'])+' and effective_from<='+sql(ship)+' order by effective_from desc limit 1')
   if not p:raise Fail('No price is available for this SKU on the ship date.')
   price=p[0]['unit_price_cents'];amount=n*price;tax=rnd(Decimal(amount)*Decimal(rate['rate'])) if sk['taxable'] else 0
   lines.append(dict(line_no=i,sku_id=sk['id'],qty=n,unit_price_cents=price,amount_cents=amount,tax_cents=tax,taxable=sk['taxable'],shipped=0,returned=0,backordered=0,cancelled=0))
   grams+=max(n*sk['unit_weight_g'],math.ceil(n/sk['case_pack'])*math.ceil(sk['length_cm']*sk['width_cm']*sk['height_cm']/5));cold|=sk['storage']!='ambient'
  sub=sum(l['amount_cents'] for l in lines);tax=sum(l['tax_cents'] for l in lines);freight=(0 if sub>=250000 else 1200+90*math.ceil(grams/500))+(650 if cold else 0);ft=rnd(Decimal(freight)*Decimal(rate['rate'])) if rate['freight_taxable'] else 0;total=sub+tax+freight+ft
  self.s['seq']+=1;oid=self.s['seq'];approval=c['approval_limit_cents'] is not None and total>c['approval_limit_cents'];o=dict(id=oid,number=f'PP-{oid:06d}',customer_id=c['id'],creator=u['id'],placed_at=stamp(),ship_date=str(ship),po_number=po,notes=notes,status='awaiting_approval' if approval else 'awaiting_payment',approval_limit=c['approval_limit_cents'],subtotal_cents=sub,tax_cents=tax,freight_cents=freight,freight_tax_cents=ft,total_cents=total,rate=rate['rate'],lines=lines,paid=False,checkout=None,payment_intent=None)
  self.s['orders'][str(oid)]=o
  if approval:
   for a in self.users.values():
    if a['active'] and a['role']=='approver' and a['customer_id']==c['id'] and a['id']!=u['id']:self.mail(a['id'],'Approval needed · '+o['number'],'Order total '+money(total)+'. Review '+E['PUBLIC_BASE_URL']+'/orders/'+str(oid))
  return o
CSS='''*{box-sizing:border-box}body{margin:0;background:#f6f5f0;color:#243d34;font:15px/1.5 system-ui,sans-serif;overflow-wrap:anywhere}a{color:#326c52;text-decoration:none}a:hover{text-decoration:underline}header{background:#173b2f;color:#fff;padding:18px 4%;display:flex;align-items:center;gap:24px;flex-wrap:wrap}header a{color:#e3ebd1}header strong{font:700 25px Georgia}nav{display:flex;gap:18px;flex-wrap:wrap}header form{margin-left:auto}main{max-width:1180px;margin:36px auto;padding:0 24px}h1{font:38px Georgia;margin:10px 0}h2{font:25px Georgia}small,.muted{color:#718075}.eyebrow{text-transform:uppercase;letter-spacing:2px;font-size:11px;font-weight:700}section,.card,form.panel{background:#fff;border:1px solid #dce1d5;border-radius:10px;padding:22px;margin:18px 0}form{max-width:100%}label{display:block;font-size:13px;font-weight:600;margin:12px 0 5px}input,select,textarea{font:inherit;padding:10px 12px;border:1px solid #bbc9ba;border-radius:5px;background:#fff;color:#243d34;max-width:100%;width:100%}textarea{min-height:90px}button,.button{display:inline-block;background:#286048;color:#fff;border:0;border-radius:5px;padding:11px 17px;font:600 13px system-ui;cursor:pointer;margin-top:14px}header button{margin:0;background:#335c49}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,250px),1fr));gap:18px}.row{display:flex;gap:12px;flex-wrap:wrap}.row>*{flex:1;min-width:0}.badge{display:inline-block;padding:4px 10px;border-radius:20px;background:#e9eedb;font-size:12px}.metric{font:32px Georgia}.listing{display:grid;grid-template-columns:minmax(0,2fr) repeat(2,minmax(0,1fr));gap:12px;padding:15px 0;border-bottom:1px solid #e6e8df;align-items:center}.error{border-left:4px solid #bb633b;background:#fff0e5;padding:18px}.auth{max-width:480px;margin:60px auto}code{white-space:pre-wrap;overflow-wrap:anywhere}.route{border-left:3px solid #b3bf8f;padding-left:20px}.statbar{display:flex;gap:30px;flex-wrap:wrap;margin:22px 0}.statbar small{display:block}footer{padding:32px 24px;color:#7d887d;font-size:12px}@media(max-width:500px){main{padding:0 16px;margin:22px auto}header{padding:16px;gap:14px}header strong{font-size:22px}nav{font-size:13px;gap:12px}h1{font-size:31px}section,.card,form.panel{padding:16px}.listing{grid-template-columns:minmax(0,1.5fr) minmax(0,1fr)}.listing>*:last-child{grid-column:1/-1}.statbar{gap:16px}}'''

def routepath(bins):
 codes=sorted(bins);n=len(codes)
 if not n:return [],0
 def dist(x,y):
  a,b=x;c,d=y
  return abs(b-d) if a==c else 4*abs(a-c)+min(b+d,42-b-d)
 pos=[(bins[c]['aisle'],bins[c]['bay']) for c in codes];dp={}
 for j in range(n):dp[(1<<j,j)]=(dist((0,0),pos[j]),(codes[j],))
 for mask in range(1,1<<n):
  for j in range(n):
   if (mask,j) not in dp:continue
   v,p=dp[(mask,j)]
   for k in range(n):
    if mask>>k&1:continue
    key=(mask|1<<k,k);cand=(v+dist(pos[j],pos[k]),p+(codes[k],))
    if key not in dp or cand<dp[key]:dp[key]=cand
 best=min((dp[((1<<n)-1,j)][0]+dist(pos[j],(0,0)),dp[((1<<n)-1,j)][1]) for j in range(n));return list(best[1]),best[0]

class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_GET(self):self.run()
 def do_POST(self):self.run()
 def send(self,body,status=200,typ='text/html; charset=utf-8',location=None):
  self.send_response(status);self.send_header('Content-Type',typ);self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','same-origin')
  if location:self.send_header('Location',location)
  if getattr(self,'cookie',None):self.send_header('Set-Cookie',self.cookie)
  b=body.encode() if isinstance(body,str) else body;self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def redirect(self,p):self.result=('',303,'text/html',p)
 def json(self,d,status=200):self.result=(json.dumps(d),status,'application/json',None)
 def page(self,title,body):
  u=self.u;nav=''
  if u:
   links=[('/', 'Overview'),('/profile','Profile')]
   if u['role'] in ['buyer','approver','supervisor','finance']:links.insert(1,('/orders','Orders'))
   if u['role'] in ['supervisor','picker']:links.insert(1,('/waves','Pick waves'))
   if u['role'] in ['buyer','approver']:links.append(('/account/api-keys','API keys'))
   nav='<nav>'+''.join('<a href="'+p+'">'+esc(t)+'</a>' for p,t in links)+'</nav>'+self.form('/signout','signout','Sign out')
  self.result=('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>'+esc(title)+' · Pickpath</title><style>'+CSS+'</style></head><body><header><a href="/"><strong>pickpath<span style="color:#b6cb89">.</span></strong></a>'+nav+'</header><main><div class="eyebrow">Larder & Line Provisions · Secaucus, NJ</div><h1>'+esc(title)+'</h1>'+body+'</main><footer>Pickpath / Larder & Line Provisions</footer></body></html>',200,'text/html; charset=utf-8',None)
 def field(self,name,value='',kind='text',options=None):
  label=esc(name.replace('_',' ').capitalize());v=esc(value)
  if options is not None:inp='<select name="'+name+'">'+''.join('<option value="'+esc(x)+'"'+(' selected' if str(x)==str(value) else '')+'>'+esc(x)+'</option>' for x in options)+'</select>'
  elif kind=='textarea':inp='<textarea name="'+name+'">'+v+'</textarea>'
  else:inp='<input name="'+name+'" type="'+kind+'" value="'+v+'">'
  return '<label>'+label+inp+'</label>'
 def form(self,path,action,label,fields='',panel=False):
  nonce=secrets.token_urlsafe(20)
  if self.sid and self.sid in self.c.s['sessions']:
   nonces=self.c.s['sessions'][self.sid].setdefault('forms',{})
   if len(nonces)>500:nonces.clear()
   nonces[nonce]=path
  return '<form method="post" action="'+esc(path)+'"'+(' class="panel"' if panel else '')+'><input type="hidden" name="csrf" data-csrf value="'+esc(self.csrf)+'">'+fields+('' if action=='signout' else '<input type="hidden" name="action_id" value="'+nonce+'">')+'<button data-action="'+action+'">'+esc(label)+'</button></form>'
 def require(self,*roles):
  if self.u['role'] not in roles:raise Fail('You do not have permission for this action.',403)
 def own(self,o):
  if self.u['role'] in ['buyer','approver'] and self.u['customer_id']!=o['customer_id']:raise Fail('Record not found.',404)
 def obj(self,table,id):
  x=self.c.s[table].get(str(id))
  if not x:raise Fail('Record not found.',404)
  return x
 def run(self):
  self.started=time.time();self.request_key=None;self.cookie=None;self.u=None;self.csrf='';self.result=None;db=None
  p=urllib.parse.urlsplit(self.path);self.pathonly=p.path;self.query=urllib.parse.parse_qs(p.query);self.post=self.command=='POST';self.raw=b'';self.d={}
  try:
   if self.post:
    n=int(self.headers.get('Content-Length','0'))
    if n>1000000:raise Fail('Request too large.',413)
    self.raw=self.rfile.read(n)
    if self.pathonly.startswith('/api/'):
     try:self.d=json.loads(self.raw)
     except:raise Fail('Invalid JSON body.',400)
    else:self.fields=urllib.parse.parse_qs(self.raw.decode(),keep_blank_values=True);self.d={k:v[-1] for k,v in self.fields.items()}
   db=DB()
   if self.pathonly=='/healthz':db.q('select 1');self.send('ok',200,'text/plain');return
   db.q('BEGIN');s=json.loads(db.q('select data from pp_state where id=1 FOR UPDATE')[0]['data']);self.c=Context(db,s)
   cookies=SimpleCookie()
   try:cookies.load(self.headers.get('Cookie',''))
   except:pass
   rawsid=cookies.get('pickpath');self.sid=digest(rawsid.value) if rawsid else None;session=s['sessions'].get(self.sid)
   if session:
    self.csrf=session['csrf'];self.u=self.c.users.get(session.get('uid'))
    if self.u and not self.u['active']:self.u=None
   api=self.pathonly.startswith('/api/')
   if api:
    auth=self.headers.get('Authorization','');key=auth[7:] if auth.startswith('Bearer ') else '';k=next((k for k in s['keys'].values() if not k['revoked'] and matches(key,k['hash'])),None) if key else None
    self.u=self.c.users.get(k['uid']) if k else None
    if not self.u or not self.u['active']:raise Fail('Invalid API key.',401)
   if self.pathonly=='/webhooks/payments':
    if not self.post:raise Fail('POST required.',405)
    self.webhook()
   elif self.pathonly in ['/signin','/password-reset'] or self.pathonly.startswith('/password-reset/'):
    if not session:
     token=secrets.token_urlsafe(32);self.sid=digest(token);self.csrf=secrets.token_urlsafe(32);s['sessions'][self.sid]={'uid':None,'csrf':self.csrf};self.cookie='pickpath='+token+'; Path=/; HttpOnly; SameSite=Lax'
    if self.post and not hmac.compare_digest(str(self.d.get('csrf','')),self.csrf):raise Fail('Session token missing or invalid. Reload the page and try again.',403)
    self.auth()
   elif not self.u:self.redirect('/signin?next='+urllib.parse.quote(self.path,safe=''))
   else:
    if self.post and not api and (not self.csrf or not hmac.compare_digest(str(self.d.get('csrf','')),self.csrf)):raise Fail('Session token missing or invalid. Reload the page and try again.',403)
    if self.post and not api:
     self.request_key=digest(self.sid+self.pathonly+self.raw.decode())
     recent=s.setdefault('concurrency',{})
     if self.started<=recent.get(self.request_key,0):raise Fail('An identical action is already being processed. Reload the page to see the result.',409)
    if self.post and not api and self.d.get('action_id'):
     forms=s['sessions'][self.sid].setdefault('forms',{})
     if forms.pop(self.d['action_id'],None)!=self.pathonly:raise Fail('This form has already been submitted. Reload the page before trying again.',409)
    self.dispatch()
   if self.request_key:
    recent=s.setdefault('concurrency',{})
    for key in list(recent):
     if recent[key]<time.time()-300:del recent[key]
    recent[self.request_key]=time.time()
   db.q('update pp_state set data='+sql(json.dumps(s))+'::jsonb where id=1');db.q('COMMIT')
   for mail in self.c.after_mail:deliver(mail)
   if self.result:self.send(*self.result)
   else:self.send('Not found',404)
  except Fail as e:
   if db:
    try:db.q('ROLLBACK')
    except:pass
   if self.pathonly.startswith('/api/'):self.send(json.dumps({'errors':[e.msg]}),e.code,'application/json')
   else:
    self.page('Please review', '<p class="error">'+esc(e.msg)+'</p><a href="'+esc(self.pathonly)+'">Return to page</a>');b,_,t,l=self.result;self.send(b,e.code,t)
  except Exception:
   traceback.print_exc()
   if db:
    try:db.q('ROLLBACK')
    except:pass
   self.send('The service is temporarily unavailable. Please try again.',503,'text/plain')
  finally:
   if db:db.close()
 def auth(self):
  c=self.c;s=c.s;d=self.d;p=self.pathonly
  if p=='/password-reset':
   if self.post:
    email=d.get('email','').strip().lower();u=next((u for u in c.users.values() if u['active'] and u['email'].strip().lower()==email),None)
    if u:
     tok=secrets.token_urlsafe(32)
     for r in s['resets'].values():
      if r['uid']==u['id']:r['replaced']=True
     s['resets'][digest(tok)]={'uid':u['id'],'expires':time.time()+int(E['RESET_LINK_TTL_SECONDS']),'used':False,'replaced':False}
     c.after_mail.append({'name':u['full_name'],'to':u['email'].strip().lower(),'subject':'Set your Pickpath password','body':'Set your password using this single-use link:\n'+E['PUBLIC_BASE_URL'].rstrip('/')+'/password-reset/'+tok})
    self.page('Check your email','<section>If this address belongs to an active account, a password link has been sent.</section>');return
   self.page('Set or reset your password','<p>Enter your work email to receive a secure password link.</p>'+self.form(p,'request-reset','Send password link',self.field('email',kind='email'),True));return
  if p.startswith('/password-reset/'):
   r=s['resets'].get(digest(p.split('/')[-1]))
   if not r:raise Fail('Link not found.',404)
   if r['used'] or r['replaced'] or r['expires']<time.time():raise Fail('This link is no longer valid. Request a new password link.',410)
   if self.post:
    pw=d.get('password','')
    if len(pw)<10:raise Fail('Use at least 10 characters for your password.')
    s['auth'][str(r['uid'])]=salted(pw);r['used']=True;self.redirect('/signin');return
   self.page('Choose your password',self.form(p,'set-password','Set password',self.field('password',kind='password'),True));return
  nxt=self.query.get('next',['/'])[0]
  if self.post:
   email=d.get('email','').strip().lower();u=next((u for u in c.users.values() if u['email'].strip().lower()==email and u['active']),None);h=s['auth'].get(str(u['id'])) if u else None
   if not h or not matches(d.get('password',''),h):raise Fail('Email or password is incorrect.',401)
   s['sessions'].pop(self.sid,None);tok=secrets.token_urlsafe(32);s['sessions'][digest(tok)]={'uid':u['id'],'csrf':secrets.token_urlsafe(32)};self.cookie='pickpath='+tok+'; Path=/; HttpOnly; SameSite=Lax'
   valid=nxt.startswith('/') and not nxt.startswith('//') and not nxt.startswith('/\\') and '\\' not in nxt and not urllib.parse.urlsplit(nxt).scheme and not urllib.parse.urlsplit(nxt).netloc and not any(ord(x)<32 for x in nxt)
   self.redirect(nxt if valid else '/');return
  self.page('Welcome back','<div class="auth"><p>Sign in to Larder & Line Provisions.</p>'+self.form('/signin?next='+urllib.parse.quote(nxt,safe=''),'signin','Sign in',self.field('email',kind='email')+self.field('password',kind='password'),True)+'<a href="/password-reset">Set or reset your password</a></div>')
 def webhook(self):
  c=self.c;s=c.s;header=self.headers.get('Stripe-Signature','');parts={}
  for x in header.split(','):
   if '=' in x:
    k,v=x.split('=',1);parts.setdefault(k,[]).append(v)
  try:
   t=parts['t'][0]
   if not re.fullmatch(r'\d+',t) or abs(time.time()-int(t))>300:raise ValueError()
   key=open(E['PAYMENTS_WEBHOOK_SIGNING_FILE']).read().strip().encode();sig=hmac.new(key,t.encode()+b'.'+self.raw,hashlib.sha256).hexdigest()
   if not any(hmac.compare_digest(sig,x) for x in parts.get('v1',[])):raise ValueError()
   e=json.loads(self.raw)
  except:raise Fail('Invalid webhook signature.',400)
  eid=e.get('id');typ=e.get('type');obj=e.get('data',{}).get('object',{})
  if eid in s['events']:self.json({'ok':True});return
  if typ=='checkout.session.completed' and obj.get('payment_status')=='paid':
   oid=str(obj.get('metadata',{}).get('order_id',''));o=s['orders'].get(oid)
   if o:
    if o['status']=='awaiting_payment' and o['checkout'] and o['checkout']['id']==obj.get('id') and obj.get('amount_total')==o['total_cents']:
     o['paid']=True;o['payment_intent']=obj['payment_intent'];o['paid_session']=obj['id'];before=c.due(o)
     for l in o['lines']:c.shortage(o,l,l['qty'])
     c.refund(o,before-c.due(o));c.update(o);c.mail(o['creator'],'Order confirmed · '+o['number'],'Your order is confirmed. Total: '+money(o['total_cents']))
    elif (o['status']=='cancelled' or o['paid']) and obj.get('id')!=o.get('paid_session'):
     reason='late:'+obj['id']
     if not any(r.get('reason')==reason for r in s['refunds'].values()):c.refund(o,obj['amount_total'],obj['payment_intent'],reason)
  elif typ=='refund.created':
   rid=str(obj.get('metadata',{}).get('refund_id',''));r=s['refunds'].get(rid) or next((r for r in s['refunds'].values() if r['provider_id']==obj.get('id')),None)
   if r and r['status']=='pending':
    r['status']='refunded';r['provider_id']=obj.get('id');o=s['orders'][str(r['order_id'])];c.mail(o['creator'],'Refund issued · '+o['number'],'Refund issued: '+money(r['amount_cents']))
  if eid:s['events'][eid]=True
  self.json({'ok':True})
 def dispatch(self):
  if not self.post and re.search(r'/(deactivate|revoke|confirm|count|approve|cancel|pay|cancel-backorder|ship|receive|inspect|rmas)$',self.pathonly):raise Fail('Use the action form.',405)
  c=self.c;s=c.s;u=self.u;d=self.d;p=self.pathonly;role=u['role'];db=c.db
  if p=='/signout':
   if not self.post:raise Fail('Use the sign-out button.',405)
   s['sessions'].pop(self.sid,None);self.cookie='pickpath=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax';self.redirect('/signin');return
  if p=='/profile':
   if self.post:db.q('update users set full_name='+sql(textval(d.get('full_name',''),1,100))+' where id='+str(u['id']));self.redirect(p);return
   self.page('Your profile',self.form(p,'save-profile','Save profile',self.field('full_name',u['full_name']),True));return
  if p=='/':
   links=[]
   if role in ['buyer','approver']:links=[('/orders/new','Create an order','Order from our specialty provisions catalog'),('/orders','Your orders','Payments, deliveries and returns'),('/account/api-keys','API access','Connect your ordering system')]
   if role=='supervisor':links=[('/waves','Pick waves','Build the shortest route through the warehouse'),('/receipts/new','Receive stock','Add a delivery and fill backorders'),('/skus/new','Add a product','Manage your specialty provisions'),('/reports/on-hand.csv','On-hand report','Download current lot balances'),('/orders','Orders','Ship ready orders and manage returns')]
   if role=='finance':links=[('/customers/new','Add a customer','Company settings and purchasing limits'),('/users/new','Add a user','Invite your team and customers'),('/prices','Price lists','Set effective prices by customer tier'),('/orders','All orders','Review sales, shipments and refunds'),('/orders/export.csv','Export orders','Download the complete order history'),('/reports/on-hand.csv','On-hand report','Current warehouse inventory')]
   if role=='picker':links=[('/waves','Your pick waves','View routes and confirm picks in zone '+u['zone'])]
   body='<p class="muted">'+esc(c.today.strftime('%A, %B %d, %Y'))+' · '+esc(role.capitalize())+'</p><h2>Hello, '+esc(u['full_name'].split(' ')[0])+'</h2><div class="grid">'+''.join('<a class="card" href="'+a+'"><h2>'+esc(b)+'</h2><p class="muted">'+esc(t)+'</p><span>Open →</span></a>' for a,b,t in links)+'</div>'
   if role in ['supervisor','finance']:
    body+='<section><h2>Product directory</h2>'+''.join('<div class="listing"><a href="/skus/'+str(sk['id'])+'">'+esc(sk['code'])+'</a><span>'+esc(sk['name'])+'</span><span>'+esc(sk['storage'])+'</span></div>' for sk in list(c.skus.values())[:40])+'</section>'
   if role=='finance':body+='<section><h2>Customers & team</h2>'+''.join('<p><a href="/customers/'+str(x['id'])+'">'+esc(x['name'])+'</a></p>' for x in c.customers.values())+''.join('<p><a href="/users/'+str(x['id'])+'">'+esc(x['full_name'])+'</a> · '+esc(x['role'])+'</p>' for x in c.users.values())+'</section>'
   self.page('Overview',body);return
  if p.startswith('/api/'):
   self.require('buyer','approver')
   if p=='/api/v1/orders' and self.post:o=c.createorder(u,d);self.json(c.orderjson(o),201);return
   m=re.fullmatch(r'/api/v1/orders/(\d+)',p)
   if m and not self.post:o=self.obj('orders',m[1]);self.own(o);self.json(c.orderjson(o));return
   raise Fail('Not found.',404)
  if p.startswith('/account/api-keys'):
   self.require('buyer','approver');m=re.fullmatch(r'/account/api-keys/(\d+)/revoke',p)
   if m:
    k=self.obj('keys',m[1])
    if k['uid']!=u['id']:raise Fail('Not found.',404)
    if k['revoked']:raise Fail('This key is already revoked.',409)
    k['revoked']=True;self.redirect('/account/api-keys');return
   out=''
   if self.post:
    label=textval(d.get('label',''),1,100);key=secrets.token_urlsafe(36);kid=c.uid();s['keys'][str(kid)]={'id':kid,'uid':u['id'],'label':label,'hash':salted(key),'revoked':False};out='<section '+attrs(data_api_key=key,data_api_key_id=kid)+'><h2>Copy your key now</h2><p>This key is shown once.</p><code>'+esc(key)+'</code></section>'
   for k in s['keys'].values():
    if k['uid']==u['id']:out+='<section '+attrs(data_api_key_id=k['id'])+'><b>'+esc(k['label'])+'</b><p>'+('Revoked' if k['revoked'] else 'Active')+'</p>'+(self.form('/account/api-keys/'+str(k['id'])+'/revoke','revoke-api-key','Revoke key') if not k['revoked'] else '')+'</section>'
   self.page('API keys',out+self.form('/account/api-keys','create-api-key','Create API key',self.field('label'),True));return
  if p=='/skus/new':
   self.require('supervisor')
   if self.post:
    skcode=code(d.get('code',''))
    if not re.fullmatch('[A-Z0-9][A-Z0-9-]{0,23}',skcode):raise Fail('Use 1–24 letters, digits or hyphens for the code.')
    if any(x['code'].upper()==skcode for x in c.skus.values()):raise Fail('SKU code already exists.',409)
    row={'code':skcode,'name':textval(d.get('name',''),1,120)}
    for k,lo,hi in [('case_pack',1,500),('unit_weight_g',1,100000),('length_cm',1,300),('width_cm',1,300),('height_cm',1,300),('reorder_point',0,1000000)]:row[k]=integer(d.get(k),lo,hi)
    row['storage']=choice(d.get('storage'),['ambient','chilled','frozen']);row['taxable']=choice(d.get('taxable'),['true','false'])=='true';row['active']=True;r=insert(db,'skus',row);self.redirect('/skus/'+str(r['id']));return
   fields=''.join(self.field(k) for k in ['code','name','case_pack','unit_weight_g','length_cm','width_cm','height_cm','reorder_point'])+self.field('storage',options=['ambient','chilled','frozen'])+self.field('taxable',options=['true','false']);self.page('Add a product',self.form(p,'create-sku','Create SKU',fields,True));return
  m=re.fullmatch(r'/skus/(\d+)',p)
  if m:
   self.require('supervisor');sk=c.skus.get(int(m[1]))
   if not sk:raise Fail('Product not found.',404)
   c.inventory();self.page(sk['code'],'<section '+attrs(data_sku_id=sk['id'])+'><h2>'+esc(sk['name'])+'</h2><p>'+esc(sk['storage'])+' · '+str(sk['case_pack'])+' units per case</p>'+''.join('<p><a href="/lots/'+str(l['id'])+'">'+esc(l['lot_code'])+'</a> · '+esc(l['expiry_date'])+' · '+str(c.stock.get(l['id'],0))+' units</p>' for l in c.lots.values() if l['sku_id']==sk['id'])+'</section>');return
  if p=='/receipts/new':
   self.require('supervisor')
   if self.post:
    sk=next((x for x in c.skus.values() if x['code']==code(d.get('sku',''))),None);b=next((x for x in c.bins.values() if x['code']==code(d.get('bin',''))),None)
    if not sk or not b or b['zone']!={'ambient':'A','chilled':'C','frozen':'F'}[sk['storage']]:raise Fail('Choose a SKU and a bin in its storage zone.')
    lc=textval(d.get('lot_code','').strip(),1,32);expiry=str(date(d.get('expiry_date','')));units=integer(d.get('units'),1,1000000);cost=d.get('unit_cost','')
    if not re.fullmatch(r'\d+(\.\d{1,4})?',cost) or Decimal(cost)<=0 or Decimal(cost)>=10**8:raise Fail('Enter a positive unit cost with at most four decimal places.')
    c.inventory();lot=next((x for x in c.lots.values() if x['sku_id']==sk['id'] and x['lot_code'].strip().lower()==lc.lower()),None)
    if lot and (lot['expiry_date']!=expiry or lot['bin_id']!=b['id'] or Decimal(lot['unit_cost'])!=Decimal(cost)):raise Fail('Existing lot has a different expiry, bin or unit cost.',409)
    if not lot:lot=insert(db,'lots',dict(sku_id=sk['id'],lot_code=lc,expiry_date=expiry,bin_id=b['id'],unit_cost=Decimal(cost),qa_hold=False));c.lots[lot['id']]=lot
    c.movement(lot['id'],'receipt',units);c.fill(sk['id']);c.low(sk['id']);self.redirect('/lots/'+str(lot['id']));return
   self.page('Receive stock',self.form(p,'receive','Receive stock',''.join(self.field(k,kind='date' if k=='expiry_date' else 'text') for k in ['sku','lot_code','expiry_date','bin','units','unit_cost']),True));return
  m=re.fullmatch(r'/lots/(\d+)(/count)?',p)
  if m:
   self.require('supervisor');c.inventory();lot=c.lots.get(int(m[1]))
   if not lot:raise Fail('Lot not found.',404)
   lid=lot['id']
   if m[2] and self.post:
    version=integer(d.get('version'),0);n=integer(d.get('counted_units'),0)
    if version!=c.version.get(lid,0):raise Fail('This lot changed since you opened the count. Reload the lot page, recount the bin and submit the new version.',409)
    picked=sum(t['units'] for t in s['tasks'].values() if t['lot_id']==lid and t['status']=='picked' and not t['shipped']);c.movement(lid,'count',n+picked-c.stock.get(lid,0));s['holds'].pop(str(lid),None)
    ts=[t for t in s['tasks'].values() if t['lot_id']==lid and t['status']=='open' and not t['shipped']];excess=max(0,sum(t['units'] for t in ts)-n);released=[];before={}
    for t in sorted(ts,key=lambda t:(t['order_id'],t['line_no'],t['id']),reverse=True):
     if not excess:break
     take=min(excess,t['units']);t['units']-=take;excess-=take;o=s['orders'][str(t['order_id'])];before.setdefault(o['id'],c.due(o));released.append((o,o['lines'][t['line_no']-1],take))
     if not t['units']:del s['tasks'][str(t['id'])]
    for o,l,take in released:c.shortage(o,l,take)
    for wave in s['waves'].values():
     bins={c.bins[c.lots[t['lot_id']]['bin_id']]['code']:c.bins[c.lots[t['lot_id']]['bin_id']] for t in s['tasks'].values() if t['wave_id']==wave['id']}
     wave['route'],wave['distance']=routepath(bins)
    for oid,bef in before.items():o=s['orders'][str(oid)];c.refund(o,bef-c.due(o));c.update(o)
    c.fill(lot['sku_id']);c.low(lot['sku_id']);self.redirect('/lots/'+str(lid));return
   f=self.field('counted_units')+'<input type="hidden" name="version" value="'+str(c.version.get(lid,0))+'">';self.page(lot['lot_code'],'<section '+attrs(data_lot_id=lid,data_on_hand=c.stock.get(lid,0),data_reserved=c.reserved(lid),data_lot_version=c.version.get(lid,0),data_lot_status=c.statuslot(lot))+'><h2>'+esc(c.skus[lot['sku_id']]['code'])+' · '+esc(c.bins[lot['bin_id']]['code'])+'</h2><p>Expiry '+esc(lot['expiry_date'])+' · '+esc(c.statuslot(lot))+'</p><p>On hand: '+str(c.stock.get(lid,0))+' · Reserved: '+str(c.reserved(lid))+' · Version: '+str(c.version.get(lid,0))+'</p></section>'+self.form('/lots/'+str(lid)+'/count','count-lot','Save cycle count',f,True));return
  if p=='/prices':
   self.require('finance')
   if self.post:
    tier=choice(d.get('tier'),['standard','gold','platinum']);sk=next((x for x in c.skus.values() if x['code']==code(d.get('sku',''))),None)
    if not sk:raise Fail('Unknown SKU.')
    price=integer(d.get('unit_price_cents'),1,10000000);ef=str(date(d.get('effective_from','')))
    if db.q('select id from price_list where tier='+sql(tier)+' and sku_id='+str(sk['id'])+' and effective_from='+sql(ef)):raise Fail('A price already exists for that date.',409)
    insert(db,'price_list',dict(tier=tier,sku_id=sk['id'],unit_price_cents=price,effective_from=ef));self.redirect(p);return
   self.page('Price lists',self.form(p,'create-price','Add price',self.field('tier',options=['standard','gold','platinum'])+self.field('sku')+self.field('unit_price_cents')+self.field('effective_from',kind='date'),True)+'<section><h2>Recent price entries</h2>'+''.join('<p>'+esc(r['tier'])+' · '+esc(c.skus[r['sku_id']]['code'])+' · '+money(r['unit_price_cents'])+' · '+esc(r['effective_from'])+'</p>' for r in db.q('select * from price_list order by id desc limit 100'))+'</section>');return
  m=re.fullmatch(r'/customers/(new|\d+)',p)
  if m:
   self.require('finance');new=m[1]=='new';cust={} if new else c.customers.get(int(m[1]))
   if cust is None:raise Fail('Customer not found.',404)
   states=[r['state'] for r in db.q('select state from tax_rates order by state')]
   if self.post:
    row=dict(name=textval(d.get('name',''),1,120),tier=choice(d.get('tier'),['standard','gold','platinum']),ship_to_state=choice(d.get('ship_to_state'),states),approval_limit_cents=None if d.get('approval_limit_cents','')=='' else integer(d['approval_limit_cents'],0,100000000),min_shelf_life_days=integer(d.get('min_shelf_life_days'),0,365),allow_split_cases=choice(d.get('allow_split_cases'),['true','false'])=='true',backorders_allowed=choice(d.get('backorders_allowed'),['true','false'])=='true')
    if new:cust=insert(db,'customers',row)
    else:db.q('update customers set '+','.join(k+'='+sql(v) for k,v in row.items())+' where id='+str(cust['id']))
    self.redirect('/customers/'+str(cust['id']));return
   fields=''
   for k in ['name','tier','ship_to_state','approval_limit_cents','min_shelf_life_days','allow_split_cases','backorders_allowed']:
    v=cust.get(k,'');v=str(v).lower() if isinstance(v,bool) else v
    options=['standard','gold','platinum'] if k=='tier' else states if k=='ship_to_state' else ['true','false'] if k in ['allow_split_cases','backorders_allowed'] else None;fields+=self.field(k,v,options=options)
   self.page('Add a customer' if new else cust['name'],'<div '+attrs(data_customer_id=cust.get('id',''))+'>'+self.form(p,'create-customer' if new else 'save-customer','Create customer' if new else 'Save customer',fields,True)+'</div>');return
  m=re.fullmatch(r'/users/(new|\d+)(/deactivate)?',p)
  if m:
   self.require('finance');new=m[1]=='new';target={} if new else c.users.get(int(m[1]))
   if target is None:raise Fail('User not found.',404)
   if m[2]:
    if not target['active']:raise Fail('User is already inactive.',409)
    db.q('update users set active=false where id='+str(target['id']));self.redirect('/users/'+str(target['id']));return
   if new and self.post:
    email=d.get('email','').strip().lower();name=textval(d.get('full_name',''),1,100);r=choice(d.get('role'),['buyer','approver','picker','supervisor','finance'])
    if len(email)>254 or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+",email):raise Fail('Enter a valid email address.')
    if any(x['email'].strip().lower()==email for x in c.users.values()):raise Fail('Email already exists.',409)
    cid=integer(d.get('customer_id'),1) if r in ['buyer','approver'] else None;zone=choice(d.get('zone'),['A','C','F']) if r=='picker' else None
    if cid and cid not in c.customers:raise Fail('Unknown customer.')
    if r not in ['buyer','approver'] and d.get('customer_id') or r!='picker' and d.get('zone'):raise Fail('Company and zone must match the role.')
    target=insert(db,'users',dict(email=email,full_name=name,role=r,customer_id=cid,zone=zone,active=True));self.redirect('/users/'+str(target['id']));return
   if new:self.page('Add a user',self.form(p,'create-user','Create user',self.field('email')+self.field('full_name')+self.field('role',options=['buyer','approver','picker','supervisor','finance'])+self.field('customer_id')+self.field('zone',options=['','A','C','F']),True));return
   self.page(target['full_name'],'<section '+attrs(data_user_id=target['id'])+'><p>'+esc(target['email'])+' · '+esc(target['role'])+' · '+('Active' if target['active'] else 'Inactive')+'</p>'+(self.form(p+'/deactivate','deactivate-user','Deactivate user') if target['active'] else '')+'</section>');return
  if p.startswith('/reports/') or p=='/orders/export.csv':self.reports();return
  if p.startswith('/orders'):self.orders();return
  if p.startswith('/waves') or p.startswith('/pick-tasks'):self.waves();return
  if p.startswith('/shipments') or p.startswith('/rmas'):self.fulfilment();return
  raise Fail('Page not found.',404)
 def orders(self):
  c=self.c;s=c.s;u=self.u;d=self.d;p=self.pathonly;role=u['role'];self.require('buyer','approver','supervisor','finance')
  if p=='/orders/new':
   self.require('buyer','approver')
   if self.post:
    skfields=self.fields.get('sku',[]);qfields=self.fields.get('qty',[]);lines=[{'sku':sk,'qty':qfields[i] if i<len(qfields) else ''} for i,sk in enumerate(skfields) if sk.strip()];o=c.createorder(u,{**d,'lines':lines});self.redirect('/orders/'+str(o['id']));return
   f=self.field('ship_date',c.today,kind='date')+self.field('po_number')+self.field('notes',kind='textarea')+'<h2>Order lines</h2><p class="muted">Enter SKU codes and units. Leave unused rows empty.</p>'+''.join('<div class="row">'+self.field('sku')+self.field('qty')+'</div>' for _ in range(12));self.page('Create an order',self.form(p,'create-order','Create order',f,True));return
  if p=='/orders':
   os=[o for o in s['orders'].values() if role not in ['buyer','approver'] or o['customer_id']==u['customer_id']];old=c.db.q('select * from legacy_orders'+(' where customer_id='+str(u['customer_id']) if role in ['buyer','approver'] else '')+' order by id desc')
   out=('<a class="button" href="/orders/new">Create order</a>' if role in ['buyer','approver'] else '')+('<p><a href="/orders/export.csv">Download order export ↓</a></p>' if role!='supervisor' else '')+'<section><div class="eyebrow">Order / Status / Total</div>'
   for o in list(reversed(os))+old:out+='<div class="listing"><a href="/orders/'+str(o['id'])+'"><b>'+esc(o['number'])+'</b><br><small>'+esc(o['ship_date'])+'</small></a><span class="badge">'+esc(o['status'].replace('_',' '))+'</span><span>'+money(o['total_cents'])+'</span></div>'
   self.page('Orders',out+'</section>');return
  m=re.fullmatch(r'/orders/(\d+)(.*)',p)
  if not m:raise Fail('Order not found.',404)
  oid=m[1];action=m[2];o=s['orders'].get(oid)
  if not o:
   rows=c.db.q('select * from legacy_orders where id='+str(int(oid)))
   if not rows:raise Fail('Order not found.',404)
   o=rows[0];self.own(o)
   if action:raise Fail('This historical order is read-only.',409)
   self.page(o['number'],'<section '+attrs(data_order_id=o['id'],data_order_status='complete' if o['status']=='shipped' else o['status'],data_total_cents=o['total_cents'])+'><h2>Historical order · '+esc(o['status'])+'</h2><p>Total '+money(o['total_cents'])+'</p>'+''.join('<p>'+esc(c.skus[l['sku_id']]['code'])+' · '+str(l['qty_units'])+' units · '+money(l['unit_price_cents'])+' per unit</p>' for l in c.db.q('select * from legacy_order_lines where order_id='+str(o['id'])+' order by line_no'))+'</section>');return
  self.own(o)
  if action and not self.post:raise Fail('Use the action form.',405)
  if action in ['/approve','/cancel','/pay'] or '/cancel-backorder' in action or action=='/rmas':self.require('buyer','approver')
  if action=='/approve':
   self.require('approver')
   if u['id']==o['creator']:raise Fail('You cannot approve your own order.',403)
   if o['status']!='awaiting_approval':raise Fail('Order is not awaiting approval.',409)
   o['status']='awaiting_payment';self.redirect('/orders/'+oid);return
  if action=='/cancel':
   if o['status'] not in ['awaiting_approval','awaiting_payment']:raise Fail('This order can no longer be cancelled.',409)
   if o['checkout']:
    checkoutpath='/v1/checkout/sessions/'+o['checkout']['id']
    current=provider(checkoutpath)
    if current['status']=='open':
     try:provider(checkoutpath+'/expire',{})
     except urllib.error.HTTPError:
      if provider(checkoutpath)['status']=='open':raise
   o['status']='cancelled';self.redirect('/orders/'+oid);return
  if action=='/pay':
   if o['status']!='awaiting_payment':raise Fail('Order is not awaiting payment.',409)
   checkout=o['checkout']
   if checkout:
    check=provider('/v1/checkout/sessions/'+checkout['id'])
    if check['status']!='open':checkout=None
   if not checkout:
    attempt=o.get('checkout_attempt',0)+1;o['checkout_attempt']=attempt
    base=E['PUBLIC_BASE_URL'].rstrip('/')+'/orders/'+oid
    checkout=provider('/v1/checkout/sessions',{'mode':'payment','line_items[0][price_data][currency]':'usd','line_items[0][price_data][unit_amount]':o['total_cents'],'line_items[0][price_data][product_data][name]':'Order '+o['number'],'line_items[0][quantity]':1,'customer_email':u['email'].strip().lower(),'metadata[order_id]':oid,'success_url':base+'?checkout=success','cancel_url':base+'?checkout=cancelled'},'checkout-'+oid+'-'+str(attempt));o['checkout']=checkout
   self.redirect(checkout['url']);return
  cm=re.fullmatch(r'/lines/(\d+)/cancel-backorder',action)
  if cm:
   l=next((l for l in o['lines'] if l['line_no']==int(cm[1])),None)
   if not l:raise Fail('Line not found.',404)
   n=integer(d.get('units'),1,l['backordered']);bef=c.due(o);l['backordered']-=n;l['cancelled']+=n;c.refund(o,bef-c.due(o));c.update(o);self.redirect('/orders/'+oid);return
  if action=='/ship':
   self.require('supervisor');c.inventory();ts=[t for t in c.tasks(o) if not t['shipped'] and t['units']]
   if not ts or any(t['status']=='open' for t in ts):raise Fail('Confirm every open pick before shipping. At least one picked unit is required.',409)
   sid=c.uid();lines=[]
   for l in o['lines']:
    n=sum(t['units'] for t in ts if t['line_no']==l['line_no'])
    if not n:continue
    tax=c.tax(o,l,l['shipped']+n)-c.tax(o,l,l['shipped']);lines.append(dict(line_no=l['line_no'],sku=c.skus[l['sku_id']]['code'],units=n,unit_price_cents=l['unit_price_cents'],amount_cents=n*l['unit_price_cents'],tax_cents=tax));l['shipped']+=n
   first=not any(x['order_id']==o['id'] for x in s['shipments'].values());fr=o['freight_cents'] if first else 0;ft=o['freight_tax_cents'] if first else 0
   sh=dict(id=sid,order_id=o['id'],tracking_number=f'PP{sid:010d}',invoice_number=f'INV-{sid:08d}',invoice_total_cents=sum(l['amount_cents']+l['tax_cents'] for l in lines)+fr+ft,freight_cents=fr,freight_tax_cents=ft,lines=lines)
   s['shipments'][str(sid)]=sh
   for t in ts:t['shipped']=True;c.movement(t['lot_id'],'ship',-t['units']);c.low(c.lots[t['lot_id']]['sku_id'])
   c.update(o);c.mail(o['creator'],'Shipped · '+o['number'],'Tracking number: '+sh['tracking_number']+'\nInvoice: '+sh['invoice_number']+'\nInvoice total: '+money(sh['invoice_total_cents']));self.redirect('/shipments/'+str(sid));return
  if action=='/rmas':
   ln=integer(d.get('line_no'),1,len(o['lines']));l=o['lines'][ln-1];used=sum(r['units'] for r in s['rmas'].values() if r['order_id']==o['id'] and r['line_no']==ln);n=integer(d.get('units'),1,l['shipped']-used);reason=textval(d.get('reason',''),1,500);rid=c.uid();s['rmas'][str(rid)]={'id':rid,'order_id':o['id'],'line_no':ln,'units':n,'reason':reason,'requester':u['id'],'status':'requested','disposition':'','credit':None};self.redirect('/rmas/'+str(rid));return
  if action:raise Fail('Action not found.',404)
  j=c.orderjson(o);buyer=role in ['buyer','approver'];body='<section '+attrs(data_order_id=o['id'],data_order_status=o['status'],data_total_cents=o['total_cents'])+'><span class="badge">'+esc(o['status'].replace('_',' '))+'</span><div class="statbar"><div><small>Order total</small><span class="metric">'+money(o['total_cents'])+'</span></div><div><small>Ship date</small>'+esc(o['ship_date'])+'</div><div><small>Purchase order</small>'+esc(o['po_number'] or '—')+'</div></div><p>'+esc(o['notes'])+'</p>'
  if o['status']=='awaiting_approval':
   body+='<p>Total '+money(o['total_cents'])+' exceeds the company approval limit of '+money(o['approval_limit'])+'. Approving lets the buyer pay.</p>'
   if role=='approver' and o['creator']!=u['id']:body+=self.form(p+'/approve','approve-order','Approve order')
  if buyer and o['status']=='awaiting_payment':body+=self.form(p+'/pay','pay-order','Pay '+money(o['total_cents']))
  if buyer and o['status'] in ['awaiting_approval','awaiting_payment']:body+=self.form(p+'/cancel','cancel-order','Cancel order')
  ts=[t for t in c.tasks(o) if not t['shipped'] and t['units']]
  if role=='supervisor' and ts and all(t['status']=='picked' for t in ts):body+=self.form(p+'/ship','ship-order','Ship order')
  body+='</section><h2>Order lines</h2>'
  for l in j['lines']:
   body+='<section '+attrs(data_line_no=l['line_no'],data_reserved=l['reserved'],data_shipped=l['shipped'],data_backordered=l['backordered'],data_cancelled=l['cancelled'])+'><h2>'+esc(l['sku'])+'</h2><p>'+str(l['qty'])+' units × '+money(l['unit_price_cents'])+' · Amount '+money(l['amount_cents'])+' · Tax '+money(l['tax_cents'])+'</p><div class="statbar">'+''.join('<div><small>'+k.capitalize()+'</small><b>'+str(l[k])+'</b></div>' for k in ['reserved','shipped','backordered','cancelled','returned'])+'</div>'
   if buyer and l['backordered']:body+='<p>Cancelled backordered units will be refunded to your card.</p>'+self.form(p+'/lines/'+str(l['line_no'])+'/cancel-backorder','cancel-backorder','Cancel backordered units',self.field('units'))
   used=sum(r['units'] for r in s['rmas'].values() if r['order_id']==o['id'] and r['line_no']==l['line_no'])
   if buyer and l['shipped']>used:body+=self.form(p+'/rmas','request-rma','Request return','<input type="hidden" name="line_no" value="'+str(l['line_no'])+'">'+self.field('units')+self.field('reason'))
   body+='</section>'
  body+='<section><h2>Order totals</h2>'+''.join('<p>'+k.replace('_cents','').replace('_',' ').capitalize()+': '+money(o[k])+'</p>' for k in ['subtotal_cents','tax_cents','freight_cents','freight_tax_cents','total_cents'])+'</section>'
  if j['refunds']:body+='<section><h2>Refunds</h2>'+''.join('<p>'+money(r['amount_cents'])+' · '+esc(r['status'])+'</p>' for r in j['refunds'])+'</section>'
  for sh in j['shipments']:body+='<p><a href="/shipments/'+str(sh['id'])+'">Shipment '+esc(sh['tracking_number'])+' · '+esc(sh['invoice_number'])+'</a></p>'
  for r in s['rmas'].values():
   if r['order_id']==o['id']:body+='<p><a href="/rmas/'+str(r['id'])+'">Return #'+str(r['id'])+' · '+esc(r['status'])+'</a></p>'
  self.page(o['number'],body)
 def waves(self):
  self.require('supervisor','picker');c=self.c;s=c.s;p=self.pathonly;d=self.d;u=self.u;c.inventory()
  if p=='/waves':
   if self.post:
    self.require('supervisor');zone=choice(d.get('zone'),['A','C','F']);ship=str(date(d.get('ship_date','')));n=integer(d.get('max_tasks'),1,12)
    ts=[t for t in s['tasks'].values() if t['status']=='open' and not t['wave_id'] and c.bins[c.lots[t['lot_id']]['bin_id']]['zone']==zone and s['orders'][str(t['order_id'])]['ship_date']==ship];ts.sort(key=lambda t:(t['order_id'],t['line_no'],c.lots[t['lot_id']]['expiry_date'],t['lot_id'],t['id']));ts=ts[:n]
    if not ts:raise Fail('No unassigned pick tasks match this zone and date.',409)
    bins={c.bins[c.lots[t['lot_id']]['bin_id']]['code']:c.bins[c.lots[t['lot_id']]['bin_id']] for t in ts};route,distance=routepath(bins);wid=c.uid();s['waves'][str(wid)]={'id':wid,'zone':zone,'ship_date':ship,'route':route,'distance':distance}
    for t in ts:t['wave_id']=wid
    self.redirect('/waves/'+str(wid));return
   body=self.form(p,'create-wave','Create wave',self.field('zone',options=['A','C','F'])+self.field('ship_date',c.today,kind='date')+self.field('max_tasks',12),True) if u['role']=='supervisor' else ''
   for w in reversed(list(s['waves'].values())):
    if u['role']=='supervisor' or w['zone']==u['zone']:body+='<section><a href="/waves/'+str(w['id'])+'">Wave #'+str(w['id'])+' · Zone '+esc(w['zone'])+'</a><p>'+esc(w['ship_date'])+' · '+str(w['distance'])+' distance units</p></section>'
   self.page('Pick waves',body);return
  m=re.fullmatch(r'/pick-tasks/(\d+)/confirm',p)
  if m:
   t=self.obj('tasks',m[1]);lot=c.lots[t['lot_id']];bin=c.bins[lot['bin_id']]
   if u['role']=='picker' and bin['zone']!=u['zone']:raise Fail('This pick belongs to another zone.',403)
   if not t['wave_id'] or t['status']!='open':raise Fail('This task is not open in a wave.',409)
   if code(d.get('bin_code',''))!=code(bin['code']):raise Fail('Wrong bin. Go to '+bin['code']+' and scan that bin before confirming again.',409)
   if code(d.get('lot_code',''))!=code(lot['lot_code']):raise Fail('Wrong lot. Check the lot label and scan '+lot['lot_code']+' before confirming again.',409)
   o=s['orders'][str(t['order_id'])];l=o['lines'][t['line_no']-1];n=integer(d.get('units'),0,t['units'])
   if not c.customers[o['customer_id']]['allow_split_cases'] and n%c.skus[l['sku_id']]['case_pack']:raise Fail('Pick a whole number of cases.')
   missing=t['units']-n;t['units']=n;t['status']='picked';bef=c.due(o)
   if missing:s['holds'][str(lot['id'])]=True;c.shortage(o,l,missing);c.refund(o,bef-c.due(o));c.update(o)
   c.low(lot['sku_id']);self.redirect('/waves/'+str(t['wave_id']));return
  m=re.fullmatch(r'/waves/(\d+)',p)
  if not m:raise Fail('Wave not found.',404)
  w=self.obj('waves',m[1])
  if u['role']=='picker' and w['zone']!=u['zone']:raise Fail('This wave belongs to another zone.',403)
  body='<section '+attrs(data_wave_id=w['id'],data_zone=w['zone'],data_ship_date=w['ship_date'],data_route_distance=w['distance'])+'><span class="badge">Zone '+esc(w['zone'])+'</span><h2>'+esc(w['ship_date'])+'</h2><p>Start and finish at the depot. Total route distance: <b>'+str(w['distance'])+'</b></p></section><div class="route">'
  for seq,bin in enumerate(w['route'],1):
   body+='<section '+attrs(data_stop_seq=seq,data_bin=bin)+'><div class="eyebrow">Stop '+str(seq)+'</div><h2>'+esc(bin)+'</h2>'
   for t in s['tasks'].values():
    if t['wave_id']!=w['id'] or c.bins[c.lots[t['lot_id']]['bin_id']]['code']!=bin:continue
    lot=c.lots[t['lot_id']];sk=c.skus[lot['sku_id']];body+='<div '+attrs(data_task_id=t['id'],data_order_id=t['order_id'],data_line_no=t['line_no'],data_lot_id=t['lot_id'],data_units=t['units'],data_task_status=t['status'])+'><h3>'+esc(sk['code'])+' · '+str(t['units'])+' units</h3><p>Lot '+esc(lot['lot_code'])+' · '+esc(t['status'])+'</p>'
    if t['status']=='open':body+=self.form('/pick-tasks/'+str(t['id'])+'/confirm','confirm-pick','Confirm pick',self.field('bin_code')+self.field('lot_code')+self.field('units'))
    body+='</div>'
   body+='</section>'
  self.page('Wave #'+str(w['id']),body+'</div>')
 def fulfilment(self):
  self.require('buyer','approver','supervisor','finance');c=self.c;s=c.s;p=self.pathonly;d=self.d;role=self.u['role'];m=re.fullmatch(r'/shipments/(\d+)',p)
  if m:
   sh=self.obj('shipments',m[1]);o=s['orders'][str(sh['order_id'])];self.own(o);body='<section '+attrs(data_shipment_id=sh['id'],data_order_id=o['id'],data_tracking_number=sh['tracking_number'],data_invoice_number=sh['invoice_number'],data_invoice_total_cents=sh['invoice_total_cents'],data_freight_cents=sh['freight_cents'],data_freight_tax_cents=sh['freight_tax_cents'])+'><p>Order <a href="/orders/'+str(o['id'])+'">'+esc(o['number'])+'</a></p><h2>Tracking '+esc(sh['tracking_number'])+'</h2>'
   for l in sh['lines']:body+='<section '+attrs(data_invoice_line_no=l['line_no'],data_units=l['units'],data_amount_cents=l['amount_cents'],data_tax_cents=l['tax_cents'])+'><b>'+esc(l['sku'])+'</b><p>'+str(l['units'])+' units · Unit price '+money(l['unit_price_cents'])+' · Amount '+money(l['amount_cents'])+' · Tax '+money(l['tax_cents'])+'</p></section>'
   body+='<p>Freight '+money(sh['freight_cents'])+' · Freight tax '+money(sh['freight_tax_cents'])+'</p><h2>Total '+money(sh['invoice_total_cents'])+'</h2></section>';self.page('Invoice '+sh['invoice_number'],body);return
  m=re.fullmatch(r'/rmas/(\d+)(/receive|/inspect)?',p)
  if not m:raise Fail('Return not found.',404)
  r=self.obj('rmas',m[1]);o=s['orders'][str(r['order_id'])];self.own(o);l=o['lines'][r['line_no']-1]
  if m[2]:
   self.require('supervisor')
   if not self.post:raise Fail('Use the action form.',405)
   if m[2]=='/receive':
    if r['status']!='requested':raise Fail('This return is not awaiting receipt.',409)
    n=l['shipped']-l['returned'];credit=r['units']*l['unit_price_cents']+c.tax(o,l,n)-c.tax(o,l,n-r['units']);r['credit']=credit;r['credit_id']=c.uid();l['returned']+=r['units'];r['status']='quarantined';c.mail(r['requester'],'Credit note · '+o['number'],'Credit amount: '+money(credit))
   else:
    if r['status']!='quarantined':raise Fail('Receive the return before inspection.',409)
    outcome=choice(d.get('outcome'),['restock','damaged']);c.inventory();lots=[c.lots[t['lot_id']] for t in c.tasks(o) if t['line_no']==r['line_no'] and t['shipped'] and t['units']];lot=min(lots,key=lambda x:(x['expiry_date'],x['id']));r['status']='closed';r['disposition']='written_off'
    if outcome=='restock' and date(lot['expiry_date'])>=c.today+dt.timedelta(days=7):r['disposition']='restocked';c.movement(lot['id'],'restock',r['units']);c.fill(lot['sku_id']);c.low(lot['sku_id'])
   self.redirect('/rmas/'+str(r['id']));return
  body='<section '+attrs(data_rma_id=r['id'],data_rma_status=r['status'],data_disposition=r['disposition'],data_credit_cents=r['credit'] if r['credit'] is not None else '')+'><span class="badge">'+esc(r['status'])+'</span><p><a href="/orders/'+str(o['id'])+'">'+esc(o['number'])+'</a> · '+esc(c.skus[l['sku_id']]['code'])+' · '+str(r['units'])+' units</p><p>'+esc(r['reason'])+'</p>'
  if r['credit'] is not None:body+='<h2>Credit note #'+str(r['credit_id'])+'</h2><p>'+money(r['credit'])+'</p>'
  if r['status']=='quarantined':body+='<p>Location: QA-01 · Quarantine</p>'
  if r['disposition']:body+='<p>'+esc(r['disposition'])+'</p>'
  if role=='supervisor' and r['status']=='requested':body+=self.form(p+'/receive','receive-rma','Receive return')
  if role=='supervisor' and r['status']=='quarantined':body+=self.form(p+'/inspect','inspect-rma','Complete inspection',self.field('outcome',options=['restock','damaged']))
  self.page('Return #'+str(r['id']),body+'</section>')
 def reports(self):
  c=self.c;s=c.s;p=self.pathonly;u=self.u;rows=[]
  if p=='/orders/export.csv':
   self.require('buyer','approver','finance');header=['number','customer_id','customer_name','placed_at','status','po_number','ship_date','total_cents'];orders=[]
   for o in c.db.q('select * from legacy_orders'):
    placed=stamp(dt.datetime.fromisoformat(o['placed_local']).replace(tzinfo=c.tz,fold=o['fold']));orders.append({**o,'placed_at':placed})
   orders+=list(s['orders'].values())
   for o in sorted(orders,key=lambda o:(o['placed_at'],o['number'])):
    if u['role'] in ['buyer','approver'] and o['customer_id']!=u['customer_id']:continue
    rows.append([o['number'],o['customer_id'],c.customers[o['customer_id']]['name'],o['placed_at'],o['status'],o['po_number'] or '',o['ship_date'],o['total_cents']])
  elif p=='/reports/on-hand.csv':
   self.require('supervisor','finance');c.inventory();header=['lot_id','sku','lot_code','bin','expiry_date','on_hand','reserved','available','status'];reserved={}
   for t in s['tasks'].values():
    if not t['shipped']:reserved[t['lot_id']]=reserved.get(t['lot_id'],0)+t['units']
   for l in sorted(c.lots.values(),key=lambda l:l['id']):
    n=c.stock.get(l['id'],0)
    if n<=0:continue
    status=c.statuslot(l);r=reserved.get(l['id'],0);rows.append([l['id'],c.skus[l['sku_id']]['code'],l['lot_code'],c.bins[l['bin_id']]['code'],l['expiry_date'],n,r,n-r if status=='available' else 0,status])
  elif p=='/reports/ledger.csv':
   self.require('supervisor','finance');sk=next((x for x in c.skus.values() if x['code']==code(self.query.get('sku',[''])[0])),None)
   if not sk:raise Fail('Unknown SKU.',400)
   header=['lot_id','kind','units','at']
   for r in c.db.q('select m.* from pp_movements m join lots l on l.id=m.lot_id where l.sku_id='+str(sk['id'])+' order by m.at,m.id'):rows.append([r['lot_id'],r['kind'],r['units'],stamp(dt.datetime.fromisoformat(r['at']))])
  elif p=='/reports/valuation.csv':
   self.require('finance');x=self.query.get('as_of',[''])[0]
   try:
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})',x):raise ValueError()
    at=dt.datetime.fromisoformat(x.replace('Z','+00:00'))
   except:raise Fail('as_of must be an RFC 3339 instant with a time zone.',400)
   header=['sku','on_hand','fifo_value_cents'];ms=c.db.q('select l.sku_id,l.unit_cost,m.units from pp_movements m join lots l on l.id=m.lot_id where m.at<='+sql(at.isoformat())+' order by m.at desc,m.id desc');tot={};positive={}
   for m in ms:
    sid=m['sku_id'];tot[sid]=tot.get(sid,0)+m['units']
    if m['units']>0:positive.setdefault(sid,[]).append(m)
   for sid in sorted(tot,key=lambda i:c.skus[i]['code']):
    n=tot[sid]
    if n<=0:continue
    left=n;value=Decimal(0)
    for m in positive.get(sid,[]):
     take=min(left,m['units']);value+=take*Decimal(m['unit_cost'])*100;left-=take
     if not left:break
    rows.append([c.skus[sid]['code'],n,rnd(value)])
  else:raise Fail('Report not found.',404)
  out=io.StringIO(newline='');w=csv.writer(out,lineterminator='\r\n');w.writerow(header)
  for row in rows:w.writerow(["'"+v if isinstance(v,str) and v and v[0] in '=+-@\t\r' else v for v in row])
  self.result=(out.getvalue(),200,'text/csv; charset=utf-8',None)

def deliver(m):
 message=EmailMessage();message['From']=E['MAIL_FROM'];message['To']=formataddr((m['name'],m['to']));message['Subject']=m['subject'];message.set_content(m['body'])
 with smtplib.SMTP(E['SMTP_HOST'],int(E['SMTP_PORT']),timeout=5) as smtp:smtp.send_message(message)

def worker():
 registered=False
 while True:
  db=None
  try:
   if not registered:
    provider('/v1/webhook_endpoints',{'url':E['PUBLIC_BASE_URL'].rstrip('/')+'/webhooks/payments'});registered=True
   db=DB();db.q('BEGIN');s=json.loads(db.q('select data from pp_state where id=1 for update')[0]['data']);send=[];refunds=[]
   for m in s['emails']:
    if not m['sent']:m['sent']=True;send.append(m)
   for r in s['refunds'].values():
    if r['status']=='pending' and not r['provider_id'] and time.time()-r.get('attempt',0)>=3:
     r['attempt']=time.time();refunds.append(dict(r))
   db.q('update pp_state set data='+sql(json.dumps(s))+'::jsonb where id=1');db.q('COMMIT');db.close();db=None
   for m in send:
    try:deliver(m)
    except:traceback.print_exc()
   for r in refunds:
    try:
     v=provider('/v1/refunds',{'payment_intent':r['pi'],'amount':r['amount_cents'],'metadata[order_id]':r['order_id'],'metadata[refund_id]':r['id']},r['key'])
     db=DB();db.q('BEGIN');s=json.loads(db.q('select data from pp_state where id=1 for update')[0]['data']);s['refunds'][str(r['id'])]['provider_id']=v['id'];db.q('update pp_state set data='+sql(json.dumps(s))+'::jsonb where id=1');db.q('COMMIT');db.close();db=None
    except Exception:
     if db:db.close();db=None
     print('Refund retry scheduled',r['id'],flush=True)
  except:traceback.print_exc()
  finally:
   if db:db.close()
  time.sleep(1)

if __name__=='__main__':
 init();threading.Thread(target=worker,daemon=True).start();server=ThreadingHTTPServer(('0.0.0.0',int(E['PORT'])),Handler);server.daemon_threads=True;print('Pickpath ready',flush=True);server.serve_forever()
