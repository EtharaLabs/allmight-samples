"""Planted defects used to validate the workflows: name -> (lever, file under solution/app, oracle text, defect text, workflow that must fail).

Run ``python3 mutants.py check`` to confirm every entry is still applicable
against the current bundle, ``list`` to print the catalogue, and
``apply <name> --into <dir>`` to materialise one mutated copy of solution/app
for a trial.
"""
import argparse
import ast
import re
import shutil
import sys
from pathlib import Path

MUTANTS = {
    "grid-from-window-start": ("rule engine", "harborview/avail.py",
        "s = datetime.fromtimestamp(-(-epoch // 900) * 900, UTC)",
        "s = a  # BUG: slots step from the window start, not the 15-minute clock grid",
        "W03"),
    "room-highest-id": ("rule engine (rooms)", "harborview/avail.py",
        'rooms = [r["id"] for r in conn.execute("SELECT id FROM rooms WHERE clinic_id = %s AND room_type = %s AND active ORDER BY id",',
        'rooms = [r["id"] for r in conn.execute("SELECT id FROM rooms WHERE clinic_id = %s AND room_type = %s AND active ORDER BY id DESC",',
        "W05"),
    "legacy-fold-ignored": ("temporal (fold)", "harborview/avail.py",
        "    return local_start.replace(tzinfo=tz, fold=fold).astimezone(UTC)",
        "    return local_start.replace(tzinfo=tz, fold=1).astimezone(UTC)  # BUG: what SQL AT TIME ZONE does",
        "W07"),
    "offer-keeps-offer-expiry": ("temporal (TTL)", "harborview/service.py",
        """        conn.execute("UPDATE hv_appt SET expires_at = %s WHERE appointment_id = %s",
                     (now_utc() + timedelta(seconds=db.HOLD_TTL), a["id"]))""",
        """        pass  # BUG: the accepted offer keeps the offer's expiry instead of a fresh hold""",
        "W15"),
    "late-payment-confirms": ("payments (late webhook)", "harborview/service.py",
        """    if raw == "held" and a["expires_at"] > now_utc():""",
        """    if raw in ("held", "expired"):  # BUG: a payment for an expired hold still confirms""",
        "W09"),
    "refund-new-key-per-attempt": ("payments (idempotency)", "harborview/service.py",
        """            provider.create_refund(r["payment_intent"], r["refund_cents"], r["appointment_id"], r["refund_key"])""",
        """            provider.create_refund(r["payment_intent"], r["refund_cents"], r["appointment_id"], r["refund_key"] + secrets.token_hex(4))""",
        "W13"),
    "deposit-rounds-down": ("money", "harborview/service.py",
        """        deposit = half_up(fee["fee_cents"] * fee["deposit_bp"], 10000)
""",
        """        deposit = fee["fee_cents"] * fee["deposit_bp"] // 10000  # BUG: truncates half cents
""",
        "W08"),
    "notes-visible-to-desk": ("security (IDOR)", "harborview/web.py",
        """        if not (treating or self.user["role"] == "admin"):""",
        """        if not (treating or self.user["role"] in ("admin", "front_desk")):  # BUG""",
        "W16"),
    "open-redirect": ("security (redirect)", "harborview/web.py",
        """        return redirect(nxt if safe_next(nxt) else "/", self._start_session(u["id"]))""",
        """        return redirect(nxt if nxt.startswith("/") else "/", self._start_session(u["id"]))  # BUG: //host passes""",
        "W02"),
    "closures-ignored": ("rule engine (closures)", "harborview/avail.py",
        """    if conn.execute("SELECT 1 FROM clinic_closures WHERE clinic_id = %s AND on_date = %s", (c["clinic_id"], day)).fetchone():
        return []""",
        """    pass  # BUG: closures ignored""",
        "W19"),
    "reschedule-blocks-itself": ("lifecycle (reschedule)", "harborview/service.py",
        """        room = _slot_room(conn, clinician_id, a["code"], start, ignore=aid)""",
        """        room = _slot_room(conn, clinician_id, a["code"], start)  # BUG: the original blocks its own move""",
        "W14"),
    "export-formula-live": ("security (CSV injection)", "harborview/service.py",
        """    return "'" + v if v and v[0] in "=+-@\\t\\r" else v""",
        """    return v  # BUG: no spreadsheet safety""",
        "W20"),
    "deposit-half-even": ("money (ties)", "harborview/service.py",
        """        deposit = half_up(fee["fee_cents"] * fee["deposit_bp"], 10000)
""",
        """        deposit = round(fee["fee_cents"] * fee["deposit_bp"] / 10000)  # BUG: Python rounds half to even
""",
        "W08"),
    "desk-sees-every-clinic": ("security (tenant authorization)", "harborview/web.py",
        """            return a["clinic_id"] in self.staff_clinics()""",
        """            return True  # BUG: any front desk sees any clinic's appointments""",
        "W23"),
    "book-slot-for-everyone": ("UI contract", "harborview/web.py",
        """            if self.user and self.user["role"] == "patient":
                items.append(""",
        """            if True:  # BUG: book-slot buttons for visitors and staff too
                items.append(""",
        "W05"),
    "reset-link-in-outbox": ("security (secrets at rest)", "harborview/web.py",
        """            S.send_now(u["email"], u["full_name"], "Set your Harborview Physio password",""",
        """            (lambda to, name, subj, body: (S.enqueue(self.conn, "reset:" + sha(token), to, subj, body, name), self.conn.commit(), S.WAKE.set()))(u["email"], u["full_name"], "Set your Harborview Physio password",  # BUG: plain link stored in the outbox""",
        "W25"),
    "reset-keeps-sessions": ("security (session survival)", "harborview/web.py",
        """        self.conn.execute("UPDATE hv_sessions SET revoked = true WHERE user_id = %s", (row["user_id"],))""",
        """        pass  # BUG: existing sessions survive a password reset""",
        "W25"),
    "offer-deposit-at-offer-time": ("money/lifecycle", "harborview/service.py",
        """        deposit = half_up(fee["fee_cents"] * fee["deposit_bp"], 10000) if fee else a["deposit_cents"]""",
        """        deposit = a["deposit_cents"]  # BUG: the deposit stays the one computed when the offer was made""",
        "W15"),
    "offer-email-in-outbox": ("security (offer token at rest)", "harborview/service.py",
        """        _pending().append((a["patient_email"], a["patient_name"], f"A slot opened up: {a['type_name']} on {when_text(a)}",""",
        """        (lambda t: enqueue(conn, f"offer:{e['id']}", t[0], t[2], t[3], t[1]))((a["patient_email"], a["patient_name"], f"A slot opened up: {a['type_name']} on {when_text(a)}",  # BUG: the offer email, link included, is stored in the outbox""",
        "W25"),
}

