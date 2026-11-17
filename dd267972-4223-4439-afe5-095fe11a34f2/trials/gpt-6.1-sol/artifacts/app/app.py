import os,json,secrets,hashlib,hmac,time,re,html,threading,smtplib,csv,io,traceback
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from http.cookies import SimpleCookie
from urllib.parse import urlsplit,parse_qs,quote
from email.message import EmailMessage
from email.utils import formataddr
from datetime import datetime,date,timedelta,timezone
from zoneinfo import ZoneInfo,reset_tzpath
from db import DB,migrate
from payroll import *
reset_tzpath([os.path.join(os.path.dirname(__file__),'zoneinfo')])
def esc(v):return html.escape(str(v),quote=True)
def digest(v):return hashlib.sha256(v.encode()).hexdigest()
def salted(v):
 salt=secrets.token_hex(16);return salt+':'+hashlib.pbkdf2_hmac('sha256',v.encode(),bytes.fromhex(salt),160000).hex()
def verify(v,s):
 if not s:return False
 a,b=s.split(':');return hmac.compare_digest(b,hashlib.pbkdf2_hmac('sha256',v.encode(),bytes.fromhex(a),160000).hex())
def hours(s):return f'{s/3600:.1f}'
def money(c):return ('-' if c<0 else '')+f'${abs(c)/100:,.2f}'
def validname(s):return 1<=len(s)<=100 and all(ord(c)>=32 for c in s)
def sendmail(recipient,subject,body):
 m=EmailMessage();m['From']=os.environ['MAIL_FROM'];m['To']=recipient;m['Subject']=subject;m.set_content(body)
 with smtplib.SMTP(os.environ['SMTP_HOST'],int(os.environ['SMTP_PORT']),timeout=10) as smtp:smtp.send_message(m)
def mailworker():
 while True:
  r=None
  try:
   with DB() as d:
    d.q('BEGIN');r=d.one('SELECT * FROM sb_outbox WHERE sent=false ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1')
    if r:
     sendmail(r['recipient'],r['subject'],r['body']);d.q('UPDATE sb_outbox SET sent=true WHERE id=$1',r['id'])
    d.q('COMMIT')
  except Exception:r=None
  if not r:time.sleep(.4)
class Problem(Exception):
 def __init__(self,status,message):self.status=status;self.message=message
