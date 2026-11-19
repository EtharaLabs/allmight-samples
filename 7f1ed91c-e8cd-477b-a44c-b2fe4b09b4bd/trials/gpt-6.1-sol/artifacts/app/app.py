import os,re,json,secrets,hashlib,hmac,time,threading,smtplib,traceback
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlsplit,parse_qs,quote
from http.cookies import SimpleCookie
from email.message import EmailMessage
from email.utils import formataddr
from email.parser import BytesParser
from email import policy
from datetime import date,datetime,timezone
from html import escape as esc
from db import DB,DBError,init
from engine import *

def digest(s):return hashlib.sha256(s.encode()).hexdigest()
def password(p,salt=None):
 salt=salt or secrets.token_hex(16);return salt+':'+hashlib.scrypt(p.encode(),salt=salt.encode(),n=16384,r=8,p=1).hex()
def today():return datetime.now(timezone.utc).date()
def e(x):return esc(str(x if x is not None else ''),quote=True)
def money(c):return ('-' if c<0 else '')+'$'+f'{abs(c)//100:,}.{abs(c)%100:02d}'
def clean(s):return str(s).replace('\r',' ').replace('\n',' ')
class Error(Exception):
 def __init__(self,status,msg):self.status=status;self.msg=msg

def decimal(s,places,positive=False):
 if not re.fullmatch(r'\d+(?:\.\d{1,'+str(places)+'})?',s or ''):raise ValueError('Invalid number (up to '+str(places)+' decimal places)')
 n=num(s)
 if positive and n<=0:raise ValueError('Number must be positive')
 return n

def validdate(s):
 if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',s):raise ValueError('Invalid date')
 return dt(s)
def ratio(s):
 if not re.fullmatch(r'[1-9]\d*:[1-9]\d*',s):raise ValueError('Invalid split ratio')
 n,m=map(int,s.split(':'));v=m
 for p in (2,5):
  while v%p==0:v//=p
 if (n!=1 and m!=1) or v!=1:raise ValueError('Invalid split ratio')
 return n,m

def mail_worker():
 while True:
  try:
   with DB() as d:
    d.q('BEGIN');d.q('SELECT pg_advisory_xact_lock(88002)')
    for r in d.q('SELECT * FROM lb_mail WHERE NOT sent ORDER BY id LIMIT 1'):
     msg=EmailMessage();msg['From']=os.environ['MAIL_FROM'];msg['To']=formataddr((clean(r['recipient_name']),clean(r['recipient']).strip()));msg['Subject']=clean(r['subject']);msg.set_content(r['body'])
     with smtplib.SMTP(os.environ['SMTP_HOST'],int(os.environ['SMTP_PORT']),timeout=8) as s:s.send_message(msg)
     d.q('UPDATE lb_mail SET sent=true WHERE id=$1',r['id'])
    d.q('COMMIT')
  except Exception:pass
  time.sleep(1)

