"""Transaction-isolated account and reset checks with captured outgoing mail."""
import unittest,secrets
from datetime import timedelta
from db import DB
import app
class AccountTests(unittest.TestCase):
 def setUp(self):
  self.d=DB();self.d.q('BEGIN');self.original_send=app.send;self.sent=[];app.send=lambda *a:self.sent.append(a)
  self.addr='auth-'+secrets.token_hex(8)+'@example.test'
  self.u=self.d.one("INSERT INTO users(email,full_name,role) VALUES($1,'Account test','patient') RETURNING *",'  '+self.addr.upper()+'  ')
 def tearDown(self):self.d.q('ROLLBACK');self.d.close();app.send=self.original_send
 def request(self,path,fields,post=True):
  h=app.Handler.__new__(app.Handler);h.d=self.d;h.u=None;h.csrf='test';h.pathonly=path;h.f=fields;h.post=post;h.sess={'token_hash':'nonexistent'};h.newcookie=None
  result={};h.page=lambda title,body,status=200:result.update(title=title,body=body,status=status);h.redirect=lambda path:result.update(redirect=path)
  h.route();return result
 def test_canonical_account(self):
  self.d.q("INSERT INTO users(email,full_name,role) VALUES($1,'Duplicate test','patient')",self.addr)
  self.assertEqual(app.account(self.d,self.addr.upper())['id'],self.u['id'])
  self.assertIsNone(app.account(self.d,'missing@example.test'))
 def test_reset_single_use_replacement_and_sessions(self):
  expected=self.request('/password-reset',{'email':'unknown@example.test'})
  actual=self.request('/password-reset',{'email':self.addr.upper()});self.assertEqual(expected,actual);self.assertEqual(len(self.sent),1)
  old=self.sent[-1][2].removeprefix(app.BASE)
  self.request('/password-reset',{'email':self.addr});new=self.sent[-1][2].removeprefix(app.BASE)
  with self.assertRaises(app.HTTPError) as err:self.request(old,{'password':'Long-password-value'})
  self.assertEqual(err.exception.status,400)
  self.assertEqual(err.exception.message,'This link is no longer valid.')
  self.d.q('INSERT INTO hv_sessions(token_hash,csrf_hash,user_id) VALUES($1,$2,$3)',app.digest('sample-session'),app.digest('csrf'),self.u['id'])
  self.request(new,{'password':'Long-password-value'})
  self.assertIsNone(self.d.one('SELECT token_hash FROM hv_sessions WHERE user_id=$1',self.u['id']))
  stored=self.d.one('SELECT password_hash FROM hv_auth WHERE user_id=$1',self.u['id'])['password_hash'];self.assertNotIn('Long-password-value',stored);self.assertTrue(app.checkpw('Long-password-value',stored))
  with self.assertRaises(app.HTTPError) as err:self.request(new,{'password':'Another-password-value'})
  self.assertEqual(err.exception.status,400)
if __name__=='__main__':unittest.main()