class App(BaseHTTPRequestHandler):
 server_version='Shiftbook'
 def log_message(self,*a):pass
 def do_GET(self):self.handle_request()
 def do_POST(self):self.handle_request()
 def do_PUT(self):self.handle_request()
 def do_DELETE(self):self.handle_request()
 def respond(self,body='',status=200,kind='text/html; charset=utf-8',location=None):
  self.output=(body,status,kind,location)
 def flush_response(self):
  if not hasattr(self,'output'):return
  body,status,kind,location=self.output
  self.send_response(status);self.send_header('Content-Type',kind);self.send_header('X-Frame-Options','DENY');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','same-origin');self.send_header('Cache-Control','no-store')
  if location:self.send_header('Location',location)
  for c in getattr(self,'cookies',[]):self.send_header('Set-Cookie',c)
  b=body.encode() if isinstance(body,str) else body;self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def redirect(self,path):return self.respond('',303,location=path)
 def fail(self,status,message):raise Problem(status,message)
 def form(self,action,content,button,label):return f'<form method="post" action="{esc(action)}"><input type="hidden" name="csrf" data-csrf value="{esc(self.session["csrf"])}">{content}<button data-action="{button}">{esc(label)}</button></form>'
 def field(self,name,label,typ='text',value='',required=True):return f'<label>{esc(label)}<input name="{esc(name)}" type="{typ}" value="{esc(value)}" {"required" if required else ""}></label>'
 def page(self,title,body,status=200):
  nav='<a href="/signin">Sign in</a>'
  if self.user:
   nav='<a href="/timesheet">My timesheet</a><a href="/profile">Profile</a>'
   if self.user['role'] in ('manager','payroll_admin'):nav+='<a href="/approvals">Approvals</a>'
   if self.user['role']=='payroll_admin':nav+='<a href="/admin/employees/new">Staff</a><a href="/admin/devices">Time clocks</a><a href="/payroll">Payroll</a>'
   nav+=self.form('/signout','','signout','Sign out')
  self.respond('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+esc(title)+' · Shiftbook</title><style>'+CSS+'</style><header><a class="brand" href="/timesheet">◷ Shiftbook</a><span>RESTAURANT OPERATIONS</span></header><nav>'+nav+'</nav><main><div class="eyebrow">TIME & PAY</div><h1>'+esc(title)+'</h1>'+body+'</main><footer>Shiftbook · Mission St / Roosevelt Row / Wicker Park</footer></html>',status)
 def handle_request(self):
  self.cookies=[];self.user=None;self.session=None;self.db=None
  try:
   u=urlsplit(self.path);self.pathpart=u.path;self.query={k:v[0] for k,v in parse_qs(u.query,keep_blank_values=True).items()}
   if self.pathpart=='/healthz':
    try:
     with DB() as d:d.q('SELECT 1')
     return self.respond('ok',200,'text/plain')
    except Exception:return self.respond('Database unavailable',503,'text/plain')
   self.db=DB();d=self.db;self.ispost=self.command=='POST';self.data={}
   if self.ispost:
    n=int(self.headers.get('Content-Length','0'))
    if n<0 or n>8000000:self.fail(400,'Request is too large. Reduce the request and try again.')
    raw=self.rfile.read(n)
    if self.pathpart=='/api/punches':return self.punchapi(raw)
    try:self.data={k:v[0] for k,v in parse_qs(raw.decode(),keep_blank_values=True,max_num_fields=100).items()}
    except Exception:self.fail(400,'Invalid form. Reload the page and try again.')
   if self.command not in ('GET','POST'):self.fail(405,'Method not allowed.')
   c=SimpleCookie()
   try:c.load(self.headers.get('Cookie',''))
   except Exception:pass
   sid=c['sb_session'].value if 'sb_session' in c else ''
   self.session=d.one('SELECT * FROM sb_sessions WHERE hash=$1',digest(sid)) if sid else None
   if self.session and self.session['employee_id']:
    self.user=d.one('SELECT * FROM employees WHERE id=$1 AND active=true',self.session['employee_id'])
   public=self.pathpart in ('/signin','/password-reset') or self.pathpart.startswith('/password-reset/')
   if not public and not self.user:return self.redirect('/signin?next='+quote(self.path,safe=''))
   if not self.session:self.newsession()
   if self.ispost:
    if not hmac.compare_digest(self.data.get('csrf',''),self.session['csrf']):self.fail(403,'Security token is missing or invalid. Reload the page and try again.')
    d.q('BEGIN');d.q("SET LOCAL statement_timeout='110000ms'");d.q('SELECT pg_advisory_xact_lock(827413)')
   result=self.route()
   if self.ispost:d.q('COMMIT')
   if getattr(self,'resetmail',None):sendmail(*self.resetmail)
   return result
  except Problem as e:
   if self.db:
    try:self.db.q('ROLLBACK')
    except Exception:pass
   self.page('Unable to complete request','<div class="notice">'+esc(e.message)+'</div><p><a href="javascript:history.back()">Return to the previous page</a></p>',e.status)
  except (ValueError,TypeError,OverflowError,UnicodeError):
   if self.db:
    try:self.db.q('ROLLBACK')
    except Exception:pass
   self.respond('Invalid request. Check the fields and try again.',400,'text/plain')
  except Exception:
   if self.db:
    try:self.db.q('ROLLBACK')
    except Exception:pass
   self.respond('Unable to complete this request. Please try again.',500,'text/plain')
  finally:
   if self.db:self.db.close()
   self.flush_response()
 def newsession(self,eid=None):
  token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32)
  self.db.q('INSERT INTO sb_sessions(hash,employee_id,csrf) VALUES($1,$2,$3)',digest(token),eid,csrf)
  self.session={'hash':digest(token),'employee_id':eid,'csrf':csrf};self.cookies.append('sb_session='+token+'; Path=/; HttpOnly; SameSite=Lax'+('; Secure' if os.environ['PUBLIC_BASE_URL'].startswith('https:') else ''))
 def admin(self):
  if self.user['role']!='payroll_admin':self.fail(403,'Payroll administrator access is required.')
 def employee(self,eid):
  e=self.db.one('SELECT e.*,l.name location,l.timezone,l.ruleset FROM employees e JOIN locations l ON e.location_id=l.id WHERE e.id=$1',eid)
  if not e:self.fail(404,'Employee not found.')
  return e
 def canapprove(self,eid):
  if str(eid)==self.user['id']:return False
  if self.user['role']=='payroll_admin':return True
  if self.user['role']!='manager':return False
  return bool(self.db.one('WITH RECURSIVE tree AS (SELECT id,manager_id,ARRAY[id] path FROM employees WHERE id=$1 UNION ALL SELECT e.id,e.manager_id,t.path||e.id FROM employees e JOIN tree t ON e.id=t.manager_id WHERE NOT e.id=ANY(t.path)) SELECT id FROM tree WHERE manager_id=$2 LIMIT 1',eid,self.user['id']))
 def route(self):
  p=self.pathpart;d=self.db;f=self.data;post=self.ispost
  if p=='/':return self.redirect('/timesheet')
  if p=='/signin':
   nxt=f.get('next',self.query.get('next','/timesheet'))
   if post:
    e=d.one('SELECT * FROM employees WHERE lower(btrim(email))=$1',f.get('email','').strip().lower());msg='Email or password is incorrect.'
    if e and e['active']=='t':
     d.q('INSERT INTO sb_auth(employee_id) VALUES($1) ON CONFLICT DO NOTHING',e['id']);a=d.one('SELECT *,locked_until>now() locked FROM sb_auth WHERE employee_id=$1 FOR UPDATE',e['id'])
     if a['locked']=='t':msg='The account is temporarily locked. Try again after the lockout period.'
     elif verify(f.get('password',''),a['password_hash']):
      d.q('UPDATE sb_auth SET failures=0,locked_until=NULL WHERE employee_id=$1',e['id']);d.q('DELETE FROM sb_sessions WHERE hash=$1',self.session['hash']);self.newsession(e['id'])
      safe=nxt.startswith('/') and not nxt.startswith('//') and '\\' not in nxt and not any(ord(c)<32 for c in nxt)
      return self.redirect(nxt if safe else '/timesheet')
     else:
      failures=1 if a['locked_until'] and datetime.fromisoformat(a['locked_until'])<=datetime.now(UTC) else int(a['failures'])+1
      d.q("UPDATE sb_auth SET failures=$2,locked_until=CASE WHEN $2::integer>=5 THEN now()+$3::integer*interval '1 second' ELSE NULL END WHERE employee_id=$1",e['id'],failures,os.environ['LOGIN_LOCKOUT_SECONDS'])
      if failures>=5:msg='The account is temporarily locked. Try again after the lockout period.'
    body='<div class="notice">'+esc(msg)+'</div>'
   else:body='<p class="lead">Welcome back. Sign in to review your hours.</p>'
   return self.page('Sign in',body+self.form('/signin',self.field('email','Email','email')+self.field('password','Password','password')+f'<input type="hidden" name="next" value="{esc(nxt)}">','signin','Sign in')+'<p><a href="/password-reset">Set or reset your password</a></p>')
  if p=='/password-reset':
   if post:
    e=d.one('SELECT * FROM employees WHERE lower(btrim(email))=$1 AND active=true',f.get('email','').strip().lower())
    if e:
     token=secrets.token_urlsafe(32);d.q('UPDATE sb_resets SET replaced=true WHERE employee_id=$1',e['id']);d.q('INSERT INTO sb_resets(hash,employee_id) VALUES($1,$2)',digest(token),e['id']);self.resetmail=(formataddr((e['full_name'],e['email'])),'Set your Shiftbook password','Set your password: '+os.environ['PUBLIC_BASE_URL'].rstrip('/')+'/password-reset/'+token)
    return self.page('Check your email','<p>If that address belongs to an active employee, a password reset link has been sent. Check your inbox.</p><a href="/signin">Return to sign in</a>')
   return self.page('Set or reset password','<p>Enter your work email to receive a single-use password link.</p>'+self.form(p,self.field('email','Email','email'),'request-reset','Send reset link'))
  if p.startswith('/password-reset/'):
   token=p.split('/')[-1];r=d.one("SELECT *, requested_at + $2::integer*interval '1 second' > now() valid FROM sb_resets WHERE hash=$1",digest(token),os.environ['RESET_LINK_TTL_SECONDS'])
   if not r:self.fail(404,'Reset link not found. Request a new password reset link.')
   if r['used']=='t' or r['replaced']=='t' or r['valid']!='t':return self.page('Link no longer valid','<p>This link is no longer valid. <a href="/password-reset">Request a new link</a>.</p>',400)
   if post:
    if len(f.get('password',''))<10:self.fail(400,'Use a password with at least 10 characters and try again.')
    d.q('INSERT INTO sb_auth(employee_id,password_hash) VALUES($1,$2) ON CONFLICT(employee_id) DO UPDATE SET password_hash=excluded.password_hash,failures=0,locked_until=NULL',r['employee_id'],salted(f['password']));d.q('UPDATE sb_resets SET used=true WHERE hash=$1',digest(token));d.q('DELETE FROM sb_sessions WHERE employee_id=$1',r['employee_id']);return self.redirect('/signin')
   return self.page('Choose a password',self.form(p,self.field('password','New password (at least 10 characters)','password'),'set-password','Set password'))
  if p=='/signout' and post:
   d.q('DELETE FROM sb_sessions WHERE hash=$1',self.session['hash']);self.cookies.append('sb_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax');return self.redirect('/signin')
  if p=='/profile':
   message=''
   if post:
    name=f.get('full_name','')
    if not validname(name):self.fail(400,'Display name must contain 1–100 characters with no control characters. Edit the name and try again.')
    d.q('UPDATE employees SET full_name=$1 WHERE id=$2',name,self.user['id']);self.user['full_name']=name;message='<p class="notice">Display name saved.</p>'
   return self.page('Your profile',message+self.form(p,self.field('full_name','Display name',value=self.user['full_name']),'save-profile','Save display name'))
  if p=='/timesheet' or re.fullmatch(r'/employees/\d+/timesheet',p):
   eid=self.user['id'] if p=='/timesheet' else p.split('/')[2]
   if eid!=self.user['id'] and not self.canapprove(eid):self.fail(403,'You do not have access to this timesheet.')
   e=self.employee(eid);ws=self.query.get('week',sunday(datetime.now(ZoneInfo(e['timezone'])).date()).isoformat());wd=day(ws)
   if wd!=sunday(wd):self.fail(400,'Choose a Sunday as the week start.')
   ids,weeks=allweeks(d,eid);w=weeks[int(eid)].get(ws,emptyweek(ws));return self.timesheet(e,ws,w)
  m=re.fullmatch(r'/employees/(\d+)/weeks/(\d{4}-\d{2}-\d{2})/approve',p)
  if m and post:
   eid,ws=m.groups()
   if not self.canapprove(eid):self.fail(403,'You cannot approve this employee’s week.')
   _,ww=allweeks(d,eid);w=ww.get(int(eid),{}).get(ws)
   if not w or w['status']!='pending' or not w['reg']+w['ot']+w['dt']:self.fail(400,'This week cannot be approved. It must have worked time, no exceptions, and be pending. Review the timesheet.')
   d.q('INSERT INTO sb_approvals(employee_id,week_start,approved,approved_by) VALUES($1,$2,true,$3) ON CONFLICT(employee_id,week_start) DO UPDATE SET approved=true,approved_by=excluded.approved_by',eid,ws,self.user['id']);return self.redirect(f'/employees/{eid}/timesheet?week={ws}')
  if p=='/approvals':
   if self.user['role'] not in ('manager','payroll_admin'):self.fail(403,'Manager access is required.')
   ids,ww=allweeks(d);body='<p>Review hours before approving. Approval makes a week payable in the next run. Later punch or pay changes return it to pending.</p>'
   allowed={eid for eid in ids if self.canapprove(eid)}
   for eid in allowed:
    for ws,w in sorted(ww[eid].items()):
     if w['status']=='pending' and w['reg']+w['ot']+w['dt']:body+=f'<article data-employee-id="{eid}" data-week-start="{ws}"><a href="/employees/{eid}/timesheet?week={ws}">{esc(ids[eid]["full_name"])} · {ws}</a><span>{hours(w["reg"]+w["ot"]+w["dt"])} hours · pending</span></article>'
   return self.page('Awaiting approval',body)
  if p.startswith('/admin/'):
   self.admin();return self.adminroute()
  if p.startswith('/payroll'):
   self.admin();return self.payrollroute()
  self.fail(404,'Page not found. Use the navigation to continue.')
 def timesheet(self,e,ws,w):
  eid=e['id'];end=day(ws)+timedelta(days=6)
  body=f'<section data-employee-id="{eid}" data-week-start="{ws}" data-status="{w["status"]}"><p class="lead">{esc(e["full_name"])} · {esc(e["location"])}</p><div class="weeknav"><a href="?week={day(ws)-timedelta(days=7)}">← Previous</a><strong>{ws} — {end}</strong><a href="?week={day(ws)+timedelta(days=7)}">Next →</a></div><p class="badge">{w["status"].upper()}</p>'
  for err in w['errors']:body+='<p class="notice">'+esc(err)+'</p>'
  for dt,vals in w['days'].items():
   body+=f'<article class="day" data-day="{dt}" data-reg-hours="{hours(vals[0])}" data-ot-hours="{hours(vals[1])}" data-dt-hours="{hours(vals[2])}"><strong>{day(dt).strftime("%a, %b %d")}</strong><span>REG {hours(vals[0])}</span><span>OT {hours(vals[1])}</span><span>DT {hours(vals[2])}</span></article>'
  body+=f'<p><strong>Week total:</strong> REG {hours(w["reg"])} · OT {hours(w["ot"])} · DT {hours(w["dt"])} hours</p><p>Gross: {money(w["gross_cents"])} · Meal premiums: {w["meals"]}</p>'
  if self.canapprove(eid) and w['status']=='pending' and w['reg']+w['ot']+w['dt']:
   body+='<div class="notice">Approving makes this week payable in the next payroll run. Later changes to punches or pay return it to pending.</div>'+self.form(f'/employees/{eid}/weeks/{ws}/approve','','approve-week','Approve week')
  return self.page('Weekly timesheet',body+'</section>')
 def select(self,name,label,rows,empty=False):
  return '<label>'+esc(label)+'<select name="'+name+'">'+('<option value="">None</option>' if empty else '')+''.join('<option value="'+esc(k)+'">'+esc(v)+'</option>' for k,v in rows)+'</select></label>'
 def ratefields(self):return self.field('job_code','Job code')+self.field('rate_cents','Hourly rate (cents)','number')+self.field('effective_from','Effective date','date')
 def ratevalues(self):
  f=self.data;j=f.get('job_code','');r=int(f.get('rate_cents','0'));dt=day(f.get('effective_from',''))
  if not j or len(j)>100 or any(ord(c)<32 for c in j) or r<=0 or r>2147483647:self.fail(400,'Enter a valid job code and a positive hourly rate in cents.')
  return j,r,dt
 def adminroute(self):
  p=self.pathpart;d=self.db;f=self.data;post=self.ispost
  if p=='/admin/employees/new':
   if post:
    email=f.get('email','').strip().lower();name=f.get('full_name','')
    if not validname(name):self.fail(400,'Display name must have 1–100 characters and no control characters. Edit the name and try again.')
    if not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+',email):self.fail(400,'Enter a valid email address.')
    if d.one('SELECT id FROM employees WHERE lower(btrim(email))=$1',email):self.fail(400,'That email address is already on file. Use a different address.')
    loc=f.get('location_id','');mgr=f.get('manager_id') or None;role=f.get('role','');job,rate,dt=self.ratevalues()
    if not d.one('SELECT id FROM locations WHERE id=$1',int(loc)) or (mgr and not d.one('SELECT id FROM employees WHERE id=$1',int(mgr))) or role not in ('employee','manager','payroll_admin'):self.fail(400,'Choose a valid location, manager, and role.')
    e=d.one('INSERT INTO employees(email,full_name,location_id,manager_id,role,active,hired_on) VALUES($1,$2,$3,$4,$5,true,$6) RETURNING id',email,name,loc,mgr,role,dt)
    d.q('INSERT INTO job_rates(employee_id,job_code,rate_cents,effective_from) VALUES($1,$2,$3,$4)',e['id'],job,rate,dt);return self.redirect('/admin/employees/'+e['id'])
   body=self.field('email','Email','email')+self.field('full_name','Full name')+self.select('location_id','Location',[(r['id'],r['name']) for r in d.q('SELECT * FROM locations ORDER BY id')])+self.select('manager_id','Manager',[(r['id'],r['full_name']) for r in d.q("SELECT * FROM employees WHERE active=true ORDER BY full_name")],True)+self.select('role','Role',[(x,x.replace('_',' ').title()) for x in ['employee','manager','payroll_admin']])+self.ratefields()
   directory='<h2>Staff directory</h2>'+''.join(f'<article><a href="/admin/employees/{e["id"]}">{esc(e["full_name"])}</a><small>{esc(e["email"])}</small></article>' for e in d.q('SELECT id,full_name,email FROM employees ORDER BY full_name'))
   return self.page('Add an employee',self.form(p,body,'create-employee','Create employee')+directory)
  m=re.fullmatch(r'/admin/employees/(\d+)(/rates)?',p)
  if m:
   eid,ratepath=m.groups();e=self.employee(eid)
   if ratepath:
    if not post:self.fail(405,'Submit a new rate from the employee page.')
    job,rate,dt=self.ratevalues()
    if d.one('SELECT id FROM job_rates WHERE employee_id=$1 AND job_code=$2 AND effective_from=$3',eid,job,dt):self.fail(400,'A rate already exists for that job and effective date. Choose a different effective date.')
    _,before=allweeks(d,eid);d.q('INSERT INTO job_rates(employee_id,job_code,rate_cents,effective_from) VALUES($1,$2,$3,$4)',eid,job,rate,dt);_,after=allweeks(d,eid);invalidate(d,eid,before[int(eid)],after[int(eid)]);return self.redirect('/admin/employees/'+eid)
   body=f'<section data-employee-id="{eid}"><p class="lead">{esc(e["email"])} · {esc(e["role"])} · {esc(e["location"])}</p><a href="/employees/{eid}/timesheet">View timesheet</a><h2>Job rates</h2>'
   for r in d.q('SELECT * FROM job_rates WHERE employee_id=$1 ORDER BY job_code,effective_from',eid):body+=f'<article>{esc(r["job_code"])} · {money(int(r["rate_cents"]))}/hour · effective {r["effective_from"]}</article>'
   body+='<h2>Add a rate</h2><p>Rates are permanent. An applicable new rate returns affected approved weeks to pending.</p>'+self.form(p+'/rates',self.ratefields(),'add-rate','Add rate')+'</section>'
   return self.page(e['full_name'],body)
  if p=='/admin/devices':
   secret=''
   if post:
    label=f.get('label','');loc=int(f.get('location_id','0'))
    if not validname(label) or not d.one('SELECT id FROM locations WHERE id=$1',loc):self.fail(400,'Enter a label and choose a valid location.')
    key=secrets.token_urlsafe(32);dev=d.one('INSERT INTO sb_devices(location_id,label,key_hash) VALUES($1,$2,$3) RETURNING id',loc,label,salted(key));secret=f'<section class="notice" data-device-id="{dev["id"]}"><h2>Save this key now</h2><p>It is shown only once.</p><code data-device-key="{key}">{key}</code></section>'
   body=secret+self.form(p,self.select('location_id','Location',[(r['id'],r['name']) for r in d.q('SELECT * FROM locations ORDER BY id')])+self.field('label','Clock label'),'create-device','Create time clock')
   for r in d.q('SELECT v.*,l.name location FROM sb_devices v JOIN locations l ON l.id=v.location_id ORDER BY v.id'):
    body+=f'<article data-device-id="{r["id"]}"><strong>{esc(r["label"])}</strong><p>{esc(r["location"])} · {"Revoked" if r["revoked"]=="t" else "Active"}</p>'+(self.form('/admin/devices/'+r['id']+'/revoke','','revoke-device','Revoke clock') if r['revoked']=='f' else '')+'</article>'
   return self.page('Time clocks',body)
  m=re.fullmatch(r'/admin/devices/(\d+)/revoke',p)
  if m and post:
   if not d.one('SELECT id FROM sb_devices WHERE id=$1',m[1]):self.fail(404,'Time clock not found.')
   d.q('UPDATE sb_devices SET revoked=true WHERE id=$1',m[1]);return self.redirect('/admin/devices')
  self.fail(404,'Page not found.')
 def payrollroute(self):
  d=self.db;p=self.pathpart;post=self.ispost;f=self.data
  if p=='/payroll':
   body='<p class="lead">Review two weeks of hours, held weeks, and earlier adjustments.</p>'+self.form('/payroll/runs',self.field('period_start','Pay period starts (Sunday)','date'),'create-run','Create or recompute draft')
   for r in d.q('SELECT id,period_start,status FROM sb_runs ORDER BY period_start DESC'):body+=f'<article><a href="/payroll/runs/{r["id"]}">{r["period_start"]} · {r["status"]}</a></article>'
   return self.page('Payroll',body)
  if p=='/payroll/runs' and post:
   period=day(f.get('period_start',''))
   if (period-ANCHOR).days%14 or period<ANCHOR:self.fail(400,'Choose a pay period Sunday: 2026-09-20 or every 14 days after it.')
   r=d.one('SELECT * FROM sb_runs WHERE period_start=$1 FOR UPDATE',period)
   if r and r['status']=='finalized':self.fail(409,'This pay period is finalized. Open its locked run; a new draft cannot be created.')
   snap=run_snapshot(d,period)
   if r:d.q('UPDATE sb_runs SET snapshot=$1,version=version+1 WHERE id=$2',json.dumps(snap),r['id'])
   else:r=d.one('INSERT INTO sb_runs(period_start,snapshot) VALUES($1,$2) RETURNING id',period,json.dumps(snap))
   return self.redirect('/payroll/runs/'+r['id'])
  m=re.fullmatch(r'/payroll/runs/(\d+)(/finalize|/export.csv)?',p)
  if m:
   rid,suffix=m.groups();r=d.one('SELECT * FROM sb_runs WHERE id=$1'+(' FOR UPDATE' if post else ''),rid)
   if not r:self.fail(404,'Payroll run not found.')
   snap=json.loads(r['snapshot']);period=day(r['period_start']);end=period+timedelta(days=13)
   if suffix=='/finalize':
    if not post:self.fail(405,'Finalize using the form on the run page.')
    if r['status']!='draft':self.fail(409,'This run is already finalized. View the locked run.')
    if f.get('version')!=r['version'] or run_snapshot(d,period)!=snap:self.fail(409,'The draft is out of date. Recompute it from Payroll, review the updated lines, then finalize again.')
    d.q("UPDATE sb_runs SET status='finalized' WHERE id=$1",rid)
    for line in snap['lines']:
     body=f'Pay statement\nPeriod: {period} through {end}\nREG hours: {hours(line["reg"])}\nOT hours: {hours(line["ot"])}\nDT hours: {hours(line["dt"])}\nGross pay: {money(line["gross_cents"])}\n'
     d.q('INSERT INTO sb_outbox(run_id,employee_id,recipient,subject,body) VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING',rid,line['employee_id'],formataddr((line['full_name'],line['email'])),'Pay statement · '+str(period),body)
    return self.redirect('/payroll/runs/'+rid)
   if suffix=='/export.csv':
    out=io.StringIO(newline='');writer=csv.writer(out,lineterminator='\r\n');writer.writerow('employee_id,full_name,location,reg_hours,ot_hours,dt_hours,meal_premiums,straight_cents,ot_premium_cents,dt_premium_cents,meal_cents,adjustment_cents,gross_cents'.split(','))
    def safe(s):return "'"+s if s and s[0] in '=+-@\t\r' else s
    for line in snap['lines']:writer.writerow([line['employee_id'],safe(line['full_name']),safe(line['location']),hours(line['reg']),hours(line['ot']),hours(line['dt']),line['meals']]+[line[k] for k in ['straight_cents','ot_premium_cents','dt_premium_cents','meal_cents','adjustment_cents','gross_cents']])
    return self.respond(out.getvalue(),kind='text/csv; charset=utf-8')
   body=f'<section data-run-id="{rid}" data-run-status="{r["status"]}" data-period-start="{period}" data-version="{r["version"]}"><p class="lead">{period} — {end}</p><span class="badge">{r["status"].upper()} · Version {r["version"]}</span><p><a href="/payroll/runs/{rid}/export.csv">Download CSV</a></p><h2>Pay statements</h2>'
   for l in snap['lines']:
    body+=f'<article data-line-employee-id="{l["employee_id"]}" data-gross-cents="{l["gross_cents"]}"><h3>{esc(l["full_name"])}</h3><p>{esc(l["location"])}</p><p>REG {hours(l["reg"])} · OT {hours(l["ot"])} · DT {hours(l["dt"])} hours · {l["meals"]} meal premiums</p><strong>{money(l["gross_cents"])}</strong>'
    for adj in l['adjustments']:body+=f'<p class="notice">Earlier week {adj["week_start"]}: adjustment {money(adj["cents"])}</p>'
    body+='</article>'
   body+='<h2>Held weeks</h2><p>Held weeks are excluded from pay. Resolve exceptions and obtain approval, then recompute the draft.</p>'
   for h in snap['held']:body+=f'<article data-held-employee-id="{h["employee_id"]}" data-week-start="{h["week_start"]}"><a href="/employees/{h["employee_id"]}/timesheet?week={h["week_start"]}">{esc(h["full_name"])} · {h["week_start"]}</a><p>{esc(h["reason"])}</p></article>'
   if r['status']=='draft':body+='<div class="notice">Finalizing locks this run and emails pay statements. It cannot be undone. Review the lines and held weeks before continuing.</div>'+self.form(p+'/finalize',f'<input type="hidden" name="version" value="{r["version"]}">','finalize-run','Finalize and email statements')
   else:body+='<p>This run is locked. Pay statements have been queued for delivery.</p>'
   return self.page('Payroll run',body+'</section>')
  self.fail(404,'Payroll page not found.')
 def punchapi(self,raw):
  d=self.db;auth=self.headers.get('Authorization','');key=auth[7:] if auth.startswith('Bearer ') else '';dev=None
  if key:
   for r in d.q('SELECT * FROM sb_devices WHERE revoked=false'):
    if verify(key,r['key_hash']):dev=r;break
  if not dev:return self.respond(json.dumps({'error':'Invalid or revoked time clock key'}),401,'application/json')
  try:
   data=json.loads(raw)
   if not isinstance(data,dict) or not isinstance(data.get('punches'),list) or len(data['punches'])>5000:raise ValueError()
  except Exception:return self.respond(json.dumps({'error':'Expected a JSON object with at most 5000 punches'}),400,'application/json')
  d.q('BEGIN');d.q("SET LOCAL statement_timeout='110000ms'");d.q('SELECT pg_advisory_xact_lock(827413)')
  if not d.one('SELECT id FROM sb_devices WHERE id=$1 AND revoked=false',dev['id']):
   d.q('ROLLBACK');return self.respond(json.dumps({'error':'Revoked time clock key'}),401,'application/json')
  results=[];before={};legacyinst={};emps={};jobcache={}
  for row in data['punches']:
   pid=row.get('punch_id') if isinstance(row,dict) else None;status='rejected';reason='Missing or malformed punch fields.'
   try:
    if not isinstance(row,dict) or not isinstance(pid,str) or not 1<=len(pid)<=64:raise ValueError(reason)
    eid=row.get('employee_id');kind=row.get('kind');ats=row.get('at');job=row.get('job_code')
    if not isinstance(eid,int) or isinstance(eid,bool) or not 1<=eid<=2147483647 or kind not in ('in','out','break_start','break_end') or not isinstance(ats,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)',ats):raise ValueError(reason)
    at=datetime.fromisoformat(ats.replace('Z','+00:00')).astimezone(UTC)
    if kind=='in' and (not isinstance(job,str) or not job):raise ValueError(reason)
    if kind!='in' and job is not None:raise ValueError('Only an in punch may specify a job code.')
    payload=json.dumps(row,sort_keys=True)
    old=d.one('SELECT payload FROM sb_punches WHERE punch_id=$1',pid)
    if old:
     if old['payload']==payload:status='duplicate';reason='This punch was already accepted.'
     else:reason='Punch ID was already used with different fields.'
    else:
     if eid not in emps:emps[eid]=d.one('SELECT e.*,l.timezone FROM employees e JOIN locations l ON l.id=e.location_id WHERE e.id=$1',eid)
     e=emps[eid]
     if not e or e['active']!='t' or e['location_id']!=dev['location_id']:raise ValueError('Employee is unknown, inactive, or assigned to another location.')
     if kind=='in':
      if eid not in jobcache:jobcache[eid]={r['job_code'] for r in d.q('SELECT job_code FROM job_rates WHERE employee_id=$1',eid)}
      if job not in jobcache[eid]:raise ValueError('Employee has no rate for this job code.')
     if eid not in legacyinst:legacyinst[eid]={datetime.fromisoformat(r['local_time']).replace(tzinfo=ZoneInfo(e['timezone']),fold=int(r['fold'])).astimezone(UTC) for r in d.q('SELECT local_time,fold FROM legacy_punches WHERE employee_id=$1',eid)}
     if at in legacyinst[eid] or d.one('SELECT punch_id FROM sb_punches WHERE employee_id=$1 AND at=$2',eid,at.isoformat()):raise ValueError('Employee already has a punch at this instant.')
     if eid not in before:_,ww=allweeks(d,eid);before[eid]=ww[eid]
     d.q('INSERT INTO sb_punches(punch_id,employee_id,kind,at,job_code,payload) VALUES($1,$2,$3,$4,$5,$6)',pid,eid,kind,at.isoformat(),job,payload);status='accepted';reason='Punch accepted.'
     _,ww=allweeks(d,eid);invalidate(d,eid,before[eid],ww[eid]);before[eid]=ww[eid]
   except (ValueError,TypeError,OverflowError) as ex:reason=str(ex) or 'Malformed punch.'
   results.append({'punch_id':pid,'status':status,'reason':reason})
  d.q('COMMIT');return self.respond(json.dumps({'results':results}),200,'application/json')
