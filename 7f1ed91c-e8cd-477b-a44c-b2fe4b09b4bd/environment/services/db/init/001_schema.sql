CREATE TABLE households (
    id integer PRIMARY KEY,
    name text NOT NULL
);
CREATE TABLE users (
    id serial PRIMARY KEY,
    household_id integer REFERENCES households (id),
    email text NOT NULL,
    full_name text NOT NULL,
    role text NOT NULL CHECK (role IN ('owner', 'member', 'viewer', 'advisor', 'preparer')),
    active boolean NOT NULL DEFAULT true
);
CREATE TABLE assignments (
    user_id integer NOT NULL REFERENCES users (id),
    household_id integer NOT NULL REFERENCES households (id),
    PRIMARY KEY (user_id, household_id)
);
CREATE TABLE accounts (
    id serial PRIMARY KEY,
    household_id integer NOT NULL REFERENCES households (id),
    name text NOT NULL,
    broker text NOT NULL CHECK (broker IN ('northwind', 'harbor')),
    account_number text NOT NULL,
    lot_method text NOT NULL CHECK (lot_method IN ('FIFO', 'HIFO')),
    opened_on date NOT NULL,
    account_type text NOT NULL DEFAULT 'taxable' CHECK (account_type IN ('taxable', 'ira')),
    UNIQUE (broker, account_number)
);
CREATE TABLE lot_method_changes (
    id serial PRIMARY KEY,
    account_id integer NOT NULL REFERENCES accounts (id),
    effective_from date NOT NULL,
    lot_method text NOT NULL CHECK (lot_method IN ('FIFO', 'HIFO')),
    recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE securities (
    id serial PRIMARY KEY,
    household_id integer NOT NULL REFERENCES households (id),
    symbol text NOT NULL,
    isin text NOT NULL,
    name text NOT NULL,
    currency text NOT NULL CHECK (currency IN ('USD', 'CAD', 'EUR'))
);
CREATE UNIQUE INDEX securities_symbol ON securities (household_id, upper(symbol));
CREATE UNIQUE INDEX securities_isin ON securities (household_id, upper(isin));
CREATE TABLE fx_rates (
    currency text NOT NULL,
    rate_date date NOT NULL,
    rate numeric(12, 6) NOT NULL,
    quote text NOT NULL CHECK (quote IN ('usd_per_unit', 'units_per_usd')),
    PRIMARY KEY (currency, rate_date)
);
CREATE TABLE security_prices (
    security_id integer NOT NULL REFERENCES securities (id),
    price_date date NOT NULL,
    close numeric(14, 4) NOT NULL,
    PRIMARY KEY (security_id, price_date)
);
CREATE TABLE legacy_transactions (
    id bigint PRIMARY KEY,
    account_id integer NOT NULL REFERENCES accounts (id),
    security_id integer NOT NULL REFERENCES securities (id),
    kind text NOT NULL,
    trade_date date NOT NULL,
    quantity numeric(20, 8),
    amount numeric(14, 2),
    currency text NOT NULL,
    split_new integer,
    split_old integer,
    to_account_id integer REFERENCES accounts (id),
    ref text,
    memo text
);
CREATE INDEX legacy_transactions_account ON legacy_transactions (account_id);
CREATE INDEX legacy_transactions_security ON legacy_transactions (security_id);
CREATE TABLE corporate_actions (
    id integer PRIMARY KEY,
    household_id integer NOT NULL REFERENCES households (id),
    security_id integer NOT NULL REFERENCES securities (id),
    kind text NOT NULL CHECK (kind IN ('spinoff', 'merger', 'return_of_capital')),
    action_date date NOT NULL,
    new_security_id integer REFERENCES securities (id),
    ratio numeric(20, 8),
    parent_fmv numeric(16, 6),
    new_fmv numeric(16, 6),
    cash_in_lieu_price numeric(16, 6),
    acquirer_security_id integer REFERENCES securities (id),
    shares_per_share numeric(20, 8),
    cash_per_share numeric(16, 6),
    acquirer_fmv numeric(16, 6),
    amount_per_share numeric(16, 6)
);
CREATE TABLE legacy_year_locks (
    household_id integer NOT NULL REFERENCES households (id),
    year integer NOT NULL,
    locked_at timestamptz NOT NULL,
    PRIMARY KEY (household_id, year)
);
CREATE TABLE legacy_locked_figures (
    household_id integer NOT NULL REFERENCES households (id),
    year integer NOT NULL,
    security_id integer NOT NULL REFERENCES securities (id),
    short_term_gain_cents bigint NOT NULL,
    long_term_gain_cents bigint NOT NULL,
    wash_disallowed_cents bigint NOT NULL,
    dividends_cents bigint NOT NULL,
    PRIMARY KEY (household_id, year, security_id)
);
