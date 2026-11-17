import os,re,json,hashlib,hmac,secrets,time,threading,traceback,csv,io,smtplib,urllib.request,urllib.parse
from datetime import datetime,date,timedelta,timezone
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from http.cookies import SimpleCookie
from email.message import EmailMessage
from email.headerregistry import Address
from html import escape
from db import DB
E=os.environ
UTC=timezone.utc
BASE=E['PUBLIC_BASE_URL'].rstrip('/')
LOCK=threading.RLock()
def now(): return datetime.now(UTC)
def digest(s): return hashlib.sha256(s.encode()).hexdigest()
def e(s): return escape(str(s if s is not None else ''),quote=True)
def money(v): return '${:,.2f}'.format(v/100)
def dt(s): return datetime.fromisoformat(str(s).replace('Z','+00:00'))
def day(s):
 if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',s or ''): raise ValueError('Enter a date as YYYY-MM-DD.')
 return date.fromisoformat(s)
def name(s):
 if not 1<=len(s)<=200 or '\r' in s or '\n' in s: raise ValueError('Names must have 1–200 characters and no line breaks.')
 return s
def email(s):
 s=s.strip().lower()
 if '\r' in s or '\n' in s or not re.fullmatch(r'[^\s@]+@[^\s@]+',s): raise ValueError('Enter a valid email address.')
 return s
def pw(s):
 if len(s)<10: raise ValueError('Use a password of at least 10 characters.')
 salt=secrets.token_hex(16); return salt+':'+hashlib.scrypt(s.encode(),salt=salt.encode(),n=16384,r=8,p=1).hex()
def checkpw(s,stored):
 if not stored:return False
 salt,val=stored.split(':'); return hmac.compare_digest(val,hashlib.scrypt(s.encode(),salt=salt.encode(),n=16384,r=8,p=1).hex())
def account(d,addr): return d.one('SELECT * FROM users WHERE active AND lower(trim(email))=$1 ORDER BY id LIMIT 1',addr.strip().lower())
def api(path,data,key=None):
 req=urllib.request.Request(E['PAYMENTS_API_URL'].rstrip('/')+path,urllib.parse.urlencode(data).encode(),headers={'Idempotency-Key':key} if key else {})
 with urllib.request.urlopen(req,timeout=2) as r:return json.load(r)
def send(user,subject,body):
 if not user or not user['active']:return
 msg=EmailMessage(); msg['From']=E['MAIL_FROM']; msg['To']=Address(display_name=user['full_name'].replace('\r',' ').replace('\n',' '),addr_spec=user['email'].strip().lower());msg['Subject']=subject;msg.set_content(body)
 with smtplib.SMTP(E['SMTP_HOST'],int(E['SMTP_PORT']),timeout=2) as s:s.send_message(msg)
def mail(d,key,uid,subject,body):d.q('INSERT INTO hv_mail(key,user_id,subject,body) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING',key,uid,subject,body)
def clinician(d,cid):
 c=d.one('SELECT c.*,u.full_name,k.name AS clinic_name,k.timezone FROM clinicians c JOIN users u ON u.id=c.user_id JOIN clinics k ON k.id=c.clinic_id WHERE c.id=$1',cid)
 if not c: raise HTTPError(404,'Clinician not found.')
 return c
def atype(d,code):
 t=d.one('SELECT * FROM appointment_types WHERE code=$1',code)
 if not t: raise HTTPError(404,'Appointment type not found.')
 return t
def wall(D,t,z,fold=0):
 text=str(t); hrs=int(text[:2]); mins=int(text[3:5]); secs=int(text[6:8]) if len(text)>7 else 0
 return (datetime.combine(D,datetime.min.time())+timedelta(hours=hrs,minutes=mins,seconds=secs)).replace(tzinfo=ZoneInfo(z),fold=fold).astimezone(UTC)
def local(a): return a['start'].astimezone(ZoneInfo(a['timezone']))
def appointment(d,aid):
 a=d.one('''SELECT a.*,h.start_at,h.hold_until,h.checkout_id,h.payment_intent,coalesce(h.refund_cents,0) AS refund_cents,coalesce(h.refund_status,'none') AS refund_status,h.offer_until,h.appointment_id AS appmade,t.code,t.name AS type_name,t.duration_minutes,t.buffer_minutes,c.user_id AS clinician_user_id,c.clinic_id,u.full_name AS clinician_name,p.full_name AS patient_name,p.email AS patient_email,k.name AS clinic_name,k.timezone FROM appointments a JOIN appointment_types t ON t.id=a.type_id JOIN clinicians c ON c.id=a.clinician_id JOIN users u ON u.id=c.user_id JOIN users p ON p.id=a.patient_id JOIN clinics k ON k.id=c.clinic_id LEFT JOIN hv_appointments h ON h.appointment_id=a.id WHERE a.id=$1''',aid)
 if not a:raise HTTPError(404,'Appointment not found.')
 a['start']=dt(a['start_at']) if a['start_at'] else dt(a['local_start']).replace(tzinfo=ZoneInfo(a['timezone']),fold=a['fold']).astimezone(UTC)
 return a
def working(d,c,D):
 if d.one('SELECT id FROM clinic_closures WHERE clinic_id=$1 AND on_date=$2',c['clinic_id'],D):return []
 v=d.one('SELECT id FROM schedule_versions WHERE clinician_id=$1 AND effective_from<=$2 ORDER BY effective_from DESC,id DESC LIMIT 1',c['id'],D)
 ranges=d.q('SELECT start_time::text,end_time::text FROM schedule_windows WHERE version_id=$1 AND weekday=$2',v['id'],D.isoweekday()) if v else []
 ex=d.q('SELECT kind,start_time::text,end_time::text FROM schedule_exceptions WHERE clinician_id=$1 AND on_date=$2',c['id'],D)
 if any(x['kind']=='off' and x['start_time'] is None for x in ex):return []
 ranges += [x for x in ex if x['kind']=='extra']
 def interval(x):
  if not x['start_time'] or not x['end_time'] or x['end_time']<=x['start_time']:return None
  a,b=wall(D,x['start_time'],c['timezone']),wall(D,x['end_time'],c['timezone']); return (a,b) if b>a else None
 merged=[]
 for a,b in sorted(filter(None,(interval(x) for x in ranges))):
  if merged and a<=merged[-1][1]: merged[-1]=(merged[-1][0],max(b,merged[-1][1]))
  else:merged.append((a,b))
 for x in ex:
  r=interval(x)
  if x['kind']=='off' and r:
   a,b=r; parts=[]
   for l,h in merged:
    if a>=h or b<=l:parts.append((l,h))
    else:
     if l<a:parts.append((l,a))
     if h>b:parts.append((b,h))
   merged=parts
 return merged
