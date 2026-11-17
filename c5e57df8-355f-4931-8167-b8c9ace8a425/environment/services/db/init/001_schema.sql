CREATE TABLE sites (
    id integer PRIMARY KEY,
    name text NOT NULL,
    timezone text NOT NULL
);
CREATE TABLE bins (
    id serial PRIMARY KEY,
    code text NOT NULL UNIQUE,
    zone text NOT NULL CHECK (zone IN ('A', 'C', 'F', 'Q')),
    aisle integer,
    bay integer,
    level integer
);
CREATE TABLE skus (
    id serial PRIMARY KEY,
    code text NOT NULL UNIQUE,
    name text NOT NULL,
    case_pack integer NOT NULL CHECK (case_pack > 0),
    unit_weight_g integer NOT NULL CHECK (unit_weight_g > 0),
    length_cm integer NOT NULL,
    width_cm integer NOT NULL,
    height_cm integer NOT NULL,
    storage text NOT NULL CHECK (storage IN ('ambient', 'chilled', 'frozen')),
    taxable boolean NOT NULL,
    reorder_point integer NOT NULL DEFAULT 0,
    active boolean NOT NULL DEFAULT true
);
CREATE TABLE tax_rates (
    state text PRIMARY KEY,
    rate numeric(7, 5) NOT NULL,
    freight_taxable boolean NOT NULL
);
CREATE TABLE price_list (
    id serial PRIMARY KEY,
    tier text NOT NULL CHECK (tier IN ('standard', 'gold', 'platinum')),
    sku_id integer NOT NULL REFERENCES skus (id),
    unit_price_cents integer NOT NULL CHECK (unit_price_cents > 0),
    effective_from date NOT NULL,
    UNIQUE (tier, sku_id, effective_from)
);
CREATE TABLE customers (
    id serial PRIMARY KEY,
    name text NOT NULL,
    tier text NOT NULL CHECK (tier IN ('standard', 'gold', 'platinum')),
    ship_to_state text NOT NULL REFERENCES tax_rates (state),
    approval_limit_cents bigint,
    min_shelf_life_days integer NOT NULL DEFAULT 0,
    allow_split_cases boolean NOT NULL DEFAULT false,
    backorders_allowed boolean NOT NULL DEFAULT true
);
CREATE TABLE users (
    id serial PRIMARY KEY,
    email text NOT NULL,
    full_name text NOT NULL,
    role text NOT NULL CHECK (role IN ('buyer', 'approver', 'picker', 'supervisor', 'finance')),
    customer_id integer REFERENCES customers (id),
    zone text CHECK (zone IN ('A', 'C', 'F')),
    active boolean NOT NULL DEFAULT true
);
CREATE TABLE lots (
    id serial PRIMARY KEY,
    sku_id integer NOT NULL REFERENCES skus (id),
    lot_code text NOT NULL,
    expiry_date date NOT NULL,
    bin_id integer NOT NULL REFERENCES bins (id),
    unit_cost numeric(12, 4) NOT NULL CHECK (unit_cost > 0),
    qa_hold boolean NOT NULL DEFAULT false
);
CREATE INDEX lots_sku ON lots (sku_id);
CREATE TABLE legacy_movements (
    id bigint PRIMARY KEY,
    lot_id integer NOT NULL REFERENCES lots (id),
    kind text NOT NULL CHECK (kind IN ('receipt', 'ship', 'adjust', 'writeoff')),
    units integer NOT NULL,
    local_time timestamp NOT NULL,
    fold smallint NOT NULL DEFAULT 0 CHECK (fold IN (0, 1)),
    ref text
);
CREATE INDEX legacy_movements_lot ON legacy_movements (lot_id);
CREATE TABLE legacy_orders (
    id integer PRIMARY KEY,
    number text NOT NULL UNIQUE,
    customer_id integer NOT NULL REFERENCES customers (id),
    placed_by integer REFERENCES users (id),
    placed_local timestamp NOT NULL,
    fold smallint NOT NULL DEFAULT 0 CHECK (fold IN (0, 1)),
    ship_date date NOT NULL,
    po_number text,
    status text NOT NULL CHECK (status IN ('shipped', 'cancelled')),
    subtotal_cents bigint NOT NULL,
    tax_cents bigint NOT NULL,
    freight_cents bigint NOT NULL,
    freight_tax_cents bigint NOT NULL,
    total_cents bigint NOT NULL
);
CREATE TABLE legacy_order_lines (
    order_id integer NOT NULL REFERENCES legacy_orders (id),
    line_no integer NOT NULL,
    sku_id integer NOT NULL REFERENCES skus (id),
    qty_units integer NOT NULL,
    unit_price_cents integer NOT NULL,
    PRIMARY KEY (order_id, line_no)
);
