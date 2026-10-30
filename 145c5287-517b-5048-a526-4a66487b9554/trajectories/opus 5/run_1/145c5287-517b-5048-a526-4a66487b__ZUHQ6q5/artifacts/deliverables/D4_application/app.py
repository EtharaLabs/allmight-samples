"""Ledgerline reconciliation review screens.

Serves the review screen at / and one detail screen per matched pair at
/pair/<receipt_id>.  The reconciliation is computed in-process from
/app/inputs at import time, so the screens need neither the database nor
anything under /app/out.

Every byte a screen needs is served by this application: the stylesheet is
inlined, there is no script, and no font is referenced by URL.  Text is set in
Liberation Sans, which the image ships in both the regular (400) and bold
(700) faces the screens use.
"""

import html

from starlette.applications import Starlette
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

import recon

RESULT = recon.reconcile()
BY_RECEIPT = {row["receipt_id"]: row for row in RESULT["matched"]}

STYLE = """
html { font-size: 16px; }
body {
  margin: 0;
  padding: 24px;
  background: #ffffff;
  color: #10151c;
  font-family: "Liberation Sans", Arial, Helvetica, sans-serif;
  font-size: 16px;
  font-weight: 400;
  line-height: 24px;
}
h1 { font-size: 24px; font-weight: 700; line-height: 32px; margin: 0 0 4px; }
h2 { font-size: 18px; font-weight: 700; line-height: 26px; margin: 0 0 6px; }
h2.spaced { margin-top: 24px; }
p.lede { font-size: 16px; font-weight: 400; margin: 0 0 20px; color: #4a5560; }
/* The table takes the main column and the exceptions sit beside it, so the
   whole reconciliation fits a 1280 by 800 viewport without scrolling. */
.layout { display: flex; align-items: flex-start; gap: 20px; }
.layout .main { flex: 0 0 940px; }
.layout .side { flex: 0 0 220px; }
.totals { list-style: none; margin: 0 0 24px; padding: 0; }
.totals li { font-size: 16px; font-weight: 400; padding: 2px 0; }
.totals .label { font-size: 16px; font-weight: 400; color: #4a5560; }
.totals .count { font-size: 18px; font-weight: 700; }
table {
  table-layout: fixed;
  width: 940px;
  border-collapse: collapse;
  font-size: 16px;
  font-weight: 400;
}
col.c-receipt { width: 150px; }
col.c-txn { width: 150px; }
col.c-amount { width: 150px; }
col.c-date { width: 140px; }
col.c-category { width: 140px; }
col.c-policy { width: 210px; }
th, td {
  box-sizing: border-box;
  margin: 0;
  padding: 6px 12px;
  border: 0;
  border-bottom: 1px solid #d8dde3;
  text-align: left;
  vertical-align: top;
  font-size: 16px;
  line-height: 24px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
th {
  font-weight: 700;
  border-bottom: 2px solid #10151c;
  background: #f3f5f7;
  /* Headers wrap rather than clip; wrapping changes a cell's height, never
     the left edge that column alignment is measured against. */
  white-space: normal;
}
td { font-weight: 400; }
td.policy-ok { font-weight: 400; color: #30424f; }
td.policy-bad { font-weight: 700; color: #8f1d1d; }
ul.ids { margin: 0 0 24px; padding: 0 0 0 22px; }
ul.ids li { font-size: 16px; font-weight: 400; line-height: 24px; }
ul.ids li.empty { color: #4a5560; }
a { color: #14487a; }
dl.pair { margin: 0; }
dl.pair div.field { display: flex; gap: 12px; padding: 6px 0; border-bottom: 1px solid #d8dde3; }
dl.pair dt { width: 220px; flex: none; font-size: 16px; font-weight: 400; color: #4a5560; margin: 0; }
dl.pair dd { margin: 0; font-size: 16px; font-weight: 400; }
dd#pair-policy-state.policy-ok { font-weight: 400; color: #30424f; }
dd#pair-policy-state.policy-bad { font-weight: 700; color: #8f1d1d; }
.back { display: inline-block; margin-top: 24px; font-size: 16px; font-weight: 400; }
"""

OK_TEXT = "within limit"
BAD_TEXT = "VIOLATION: over limit"


def page(title, body):
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>{title}</title>\n<style>{style}</style>\n</head>\n"
        "<body>\n{body}</body>\n</html>\n"
    ).format(title=html.escape(title), style=STYLE, body=body)


def esc(value):
    return html.escape(str(value), quote=True)


def policy_bits(row):
    if row["violates_policy"]:
        return "policy-bad", BAD_TEXT
    return "policy-ok", OK_TEXT


