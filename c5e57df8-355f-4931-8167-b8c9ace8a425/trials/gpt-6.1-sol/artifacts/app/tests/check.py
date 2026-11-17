import sys,os,json,time,hmac,hashlib,datetime as dt
sys.path.insert(0,os.path.dirname(os.path.dirname(__file__)))
import app
D=app.DB();D.q('BEGIN');s=json.loads(D.q('select data from pp_state where id=1 for update')[0]['data'])
try:
 c=app.Context(D,s);today=c.today;future=str(today+dt.timedelta(days=150))
 co=app.insert(D,'customers',dict(name='Verification customer',tier='standard',ship_to_state='NY',approval_limit_cents=None,min_shelf_life_days=14,allow_split_cases=True,backorders_allowed=True))
 user=app.insert(D,'users',dict(email='verification@example.test',full_name='Verification User',role='buyer',customer_id=co['id'],zone=None,active=True))
 skus=[]
 for i,(pack,weight,l,w,h,tax,price) in enumerate([(6,1050,30,20,32,False,1450),(24,130,30,20,12,True,389)]):
  sk=app.insert(D,'skus',dict(code='VERIFY-'+str(i),name='Verification',case_pack=pack,unit_weight_g=weight,length_cm=l,width_cm=w,height_cm=h,storage='ambient',taxable=tax,reorder_point=0,active=True));skus.append(sk)
  app.insert(D,'price_list',dict(tier='standard',sku_id=sk['id'],unit_price_cents=price,effective_from=str(today)))
 c=app.Context(D,s);c.inventory();b=next(b for b in c.bins.values() if b['code']=='A03-07-2')
 for sk in skus:
  lot=app.insert(D,'lots',dict(sku_id=sk['id'],lot_code='VERIFY',expiry_date=future,bin_id=b['id'],unit_cost=app.Decimal('2.1250'),qa_hold=False));c.lots[lot['id']]=lot;c.movement(lot['id'],'receipt',100)
 order=c.createorder(user,dict(ship_date=str(today),lines=[{'sku':skus[0]['code'],'qty':12},{'sku':skus[1]['code'],'qty':30}]))
 assert order['total_cents']==34646,order
 def handler(u,path,d=None):
  x=app.Handler.__new__(app.Handler);x.c=c;x.u=u;x.pathonly=path;x.path=path;x.d=d or {};x.post=d is not None;x.csrf='test';x.sid='verify';x.query={};x.result=None;s['sessions']['verify']={'uid':u['id'],'csrf':'test'};return x
 order['checkout']={'id':'cs_verify'}
 event={'id':'evt_verify','type':'checkout.session.completed','data':{'object':{'id':'cs_verify','payment_status':'paid','amount_total':34646,'payment_intent':'pi_verify','metadata':{'order_id':str(order['id'])}}}}
 x=handler(user,'/webhooks/payments',{});x.raw=json.dumps(event).encode();t=str(int(time.time()));key=open(os.environ['PAYMENTS_WEBHOOK_SIGNING_FILE']).read().strip().encode();sig=hmac.new(key,t.encode()+b'.'+x.raw,hashlib.sha256).hexdigest();x.headers={'Stripe-Signature':'t='+t+',v1='+sig};x.webhook();assert order['paid'];assert sum(l['reserved'] for l in c.orderjson(order)['lines'])==42
 x.webhook();assert len(c.tasks(order))==2
 supervisor=next(u for u in c.users.values() if u['role']=='supervisor')
 x=handler(supervisor,'/waves',{'zone':'A','ship_date':str(today),'max_tasks':'12'});x.waves();wave=list(s['waves'].values())[-1]
 for task in c.tasks(order):
  x=handler(supervisor,'/pick-tasks/'+str(task['id'])+'/confirm',{'bin_code':b['code'],'lot_code':'VERIFY','units':str(task['units'])});x.waves()
 x=handler(supervisor,'/orders/'+str(order['id'])+'/ship',{});x.orders();assert order['status']=='complete';sh=list(s['shipments'].values())[-1];assert sh['invoice_total_cents']==34646
 x=handler(user,'/orders/'+str(order['id'])+'/rmas',{'line_no':'2','units':'5','reason':'Quality issue'});x.orders();r=list(s['rmas'].values())[-1]
 x=handler(supervisor,'/rmas/'+str(r['id'])+'/receive',{});x.fulfilment();assert r['credit']==2118,r
 x=handler(supervisor,'/rmas/'+str(r['id'])+'/inspect',{'outcome':'restock'});x.fulfilment();assert r['disposition']=='restocked'
 for path in ['/orders/'+str(order['id']),'/waves/'+str(wave['id']),'/shipments/'+str(sh['id']),'/rmas/'+str(r['id'])]:
  x=handler(supervisor,path);x.dispatch();assert x.result[1]==200;assert 'data-action="signout"' in x.result[0]
 # Stale counts reject without a movement.
 lid=next(l['id'] for l in c.lots.values() if l['sku_id']==skus[0]['id']);version=c.version[lid]
 x=handler(supervisor,'/lots/'+str(lid)+'/count',{'counted_units':'88','version':str(version)});x.dispatch();assert c.version[lid]==version+1
 try:x.dispatch();raise AssertionError('stale count accepted')
 except app.Fail as e:assert e.code==409
 # Tax delta for cancellation and route tie breaking.
 order['lines'][1]['cancelled']=18;assert 34646-c.due(order)==7624
 bins={code:next(b for b in c.bins.values() if b['code']==code) for code in ['A03-07-2','A05-15-1','A03-18-4','A05-04-3']}
 assert app.routepath(bins)==(['A03-07-2','A03-18-4','A05-15-1','A05-04-3'],82)
 bins={code:next(b for b in c.bins.values() if b['code']==code) for code in ['A01-05-1','A01-12-3','A02-08-2']};assert app.routepath(bins)[1]==56
 print('PASS: exact cents, webhook replay, allocation, wave, picking, shipping, invoice, returns, credit, restock, stale count, route examples, page markers')
finally:D.q('ROLLBACK');D.close()
