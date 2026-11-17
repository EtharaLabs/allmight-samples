CREATE TABLE organizations (
    id serial PRIMARY KEY,
    name text NOT NULL,
    created_at timestamptz NOT NULL
);
CREATE TABLE users (
    id serial PRIMARY KEY,
    email text NOT NULL,
    full_name text NOT NULL,
    created_at timestamptz NOT NULL
);
CREATE TABLE memberships (
    organization_id integer NOT NULL REFERENCES organizations (id),
    user_id integer NOT NULL REFERENCES users (id),
    role text NOT NULL CHECK (role IN ('owner', 'manager', 'box_office', 'scanner')),
    PRIMARY KEY (organization_id, user_id)
);
CREATE TABLE venues (
    id serial PRIMARY KEY,
    organization_id integer NOT NULL REFERENCES organizations (id),
    name text NOT NULL,
    timezone text NOT NULL,
    address text NOT NULL
);
CREATE TABLE sections (
    id serial PRIMARY KEY,
    venue_id integer NOT NULL REFERENCES venues (id),
    code text NOT NULL,
    name text NOT NULL,
    UNIQUE (venue_id, code)
);
CREATE TABLE seats (
    id serial PRIMARY KEY,
    section_id integer NOT NULL REFERENCES sections (id),
    row_label text NOT NULL,
    seat_number integer NOT NULL,
    accessible boolean NOT NULL DEFAULT false,
    UNIQUE (section_id, row_label, seat_number)
);
CREATE TABLE events (
    id serial PRIMARY KEY,
    organization_id integer NOT NULL REFERENCES organizations (id),
    venue_id integer NOT NULL REFERENCES venues (id),
    name text NOT NULL,
    description text NOT NULL DEFAULT '',
    starts_local timestamp NOT NULL,
    starts_fold smallint NOT NULL DEFAULT 0 CHECK (starts_fold IN (0, 1)),
    on_sale_local timestamp,
    on_sale_fold smallint NOT NULL DEFAULT 0 CHECK (on_sale_fold IN (0, 1)),
    status text NOT NULL,
    facility_fee_cents integer NOT NULL CHECK (facility_fee_cents >= 0)
);
CREATE TABLE event_prices (
    event_id integer NOT NULL REFERENCES events (id),
    section_id integer NOT NULL REFERENCES sections (id),
    price_cents integer NOT NULL CHECK (price_cents > 0),
    PRIMARY KEY (event_id, section_id)
);
CREATE TABLE legacy_orders (
    id integer PRIMARY KEY,
    event_id integer NOT NULL REFERENCES events (id),
    buyer_email text NOT NULL,
    buyer_name text NOT NULL,
    placed_local timestamp NOT NULL,
    placed_fold smallint NOT NULL DEFAULT 0 CHECK (placed_fold IN (0, 1)),
    total_cents integer NOT NULL,
    old_payment_ref text NOT NULL
);
CREATE INDEX legacy_orders_event ON legacy_orders (event_id);
CREATE TABLE legacy_tickets (
    id integer PRIMARY KEY,
    order_id integer NOT NULL REFERENCES legacy_orders (id),
    seat_id integer NOT NULL REFERENCES seats (id),
    holder_email text NOT NULL,
    holder_name text NOT NULL,
    status text NOT NULL CHECK (status IN ('valid', 'comp', 'refunded')),
    face_cents integer NOT NULL,
    discount_cents integer NOT NULL DEFAULT 0,
    service_fee_cents integer NOT NULL,
    facility_fee_cents integer NOT NULL,
    tax_cents integer NOT NULL,
    total_cents integer NOT NULL,
    refunded_cents integer NOT NULL DEFAULT 0,
    refunded_local timestamp,
    refunded_fold smallint NOT NULL DEFAULT 0 CHECK (refunded_fold IN (0, 1)),
    barcode text NOT NULL UNIQUE,
    checked_in_local timestamp,
    checked_in_fold smallint NOT NULL DEFAULT 0 CHECK (checked_in_fold IN (0, 1)),
    checked_in_gate text
);
CREATE INDEX legacy_tickets_order ON legacy_tickets (order_id);
CREATE INDEX legacy_tickets_seat ON legacy_tickets (seat_id);
CREATE TABLE legacy_transfers (
    id integer PRIMARY KEY,
    ticket_id integer NOT NULL REFERENCES legacy_tickets (id),
    from_email text NOT NULL,
    to_email text NOT NULL,
    transferred_local timestamp NOT NULL,
    transferred_fold smallint NOT NULL DEFAULT 0 CHECK (transferred_fold IN (0, 1))
);
CREATE INDEX legacy_transfers_ticket ON legacy_transfers (ticket_id);
