"""In-memory integration checks; reads legacy state but does not change the database."""
import app as a,threading,http.client,urllib.parse as U,json,re,os,hmac,hashlib
from http.server import ThreadingHTTPServer
a.init(); a.save=lambda:None
uid=a.ident(); a.S['users'][str(uid)]={'id':uid,'email':'check@example.test','full_name':'Check Person','verified':True,'password':a.password('secure-password')}
sid='test-session'; csrf='test-csrf'; a.S['sessions'][a.sha(sid)]={'user':uid,'csrf':csrf}
server=ThreadingHTTPServer(('127.0.0.1',0),a.App);threading.Thread(target=server.serve_forever,daemon=True).start()
def req(path,data=None,raw=None,headers=None):
 c=http.client.HTTPConnection('127.0.0.1',server.server_port); h={'Cookie':'seatwise='+sid};h.update(headers or {})
 if data is not None: data=U.urlencode({'csrf':csrf,**data},doseq=True).encode()
 c.request('POST' if data is not None or raw is not None else 'GET',path,body=raw if raw is not None else data,headers=h);r=c.getresponse();body=r.read().decode();loc=r.getheader('Location');return r.status,body,loc
def ok(path,data=None,status=200):
 r=req(path,data); assert r[0]==status,(path,r[0],r[1][-600:]); return r
assert req('/orgs/1')[0]==403
assert req('/orgs/new',raw=b'name=x')[0]==403
root=ok('/orgs/new',{'name':'Check <safe>'},303)[2]
vpath=ok(root+'/venues/new',{'name':'Test Hall','address':'Main street','timezone':'America/Chicago'},303)[2]
ok(vpath+'/sections',{'name':'Orchestra','code':'ORCH','rows':'A-B','seats_per_row':'4','accessible':'A1'},303)
assert 'data-seat-count="8"' in ok(vpath)[1]
vid=vpath.split('/')[-1]
epath=ok(root+'/events/new',{'venue_id':vid,'name':'Test show','description':'<script>bad</script>','starts_at':'2027-06-12T20:00','on_sale_at':'2026-01-01T12:00','off_sale_at':'','presale_at':'','presale_code':'','facility_fee_cents':'200','price_ORCH':'4000'},303)[2]
ok(epath+'/publish',{},303); eid=epath.split('/')[-1]
event=ok('/events/'+eid)[1];assert '&lt;script&gt;' in event and 'data-starts-at="2027-06-13T01:00:00Z"' in event
assert req('/events/'+eid,{'seats':['ORCH-A-1']})[0]==400
orderpath=ok('/events/'+eid,{'seats':['ORCH-A-2','ORCH-A-3']},303)[2]
ok(epath+'/codes',{'code':'test20','kind':'percent','value':'20','min_subtotal_cents':'0','max_redemptions':'1'},303)
ok(orderpath+'/codes',{'code':' test20 '},303)
assert 'data-applied-code="TEST20"' in ok(orderpath)[1]
a.provider=lambda path,data=None,key=None: {'id':'cs_test','url':a.BASE+'/fake-checkout'}
ok(orderpath+'/pay',{},303);assert req(orderpath+'/codes',{'code':'test20'})[0]==400
order=a.S['orders'][orderpath.split('/')[-1]]
def webhook(typ,obj):
 raw=json.dumps({'id':'evt_test','type':typ,'data':{'object':obj}}).encode();ts=str(a.now());secret=open(os.environ['PAYMENTS_WEBHOOK_SIGNING_FILE']).read().strip(); sig=hmac.new(secret.encode(),ts.encode()+b'.'+raw,hashlib.sha256).hexdigest();r=req('/webhooks/payments',raw=raw,headers={'Stripe-Signature':'t='+ts+',v1='+sig});assert r[0]==200,r
webhook('checkout.session.completed',{'id':'cs_test','payment_intent':'pi_test'})
webhook('checkout.session.completed',{'id':'cs_test','payment_intent':'pi_test'})
tt=a.tickets(a.S['events'][eid]); assert len(tt)==2 and order['status']=='paid'
t=tt[0];oldtoken=t['token']; tp='/tickets/'+str(t['id']);ok(tp)
ok(tp+'/transfer',{'to_email':' Friend@Example.Test ','to_name':'Friend'},303);assert oldtoken in t['old_tokens'];assert 'data-qr-token' not in ok(tp)[1]
d=ok(epath+'/devices',{'label':'Front'},200)[1];key=re.search('data-device-key="([^"]+)',d)[1]
a.S['events'][eid]['start']=a.now()+3600
raw=json.dumps({'scans':[{'scan_id':'later','token':tt[1]['token'],'gate':'North','scanned_at':a.iso(a.now())},{'scan_id':'earlier','token':tt[1]['token'],'gate':'South','scanned_at':a.iso(a.now()-60)}]}).encode()
r=req('/api/scan/batch',raw=raw,headers={'Authorization':'Bearer '+key});assert r[0]==200,r;results=json.loads(r[1]);assert [x['result'] for x in results]==['already_admitted','admitted'];assert results[0]['gate']=='South'
r=req('/api/scan/batch',raw=raw,headers={'Authorization':'Bearer '+key});assert all(x['result']=='duplicate' for x in json.loads(r[1]))
ok(epath+'/dashboard');assert ok(epath+'/attendees.csv')[1].startswith('ticket_id,order_id,section,row,seat,')
# All four specification price cases.
e=a.S['events'][eid];sec=list(e['prices'])[0]
def pricing(faces,facility,codes,expected):
 e['facility_fee_cents']=facility;o={'event_id':int(eid),'items':[{'seat_id':t['seat_id'],'face_cents':v} for t,v in zip(tt*2,faces)],'codes':[]}
 for kind,val in codes:
  cid=a.ident();a.S['codes'][str(cid)]={'kind':kind,'value':val};o['codes'].append(cid)
 a.price(o);assert [i['total_cents'] for i in o['items']]==expected,o
