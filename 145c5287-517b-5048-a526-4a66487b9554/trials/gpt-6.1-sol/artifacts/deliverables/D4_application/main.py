import csv
import html
import io
import json
import os
import subprocess
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from starlette.applications import Starlette
from starlette.responses import HTMLResponse
from starlette.routing import Route

ROOT = Path(__file__).resolve().parent
DB = 'postgresql://ledgerline:ledgerline@localhost:5432/ledgerline'
FIELDS = {'RECEIPT-ID', 'DATE', 'VENDOR', 'TOTAL', 'CATEGORY'}


def read_inputs():
    policy = json.loads((ROOT / 'inputs/policy.json').read_text())
    with (ROOT / 'inputs/statement.csv').open(newline='') as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        row['amount_cents'] = int(row['amount_cents'])
        row['date'] = date.fromisoformat(row['posted_date'])
    receipts = []
    for path in sorted((ROOT / 'inputs/receipts').glob('*.txt')):
        receipt = {'raw': path.read_text()}
        for line in receipt['raw'].splitlines():
            key, sep, value = line.partition(':')
            if sep and key.strip() in FIELDS:
                receipt[key.strip()] = value.strip()
        receipt['cents'] = int(Decimal(receipt['TOTAL']) * 100)
        receipt['date'] = date.fromisoformat(receipt['DATE'])
        receipts.append(receipt)
    return policy, rows, sorted(receipts, key=lambda r: r['RECEIPT-ID'])


def eligible(policy, rows, receipts, signed=False):
    return [(i, j) for i, t in enumerate(rows) for j, r in enumerate(receipts)
            if abs(r['cents'] - (t['amount_cents'] if signed else abs(t['amount_cents']))) <= policy['amount_tolerance_cents']
            and abs((r['date'] - t['date']).days) <= policy['match_window_days']]


def matching(rows, receipts, edges):
    # First maximise cardinality, then prefer edges in the specified priority order.
    ranked = sorted(edges, key=lambda e: (
        abs(receipts[e[1]]['cents'] - abs(rows[e[0]]['amount_cents'])),
        abs((receipts[e[1]]['date'] - rows[e[0]]['date']).days),
        receipts[e[1]]['RECEIPT-ID'], rows[e[0]]['txn_id']))
    weights = {edge: 1 << (len(ranked) - rank) for rank, edge in enumerate(ranked)}
    by_row = {i: [j for ii, j in edges if ii == i] for i in range(len(rows))}

    @lru_cache(None)
    def solve(i, used):
        if i == len(rows):
            return 0, 0, ()
        best = solve(i + 1, used)
        for j in by_row[i]:
            if not used & (1 << j):
                count, score, pairs = solve(i + 1, used | (1 << j))
                option = (count + 1, score + weights[i, j], ((i, j),) + pairs)
                if option[:2] > best[:2]:
                    best = option
        return best
    return solve(0, 0)[2]


