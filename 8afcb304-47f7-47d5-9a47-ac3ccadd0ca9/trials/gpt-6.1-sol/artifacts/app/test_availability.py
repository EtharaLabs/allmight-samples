"""Transaction-isolated checks of schedule, buffer, fee, and DST rules.
Run with the application's environment exported. Added fixture rows are rolled back.
"""
import unittest,secrets
from datetime import datetime,date,timezone,timedelta
from zoneinfo import ZoneInfo
import app
from db import DB
class AvailabilityTests(unittest.TestCase):
 def setUp(self):
  self.d=DB();self.d.q('BEGIN');self.old_now=app.now;app.now=lambda:datetime(2026,12,1,tzinfo=timezone.utc)
  self.uid=self.d.one("INSERT INTO users(email,full_name,role) VALUES($1,'Schedule test','clinician') RETURNING id",'unit-'+secrets.token_hex(8)+'@example.test')['id']
  self.cid=self.d.one('INSERT INTO clinicians(user_id,clinic_id,max_per_day) VALUES($1,1,100) RETURNING id',self.uid)['id'];self.c=app.clinician(self.d,self.cid)
  roomtype=secrets.token_hex(8);self.d.q("INSERT INTO rooms(clinic_id,name,room_type) VALUES(1,'Test room',$1)",roomtype)
  self.t=self.d.one("INSERT INTO appointment_types(code,name,duration_minutes,buffer_minutes,room_type) VALUES($1,'Test type',45,15,$2) RETURNING *",'UNIT'+secrets.token_hex(6),roomtype)
  self.d.q("INSERT INTO type_fees(type_id,effective_from,fee_cents,deposit_bp) VALUES($1,'2020-01-01',12550,2000)",self.t['id'])
 def tearDown(self):
  self.d.q('ROLLBACK');self.d.close();app.now=self.old_now
 def version(self,D,windows):
  v=self.d.one('INSERT INTO schedule_versions(clinician_id,effective_from) VALUES($1,$2) RETURNING id',self.cid,D)['id']
  for weekday,start,end in windows:self.d.q('INSERT INTO schedule_windows(version_id,weekday,start_time,end_time) VALUES($1,$2,$3,$4)',v,weekday,start,end)
 def times(self,D):return [s['start'].astimezone(ZoneInfo(self.c['timezone'])).strftime('%H:%M') for s in app.availability(self.d,self.c,self.t,date.fromisoformat(D))]
 def test_grid_fit_buffer_and_deposit(self):
  self.version('2026-01-01',[(2,'09:10','12:00')]);D=date(2026,12,8)
  other=self.d.one("INSERT INTO appointment_types(code,name,duration_minutes,buffer_minutes,room_type) VALUES($1,'Test busy',30,10,$2) RETURNING *",'UNIT'+secrets.token_hex(6),self.t['room_type'])
  s={'start':app.wall(D,'10:30',self.c['timezone']),'room_id':self.d.one('SELECT id FROM rooms WHERE room_type=$1',self.t['room_type'])['id'],'deposit':0}
  app.create_appt(self.d,self.uid,self.c,other,s,'confirmed')
  self.assertEqual(self.times('2026-12-08'),['09:15','09:30','11:15'])
  self.assertEqual(app.availability(self.d,self.c,self.t,D)[0]['deposit'],2510)
 def test_union_removal_versions_and_closure(self):
  self.d.q('UPDATE appointment_types SET duration_minutes=60 WHERE id=$1',self.t['id']);self.t['duration_minutes']=60
  self.version('2026-01-01',[(1,'08:00','12:00'),(1,'13:00','17:00')])
  self.assertEqual(len(self.times('2026-12-07')),26)
  self.d.q("INSERT INTO schedule_exceptions(clinician_id,on_date,kind,start_time,end_time) VALUES($1,'2026-12-14','off','10:00','11:00')",self.cid)
  self.assertEqual(len(self.times('2026-12-14')),19)
  self.d.q("INSERT INTO schedule_exceptions(clinician_id,on_date,kind,start_time,end_time) VALUES($1,'2026-12-28','extra','12:00','13:00')",self.cid)
  self.assertEqual(len(self.times('2026-12-28')),33)
  self.version('2027-01-04',[(1,'09:00','15:00')]);self.assertEqual(len(self.times('2027-01-04')),21)
 def test_wall_clock_dst(self):
  z='America/Los_Angeles'
  self.assertEqual(app.wall(date(2026,11,1),'01:30',z),datetime(2026,11,1,8,30,tzinfo=timezone.utc))
  self.assertEqual(app.wall(date(2026,11,1),'01:30',z,1),datetime(2026,11,1,9,30,tzinfo=timezone.utc))
  self.assertEqual(app.wall(date(2027,3,14),'02:30',z),datetime(2027,3,14,10,30,tzinfo=timezone.utc))
  self.assertEqual(app.wall(date(2026,12,8),'24:00',z),datetime(2026,12,9,8,tzinfo=timezone.utc))
  self.assertEqual(app.wall(date(2027,3,14),'02:30','Pacific/Honolulu'),datetime(2027,3,14,12,30,tzinfo=timezone.utc))
if __name__=='__main__':unittest.main()
