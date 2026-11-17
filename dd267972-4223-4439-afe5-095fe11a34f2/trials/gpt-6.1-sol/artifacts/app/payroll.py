from datetime import datetime,date,timedelta,timezone
from zoneinfo import ZoneInfo
from collections import defaultdict
from fractions import Fraction
import json
UTC=timezone.utc
ANCHOR=date(2026,9,20)
def day(s):return date.fromisoformat(str(s))
def sunday(d):return d-timedelta(days=(d.weekday()+1)%7)
def roundtime(dt):
 u=(dt-datetime(1970,1,1,tzinfo=UTC))//timedelta(microseconds=1)
 return ((u+180000000)//360000000)*360

def cents(x):return (x.numerator*2+x.denominator)//(2*x.denominator)
def load(d,employee=None):
 where='' if employee is None else ' WHERE e.id=$1';args=() if employee is None else (employee,)
 emps=d.q('SELECT e.*,l.name location,l.timezone,l.ruleset FROM employees e JOIN locations l ON l.id=e.location_id'+where,*args)
 ids={int(e['id']):e for e in emps}; ps=defaultdict(list);rates=defaultdict(list); approvals={}
 filt='' if employee is None else ' WHERE employee_id=$1'
 for r in d.q('SELECT * FROM job_rates'+filt,*args):rates[int(r['employee_id'])].append(r)
 for r in d.q('SELECT * FROM legacy_punches'+filt,*args):
  e=ids[int(r['employee_id'])];t=datetime.fromisoformat(r['local_time']).replace(tzinfo=ZoneInfo(e['timezone']),fold=int(r['fold'])).astimezone(UTC)
  ps[int(e['id'])].append((t,r['kind'],r['job_code']))
 for r in d.q('SELECT * FROM sb_punches'+filt,*args):ps[int(r['employee_id'])].append((datetime.fromisoformat(r['at']).astimezone(UTC),r['kind'],r['job_code']))
 for r in d.q('SELECT * FROM legacy_approvals'+filt,*args):approvals[(int(r['employee_id']),r['week_start'])]=True
 for r in d.q('SELECT * FROM sb_approvals'+filt,*args):approvals[(int(r['employee_id']),r['week_start'])]=r['approved']=='t'
 return ids,ps,rates,approvals

def calculate(e,punches,rates,approvals):
 tz=ZoneInfo(e['timezone']);eid=int(e['id']); weeks={};segments=[];meal_days=set()
 def week(dt):
  w=sunday(dt).isoformat()
  if w not in weeks:weeks[w]={'days':{(day(w)+timedelta(days=i)).isoformat():[0,0,0] for i in range(7)},'errors':[],'reg':0,'ot':0,'dt':0,'meals':0,'straight':Fraction(0)}
  return weeks[w]
 def error(t,msg):
  a=week(t.astimezone(tz).date())
  if msg not in a['errors']:a['errors'].append(msg)
 shifts=[];cur=None;br=None;breaks=[]
 for t,k,job in sorted(punches,key=lambda p:p[0]):
  if cur is None:
   if k=='in':cur=(t,job);br=None;breaks=[]
   else:error(t,'Punch order is invalid. Correct the punches before approval.')
  elif k=='break_start' and br is None:br=t
  elif k=='break_end' and br is not None:breaks.append((br,t));br=None
  elif k=='out' and br is None:
   if (t-cur[0]).total_seconds()>57600:error(cur[0],'Shift exceeds 16 hours. Correct its punches.')
   else:shifts.append((cur[0],t,cur[1],breaks))
   cur=None
  else:
   error(t,'Punch order is invalid. Correct the punches before approval.');error(cur[0],'Shift has no out punch. Add or correct the missing punch.');cur=None;br=None
 if cur:error(cur[0],'Shift has no out punch. Add or correct the missing punch.')
 jobs=defaultdict(list)
 for r in rates:jobs[r['job_code']].append((day(r['effective_from']),int(r['rate_cents'])))
 for j in jobs:jobs[j].sort()
 for start,end,job,breaks in shifts:
  a=roundtime(start);b=roundtime(end);work=[];cursor=a;worked=0;qual=False
  for bs,be in breaks:
   x=roundtime(bs);y=roundtime(be)
   if x>cursor:work.append((cursor,x));worked+=x-cursor
   if y-x>=1800 and worked<=18000:qual=True
   cursor=y
  if b>cursor:work.append((cursor,b));worked+=b-cursor
  startday=start.astimezone(tz).date()
  if e['ruleset']=='CA' and worked>18000 and not qual:meal_days.add(startday)
  for x,y in work:
   while x<y:
    ld=datetime.fromtimestamp(x,UTC).astimezone(tz).date();mid=int(datetime.combine(ld+timedelta(days=1),datetime.min.time(),tz).timestamp());stop=min(y,mid)
    eligible=[r for ed,r in jobs[job] if ed<=ld];rate=eligible[-1] if eligible else 0
    if not eligible:error(start,"Work precedes the job's first effective rate. Add an applicable rate.")
    segments.append((x,stop,ld,rate));x=stop
 totals=defaultdict(int)
 for x,y,ld,r in segments:totals[ld]+=y-x
 dayworked=defaultdict(int)
 for x,y,ld,rate in sorted(segments):
  w=week(ld);seventh=e['ruleset']=='CA' and ld.weekday()==5 and all(totals[sunday(ld)+timedelta(days=i)]>0 for i in range(7));remaining=y-x
  while remaining>0:
   h=dayworked[ld];r=w['reg'];cat='reg';limit=remaining
   if e['ruleset']=='CA':
    if seventh:
     cat='ot' if h<28800 else 'dt'
     if h<28800:limit=min(limit,28800-h)
    elif h>=43200:cat='dt'
    elif h>=28800:cat='ot';limit=min(limit,43200-h)
    elif r>=144000:cat='ot';limit=min(limit,28800-h)
    else:limit=min(limit,28800-h,144000-r)
   elif r>=144000:cat='ot'
   else:limit=min(limit,144000-r)
   w[cat]+=limit;w['days'][ld.isoformat()][['reg','ot','dt'].index(cat)]+=limit;w['straight']+=Fraction(limit*rate,3600);dayworked[ld]+=limit;remaining-=limit
 for ld in meal_days:week(ld)['meals']+=1
 for ws,w in weeks.items():
  total=w['reg']+w['ot']+w['dt'];rr=w['straight']/Fraction(total,3600) if total else Fraction(0)
  w['straight_cents']=cents(w.pop('straight'));w['ot_premium_cents']=cents(rr*Fraction(w['ot'],7200));w['dt_premium_cents']=cents(rr*Fraction(w['dt'],3600));w['meal_cents']=cents(rr*w['meals']);w['gross_cents']=sum(w[k] for k in ['straight_cents','ot_premium_cents','dt_premium_cents','meal_cents']);w['status']='exception' if w['errors'] else 'approved' if approvals.get((eid,ws)) else 'pending'
 return weeks

def allweeks(d,employee=None):
 ids,ps,rs,ap=load(d,employee);return ids,{eid:calculate(e,ps[eid],rs[eid],ap) for eid,e in ids.items()}
def invalidate(d,eid,before,after):
 for ws in set(before)|set(after):
  a=dict(before.get(ws,{}));b=dict(after.get(ws,{}));a.pop('status',None);b.pop('status',None)
  if a!=b:d.q('INSERT INTO sb_approvals(employee_id,week_start,approved) VALUES($1,$2,false) ON CONFLICT(employee_id,week_start) DO UPDATE SET approved=false',eid,ws)
def emptyweek(ws):return {'days':{(day(ws)+timedelta(days=i)).isoformat():[0,0,0] for i in range(7)},'status':'pending','errors':[],'reg':0,'ot':0,'dt':0,'meals':0,'straight_cents':0,'ot_premium_cents':0,'dt_premium_cents':0,'meal_cents':0,'gross_cents':0}
def run_snapshot(d,period):
 ids,allw=allweeks(d);paid=defaultdict(int)
 for r in d.q("SELECT snapshot FROM sb_runs WHERE status='finalized'"):
  for line in json.loads(r['snapshot'])['lines']:
   for ws,amt in line['allocations'].items():paid[(line['employee_id'],ws)]+=amt
 out={'lines':[],'held':[]};end=period+timedelta(days=14)
 for eid,e in ids.items():
  line={'employee_id':eid,'full_name':e['full_name'],'email':e['email'],'location':e['location'],'reg':0,'ot':0,'dt':0,'meals':0,'straight_cents':0,'ot_premium_cents':0,'dt_premium_cents':0,'meal_cents':0,'adjustment_cents':0,'gross_cents':0,'adjustments':[],'allocations':{}};has=False
  for ws,w in sorted(allw[eid].items()):
   wd=day(ws);payable=w['status']=='approved' and not w['errors']
   if period<=wd<end:
    if payable and w['reg']+w['ot']+w['dt']:
     has=True
     for k in ['reg','ot','dt','meals','straight_cents','ot_premium_cents','dt_premium_cents','meal_cents']:line[k]+=w[k]
     line['allocations'][ws]=w['gross_cents']
    elif w['reg']+w['ot']+w['dt'] or w['errors']:out['held'].append({'employee_id':eid,'full_name':e['full_name'],'week_start':ws,'reason':'; '.join(w['errors']) if w['errors'] else 'Week is pending approval.'})
   elif ANCHOR<=wd<period and payable:
    diff=w['gross_cents']-paid[(eid,ws)]
    if diff:has=True;line['adjustment_cents']+=diff;line['adjustments'].append({'week_start':ws,'cents':diff});line['allocations'][ws]=diff
  if has:
   line['gross_cents']=sum(line[k] for k in ['straight_cents','ot_premium_cents','dt_premium_cents','meal_cents','adjustment_cents']);out['lines'].append(line)
 out['lines'].sort(key=lambda x:x['employee_id']);return out
