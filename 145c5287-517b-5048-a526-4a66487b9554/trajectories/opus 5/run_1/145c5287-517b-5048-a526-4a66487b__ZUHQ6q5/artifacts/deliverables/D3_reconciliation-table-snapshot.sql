--
-- PostgreSQL database dump
--

-- Dumped from database version 16.2 (Ubuntu 16.2-1ubuntu4)
-- Dumped by pg_dump version 16.2 (Ubuntu 16.2-1ubuntu4)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: reconciliation; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.reconciliation (
    receipt_id text NOT NULL,
    txn_id text NOT NULL,
    amount_delta_cents integer NOT NULL,
    date_delta_days integer NOT NULL,
    category text NOT NULL,
    violates_policy boolean NOT NULL
);


--
-- Data for Name: reconciliation; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.reconciliation (receipt_id, txn_id, amount_delta_cents, date_delta_days, category, violates_policy) FROM stdin;
RCP-0001	TXN-1001	0	0	travel	f
RCP-0002	TXN-1002	25	1	meals	f
RCP-0003	TXN-1003	0	0	software	f
RCP-0004	TXN-1004	0	1	other	f
RCP-0005	TXN-1005	0	0	meals	t
RCP-0006	TXN-1006	-30	1	hardware	f
RCP-0007	TXN-1008	0	1	travel	f
RCP-0008	TXN-1009	-40	1	meals	t
RCP-0009	TXN-1010	0	1	software	f
RCP-0010	TXN-1012	-35	1	other	f
RCP-0011	TXN-1007	45	3	meals	t
\.


--
-- PostgreSQL database dump complete
--

