from fractions import Fraction as F
from datetime import date,timedelta
import json,csv,io,math,bisect

def num(v): return F(str(v or 0))
def rnd(v):
 v=num(v); return (v.numerator*2+v.denominator)//(2*v.denominator) if v>=0 else -rnd(-v)
def share(c,m,q): return c if m==q else rnd(F(c)*m/q)
def dec(v):
 v=num(v)
 if v.denominator==1:return str(v.numerator)
 sign='-' if v<0 else ''; v=abs(v); n,r=divmod(v.numerator,v.denominator); out=''
 while r:
  r*=10; a,r=divmod(r,v.denominator); out+=str(a)
 return sign+str(n)+'.'+out

def dt(s):return s if isinstance(s,date) else date.fromisoformat(s)
def term(start,end):
 start=dt(start);end=dt(end)
 try:ann=start.replace(year=start.year+1)
 except ValueError:ann=start.replace(year=start.year+1,day=28)
 return 'long' if end>ann else 'short'
def safe(s):
 s=str(s);return "'"+s if s and s[0] in '=+-@\t\r' else s
def csvout(header,rows):
 o=io.StringIO(newline='');w=csv.writer(o,lineterminator='\r\n');w.writerow(header);w.writerows(rows);return o.getvalue()
RH='sale_date,account,symbol,quantity,date_acquired,term,proceeds_cents,basis_cents,wash_disallowed_cents,gain_cents'.split(',')
SH='symbol,name,short_term_gain_cents,long_term_gain_cents,wash_disallowed_cents,dividends_cents,adj_short_term_cents,adj_long_term_cents,adj_dividends_cents'.split(',')
UH='account,symbol,quantity,date_acquired,term,basis_cents,market_value_cents,unrealized_cents'.split(',')
class Engine:
 def __init__(self,d,h,extra=None,actions=None):
  self.d=d;self.h=int(h);self.accounts={int(x['id']):x for x in d.q('SELECT * FROM accounts WHERE household_id=$1',h)};self.secs={int(x['id']):x for x in d.q('SELECT * FROM securities WHERE household_id=$1',h)}
  self.rates={}
  for x in d.q('SELECT * FROM fx_rates ORDER BY rate_date'):self.rates.setdefault(x['currency'],[]).append((dt(x['rate_date']),num(x['rate']) if x['quote']=='usd_per_unit' else 1/num(x['rate'])))
  self.methods={}
  for x in d.q('SELECT m.* FROM lot_method_changes m JOIN accounts a ON a.id=m.account_id WHERE a.household_id=$1 ORDER BY effective_from,m.id',h):self.methods.setdefault(int(x['account_id']),[]).append(x)
  self.tx=[]
  for table in ('legacy_transactions','lb_transactions'):
   for x in d.q('SELECT t.* FROM '+table+' t JOIN accounts a ON a.id=t.account_id WHERE a.household_id=$1',h):
    x={k:v for k,v in x.items() if k not in ('household_id','name','broker','account_number','lot_method','opened_on','account_type')};self.tx.append(self.normalize(x))
  self.tx+=extra or []
  self.actions=d.q('SELECT * FROM corporate_actions WHERE household_id=$1',h)+(actions or [])
  self.lots=[];self.bysec={};self.byid={};self.real=[];self.div=[];self.errors=[];self.pending={};self.serial=0
 def normalize(self,x):
  x=dict(x)
  for k in ('id','account_id','security_id','to_account_id','split_new','split_old'):
   if x.get(k) is not None:x[k]=int(x[k])
  x['kind']=x['kind'].strip().lower();x['trade_date']=dt(x['trade_date']);x['quantity']=abs(num(x.get('quantity')));x['amount']=abs(num(x.get('amount')));x['specific']=json.loads(x.get('specific') or '{}') if isinstance(x.get('specific'),str) else x.get('specific',{})
  return x
 def rate(self,c,day):
  if c=='USD':return F(1)
  day=dt(day)
  for dd,r in reversed(self.rates.get(c,[])):
   if dd<=day:return r
  raise ValueError('No exchange rate for '+c+' on '+str(day))
 def usd(self,s,amount,day):return rnd(num(amount)*100*self.rate(self.secs[int(s)]['currency'],day))
 def fifo(self,l):return (l['acq'],l['id'],l['order'])
 def lot(self,t,q,b,**kw):
  self.serial+=1;l=dict(id=t['id'],a=t['account_id'],s=t['security_id'],q=q,b=b,acq=t['trade_date'],start=t['trade_date'],used=False,wash=False,order=self.serial);l.update(kw);return l
 def add(self,l):
  self.lots.append(l);self.bysec.setdefault(l['s'],[]).append(l);self.byid.setdefault(l['id'],[]).append(l)
 def open(self,a,s):return sorted([l for l in self.bysec.get(int(s),[]) if l['q'].numerator>0 and l['a']==int(a) and l['s']==int(s)],key=self.fifo)
 def divide(self,l,m):
  if m==l['q']:return l
  p=dict(l);p['q']=m;p['b']=share(l['b'],m,l['q']);l['b']-=p['b'];l['q']-=m
  # A divided part immediately precedes the rest.
  p['order']=l['order'];l['order']=math.nextafter(float(l['order']),math.inf);self.add(p);return p
 def adjustment(self,l,loss,days):
  l['used']=True
  if self.accounts[l['a']]['account_type']!='ira':l['b']+=loss;l['start']-=timedelta(days=days);l['wash']=True
 def row(self,t,l,m,b,p,day=None,s=None,action=False):
  day=day or t['trade_date'];return dict(day=day,a=l['a'],s=s or l['s'],q=m,start=l['start'],term=term(l['start'],day),p=p,b=b,w=0,g=p-b,tid=int(t['id']),action=action)
 def run(self):
  acquisitions=sorted([t for t in self.tx if t['kind'] in ('buy','reinvest')],key=lambda t:(t['trade_date'],t['id']))
  ac_bysec={}
  for ac in acquisitions:ac_bysec.setdefault(ac['security_id'],[]).append(ac)
  ac_dates={s:[a['trade_date'] for a in aa] for s,aa in ac_bysec.items()}
  order={'split':0,'action':1,'buy':2,'reinvest':2,'transfer':3,'sell':4,'dividend':5}
  events=list(self.tx)+[dict(x,kind='action',action_kind=x['kind'],trade_date=dt(x['action_date']),id=int(x['id']),security_id=int(x['security_id'])) for x in self.actions]
  for t in sorted(events,key=lambda t:(t['trade_date'],order[t['kind']],t['id'])):
   k=t['kind'];s=t['security_id'];day=t['trade_date'];a=t.get('account_id');ls=self.open(a,s) if a and k in ('split','transfer','sell') else []
   if k in ('buy','reinvest'):
    base=self.usd(s,t['amount'],day);q=t['quantity'];l=self.lot(t,q,base);self.add(l)
    for m,loss,days in self.pending.get(t['id'],[]):
     p=self.divide(l,m);self.adjustment(p,loss,days)
     if p is l:break
   elif k=='split':
    for l in ls:l['q']*=F(t['split_new'],t['split_old'])
   elif k=='transfer':
    if not ls:self.errors.append(t);continue
    for l in ls:l['a']=t['to_account_id']
   elif k=='dividend':
    if self.accounts[a]['account_type']!='ira':self.div.append((day,s,self.usd(s,t['amount'],day)))
   elif k=='sell':
    spec={int(i):num(q) for i,q in (t.get('specific') or {}).items() if num(q)>0}
    if sum(l['q'] for l in ls)<t['quantity'] or (spec and (sum(spec.values())!=t['quantity'] or any(sum(l['q'] for l in ls if l['id']==i)<q for i,q in spec.items()))):self.errors.append(t);continue
    method=self.accounts[a]['lot_method']
    for ch in self.methods.get(a,[]):
     if dt(ch['effective_from'])<=day:method=ch['lot_method']
    if not spec and method=='HIFO':ls.sort(key=lambda l:(-F(l['b'])/l['q'],self.fifo(l)))
    rem=t['quantity'];money=self.usd(s,t['amount'],day);portions=[]
    for l in ls:
     m=min(l['q'],spec.get(l['id'],0) if spec else rem)
     if not m:continue
     b=share(l['b'],m,l['q']);p=share(money,m,rem);r=self.row(t,l,m,b,p);r['source']=l['id'];portions.append(r);l['q']-=m;l['b']-=b;money-=p;rem-=m
     if spec:spec[l['id']]-=m
     if rem==0:break
    if self.accounts[a]['account_type']=='ira':continue
    self.real+=portions
    for r in portions:
     if r['g']>=0:continue
     rq=r['q'];loss=-r['g'];days=(day-r['start']).days
     for ac in ac_bysec.get(s,[])[bisect.bisect_left(ac_dates.get(s,[]),day-timedelta(days=30)):bisect.bisect_right(ac_dates.get(s,[]),day+timedelta(days=30))]:
      ad=ac['trade_date']
      if ac['security_id']!=s or ac['id']==r['source'] or abs((ad-day).days)>30:continue
      if ad<=day:
       avail=[l for l in sorted(self.byid.get(ac['id'],[]),key=self.fifo) if l['id']==ac['id'] and l['s']==s and l['q']>0 and not l['used']]
       for l in avail:
        if not rq:break
        m=min(rq,l['q']);v=share(loss,m,rq);p=self.divide(l,m);self.adjustment(p,v,days);r['w']+=v;rq-=m;loss-=v
      else:
       avail=ac['quantity']-sum(p[0] for p in self.pending.get(ac['id'],[]))
       m=min(rq,avail)
       if m:
        v=share(loss,m,rq);self.pending.setdefault(ac['id'],[]).append((m,v,days));r['w']+=v;rq-=m;loss-=v
      if not rq:break
     r['g']+=r['w']
   elif k=='action':self.corporate(t)
  self.real.sort(key=lambda r:(r['day'],0 if r['action'] else 1,r['tid']))
  return self
 def corporate(self,t):
  s=t['security_id'];day=t['trade_date'];kind=t['action_kind']
  for a in sorted(self.accounts):
   for l in self.open(a,s):
    def emit(q,b,p,sec=s):
     if self.accounts[a]['account_type']!='ira':self.real.append(self.row(t,l,q,b,p,day,sec,True))
    if kind=='return_of_capital':
     cash=self.usd(s,F(rnd(num(t['amount_per_share'])*l['q']*100),100),day);ex=max(0,cash-l['b']);l['b']=max(0,l['b']-cash)
     if ex:emit(l['q'],0,ex)
    elif kind=='spinoff':
     ratio=num(t['ratio']);frac=ratio*num(t['new_fmv'])/(ratio*num(t['new_fmv'])+num(t['parent_fmv']));b=rnd(l['b']*frac);l['b']-=b;n=l['q']*ratio;w=n.numerator//n.denominator;f=n-w;fb=share(b,f,n) if f else 0;ns=int(t['new_security_id'])
     if f:emit(f,fb,self.usd(s,F(rnd(f*num(t['cash_in_lieu_price'])*100),100),day),ns)
     if w:self.add(dict(l,s=ns,q=F(w),b=b-fb))
    else:
     cash=self.usd(s,F(rnd(l['q']*num(t['cash_per_share'])*100),100),day);ratio=num(t['shares_per_share'])
     if not ratio:emit(l['q'],l['b'],cash);l['q']=0
     else:
      v=self.usd(s,F(rnd(l['q']*ratio*num(t['acquirer_fmv'])*100),100),day);recognized=max(0,min(cash,cash+v-l['b']))
      if cash:emit(l['q'],cash-recognized,cash)
      l['q']*=ratio;l['s']=int(t['acquirer_security_id']);self.bysec.setdefault(l['s'],[]).append(l);l['b']=l['b']-cash+recognized
 def figures(self,y):
  out={}
  for r in self.real:
   if r['day'].year==y:
    v=out.setdefault(r['s'],[0]*7);v[0 if r['term']=='short' else 1]+=r['g'];v[2]+=r['w']
  for day,s,c in self.div:
   if day.year==y:out.setdefault(s,[0]*7)[3]+=c
  return out
 def realized(self,y):return csvout(RH,[[r['day'],safe(self.accounts[r['a']]['name']),safe(self.secs[r['s']]['symbol']),dec(r['q']),r['start'],r['term'],r['p'],r['b'],r['w'],r['g']] for r in self.real if r['day'].year==y])
 def locks(self):return {int(x['year']):x for x in self.d.q('SELECT * FROM lb_locks WHERE household_id=$1 ORDER BY year',self.h)}
 def report(self,y):
  locks=self.locks()
  if y in locks:
   l=locks[y];return l['summary'],l['realized'],{int(k):v for k,v in json.loads(l['figures']).items()}
  if locks and y<min(locks):return csvout(SH,[]),csvout(RH,[]),{}
  f=self.figures(y)
  if locks and y==max(locks)+1:
   totals={}
   years=set(locks)|{t['trade_date'].year for t in self.tx if t['trade_date'].year<=max(locks)}|{dt(a['action_date']).year for a in self.actions if dt(a['action_date']).year<=max(locks)}
   for sy in years:
    now=self.figures(sy)
    for s,v in now.items():
     tv=totals.setdefault(s,[0,0,0])
     for j,cat in enumerate((0,1,3)):tv[j]+=v[cat]
   for l in locks.values():
    for sk,v in json.loads(l['figures']).items():
     tv=totals.setdefault(int(sk),[0,0,0])
     for j,(cat,adj) in enumerate(((0,4),(1,5),(3,6))):tv[j]-=v[cat]+v[adj]
   for s,v in totals.items():
    if any(v):f.setdefault(s,[0]*7)[4:]=v
  return self.summary(f),self.realized(y),f
 def summary(self,f):return csvout(SH,[[safe(self.secs[s]['symbol']),safe(self.secs[s]['name'])]+v for s,v in sorted(f.items(),key=lambda kv:self.secs[kv[0]]['symbol']) if any(v) or s in f])
 def unrealized(self):
  prices={int(x['security_id']):x for x in self.d.q('SELECT DISTINCT ON (p.security_id) p.* FROM security_prices p JOIN securities s ON s.id=p.security_id WHERE s.household_id=$1 ORDER BY p.security_id,price_date DESC',self.h)};rs=[]
  for l in sorted(self.lots,key=lambda l:(self.secs[l['s']]['symbol'],l['a'],self.fifo(l))):
   if l['q']<=0 or l['s'] not in prices:continue
   p=prices[l['s']];v=self.usd(l['s'],l['q']*num(p['close']),p['price_date']);rs.append([safe(self.accounts[l['a']]['name']),safe(self.secs[l['s']]['symbol']),dec(l['q']),l['start'],term(l['start'],p['price_date']),l['b'],v,v-l['b']])
  return csvout(UH,rs)