def review(request):
    totals = RESULT["totals"]
    rows = []
    for row in RESULT["matched"]:
        cls, text = policy_bits(row)
        rows.append(
            '<tr data-receipt-id="{rid}">'
            '<td class="cell-receipt"><a href="/pair/{rid}">{rid}</a></td>'
            '<td class="cell-txn">{txn}</td>'
            '<td class="cell-amount">{amount}</td>'
            '<td class="cell-date">{date}</td>'
            '<td class="cell-category">{category}</td>'
            '<td class="cell-policy {cls}">{text}</td>'
            "</tr>".format(
                rid=esc(row["receipt_id"]),
                txn=esc(row["txn_id"]),
                amount=esc(row["amount_delta_cents"]),
                date=esc(row["date_delta_days"]),
                category=esc(row["category"]),
                cls=cls,
                text=esc(text),
            )
        )

    def id_list(list_id, values, empty_label):
        if not values:
            items = '<li class="empty">{0}</li>'.format(esc(empty_label))
        else:
            items = "".join("<li>{0}</li>".format(esc(value)) for value in values)
        return '<ul class="ids" id="{0}">{1}</ul>'.format(list_id, items)

    body = """<h1>Ledgerline reconciliation review</h1>
<p class="lede">Bank statement reconciled against filed receipts. A pair whose
policy state reads VIOLATION, in bold type, exceeds the policy limit for its
category and needs a decision.</p>
<div class="layout">
<div class="main">
<h2>Matched pairs</h2>
<table id="matched-table">
<colgroup>
<col class="c-receipt"><col class="c-txn"><col class="c-amount">
<col class="c-date"><col class="c-category"><col class="c-policy">
</colgroup>
<thead><tr>
<th scope="col">Receipt</th><th scope="col">Transaction</th>
<th scope="col">Amount delta (cents)</th><th scope="col">Date delta (days)</th>
<th scope="col">Category</th><th scope="col">Policy state</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>
</div>
<div class="side">
<h2>Totals</h2>
<ul class="totals">
<li><span class="count" id="totals-matched">{matched}</span> <span class="label">matched pairs</span></li>
<li><span class="count" id="totals-unreceipted">{unreceipted}</span> <span class="label">unreceipted rows</span></li>
<li><span class="count" id="totals-unposted">{unposted}</span> <span class="label">unposted receipts</span></li>
<li><span class="count" id="totals-violations">{violations}</span> <span class="label">policy violations</span></li>
</ul>
<h2>Unreceipted rows</h2>
{unreceipted_list}
<h2>Unposted receipts</h2>
{unposted_list}
</div>
</div>
""".format(
        matched=totals["matched_count"],
        unreceipted=totals["unreceipted_count"],
        unposted=totals["unposted_count"],
        violations=totals["violation_count"],
        rows="".join(rows),
        unreceipted_list=id_list(
            "unreceipted-list", RESULT["unreceipted"], "no unreceipted rows"
        ),
        unposted_list=id_list("unposted-list", RESULT["unposted"], "no unposted receipts"),
    )
    return HTMLResponse(page("Reconciliation review", body))


def pair(request):
    receipt_id = request.path_params["receipt_id"]
    row = BY_RECEIPT.get(receipt_id)
    if row is None:
        body = (
            "<h1>No such matched pair</h1>"
            '<p class="lede">{0} is not a matched pair in this reconciliation.</p>'
            '<a class="back" href="/">Back to the review screen</a>'.format(esc(receipt_id))
        )
        return HTMLResponse(page("No such pair", body), status_code=404)

    cls, text = policy_bits(row)
    body = """<h1>Matched pair {rid}</h1>
<p class="lede">One receipt reconciled against one statement row.</p>
<dl class="pair">
<div class="field"><dt>Receipt id</dt><dd id="pair-receipt-id">{rid}</dd></div>
<div class="field"><dt>Transaction id</dt><dd id="pair-txn-id">{txn}</dd></div>
<div class="field"><dt>Amount delta (cents)</dt><dd id="pair-amount-delta">{amount}</dd></div>
<div class="field"><dt>Date delta (days)</dt><dd id="pair-date-delta">{date}</dd></div>
<div class="field"><dt>Category</dt><dd id="pair-category">{category}</dd></div>
<div class="field"><dt>Policy state</dt><dd id="pair-policy-state" class="{cls}">{text}</dd></div>
</dl>
<a class="back" href="/">Back to the review screen</a>
""".format(
        rid=esc(row["receipt_id"]),
        txn=esc(row["txn_id"]),
        amount=esc(row["amount_delta_cents"]),
        date=esc(row["date_delta_days"]),
        category=esc(row["category"]),
        cls=cls,
        text=esc(text),
    )
    return HTMLResponse(page("Pair {0}".format(row["receipt_id"]), body))


def favicon(request):
    return Response(status_code=204)


app = Starlette(
    routes=[
        Route("/", review),
        Route("/favicon.ico", favicon),
        Route("/pair/{receipt_id}", pair),
    ]
)
