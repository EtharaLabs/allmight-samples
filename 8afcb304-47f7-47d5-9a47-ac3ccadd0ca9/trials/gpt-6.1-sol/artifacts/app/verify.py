"""Integration smoke check using only configured services; creates a patient and app appointments."""
import urllib.request,urllib.parse,http.cookiejar,re,json,os,secrets,time,hmac,hashlib
from datetime import datetime,timedelta,timezone
BASE=os.environ['PUBLIC_BASE_URL'].rstrip('/')
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args):return None
class Client:
 def __init__(self):self.op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),NoRedirect())
 def req(self,path,data=None,headers={}):
  req=urllib.request.Request(path if path.startswith('http') else BASE+path, None if data is None else urllib.parse.urlencode(data).encode(),headers)
  try:r=self.op.open(req,timeout=5)
  except urllib.error.HTTPError as ex:r=ex
  return r.code,r.read().decode(),r.headers
 def post(self,path,fields):
  code,body,_=self.req(path);assert code==200,(code,body[:100]);csrf=re.search(r'name="csrf" data-csrf value="([^"]+)"',body)[1];return self.req(path,dict(fields,csrf=csrf))
c=Client();addr='smoke-'+secrets.token_hex(5)+'@example.test'
s,b,h=c.post('/register',{'email':addr,'full_name':'Smoke <Patient>','password':'A-long-test-password'});assert s==303,s
assert c.req('/profile',{'full_name':'Bad'})[0]==403
assert c.req('/admin/rooms/new')[0]==403
assert c.req('/profile')[0]==200
assert c.req('/api/availability?clinician_id=1&type=FU30&date=no')[0]==400
assert c.req('/api/availability?clinician_id=999999&type=FU30&date=2026-11-20')[0]==404
slot=None
for i in range(2,30):
 D=(datetime.now(timezone.utc)+timedelta(days=i)).date().isoformat();s,b,h=c.req('/api/availability?clinician_id=1&type=FU30&date='+D);a=json.loads(b)
 if a['slots']:slot=a['slots'][0];break
assert slot
s,b,h=c.post('/book?clinician_id=1&type=FU30&date='+D,{'clinician_id':1,'type':'FU30','start':slot['start'],'deposit_cents':1,'room_id':9999});assert s==303,(s,b)
checkout_url=h['Location'];s,b,h=c.req(checkout_url);assert s==200
assert 'Payments' in b
s,b,h=c.req('/appointments');aid=re.search(r'data-appointment-id="(\d+)"',b)[1]
s,b,h=c.req('/appointments/'+aid);assert 'data-status="held"' in b and 'data-deposit-cents="1900"' in b
from db import DB
with DB() as d:session=d.one('select checkout_id from hv_appointments where appointment_id=$1',aid)['checkout_id']
print('Created hold',aid,'checkout',session)
print('Registration, CSRF, role authorization, availability, fee integrity, hold and hosted checkout passed.')
# Release this smoke-test hold so it does not consume availability.
s,b,h=c.req('/appointments/'+aid);csrf=re.search(r'name="csrf" data-csrf value="([^"]+)"',b)[1]
s,b,h=c.req('/appointments/'+aid+'/release',{'csrf':csrf});assert s==303
assert 'data-status="released"' in c.req('/appointments/'+aid)[1]
print('Release passed.')
