import unittest
from payroll import *
class PayrollTests(unittest.TestCase):
 def calc(self,rows,rules='CA',rates=None):
  e={'id':'1','timezone':'America/Los_Angeles','ruleset':rules}
  ps=[(datetime.fromisoformat(t).astimezone(UTC),kind,job) for t,kind,job in rows]
  return calculate(e,ps,rates or [{'job_code':'COOK','rate_cents':'2000','effective_from':'2026-01-01'}],{})
 def shift(self,start,end):return [(start,'in','COOK'),(end,'out',None)]
 def test_daily_overtime(self):
  w=self.calc(self.shift('2026-09-21T06:00:00-07:00','2026-09-21T19:00:00-07:00'))['2026-09-20']
  self.assertEqual((w['reg'],w['ot'],w['dt'],w['gross_cents']),(8*3600,4*3600,3600,34000))
 def test_seventh_day_and_weekly(self):
  ps=[]
  for i in range(7):ps+=self.shift(f'2026-09-{20+i}T08:00:00-07:00',f'2026-09-{20+i}T16:00:00-07:00')
  w=self.calc(ps)['2026-09-20'];self.assertEqual((w['reg']/3600,w['ot']/3600,w['dt']),(40,16,0))
 def test_meal_timely(self):
  ps=[('2026-09-21T08:00:00-07:00','in','COOK'),('2026-09-21T13:00:00-07:00','break_start',None),('2026-09-21T13:30:00-07:00','break_end',None),('2026-09-21T17:00:00-07:00','out',None)]
  w=self.calc(ps)['2026-09-20'];self.assertEqual(w['meals'],0);self.assertEqual(w['ot'],1800)
 def test_interrupted_shift(self):
  ps=[('2026-09-21T08:00:00-07:00','in','COOK'),('2026-09-21T09:00:00-07:00','in','COOK'),('2026-09-21T10:00:00-07:00','out',None)]
  w=self.calc(ps)['2026-09-20'];self.assertEqual(w['reg'],0);self.assertEqual(w['status'],'exception')
 def test_dst_elapsed(self):
  w=self.calc(self.shift('2026-11-01T00:00:00-07:00','2026-11-01T04:00:00-08:00'))['2026-11-01'];self.assertEqual(w['reg'],18000)
 def test_rounding_tie(self):
  a=datetime(2026,9,21,tzinfo=UTC);self.assertEqual(roundtime(a+timedelta(minutes=3))-roundtime(a),360)
 def test_weighted_rate(self):
  ps=self.shift('2026-09-21T06:00:00-07:00','2026-09-21T14:00:00-07:00')+ [('2026-09-21T15:00:00-07:00','in','PREP'),('2026-09-21T19:00:00-07:00','out',None)]
  rates=[{'job_code':j,'rate_cents':str(r),'effective_from':'2026-01-01'} for j,r in [('COOK',2000),('PREP',3000)]]
  w=self.calc(ps,rates=rates)['2026-09-20'];self.assertEqual(w['gross_cents'],35000)
 def test_missing_rate_and_long_shift(self):
  w=self.calc(self.shift('2025-09-21T08:00:00-07:00','2025-09-21T12:00:00-07:00'))['2025-09-21'];self.assertEqual(w['status'],'exception')
  w=self.calc(self.shift('2026-09-21T01:00:00-07:00','2026-09-21T18:00:00-07:00'))['2026-09-20'];self.assertEqual(w['reg'],0)
if __name__=='__main__':unittest.main()