pricing([4000,2600],200,[],[4882,3243]);pricing([3000,3000,5000],150,[('percent',20)],[3008,3008,4913]);pricing([3000,3000,5000],150,[('percent',20),('fixed',400)],[2890,2889,4715]);pricing([2000],100,[('fixed',2500)],[263])
# Organizer refund remains allowed after admission; webhook completes once.
refpath=root+'/tickets/'+str(tt[1]['id'])+'/refund'
ok(refpath,{},303)
r=next(r for r in a.S['refunds'].values() if r.get('ticket_id')==tt[1]['id']);r['provider_id']='re_test'
webhook('refund.created',{'id':'re_test','payment_intent':'pi_test','amount':r['amount'],'metadata':{'ticket_id':str(tt[1]['id'])}})
webhook('refund.created',{'id':'re_test','payment_intent':'pi_test','amount':r['amount'],'metadata':{'ticket_id':str(tt[1]['id'])}})
assert a.S['tickets'][str(tt[1]['id'])]['status']=='refunded'
assert a.S['tickets'][str(tt[1]['id'])]['refunded_cents']==r['amount']
# Waiting list offers skip accessible seats and accept into a fresh hold.
e=a.S['events'][eid];sec=next(iter(e['prices']));available=[s for s in a.seats(e) if a.seatstatus(e,s)=='available']
other={'email':'other@example.test','full_name':'Other Buyer'}
for offset in range(0,len(available),6): a.neworder(e,{**other,'email':str(offset)+other['email']},available[offset:offset+6])
ok('/events/'+eid+'/waitlist',{'section_id':sec,'quantity':'2'},303)
for o in a.S['orders'].values():
 if o['event_id']==e['id'] and o['buyer_email'].endswith(other['email']): o['expires']=a.now()-1
class FakeSMTP:
 def __init__(self,*args,**kwargs): pass
 def __enter__(self): return self
 def __exit__(self,*args): pass
 def send_message(self,msg): pass
a.smtplib.SMTP=FakeSMTP
a.work();of=next(of for of in a.S['offers'].values() if of['event_id']==e['id']);assert all(not a.S['seats'][str(s)]['accessible'] for s in of['seats'])
offerpath='/waitlist/offers/'+of['token'];assert 'data-offer-status="open"' in ok(offerpath)[1]
ok(offerpath+'/accept',{},303);assert 'data-offer-status="accepted"' in ok(offerpath)[1]
# Account-link replacement and expiry, reset session invalidation, safe redirects.
addr='signup-check@example.test'
r=ok('/signup',{'email':addr,'full_name':'New Person','password':'very-secure-password'})
u=next(u for u in a.S['users'].values() if u['email']==addr)
m=list(a.S['emails'].values())[-1];link=a.unseal(m['sealed_body']); assert 'verify/' in link and addr not in str(a.S['links'])
oldlink=link.split(a.BASE)[-1]
ok('/signup',{'email':' SIGNUP-CHECK@EXAMPLE.TEST ','full_name':'New Name','password':'changed-password'})
assert req(oldlink)[0]==400
m=list(a.S['emails'].values())[-1];link=a.unseal(m['sealed_body']).split(a.BASE)[-1];ok(link);assert a.S['users'][str(u['id'])]['verified'];assert req(link)[0]==400
ok('/password-reset',{'email':addr});m=list(a.S['emails'].values())[-1];link=a.unseal(m['sealed_body']).split(a.BASE)[-1]
a.S['sessions'][a.sha('account-old-session')]={'user':u['id'],'csrf':'x'}
ok(link,{'password':'my-new-password'},303);assert a.sha('account-old-session') not in a.S['sessions']
r=req('/signin?next=//evil.example',{'email':addr,'password':'my-new-password'});assert r[0]==303 and r[2]=='/tickets'
# Date interpretation: first fold occurrence, nonexistent spring-forward time refused.
assert a.iso(a.local('2027-11-07T01:30','America/Chicago'))=='2027-11-07T06:30:00Z'
try: a.local('2027-03-14T02:30','America/Chicago');raise AssertionError('Accepted nonexistent local time')
except ValueError: pass
server.shutdown(); print('PASS: account permissions, CSRF, venue/event creation, escaping, accessible holds, promo, payment idempotency, transfer, scan ordering, CSV, dashboard, all price examples')