def write_notes(out, policy, rows, receipts, edges, pairs):
    assigned = dict(pairs)
    lookup = {r['RECEIPT-ID']: r for r in receipts}
    contested = []
    for txn in ('TXN-1007', 'TXN-1009', 'TXN-1012'):
        i = next(i for i, t in enumerate(rows) if t['txn_id'] == txn)
        j = assigned[i]
        r = receipts[j]
        t = rows[i]
        intro = (f"## {txn}\n\nThis row is matched to {r['RECEIPT-ID']}. Its receipt total is {r['cents']} cents, "
                 f"compared with the row's absolute amount of {abs(t['amount_cents'])} cents; the absolute amount difference is "
                 f"{abs(r['cents'] - abs(t['amount_cents']))} cents and the absolute date difference is {abs((r['date'] - t['date']).days)} day(s). ")
        if txn == 'TXN-1007':
            explanation = ('RCP-0011 is its only eligible receipt, and that receipt is also eligible for TXN-1009. '
                           'RCP-0008 is five days from TXN-1007, beyond the three-day window, so cannot replace it here. '
                           'Assigning RCP-0011 to TXN-1009 would leave TXN-1007 unmatched. Maximising cardinality therefore puts '
                           'RCP-0011 here and RCP-0008 at TXN-1009, even though RCP-0011 has a smaller amount difference at TXN-1009 (5 rather than 45 cents).')
        elif txn == 'TXN-1009':
            explanation = ('This row could instead match RCP-0011: its 9945-cent total differs by 5 cents and its March 15 date is one day before the March 16 posting. '
                           'RCP-0008 totals 9910 cents and is dated March 17. Both candidates pass the 50-cent and three-day limits. '
                           'The smaller amount difference would favour RCP-0011 if cardinality were equal, but RCP-0011 is the only candidate for TXN-1007. '
                           'RCP-0008 cannot match TXN-1007 because its date is five days away. The chosen assignment matches both rows; the alternative matches only one of them.')
        else:
            explanation = ('There is no other eligible receipt for this row, and RCP-0010 has no other eligible statement row. '
                           'The 1840-cent receipt dated March 22 is 35 cents below the 1875-cent magnitude posted March 21, within the 50-cent tolerance and three-day window. '
                           'Leaving this pair unmatched would reduce cardinality by one; no tie-break is needed.')
        contested.append(intro + explanation)
    (out / 'contested.md').write_text('\n\n'.join(contested) + '\n')

    violating = [receipts[j] for i, j in pairs if receipts[j]['CATEGORY'] in policy['category_limits_cents'] and receipts[j]['cents'] > policy['category_limits_cents'][receipts[j]['CATEGORY']]]
    compliant = next(receipts[j] for i, j in pairs if receipts[j]['CATEGORY'] in policy['category_limits_cents'] and receipts[j]['cents'] <= policy['category_limits_cents'][receipts[j]['CATEGORY']])
    sections = []
    for r in sorted(violating, key=lambda r: r['RECEIPT-ID']) + [compliant]:
        limit = policy['category_limits_cents'][r['CATEGORY']]
        difference = r['cents'] - limit
        verdict = (f"It violates policy because its total strictly exceeds that limit, by {difference} cents." if difference > 0 else
                   f"It is compliant because its total does not strictly exceed that limit; it falls short by {-difference} cents.")
        sections.append(f"## {r['RECEIPT-ID']}\n\nThe matched receipt's category is {r['CATEGORY']} and its TOTAL is {r['cents']} cents. The category_limits_cents limit for {r['CATEGORY']} is {limit} cents. {verdict}")
    (out / 'policy-notes.md').write_text('\n\n'.join(sections) + '\n')

    sections = []
    for r in receipts:
        notes = []
        if ':' in r['VENDOR']:
            wrong = r['VENDOR'].split(':')[0].strip()
            notes.append(f'This receipt exercises the rule that a VENDOR value can contain a colon and only the first colon separates the field name from its value. A reader splitting at every colon and keeping only the next segment would produce the wrong VENDOR "{wrong}". The correct VENDOR is "{r["VENDOR"]}"; the later colon is part of the value.')
        extra = [line for line in r['raw'].splitlines() if line.strip() and (':' not in line or line.partition(':')[0].strip() not in FIELDS)]
        if extra:
            notes.append('This receipt exercises the rule that additional non-field lines are ignored. A reader treating every line as a field would invent spurious fields named ' + ' and '.join('"' + s + '"' for s in extra) + f'. Both lines are ignored. The correct CATEGORY remains "{r["CATEGORY"]}" and the correct VENDOR remains "{r["VENDOR"]}"; neither extra line supplies a field value.')
        if notes:
            sections.append(f"## {r['RECEIPT-ID']}\n\n" + ' '.join(notes))
    (out / 'parse-notes.md').write_text('\n\n'.join(sections) + '\n')

    signed_edges = eligible(policy, rows, receipts, signed=True)
    signed_pairs = matching(rows, receipts, signed_edges)
    different, same = [], []
    for i, t in enumerate(rows):
        a = {j for ii, j in edges if ii == i}
        b = {j for ii, j in signed_edges if ii == i}
        (different if a != b else same).append(t['txn_id'])
    surviving = '; '.join(f"{receipts[j]['RECEIPT-ID']} with {rows[i]['txn_id']}" for i, j in signed_edges)
    (out / 'convention-notes.md').write_text(
        f"## collapse\n\nThe absolute-value rule admits {len(edges)} eligible pairs; comparison with the signed amount admits {len(signed_edges)}. "
        f"The maximum matching contains {len(pairs)} pairs under the absolute-value rule and {len(signed_pairs)} under the signed rule. "
        f"The surviving eligible pairs under signed comparison are: {surviving}. These are also all pairs in its maximum matching.\n\n"
        f"## divergence\n\nThe rows whose eligible receipt sets differ are {', '.join(different)}. The rows whose sets are the same are {', '.join(same)}. "
        "TXN-1011 has an empty eligible set under both conventions; TXN-1007 has RCP-0011 under both. "
        "For a differing row, TXN-1001 has amount_cents -48200 and RCP-0001 totals 48200 cents, on the same date. "
        f"The absolute-value difference is |48200 - |-48200|| = 0 cents, within the {policy['amount_tolerance_cents']}-cent tolerance. "
        f"The signed-comparison difference is |48200 - (-48200)| = 96400 cents, above {policy['amount_tolerance_cents']} cents. "
        "For a coinciding row, TXN-1007 has amount_cents +9900 and RCP-0011 totals 9945 cents, three days later. "
        f"The absolute-value difference is |9945 - |9900|| = 45 cents, and the signed-comparison difference is |9945 - 9900| = 45 cents; both are within {policy['amount_tolerance_cents']} cents.\n")


