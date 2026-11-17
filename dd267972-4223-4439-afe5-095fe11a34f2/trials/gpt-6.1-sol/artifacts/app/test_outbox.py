"""Use an in-memory SMTP sink to verify a 1500-line outbox drains once within 30 seconds.
Stop the production server before running; only this script's fixture run is removed.
"""
import app,time,threading
from db import DB
received=[];lock=threading.Lock();rid=None

def sink(*args):
 time.sleep(.05)
 with lock:received.append(args)
app.sendmail=sink
try:
 with DB() as d:
  rid=d.one("INSERT INTO sb_runs(period_start,status,snapshot) VALUES('2099-01-01','finalized','{}') RETURNING id")['id']
  d.q("INSERT INTO sb_outbox(run_id,employee_id,recipient,subject,body) SELECT $1,id,'Test <test@example.test>','test','test' FROM employees ORDER BY id LIMIT 1500",rid)
 start=time.monotonic()
 for _ in range(16):threading.Thread(target=app.mailworker,daemon=True).start()
 while len(received)<1500 and time.monotonic()-start<29:time.sleep(.1)
 elapsed=time.monotonic()-start
 assert len(received)==1500,(len(received),elapsed)
 time.sleep(1)
 assert len(received)==1500
 print(f'1500 statements delivered exactly once to the test sink in {elapsed:.1f}s')
finally:
 if rid:
  with DB() as d:d.q('DELETE FROM sb_outbox WHERE run_id=$1',rid);d.q('DELETE FROM sb_runs WHERE id=$1',rid)
