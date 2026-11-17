CREATE TABLE IF NOT EXISTS sb_auth(employee_id integer PRIMARY KEY REFERENCES employees(id), password_hash text, failures integer NOT NULL DEFAULT 0, locked_until timestamptz);
CREATE TABLE IF NOT EXISTS sb_sessions(hash text PRIMARY KEY, employee_id integer REFERENCES employees(id), csrf text NOT NULL, created_at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS sb_resets(hash text PRIMARY KEY, employee_id integer REFERENCES employees(id), requested_at timestamptz NOT NULL DEFAULT now(), used boolean NOT NULL DEFAULT false, replaced boolean NOT NULL DEFAULT false);
CREATE TABLE IF NOT EXISTS sb_devices(id bigserial PRIMARY KEY, location_id integer REFERENCES locations(id), label text NOT NULL, key_hash text NOT NULL, revoked boolean NOT NULL DEFAULT false);
CREATE TABLE IF NOT EXISTS sb_punches(punch_id text PRIMARY KEY, employee_id integer REFERENCES employees(id), kind text NOT NULL, at timestamptz NOT NULL, job_code text, payload text NOT NULL, UNIQUE(employee_id,at));
CREATE TABLE IF NOT EXISTS sb_approvals(employee_id integer REFERENCES employees(id), week_start date, approved boolean NOT NULL, approved_by integer REFERENCES employees(id), PRIMARY KEY(employee_id,week_start));
CREATE TABLE IF NOT EXISTS sb_runs(id bigserial PRIMARY KEY, period_start date UNIQUE NOT NULL, status text NOT NULL DEFAULT 'draft', version integer NOT NULL DEFAULT 1, snapshot jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS sb_outbox(id bigserial PRIMARY KEY, run_id bigint REFERENCES sb_runs(id), employee_id integer REFERENCES employees(id), recipient text NOT NULL, subject text NOT NULL, body text NOT NULL, sent boolean NOT NULL DEFAULT false, UNIQUE(run_id,employee_id));
CREATE INDEX IF NOT EXISTS sb_punches_employee ON sb_punches(employee_id);
