import os,json,subprocess,threading,time,datetime as D,zoneinfo,secrets,hashlib,hmac,base64,re,copy,html,urllib.parse as U,urllib.request as Q,smtplib,csv,io,traceback
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from email.message import EmailMessage
from email.utils import formataddr
E=os.environ
BASE=E['PUBLIC_BASE_URL'].rstrip('/')
LOCK=threading.RLock(); S={}
TCACHE={}; SCACHE={}; OCACHE={}; LAST_SAVED=None
def clearcache(): TCACHE.clear(); SCACHE.clear(); OCACHE.clear()
def eventorders(e):
 if e['id'] not in OCACHE: OCACHE[e['id']]=[o for o in S['orders'].values() if o['event_id']==e['id']]
 return OCACHE[e['id']]
def now(): return int(time.time())
def iso(t): return D.datetime.fromtimestamp(t,D.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') if t is not None else ''
def norm(s): return str(s).strip().lower()
def esc(s): return html.escape(str(s),quote=True)
def sha(s): return hashlib.sha256(s.encode()).hexdigest()
def password(p,salt=None):
 salt=salt or secrets.token_hex(16)
 return salt+'$'+hashlib.pbkdf2_hmac('sha256',p.encode(),salt.encode(),180000).hex()
def pwcheck(p,v): return bool(v) and hmac.compare_digest(password(p,v.split('$')[0]),v)
def db(sql):
 p=subprocess.run([os.path.join(os.path.dirname(__file__),'runtime','psql'),E['DATABASE_URL'],'-X','-qAt','-v','ON_ERROR_STOP=1',],input=sql,capture_output=True,text=True,timeout=4,env={**E,'PGCONNECT_TIMEOUT':'2'})
 if p.returncode: raise RuntimeError('Database unavailable')
 return p.stdout.strip()
def save():
 global LAST_SAVED
 raw=json.dumps(S,separators=(',',':'))
 if raw==LAST_SAVED: return
 v=raw.replace("'","''")
 db("UPDATE seatwise_state SET data='"+v+"'::jsonb WHERE id=1")
 LAST_SAVED=raw
def local(s,tz,fold=0):
 d=D.datetime.fromisoformat(str(s)); z=zoneinfo.ZoneInfo(tz); a=d.replace(tzinfo=z,fold=fold)
 if a.astimezone(D.timezone.utc).astimezone(z).replace(tzinfo=None)!=d: raise ValueError('This local time does not exist because of daylight saving time.')
 return int(a.timestamp())
def display(t,e): return D.datetime.fromtimestamp(t,zoneinfo.ZoneInfo(S['venues'][str(e['venue_id'])]['timezone'])).strftime('%b %d, %Y · %I:%M %p')+' '+S['venues'][str(e['venue_id'])]['timezone']
def init():
 global S
 db('CREATE TABLE IF NOT EXISTS seatwise_state (id integer PRIMARY KEY, data jsonb NOT NULL)')
 raw=db('SELECT data FROM seatwise_state WHERE id=1')
 if raw: S=json.loads(raw); return
 names=['organizations','users','memberships','venues','sections','seats','events','event_prices','legacy_orders','legacy_tickets','legacy_transfers']
 old={n:json.loads(db('SELECT coalesce(json_agg(t),\'[]\') FROM '+n+' t')) for n in names}
 S={k:{} for k in ['orgs','users','venues','sections','seats','events','orders','tickets','sessions','links','invites','codes','devices','wait','offers','refunds','scans','reminders','emails']}; S['members']=old['memberships']; S['next']=1000000
 for tab,key in [('organizations','orgs'),('users','users'),('venues','venues'),('sections','sections'),('seats','seats')]:
  for v in old[tab]: S[key][str(v['id'])]=v
 for u in S['users'].values(): u.update(email=norm(u['email']),verified=False,password=None,legacy=True)
 for seat in S['seats'].values(): seat['label']=S['sections'][str(seat['section_id'])]['code']+'-'+seat['row_label']+'-'+str(seat['seat_number'])
 for e in old['events']:
  tz=S['venues'][str(e['venue_id'])]['timezone']; e.update(start=local(e['starts_local'],tz,e['starts_fold']),on_sale=local(e['on_sale_local'],tz,e['on_sale_fold']) if e['on_sale_local'] else 0,off_sale=None,presale=None,presale_code='',legacy=True,prices={str(p['section_id']):p['price_cents'] for p in old['event_prices'] if p['event_id']==e['id']},reschedules=[])
  S['events'][str(e['id'])]=e
 for o in old['legacy_orders']:
  e=S['events'][str(o['event_id'])]; o.update(status='paid',buyer_email=norm(o['buyer_email']),paid_at=local(o['placed_local'],S['venues'][str(e['venue_id'])]['timezone'],o['placed_fold']),legacy=True,items=[],codes=[],expires=0,payment=None,checkout=None)
  S['orders'][str(o['id'])]=o
 for t in old['legacy_tickets']:
  o=S['orders'][str(t['order_id'])]; e=S['events'][str(o['event_id'])]; tz=S['venues'][str(e['venue_id'])]['timezone']
  t.update(event_id=e['id'],holder_email=norm(t['holder_email']),status='valid' if t['status']=='comp' else t['status'],legacy=True,token=t['barcode'],old_tokens=[],transferred=any(x['ticket_id']==t['id'] for x in old['legacy_transfers']),admission={'time':local(t['checked_in_local'],tz,t['checked_in_fold']),'gate':t['checked_in_gate'],'key':'legacy'} if t['checked_in_local'] else None)
  S['tickets'][str(t['id'])]=t; o['items'].append({k:t[k] for k in ['seat_id','face_cents','discount_cents','service_fee_cents','facility_fee_cents','tax_cents','total_cents']})
 db("INSERT INTO seatwise_state VALUES (1,'"+json.dumps(S).replace("'","''")+"'::jsonb) ON CONFLICT DO NOTHING")
def ident(): S['next']+=1; return S['next']
def name(v,maxlen=100):
 if not 1<=len(v)<=maxlen or any(ord(c)<32 for c in v): raise ValueError('Enter a name of 1 to '+str(maxlen)+' characters without control characters.')
 return v
def email(v):
 v=norm(v)
 if not re.fullmatch(r'[^\s@<>\x00-\x1f]+@[^\s@<>\x00-\x1f]+',v): raise ValueError('Enter a valid email address.')
 return v
def integer(v,minimum=0,maximum=100000000):
 try: n=int(v)
 except: raise ValueError('Enter a whole number.')
 if not minimum<=n<=maximum: raise ValueError('Number is outside the allowed range.')
 return n
def money(n): return '${:,.2f}'.format(n/100)
def seal(value):
 nonce=secrets.token_bytes(16); data=value.encode(); key=E['TICKET_SIGNING_KEY'].encode()
 stream=b''.join(hmac.new(key,b'email:'+nonce+i.to_bytes(4,'big'),hashlib.sha256).digest() for i in range((len(data)+31)//32))
 cipher=bytes(a^b for a,b in zip(data,stream)); tag=hmac.new(key,nonce+cipher,hashlib.sha256).digest()
 return base64.b64encode(nonce+tag+cipher).decode()
def unseal(value):
 data=base64.b64decode(value); nonce,tag,cipher=data[:16],data[16:48],data[48:]; key=E['TICKET_SIGNING_KEY'].encode()
 if not hmac.compare_digest(tag,hmac.new(key,nonce+cipher,hashlib.sha256).digest()): raise ValueError('Invalid email data')
 stream=b''.join(hmac.new(key,b'email:'+nonce+i.to_bytes(4,'big'),hashlib.sha256).digest() for i in range((len(cipher)+31)//32))
 return bytes(a^b for a,b in zip(cipher,stream)).decode()
def mail(key,to,n,subject,body):
 if key not in S['emails']: S['emails'][key]={'to':norm(to),'name':n,'subject':subject,'sealed_body':seal(body),'sent':False}
def tokenlink(kind,u):
 for l in S['links'].values():
  if l['user']==u['id'] and l['kind']==kind: l['active']=False
 tok=secrets.token_urlsafe(32); S['links'][sha(tok)]={'user':u['id'],'kind':kind,'expires':now()+int(E['ACCOUNT_LINK_TTL_SECONDS']),'active':True}
 mail('link:'+sha(tok),u['email'],u['full_name'],'Verify your address' if kind=='verify' else 'Reset your password',BASE+'/'+('verify/' if kind=='verify' else 'reset/')+tok)
def membership(uid,oid): return next((m for m in S['members'] if m['user_id']==uid and str(m['organization_id'])==str(oid)),None)
def seats(e):
 if e['venue_id'] not in SCACHE: SCACHE[e['venue_id']]=sorted([s for s in S['seats'].values() if S['sections'][str(s['section_id'])]['venue_id']==e['venue_id']],key=lambda s:(S['sections'][str(s['section_id'])]['code'],s['row_label'],s['seat_number']))
 return SCACHE[e['venue_id']]
def tickets(e):
 if e['id'] not in TCACHE: TCACHE[e['id']]=sorted([t for t in S['tickets'].values() if t['event_id']==e['id']],key=lambda t:(seatkey(t['seat_id']),t['id']))
 return TCACHE[e['id']]
def seatkey(sid):
 s=S['seats'][str(sid)]; return (S['sections'][str(s['section_id'])]['code'],s['row_label'],s['seat_number'])
def label(t): return S['seats'][str(t['seat_id'])]['label']
def sale(e):
 t=now()
 if e['status']=='cancelled': return 'cancelled'
 if t>= (e['off_sale'] if e['off_sale'] is not None else e['start']): return 'ended'
 if t>=e['on_sale']: return 'on_sale'
 if e['presale'] is not None and t>=e['presale']: return 'presale'
 return 'not_on_sale'
def seatstatus(e,s,exclude=None,reserve=True):
 if any(t['seat_id']==s['id'] and t['status'] in ['valid','refund_pending'] for t in tickets(e)): return 'sold'
 if any(o['event_id']==e['id'] and str(o['id'])!=str(exclude) and o['status']=='pending' and o['expires']>now() and any(i['seat_id']==s['id'] for i in o['items']) for o in eventorders(e)): return 'held'
 if any(o['event_id']==e['id'] and o['status']=='open' and o['expires']>now() and s['id'] in o['seats'] for o in S['offers'].values()): return 'held'
 if reserve and not s['accessible'] and any(w['event_id']==e['id'] and w['section_id']==s['section_id'] and w['status']=='waiting' for w in S['wait'].values()): return 'waitlist'
 return 'available'
def buyer_count(e,addr):
 c=sum(t['status']!='refunded' for t in tickets(e) if S['orders'][str(t['order_id'])]['buyer_email']==addr)
 return c+sum(len(o['items']) for o in S['orders'].values() if o['event_id']==e['id'] and o['buyer_email']==addr and o['status']=='pending' and o['expires']>now())
def allocate(a,weights):
 total=sum(weights)
 if not total: return [0]*len(weights)
 v=[a*w//total for w in weights]
 for i in sorted(range(len(v)),key=lambda i:(-(a*weights[i]%total),i))[:a-sum(v)]: v[i]+=1
 return v
def rounddiv(a,b): return (2*a+b)//(2*b)
def price(o):
 e=S['events'][str(o['event_id'])]; items=o['items']; face=[i['face_cents'] for i in items]; subtotal=sum(face); discount=0
 codes=[S['codes'][str(c)] for c in o['codes']]
 for c in codes:
  if c['kind']=='percent': discount=rounddiv(subtotal*c['value'],100)
 for c in codes:
  if c['kind']=='fixed': discount+=min(c['value'],subtotal-discount)
 shares=allocate(discount,face); fees=[rounddiv(f*75,1000) for f in face]; taxable=[f-d+s for f,d,s in zip(face,shares,fees)]; taxes=allocate(rounddiv(sum(taxable)*8875,100000),taxable)
 for i,d,f,t in zip(items,shares,fees,taxes): i.update(discount_cents=d,service_fee_cents=f,facility_fee_cents=e['facility_fee_cents'],tax_cents=t,total_cents=i['face_cents']-d+f+e['facility_fee_cents']+t)
 o['total_cents']=sum(i['total_cents'] for i in items)
def neworder(e,u,ss):
 if buyer_count(e,u['email'])+len(ss)>6: raise ValueError('Per-buyer limit is 6 tickets for this event.')
 oid=ident(); o={'id':oid,'event_id':e['id'],'buyer_email':u['email'],'buyer_name':u['full_name'],'status':'pending','expires':now()+int(E['HOLD_TTL_SECONDS']),'items':[{'seat_id':s['id'],'face_cents':e['prices'][str(s['section_id'])]} for s in sorted(ss,key=lambda s:seatkey(s['id']))],'codes':[],'checkout':None,'payment':None,'legacy':False,'paid_at':None}; price(o); S['orders'][str(oid)]=o; OCACHE.pop(e['id'],None); return o
def qr(t):
 payload=str(t['id'])+'.'+secrets.token_hex(16)
 return payload+'.'+hmac.new(E['TICKET_SIGNING_KEY'].encode(),payload.encode(),hashlib.sha256).hexdigest()
def provider(path,data=None,key=None):
 req=Q.Request(E['PAYMENTS_API_URL'].rstrip('/')+path,data=U.urlencode(data).encode() if data is not None else None,headers={'Idempotency-Key':key} if key else {})
 with Q.urlopen(req,timeout=3) as r: return json.load(r)
def refund(t,amount,cancel=False):
 rid=str(ident()); S['refunds'][rid]={'id':rid,'ticket_id':t['id'],'order_id':t['order_id'],'amount':amount,'key':'refund-'+rid,'provider_id':None,'complete':False,'cancel':cancel,'last_try':0}; t['status']='refund_pending'
def deadlines(t):
 e=S['events'][str(t['event_id'])]; o=S['orders'][str(t['order_id'])]; special=any(r>=o['paid_at'] for r in e['reschedules']) if o['paid_at'] else False
 a=e['start'] if special else e['start']-168*3600; b=e['start'] if special else e['start']-48*3600
 percent=100 if now()<=a else 50 if now()<=b else 0
 if now()>=e['start']: percent=0
 return a,b,percent
def work():
 clearcache()
 t=now()
 for o in list(S['orders'].values()):
  if o['status']=='pending' and o['expires']<=t: o['status']='expired'
  if o['status'] in ['expired','released'] and o.get('checkout') and not o.get('checkout_expired'):
   try: provider('/v1/checkout/sessions/'+o['checkout']['id']+'/expire',{}); o['checkout_expired']=True
   except: pass
 for of in S['offers'].values():
  if of['status']=='open' and of['expires']<=t: of['status']='expired'; S['wait'][str(of['wait_id'])]['status']='expired'
 for e in S['events'].values():
  if e['status']!='published' or sale(e) not in ['on_sale','presale'] or not any(w['event_id']==e['id'] and w['status']=='waiting' for w in S['wait'].values()): continue
  for sec in [s for s in S['sections'].values() if s['venue_id']==e['venue_id']]:
   free=[s for s in seats(e) if s['section_id']==sec['id'] and not s['accessible'] and seatstatus(e,s,reserve=False)=='available']
   for w in sorted(S['wait'].values(),key=lambda w:w['id']):
    if w['event_id']!=e['id'] or w['section_id']!=sec['id'] or w['status']!='waiting' or w['quantity']>len(free): continue
    tok=secrets.token_urlsafe(32); of={'id':ident(),'token':tok,'wait_id':w['id'],'event_id':e['id'],'email':w['email'],'seats':[s['id'] for s in free[:w['quantity']]],'status':'open','expires':t+int(E['WAITLIST_OFFER_TTL_SECONDS'])}; free=free[w['quantity']:]; w['status']='offered'; S['offers'][sha(tok)]=of
    mail('offer:'+str(of['id']),w['email'],w['name'],'Seats available',BASE+'/waitlist/offers/'+tok)
 for r in S['refunds'].values():
  if r['provider_id'] or t-r['last_try']<3: continue
  r['last_try']=t; o=S['orders'][str(r['order_id'])]
  try:
   data={'payment_intent':o['payment'],'amount':r['amount']}
   if r.get('ticket_id'): data['metadata[ticket_id]']=r['ticket_id']
   result=provider('/v1/refunds',data,r['key']); r['provider_id']=result['id']
  except: pass
 save()
 for m in S['emails'].values():
  if m['sent']: continue
  msg=EmailMessage(); msg['From']=E['MAIL_FROM']; msg['To']=formataddr((m['name'],m['to'])); msg['Subject']=m['subject']; msg.set_content(unseal(m['sealed_body']) if 'sealed_body' in m else m['body'])
  try:
   with smtplib.SMTP(E['SMTP_HOST'],int(E['SMTP_PORT']),timeout=3) as smtp: smtp.send_message(msg)
   m['sent']=True; m.pop('sealed_body',None); m.pop('body',None); save()
  except: pass
class HTTPError(Exception):
 def __init__(self,code,message): self.code=code; self.message=message
class Redirect(Exception):
 def __init__(self,path): self.path=path
class App(BaseHTTPRequestHandler):
 def log_message(self,*a): pass
 def do_GET(self): self.run()
 def do_POST(self): self.run()
 def output(self,body,code=200,ctype='text/html; charset=utf-8',headers={}):
  self.send_response(code); self.send_header('Content-Type',ctype); self.send_header('X-Frame-Options','DENY'); self.send_header('Cache-Control','no-store')
  for k,v in headers.items(): self.send_header(k,v)
  if getattr(self,'newcookie',None): self.send_header('Set-Cookie','seatwise='+self.newcookie+'; Path=/; HttpOnly; SameSite=Lax')
  self.end_headers(); self.wfile.write(body.encode() if isinstance(body,str) else body)
 def page(self,title,body):
  nav='<a class="brand" href="/">seatwise<span>●</span></a><a href="/">Explore events</a>'
  if self.user:
   nav+='<a href="/tickets">My tickets</a><a href="/orgs/new">Organizations</a>'+self.form('/signout','signout','Sign out')
  else: nav+='<a href="/signin">Sign in</a><a href="/signup">Create account</a>'
  return '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+esc(title)+' · Seatwise</title><link rel="stylesheet" href="/style.css"></head><body><nav>'+nav+'</nav><main><p class="eyebrow">LIVE & IN PERSON</p><h1>'+esc(title)+'</h1>'+body+'</main><footer>Seatwise · Good nights start here.</footer></body></html>'
 def field(self,n,label=None,typ='text',value='',required=True): return '<label>'+esc(label or n.replace('_',' ').capitalize())+'<input name="'+esc(n)+'" type="'+typ+'" value="'+esc(value)+'" '+('required' if required else '')+'></label>'
 def form(self,path,action,text,fields='',hidden=''):
  return '<form method="post" action="'+esc(path)+'"><input type="hidden" name="csrf" data-csrf value="'+esc(self.session['csrf'])+'">'+hidden+fields+'<button data-action="'+action+'">'+esc(text)+'</button></form>'
 def needuser(self):
  if not self.user: raise Redirect('/signin?next='+U.quote(self.path,safe=''))
 def org(self,oid,roles=None):
  self.needuser(); m=membership(self.user['id'],oid)
  if not m or roles and m['role'] not in roles: raise HTTPError(403,'You do not have permission for this organization.')
  return m
 def eventorg(self,oid,eid,roles=None):
  m=self.org(oid,roles); e=S['events'].get(str(eid))
  if not e or str(e['organization_id'])!=str(oid): raise HTTPError(404,'Event not found.')
  return e,m
 def get(self,key,collection):
  v=S[collection].get(str(key))
  if not v: raise HTTPError(404,'Not found.')
  return v
 def run(self):
  self.newcookie=None; self.user=None
  if self.path.split('?')[0]=='/healthz':
   try: db('SELECT 1'); self.output('OK',ctype='text/plain')
   except: self.output('Database unavailable',503,'text/plain')
   return
  with LOCK:
   clearcache()
   snapshot=None
   try:
    path=U.urlsplit(self.path).path; self.pathonly=path; self.query=U.parse_qs(U.urlsplit(self.path).query); self.post=self.command=='POST'
    sid=next((x.strip().split('=',1)[1] for x in self.headers.get('Cookie','').split(';') if x.strip().startswith('seatwise=')),None)
    self.session=S['sessions'].get(sha(sid)) if sid else None
    if not self.session:
     sid=secrets.token_urlsafe(32); self.newcookie=sid; self.session={'csrf':secrets.token_urlsafe(32),'user':None}; S['sessions'][sha(sid)]=self.session
    self.user=S['users'].get(str(self.session['user'])) if self.session['user'] else None
    length=int(self.headers.get('Content-Length','0'))
    if length>2000000: raise HTTPError(400,'Request too large.')
    self.raw=self.rfile.read(length) if length else b''; self.f={k:v[-1] for k,v in U.parse_qs(self.raw.decode(),keep_blank_values=True).items()} if self.post and path not in ['/api/scan','/api/scan/batch','/webhooks/payments'] else {}
    self.multi=U.parse_qs(self.raw.decode(),keep_blank_values=True) if self.f else {}
    if self.post and path not in ['/api/scan','/api/scan/batch','/webhooks/payments'] and not hmac.compare_digest(self.f.get('csrf',''),self.session['csrf']): raise HTTPError(403,'Invalid form token. Reload the page and try again.')
    snapshot=copy.deepcopy(S); self.checkpoint=None
    body=self.route(path)
    save()
    if isinstance(body,tuple): self.output(*body)
    else: self.output(body)
   except Redirect as r: save(); self.output('',303,headers={'Location':r.path})
   except (ValueError,KeyError,UnicodeError) as ex:
    if snapshot is not None: S.clear(); S.update(getattr(self,'checkpoint',None) or snapshot)
    self.output(self.page('Please check your request','<p>'+esc(str(ex) if isinstance(ex,ValueError) else 'Missing or invalid field.')+'</p><a href="/">Go back</a>'),400)
   except HTTPError as ex:
    if snapshot is not None: S.clear(); S.update(getattr(self,'checkpoint',None) or snapshot)
    self.output(self.page('Unable to continue','<p>'+esc(ex.message)+'</p>'),ex.code)
   except Exception:
    if getattr(self,'checkpoint',None) is not None: snapshot=self.checkpoint
    traceback.print_exc()
    if snapshot is not None: S.clear(); S.update(getattr(self,'checkpoint',None) or snapshot)
    self.output(self.page('Unable to continue','<p>Please try again shortly.</p>'),500)
 def route(self,p):
  f=self.f; F=self.field; form=self.form
  if p=='/style.css': return (CSS,200,'text/css')
  if p=='/webhooks/payments': return self.webhook()
  if p in ['/api/scan','/api/scan/batch']: return self.scanapi(p)
  if p=='/':
   b='<div class="hero"><p class="lead">Find your seat.<br>Feel the room.</p><p>Unforgettable shows. Independent venues. A place for you.</p></div><h2>Coming to a stage near you</h2><div class="grid">'
   for e in S['events'].values():
    if e['status']=='published': b+='<a class="card event" href="/events/'+str(e['id'])+'"><div class="art">✦</div><p class="eyebrow">'+esc(S['venues'][str(e['venue_id'])]['name'])+'</p><h2>'+esc(e['name'])+'</h2><p>'+esc(display(e['start'],e))+'</p><span>Choose your seats ↗</span></a>'
   b+='</div>'
   if self.user:
    b+='<h2>Your organizations</h2>'
    for m in S['members']:
     if m['user_id']==self.user['id']: b+='<p><a href="/orgs/'+str(m['organization_id'])+'">'+esc(S['orgs'][str(m['organization_id'])]['name'])+'</a> · '+esc(m['role'])+'</p>'
   return self.page('A great night is waiting.',b)
  if p in ['/signup','/signin','/password-reset']:
   title={'/signup':'Create your account','/signin':'Welcome back','/password-reset':'Reset your password'}[p]
   if self.post:
    addr=email(f.get('email','')); u=next((u for u in S['users'].values() if u['email']==addr),None)
    if p=='/password-reset':
     if u and (u['verified'] or any(m['user_id']==u['id'] for m in S['members'])): tokenlink('reset',u)
     return self.page(title,'<p>If this address is eligible, a reset link is on its way.</p>')
    if p=='/signup':
     full=name(f['full_name']); pw=f['password']
     if len(pw)<10: raise ValueError('Password must contain at least 10 characters.')
     if u and (u['verified'] or any(m['user_id']==u['id'] for m in S['members'])): raise ValueError('This address already belongs to an account or organization member. Sign in or reset your password.')
     if not u: u={'id':ident(),'email':addr}; S['users'][str(u['id'])]=u
     u.update(full_name=full,password=password(pw),verified=False); tokenlink('verify',u)
     return self.page('Check your email','<p>Follow the verification link to verify your address before signing in.</p>')
    if not u or not pwcheck(f.get('password',''),u.get('password')): raise ValueError('Email or password is incorrect.')
    if not u['verified']: raise ValueError('Please verify your address first.')
    if sid:=next((k for k,v in S['sessions'].items() if v is self.session),None): del S['sessions'][sid]
    sid=secrets.token_urlsafe(32); self.newcookie=sid; S['sessions'][sha(sid)]={'user':u['id'],'csrf':secrets.token_urlsafe(32)}
    nxt=self.query.get('next',['/tickets'])[0]
    if not re.match(r'^/(?![/\\])',nxt) or '\\' in nxt or any(ord(c)<32 for c in nxt) or U.urlsplit(nxt).netloc: nxt='/tickets'
    raise Redirect(nxt)
   fields=F('email',typ='email')+(F('full_name','Full name') if p=='/signup' else '')+(F('password',typ='password') if p!='/password-reset' else '')
   return self.page(title,form(self.path,'signup' if p=='/signup' else 'signin' if p=='/signin' else 'password-reset',title,fields)+'<p><a href="/password-reset">Forgot your password?</a></p>')
  if p=='/signout' and self.post:
   for k,v in list(S['sessions'].items()):
    if v is self.session: del S['sessions'][k]
   self.newcookie=''; raise Redirect('/signin')
  if re.match(r'^/(verify|reset)/',p):
   kind,tok=p.strip('/').split('/',1); l=S['links'].get(sha(tok))
   if not l or l['kind']!=kind: raise HTTPError(404,'Link not found.')
   if not l['active'] or l['expires']<=now(): raise ValueError('This link is no longer valid.')
   u=S['users'][str(l['user'])]
   if kind=='verify': u['verified']=True; l['active']=False; return self.page('Address verified','<p>You can now <a href="/signin">sign in</a>.</p>')
   if self.post:
    if len(f.get('password',''))<10: raise ValueError('Password must contain at least 10 characters.')
    u['password']=password(f['password']); u['verified']=True; l['active']=False
    S['sessions']={k:s for k,s in S['sessions'].items() if s['user']!=u['id']}; raise Redirect('/signin')
   return self.page('Set your password',form(p,'set-password','Set password',F('password',typ='password')))
  if p=='/orgs/new':
   self.needuser()
   if self.post:
    n=name(f['name']); oid=ident(); S['orgs'][str(oid)]={'id':oid,'name':n}; S['members'].append({'organization_id':oid,'user_id':self.user['id'],'role':'owner'}); raise Redirect('/orgs/'+str(oid))
   b=form(p,'create-org','Create organization',F('name'))
   for m in S['members']:
    if m['user_id']==self.user['id']: b+='<p><a href="/orgs/'+str(m['organization_id'])+'">'+esc(S['orgs'][str(m['organization_id'])]['name'])+'</a></p>'
   return self.page('Your organizations',b)
  if p.startswith('/invites/'):
   self.needuser(); inv=self.get(sha(p.split('/')[2]),'invites')
   if inv['email']!=self.user['email']: raise HTTPError(403,'This invitation is for another address.')
   if inv['used']: raise ValueError('This invitation is no longer valid.')
   if self.post:
    if membership(self.user['id'],inv['org']): raise ValueError('Already a member.')
    S['members'].append({'organization_id':inv['org'],'user_id':self.user['id'],'role':inv['role']}); inv['used']=True; raise Redirect('/orgs/'+str(inv['org']))
   return self.page('Join '+S['orgs'][str(inv['org'])]['name'],form(p,'accept-invite','Accept invitation'))
  if p.startswith('/orgs/'): return self.organizer(p)
  if p.startswith('/events/'): return self.eventpage(p)
  if p.startswith('/orders/'): return self.orderpage(p)
  if p=='/tickets':
   self.needuser(); b=''
   for t in sorted(S['tickets'].values(),key=lambda t:(t['event_id'],seatkey(t['seat_id']))):
    if t['holder_email']==self.user['email']: b+='<a class="card" href="/tickets/'+str(t['id'])+'" '+attrs(ticket_id=t['id'],event_id=t['event_id'],seat=label(t),ticket_status=t['status'])+'><h2>'+esc(S['events'][str(t['event_id'])]['name'])+'</h2><p>'+esc(label(t))+' · '+esc(t['status'])+'</p></a>'
   return self.page('Your tickets',b or '<p>Your next great night starts with a ticket. <a href="/">Explore events</a></p>')
  if p.startswith('/tickets/'): return self.ticketpage(p)
  if p.startswith('/waitlist/offers/'):
   self.needuser(); bits=p.split('/'); of=self.get(sha(bits[3]),'offers')
   if of['email']!=self.user['email']: raise HTTPError(403,'This offer is for another account.')
   if of['status']=='open' and of['expires']<=now(): of['status']='expired'; S['wait'][str(of['wait_id'])]['status']='expired'
   if self.post:
    if of['status']!='open': raise ValueError('Offer no longer open.')
    if bits[-1]=='accept':
     e=S['events'][str(of['event_id'])]
     if e['status']!='published': raise ValueError('Event unavailable.')
     o=neworder(e,self.user,[S['seats'][str(s)] for s in of['seats']]); of['status']='accepted'; S['wait'][str(of['wait_id'])]['status']='accepted'; raise Redirect('/orders/'+str(o['id']))
    of['status']='declined'; S['wait'][str(of['wait_id'])]['status']='declined'; raise Redirect('/waitlist/offers/'+bits[3])
   b='<div '+attrs(offer_status=of['status'],offer_seats=' '.join(S['seats'][str(s)]['label'] for s in of['seats']))+'><p>'+esc(of['status'])+' · '+esc(' '.join(S['seats'][str(s)]['label'] for s in of['seats']))+'</p>'
   if of['status']=='open': b+=form(p+'/accept','accept-offer','Accept seats')+form(p+'/decline','decline-offer','Decline')
   return self.page('Your seat offer',b+'</div>')
  raise HTTPError(404,'Page not found.')
 def organizer(self,p):
  f=self.f; F=self.field; form=self.form; b=''; bits=p.strip('/').split('/'); oid=bits[1]; m=self.org(oid); org=self.get(oid,'orgs'); root='/orgs/'+oid; role=m['role']; manage=['owner','manager']
  if len(bits)==2:
   b='<div '+attrs(org_id=oid,my_role=role)+'><p class="lead">Your venue. Your community.</p>'
   if role=='owner': b+='<a class="pill" href="'+root+'/members">Team members</a>'
   if role in manage: b+='<a class="pill" href="'+root+'/venues/new">New venue</a><a class="pill" href="'+root+'/events/new">New event</a>'
   b+='<h2>Venues</h2>'
   for v in S['venues'].values():
    if str(v['organization_id'])==oid: b+='<p><a href="'+root+'/venues/'+str(v['id'])+'">'+esc(v['name'])+'</a></p>'
   b+='<h2>Events</h2>'
   for e in S['events'].values():
    if str(e['organization_id'])==oid: b+='<p><a href="'+root+'/events/'+str(e['id'])+'">'+esc(e['name'])+'</a> · '+esc(e['status'])+'</p>'
   return self.page(org['name'],b+'</div>')
  if bits[2]=='members':
   self.org(oid,['owner'])
   if self.post:
    if len(bits)==3:
     addr=email(f['email']); role=f['role']
     if role not in ['manager','box_office','scanner']: raise ValueError('Invalid role.')
     if any(S['users'][str(x['user_id'])]['email']==addr for x in S['members'] if str(x['organization_id'])==oid): raise ValueError('Already a member.')
     tok=secrets.token_urlsafe(32); S['invites'][sha(tok)]={'org':int(oid),'email':addr,'role':role,'used':False}; user=next((u for u in S['users'].values() if u['email']==addr),None); mail('invite:'+sha(tok),addr,user['full_name'] if user else addr,'Invitation to '+org['name'],org['name']+' invites you: '+BASE+'/invites/'+tok)
    else:
     member=membership(int(bits[3]),oid)
     if not member: raise HTTPError(404,'Member not found.')
     if member['role']=='owner': raise ValueError('Owner cannot be changed or removed.')
     if bits[4]=='remove': S['members'].remove(member)
     else:
      if f['role'] not in ['manager','box_office','scanner']: raise ValueError('Invalid role.')
      member['role']=f['role']
    raise Redirect(root+'/members')
   for member in S['members']:
    if str(member['organization_id'])!=oid: continue
    u=S['users'][str(member['user_id'])]; b+='<div class="card" '+attrs(member_user_id=u['id'],member_email=u['email'],role=member['role'])+'><h3>'+esc(u['full_name'])+'</h3><p>'+esc(u['email'])+' · '+esc(member['role'])+'</p>'
    if member['role']!='owner': b+=form(p+'/'+str(u['id'])+'/role','change-role','Change role',roleselect())+form(p+'/'+str(u['id'])+'/remove','remove-member','Remove member')
    b+='</div>'
   return self.page('Team members',b+form(p,'invite-member','Send invitation',F('email',typ='email')+roleselect()))
  if bits[2]=='venues':
   self.org(oid,manage)
   if bits[3]=='new':
    if self.post:
     n=name(f['name']); z=f['timezone']
     try: zoneinfo.ZoneInfo(z)
     except: raise ValueError('Unknown IANA time zone.')
     vid=ident(); S['venues'][str(vid)]={'id':vid,'organization_id':int(oid),'name':n,'address':f['address'],'timezone':z}; raise Redirect(root+'/venues/'+str(vid))
    return self.page('Create a venue',form(p,'create-venue','Create venue',F('name')+F('address')+F('timezone',value='America/Chicago')))
   v=self.get(bits[3],'venues')
   if str(v['organization_id'])!=oid: raise HTTPError(404,'Venue not found.')
   fixed=any(e['venue_id']==v['id'] for e in S['events'].values())
   if self.post:
    if fixed: raise ValueError('Seat map is fixed because an event exists.')
    code=f['code'].upper(); n=name(f['name']); rows=f['rows']; count=integer(f['seats_per_row'],1,60)
    if not re.fullmatch('[A-Z0-9]{1,8}',code) or any(s['venue_id']==v['id'] and s['code']==code for s in S['sections'].values()): raise ValueError('Section code must be unique, 1–8 uppercase letters or digits.')
    if not re.fullmatch('[A-Z]-[A-Z]',rows) or rows[0]>rows[-1]: raise ValueError('Enter rows like A-F.')
    valid=[r+str(i) for r in map(chr,range(ord(rows[0]),ord(rows[-1])+1)) for i in range(1,count+1)]; access=f.get('accessible','').split()
    if any(a not in valid for a in access): raise ValueError('Accessible seat is not in this section.')
    sid=ident(); S['sections'][str(sid)]={'id':sid,'venue_id':v['id'],'code':code,'name':n}
    for r in map(chr,range(ord(rows[0]),ord(rows[-1])+1)):
     for i in range(1,count+1):
      si=ident(); S['seats'][str(si)]={'id':si,'section_id':sid,'row_label':r,'seat_number':i,'accessible':r+str(i) in access,'label':code+'-'+r+'-'+str(i)}
    raise Redirect(root+'/venues/'+str(v['id']))
   b='<div '+attrs(venue_id=v['id'])+'><p>'+esc(v['address'])+' · '+esc(v['timezone'])+'</p>'
   for s in S['sections'].values():
    if s['venue_id']==v['id']:
     ss=sorted([x for x in S['seats'].values() if x['section_id']==s['id']],key=lambda x:seatkey(x['id']))
     b+='<div class="card" '+attrs(section=s['code'],seat_count=len(ss))+'><h2>'+esc(s['name'])+' · '+esc(s['code'])+'</h2><p>'+', '.join(esc(x['label'])+(' ♿' if x['accessible'] else '') for x in ss)+'</p></div>'
   if not fixed: b+=form(root+'/venues/'+str(v['id'])+'/sections','add-section','Add section',''.join(F(k) for k in ['code','name','rows','seats_per_row','accessible']))
   else: b+='<p>Seat map locked: this venue has events.</p>'
   return self.page(v['name'],b+'</div>')
  if bits[2]=='tickets':
   t=self.get(bits[3],'tickets'); e=self.get(t['event_id'],'events')
   if str(e['organization_id'])!=oid: raise HTTPError(404,'Ticket not found.')
   if bits[4]=='refund':
    self.org(oid,['owner','manager','box_office'])
    if t['legacy'] or t['status']!='valid': raise ValueError('This ticket cannot be refunded here.')
    refund(t,t['total_cents'])
   else:
    if t['status']!='valid' or t['admission']: raise ValueError('Ticket cannot be checked in.')
    self.scans([{'scan_id':secrets.token_hex(16),'token':t['token'],'gate':'Door','scanned_at':iso(now())}],e)
   raise Redirect(root+'/events/'+str(e['id'])+('/orders' if bits[4]=='refund' else '/door'))
  if bits[2]!='events': raise HTTPError(404,'Not found.')
  if bits[3]=='new':
   self.org(oid,manage); vv=[v for v in S['venues'].values() if str(v['organization_id'])==oid]
   if self.post:
    v=self.get(f['venue_id'],'venues')
    if str(v['organization_id'])!=oid: raise HTTPError(403,'Invalid venue.')
    ss=[s for s in S['sections'].values() if s['venue_id']==v['id']]
    if not ss: raise ValueError('Add sections before creating an event.')
    start=local(f['starts_at'],v['timezone']); on=local(f['on_sale_at'],v['timezone']); off=local(f['off_sale_at'],v['timezone']) if f.get('off_sale_at') else None; pre=local(f['presale_at'],v['timezone']) if f.get('presale_at') else None; code=f.get('presale_code','').strip().upper()
    if start<=now() or not on<(off if off is not None else start) or off is not None and off>start or pre is not None and pre>=on or bool(pre is not None)!=bool(code) or code and not re.fullmatch('[A-Z0-9]{4,20}',code): raise ValueError('Check event dates, sales windows and presale code.')
    e={'id':ident(),'organization_id':int(oid),'venue_id':v['id'],'name':name(f['name']),'description':f.get('description',''),'start':start,'on_sale':on,'off_sale':off,'presale':pre,'presale_code':code,'facility_fee_cents':integer(f['facility_fee_cents']),'prices':{str(s['id']):integer(f['price_'+s['code']],100) for s in ss},'status':'draft','reschedules':[],'legacy':False}; S['events'][str(e['id'])]=e; raise Redirect(root+'/events/'+str(e['id']))
   b='<label>Venue<select name="venue_id">'+''.join('<option value="'+str(v['id'])+'">'+esc(v['name'])+'</option>' for v in vv)+'</select></label>'+''.join(F(k,typ='datetime-local' if k.endswith('_at') else 'text') for k in ['name','description','starts_at','on_sale_at','off_sale_at','presale_at','presale_code','facility_fee_cents'])
   for code in sorted({s['code'] for s in S['sections'].values() if any(v['id']==s['venue_id'] for v in vv)}): b+=F('price_'+code,'Price '+code+' (cents)')
   return self.page('Create an event',form(p,'create-event','Create draft',b))
  eid=bits[3]; action=bits[4] if len(bits)>4 else ''; need=manage if action in ['','publish','reschedule','codes','devices','reminders','attendees.csv'] else ['owner'] if action=='cancel' else ['owner','manager','box_office'] if action in ['orders','dashboard'] else None
  e,m=self.eventorg(oid,eid,need); eroot=root+'/events/'+eid
  if not action:
   b='<div '+attrs(event_id=eid,event_status=e['status'],starts_at=iso(e['start']))+'><p>'+esc(display(e['start'],e))+' · '+esc(e['status'])+'</p><a class="pill" href="/events/'+eid+'">View event</a>'
   for a in ['dashboard','orders','door','codes','devices','reminders','attendees.csv']: b+='<a class="pill" href="'+eroot+'/'+a+'">'+a.replace('.csv',' export').title()+'</a>'
   if e['status']=='draft': b+=form(eroot+'/publish','publish-event','Publish event')
   if e['status']=='published' and e['start']>now(): b+=form(eroot+'/reschedule','reschedule-event','Reschedule',F('starts_at',typ='datetime-local'))
   if role=='owner' and e['status']=='published' and not any(t['legacy'] for t in tickets(e)): b+='<p>Cancellation refunds every ticket and closes all sales and waiting lists.</p>'+form(eroot+'/cancel','cancel-event','Cancel event & refund all tickets')
   return self.page(e['name'],b+'</div>')
  if action=='publish':
   if e['status']!='draft': raise ValueError('Only drafts can be published.')
   e['status']='published'; raise Redirect(eroot)
  if action=='reschedule':
   start=local(f['starts_at'],S['venues'][str(e['venue_id'])]['timezone'])
   if e['status']!='published' or e['start']<=now() or start<=now(): raise ValueError('Enter a future date for a published event.')
   if start!=e['start']: e['reschedules'].append(now()); e['start']=start
   if e['off_sale'] is not None and e['off_sale']>start: e['off_sale']=start
   raise Redirect(eroot)
  if action=='cancel':
   if e['status']!='published' or any(t['legacy'] for t in tickets(e)): raise ValueError('This event cannot be cancelled here.')
   e['status']='cancelled'; recipients={}
   for o in S['orders'].values():
    if o['event_id']==e['id'] and o['status']=='pending': o['status']='expired'
   for w in S['wait'].values():
    if w['event_id']==e['id']: w['status']='closed'
   for of in S['offers'].values():
    if of['event_id']==e['id'] and of['status']=='open': of['status']='declined'
   for t in tickets(e):
    o=S['orders'][str(t['order_id'])]; recipients[o['buyer_email']]=o['buyer_name']; recipients[t['holder_email']]=t['holder_name']; pending=sum(r['amount'] for r in S['refunds'].values() if r.get('ticket_id')==t['id'] and not r['complete']); remain=t['total_cents']-t['refunded_cents']-pending
    if remain>0: refund(t,remain,True)
   for addr,n in recipients.items(): mail('cancel:'+eid+':'+addr,addr,n,'Cancelled: '+e['name'],e['name']+' has been cancelled. Your tickets will be refunded.')
   raise Redirect(eroot)
  if action=='codes':
   if self.post:
    code=f['code'].strip().upper(); kind=f['kind']
    if not re.fullmatch('[A-Z0-9]{4,20}',code) or kind not in ['fixed','percent'] or any(c['event_id']==e['id'] and c['code']==code for c in S['codes'].values()): raise ValueError('Invalid or duplicate code.')
    cid=ident(); S['codes'][str(cid)]={'id':cid,'event_id':e['id'],'code':code,'kind':kind,'value':integer(f['value'],1,100 if kind=='percent' else 100000000),'min':integer(f['min_subtotal_cents']),'max':integer(f['max_redemptions'],1) if f.get('max_redemptions') else None}; raise Redirect(p)
   b=''.join('<p>'+esc(c['code'])+' · '+esc(c['kind'])+' '+str(c['value'])+'</p>' for c in S['codes'].values() if c['event_id']==e['id'])
   return self.page('Promo codes',b+form(p,'create-code','Create code',F('code')+'<label>Kind<select name="kind"><option>percent</option><option>fixed</option></select></label>'+''.join(F(k) for k in ['value','min_subtotal_cents','max_redemptions'])))
  if action=='devices':
   key=None; device=None
   if self.post:
    if len(bits)>5:
     device=self.get(bits[5],'devices')
     if device['event_id']!=e['id']: raise HTTPError(404,'Device not found.')
     device['revoked']=True; raise Redirect(eroot+'/devices')
    key=secrets.token_urlsafe(32); did=ident(); device={'id':did,'event_id':e['id'],'label':name(f['label']),'hash':password(key),'revoked':False}; S['devices'][str(did)]=device
   if key: b+='<div '+attrs(device_id=device['id'])+'><p>Copy this key now. It is shown only once.</p><code '+attrs(device_key=key)+'>'+esc(key)+'</code></div>'
   for d in S['devices'].values():
    if d['event_id']==e['id']: b+='<div class="card" '+attrs(device_id=d['id'],**({'revoked':'true'} if d['revoked'] else {}))+'>'+esc(d['label'])+(form(eroot+'/devices/'+str(d['id'])+'/revoke','revoke-device','Revoke') if not d['revoked'] else ' · Revoked')+'</div>'
   return self.page('Scanner devices',b+form(eroot+'/devices','create-device','Create device',F('label')))
  if action=='reminders':
   if self.post:
    subject=name(f['subject'],150); key=f.get('reminder_key',''); record=S['reminders'].get(sha(key))
    if not record or record['event_id']!=e['id']: raise ValueError('Invalid reminder key.')
    if not record['used']:
     seen=set()
     for t in tickets(e):
      if t['status']=='valid' and t['holder_email'] not in seen: mail('reminder:'+sha(key)+':'+t['holder_email'],t['holder_email'],t['holder_name'],subject,f['body']); seen.add(t['holder_email'])
     record['used']=True
    return self.page('Reminder sent','<p>Your reminder has been queued.</p>')
   key=secrets.token_urlsafe(32); S['reminders'][sha(key)]={'event_id':e['id'],'used':False}
   return self.page('Send a reminder',form(p,'send-reminder','Send reminder','<input type="hidden" data-reminder-key name="reminder_key" value="'+key+'">'+F('subject')+'<label>Body<textarea name="body"></textarea></label>'))
  if action=='attendees.csv': return self.export(e)
  if action=='dashboard': return self.dashboard(e)
  if action in ['orders','door']:
   for t in tickets(e):
    b+='<div class="card" '+attrs(ticket_id=t['id'],ticket_status=t['status'],admitted_at=iso(t['admission']['time']) if t['admission'] else '')+'><h3>'+esc(label(t))+'</h3><p>'+esc(t['holder_name'])+' · '+esc(t['status'])+(' · '+esc(iso(t['admission']['time']))+' '+esc(t['admission']['gate']) if t['admission'] else '')+'</p>'
    if action=='orders' and t['status']=='valid' and not t['legacy']: b+=form(root+'/tickets/'+str(t['id'])+'/refund','organizer-refund','Refund '+money(t['total_cents']))
    if action=='door' and t['status']=='valid' and not t['admission']: b+=form(root+'/tickets/'+str(t['id'])+'/checkin','door-checkin','Check in')
    b+='</div>'
   return self.page('Door list' if action=='door' else 'Orders & tickets',b)
  raise HTTPError(404,'Not found.')
 def eventpage(self,p):
  bits=p.split('/'); e=self.get(bits[2],'events'); u=self.user
  if e['status']=='draft' and (not u or not membership(u['id'],e['organization_id'])): raise HTTPError(404,'Event not found.')
  if self.post:
   self.needuser()
   if e['status']!='published' or sale(e) not in ['on_sale','presale']: raise ValueError('Sales are not open for this event.')
   if len(bits)>3 and bits[3]=='waitlist':
    sec=self.get(self.f['section_id'],'sections')
    if sec['venue_id']!=e['venue_id']: raise HTTPError(403,'Invalid section.')
    if any(s['section_id']==sec['id'] and seatstatus(e,s)=='available' for s in seats(e)): raise ValueError('This section still has available seats.')
    if any(w['event_id']==e['id'] and w['section_id']==sec['id'] and w['email']==u['email'] and w['status'] in ['waiting','offered'] for w in S['wait'].values()): raise ValueError('You already joined this waiting list.')
    qty=integer(self.f['quantity'],1,4)
    if buyer_count(e,u['email'])+qty>6: raise ValueError('Per-buyer ticket limit exceeded.')
    wid=ident(); S['wait'][str(wid)]={'id':wid,'event_id':e['id'],'section_id':sec['id'],'email':u['email'],'name':u['full_name'],'quantity':qty,'status':'waiting'}; raise Redirect('/events/'+str(e['id']))
   if sale(e)=='presale' and self.f.get('presale_code','').strip().upper()!=e['presale_code']: raise ValueError('Enter the presale code to reserve seats.')
   requested=self.multi.get('seats',[])
   if not 1<=len(requested)<=6 or len(set(requested))!=len(requested): raise ValueError('Choose 1 to 6 different seats.')
   ss=[s for s in seats(e) if s['label'] in requested]
   if len(ss)!=len(requested) or any(seatstatus(e,s)!='available' for s in ss): raise ValueError('One or more seats are no longer available.')
   if any(s['accessible'] for s in ss) and self.f.get('accessible')!='1': raise ValueError('Confirm that you need accessible seating.')
   o=neworder(e,u,ss); raise Redirect('/orders/'+str(o['id']))
  b='<div '+attrs(event_id=e['id'],starts_at=iso(e['start']),sale_state=sale(e))+'><p class="lead">'+esc(S['venues'][str(e['venue_id'])]['name'])+'</p><p>'+esc(display(e['start'],e))+'</p><p>'+esc(e['description'])+'</p><span class="pill">'+sale(e).replace('_',' ').title()+'</span><h2>Make yourself at home</h2><p class="legend">□ Available　 ▧ Held　 ■ Sold　 ♿ Accessible</p>'
  # Separate waitlist forms avoid nesting forms.
  seathtml=''; waiting=''
  for sec in sorted([s for s in S['sections'].values() if s['venue_id']==e['venue_id']],key=lambda s:s['code']):
   ss=[s for s in seats(e) if s['section_id']==sec['id']]; states={s['id']:seatstatus(e,s) for s in ss}; av=sum(v=='available' for v in states.values()); w=next((w for w in S['wait'].values() if u and w['email']==u['email'] and w['event_id']==e['id'] and w['section_id']==sec['id'] and w['status'] in ['waiting','offered']),None)
   sectionhtml='<section class="seatsection" '+attrs(section=sec['code'],price_cents=e['prices'][str(sec['id'])],available=av,**({'my_waitlist':w['quantity']} if w else {}))+'><h3>'+esc(sec['name'])+' <span>'+money(e['prices'][str(sec['id'])])+'</span></h3><div class="stage">STAGE</div><div class="seats">'
   row=None
   for s in ss:
    if row!=s['row_label']:
     if row: sectionhtml+='</div>'
     row=s['row_label']; sectionhtml+='<div class="seatrow"><strong>'+esc(row)+'</strong>'
    status=states[s['id']]; sectionhtml+='<label class="seat '+status+'" title="'+esc(s['label'])+'" '+attrs(seat=s['label'],seat_status=status,accessible=str(s['accessible']).lower())+'>'
    if status=='available': sectionhtml+='<input form="holdform" type="checkbox" name="seats" value="'+esc(s['label'])+'">'
    sectionhtml+='<span>'+('♿' if s['accessible'] else str(s['seat_number']))+'</span></label>'
   if row: sectionhtml+='</div>'
   sectionhtml+='</div>'
   if av==0 and e['status']=='published' and sale(e) in ['on_sale','presale']:
    sectionhtml+=self.form('/events/'+str(e['id'])+'/waitlist','join-waitlist','Join waiting list','<input type="hidden" name="section_id" value="'+str(sec['id'])+'">'+self.field('quantity','Seats (1–4)',value='1')) if u else '<a href="/signin">Sign in to join the waiting list</a>'
   sectionhtml+='</section>'
   if av: seathtml+=sectionhtml
   else: waiting+=sectionhtml
  if u:
   b+='<form id="holdform" method="post" action="/events/'+str(e['id'])+'">'+self.csrf()+seathtml+'<label><input type="checkbox" name="accessible" value="1"> I need accessible seating</label>'+self.field('presale_code','Presale code (if needed)')+'<button data-action="hold-seats">Reserve selected seats</button></form>'
  else: b+=seathtml+'<a class="pill" href="/signin?next=/events/'+str(e['id'])+'">Sign in to reserve your seats</a>'
  b+=waiting
  return self.page(e['name'],b+'</div>')
 def csrf(self): return '<input type="hidden" name="csrf" data-csrf value="'+esc(self.session['csrf'])+'">'
 def orderpage(self,p):
  self.needuser(); bits=p.split('/'); o=self.get(bits[2],'orders'); e=self.get(o['event_id'],'events'); f=self.f
  if o['buyer_email']!=self.user['email']: raise HTTPError(403,'This order belongs to another account.')
  if o['status']=='pending' and o['expires']<=now(): o['status']='expired'
  pending=o['status']=='pending' and o['expires']>now()
  if self.post:
   if not pending: raise ValueError('This hold is no longer active.')
   action=bits[3]
   if action=='release': o['status']='released'; raise Redirect('/orders/'+str(o['id']))
   if action=='pay':
    if not o['checkout']:
     o['payment_started']=True; save(); self.checkpoint=copy.deepcopy(S)
     price(o); url=BASE+'/orders/'+str(o['id']); result=provider('/v1/checkout/sessions',{'mode':'payment','success_url':url,'cancel_url':url,'customer_email':o['buyer_email'],'metadata[order_id]':o['id'],'line_items[0][price_data][currency]':'usd','line_items[0][price_data][unit_amount]':o['total_cents'],'line_items[0][price_data][product_data][name]':e['name'],'line_items[0][quantity]':1},'order-'+str(o['id'])); o['checkout']=result
    raise Redirect(o['checkout']['url'])
   if o['checkout'] or o.get('payment_started'): raise ValueError('Codes cannot change after Pay has been pressed.')
   code=f.get('code','').strip().upper(); c=next((c for c in S['codes'].values() if c['event_id']==e['id'] and c['code']==code),None)
   if not c: raise ValueError('Code not found for this event.')
   if action in ['remove-code','remove']:
    if c['id'] not in o['codes']: raise ValueError('Code is not applied.')
    o['codes'].remove(c['id'])
   else:
    if c['id'] in o['codes'] or any(S['codes'][str(cid)]['kind']==c['kind'] for cid in o['codes']): raise ValueError('A code of this kind is already applied.')
    if sum(i['face_cents'] for i in o['items'])<c['min']: raise ValueError('Face subtotal is below the code minimum.')
    use=sum(c['id'] in x['codes'] and (x['status']=='paid' or x['status']=='pending' and x['expires']>now()) for x in S['orders'].values())
    if c['max'] is not None and use>=c['max']: raise ValueError('This code has no redemptions left.')
    o['codes'].append(c['id'])
   price(o); raise Redirect('/orders/'+str(o['id']))
  totals={k:sum(i[k] for i in o['items']) for k in ['face_cents','discount_cents','service_fee_cents','facility_fee_cents','tax_cents','total_cents']}
  b='<div '+attrs(order_id=o['id'],order_status=o['status'],hold_expires_at=iso(o['expires']),subtotal_cents=totals['face_cents'],**{k:v for k,v in totals.items() if k!='face_cents'})+'><p>'+esc(e['name'])+' · <strong>'+esc(o['status'])+'</strong></p>'
  if pending: b+='<p class="notice">Your seats are held until '+iso(o['expires'])+' · '+str(max(0,o['expires']-now()))+' seconds remaining.</p>'
  for item in o['items']:
   t=next((t for t in S['tickets'].values() if t['order_id']==o['id'] and t['seat_id']==item['seat_id']),None)
   b+='<div class="card" '+attrs(seat=label(item),**{k:v for k,v in item.items() if k!='seat_id'},**({'ticket_id':t['id']} if t else {}))+'><h3>'+esc(label(item))+' <span>'+money(item['total_cents'])+'</span></h3><p>Face '+money(item['face_cents'])+' − discount '+money(item['discount_cents'])+' + service '+money(item['service_fee_cents'])+' + facility '+money(item['facility_fee_cents'])+' + tax '+money(item['tax_cents'])+'</p>'+(('<a href="/tickets/'+str(t['id'])+'">View ticket</a>') if t else '')+'</div>'
  b+='<dl>'+''.join('<dt>'+k.replace('_cents','').replace('_',' ').title()+'</dt><dd>'+money(v)+'</dd>' for k,v in totals.items())+'</dl>'
  for cid in o['codes']:
   c=S['codes'][str(cid)]; b+='<div '+attrs(applied_code=c['code'])+'>'+esc(c['code'])
   if pending and not o['checkout'] and not o.get('payment_started'): b+=self.form('/orders/'+str(o['id'])+'/remove-code','remove-code','Remove','<input type="hidden" name="code" value="'+esc(c['code'])+'">')
   b+='</div>'
  if pending:
   if not o['checkout'] and not o.get('payment_started'): b+=self.form('/orders/'+str(o['id'])+'/codes','apply-code','Apply code',self.field('code','Promo code'))
   b+=self.form('/orders/'+str(o['id'])+'/pay','pay','Pay '+money(o['total_cents']))+self.form('/orders/'+str(o['id'])+'/release','release-hold','Release hold')
  return self.page('Your reservation',b+'</div>')
 def ticketpage(self,p):
  self.needuser(); bits=p.split('/'); t=self.get(bits[2],'tickets'); e=self.get(t['event_id'],'events'); o=self.get(t['order_id'],'orders'); holder=t['holder_email']==self.user['email']; buyer=o['buyer_email']==self.user['email']
  if not holder and not buyer: raise HTTPError(403,'This ticket belongs to another account.')
  full,half,percent=deadlines(t); transferable=holder and t['status']=='valid' and not t['admission'] and now()<e['start'] and e['status']!='cancelled'; refundable=transferable and buyer and not t['transferred'] and not t['legacy'] and percent>0
  if self.post:
   if bits[3]=='refund':
    if not refundable: raise ValueError('This ticket is not eligible for a buyer refund.')
    refund(t,rounddiv(t['total_cents']*percent,100)); raise Redirect('/tickets/'+str(t['id']))
   if not transferable: raise ValueError('This ticket cannot be transferred.')
   addr=email(self.f['to_email']); n=name(self.f['to_name'])
   if addr==t['holder_email']: raise ValueError('You cannot transfer a ticket to yourself.')
   previous=t['holder_email']; previousname=t['holder_name']; t['old_tokens'].append(t['token']); t['holder_email']=addr; t['holder_name']=n; t['transferred']=True; t['token']=qr(t); k=str(ident())
   mail('transfer-in:'+k,addr,n,'You have a ticket',label(t)+' Ticket '+str(t['id'])+'\n'+t['token']+'\n'+BASE+'/tickets/'+str(t['id'])); mail('transfer-out:'+k,previous,previousname,'Ticket transferred',label(t)+' was transferred.'); raise Redirect('/tickets/'+str(t['id']))
  b='<div '+attrs(ticket_id=t['id'],seat=label(t),ticket_status=t['status'],holder_email=t['holder_email'],admitted_at=iso(t['admission']['time']) if t['admission'] else '',**({'full_refund_until':iso(full),'half_refund_until':iso(half),'refund_percent':percent if refundable else 0} if not t['legacy'] else {}))+'><p class="lead">'+esc(label(t))+'</p><p>'+esc(e['name'])+' · '+esc(display(e['start'],e))+'</p><p>'+esc(t['holder_name'])+' · '+esc(t['holder_email'])+' · '+esc(t['status'])+'</p>'
  if holder: b+='<div class="token" '+attrs(qr_token=t['token'])+'><p>Your entry token</p><code>'+esc(t['token'])+'</code></div>'
  if not t['legacy']:
   b+='<h2>Refund options</h2><p>100% until '+esc(display(full,e))+'<br>50% until '+esc(display(half,e))+'</p><p>A request now would return '+money(rounddiv(t['total_cents']*percent,100) if refundable else 0)+'.</p>'
  else: b+='<p>This ticket was purchased on the old platform. Refunds are unavailable here.</p>'
  if refundable: b+=self.form('/tickets/'+str(t['id'])+'/refund','request-refund','Request refund')
  if transferable: b+='<h2>Pass it to a friend</h2><p>Transferring replaces the QR token. The old QR code immediately stops admitting anyone.</p>'+self.form('/tickets/'+str(t['id'])+'/transfer','transfer-ticket','Transfer ticket',self.field('to_email','Recipient email',typ='email')+self.field('to_name','Recipient full name'))
  return self.page('Your ticket',b+'</div>')
 def webhook(self):
  try:
   sig=dict(x.split('=',1) for x in self.headers.get('Stripe-Signature','').split(',')); ts=int(sig['t']); secret=open(E['PAYMENTS_WEBHOOK_SIGNING_FILE']).read().strip(); expected=hmac.new(secret.encode(),sig['t'].encode()+b'.'+self.raw,hashlib.sha256).hexdigest()
   if abs(now()-ts)>300 or not hmac.compare_digest(expected,sig['v1']): raise ValueError()
   event=json.loads(self.raw); obj=event['data']['object']
  except: raise HTTPError(400,'Invalid webhook signature or payload.')
  if event['type']=='checkout.session.completed':
   o=next((o for o in S['orders'].values() if o.get('checkout') and o['checkout']['id']==obj.get('id')),None)
   if not o or o['status'] in ['paid','unavailable']: return ('OK',200,'text/plain')
   e=S['events'][str(o['event_id'])]; available=e['status']!='cancelled' and all(seatstatus(e,S['seats'][str(i['seat_id'])],exclude=o['id'],reserve=not(o['status']=='pending' and o['expires']>now()))=='available' for i in o['items'])
   o['payment']=obj.get('payment_intent'); o['paid_at']=now()
   if not available:
    o['status']='unavailable'; rid=str(ident()); S['refunds'][rid]={'id':rid,'ticket_id':None,'order_id':o['id'],'amount':o['total_cents'],'key':'late-'+str(o['id']),'provider_id':None,'complete':False,'cancel':False,'last_try':0}; mail('unavailable:'+str(o['id']),o['buyer_email'],o['buyer_name'],'Seats no longer available','Your seats were no longer available. '+money(o['total_cents'])+' was refunded.')
   else:
    o['status']='paid'; lines=[e['name']]
    for i in o['items']:
     tid=ident(); t={**i,'id':tid,'order_id':o['id'],'event_id':e['id'],'holder_email':o['buyer_email'],'holder_name':o['buyer_name'],'status':'valid','legacy':False,'old_tokens':[],'transferred':False,'admission':None,'refunded_cents':0}; t['token']=qr(t); S['tickets'][str(tid)]=t; lines.append(label(t)+' Ticket '+str(tid)+' '+t['token'])
    mail('confirmation:'+str(o['id']),o['buyer_email'],o['buyer_name'],'Your tickets for '+e['name'],'\n'.join(lines))
  elif event['type']=='refund.created':
   r=next((r for r in S['refunds'].values() if r['provider_id']==obj.get('id')),None)
   if not r:
    # A webhook can beat the HTTP refund response. Match its immutable metadata.
    r=next((r for r in S['refunds'].values() if not r['provider_id'] and str(r.get('ticket_id'))==str(obj.get('metadata',{}).get('ticket_id')) and S['orders'][str(r['order_id'])]['payment']==obj.get('payment_intent') and r['amount']==obj.get('amount')),None)
   if r and not r['complete']:
    r['complete']=True; r['provider_id']=obj['id']
    if r['ticket_id']:
     t=S['tickets'][str(r['ticket_id'])]; t['refunded_cents']+=r['amount']; t['status']='refunded'; o=S['orders'][str(t['order_id'])]
     if not r['cancel']: mail('refund:'+r['id'],o['buyer_email'],o['buyer_name'],'Refund completed',money(r['amount'])+' refunded for '+label(t))
  return ('OK',200,'text/plain')
 def scanapi(self,p):
  auth=self.headers.get('Authorization',''); key=auth[7:] if auth.startswith('Bearer ') else ''; device=next((d for d in S['devices'].values() if not d['revoked'] and pwcheck(key,d['hash'])),None)
  if not device: raise HTTPError(401,'Invalid scanner key.')
  try:
   data=json.loads(self.raw)
   if not isinstance(data,dict): raise ValueError()
   if p.endswith('/batch'):
    rows=data['scans']
    if not isinstance(rows,list) or len(rows)>1000: raise ValueError()
   else: rows=[{'scan_id':secrets.token_hex(16),'token':data['token'],'gate':data['gate'],'scanned_at':iso(now())}]
   for row in rows:
    if not isinstance(row,dict) or set(row)!={'scan_id','token','gate','scanned_at'} or not all(isinstance(v,str) for v in row.values()) or not 1<=len(row['scan_id'])<=64 or not 1<=len(row['gate'])<=40 or any(ord(c)<32 for c in row['gate']): raise ValueError()
    if not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)',row['scanned_at']): raise ValueError()
    D.datetime.fromisoformat(row['scanned_at'].replace('Z','+00:00'))
  except: raise HTTPError(400,'Malformed scan request.')
  result=self.scans(rows,S['events'][str(device['event_id'])]); return (json.dumps(result if p.endswith('/batch') else result[0]),200,'application/json')
 def scans(self,rows,e):
  results=[]; accepted=[]
  for row in rows:
   sk=str(e['id'])+':'+row['scan_id']; previous=S['scans'].get(sk)
   if previous:
    results.append({'scan_id':row['scan_id'],'result':'duplicate' if previous['row']==row else 'refused','reason':None if previous['row']==row else 'scan_id_reused','ticket_id':previous['ticket_id']}); continue
   tok=row['token']; t=next((t for t in S['tickets'].values() if tok==t['token'] or tok in t['old_tokens']),None); stamp=int(D.datetime.fromisoformat(row['scanned_at'].replace('Z','+00:00')).timestamp()); reason=None
   if not t: reason='invalid'
   elif t['event_id']!=e['id']: reason='wrong_event'
   elif e['status']=='cancelled': reason='cancelled'
   elif t['status'] in ['refund_pending','refunded']: reason='refunded'
   elif tok!=t['token']: reason='transferred'
   elif stamp<e['start']-10800 or stamp>=e['start']+21600: reason='outside_window'
   tid=t['id'] if t else None; result={'scan_id':row['scan_id'],'result':'refused' if reason else 'already_admitted','reason':reason,'ticket_id':tid}; results.append(result); S['scans'][sk]={'row':row,'ticket_id':tid}
   if not reason:
    accepted.append((result,t,sk))
    if not t['admission'] or stamp<t['admission']['time']: t['admission']={'time':stamp,'gate':row['gate'],'key':sk}
  for result,t,sk in accepted:
   a=t['admission']; result.update(result='admitted' if a['key']==sk else 'already_admitted',admitted_at=iso(a['time']),gate=a['gate'])
  return results
 def export(self,e):
  stream=io.StringIO(newline=''); writer=csv.writer(stream,lineterminator='\r\n'); writer.writerow('ticket_id,order_id,section,row,seat,holder_name,holder_email,buyer_name,buyer_email,status,total_cents,refunded_cents,purchased_at,checked_in_at,gate'.split(','))
  def safe(v): return "'"+v if v and v[0] in '=+-@\t\r' else v
  for t in tickets(e):
   o=S['orders'][str(t['order_id'])]; s=S['seats'][str(t['seat_id'])]; a=t['admission']; writer.writerow([t['id'],t['order_id'],S['sections'][str(s['section_id'])]['code'],s['row_label'],s['seat_number'],safe(t['holder_name']),safe(t['holder_email']),safe(o['buyer_name']),safe(o['buyer_email']),t['status'],t['total_cents'],t['refunded_cents'],iso(o['paid_at']),iso(a['time']) if a else '',safe(a['gate']) if a else ''])
  return (stream.getvalue(),200,'text/csv; charset=utf-8',{'Content-Disposition':'attachment; filename="attendees.csv"'})
 def dashboard(self,e):
  def stats(section=None):
   ss=[s for s in seats(e) if section is None or s['section_id']==section]; ids={s['id'] for s in ss}; tt=[t for t in tickets(e) if t['seat_id'] in ids]; sold=sum(t['status'] in ['valid','refund_pending'] for t in tt); held=sum(seatstatus(e,s,reserve=False)=='held' for s in ss); gross=0; refunded=sum(t['refunded_cents'] for t in tt if t['legacy'])
   for o in S['orders'].values():
    if o['event_id']==e['id'] and o['status'] in ['paid','unavailable']: gross+=sum(i['total_cents'] for i in o['items'] if i['seat_id'] in ids)
   for r in S['refunds'].values():
    if not r['complete']: continue
    if r['ticket_id'] and S['tickets'][str(r['ticket_id'])]['seat_id'] in ids: refunded+=r['amount']
    elif not r['ticket_id']:
     o=S['orders'][str(r['order_id'])]
     if o['event_id']==e['id']: refunded+=sum(i['total_cents'] for i in o['items'] if i['seat_id'] in ids)
   return dict(capacity=len(ss),sold=sold,held=held,available=len(ss)-sold-held,admitted=sum(bool(t['admission']) for t in tt),gross_cents=gross,refunded_cents=refunded,net_cents=gross-refunded)
  def block(st,extra): return '<div class="metrics" '+attrs(**extra,**st)+'>'+''.join('<div><small>'+k.replace('_cents','').title()+'</small><strong>'+(money(v) if k.endswith('_cents') else str(v))+'</strong></div>' for k,v in st.items())+'</div>'
  b=block(stats(),{'dashboard_event_id':e['id']})
  for sec in sorted([s for s in S['sections'].values() if s['venue_id']==e['venue_id']],key=lambda s:s['code']): b+='<h2>'+esc(sec['name'])+'</h2>'+block(stats(sec['id']),{'section':sec['code']})
  return self.page('Event dashboard',b)
def attrs(**kw): return ' '.join('data-'+k.replace('_','-')+'="'+esc(v)+'"' for k,v in kw.items())
def roleselect(): return '<label>Role<select name="role"><option value="manager">Manager</option><option value="box_office">Box office</option><option value="scanner">Scanner</option></select></label>'
# Shared form helpers keep CSRF fields inside every form.
def field(self,k,label=None,typ='text',value=''): return '<label>'+esc(label or k.replace('_',' ').title())+'<input name="'+esc(k)+'" type="'+esc(typ)+'" value="'+esc(value)+'"></label>'
def form(self,path,action,label,fields=''): return '<form method="post" action="'+esc(path)+'">'+self.csrf()+fields+'<button data-action="'+esc(action)+'">'+esc(label)+'</button></form>'
App.field=field; App.form=form
original_route=App.route
def routed(self,p):
 if p=='/signout':
  if not self.post: raise HTTPError(405,'Use the sign-out button.')
  for k in list(S['sessions']):
   if S['sessions'][k] is self.session: del S['sessions'][k]
  self.newcookie=''; raise Redirect('/signin')
 return original_route(self,p)
App.route=routed
CSS=''':root{--ink:#173e35;--accent:#dbf266;--paper:#f7f7ef}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.6 system-ui,-apple-system,sans-serif}nav{display:flex;align-items:center;gap:28px;padding:22px 5%;border-bottom:1px solid #ccd5c8;background:#fff}nav a{font-size:14px;text-decoration:none}nav .brand{font-size:28px;font-weight:850;letter-spacing:-1.5px;margin-right:auto}nav .brand span{font-size:17px;color:#a8bf39;margin-left:4px}nav form{margin:0}nav button{padding:8px 16px}main{max-width:1200px;margin:54px auto;padding:0 30px;min-height:70vh}h1{font-size:clamp(32px,4.5vw,60px);line-height:1.1;letter-spacing:-2px;font-weight:650;max-width:900px;margin:10px 0 34px}h2{font-size:26px;letter-spacing:-.7px;margin-top:36px}h3{margin:0 0 12px;font-size:20px}h3 span{float:right}.eyebrow{font-size:11px;font-weight:750;letter-spacing:2px}.lead{font-size:32px;line-height:1.25;letter-spacing:-.8px}.hero{padding:25px 36px;background:var(--ink);color:var(--paper);border-radius:18px;position:relative;overflow:hidden}.hero:after{content:'✳';position:absolute;right:8%;top:-20px;font-size:210px;color:var(--accent);line-height:1.3}.hero p{position:relative;z-index:1}.hero .lead{font-size:45px;margin:15px 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:24px}.card{display:block;border:1px solid #d6dccd;padding:24px;border-radius:14px;margin:16px 0;background:white;text-decoration:none;color:inherit}.event{padding:0 0 22px;overflow:hidden}.event p,.event h2,.event span{margin-left:22px;margin-right:22px}.event h2{margin-top:12px}.art{height:140px;background:#dfe6d6;color:#47715a;font-size:100px;text-align:center;line-height:140px}.event:nth-child(3n+2) .art{background:#e4ddd2;color:#946950}.event:nth-child(3n) .art{background:#d9e2e7;color:#426674}.pill{display:inline-block;border:1px solid #b6c5b5;border-radius:30px;padding:7px 17px;text-decoration:none;margin:5px 8px 5px 0;font-size:14px}a{color:inherit}button{background:var(--ink);color:white;border:0;border-radius:7px;padding:12px 22px;font:600 14px system-ui;cursor:pointer}button:hover{background:#346454}form{margin:22px 0;max-width:660px}label{display:block;font-size:14px;font-weight:600;margin:16px 0}input:not([type=checkbox]),select,textarea{display:block;width:100%;border:1px solid #becbbd;background:white;border-radius:6px;padding:12px;color:var(--ink);font:16px system-ui;margin-top:6px}input[type=checkbox]{accent-color:var(--ink)}textarea{min-height:150px}.seatsection{background:#fff;border:1px solid #d6dccd;border-radius:14px;padding:24px;margin:25px 0}.stage{background:#e6ebdf;text-align:center;letter-spacing:6px;font-size:11px;margin:25px auto;width:70%;border-radius:50% 50% 4px 4px;padding:8px}.seats{overflow:auto}.seatrow{display:flex;gap:6px;align-items:center;padding:4px;width:max-content}.seatrow strong{width:22px}.seat{margin:0;position:relative;font-size:12px}.seat span{display:block;min-width:32px;height:32px;text-align:center;line-height:30px;border:1px solid #7e9b82;border-radius:5px;background:#f3f7ed}.seat input{position:absolute;opacity:0}.seat input:checked+span{background:var(--ink);color:white}.seat.sold span{background:#69816f;color:#fff}.seat.held span{background:#ead9a6;border-style:dashed}.seat.waitlist span{background:#ddd7ed}.legend{font-size:13px;color:#59715f}.notice{padding:15px;background:#e6edcb;border-radius:8px}dl{display:grid;grid-template-columns:1fr 1fr;max-width:500px}dd{text-align:right;font-weight:650}.token{padding:24px;border:2px dashed #b8c99b;border-radius:12px;max-width:650px;overflow-wrap:anywhere;background:#fff}code{font-size:14px}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:18px}.metrics div{background:white;border:1px solid #d6dccd;border-radius:12px;padding:20px}.metrics small{display:block}.metrics strong{font-size:30px}footer{padding:30px 5%;border-top:1px solid #ccd5c8;font-size:13px;color:#647663}@media(max-width:650px){nav{gap:12px;flex-wrap:wrap}nav .brand{width:100%}main{margin-top:30px;padding:0 18px}.hero:after{opacity:.25}.metrics{grid-template-columns:repeat(2,1fr)}}'''
def background():
 while True:
  try:
   with LOCK: work()
  except Exception: pass
  time.sleep(2)
def main():
 init()
 try: provider('/v1/webhook_endpoints',{'url':BASE+'/webhooks/payments'})
 except Exception: pass
 threading.Thread(target=background,daemon=True).start()
 ThreadingHTTPServer(('0.0.0.0',int(E['PORT'])),App).serve_forever()
if __name__=='__main__': main()