def availability(d,c,t,D,ignore=None):
 n=now(); today=n.astimezone(ZoneInfo(c['timezone'])).date()
 if not c['active'] or not t['active'] or D>today+timedelta(days=int(E['BOOKING_HORIZON_DAYS'])):return []
 fee=d.one('SELECT * FROM type_fees WHERE type_id=$1 AND effective_from<=$2 ORDER BY effective_from DESC LIMIT 1',t['id'],D)
 if not fee:return []
 rooms=d.q('SELECT id FROM rooms WHERE clinic_id=$1 AND room_type=$2 AND active ORDER BY id',c['clinic_id'],t['room_type'])
 occupied=d.q('''SELECT a.id,a.clinician_id,a.room_id,a.local_start,a.fold,k.timezone,t.duration_minutes,t.buffer_minutes,h.start_at FROM appointments a JOIN clinicians c ON c.id=a.clinician_id JOIN clinics k ON k.id=c.clinic_id JOIN appointment_types t ON t.id=a.type_id LEFT JOIN hv_appointments h ON h.appointment_id=a.id WHERE c.clinic_id=$1 AND a.id<>$2 AND (a.status IN ('booked','completed','no_show','confirmed') OR (a.status='held' AND h.hold_until>now()) OR (a.status='offered' AND h.offer_until>now())) AND a.local_start::date BETWEEN $3::date-1 AND $3::date+1''',c['clinic_id'],ignore or -1,D)
 busy=[]; count=0
 for a in occupied:
  s=dt(a['start_at']) if a['start_at'] else dt(a['local_start']).replace(tzinfo=ZoneInfo(a['timezone']),fold=a['fold']).astimezone(UTC)
  busy.append((a,s,s+timedelta(minutes=a['duration_minutes']+a['buffer_minutes'])))
  if a['clinician_id']==c['id'] and s.astimezone(ZoneInfo(c['timezone'])).date()==D:count+=1
 if count>=c['max_per_day']:return []
 out=[]; dur=timedelta(minutes=t['duration_minutes']); block=dur+timedelta(minutes=t['buffer_minutes']); stretches=working(d,c,D)
 # Walk instants so both occurrences of a repeated quarter-hour are offered.
 s=wall(D,'00:00',c['timezone']); stop=wall(D+timedelta(days=1),'00:00',c['timezone'])
 while s<stop:
  loc=s.astimezone(ZoneInfo(c['timezone']))
  if loc.date()==D and loc.minute%15==0 and s>=n+timedelta(minutes=int(E['MIN_NOTICE_MINUTES'])) and any(l<=s and s+dur<=h for l,h in stretches):
   clash=lambda a,b:s<b and s+block>a
   if not any(a['clinician_id']==c['id'] and clash(l,h) for a,l,h in busy):
    free=next((r['id'] for r in rooms if not any(a['room_id']==r['id'] and clash(l,h) for a,l,h in busy)),None)
    if free:out.append({'start':s,'end':s+dur,'room_id':free,'deposit':(fee['fee_cents']*fee['deposit_bp']+5000)//10000})
  s+=timedelta(minutes=15)
 return out
def choose(d,c,t,start,ignore=None):
 s=dt(start)
 if s.tzinfo is None:raise ValueError('The start needs a time-zone offset.')
 D=s.astimezone(ZoneInfo(c['timezone'])).date()
 slot=next((x for x in availability(d,c,t,D,ignore) if x['start']==s),None)
 if not slot:raise HTTPError(409,'That slot is no longer available. Choose another time.')
 return slot
def create_appt(d,p,c,t,s,status='held',payment=None,deposit=None):
 loc=s['start'].astimezone(ZoneInfo(c['timezone'])); aid=d.one('INSERT INTO appointments(patient_id,clinician_id,type_id,room_id,local_start,fold,status,deposit_cents) VALUES($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id',p,c['id'],t['id'],s['room_id'],loc.replace(tzinfo=None),loc.fold,status,s['deposit'] if deposit is None else deposit)['id']
 d.q('INSERT INTO hv_appointments(appointment_id,start_at,hold_until,payment_intent,offer_until) VALUES($1,$2,$3,$4,$5)',aid,s['start'],now()+timedelta(seconds=int(E['HOLD_TTL_SECONDS'])) if status=='held' else None,payment,now()+timedelta(seconds=int(E['OFFER_TTL_SECONDS'])) if status=='offered' else None)
 return aid
def checkout(d,aid):
 a=appointment(d,aid)
 try:
  sess=api('/v1/checkout/sessions',{'mode':'payment','customer_email':a['patient_email'].strip().lower(),'metadata[appointment_id]':aid,'success_url':BASE+f'/appointments/{aid}?checkout=success','cancel_url':BASE+f'/appointments/{aid}?checkout=cancelled','line_items[0][price_data][currency]':'usd','line_items[0][price_data][unit_amount]':a['deposit_cents'],'line_items[0][price_data][product_data][name]':a['type_name'],'line_items[0][quantity]':1},f'checkout-{aid}')
 except Exception:raise HTTPError(502,'Payment service is unavailable. Your slot remains free; please try again.')
 d.q('UPDATE hv_appointments SET checkout_id=$1 WHERE appointment_id=$2',sess['id'],aid);return sess

def trigger(d,cid,D):
 c=clinician(d,cid)
 for w in d.q("SELECT w.*,t.code FROM hv_waitlist w JOIN appointment_types t ON t.id=w.type_id WHERE clinician_id=$1 AND on_date=$2 AND status='waiting' ORDER BY joined_at,id",cid,D):
  t=atype(d,w['code']); slots=availability(d,c,t,D)
  if not slots:continue
  token=secrets.token_urlsafe(32);aid=create_appt(d,w['patient_id'],c,t,slots[0],'offered'); a=appointment(d,aid)
  d.q("UPDATE hv_waitlist SET status='offered',token_hash=$1,appointment_id=$2,expires_at=$3 WHERE id=$4",digest(token),aid,a['offer_until'],w['id'])
  user=d.one('SELECT * FROM users WHERE id=$1',w['patient_id'])
  send(user,'A slot opened up',f"{c['clinic_name']} • {local(a):%Y-%m-%d %H:%M %Z}\nOffer expires {a['offer_until']}. Accept to hold the slot and pay your deposit.\n{BASE}/offers/{token}")
  break

def stopped(d,a):trigger(d,a['clinician_id'],local(a).date())
def refund(d,a,amount,pi=None,key=None):
 if d.one('SELECT key FROM hv_refunds WHERE key=$1',key or f"cancel-{a['id']}"):return
 status='pending' if amount else 'none';d.q('UPDATE hv_appointments SET refund_cents=$1,refund_status=$2 WHERE appointment_id=$3',amount,status,a['id'])
 if amount:d.q('INSERT INTO hv_refunds(key,appointment_id,payment_intent,amount) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING',key or f"cancel-{a['id']}",a['id'],pi or a['payment_intent'],amount)
def sweep(d):
 rows=d.q("SELECT a.id FROM appointments a JOIN hv_appointments h ON a.id=h.appointment_id WHERE (a.status='held' AND hold_until<=now()) OR (a.status='offered' AND offer_until<=now()) ORDER BY a.id")
 for r in rows:
  a=appointment(d,r['id']);d.q("UPDATE appointments SET status='expired' WHERE id=$1",a['id']);d.q("UPDATE hv_waitlist SET status='expired' WHERE appointment_id=$1 AND status='offered'",a['id']);stopped(d,a)

class HTTPError(Exception):
 def __init__(self,status,message):self.status=status;self.message=message

STYLE='''*{box-sizing:border-box}body{margin:0;background:#f4f6f3;color:#183c3c;font:16px/1.55 system-ui,sans-serif}header{background:#123e40;color:white;padding:22px max(5%,calc((100% - 1120px)/2));display:flex;gap:24px;align-items:center;flex-wrap:wrap}header a{color:#fff;text-decoration:none}header .brand{font:600 24px Georgia,serif;margin-right:auto}nav{display:flex;gap:18px;flex-wrap:wrap}main{max-width:1120px;margin:40px auto;padding:0 24px 60px}h1{font:normal 42px/1.2 Georgia,serif;margin:12px 0 22px}h2{font:normal 26px Georgia,serif}a{color:#176761}p{max-width:760px}.eyebrow{font-size:12px;letter-spacing:2px;text-transform:uppercase;color:#5c7770}.card,form.panel{background:#fff;border:1px solid #dbe3dc;padding:24px;border-radius:12px;margin:16px 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,280px),1fr));gap:20px}label{display:block;font-size:14px;font-weight:600;margin:14px 0}input,select,textarea{display:block;width:100%;max-width:620px;padding:12px;border:1px solid #adbfba;border-radius:6px;background:white;color:#183c3c;font:inherit;margin:7px 0}textarea{min-height:110px}button,.button{display:inline-block;background:#146b61;color:white;border:0;border-radius:6px;padding:12px 20px;font:600 14px system-ui;cursor:pointer;text-decoration:none}button:hover,.button:hover{background:#0d5049}.muted{color:#61736d}.tag{font-size:12px;text-transform:uppercase;letter-spacing:1px;background:#e5eee6;border-radius:20px;padding:5px 10px;display:inline-block}.row{display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:12px;border-bottom:1px solid #ddd;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere}header form{margin:0}header button{background:transparent;padding:0}footer{border-top:1px solid #d5dfd6;padding:20px 5%;color:#61736d;font-size:13px} .hero{padding:30px 0 40px}.hero h1{max-width:650px;font-size:54px} .card a{overflow-wrap:anywhere}@media(max-width:500px){main{margin:24px auto;padding:0 18px 40px}h1,.hero h1{font-size:35px}header{padding:18px}nav{gap:12px;font-size:14px}.card,form.panel{padding:18px}td,th{padding:6px;font-size:13px}}'''
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def response(self,body,status=200,ctype='text/html; charset=utf-8',headers=None):
  data=body.encode() if isinstance(body,str) else body
  self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(data)));self.send_header('X-Frame-Options','DENY');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Cache-Control','no-store')
  if getattr(self,'newcookie',None):self.send_header('Set-Cookie',f'hv_session={self.newcookie}; HttpOnly; SameSite=Lax; Path=/')
  for k,v in (headers or {}).items():self.send_header(k,v)
  self.end_headers();self.wfile.write(data)
 def page(self,title,body,status=200):
  nav='<a href="/book">Find a visit</a>'
  if self.u:
   nav+='<a href="/">Dashboard</a><a href="/profile">Profile</a>'+self.form('/signout','signout','Sign out','',False)
  else:nav+='<a href="/signin">Sign in</a><a href="/register">Register</a>'
  self.response('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+e(title)+' · Harborview Physio</title><style>'+STYLE+'</style></head><body><header><a class="brand" href="/">Harborview <span style="font-weight:normal">Physio</span></a><nav>'+nav+'</nav></header><main><div class="eyebrow">Seattle & Honolulu · Care that moves you</div><h1>'+e(title)+'</h1>'+body+'</main><footer>Harborview Physio · A little more movement. A little more possibility.</footer></body></html>',status)
 def form(self,path,action,label,fields, panel=True):return '<form method="post" action="'+e(path)+'"'+(' class="panel"' if panel else '')+'><input type="hidden" name="csrf" data-csrf value="'+e(self.csrf)+'">'+fields+'<button data-action="'+e(action)+'">'+e(label)+'</button></form>'
 def field(self,key,label=None,value='',typ='text',required=True):return '<label>'+e(label or key.replace('_',' ').title())+'<input name="'+e(key)+'" type="'+typ+'" value="'+e(value)+'" '+('required' if required else '')+'></label>'
 def hidden(self,key,val):return '<input type="hidden" name="'+e(key)+'" value="'+e(val)+'">'
 def select(self,key,rows,valuekey='id',labelkey='name'):return '<label>'+e(key.replace('_',' ').title())+'<select name="'+e(key)+'">'+''.join('<option value="'+e(r[valuekey])+'">'+e(r[labelkey])+'</option>' for r in rows)+'</select></label>'
 def redirect(self,path):self.response('',303,headers={'Location':path})
 def login(self):
  if not self.u:raise HTTPError(302,'/signin?next='+urllib.parse.quote(self.path,safe=''))
 def role(self,*roles):
  self.login()
  if self.u['role'] not in roles:raise HTTPError(403,'You do not have access to this page or action.')
 def clinic_access(self,cid,manager=False):
  self.role(*(['manager','admin'] if manager else ['front_desk','manager','admin']))
  if self.u['role']!='admin' and self.u['clinic_id']!=int(cid):raise HTTPError(403,'This clinic is outside your access.')
 def can_desk(self,a):return self.u and (self.u['role']=='admin' or self.u['role'] in ('manager','front_desk') and self.u['clinic_id']==a['clinic_id'])
 def access(self,a):
  self.login()
  if self.u['id'] not in (a['patient_id'],a['clinician_user_id']) and not self.can_desk(a):raise HTTPError(403,'You do not have access to this appointment.')
 def schedule_access(self,c):
  self.login()
  if self.u['role']=='admin' or self.u['id']==c['user_id'] or self.u['role']=='manager' and self.u['clinic_id']==c['clinic_id']:return
  raise HTTPError(403,'You do not have access to this schedule.')
 def do_GET(self):self.handle_request()
 def do_POST(self):self.handle_request()
 def handle_request(self):
  self.u=None;self.csrf='';self.newcookie=None
  try:
   parsed=urllib.parse.urlsplit(self.path);self.pathonly=parsed.path; self.args={k:v[-1] for k,v in urllib.parse.parse_qs(parsed.query,keep_blank_values=True).items()};self.post=self.command=='POST'
   size=int(self.headers.get('Content-Length','0'))
   if size<0 or size>100000:raise HTTPError(400,'Request too large.')
   self.raw=self.rfile.read(size) if self.post else b''
   if self.pathonly=='/healthz':
    try:
     d=DB();d.q('SELECT 1');d.close();self.response('ok',ctype='text/plain')
    except Exception:self.response('Database unavailable',503,'text/plain')
    return
   if self.pathonly=='/webhooks/payments':self.webhook();return
   self.f={k:v[-1] for k,v in urllib.parse.parse_qs(self.raw.decode(),keep_blank_values=True).items()} if self.post else self.args
   with LOCK,DB() as d:
    self.d=d
    cookies=SimpleCookie();cookies.load(self.headers.get('Cookie',''));token=cookies['hv_session'].value if 'hv_session' in cookies else ''
    sess=d.one('SELECT * FROM hv_sessions WHERE token_hash=$1',digest(token)) if token else None
    if not sess:
     token=secrets.token_urlsafe(32);self.newcookie=token;self.csrf=digest('csrf:'+token);sess={'token_hash':digest(token),'user_id':None};d.q('INSERT INTO hv_sessions(token_hash,csrf_hash) VALUES($1,$2)',sess['token_hash'],digest(self.csrf))
    else:self.csrf=digest('csrf:'+token)
    self.sess=sess
    if sess['user_id']:
     u=d.one('SELECT * FROM users WHERE id=$1 AND active',sess['user_id'])
     if u and account(d,u['email'])['id']==u['id']:self.u=u
    if self.post and not self.u and self.pathonly not in ('/signin','/register','/password-reset') and not self.pathonly.startswith('/password-reset/'):
     self.login()
    if self.post and not hmac.compare_digest(self.f.get('csrf',''),self.csrf):raise HTTPError(403,'The security token is missing or invalid. Reload the page and try again.')
    sweep(d);d.q('COMMIT');d.q('BEGIN');self.route()
  except HTTPError as ex:
   if ex.status==302:self.redirect(ex.message)
   else:self.page('Unable to complete request','<p>'+e(ex.message)+'</p>',ex.status)
  except (ValueError,KeyError,TypeError,OverflowError):self.page('Check your details','<p>The request contains invalid values or conflicts with existing data. Please check and try again.</p>',400)
  except Exception:
   traceback.print_exc();self.page('Temporarily unavailable','<p>Please try again shortly.</p>',503)
 def route(self):
  d=self.d;f=self.f;p=self.pathonly
  if p in ('/signin','/register','/password-reset'):
   if self.post:
    addr=f.get('email','').strip().lower();u=account(d,addr)
    if p=='/register':
     addr=email(addr);nm=name(f['full_name']);hashed=pw(f['password'])
     if d.one('SELECT id FROM users WHERE lower(trim(email))=$1',addr):raise HTTPError(409,'That email address is already registered.')
     u=d.one("INSERT INTO users(email,full_name,role) VALUES($1,$2,'patient') RETURNING *",addr,nm);d.q('INSERT INTO hv_auth(user_id,password_hash) VALUES($1,$2)',u['id'],hashed)
    elif p=='/password-reset':
     if u:
      token=secrets.token_urlsafe(32);d.q('INSERT INTO hv_reset_links(token_hash,user_id) VALUES($1,$2)',digest(token),u['id']);d.q('INSERT INTO hv_auth(user_id,reset_hash,reset_at,reset_used) VALUES($1,$2,$3,false) ON CONFLICT(user_id) DO UPDATE SET reset_hash=excluded.reset_hash,reset_at=excluded.reset_at,reset_used=false',u['id'],digest(token),now());send(u,'Set your Harborview Physio password',BASE+'/password-reset/'+token)
     self.page('Check your email','<p>If that address has an active account, a password link is on its way.</p>');return
    else:
     auth=d.one('SELECT password_hash FROM hv_auth WHERE user_id=$1',u['id']) if u else None
     if not auth or not checkpw(f.get('password',''),auth['password_hash']):raise HTTPError(400,'The email or password is incorrect.')
    d.q('DELETE FROM hv_sessions WHERE token_hash=$1',self.sess['token_hash']);token=secrets.token_urlsafe(32);d.q('INSERT INTO hv_sessions(token_hash,csrf_hash,user_id) VALUES($1,$2,$3)',digest(token),digest(digest('csrf:'+token)),u['id']);self.newcookie=token
    nxt=f.get('next','');safe=bool(re.match(r'^/[^/\\]',nxt) or nxt=='/') and not any(ord(ch)<32 or ord(ch)==127 for ch in nxt)
    self.redirect(nxt if safe else '/');return
   fields=self.field('email','Email address',typ='email')
   if p=='/register':fields+=self.field('full_name','Full name')
   if p!='/password-reset':fields+=self.field('password','Password',typ='password')
   if p=='/signin':fields+=self.hidden('next',f.get('next',''))
   title={'/signin':'Welcome back','/register':'Start your care','/password-reset':'Set or reset your password'}[p];action={'/signin':'signin','/register':'register','/password-reset':'request-reset'}[p]
   self.page(title,self.form(p,action,{'/signin':'Sign in','/register':'Create account','/password-reset':'Send password link'}[p],fields)+'<p><a href="/password-reset">Need to set or reset a password?</a></p>');return
  if p.startswith('/password-reset/'):
   auth=d.one('SELECT * FROM hv_auth WHERE reset_hash=$1',digest(p.split('/')[-1]))
   if not auth:
    if d.one('SELECT token_hash FROM hv_reset_links WHERE token_hash=$1',digest(p.split('/')[-1])):raise HTTPError(400,'This link is no longer valid.')
    raise HTTPError(404,'Link not found.')
   if auth['reset_used'] or dt(auth['reset_at'])+timedelta(seconds=int(E['RESET_LINK_TTL_SECONDS']))<=now():raise HTTPError(400,'This link is no longer valid.')
   if self.post:
    hashed=pw(f['password']);d.q('UPDATE hv_auth SET password_hash=$1,reset_used=true WHERE user_id=$2',hashed,auth['user_id']);d.q('DELETE FROM hv_sessions WHERE user_id=$1',auth['user_id']);self.page('Password updated','<p>Your password has been set. <a href="/signin">Sign in</a> to continue.</p>');return
   self.page('Choose a new password',self.form(p,'set-password','Set password',self.field('password','New password · at least 10 characters',typ='password')));return
  if p=='/signout':
   if not self.post:raise HTTPError(405,'Use the sign-out button to end your session.')
   self.login();d.q('DELETE FROM hv_sessions WHERE token_hash=$1',self.sess['token_hash']);self.newcookie='';self.redirect('/signin');return
  if p=='/profile':
   self.login()
   if self.post:d.q('UPDATE users SET full_name=$1 WHERE id=$2',name(f['full_name']),self.u['id']);self.u['full_name']=f['full_name']
   self.page('Your profile',self.form(p,'save-profile','Save profile',self.field('full_name','Display name',self.u['full_name'])));return
  if p=='/':
   if not self.u:self.page('Move well. Live fully.','<div class="hero"><p>Personal physiotherapy, with room for your life. Find your next visit at our Seattle or Honolulu clinic.</p><a class="button" href="/book">Find an appointment →</a></div><div class="grid">'+''.join('<div class="card"><span class="tag">Your neighborhood clinic</span><h2>'+e(c['name'])+'</h2><p>'+e(c['timezone'])+'</p></div>' for c in d.q('SELECT * FROM clinics'))+'</div>');return
   r=self.u['role'];links=[('/book','Find an appointment'),('/profile','Your profile')]
   if r=='patient':links += [('/appointments','Your appointments'),('/waitlist','Your waitlist')]
   if r=='clinician':
    c=d.one('SELECT id FROM clinicians WHERE user_id=$1',self.u['id']);links += [('/clinician/day','Your day sheet'),(f"/clinicians/{c['id']}/schedule",'Your schedule')]
   if r in ('front_desk','manager','admin'):links += [('/desk/day','Clinic day sheet'),('/desk/book','Book for a patient'),('/desk/reminders','Send reminders')]
   if r=='admin':links += [('/admin/clinicians/new','Add a clinician'),('/admin/rooms/new','Add a room'),('/admin/types/new','Add an appointment type')]
   body='<p>Welcome, '+e(self.u['full_name'])+'. Your care workspace, all in one place.</p><div class="grid">'+''.join('<a class="card" href="'+e(url)+'">'+e(label)+' →</a>' for url,label in links)+'</div>'
   if r in ('manager','admin'):
    body+='<h2>Clinics & schedules</h2>'
    for c in d.q('SELECT c.*,u.full_name FROM clinicians c JOIN users u ON u.id=c.user_id'):
     if r=='admin' or c['clinic_id']==self.u['clinic_id']:body+=f'<p><a href="/clinicians/{c["id"]}/schedule">{e(c["full_name"])} · Schedule</a></p>'
    for c in d.q('SELECT * FROM clinics'):
     if r=='admin' or c['id']==self.u['clinic_id']:body+=f'<p><a href="/admin/clinics/{c["id"]}">{e(c["name"])} · Closures</a></p>'
   if r=='admin':
    body+='<h2>Fees</h2>'+''.join(f'<p><a href="/admin/types/{e(t["code"])}">{e(t["name"])} ({e(t["code"])})</a></p>' for t in d.q('SELECT * FROM appointment_types'))
   self.page('Your dashboard',body);return
  if p in ('/api/availability','/book'):
   if self.post and p=='/api/availability':raise HTTPError(405,'Availability accepts GET requests.')
   if self.post:
    self.role('patient');c=clinician(d,f['clinician_id']);t=atype(d,f['type']);slot=choose(d,c,t,f['start']);aid=create_appt(d,self.u['id'],c,t,slot);sess=checkout(d,aid);self.redirect(sess['url']);return
   cs=d.q('SELECT c.id,u.full_name AS name FROM clinicians c JOIN users u ON u.id=c.user_id ORDER BY c.id');ts=d.q('SELECT code,name FROM appointment_types WHERE active ORDER BY code')
   if p=='/api/availability' or all(f.get(k) for k in ('clinician_id','type','date')):
    c=clinician(d,f['clinician_id']);t=atype(d,f['type']);D=day(f['date']);slots=availability(d,c,t,D)
    if p=='/api/availability':self.response(json.dumps({'clinician_id':c['id'],'type':t['code'],'date':D.isoformat(),'slots':[{'start':s['start'].astimezone(ZoneInfo(c['timezone'])).isoformat(),'end':s['end'].astimezone(ZoneInfo(c['timezone'])).isoformat()} for s in slots]}),ctype='application/json');return
    body='<p>'+e(c['clinic_name'])+' · '+e(c['full_name'])+' · '+e(t['name'])+'</p><p>'+e(D)+' · '+e(c['timezone'])+'</p>'
    fee=d.one('SELECT * FROM type_fees WHERE type_id=$1 AND effective_from<=$2 ORDER BY effective_from DESC LIMIT 1',t['id'],D)
    if fee:body+='<p>Deposit due: <strong>'+money((fee['fee_cents']*fee['deposit_bp']+5000)//10000)+'</strong></p>'
    body+='<div class="grid">'
    for s in slots:
     loc=s['start'].astimezone(ZoneInfo(c['timezone']));body+='<div class="card"><h2>'+e(loc.strftime('%H:%M %Z'))+'</h2><p>'+e(t['duration_minutes'])+' minute visit</p>'
     if self.u and self.u['role']=='patient':body+=self.form('/book','book-slot','Book this time',self.hidden('clinician_id',c['id'])+self.hidden('type',t['code'])+self.hidden('start',loc.isoformat()),False)
     body+='</div>'
    body+='</div>'
    if not slots:body+='<div class="card">No times are available for this date. Try another day or <a href="/waitlist">join the waitlist</a>.</div>'
   else:body='<p>Choose your clinician, visit type, and date. All times are shown at the clinic.</p>'
   body+='<form class="panel" method="get">'+self.select('clinician_id',cs)+self.select('type',ts,'code')+self.field('date',typ='date',value=f.get('date',now().date().isoformat()))+'<button>Find times</button></form>'
   self.page('Find your next visit',body);return
  if p=='/appointments':
   self.role('patient');rows=d.q('SELECT id FROM appointments WHERE patient_id=$1 ORDER BY local_start DESC,id DESC',self.u['id']);self.page('Your appointments',''.join(self.appt_card(appointment(d,x['id'])) for x in rows) or '<p>No appointments yet. <a href="/book">Find your first visit.</a></p>');return
  m=re.fullmatch(r'/appointments/(\d+)(?:/(release|cancel|reschedule|notes))?',p)
  if m:
   a=appointment(d,int(m[1]));self.access(a);action=m[2];own=self.u['id']==a['patient_id'];desk=self.can_desk(a)
   if action=='notes':
    if self.u['id']!=a['clinician_user_id'] and self.u['role']!='admin':raise HTTPError(403,'Clinical notes are restricted to the treating clinician and admins.')
    if self.post:
     if self.u['id']!=a['clinician_user_id']:raise HTTPError(403,'Only the treating clinician can add a note.')
     if not 1<=len(f['body'])<=5000:raise ValueError('Invalid note length.')
     d.q('INSERT INTO clinical_notes(appointment_id,author_id,body) VALUES($1,$2,$3)',a['id'],self.u['id'],f['body'])
    body=self.appt_card(a)+''.join('<div class="card" data-note-id="'+e(n['id'])+'"><pre>'+e(n['body'])+'</pre></div>' for n in d.q('SELECT * FROM clinical_notes WHERE appointment_id=$1 ORDER BY id',a['id']))
    if self.u['id']==a['clinician_user_id']:body+=self.form(p,'add-note','Add note','<label>Clinical note<textarea name="body" maxlength="5000" required></textarea></label>')
    self.page('Clinical notes',body);return
   if action and self.post:
    if action=='release':
     if not own:raise HTTPError(403,'Only the patient may release a hold.')
     if not a['appmade'] or a['status']!='held':raise HTTPError(409,'This appointment is not held.')
     d.q("UPDATE appointments SET status='released' WHERE id=$1",a['id']);stopped(d,a)
    elif action=='cancel':
     if not own and not desk:raise HTTPError(403,'You cannot cancel this appointment.')
     if not a['appmade'] or a['status']!='confirmed' or a['start']<=now():raise HTTPError(409,'This appointment cannot be cancelled.')
     amount,why=self.refund_quote(a);d.q("UPDATE appointments SET status='cancelled' WHERE id=$1",a['id']);refund(d,a,amount);mail(d,f"cancel-{a['id']}",a['patient_id'],'Appointment cancelled',f"Your appointment at {a['clinic_name']} has been cancelled. Refund: {money(amount)}. {why}");stopped(d,a)
    else:
     if not own or self.u['role']!='patient':raise HTTPError(403,'Only the patient may reschedule.')
     if not a['appmade'] or a['status']!='confirmed' or a['start']-now()<timedelta(hours=24):raise HTTPError(409,'Rescheduling needs at least 24 hours notice.')
     c=clinician(d,f['clinician_id'])
     if c['clinic_id']!=a['clinic_id']:raise HTTPError(409,'Choose a clinician at the same clinic.')
     t=atype(d,a['code']);slot=choose(d,c,t,f['start'],a['id']);newid=create_appt(d,a['patient_id'],c,t,slot,'confirmed',a['payment_intent'],a['deposit_cents']);d.q("UPDATE appointments SET status='rescheduled' WHERE id=$1",a['id']);new=appointment(d,newid);mail(d,f'reschedule-{newid}',a['patient_id'],'Appointment rescheduled',f"{new['clinic_name']} • {new['clinician_name']} • {local(new):%Y-%m-%d %H:%M %Z}");stopped(d,a);self.redirect(f'/appointments/{newid}');return
    self.redirect(f"/appointments/{a['id']}");return
   body=self.appt_card(a,True)
   if a['status']=='held':
    body+='<p>Your payment is being confirmed. Your slot stays held until '+e(dt(a['hold_until']).astimezone(ZoneInfo(a['timezone'])).strftime('%Y-%m-%d %H:%M:%S %Z'))+'.</p>'
    if own:body+=self.form(p+'/release','release-hold','Give up this hold','')
   if a['status']=='confirmed':
    body+='<p>Deposit paid: <strong>'+money(a['deposit_cents'])+'</strong></p>'
    if a['appmade'] and a['start']>now() and (own or desk):
     amount,why=self.refund_quote(a);body+='<div class="card"><h2>Cancel your visit</h2><p>Cancelling now refunds '+money(amount)+'. '+e(why)+'</p>'+self.form(p+'/cancel','cancel-appointment','Cancel appointment','',False)+'</div>'
    if a['appmade'] and own and self.u['role']=='patient' and a['start']-now()>=timedelta(hours=24):
     cs=d.q('SELECT c.id,u.full_name AS name FROM clinicians c JOIN users u ON u.id=c.user_id WHERE c.clinic_id=$1',a['clinic_id']);body+='<h2>Move your visit</h2><p>Use <a href="/book">Find a visit</a> to choose an available start with its time-zone offset.</p>'+self.form(p+'/reschedule','reschedule','Reschedule',self.select('clinician_id',cs)+self.field('start','New start (ISO 8601 with offset)'))
   if a['status']=='cancelled':body+='<p>Refund: '+money(a['refund_cents'])+' · '+e(a['refund_status'])+'</p>'
   if a['status'] in ('expired','released'):body+='<p>This slot was given up and is available to other patients. <a href="/book">Choose a new appointment</a> to book again.</p>'
   if self.u['id']==a['clinician_user_id'] or self.u['role']=='admin':body+=f'<p><a href="{p}/notes">Clinical notes</a></p>'
   self.page('Your appointment',body);return
  if p=='/waitlist':
   self.role('patient')
   if self.post:
    c=clinician(d,f['clinician_id']);t=atype(d,f['type']);D=day(f['date']);today=now().astimezone(ZoneInfo(c['timezone'])).date()
    if not today<=D<=today+timedelta(days=int(E['BOOKING_HORIZON_DAYS'])):raise ValueError('Date outside booking horizon.')
    if d.one("SELECT id FROM hv_waitlist WHERE patient_id=$1 AND clinician_id=$2 AND type_id=$3 AND on_date=$4 AND status='waiting'",self.u['id'],c['id'],t['id'],D):raise HTTPError(409,'You are already waiting for this date.')
    d.q('INSERT INTO hv_waitlist(patient_id,clinician_id,type_id,on_date) VALUES($1,$2,$3,$4)',self.u['id'],c['id'],t['id'],D)
   rows=d.q('SELECT w.*,t.code,u.full_name FROM hv_waitlist w JOIN appointment_types t ON t.id=w.type_id JOIN clinicians c ON c.id=w.clinician_id JOIN users u ON u.id=c.user_id WHERE patient_id=$1 ORDER BY w.id',self.u['id'])
   body='<p>When a time opens, we’ll email the first waiting patient whose visit fits.</p>'+''.join('<div class="card" data-clinician-id="'+e(w['clinician_id'])+'" data-type="'+e(w['code'])+'" data-date="'+e(w['on_date'])+'" data-entry-status="'+e(w['status'])+'">'+e(w['full_name'])+' · '+e(w['on_date'])+' · '+e(w['code'])+' <span class="tag">'+e(w['status'])+'</span></div>' for w in rows)
   cs=d.q('SELECT c.id,u.full_name AS name FROM clinicians c JOIN users u ON u.id=c.user_id');ts=d.q('SELECT code,name FROM appointment_types WHERE active');body+=self.form(p,'join-waitlist','Join waitlist',self.select('clinician_id',cs)+self.select('type',ts,'code')+self.field('date',typ='date'));self.page('Your waitlist',body);return
  m=re.fullmatch(r'/offers/([^/]+)(?:/(accept|decline))?',p)
  if m:
   self.role('patient');w=d.one('SELECT * FROM hv_waitlist WHERE token_hash=$1',digest(m[1]))
   if not w:raise HTTPError(404,'Offer not found.')
   if w['patient_id']!=self.u['id']:raise HTTPError(403,'This offer belongs to another patient.')
   a=appointment(d,w['appointment_id'])
   if self.post:
    if w['status']!='offered' or dt(w['expires_at'])<=now():raise HTTPError(409,'This offer is no longer available.')
    if m[2]=='accept':
     d.q("UPDATE appointments SET status='held' WHERE id=$1",a['id']);d.q('UPDATE hv_appointments SET hold_until=$1,offer_until=NULL WHERE appointment_id=$2',now()+timedelta(seconds=int(E['HOLD_TTL_SECONDS'])),a['id']);sess=checkout(d,a['id']);d.q("UPDATE hv_waitlist SET status='booked' WHERE id=$1",w['id']);self.redirect(sess['url']);return
    if m[2]=='decline':d.q("UPDATE appointments SET status='released' WHERE id=$1",a['id']);d.q("UPDATE hv_waitlist SET status='declined' WHERE id=$1",w['id']);stopped(d,a);self.redirect('/offers/'+m[1]);return
   status='accepted' if w['status']=='booked' else w['status'];body='<div class="card" data-offer-status="'+e(status)+'" data-start="'+e(local(a).isoformat())+'" data-clinician-id="'+e(a['clinician_id'])+'" data-type="'+e(a['code'])+'"><h2>'+e(local(a).strftime('%Y-%m-%d %H:%M %Z'))+'</h2><p>'+e(a['clinic_name'])+' · '+e(a['type_name'])+'</p><p>Offer expires '+e(dt(w['expires_at']).astimezone(ZoneInfo(a['timezone'])).strftime('%Y-%m-%d %H:%M:%S %Z'))+'. Accepting holds this slot while you pay the '+money(a['deposit_cents'])+' deposit.</p></div>'
   if status=='offered':body+=self.form(p+'/accept','accept-offer','Accept & pay deposit','')+self.form(p+'/decline','decline-offer','Decline offer','')
   self.page('A time for you',body);return
  m=re.fullmatch(r'/clinicians/(\d+)/schedule(?:/(versions|exceptions))?',p)
  if m:
   c=clinician(d,int(m[1]));self.schedule_access(c)
   if self.post:
    if m[2]=='versions':
     D=day(f['effective_from']);windows=[]
     for line in f.get('windows','').splitlines():
      match=re.fullmatch(r'(MON|TUE|WED|THU|FRI|SAT|SUN) (\d{2}:\d{2})-(\d{2}:\d{2})',line.strip())
      if not match:raise ValueError('Invalid schedule line.')
      a,b=self.times(match[2],match[3]);windows.append(('MON TUE WED THU FRI SAT SUN'.split().index(match[1])+1,a,b))
     vid=d.one('INSERT INTO schedule_versions(clinician_id,effective_from) VALUES($1,$2) RETURNING id',c['id'],D)['id']
     for weekday,a,b in windows:d.q('INSERT INTO schedule_windows(version_id,weekday,start_time,end_time) VALUES($1,$2,$3,$4)',vid,weekday,a,b)
    elif m[2]=='exceptions':
     D=day(f['on_date']);kind=f['kind'];a=f.get('start_time','');b=f.get('end_time','')
     if kind not in ('off','extra') or kind=='extra' and not a or bool(a)!=bool(b):raise ValueError('Invalid exception.')
     if a:a,b=self.times(a,b)
     d.q('INSERT INTO schedule_exceptions(clinician_id,on_date,kind,start_time,end_time) VALUES($1,$2,$3,$4,$5)',c['id'],D,kind,a or None,b or None)
    self.redirect(f'/clinicians/{c["id"]}/schedule');return
   body='<p>'+e(c['full_name'])+' · '+e(c['clinic_name'])+' · '+e(c['timezone'])+'</p>'
   for v in d.q('SELECT * FROM schedule_versions WHERE clinician_id=$1 ORDER BY effective_from DESC,id DESC',c['id']):
    windows=d.q('SELECT weekday,start_time::text,end_time::text FROM schedule_windows WHERE version_id=$1 ORDER BY weekday,start_time',v['id']);body+='<div class="card"><h2>From '+e(v['effective_from'])+'</h2>'+(''.join('<p>'+e('MON TUE WED THU FRI SAT SUN'.split()[w['weekday']-1])+' '+e(w['start_time'][:5])+'–'+e(w['end_time'][:5])+'</p>' for w in windows) or '<p>Off every day</p>')+'</div>'
   body+='<h2>Add schedule version</h2>'+self.form(p+'/versions','add-version','Add version',self.field('effective_from',typ='date')+'<label>Weekly windows<textarea name="windows" placeholder="MON 09:00-17:00"></textarea></label>')+'<h2>Date exceptions</h2>'
   for x in d.q('SELECT *,start_time::text,end_time::text FROM schedule_exceptions WHERE clinician_id=$1 ORDER BY on_date',c['id']):body+='<p>'+e(x['on_date'])+' · '+e(x['kind'])+' '+e(x['start_time'] or 'Whole day')+' '+e(x['end_time'] or '')+'</p>'
   body+=self.form(p+'/exceptions','add-exception','Add exception',self.field('on_date',typ='date')+self.select('kind',[{'id':'off','name':'Off'},{'id':'extra','name':'Extra hours'}])+self.field('start_time','Start · HH:MM',required=False)+self.field('end_time','End · HH:MM or 24:00',required=False));self.page('Working schedule',body);return
  if p in ('/desk/day','/clinician/day','/desk/reminders','/desk/export.csv'):
   if p=='/clinician/day':
    self.role('clinician');c=d.one('SELECT * FROM clinicians WHERE user_id=$1',self.u['id']);cid=c['clinic_id']
   else:
    self.role('front_desk','manager','admin');cid=int(f.get('clinic_id') or self.u['clinic_id'] or 1);self.clinic_access(cid)
   clinic=d.one('SELECT * FROM clinics WHERE id=$1',cid)
   if not clinic:raise HTTPError(404,'Clinic not found.')
   D=day(f.get('date') or now().astimezone(ZoneInfo(clinic['timezone'])).date().isoformat())
   if p=='/desk/export.csv':
    lo=day(f['from']);hi=day(f['to'])
    if hi<lo or (hi-lo).days>=31:raise ValueError('Export up to 31 days.')
   else:lo=hi=D
   ids=d.q('SELECT a.id FROM appointments a JOIN clinicians c ON c.id=a.clinician_id WHERE c.clinic_id=$1 AND a.local_start::date BETWEEN $2 AND $3 ORDER BY a.local_start,a.id',cid,lo,hi)
   rows=[appointment(d,x['id']) for x in ids];rows.sort(key=lambda a:(a['start'],a['id']))
   if p=='/clinician/day':rows=[a for a in rows if a['clinician_id']==c['id']]
   if p=='/desk/export.csv':
    s=io.StringIO(newline='');writer=csv.writer(s,lineterminator='\r\n');writer.writerow('appointment_id,local_date,local_time,utc_start,clinician_id,clinician_name,patient_name,patient_email,type,status,deposit_cents,refund_cents'.split(','))
    safe=lambda s:"'"+s if s and s[0] in '=+-@\t\r' else s
    for a in rows:
     if a['status'] not in ('booked','completed','no_show','cancelled','confirmed','rescheduled'):continue
     l=local(a);writer.writerow([a['id'],l.strftime('%Y-%m-%d'),l.strftime('%H:%M'),a['start'].astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'),a['clinician_id'],safe(a['clinician_name']),safe(a['patient_name']),a['patient_email'].strip().lower(),a['code'],a['status'],a['deposit_cents'],a['refund_cents']])
    self.response(s.getvalue(),ctype='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="appointments.csv"'});return
   rows=[a for a in rows if a['status'] in ('booked','completed','no_show','confirmed')]
   body='<p>'+e(clinic['name'])+' · '+e(D)+' · '+e(clinic['timezone'])+'</p>'
   if p=='/desk/reminders':
    count=0
    if self.post:
     for a in rows:
      if a['status'] not in ('booked','confirmed') or not d.one('SELECT id FROM users WHERE id=$1 AND active',a['patient_id']):continue
      added=d.one('INSERT INTO hv_reminders(appointment_id) VALUES($1) ON CONFLICT DO NOTHING RETURNING appointment_id',a['id'])
      if added:mail(d,f"reminder-{a['id']}",a['patient_id'],'Appointment reminder',f"{a['clinic_name']} • {a['clinician_name']} • {local(a):%Y-%m-%d %H:%M %Z}");count+=1
     body+=f'<p data-reminders-sent="{count}">{count} reminders sent.</p>'
    body+=self.form(p,'send-reminders','Send reminders',self.field('clinic_id',value=cid)+self.field('date',typ='date',value=D))
   else:
    body+='<form class="panel" method="get">'+('' if p=='/clinician/day' else self.field('clinic_id',value=cid))+self.field('date',typ='date',value=D)+'<button>View day</button></form>'+(''.join(self.appt_card(a) for a in rows) or '<div class="card">No appointments for this day.</div>')
    if p=='/desk/day':body+=f'<p><a href="/desk/reminders?clinic_id={cid}&date={D}">Send reminders</a> · <a href="/desk/export.csv?clinic_id={cid}&from={D}&to={D}">Export this day as CSV</a></p>'
   self.page('Reminders' if p=='/desk/reminders' else 'Your day sheet',body);return
  if p=='/desk/book':
   self.role('front_desk','manager','admin')
   if self.post:
    c=clinician(d,f['clinician_id']);self.clinic_access(c['clinic_id']);patient=account(d,f['patient_email'])
    if not patient or patient['role']!='patient':raise HTTPError(400,'Enter an active patient account email.')
    t=atype(d,f['type']);slot=choose(d,c,t,f['start']);aid=create_appt(d,patient['id'],c,t,slot);sess=checkout(d,aid);a=appointment(d,aid);mail(d,f'desk-{aid}',patient['id'],'Pay your deposit',f"Pay your deposit: {sess['url']}\nYour hold expires {dt(a['hold_until']).astimezone(ZoneInfo(a['timezone']))}.")
    self.page('Deposit payment link',f'<div class="card" data-appointment-id="{aid}"><p>Send this secure link to the patient to pay their deposit.</p><a data-checkout-url="{e(sess["url"])}" href="{e(sess["url"])}">{e(sess["url"])}</a></div>');return
   cs=d.q('SELECT c.id,c.clinic_id,u.full_name AS name FROM clinicians c JOIN users u ON u.id=c.user_id');cs=[c for c in cs if self.u['role']=='admin' or c['clinic_id']==self.u['clinic_id']];ts=d.q('SELECT code,name FROM appointment_types WHERE active')
   self.page('Book for a patient',self.form(p,'desk-book','Create payment link',self.field('patient_email',typ='email')+self.select('clinician_id',cs)+self.select('type',ts,'code')+self.field('start','Start · ISO 8601 with offset')));return
  m=re.fullmatch(r'/admin/clinics/(\d+)',p)
  if m:
   cid=int(m[1]);self.clinic_access(cid,True);c=d.one('SELECT * FROM clinics WHERE id=$1',cid)
   if not c:raise HTTPError(404,'Clinic not found.')
   if self.post:
    D=day(f['on_date'])
    if d.one('SELECT id FROM clinic_closures WHERE clinic_id=$1 AND on_date=$2',cid,D):raise HTTPError(409,'This clinic is already closed on that date.')
    d.q('INSERT INTO clinic_closures(clinic_id,on_date,reason) VALUES($1,$2,$3)',cid,D,f.get('reason',''))
   body='<p>'+e(c['name'])+'</p>'+''.join('<div class="card" data-closure-date="'+e(x['on_date'])+'">'+e(x['on_date'])+' · '+e(x['reason'])+'</div>' for x in d.q('SELECT * FROM clinic_closures WHERE clinic_id=$1 ORDER BY on_date',cid))+self.form(p,'add-closure','Add closure',self.field('on_date',typ='date')+self.field('reason'))
   self.page('Clinic closures',body);return
  if p.startswith('/admin/'):
   self.role('admin');clinics=d.q('SELECT * FROM clinics')
   if p=='/admin/clinicians/new':
    if self.post:
     addr=email(f['email']);nm=name(f['full_name']);cid=int(f['clinic_id']);mx=int(f['max_per_day'])
     if mx<=0:raise ValueError('Daily limit must be positive.')
     if d.one('SELECT id FROM users WHERE lower(trim(email))=$1',addr):raise HTTPError(409,'That email address is already in use.')
     uid=d.one("INSERT INTO users(email,full_name,role,clinic_id) VALUES($1,$2,'clinician',$3) RETURNING id",addr,nm,cid)['id'];cid=d.one('INSERT INTO clinicians(user_id,clinic_id,max_per_day) VALUES($1,$2,$3) RETURNING id',uid,cid,mx)['id'];self.page('Clinician created',f'<div class="card" data-clinician-id="{cid}" data-user-id="{uid}">{e(nm)} has been added. <a href="/clinicians/{cid}/schedule">Set working hours</a></div>');return
    self.page('Add a clinician',self.form(p,'create-clinician','Create clinician',self.field('email',typ='email')+self.field('full_name')+self.select('clinic_id',clinics)+self.field('max_per_day',typ='number')));return
   if p=='/admin/rooms/new':
    if self.post:
     nm=name(f['name']);rid=d.one('INSERT INTO rooms(clinic_id,name,room_type) VALUES($1,$2,$3) RETURNING id',int(f['clinic_id']),nm,name(f['room_type']))['id'];self.page('Room created',f'<div class="card" data-room-id="{rid}">{e(nm)} has been added.</div>');return
    self.page('Add a room',self.form(p,'create-room','Create room',self.select('clinic_id',clinics)+self.field('name')+self.field('room_type')));return
   if p=='/admin/types/new' or re.fullmatch(r'/admin/types/[^/]+',p):
    new=p.endswith('/new');t=None if new else atype(d,urllib.parse.unquote(p.split('/')[-1]))
    if self.post:
     D=day(f['effective_from']);fee=int(f['fee_cents']);bp=int(f['deposit_bp'])
     if fee<=0 or not 0<bp<=10000:raise ValueError('Invalid fee or deposit.')
     if new:
      nm=name(f['name']);code=name(f['code']);dur=int(f['duration_minutes']);buf=int(f['buffer_minutes'])
      if dur<=0 or buf<0:raise ValueError('Invalid duration.')
      if d.one('SELECT id FROM appointment_types WHERE code=$1',code):raise HTTPError(409,'This type code already exists.')
      t=d.one('INSERT INTO appointment_types(code,name,duration_minutes,buffer_minutes,room_type) VALUES($1,$2,$3,$4,$5) RETURNING *',code,nm,dur,buf,name(f['room_type']))
     if d.one('SELECT id FROM type_fees WHERE type_id=$1 AND effective_from=$2',t['id'],D):raise HTTPError(409,'A fee already exists for this effective date.')
     d.q('INSERT INTO type_fees(type_id,effective_from,fee_cents,deposit_bp) VALUES($1,$2,$3,$4)',t['id'],D,fee,bp)
     if new:self.page('Appointment type created','<div class="card" data-type-code="'+e(t['code'])+'">'+e(t['name'])+' has been added.</div>');return
    fields=''
    if new:
     for key in ('code','name','duration_minutes','buffer_minutes','room_type'):fields+=self.field(key,typ='number' if key.endswith('minutes') else 'text')
    for key in ('effective_from','fee_cents','deposit_bp'):fields+=self.field(key,typ='date' if key=='effective_from' else 'number')
    body=''
    if t:
     body='<h2>'+e(t['name'])+'</h2>'+''.join('<p>'+e(x['effective_from'])+' · '+money(x['fee_cents'])+' · '+e(x['deposit_bp']/100)+'% deposit</p>' for x in d.q('SELECT * FROM type_fees WHERE type_id=$1 ORDER BY effective_from',t['id']))
    self.page('Add appointment type' if new else 'Appointment fees',body+self.form(p,'create-type' if new else 'add-fee','Create type' if new else 'Add fee',fields));return
  raise HTTPError(404,'This page could not be found.')
 def times(self,a,b):
  if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',a) or not re.fullmatch(r'(?:(?:[01]\d|2[0-3]):[0-5]\d|24:00)',b) or b<=a:raise ValueError('Invalid time range.')
  return a,b
 def refund_quote(self,a):
  seconds=(a['start']-now()).total_seconds()
  if self.can_desk(a):return a['deposit_cents'],'Staff cancellation returns the full deposit.'
  if seconds>=86400:return a['deposit_cents'],'At least 24 hours notice returns the full deposit.'
  if seconds>=7200:return (a['deposit_cents']+1)//2,'Between 2 and 24 hours notice returns half the deposit.'
  return 0,'Less than 2 hours notice gives no refund.'
 def appt_card(self,a,full=False):
  attrs={'appointment-id':a['id'],'start':local(a).isoformat(),'status':a['status'],'clinician-id':a['clinician_id']}
  if full:attrs.update({'type':a['code'],'room-id':a['room_id'] or '', 'deposit-cents':a['deposit_cents'],'refund-cents':a['refund_cents'],'refund-status':a['refund_status']})
  return '<div class="card" '+ ' '.join('data-'+k+'="'+e(v)+'"' for k,v in attrs.items())+'><div class="row"><h2><a href="/appointments/'+e(a['id'])+'">'+e(local(a).strftime('%Y-%m-%d · %H:%M %Z'))+'</a></h2><span class="tag">'+e(a['status'])+'</span></div><p>'+e(a['clinic_name'])+'</p><p>'+e(a['patient_name'])+' · '+e(a['type_name'])+' ('+e(a['code'])+') · '+e(a['clinician_name'])+'</p></div>'
 def webhook(self):
  try:
   parts=self.headers.get('Stripe-Signature','').split(',');t=next(x[2:] for x in parts if x.startswith('t='));stamp=int(t);sigs=[x[3:] for x in parts if x.startswith('v1=')]
   with open(E['PAYMENTS_WEBHOOK_SIGNING_FILE']) as f:key=f.read().strip().encode()
   signature=hmac.new(key,t.encode()+b'.'+self.raw,hashlib.sha256).hexdigest()
   if abs(time.time()-stamp)>300 or not any(hmac.compare_digest(signature,v) for v in sigs):raise ValueError()
   event=json.loads(self.raw);eid=event['id'];obj=event.get('data',{}).get('object',{})
  except Exception:self.response('Invalid signature',400,'text/plain');return
  with LOCK,DB() as d:
   if not d.one('INSERT INTO hv_events(id) VALUES($1) ON CONFLICT DO NOTHING RETURNING id',eid):self.response('ok',ctype='text/plain');return
   aid=obj.get('metadata',{}).get('appointment_id')
   if aid and str(aid).isdigit() and d.one('SELECT appointment_id FROM hv_appointments WHERE appointment_id=$1',aid):
    a=appointment(d,aid)
    if event['type']=='checkout.session.completed' and obj.get('payment_status')=='paid':
     if a['status']=='held' and dt(a['hold_until'])>now():
      d.q("UPDATE appointments SET status='confirmed' WHERE id=$1",aid);d.q('UPDATE hv_appointments SET payment_intent=$1 WHERE appointment_id=$2',obj.get('payment_intent'),aid);mail(d,f'confirm-{aid}',a['patient_id'],'Appointment confirmed',f"{a['clinic_name']} • {a['clinician_name']} • {local(a):%Y-%m-%d %H:%M %Z}\nDeposit paid: {money(a['deposit_cents'])}.")
     elif a['status'] in ('held','expired','released'):
      if a['status']=='held':d.q("UPDATE appointments SET status='expired' WHERE id=$1",aid);stopped(d,a)
      refund(d,a,int(obj.get('amount_total',a['deposit_cents'])),obj.get('payment_intent'),f'late-{aid}')
    elif event['type']=='refund.created' and a['refund_status']=='pending':
     d.q("UPDATE hv_appointments SET refund_status='refunded' WHERE appointment_id=$1",aid);mail(d,f'refunded-{aid}',a['patient_id'],'Refund issued',f"Your refund of {money(a['refund_cents'])} has been issued.")
  self.response('ok',ctype='text/plain')

def worker():
 registered=False
 while True:
  try:
   if not registered:
    try:api('/v1/webhook_endpoints',{'url':BASE+'/webhooks/payments'});registered=True
    except Exception:pass
   with LOCK,DB() as d:sweep(d)
   with DB() as d:
    expirations=d.q("SELECT h.appointment_id,h.checkout_id FROM hv_appointments h JOIN appointments a ON a.id=h.appointment_id WHERE a.status IN ('expired','released') AND NOT expire_sent AND checkout_id IS NOT NULL")
    refunds=d.q('SELECT * FROM hv_refunds WHERE NOT accepted AND next_try<=now()')
    messages=d.q('SELECT * FROM hv_mail WHERE NOT sent ORDER BY key')
   for x in expirations:
    try:
     api('/v1/checkout/sessions/'+x['checkout_id']+'/expire',{})
     with DB() as d:d.q('UPDATE hv_appointments SET expire_sent=true WHERE appointment_id=$1',x['appointment_id'])
    except Exception:pass
   for r in refunds:
    try:
     api('/v1/refunds',{'payment_intent':r['payment_intent'],'amount':r['amount'],'metadata[appointment_id]':r['appointment_id']},r['key'])
     with DB() as d:d.q('UPDATE hv_refunds SET accepted=true WHERE key=$1',r['key'])
    except Exception:
     with DB() as d:d.q("UPDATE hv_refunds SET next_try=now()+interval '5 seconds' WHERE key=$1",r['key'])
   for m in messages:
    try:
     with DB() as d:user=d.one('SELECT * FROM users WHERE id=$1',m['user_id'])
     send(user,m['subject'],m['body'])
     with DB() as d:d.q('UPDATE hv_mail SET sent=true WHERE key=$1',m['key'])
    except Exception:pass
  except Exception:traceback.print_exc()
  time.sleep(1)

def main():
 while True:
  try:
   with DB() as d:
    for sql in open(os.path.join(os.path.dirname(__file__),'schema.sql')).read().split(';'):
     if sql.strip():d.q(sql)
   break
  except Exception:time.sleep(1)
 threading.Thread(target=worker,daemon=True).start();ThreadingHTTPServer(('0.0.0.0',int(E['PORT'])),Handler).serve_forever()
if __name__=='__main__':main()