CSS='''*{box-sizing:border-box}body{margin:0;background:#f6f5f0;color:#233e39;font:16px/1.6 system-ui,sans-serif}header{background:#153f36;color:white;padding:20px 5%;display:flex;align-items:center;justify-content:space-between;gap:20px}header span{font-size:11px;letter-spacing:2px}.brand{font-size:26px;font-weight:750;color:white;text-decoration:none}nav{display:flex;flex-wrap:wrap;align-items:center;gap:12px 24px;padding:12px 5%;background:#fff;border-bottom:1px solid #dce1d9}nav form{margin:0;padding:0;border:0;background:none}nav button{padding:6px 12px;font-size:13px}a{color:#17644d;overflow-wrap:anywhere}main{max-width:1080px;margin:42px auto;padding:0 24px;min-height:65vh}h1{font-size:36px;letter-spacing:-1px;margin:5px 0 18px;line-height:1.2}h2{font-size:22px;margin-top:30px}h3{margin:0}.eyebrow{font-size:11px;letter-spacing:2px;color:#68756e}.lead{color:#66756d;font-size:18px}form,article{background:white;border:1px solid #dce1d9;border-radius:10px;padding:20px;margin:14px 0}form{max-width:640px}label{display:block;font-size:14px;font-weight:600;margin:0 0 16px}input,select{display:block;margin-top:6px;border:1px solid #a6b6ad;border-radius:5px;padding:11px 12px;width:100%;font:inherit;background:white;color:#233e39}button{border:0;border-radius:5px;background:#216649;color:white;padding:12px 20px;font-size:15px;font-weight:650;cursor:pointer}button:hover{background:#164b36}.notice{border-left:4px solid #b58d39;padding:15px;background:#fff6df;margin:18px 0;border-radius:4px}.badge{display:inline-block;background:#e2ebdf;border-radius:5px;padding:6px 12px;font-size:12px;font-weight:700;letter-spacing:1px}.day{display:grid;grid-template-columns:2fr repeat(3,1fr);gap:12px}.weeknav{display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap}small,article>span{display:block;color:#67776e}code{overflow-wrap:anywhere}footer{padding:24px 5%;font-size:12px;border-top:1px solid #dce1d9;color:#6b7970}section,article{min-width:0;overflow-wrap:anywhere}@media(max-width:600px){header span{display:none}header{padding:16px 20px}nav{gap:8px 16px;padding:12px 20px;font-size:14px}main{margin:28px auto;padding:0 16px}h1{font-size:30px}.day{grid-template-columns:repeat(3,1fr);padding:16px 12px;font-size:14px}.day strong{grid-column:1/-1}.weeknav strong{order:-1;width:100%}form,article{padding:16px}}'''
if __name__=='__main__':
 migrate()
 for _ in range(16):threading.Thread(target=mailworker,daemon=True).start()
 ThreadingHTTPServer(('0.0.0.0',int(os.environ['PORT'])),App).serve_forever()
