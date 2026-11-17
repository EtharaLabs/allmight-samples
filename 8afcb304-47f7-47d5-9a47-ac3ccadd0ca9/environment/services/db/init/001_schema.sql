CREATE TABLE clinics (
    id integer PRIMARY KEY,
    name text NOT NULL,
    timezone text NOT NULL
);
CREATE TABLE clinic_closures (
    id serial PRIMARY KEY,
    clinic_id integer NOT NULL REFERENCES clinics (id),
    on_date date NOT NULL,
    reason text NOT NULL,
    UNIQUE (clinic_id, on_date)
);
CREATE TABLE rooms (
    id serial PRIMARY KEY,
    clinic_id integer NOT NULL REFERENCES clinics (id),
    name text NOT NULL,
    room_type text NOT NULL,
    active boolean NOT NULL DEFAULT true
);
CREATE TABLE users (
    id serial PRIMARY KEY,
    email text NOT NULL,
    full_name text NOT NULL,
    role text NOT NULL CHECK (role IN ('patient', 'clinician', 'front_desk', 'manager', 'admin')),
    clinic_id integer REFERENCES clinics (id),
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE clinicians (
    id serial PRIMARY KEY,
    user_id integer NOT NULL UNIQUE REFERENCES users (id),
    clinic_id integer NOT NULL REFERENCES clinics (id),
    max_per_day integer NOT NULL CHECK (max_per_day > 0),
    active boolean NOT NULL DEFAULT true
);
CREATE TABLE appointment_types (
    id serial PRIMARY KEY,
    code text NOT NULL UNIQUE,
    name text NOT NULL,
    duration_minutes integer NOT NULL CHECK (duration_minutes > 0),
    buffer_minutes integer NOT NULL DEFAULT 0 CHECK (buffer_minutes >= 0),
    room_type text NOT NULL,
    active boolean NOT NULL DEFAULT true
);
CREATE TABLE type_fees (
    id serial PRIMARY KEY,
    type_id integer NOT NULL REFERENCES appointment_types (id),
    effective_from date NOT NULL,
    fee_cents integer NOT NULL CHECK (fee_cents > 0),
    deposit_bp integer NOT NULL CHECK (deposit_bp > 0 AND deposit_bp <= 10000),
    UNIQUE (type_id, effective_from)
);
CREATE TABLE schedule_versions (
    id serial PRIMARY KEY,
    clinician_id integer NOT NULL REFERENCES clinicians (id),
    effective_from date NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE schedule_windows (
    id serial PRIMARY KEY,
    version_id integer NOT NULL REFERENCES schedule_versions (id),
    weekday smallint NOT NULL CHECK (weekday BETWEEN 1 AND 7),
    start_time time NOT NULL,
    end_time time NOT NULL
);
CREATE TABLE schedule_exceptions (
    id serial PRIMARY KEY,
    clinician_id integer NOT NULL REFERENCES clinicians (id),
    on_date date NOT NULL,
    kind text NOT NULL CHECK (kind IN ('off', 'extra')),
    start_time time,
    end_time time
);
CREATE TABLE appointments (
    id serial PRIMARY KEY,
    patient_id integer NOT NULL REFERENCES users (id),
    clinician_id integer NOT NULL REFERENCES clinicians (id),
    type_id integer NOT NULL REFERENCES appointment_types (id),
    room_id integer REFERENCES rooms (id),
    local_start timestamp NOT NULL,
    fold smallint NOT NULL DEFAULT 0 CHECK (fold IN (0, 1)),
    status text NOT NULL,
    deposit_cents integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX appointments_clinician ON appointments (clinician_id, local_start);
CREATE INDEX appointments_room ON appointments (room_id, local_start);
CREATE INDEX appointments_patient ON appointments (patient_id);
CREATE TABLE clinical_notes (
    id serial PRIMARY KEY,
    appointment_id integer NOT NULL REFERENCES appointments (id),
    author_id integer NOT NULL REFERENCES users (id),
    body text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX clinical_notes_appointment ON clinical_notes (appointment_id);
