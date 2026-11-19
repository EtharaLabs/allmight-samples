import unittest
from engine import *
class FakeDB:
 def q(self,sql,*args):return []
class Rules(unittest.TestCase):
 def setup_engine(self,method='FIFO',ira=False):
  en=object.__new__(Engine);en.d=FakeDB();en.h=1;en.accounts={1:dict(name='Taxable',lot_method=method,account_type='taxable'),2:dict(name='Other',lot_method='FIFO',account_type='ira' if ira else 'taxable')};en.secs={1:dict(symbol='P',name='Parent',currency='USD'),2:dict(symbol='N',name='New',currency='USD')};en.rates={};en.methods={};en.tx=[];en.actions=[];en.lots=[];en.bysec={};en.byid={};en.real=[];en.div=[];en.errors=[];en.pending={};en.serial=0
  return en
 def tx(self,en,i,day,k,q,amount,a=1):en.tx.append(dict(id=i,account_id=a,security_id=1,kind=k,trade_date=dt(day),quantity=num(q),amount=num(amount)))
 def test_fifo(self):
  en=self.setup_engine();self.tx(en,1,'2024-03-04','buy',10,'1000.03');self.tx(en,2,'2024-06-03','buy',4,'500.02');self.tx(en,3,'2025-04-01','sell',12,'1500.02');en.run()
  self.assertEqual([(r['p'],r['b'],r['g'],r['term']) for r in en.real],[(125002,100003,24999,'long'),(25000,25001,-1,'short')]);self.assertEqual(en.open(1,1)[0]['b'],25001)
 def test_future_wash(self):
  en=self.setup_engine();self.tx(en,1,'2025-01-06','buy',100,5000);self.tx(en,2,'2025-03-03','sell',100,4000);self.tx(en,3,'2025-03-20','buy',30,1230,2);self.tx(en,4,'2025-03-24','buy',80,'3360.01');self.tx(en,5,'2025-04-17','buy',40,1700);en.run()
  self.assertEqual(en.real[0]['w'],100000);self.assertEqual(en.real[0]['g'],0);ls=en.open(1,1);self.assertEqual([(dec(l['q']),l['b'],str(l['start'])) for l in ls],[('70',364001,'2025-01-27'),('10',42000,'2025-03-24'),('40',170000,'2025-04-17')])
 def test_prior_wash(self):
  en=self.setup_engine('HIFO');self.tx(en,1,'2025-05-05','buy',20,2000);self.tx(en,2,'2025-05-20','buy',20,2600);self.tx(en,3,'2025-06-02','sell',10,1100);self.tx(en,4,'2025-06-09','sell',10,1150);self.tx(en,5,'2025-06-16','reinvest','0.4375','50.31',2);en.run()
  self.assertEqual([r['w'] for r in en.real],[20000,656]);self.assertEqual(en.open(2,1)[0]['b'],5687)
 def test_ira(self):
  en=self.setup_engine(ira=True);self.tx(en,1,'2025-01-06','buy',10,400);self.tx(en,2,'2025-03-03','sell',10,350);self.tx(en,3,'2025-03-10','buy',10,300,2);en.run();l=en.open(2,1)[0];self.assertEqual(en.real[0]['w'],5000);self.assertEqual(l['b'],30000);self.assertEqual(str(l['start']),'2025-03-10');self.assertFalse(l['wash'])
 def test_spinoff(self):
  en=self.setup_engine();self.tx(en,1,'2024-03-04','buy',10,'1000.03');self.tx(en,2,'2024-05-06','buy',7,770);en.actions=[dict(id=1,security_id=1,kind='spinoff',action_date='2024-08-05',new_security_id=2,ratio='0.5',parent_fmv=90,new_fmv=20,cash_in_lieu_price='21.50')];en.run();self.assertEqual([(l['q'],l['b']) for l in en.open(1,2)],[(5,10000),(3,6600)]);self.assertEqual([(r['p'],r['b'],r['g']) for r in en.real],[(1075,1100,-25)])
 def test_merger(self):
  en=self.setup_engine();self.tx(en,1,'2023-01-10','buy',10,500);self.tx(en,2,'2023-02-14','buy',10,600);en.actions=[dict(id=1,security_id=1,kind='merger',action_date='2025-06-16',acquirer_security_id=2,shares_per_share='0.5',cash_per_share=12,acquirer_fmv=80)];en.run();self.assertEqual([r['g'] for r in en.real],[2000,0]);self.assertEqual([l['b'] for l in en.open(1,2)],[40000,48000]);self.assertEqual(len(en.open(1,1)),0)
 def test_roc(self):
  en=self.setup_engine();self.tx(en,1,'2025-02-03','buy',100,40);en.actions=[dict(id=i,security_id=1,kind='return_of_capital',action_date=day,amount_per_share='0.25') for i,day in [(1,'2025-04-01'),(2,'2025-07-01')]];en.run();self.assertEqual(en.open(1,1)[0]['b'],0);self.assertEqual(en.real[0]['g'],1000)
 def test_fx(self):
  en=self.setup_engine();en.secs[1]['currency']='EUR';en.rates={'EUR':[(dt('2025-03-07'),num('1.085'))]};self.assertEqual(en.usd(1,num('4321.40'),'2025-03-09'),468872)
 def test_adjustments_do_not_repeat(self):
  en=self.setup_engine();self.tx(en,1,'2024-02-01','buy',10,1000);self.tx(en,2,'2024-08-01','sell',10,1500);en.run()
  frozen2024={1:[50000,0,0,0,0,0,0]};locks={2024:dict(summary=en.summary(frozen2024),realized=en.realized(2024),figures=json.dumps(frozen2024))};en.locks=lambda:locks
  self.tx(en,3,'2024-01-15','buy',10,800)
  # Replay from a clean engine with the additional old acquisition.
  newer=self.setup_engine();newer.tx=en.tx;newer.locks=lambda:locks;newer.run()
  self.assertEqual(newer.report(2025)[2][1][4],20000)
  self.assertEqual(newer.report(2024)[0],locks[2024]['summary'])
  summary,realized,f=newer.report(2025);locks[2025]=dict(summary=summary,realized=realized,figures=json.dumps(f))
  self.tx(newer,4,'2024-11-15','dividend',0,'12.34');final=self.setup_engine();final.tx=newer.tx;final.locks=lambda:locks;final.run();f=final.report(2026)[2];self.assertEqual(f[1][4],0);self.assertEqual(f[1][6],1234)
 def test_csv(self):
  s=csvout(['text','number'],[[safe('=x,"a"'),23]]);self.assertEqual(s,'text,number\r\n"\'=x,""a""",23\r\n')
if __name__=='__main__':unittest.main()