def build():
    policy, rows, receipts = read_inputs()
    edges = eligible(policy, rows, receipts)
    pairs = matching(rows, receipts, edges)
    matched = []
    for i, j in pairs:
        t, r = rows[i], receipts[j]
        limit = policy['category_limits_cents'].get(r['CATEGORY'])
        matched.append(dict(receipt_id=r['RECEIPT-ID'], txn_id=t['txn_id'],
                            amount_delta_cents=r['cents'] - abs(t['amount_cents']),
                            date_delta_days=(r['date'] - t['date']).days, category=r['CATEGORY'],
                            violates_policy=limit is not None and r['cents'] > limit))
    matched.sort(key=lambda p: p['receipt_id'])
    unreceipted = sorted(t['txn_id'] for i, t in enumerate(rows) if i not in dict(pairs))
    used = {j for i, j in pairs}
    unposted = sorted(r['RECEIPT-ID'] for j, r in enumerate(receipts) if j not in used)
    totals = dict(matched_count=len(matched), unreceipted_count=len(unreceipted), unposted_count=len(unposted), violation_count=sum(p['violates_policy'] for p in matched))
    data = dict(matched=matched, unreceipted=unreceipted, unposted=unposted, totals=totals)
    out = ROOT / 'out'
    out.mkdir(exist_ok=True)
    (out / 'reconciliation.json').write_text(json.dumps(data, indent=2) + '\n')
    (out / 'summary.txt').write_text('\n'.join(f'{name}: {totals[key]}' for name, key in [('matched','matched_count'), ('unreceipted','unreceipted_count'), ('unposted','unposted_count'), ('violations','violation_count')]) + '\n')
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator='\n')
    for p in matched:
        writer.writerow([*list(p.values())[:-1], 't' if p['violates_policy'] else 'f'])
    sql = '''BEGIN;
DROP TABLE IF EXISTS reconciliation;
CREATE TABLE reconciliation (
receipt_id text NOT NULL,
txn_id text NOT NULL,
amount_delta_cents integer NOT NULL,
date_delta_days integer NOT NULL,
category text NOT NULL,
violates_policy boolean NOT NULL
);
COPY reconciliation FROM STDIN WITH (FORMAT csv);
''' + buf.getvalue() + '\\.\nCOMMIT;\n'
    subprocess.run(['psql', DB, '-X', '-v', 'ON_ERROR_STOP=1'], input=sql, text=True, check=True, capture_output=True)
    with (out / 'snapshot.sql').open('w') as f:
        subprocess.run(['pg_dump', DB, '--no-owner', '--no-privileges', '--table=public.reconciliation'], stdout=f, check=True)
    write_notes(out, policy, rows, receipts, edges, pairs)
    return data


STYLE = '''
@font-face{font-family:LedgerSans;src:local("Liberation Sans");font-weight:400}
@font-face{font-family:LedgerSans;src:local("Liberation Sans Bold");font-weight:700}
*{box-sizing:border-box}body{margin:0;background:#f4f5f2;color:#1f302e;font:14px LedgerSans,"Liberation Sans",sans-serif}
header{height:60px;background:#163d36;color:#fff;padding:0 48px;display:flex;align-items:center;justify-content:space-between}
.brand{font-size:23px;letter-spacing:-.7px;font-weight:700}.eyebrow{text-transform:uppercase;font-size:11px;letter-spacing:1.5px;color:#677b74}
header .eyebrow{color:#bfcec9}main{max-width:1184px;margin:24px auto}h1{font-size:28px;letter-spacing:-.8px;margin:6px 0 8px}p{line-height:1.5}.intro{color:#61716b;margin:0 0 16px}
.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:18px}.metric{padding:12px 20px;background:#fff;border:1px solid #dce3dd;border-radius:8px}.number{font-size:26px;margin-top:6px}.metric:last-child{background:#fff6ec}
.panel{background:#fff;border:1px solid #dce3dd;border-radius:8px;overflow:hidden}.panel-title{padding:12px 20px;border-bottom:1px solid #e1e6e1;display:flex;justify-content:space-between;align-items:center}h2{font-size:16px;margin:0}.hint{font-size:12px;color:#61716b}
table{width:100%;border-collapse:collapse;table-layout:fixed}th,td{text-align:left;padding:7px 20px;border-bottom:1px solid #edf0ed;white-space:nowrap}th{font-size:11px;text-transform:uppercase;letter-spacing:.6px;background:#fafbf9;font-weight:400;color:#64756d}tbody tr:last-child td{border-bottom:0}tbody tr:hover{background:#f7faf7}a{color:#215f50;text-underline-offset:3px}.state{font-weight:400}.state.violation{font-weight:700;color:#954011}.state.compliant{font-weight:400;color:#316c55}.lower{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:14px}.lower .panel{padding:12px 20px}ul{list-style:none;margin:12px 0 0;padding:0}li{padding:6px 0;color:#52675d}.detail{margin-top:22px;padding:24px}dl{display:grid;grid-template-columns:220px 1fr;gap:22px;margin:0}dt{color:#677b74}dd{margin:0;font-size:18px}.back{display:inline-block;margin-bottom:22px}
'''