CSS='''*{box-sizing:border-box}body{overflow-wrap:anywhere;margin:0;background:#f4f5f0;color:#193830;font:15px/1.6 system-ui,sans-serif}a{color:#17634b;text-underline-offset:4px}header{background:#173e32;color:white;padding:18px 5%;display:flex;align-items:center;justify-content:space-between;gap:15px;flex-wrap:wrap}header a{color:white;text-decoration:none}header strong{font:28px Georgia,serif}header nav{display:flex;gap:20px;align-items:center;flex-wrap:wrap}main{max-width:1160px;padding:38px 24px 80px;margin:auto}h1{font:42px/1.2 Georgia,serif;letter-spacing:-1px;margin:8px 0 18px}h2{font:25px Georgia,serif;margin:8px 0 18px}h3{margin:6px 0}p{max-width:780px}small,.muted{color:#64776e}.eyebrow{text-transform:uppercase;letter-spacing:2px;font-size:11px;font-weight:700}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr));gap:18px;margin:24px 0}.card,section{background:white;border:1px solid #dce2da;border-radius:8px;padding:24px;margin-bottom:18px}.card{margin:0;border-top:3px solid #c59a50}.card a{font-size:21px}.pill{display:inline-block;border-radius:20px;background:#e9efe6;padding:2px 10px;font-size:12px}.number{font:30px Georgia,serif}.notice{border-left:4px solid #bb934a;padding:16px 20px;background:#fff8e8;margin:22px 0}.error{background:#fff0ed;border-color:#b34937}form{margin:14px 0}.fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,220px),1fr));gap:16px}label{display:block;font-size:13px;font-weight:600;margin-bottom:12px}input,select,textarea{display:block;width:100%;border:1px solid #b8c7bd;border-radius:5px;padding:10px 12px;font:inherit;background:white;color:#193830;margin-top:5px}button{background:#216248;color:white;border:0;border-radius:5px;padding:11px 20px;font:600 14px system-ui;cursor:pointer}header button{background:#35604d;padding:7px 12px}table{border-collapse:collapse;width:100%;font-size:13px}td,th{text-align:left;padding:12px;border-bottom:1px solid #e6eae2;vertical-align:top}th{font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:#64776e}.table-wrap{overflow:auto;max-width:100%}.actions{display:flex;flex-wrap:wrap;gap:16px;margin:22px 0}.auth{max-width:480px;margin:40px auto}.row{padding:15px 0;border-bottom:1px solid #e6eae2;overflow-wrap:anywhere}footer{color:#64776e;margin-top:38px;font-size:12px}a,td,.card{overflow-wrap:anywhere}@media(max-width:500px){main{padding:24px 16px}h1{font-size:32px}section,.card{padding:18px}header{padding:16px}header nav{gap:12px}td,th{padding:8px}.number{font-size:25px}}'''
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def reply(self,status,body='',ctype='text/html; charset=utf-8',headers=None):
  self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('X-Frame-Options','DENY');self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
  for k,v in (headers or {}).items():self.send_header(k,v)
  raw=body.encode() if isinstance(body,str) else body;self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
 def redirect(self,path,cookie=None):self.reply(303,'',headers=dict({'Location':path},**({'Set-Cookie':cookie} if cookie else {})))
 def page(self,title,body,status=200):
  nav='<a href="/signin">Sign in</a>'
  if self.user:
   nav=f'<span>{e(self.user["full_name"])}</span><a href="/">Households</a>'
   if self.user['role'] in ('owner','member','viewer'):nav+='<a href="/profile">Profile</a>'
   nav+=self.form('/signout','', 'signout','Sign out')
  self.reply(status,f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)} · Lotbook</title><style>{CSS}</style></head><body><header><a href="/"><strong>Lotbook<span style="color:#d0ac6b">.</span></strong></a><nav>{nav}</nav></header><main>{body}<footer>LOTBOOK &nbsp; / &nbsp; Household investment records · All report amounts in USD</footer></main></body></html>')
 def form(self,path,body,action,label,multipart=False):return f'<form method="post" action="{e(path)}"'+(' enctype="multipart/form-data"' if multipart else '')+f'><input type="hidden" name="csrf" data-csrf value="{e(self.csrf)}">{body}<button data-action="{action}">{e(label)}</button></form>'
 def inp(self,name,label=None,value='',typ='text'):return f'<label>{e(label or name.replace("_"," ").title())}<input name="{e(name)}" type="{typ}" value="{e(value)}"></label>'
 def select(self,name,options,selected=None):return f'<label>{e(name.replace("_"," ").title())}<select name="{name}">'+''.join(f'<option value="{e(v)}"'+(' selected' if str(v)==str(selected) else '')+f'>{e(l)}</option>' for v,l in options)+'</select></label>'
 def f(self,k,default=''):return self.fields.get(k,[default])[0]
 def auth(self):
  if not self.user:raise Error(401,'Sign in required')
 def access(self,h,write=False):
  self.auth();u=self.user
  if u['household_id']!=str(h) and not (u['role'] in ('advisor','preparer') and self.d.one('SELECT 1 FROM assignments WHERE user_id=$1 AND household_id=$2',u['id'],h)):raise Error(403,'This household is outside your access.')
  if write and u['role'] not in ('owner','member'):raise Error(403,'Your role does not allow this change.')
 def account(self,a,write=False):
  x=self.d.one('SELECT * FROM accounts WHERE id=$1',a)
  if not x:raise Error(404,'Account not found')
  self.access(x['household_id'],write);return x
 def writable(self):return self.user and self.user['role'] in ('owner','member')
 def security(self,en,s):
  if int(s) not in en.secs:raise ValueError('Security must belong to this household')
  return en.secs[int(s)]
 def writebegin(self):self.d.q('BEGIN');self.d.q('SELECT pg_advisory_xact_lock(88001)')
 def nextid(self):return int(self.d.one('SELECT greatest(coalesce((SELECT max(id) FROM legacy_transactions),0),coalesce((SELECT max(id) FROM lb_transactions),0))+1 AS id')['id'])
 def insert(self,t):
  keys=['id','account_id','security_id','kind','trade_date','quantity','amount','split_new','split_old','to_account_id','ref','memo','description','specific'];vals=[t.get(k) for k in keys]
  vals=[dec(v) if isinstance(v,F) else json.dumps(v) if isinstance(v,dict) else v for v in vals]
  self.d.q('INSERT INTO lb_transactions('+','.join(keys)+') VALUES('+','.join('$'+str(i+1) for i in range(len(keys)))+')',*vals)
 def engine(self,h,extra=None,actions=None):return Engine(self.d,h,extra,actions).run()
 def do_GET(self):self.handle_request()
 def do_POST(self):self.handle_request()
 def handle_request(self):
  self.user=None;self.csrf='';self.fields={};self.d=None
  try:
   p=urlsplit(self.path);self.pathonly=p.path;self.query=parse_qs(p.query,keep_blank_values=True)
   if self.pathonly=='/healthz':
    try:
     with DB() as d:d.q('SELECT 1')
     self.reply(200,'ok','text/plain')
    except Exception:self.reply(503,'Database unavailable','text/plain')
    return
   self.d=DB();cookie=SimpleCookie()
   try:cookie.load(self.headers.get('Cookie',''))
   except Exception:pass
   sid=cookie.get('lotbook');self.session_hash=digest(sid.value) if sid else ''
   s=self.d.one('SELECT s.*,u.* FROM lb_sessions s JOIN users u ON u.id=s.user_id WHERE s.hash=$1 AND u.active=true',self.session_hash)
   if s:self.user=s;self.csrf=s['csrf']
   if self.command=='POST':
    length=int(self.headers.get('Content-Length','0'))
    if length>int(os.environ['IMPORT_MAX_BYTES'])+1048576:raise Error(413,'Upload too large')
    raw=self.rfile.read(length)
    if 'multipart/form-data' in self.headers.get('Content-Type',''):
     msg=BytesParser(policy=policy.default).parsebytes(('Content-Type: '+self.headers['Content-Type']+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+raw);self.upload=None
     for part in msg.iter_parts():
      name=part.get_param('name',header='content-disposition');data=part.get_payload(decode=True)
      if name=='file':self.upload=(part.get_filename() or '',data)
      else:self.fields[name]=[data.decode('utf-8')]
    else:self.fields=parse_qs(raw.decode('utf-8'),keep_blank_values=True)
    if self.user and not hmac.compare_digest(self.f('csrf'),self.csrf):raise Error(403,'The form token is invalid. Reload the page and try again.')
   if self.pathonly not in ('/signin','/password-reset') and not self.pathonly.startswith('/password-reset/') and not self.user:
    self.redirect('/signin?next='+quote(self.path,safe=''));return
   self.route()
  except Error as ex:
   if self.d:
    try:self.d.q('ROLLBACK')
    except Exception:pass
   if ex.status==401:self.redirect('/signin?next='+quote(self.path,safe=''))
   else:self.page('Request could not be completed',f'<h1>Request could not be completed</h1><div class="notice error">{e(ex.msg)}</div><a href="/">Return home</a>',ex.status)
  except (ValueError,KeyError,TypeError,OverflowError):
   if self.d:
    try:self.d.q('ROLLBACK')
    except Exception:pass
   self.page('Invalid request','<h1>Invalid request</h1><p>Check the values and try again. Nothing was recorded.</p>',422)
  except Exception:
   if self.d:
    try:self.d.q('ROLLBACK')
    except Exception:pass
   self.page('Unable to complete request','<h1>Unable to complete request</h1><p>Please try again.</p>',500)
  finally:
   if self.d:self.d.close()
 def route(self):
  p=self.pathonly;post=self.command=='POST';d=self.d
  if p=='/password-reset':
   msg=''
   if post:
    self.writebegin();u=d.one('SELECT * FROM users WHERE lower(btrim(email,chr(32)||chr(9)||chr(10)||chr(13)||chr(12)||chr(11)))=lower(btrim($1,chr(32)||chr(9)||chr(10)||chr(13)||chr(12)||chr(11))) AND active=true',self.f('email'))
    if u:
     token=secrets.token_urlsafe(32);d.q('UPDATE lb_resets SET used=true WHERE user_id=$1',u['id']);d.q('INSERT INTO lb_resets(hash,user_id) VALUES($1,$2)',digest(token),u['id'])
     em=EmailMessage();em['From']=os.environ['MAIL_FROM'];em['To']=u['email'].strip();em['Subject']='Set your Lotbook password';em.set_content('Set your password: '+os.environ['PUBLIC_BASE_URL'].rstrip('/')+'/password-reset/'+token)
     with smtplib.SMTP(os.environ['SMTP_HOST'],int(os.environ['SMTP_PORT']),timeout=8) as smtp:smtp.send_message(em)
    d.q('COMMIT');msg='<div class="notice">If that address belongs to an active user, a password link has been sent.</div>'
   self.page('Reset password','<div class="auth"><div class="eyebrow">Welcome to Lotbook</div><h1>Set your password</h1><p>Enter your email to receive a secure, single-use link.</p>'+msg+self.form(p,self.inp('email','Email address',typ='email'),'request-reset','Send password link')+'<a href="/signin">Back to sign in</a></div>');return
  if p.startswith('/password-reset/'):
   token=p.rsplit('/',1)[1];r=d.one('SELECT r.*,u.active FROM lb_resets r JOIN users u ON u.id=r.user_id WHERE hash=$1',digest(token))
   if not r:raise Error(404,'Link not found')
   if post:self.writebegin();r=d.one('SELECT r.*,u.active FROM lb_resets r JOIN users u ON u.id=r.user_id WHERE hash=$1 FOR UPDATE',digest(token))
   valid=r['used']=='f' and r['active']=='t' and (datetime.now(timezone.utc)-datetime.fromisoformat(r['requested_at'])).total_seconds()<int(os.environ['RESET_LINK_TTL_SECONDS'])
   if not valid:raise Error(422,'This link is no longer valid. Request a new password link.')
   if post:
    pw=self.f('password')
    if len(pw)<10:raise Error(422,'Choose a password with at least 10 characters.')
    d.q('INSERT INTO lb_auth(user_id,password_hash) VALUES($1,$2) ON CONFLICT(user_id) DO UPDATE SET password_hash=excluded.password_hash',r['user_id'],password(pw));d.q('UPDATE lb_resets SET used=true WHERE user_id=$1',r['user_id']);d.q('DELETE FROM lb_sessions WHERE user_id=$1',r['user_id']);d.q('COMMIT');self.redirect('/signin');return
   self.page('New password','<div class="auth"><h1>Choose a password</h1><p>Use at least 10 characters.</p>'+self.form('',self.inp('password',typ='password'),'set-password','Set password')+'</div>');return
  if p=='/signin':
   nxt=self.f('next') if post else self.query.get('next',[''])[0];msg=''
   if post:
    u=d.one('SELECT u.*,a.password_hash FROM users u LEFT JOIN lb_auth a ON a.user_id=u.id WHERE lower(btrim(u.email,chr(32)||chr(9)||chr(10)||chr(13)||chr(12)||chr(11)))=lower(btrim($1,chr(32)||chr(9)||chr(10)||chr(13)||chr(12)||chr(11)))',self.f('email'));ok=False
    if u and u['password_hash']:
     ok=hmac.compare_digest(password(self.f('password'),u['password_hash'].split(':')[0]),u['password_hash']) and u['active']=='t'
    if ok:
     sid=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32);d.q('INSERT INTO lb_sessions(hash,user_id,csrf) VALUES($1,$2,$3)',digest(sid),u['id'],csrf)
     valid=nxt.startswith('/') and (len(nxt)<2 or nxt[1] not in '/\\') and not any(ord(c)<32 or ord(c)==127 for c in nxt)
     self.redirect(nxt if valid else '/',f'lotbook={sid}; HttpOnly; SameSite=Lax; Path=/');return
    msg='<div class="notice error">Email or password is incorrect.</div>'
   self.page('Sign in','<div class="auth"><div class="eyebrow">Your household, in balance</div><h1>Welcome back.</h1><p>Your investments. Every lot accounted for.</p>'+msg+self.form(p,self.inp('email','Email address',typ='email')+self.inp('password',typ='password')+f'<input type="hidden" name="next" value="{e(nxt)}">','signin','Sign in')+'<a href="/password-reset">Set or reset your password</a></div>');return
  if p=='/signout' and post:
   d.q('DELETE FROM lb_sessions WHERE hash=$1',self.session_hash);self.redirect('/signin','lotbook=; Max-Age=0; HttpOnly; SameSite=Lax; Path=/');return
  if p=='/profile':
   if self.user['role'] not in ('owner','member','viewer'):raise Error(403,'Your role does not allow profile changes.')
   if post:
    name=self.f('full_name')
    if not 1<=len(name)<=120:raise Error(422,'Display name must contain 1 to 120 characters.')
    d.q('UPDATE users SET full_name=$1 WHERE id=$2',name,self.user['id']);self.redirect('/profile');return
   self.page('Profile','<h1>Your profile</h1><section>'+self.form(p,self.inp('full_name','Display name',self.user['full_name']),'save-profile','Save profile')+'</section>');return
  if p=='/':
   u=self.user;hs=d.q('SELECT * FROM households WHERE id=$1',u['household_id']) if u['household_id'] else d.q('SELECT h.* FROM households h JOIN assignments a ON a.household_id=h.id WHERE a.user_id=$1 ORDER BY h.id',u['id'])
   access={'advisor':'Read-only access to your assigned households.','preparer':'You can read reports and lock tax years for your assigned households.','viewer':'Read-only access to household records.'}.get(u['role'],'Manage accounts, track lots, and prepare your tax years.')
   self.page('Households','<div class="eyebrow">Portfolio workspace</div><h1>A clear view of every lot.</h1><p>'+access+'</p><div class="grid">'+''.join(f'<article class="card" data-household-id="{h["id"]}"><span class="eyebrow">Household</span><h2><a data-household-id="{h["id"]}" href="/households/{h["id"]}">{e(h["name"])}</a></h2><p>Accounts, holdings &amp; tax reports</p><a href="/households/{h["id"]}">Open household →</a></article>' for h in hs)+'</div>');return
  m=re.fullmatch(r'/households/(\d+)(.*)',p)
  if m:self.household(int(m[1]),m[2],post);return
  m=re.fullmatch(r'/accounts/(\d+)(.*)',p)
  if m:self.account_route(int(m[1]),m[2],post);return
  raise Error(404,'Page not found')
 def household(self,h,tail,post):
  d=self.d;self.access(h,post and not tail.endswith('/lock'));house=d.one('SELECT * FROM households WHERE id=$1',h)
  if not house:raise Error(404,'Household not found')
  base=f'/households/{h}';acc=d.q('SELECT * FROM accounts WHERE household_id=$1 ORDER BY id',h);secs=d.q('SELECT * FROM securities WHERE household_id=$1 ORDER BY symbol',h)
  opts=[(x['id'],x['symbol']+' · '+x['name']) for x in secs];aopts=[(x['id'],x['name']) for x in acc];links=f'<a href="{base}">← {e(house["name"])}</a>'
  if tail=='' and not post:
   content=links+'<div class="eyebrow" style="margin-top:22px">Household overview</div><h1>'+e(house['name'])+'</h1>'
   if not self.writable():content+='<p class="notice">'+('Reading and locking tax years.' if self.user['role']=='preparer' else 'Read-only access. You can view all records and download reports.')+'</p>'
   content+=f'<div class="actions"><a href="{base}/securities">Securities</a><a href="{base}/corporate-actions">Corporate actions</a><a href="{base}/transfers">Transfers</a><a href="{base}/unrealized.csv">Download unrealized CSV</a></div><div class="grid">'
   for a in acc:content+=f'<article class="card" data-account-id="{a["id"]}" data-account-type="{a["account_type"]}"><span class="eyebrow">{e(a["broker"])} · {e(a["account_type"])}</span><h2><a href="/accounts/{a["id"]}">{e(a["name"])}</a></h2><p>{e(a["account_number"])} · {e(a["lot_method"])} lot method</p><a href="/accounts/{a["id"]}/lots">View open lots →</a></article>'
   content+='</div><section><h2>Tax years</h2><div class="actions">'+''.join(f'<a href="{base}/reports/{y}">{y}</a>' for y in range(2023,today().year+1))+'</div></section>'
   if self.writable():content+='<section><h2>Add an account</h2>'+self.form(base+'/accounts','<div class="fields">'+self.inp('name')+self.select('broker',[('northwind','Northwind Securities'),('harbor','Harbor Direct')])+self.inp('account_number')+self.select('lot_method',[('FIFO','FIFO'),('HIFO','HIFO')])+self.select('account_type',[('taxable','Taxable'),('ira','IRA')])+'</div>','create-account','Add account')+'</section>'
   self.page(house['name'],content);return
  if tail=='/accounts' and post:
   name=self.f('name');broker=self.f('broker');number=self.f('account_number').strip();method=self.f('lot_method');typ=self.f('account_type') or 'taxable'
   if not 1<=len(name)<=100 or broker not in ('northwind','harbor') or not 1<=len(number)<=40 or method not in ('FIFO','HIFO') or typ not in ('taxable','ira'):raise Error(422,'Check account name, number, broker, type, and lot method.')
   self.writebegin()
   if d.one('SELECT 1 FROM accounts WHERE broker=$1 AND account_number=$2',broker,number):raise Error(409,'This broker and account number are already on file.')
   a=d.one('INSERT INTO accounts(household_id,name,broker,account_number,lot_method,account_type,opened_on) VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING id',h,name,broker,number,method,typ,today());d.q('COMMIT');self.redirect('/accounts/'+a['id']);return
  if tail=='/securities':
   if post:
    symbol=self.f('symbol').strip().upper();isin=self.f('isin').strip().upper();name=self.f('name');currency=self.f('currency')
    if not re.fullmatch('[A-Z0-9.]{1,12}',symbol) or not re.fullmatch('[A-Z]{2}[A-Z0-9]{9}[0-9]',isin) or not 1<=len(name)<=200 or currency not in ('USD','CAD','EUR'):raise Error(422,'Check symbol, ISIN, name, and currency.')
    self.writebegin()
    if d.one('SELECT 1 FROM securities WHERE household_id=$1 AND (upper(symbol)=$2 OR upper(isin)=$3)',h,symbol,isin):raise Error(409,'This symbol or ISIN is already used in this household.')
    d.q('INSERT INTO securities(household_id,symbol,isin,name,currency) VALUES($1,$2,$3,$4,$5)',h,symbol,isin,name,currency);d.q('COMMIT');self.redirect(base+tail);return
   body=links+'<h1>Securities</h1><section>'+''.join(f'<div class="row" data-security-id="{s["id"]}" data-symbol="{e(s["symbol"])}"><strong>{e(s["symbol"])}</strong> · {e(s["name"])}<br><small>{e(s["isin"])} · {s["currency"]}</small></div>' for s in secs)+'</section>'
   if self.writable():body+='<section><h2>Add a security</h2>'+self.form(base+tail,'<div class="fields">'+self.inp('symbol')+self.inp('isin','ISIN')+self.inp('name')+self.select('currency',[(c,c) for c in ('USD','CAD','EUR')])+'</div>','create-security','Add security')+'</section>'
   self.page('Securities',body);return
  if tail=='/transfers':
   if post:
    self.writebegin();en=Engine(d,h);a=int(self.f('from_account_id'));b=int(self.f('to_account_id'));s=int(self.f('security_id'));self.security(en,s)
    if a==b or a not in en.accounts or b not in en.accounts or en.accounts[a]['account_type']!=en.accounts[b]['account_type']:raise Error(422,'Choose two different household accounts of the same account type.')
    day=validdate(self.f('trade_date'));en.rate(en.secs[s]['currency'],day);t=dict(id=self.nextid(),account_id=a,to_account_id=b,security_id=s,kind='transfer',trade_date=day,quantity=F(0),amount=F(0));en.tx.append(t);en.run()
    if en.errors:raise Error(422,'This transfer would leave a transaction short. Nothing was recorded.')
    self.insert(t);d.q('COMMIT');self.redirect(base+tail);return
   body=links+'<h1>Transfer positions</h1><p>A transfer moves every share of a security, retaining lot basis and dates.</p>'
   if self.writable():body+='<section>'+self.form(base+tail,'<div class="fields">'+self.select('from_account_id',aopts)+self.select('to_account_id',aopts)+self.select('security_id',opts)+self.inp('trade_date',value=today(),typ='date')+'</div>','record-transfer','Record transfer')+'</section>'
   body+='<section>'+''.join(f'<div class="row">{e(t["trade_date"])} · {e(t["name"])} → account {e(t["to_account_id"])}</div>' for t in d.q("SELECT t.*,a.name FROM legacy_transactions t JOIN accounts a ON a.id=t.account_id WHERE a.household_id=$1 AND lower(btrim(t.kind))='transfer' UNION ALL SELECT t.id,t.account_id,t.security_id,t.kind,t.trade_date,t.quantity,t.amount,'' AS currency,t.split_new,t.split_old,t.to_account_id,t.ref,t.memo,a.name FROM lb_transactions t JOIN accounts a ON a.id=t.account_id WHERE a.household_id=$1 AND t.kind='transfer' ORDER BY trade_date",h))+'</section>'
   self.page('Transfers',body);return
  if tail=='/corporate-actions':
   if post:
    self.writebegin();en=Engine(d,h);s=int(self.f('security_id'));sec=self.security(en,s);kind=self.f('kind');day=validdate(self.f('action_date'));en.rate(sec['currency'],day);act=dict(id=int(d.one('SELECT coalesce(max(id),0)+1 AS id FROM corporate_actions')['id']),household_id=h,security_id=s,kind=kind,action_date=day)
    def number(k,positive=False,places=6):act[k]=decimal(self.f(k),places,positive)
    def other(k):
     ns=int(self.f(k));ss=self.security(en,ns)
     if ns==s or ss['currency']!=sec['currency']:raise ValueError('Securities must be different and have the same currency')
     act[k]=ns
    if kind=='spinoff':
     other('new_security_id')
     for k in ('ratio','parent_fmv','new_fmv','cash_in_lieu_price'):number(k,True,8 if k=='ratio' else 6)
    elif kind=='return_of_capital':number('amount_per_share',True)
    elif kind=='merger':
     number('shares_per_share',False,8);number('cash_per_share')
     if act['shares_per_share']>0:other('acquirer_security_id');number('acquirer_fmv',True)
     elif self.f('acquirer_security_id') or self.f('acquirer_fmv'):raise ValueError('Cash-only mergers have no acquirer')
     if act['shares_per_share']==0 and act['cash_per_share']==0:raise ValueError('Merger must distribute cash or stock')
    else:raise ValueError('Invalid corporate action')
    en.actions.append(act);en.run()
    if en.errors:raise Error(422,'This action would leave a transaction short.')
    keys=list(act);d.q('INSERT INTO corporate_actions('+','.join(keys)+') VALUES('+','.join('$'+str(i+1) for i in range(len(keys)))+')',*[dec(act[k]) if isinstance(act[k],F) else act[k] for k in keys]);d.q('COMMIT');self.redirect(base+tail);return
   acts=d.q('SELECT c.*,s.symbol FROM corporate_actions c JOIN securities s ON s.id=c.security_id WHERE c.household_id=$1 ORDER BY action_date,c.id',h);body=links+'<h1>Corporate actions</h1><p>Distributions and reorganizations apply across all household accounts.</p><section>'+''.join(f'<div class="row" data-action-id="{a["id"]}" data-kind="{a["kind"]}" data-action-date="{a["action_date"]}" data-symbol="{e(a["symbol"])}"><strong>{e(a["symbol"])}</strong> · {e(a["kind"].replace("_"," "))} · {a["action_date"]}</div>' for a in acts)+'</section>'
   if self.writable():
    fields=self.select('kind',[(k,k.replace('_',' ').title()) for k in ('spinoff','merger','return_of_capital')])+self.select('security_id',opts)+self.inp('action_date',value=today(),typ='date')
    fields+='<p>Spin-off fields</p>'+self.select('new_security_id',[('','Choose security')]+opts)+''.join(self.inp(k) for k in ('ratio','parent_fmv','new_fmv','cash_in_lieu_price'))
    fields+='<p>Merger fields</p>'+self.select('acquirer_security_id',[('','Cash only')]+opts)+''.join(self.inp(k) for k in ('shares_per_share','cash_per_share','acquirer_fmv'))+'<p>Return of capital</p>'+self.inp('amount_per_share')
    body+='<section><h2>Record corporate action</h2><p>Complete only the fields for the selected action.</p>'+self.form(base+tail,'<div class="fields">'+fields+'</div>','record-corporate-action','Record action')+'</section>'
   self.page('Corporate actions',body);return
  if tail=='/unrealized.csv' and not post:self.reply(200,self.engine(h).unrealized(),'text/csv; charset=utf-8');return
  m=re.fullmatch(r'/reports/(\d{4})(.*)',tail)
  if m:
   y=int(m[1]);sub=m[2]
   if not 1900<=y<=9998:raise Error(422,'Invalid year')
   if post and sub=='/lock':
    if self.user['role'] not in ('owner','preparer'):raise Error(403,'Only an owner or assigned preparer can lock a year.')
    self.writebegin();en=self.engine(h);locks=en.locks();L=max(locks) if locks else None
    if date(y,12,31)>=today() or (L is not None and y!=L+1):raise Error(409,'Only the next ended tax year can be locked.')
    years=[y] if L is not None else range(min([y]+[t['trade_date'].year for t in en.tx]+[dt(a['action_date']).year for a in en.actions]),y+1)
    for yy in years:
     summary,realized,f=en.report(yy);d.q('INSERT INTO lb_locks(household_id,year,summary,realized,figures) VALUES($1,$2,$3,$4,$5)',h,yy,summary,realized,json.dumps(f))
    totals=[sum(v[i] for v in f.values()) for i in range(7)];subject=f'Year-end summary {y} — locked by '+self.user['full_name'];body=house['name']+'\n'+f'Year-end summary {y}\n\n'+ '\n'.join(label+': '+money(v) for label,v in zip(['Short-term gains','Long-term gains','Wash-sale loss disallowed','Dividends','Adjustments for earlier years'],totals[:4]+[sum(totals[4:])]))
    for u in d.q("SELECT * FROM users WHERE household_id=$1 AND active=true AND role IN ('owner','member','viewer')",h):d.q('INSERT INTO lb_mail(recipient,recipient_name,subject,body) VALUES($1,$2,$3,$4)',u['email'].strip(),u['full_name'],subject,body)
    d.q('COMMIT');self.redirect(base+f'/reports/{y}');return
   if post:raise Error(404,'Page not found')
   en=self.engine(h);summary,realized,f=en.report(y)
   if sub in ('/summary.csv','/realized.csv'):self.reply(200,summary if sub=='/summary.csv' else realized,'text/csv; charset=utf-8');return
   if sub:raise Error(404,'Page not found')
   locks=en.locks();locked=bool(locks and y<=max(locks));totals=[sum(v[i] for v in f.values()) for i in range(7)];adj=sum(totals[4:]);attrs=f'data-year="{y}" data-locked="{str(locked).lower()}" data-total-short-cents="{totals[0]}" data-total-long-cents="{totals[1]}" data-total-disallowed-cents="{totals[2]}" data-total-dividends-cents="{totals[3]}" data-total-adjustment-cents="{adj}"'
   body=links+f'<div {attrs}><div class="eyebrow" style="margin-top:22px">Tax report · <span class="pill">'+('Locked' if locked else 'Open year')+f'</span></div><h1>{y} year in review</h1><div class="grid">'+''.join(f'<article class="card"><span class="eyebrow">{label}</span><div class="number">{money(v)}</div></article>' for label,v in zip(['Short-term gains','Long-term gains','Wash-sale loss disallowed','Dividends','Adjustments for earlier years'],totals[:4]+[adj]))+'</div></div>'
   body+=f'<div class="actions"><a href="{base}/reports/{y}/summary.csv">Download summary CSV</a><a href="{base}/reports/{y}/realized.csv">Download realized CSV</a></div><section><h2>By security</h2><div class="table-wrap"><table><tr><th>Security</th><th>Short term</th><th>Long term</th><th>Disallowed</th><th>Dividends</th><th>Adjustments</th></tr>'+''.join('<tr><td>'+e(en.secs[s]['symbol'])+'</td>'+''.join('<td>'+money(vv)+'</td>' for vv in v[:4]+[sum(v[4:])])+'</tr>' for s,v in sorted(f.items(),key=lambda kv:en.secs[kv[0]]['symbol']))+'</table></div></section>'
   if locked:body+='<div class="notice">This year is locked. Its reports are frozen. Later changes appear as adjustments in the first unlocked year.</div>'
   elif self.user['role'] in ('owner','preparer') and date(y,12,31)<today() and (not locks or y==max(locks)+1):body+='<section><h2>Close this tax year</h2><p>Locking freezes this year’s reports. Later changes appear as adjustments in the next year. Each active person in the household gets a summary email. This cannot be undone.</p>'+self.form(base+f'/reports/{y}/lock','','lock-year',f'Lock {y}')+'</section>'
   self.page(str(y)+' tax report',body);return
  raise Error(404,'Page not found')
 def account_route(self,a,tail,post):
  acc=self.account(a,post);h=int(acc['household_id']);base=f'/accounts/{a}';d=self.d;en=Engine(d,h);secs=sorted(en.secs.values(),key=lambda s:s['symbol']);opts=[(s['id'],s['symbol']+' · '+s['name']) for s in secs];back=f'<a href="/households/{h}">← Household</a>'
  if tail=='/imports' and post:self.import_file(acc,en);return
  if tail=='/method' and post:
   method=self.f('lot_method');day=validdate(self.f('effective_from'))
   if method not in ('FIFO','HIFO'):raise ValueError()
   self.writebegin();d.q('INSERT INTO lot_method_changes(account_id,effective_from,lot_method) VALUES($1,$2,$3)',a,day,method)
   if self.engine(h).errors:raise Error(422,'This method change would leave a specific-lot transaction short.')
   d.q('COMMIT');self.redirect(base);return
  if tail=='/transactions' and post:
   self.writebegin();en=Engine(d,h);s=int(self.f('security_id'));sec=self.security(en,s);day=validdate(self.f('trade_date'));en.rate(sec['currency'],day);kind=self.f('kind');t=dict(id=self.nextid(),account_id=a,security_id=s,trade_date=day,kind=kind,quantity=F(0),amount=F(0),memo=self.f('memo'))
   if len(t['memo'])>500:raise ValueError()
   if kind in ('buy','reinvest','sell'):
    q=decimal(self.f('quantity'),8,True);pr=decimal(self.f('price'),6);fee=decimal(self.f('fee') or '0',2);amt=F(rnd((q*pr+(-fee if kind=='sell' else fee))*100),100)
    if amt<=0:raise ValueError('Amount must be positive')
    t.update(quantity=q,amount=amt)
    if kind=='sell':
     specific={}
     for k,vs in self.fields.items():
      if k.startswith('lot_') and vs[0] and num(vs[0])!=0:
       lid=int(k[4:]);v=decimal(vs[0],8,True);specific[str(lid)]=dec(v)
     if specific:t['specific']=specific
   elif kind=='dividend':t['amount']=decimal(self.f('amount'),2,True)
   elif kind=='split':t['split_new'],t['split_old']=ratio(self.f('ratio'))
   else:raise ValueError('Invalid transaction kind')
   en.tx.append(t);en.run()
   if en.errors:raise Error(422,'This entry would leave a transaction short or selects unavailable lots. Nothing was recorded.')
   self.insert(t);d.q('COMMIT');self.redirect(base);return
  if post:raise Error(404,'Page not found')
  if tail=='/transactions/new':
   if not self.writable():raise Error(403,'Your role does not allow transactions.')
   kind=self.query.get('kind',['buy'])[0];sid=self.query.get('security_id',[''])[0];en.run();body=back+f'<h1>Record a transaction</h1><p>{e(acc["name"])}</p><section><p>Without chosen lots, the account’s effective lot method ({e(acc["lot_method"])}, FIFO or HIFO) applies. For a specific-lot sale, chosen quantities must add up to the sale quantity.</p>'
   fields='<div class="fields">'+self.select('kind',[(k,k.title()) for k in ('buy','sell','reinvest','dividend','split')],kind)+self.inp('trade_date',value=today(),typ='date')+self.select('security_id',opts,sid)+''.join(self.inp(k,label) for k,label in [('quantity','Quantity'),('price','Price per share'),('fee','Fee (optional)'),('amount','Cash dividend amount'),('ratio','Split ratio (N:M)'),('memo','Memo (optional)')])+'</div>'
   if kind=='sell' and sid:
    self.security(en,sid);groups={}
    for l in en.open(a,int(sid)):groups.setdefault(l['id'],[]).append(l)
    fields+='<h2>Choose specific lots</h2>'
    for lid,ls in groups.items():fields+=self.inp('lot_'+str(lid),f'Lot {lid} · {ls[0]["acq"]} · {dec(sum(l["q"] for l in ls))} shares')
   body+=self.form(base+'/transactions',fields,'record-transaction','Record transaction')+'</section>';self.page('New transaction',body);return
  if tail=='/lots':
   en.run();body=back+f'<h1>Open lots</h1><p>{e(acc["name"])} · Basis and holding periods across your full history.</p>'
   for s in secs:
    ls=en.open(a,int(s['id']))
    if not ls:continue
    body+='<section><h2>'+e(s['symbol'])+' <small>'+e(s['name'])+'</small></h2>'
    if self.writable():body+=f'<a href="{base}/transactions/new?kind=sell&amp;security_id={s["id"]}">Sell / choose lots</a>'
    for l in ls:
     body+=f'<article class="row" data-lot-id="{l["id"]}" data-symbol="{e(s["symbol"])}" data-quantity="{dec(l["q"])}" data-acquired="{l["acq"]}" data-holding-start="{l["start"]}" data-basis-cents="{l["b"]}" data-wash-adjusted="{str(l["wash"]).lower()}"><strong>{dec(l["q"])} shares · {money(l["b"])} basis</strong> <span class="pill">{term(l["start"],today())}-term</span><br>Acquired {l["acq"]} · Holding-period start {l["start"]}'+(' <span class="pill">Basis includes wash-sale disallowed loss</span>' if l['wash'] else '')+'</article>'
    body+='</section>'
   self.page('Open lots',body);return
  if tail=='':
   body=back+f'<div data-account-id="{a}"><div class="eyebrow" style="margin-top:22px">{e(acc["broker"])} · {e(acc["account_type"])}</div><h1>{e(acc["name"])}</h1><p>Account {e(acc["account_number"])} · {e(acc["lot_method"])} lot method</p></div><div class="actions"><a href="{base}/lots">View open lots</a>'
   if self.writable():body+=f'<a href="{base}/transactions/new">Record transaction</a>'
   body+='</div>'
   if self.writable():
    body+='<div class="grid"><section><h2>Import a statement</h2><p>Upload the original broker CSV. Overlapping rows are checked automatically.</p>'+self.form(base+'/imports','<label>Statement file<input type="file" name="file" required></label>','import-statement','Import statement',True)+'</section><section><h2>Change lot method</h2><p>Changes take effect from the date you choose.</p>'+self.form(base+'/method','<div class="fields">'+self.select('lot_method',[('FIFO','FIFO'),('HIFO','HIFO')])+self.inp('effective_from',value=today(),typ='date')+'</div>','change-method','Record method change')+'</section></div>'
   imports=d.q('SELECT * FROM lb_imports WHERE account_id=$1 ORDER BY id DESC',a)
   if imports:body+='<section><h2>Statement imports</h2>'+''.join(f'<div class="row" data-import-filename="{e(x["filename"])}">{e(x["filename"])} <small>{e(x["created_at"])}</small></div>' for x in imports)+'</section>'
   body+='<section><h2>Transaction history</h2><div class="table-wrap"><table><thead><tr><th>Date</th><th>Activity</th><th>Security</th><th>Quantity</th><th>Amount (local)</th><th>Description / reference</th></tr></thead><tbody>'
   for t in sorted([t for t in en.tx if t['account_id']==a],key=lambda t:(t['trade_date'],t['id']),reverse=True):
    sym=en.secs[t['security_id']]['symbol'];body+=f'<tr data-txn-id="{t["id"]}" data-kind="{t["kind"]}" data-trade-date="{t["trade_date"]}" data-symbol="{e(sym)}" data-ref="{e(t.get("ref"))}"><td>{t["trade_date"]}</td><td>{t["kind"]}</td><td>{e(sym)}</td><td>{dec(t["quantity"])}</td><td>{dec(t["amount"])}</td><td>{e(t.get("description") or t.get("memo") or "")}<br><small>{e(t.get("ref"))}</small></td></tr>'
   body+='</tbody></table></div></section>';self.page(acc['name'],body);return
  raise Error(404,'Page not found')
 def import_file(self,acc,en):
  a=int(acc['id']);h=int(acc['household_id']);d=self.d
  if not getattr(self,'upload',None):raise Error(422,'Choose a statement file.')
  filename,raw=self.upload
  if len(raw)>int(os.environ['IMPORT_MAX_BYTES']):raise Error(413,'Statement file exceeds the configured byte limit.')
  problems=[];new=[];duplicates=0
  def reject():
   d.q('ROLLBACK');self.page('Statement rejected',f'<h1>Nothing was imported</h1><section data-import-status="rejected"><p>Correct these problems and upload the statement again.</p>'+''.join(f'<div class="row" data-error-line="{ln}"><strong>Line {ln}</strong>: {e(reason)}</div>' for ln,reason in problems)+f'</section><a href="/accounts/{a}">Back to account</a>',422)
  try:text=raw.decode('utf-8-sig')
  except UnicodeDecodeError:problems.append((0,'File is not UTF-8 text.'));reject();return
  nw=acc['broker']=='northwind';sep=',' if nw else ';';header=('Trade Date,Settlement Date,Activity,Symbol,Description,Quantity,Price,Commission,Net Amount,Currency,Reference' if nw else 'Booking date;Value date;Transaction;ISIN;Name;Shares;Price;Fees;Amount;Currency;Transaction no.').split(sep)
  found=False;number=None;rows=[];reader=csv.reader(io.StringIO(text,newline=''),delimiter=sep,strict=True)
  try:
   while True:
    ln=reader.line_num+1
    try:r=next(reader)
    except StopIteration:break
    if not r or all(not x.strip() for x in r):continue
    if not found:
     prefix='Account Number:' if nw else 'Depot:'
     if r[0].startswith(prefix):number=r[0][len(prefix):].strip()
     if r==header:found=True
     continue
    if nw and (r[0].startswith('Total') or r[0].startswith('End of statement')):continue
    if not nw and r[0]=='':continue
    rows.append((ln,r))
  except csv.Error:problems.append((reader.line_num,'Malformed quoted field.'))
  if not found:problems.append((0,'Header row not found.'))
  if number!=acc['account_number']:problems.append((0,'Account number is missing or does not match.'))
  self.writebegin();en=Engine(d,h);existing={t['ref']:t for t in en.tx if t['account_id']==a and t.get('ref') is not None};nextid=self.nextid()
  mapping={'buy':'buy','sell':'sell','dividend':'dividend','reinvest':'reinvest','split':'split'} if nw else {'purchase':'buy','sale':'sell','dividend':'dividend','dividend reinvestment':'reinvest','split':'split'}
  def parse_number(v,places):
   if nw:
    neg=v.startswith('(') and v.endswith(')');v=v[1:-1] if neg else v
    if not re.fullmatch(r'(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,'+str(places)+'})?',v):raise ValueError('Malformed number')
    return num(v.replace(',',''))*(-1 if neg else 1)
   if not re.fullmatch(r'-?(?:\d+|\d{1,3}(?:\.\d{3})+)(?:,\d{1,'+str(places)+'})?',v):raise ValueError('Malformed number')
   return num(v.replace('.','').replace(',','.'))
  def content(t):return (t['kind'],t['trade_date'],t['security_id'],t['quantity'],t['amount'],t.get('split_new'),t.get('split_old'))
  for ln,r in rows:
   try:
    if len(r)!=11:raise ValueError('Expected 11 fields')
    dateformat='%m/%d/%Y' if nw else '%d.%m.%Y';day=datetime.strptime(r[0],dateformat).date()
    if not re.fullmatch(r'\d{2}/\d{2}/\d{4}' if nw else r'\d{2}\.\d{2}\.\d{4}',r[0]):raise ValueError('Invalid trade date')
    kind=mapping.get(r[2].lower())
    if not kind:raise ValueError('Unknown activity')
    sec=next((s for s in en.secs.values() if s['symbol'].upper()==r[3].strip().upper()),None) if nw else next((s for s in en.secs.values() if s['isin'].upper()==r[3].strip().upper()),None)
    if not sec:raise ValueError('Unknown security')
    if r[9]!=sec['currency']:raise ValueError('Currency does not match security')
    ref=r[10].strip()
    if not 1<=len(ref)<=64:raise ValueError('Reference must have 1 to 64 characters')
    t=dict(id=nextid,account_id=a,security_id=int(sec['id']),kind=kind,trade_date=day,quantity=F(0),amount=F(0),ref=ref,description=r[4],line=ln)
    if kind=='split':
     t['split_new'],t['split_old']=ratio(r[5])
     if r[8] and parse_number(r[8],2)!=0:raise ValueError('Split amount must be zero or empty')
    else:
     amt=parse_number(r[8],2)
     if amt==0 or (kind in ('buy','reinvest') and amt>=0) or (kind in ('sell','dividend') and amt<=0):raise ValueError('Amount has wrong sign or is zero')
     t['amount']=abs(amt)
     if kind=='dividend':
      if r[5]!='':raise ValueError('Dividend quantity must be empty')
     else:
      qty=parse_number(r[5],8)
      if qty==0 or (nw and kind=='sell' and qty>=0) or ((not nw or kind!='sell') and qty<=0):raise ValueError('Quantity has wrong sign or is zero')
      t['quantity']=abs(qty)
    en.rate(sec['currency'],day)
    if ref in existing:
     if content(t)!=content(existing[ref]):raise ValueError('Reference conflicts with a recorded row')
     duplicates+=1
    else:existing[ref]=t;new.append(t);nextid+=1
   except (ValueError,KeyError,OverflowError) as ex:problems.append((ln,str(ex) if str(ex) else 'Invalid value'))
  if problems:reject();return
  en.tx+=new;en.run()
  if en.errors:
   for t in en.errors:problems.append((t.get('line',0),'Transaction would be short on '+str(t['trade_date'])))
   reject();return
  for t in new:self.insert(t)
  d.q('INSERT INTO lb_imports(account_id,filename) VALUES($1,$2)',a,filename);d.q('COMMIT')
  self.page('Statement imported',f'<h1>Statement imported</h1><section data-import-status="imported" data-imported-count="{len(new)}" data-duplicate-count="{duplicates}"><p><strong>{len(new)} rows recorded</strong> · {duplicates} duplicates skipped.</p><p>{e(filename)}</p></section><a href="/accounts/{a}">Back to account</a>')

def bootstrap():
 init()
 with DB() as d:
  d.q('BEGIN');d.q('SELECT pg_advisory_xact_lock(88001)')
  for lock in d.q('SELECT * FROM legacy_year_locks ORDER BY year'):
   h=int(lock['household_id']);y=int(lock['year'])
   if d.one('SELECT 1 FROM lb_locks WHERE household_id=$1 AND year=$2',h,y):continue
   en=Engine(d,h).run();f={int(x['security_id']):[int(x[k]) for k in ('short_term_gain_cents','long_term_gain_cents','wash_disallowed_cents','dividends_cents')]+[0,0,0] for x in d.q('SELECT * FROM legacy_locked_figures WHERE household_id=$1 AND year=$2',h,y)}
   d.q('INSERT INTO lb_locks(household_id,year,summary,realized,figures) VALUES($1,$2,$3,$4,$5)',h,y,en.summary(f),en.realized(y),json.dumps(f))
  d.q('COMMIT')
if __name__=='__main__':
 bootstrap();threading.Thread(target=mail_worker,daemon=True).start();ThreadingHTTPServer(('0.0.0.0',int(os.environ['PORT'])),Handler).serve_forever()