VALIDATION_DIR = Path(__file__).resolve().parent
SOLUTION_DIR = VALIDATION_DIR.parent
APP_DIR = SOLUTION_DIR / "app"
TASK_DIR = SOLUTION_DIR.parent
WORKFLOWS_TOML = TASK_DIR / "tests" / "workflows.toml"


def declared_workflows():
    return set(re.findall(r'"(W\d+)"', WORKFLOWS_TOML.read_text(encoding="utf-8")))


def patched_source(source, oracle, defect):
    return source.replace(oracle, defect, 1)


def syntax_error(source):
    try:
        ast.parse(source)
    except SyntaxError as exc:
        return "{} at line {}".format(exc.msg, exc.lineno)
    return ""


def check():
    workflows = declared_workflows()
    problems = []
    for name, (_lever, relative, oracle, defect, workflow) in sorted(MUTANTS.items()):
        target = APP_DIR / relative
        if not target.is_file():
            problems.append("{}: app/{} is not in the bundle".format(name, relative))
            continue
        source = target.read_text(encoding="utf-8")
        found = source.count(oracle)
        if found != 1:
            problems.append(
                "{}: replaced text occurs {} times in app/{}, expected exactly 1".format(
                    name, found, relative
                )
            )
            continue
        if workflow not in workflows:
            problems.append(
                "{}: {} is not declared in tests/workflows.toml".format(name, workflow)
            )
        detail = syntax_error(patched_source(source, oracle, defect))
        if detail:
            problems.append(
                "{}: patched app/{} does not parse: {}".format(name, relative, detail)
            )
    return problems


def apply_one(name, destination):
    _lever, relative, oracle, defect, _workflow = MUTANTS[name]
    if destination.exists():
        raise SystemExit("{} already exists; choose an empty path".format(destination))
    shutil.copytree(APP_DIR, destination)
    target = destination / relative
    source = target.read_text(encoding="utf-8")
    if source.count(oracle) != 1:
        raise SystemExit(
            "{}: replaced text is no longer unique in app/{}".format(name, relative)
        )
    target.write_text(patched_source(source, oracle, defect), encoding="utf-8")
    return target


def main():
    parser = argparse.ArgumentParser(
        description="Planted-defect catalogue for harborview-physio."
    )
    sub = parser.add_subparsers(dest="command")
    sub.required = True
    sub.add_parser("list", help="print the catalogue")
    sub.add_parser("check", help="verify every entry is still applicable")
    applier = sub.add_parser("apply", help="write one mutated copy of solution/app")
    applier.add_argument("name", choices=sorted(MUTANTS))
    applier.add_argument("--into", required=True, type=Path)
    args = parser.parse_args()

    if args.command == "list":
        width = max(len(name) for name in MUTANTS)
        for name, (lever, relative, _o, _d, workflow) in sorted(MUTANTS.items()):
            print(
                "{:<{w}}  {:<24}  {:<34}  {}".format(
                    name, relative, lever, workflow, w=width
                )
            )
        print("\n{} defects".format(len(MUTANTS)))
        return 0

    if args.command == "check":
        problems = check()
        for problem in problems:
            print("FAIL  {}".format(problem))
        print(
            "\n{} of {} entries applicable".format(
                len(MUTANTS) - len(problems), len(MUTANTS)
            )
        )
        return 1 if problems else 0

    print("wrote {}".format(apply_one(args.name, args.into)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