def escape(value):
    return html.escape(str(value), quote=True)


def page(title, body):
    return HTMLResponse(f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)} · Ledgerline</title><style>{STYLE}</style></head><body><header><div class="brand">Ledgerline<span style="color:#9ab7a8"> /</span></div><div class="eyebrow">Expense reconciliation</div></header><main>{body}</main></body></html>')


def values(p):
    return [p['receipt_id'], p['txn_id'], p['amount_delta_cents'], p['date_delta_days'], p['category'], 'violation' if p['violates_policy'] else 'compliant']


async def review(request):
    data = request.app.state.data
    metrics = ''.join(f'<div class="metric"><div class="eyebrow">{label}</div><div class="number" id="totals-{ident}">{data["totals"][key]}</div></div>' for label, ident, key in [('Matched pairs','matched','matched_count'),('Unreceipted','unreceipted','unreceipted_count'),('Unposted','unposted','unposted_count'),('Policy violations','violations','violation_count')])
    rows = ''
    for p in data['matched']:
        v = values(p)
        cells = f'<td><a href="/pair/{quote(p["receipt_id"], safe="")}">{escape(v[0])}</a></td>'
        cells += ''.join(f'<td>{escape(x)}</td>' for x in v[1:5])
        cells += f'<td class="state {v[5]}">{v[5]}</td>'
        rows += f'<tr data-receipt-id="{escape(p["receipt_id"])}">{cells}</tr>'
    lower = ''.join(f'<section class="panel"><h2>{label}</h2><ul id="{key}-list">' + ''.join(f'<li>{escape(x)}</li>' for x in data[key]) + '</ul></section>' for key, label in [('unreceipted','Statement rows without receipts'),('unposted','Receipts without statement rows')])
    return page('Review', f'<div class="eyebrow">Reconciliation review</div><h1>Every expense, accounted for.</h1><p class="intro">Review matched records, policy exceptions, and items awaiting a counterpart.</p><div class="metrics">{metrics}</div><section class="panel"><div class="panel-title"><h2>Matched records</h2><span class="hint">Select a receipt to inspect the pair · deltas are signed</span></div><table id="matched-table"><thead><tr>' + ''.join(f'<th scope="col">{s}</th>' for s in ['Receipt','Transaction','Amount Δ (cents)','Date Δ (days)','Category','Policy state']) + f'</tr></thead><tbody>{rows}</tbody></table></section><div class="lower">{lower}</div>')


async def detail(request):
    p = next((p for p in request.app.state.data['matched'] if p['receipt_id'] == request.path_params['receipt_id']), None)
    if p is None:
        return HTMLResponse('Matched pair not found', status_code=404)
    v = values(p)
    ids = ['receipt-id','txn-id','amount-delta','date-delta','category','policy-state']
    labels = ['Receipt ID','Transaction ID','Amount delta (cents)','Date delta (days)','Category','Policy state']
    fields = ''.join(f'<dt>{label}</dt><dd id="pair-{ident}"' + (f' class="state {v[5]}"' if ident == 'policy-state' else '') + f'>{escape(value)}</dd>' for ident, label, value in zip(ids, labels, v))
    return page(p['receipt_id'], f'<a class="back" href="/">← Back to reconciliation</a><div class="eyebrow">Matched pair</div><h1>{escape(p["receipt_id"])}</h1><p class="intro">Receipt and statement comparison</p><section class="panel detail"><dl>{fields}</dl></section>')


app = Starlette(routes=[Route('/', review), Route('/pair/{receipt_id:path}', detail)])

if __name__ == '__main__':
    import uvicorn
    app.state.data = build()
    uvicorn.run(app, host='127.0.0.1', port=8080)
