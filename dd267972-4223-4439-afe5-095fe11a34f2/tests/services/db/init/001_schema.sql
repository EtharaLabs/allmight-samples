CREATE TABLE locations (
    id integer PRIMARY KEY,
    name text NOT NULL,
    timezone text NOT NULL,
    ruleset text NOT NULL CHECK (ruleset IN ('CA', 'FLSA'))
);
CREATE TABLE employees (
    id serial PRIMARY KEY,
    email text NOT NULL,
    full_name text NOT NULL,
    location_id integer NOT NULL REFERENCES locations (id),
    manager_id integer REFERENCES employees (id),
    role text NOT NULL CHECK (role IN ('employee', 'manager', 'payroll_admin')),
    active boolean NOT NULL DEFAULT true,
    hired_on date NOT NULL
);
CREATE TABLE job_rates (
    id serial PRIMARY KEY,
    employee_id integer NOT NULL REFERENCES employees (id),
    job_code text NOT NULL,
    rate_cents integer NOT NULL CHECK (rate_cents > 0),
    effective_from date NOT NULL,
    UNIQUE (employee_id, job_code, effective_from)
);
CREATE TABLE legacy_punches (
    id bigint PRIMARY KEY,
    employee_id integer NOT NULL REFERENCES employees (id),
    job_code text,
    kind text NOT NULL CHECK (kind IN ('in', 'out', 'break_start', 'break_end')),
    local_time timestamp NOT NULL,
    fold smallint NOT NULL DEFAULT 0 CHECK (fold IN (0, 1))
);
CREATE INDEX legacy_punches_employee ON legacy_punches (employee_id);
CREATE TABLE legacy_approvals (
    employee_id integer NOT NULL REFERENCES employees (id),
    week_start date NOT NULL,
    approved_by integer NOT NULL REFERENCES employees (id),
    approved_at timestamptz NOT NULL,
    PRIMARY KEY (employee_id, week_start)
);
