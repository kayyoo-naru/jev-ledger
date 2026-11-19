#!/usr/bin/env python3
"""
Jev Ledger: personal financial statements, classified by TypeSafe AI's Jev and
checked the way an auditor would check them.

Jev makes the judgment calls: which statement line a transaction belongs on, and
whether money is just moving between your own accounts. Everything else is
plain code: reading statements, dates, FX, sums, matching transfers, and the
controls that decide when Jev's answer is trusted and when you review it.

Inputs (mix and match):
  * Chase checking/savings PDF statements         (needs: pip install pdfplumber)
  * A folder with accounts.csv + transactions.csv (see sample/)

Usage:
  export TYPESAFE_API_KEY=...                       # console.typesafe.ai
  python3 jev_ledger.py sample/ --rules sample/rules.csv
  python3 jev_ledger.py statements/*.pdf --base USD --rules my_rules.csv
  python3 jev_ledger.py sample/ --reviews jev_output/review_queue.csv   # after filling in your_line
  python3 jev_ledger.py sample/ --mock               # no API calls; fake answers to test the pipeline

Outputs go to ./jev_output/: report.html, audit_trail.csv, review_queue.csv, suggested_rules.csv
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
API_URL = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai").rstrip("/") + "/v1/systemone"
MODEL = os.environ.get("JEV_MODEL", "jev-latest")
CACHE_FILE = ".jev_cache.json"

# Fixed FX so every statement ties exactly. Edit to taste.
FX_TO_CAD = {"CAD": 1.0, "USD": 1.38}

# Materiality: bigger amounts need more certainty (amounts in base currency).
# None = always reviewed by you, whatever Jev's confidence.
MATERIALITY = [(100, 0.75), (1000, 0.90), (math.inf, None)]

TRANSFER = "Transfer between own accounts"
EXCLUDED = "Excluded from this analysis"
INCOME_LINES = {
    "Salary & wages": "Pay from an employer",
    "Contract & freelance income": "Payments from clients or organizations for contract or freelance work",
    "Interest & dividends": "Bank interest, investment dividends or distributions",
    "Other income": "Refunds, money from friends paying back a shared cost, gifts received, anything else coming in",
}
EXPENSE_LINES = {
    "Housing": "Rent or mortgage",
    "Utilities & phone": "Electricity, water, internet, mobile phone",
    "Groceries": "Supermarkets and food bought to cook at home",
    "Dining & coffee": "Restaurants, cafes, food delivery",
    "Transport": "Transit, ride-hailing, taxis, fuel, parking",
    "Travel": "Flights, trains between cities, hotels, short-term rentals",
    "Subscriptions & software": "Streaming, software, apps, online memberships and publications",
    "Health & fitness": "Pharmacy, clinics, therapy, physio, gyms",
    "Shopping": "Online marketplaces, retail, books, beauty, household goods",
    "Gifts & giving": "Donations to charity and money or gifts given to other people",
    "Business & work costs": "Costs of running one's own business or freelance work: company formation, business software and AI tools, design services, business shipping",
    "Entertainment & hobbies": "Museums, events, lessons, hobbies and leisure activities",
    "Personal care & services": "Laundry, salons, storage units, wellness and other personal services",
    "Fees & interest": "Bank fees, wire fees, foreign transaction fees, card interest",
}
LINES = {
    **INCOME_LINES, **EXPENSE_LINES,
    TRANSFER: "Money moving between the person's own accounts: paying their own credit card, "
              "moving to or from their own savings, investment, or money-transfer account",
}
QUESTIONS = {
    "line": {
        "type": "choice",
        "instructions": "Which line of a personal income statement does this bank transaction belong on?",
        "criteria": LINES,
    },
    "own_transfer": {
        "type": "noul",
        "instructions": "Is this money moving between two accounts that belong to the same person, "
                        "such as paying their own credit card bill or moving money to their own savings, "
                        "investment, or money-transfer account?",
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def to_base(amount: float, cur: str, base: str) -> float:
    return amount * FX_TO_CAD[cur] / FX_TO_CAD[base]


def mask(text: str) -> str:
    """Hide long numbers (account, reference and phone numbers), keep the last 4 digits."""
    return re.sub(r"\d{6,}", lambda m: "••" + m.group(0)[-4:], text)


def merchant_key(desc: str) -> str:
    d = desc.upper()
    d = re.sub(r"\b\d{1,2}/\d{1,2}\b", " ", d)
    d = re.sub(r"[#*]\s*\w*\d\w*", " ", d)
    d = re.sub(r"\b(TRANSACTION#|WEB ID|PPD ID|TRN)\b.*$", " ", d)
    d = re.sub(r"\bUSD [\d.,]+", " ", d)
    d = re.sub(r"\b[A-Z]?\d[\w\-]*\b", " ", d)
    d = re.sub(r"\b(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\b", " ", d)
    return re.sub(r"\s+", " ", d).strip(" -:")


def txn_id(d: date, account: str, desc: str, amount: float, n: int) -> str:
    return hashlib.sha1(f"{d}|{account}|{desc}|{amount}|{n}".encode()).hexdigest()[:8]


def num(s: str) -> float:
    return float(s.replace("$", "").replace(",", ""))


# ---------------------------------------------------------------------------
# Readers. Every reader also returns the bank's own totals so we can prove the read.
# ---------------------------------------------------------------------------
def resolve_year(mmdd: str, start: date, end: date) -> date:
    """Statements print MM/DD only; pick the year that puts the date closest to the statement period."""
    mo, dy = map(int, mmdd.split("/"))
    mid = start + (end - start) / 2
    cands = []
    for y in (start.year - 1, start.year, end.year, end.year + 1):
        try:
            cands.append(date(y, mo, dy))
        except ValueError:
            pass
    return min(cands, key=lambda d: abs(d - mid))


def read_chase_pdf(path: str) -> dict:
    try:
        import pdfplumber
    except ImportError:
        sys.exit("Reading PDFs needs pdfplumber:  pip install pdfplumber")
    with pdfplumber.open(path) as pdf:
        text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    if "chase" not in text.lower():
        sys.exit(f"{path}: only Chase PDF statements are supported. Export a CSV instead.")
    if re.search(r"Previous Balance", text) and re.search(r"New Balance", text):
        return _chase_card(text, path)
    return _chase_bank(text, path)


def _chase_bank(text: str, path: str) -> dict:
    m = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4}) through ([A-Z][a-z]+ \d{1,2}, \d{4})", text)
    start, end = (datetime.strptime(x, "%B %d, %Y").date() for x in m.groups())
    acct_no = re.search(r"Account Number:\s*(\d+)", text).group(1)
    prod = re.search(r"SUMMARY\s+(Chase [A-Za-z ]+?)\s*$", text, re.M)
    product = prod.group(1).strip() if prod else "Chase account"

    summary = {}
    sm = re.search(r"\*start\*summary(.*?)\*end\*summary", text, re.S)
    for line in (sm.group(1) if sm else "").splitlines():
        mm = re.match(r"^\s*([A-Za-z][A-Za-z &,'\-]+?)\s+\$?(-?[\d,]+\.\d{2})\s*$", line)
        if mm:
            summary[mm.group(1).strip()] = num(mm.group(2))

    rows, pending_date, last_was_row = [], None, False
    row_re = re.compile(r"^(?:(\d{2}/\d{2})\s+)?(.+?)\s+(-?[\d,]+\.\d{2})\s+(-?[\d,]+\.\d{2})$")
    skip = re.compile(r"^(\*|TRANSACTION DETAIL|DATE DESCRIPTION|Page\s*\d|Account Number|.*\d{4} through |Beginning Balance|Ending Balance)")
    for block in re.findall(r"\*start\*transactiondetail(.*?)\*end\*transactiondetail", text, re.S):
        for raw in block.splitlines():
            line = raw.strip()
            if not line or skip.match(line):
                last_was_row = False
                continue
            mm = row_re.match(line)
            if mm:
                d = mm.group(1) or pending_date
                pending_date = None
                rows.append({"date": resolve_year(d, start, end), "description": re.sub(r"\s+", " ", mm.group(2)),
                             "amount": num(mm.group(3)), "balance_after": num(mm.group(4))})
                last_was_row = True
            elif re.fullmatch(r"\d{2}/\d{2}", line):
                pending_date, last_was_row = line, False
            elif last_was_row and rows:
                rows[-1]["description"] += " " + line
    pos = sum(r["amount"] for r in rows if r["amount"] > 0)
    neg = sum(r["amount"] for r in rows if r["amount"] < 0)
    checks = []
    if "Deposits and Additions" in summary:
        checks.append(("Money in", pos, summary["Deposits and Additions"]))
    outs = [v for k, v in summary.items() if v < 0 and "Balance" not in k]
    if outs:
        checks.append(("Money out", neg, sum(outs)))
    return {"account": f"{product} ••{acct_no[-4:]}", "type": "credit" if "Card" in product else "cash", "currency": "USD",
            "start": start, "end": end, "opening": summary.get("Beginning Balance"), "closing": summary.get("Ending Balance"),
            "checks": checks, "rows": rows, "source": os.path.basename(path)}


def _chase_card(text: str, path: str) -> dict:
    """Chase credit card. The statement shows charges as positive; we store them as money out (negative)
    and the balance owed as a negative number, so opening + transactions = closing for every account."""
    m = re.search(r"Opening/Closing Date\s+(\d{2}/\d{2}/\d{2})\s*-\s*(\d{2}/\d{2}/\d{2})", text)
    start, end = (datetime.strptime(x, "%m/%d/%y").date() for x in m.groups())
    last4 = re.search(r"Account Number:\s*[X ]+(\d{4})", text).group(1)

    def summ(label):
        mm = re.search(label + r"\s+([+-]?)\$?([\d,]+\.\d{2})", text)
        return (-1 if mm and mm.group(1) == "-" else 1) * num(mm.group(2)) if mm else 0.0
    prev, new = summ(r"Previous Balance"), summ(r"New Balance")
    bank = {"Payments and credits": summ(r"Payment, Credits"), "Purchases": summ(r"Purchases"),
            "Cash advances": summ(r"Cash Advances"), "Fees": summ(r"Fees Charged"), "Interest": summ(r"Interest Charged")}

    i = text.find("Transaction Merchant Name or Transaction Description")
    j = text.find("Totals Year-to-Date", i)
    body = text[i:j if j > 0 else None]
    section_of = [("PAYMENTS AND OTHER CREDITS", "Payments and credits"), ("PURCHASE", "Purchases"),
                  ("CASH ADVANCE", "Cash advances"), ("FEES CHARGED", "Fees"), ("INTEREST CHARGED", "Interest")]
    section, rows = "Purchases", []
    row_re = re.compile(r"^(\d{2}/\d{2})\s+(.+?)\s+(-?[\d,]*\d\.\d{2})$")
    for raw in body.splitlines():
        line = raw.strip()
        hit = next((sec for hdr, sec in section_of if line.upper().startswith(hdr) and not re.search(r"\d\.\d{2}$", line)), None)
        if hit:
            section = hit
            continue
        fx = re.fullmatch(r"\d{2}/\d{2}\s+([A-Z][A-Z ]+)", line)
        if fx and rows and "fx_currency" not in rows[-1]:
            rows[-1]["fx_currency"] = fx.group(1).strip()
            continue
        mm = row_re.match(line)
        if mm and "EXCHG RATE" not in line:
            stmt_amt = num(mm.group(3))
            rows.append({"date": resolve_year(mm.group(1), start, end), "description": re.sub(r"\s+", " ", mm.group(2)),
                         "amount": -stmt_amt, "section": section, "stmt_amount": stmt_amt})
    checks = []
    for sec, bank_total in bank.items():
        parsed = sum(r["stmt_amount"] for r in rows if r["section"] == sec)
        if abs(bank_total) > 0.004 or abs(parsed) > 0.004:
            checks.append((sec, parsed, bank_total))
    return {"account": f"Chase credit card ••{last4}", "type": "credit", "currency": "USD",
            "start": start, "end": end, "opening": -prev, "closing": -new,
            "checks": checks, "rows": rows, "source": os.path.basename(path)}


def read_csv_folder(folder: str) -> list[dict]:
    with open(os.path.join(folder, "accounts.csv"), newline="", encoding="utf-8-sig") as f:
        accts = list(csv.DictReader(f))
    with open(os.path.join(folder, "transactions.csv"), newline="", encoding="utf-8-sig") as f:
        txns = list(csv.DictReader(f))
    out = []
    for a in accts:
        rows = [{"date": datetime.strptime(t["date"], "%Y-%m-%d").date(), "description": t["description"].strip(),
                 "amount": num(t["amount"]), "my_label": (t.get("my_label") or "").strip() or None}
                for t in txns if t["account"] == a["account"]]
        out.append({
            "account": a["account"], "type": a["type"], "currency": a["currency"].upper(),
            "start": datetime.strptime(a["period_start"], "%Y-%m-%d").date() if a.get("period_start") else min((r["date"] for r in rows), default=None),
            "end": datetime.strptime(a["period_end"], "%Y-%m-%d").date() if a.get("period_end") else max((r["date"] for r in rows), default=None),
            "opening": num(a["opening_balance"]), "closing": num(a["closing_balance"]),
            "checks": [], "rows": rows, "source": os.path.basename(os.path.normpath(folder)),
        })
    return out


def load_inputs(paths: list[str]) -> list[dict]:
    stmts = []
    for p in paths:
        if os.path.isdir(p):
            stmts += read_csv_folder(p)
        elif p.lower().endswith(".pdf"):
            stmts.append(read_chase_pdf(p))
        elif os.path.basename(p) == "transactions.csv":
            stmts += read_csv_folder(os.path.dirname(p) or ".")
        else:
            sys.exit(f"Don't know how to read {p}")
    for s in stmts:
        if s["currency"] not in FX_TO_CAD:
            sys.exit(f"{s['account']}: no FX rate for {s['currency']}; add it to FX_TO_CAD.")
    return stmts


# ---------------------------------------------------------------------------
# Control 1 + 2: prove the read is complete before Jev sees anything
# ---------------------------------------------------------------------------
def check_reads(stmts: list[dict]) -> list[dict]:
    findings = []
    for s in stmts:
        rows, cur = s["rows"], s["currency"]
        tol = 0.005
        label = f"{s['account']} ({s['source']})"
        total = sum(r["amount"] for r in rows)
        ok = s["opening"] is not None and s["closing"] is not None and abs(s["opening"] + total - s["closing"]) <= tol
        findings.append({"ok": ok, "text": f"{label}: opening {s['opening']:,.2f} + {len(rows)} transactions "
                                             f"({total:,.2f}) = closing {s['closing']:,.2f} {cur}" if ok else
                         f"{label}: opening + transactions ≠ closing (off by {s['opening'] + total - s['closing']:,.2f} {cur})"})
        if any("balance_after" in r for r in rows):
            bal, breaks = s["opening"], 0
            for r in rows:
                bal = round(bal + r["amount"], 2)
                if abs(bal - r["balance_after"]) > tol:
                    breaks += 1
                    bal = r["balance_after"]
            findings.append({"ok": breaks == 0, "text": f"{label}: running balance matches the bank on all {len(rows)} lines"
                             if breaks == 0 else f"{label}: running balance breaks on {breaks} line(s); a row may be misread"})
        for name, parsed, bank in s.get("checks", []):
            ok3 = abs(parsed - bank) <= tol
            findings.append({"ok": ok3, "text": f"{label}: {name.lower()} {parsed:,.2f} matches the bank's summary"
                             if ok3 else f"{label}: {name.lower()} {parsed:,.2f} ≠ bank's summary {bank:,.2f}"})
    return findings


def merge_accounts(stmts: list[dict]) -> tuple[dict, list[dict]]:
    """Chain monthly statements per account; check nothing is missing between them."""
    by_acct = defaultdict(list)
    for s in stmts:
        by_acct[s["account"]].append(s)
    accounts, findings = {}, []
    for name, ss in by_acct.items():
        ss.sort(key=lambda s: s["start"] or date.min)
        for a, b in zip(ss, ss[1:]):
            gap = a["end"] and b["start"] and (b["start"] - a["end"]).days > 1
            ok = abs(a["closing"] - b["opening"]) < 0.01 and not gap
            findings.append({"ok": ok, "text": f"{name}: {a['source']} closing = {b['source']} opening, no gap in dates"
                             if ok else f"{name}: break between {a['source']} and {b['source']}. Is a statement missing?"})
        accounts[name] = {"name": name, "type": ss[0]["type"], "currency": ss[0]["currency"],
                          "opening": ss[0]["opening"], "closing": ss[-1]["closing"],
                          "start": ss[0]["start"], "end": ss[-1]["end"]}
    if len(stmts) == len(by_acct):
        findings.append({"ok": True, "text": "One statement per account, so there are no month-to-month links to check"})
    return accounts, findings


def flatten(stmts: list[dict], base: str) -> list[dict]:
    txns, seen = [], defaultdict(int)
    for s in stmts:
        for r in s["rows"]:
            k = (r["date"], s["account"], r["description"], r["amount"])
            seen[k] += 1
            txns.append({
                "id": txn_id(*k, seen[k]), "date": r["date"], "account": s["account"], "acct_type": s["type"],
                "description": r["description"], "shown": mask(r["description"]), "amount": r["amount"],
                "currency": s["currency"], "base": round(to_base(r["amount"], s["currency"], base), 2),
                "my_label": r.get("my_label"), "fx_currency": r.get("fx_currency"),
            })
    txns.sort(key=lambda t: (t["date"], t["account"]))
    return txns


# ---------------------------------------------------------------------------
# Jev (cached, retried, parallel) + a mock for offline testing
# ---------------------------------------------------------------------------
def jev_state(t: dict) -> dict:
    """Words only: no date, no arithmetic. Long numbers masked."""
    return {"bank_description": t["shown"],
            "direction": "money coming into this account" if t["amount"] > 0 else "money leaving this account",
            "amount": f"{abs(t['amount']):,.2f} {t['currency']}",
            "account": f"{t['account']} ({t['acct_type']} account)"}


def call_jev(payload: dict, key: str, retries: int = 4) -> tuple[dict, float]:
    req = urllib.request.Request(API_URL, data=json.dumps(payload, ensure_ascii=False).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    for i in range(retries):
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode()), (time.perf_counter() - t0) * 1000
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and i < retries - 1:
                time.sleep(1.5 * 2 ** i)
                continue
            sys.exit(f"Jev API error {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
        except urllib.error.URLError as e:
            if i < retries - 1:
                time.sleep(1.5 * 2 ** i)
                continue
            sys.exit(f"Could not reach {API_URL}: {e.reason}")


def mock_jev(payload: dict) -> tuple[dict, float]:
    d = payload["state"]["bank_description"].upper()
    rules = [
        (r"PAYMENT TO CHASE CARD|PAYMENT THANK YOU|AMERICAN EXPRESS ACH|TRANSFER FROM SAV|TRANSFER TO SAV|TRANSFER FROM CHK|TRANSFER TO CHK", TRANSFER, 0.93),
        (r"DINNER", TRANSFER, 0.58), (r"ZELLE PAYMENT TO", "Gifts & giving", 0.62),
        (r"WIRE FEE|TRANSACTION FEE|MONTHLY FEE|INTEREST CHARGE|MEMBERSHIP FEE", "Fees & interest", 0.9),
        (r"NETFLIX|SPOTIFY|CLAUDE|CHATGPT|MEDIUM|NYTIMES", "Subscriptions & software", 0.88),
        (r"REPLIT|GITHUB|FIGMA|WORKSPACE|UPS STORE", "Business & work costs", 0.72),
        (r"CLIENT ACH|INVOICE", "Contract & freelance income", 0.86),
        (r"PAYROLL", "Salary & wages", 0.95), (r"INTEREST PAYMENT|DIVIDEND", "Interest & dividends", 0.92),
        (r"RENT|PROPERTY MGMT", "Housing", 0.9), (r"COMCAST|VERIZON|PG&E|CON ED", "Utilities & phone", 0.9),
        (r"TRADER JOE|WHOLE FOODS|SAFEWAY", "Groceries", 0.94), (r"COFFEE|CAFE|RESTAURANT|SWEETGREEN|CHIPOTLE|DOORDASH", "Dining & coffee", 0.87),
        (r"UBER|LYFT|CLIPPER|MTA", "Transport", 0.9), (r"AIRBNB|DELTA|UNITED|MARRIOTT", "Travel", 0.85),
        (r"CVS|WALGREENS|EQUINOX|CLASSPASS|BETTERHELP", "Health & fitness", 0.84), (r"AMAZON|TARGET|APPLE STORE|IKEA", "Shopping", 0.79),
        (r"RED CROSS|UNICEF|DONATION", "Gifts & giving", 0.91), (r"MUSEUM|MOMA|TICKETMASTER", "Entertainment & hobbies", 0.8),
        (r"LAUNDROMAT|EXTRA SPACE|SALON", "Personal care & services", 0.7),
    ]
    line, conf = next(((l, c) for p, l, c in rules if re.search(p, d)), ("Shopping", 0.5))
    jitter = (int(hashlib.md5(d.encode()).hexdigest(), 16) % 9 - 4) / 100
    conf = max(0.3, min(0.99, conf + jitter))
    rest = (1 - conf) / (len(LINES) - 1)
    probs = {k: (conf if k == line else rest) for k in LINES}
    p_tr = 0.92 if line == TRANSFER and conf > 0.8 else 0.35 if line == TRANSFER else 0.05
    return {"model": "MOCK", "answers": {
        "line": {"type": "choice", "choice": line, "probabilities": probs, "confidence": conf},
        "own_transfer": {"type": "noul", "noul": p_tr}},
        "usage": {"input_tokens": 380, "output_tokens": 0}}, 0.0


def run_jev(txns: list[dict], key: str | None, mock: bool) -> dict:
    cache = {}
    if not mock and os.path.exists(CACHE_FILE):
        cache = json.load(open(CACHE_FILE, encoding="utf-8"))

    def one(t):
        payload = {"model": MODEL, "state": jev_state(t), "questions": QUESTIONS}
        if mock:
            return t["id"], *mock_jev(payload), False
        k = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if k in cache:
            return t["id"], cache[k]["body"], cache[k]["ms"], True
        body, ms = call_jev(payload, key)
        cache[k] = {"body": body, "ms": ms}
        return t["id"], body, ms, False

    out = {}
    if txns:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for n, (tid, body, ms, cached) in enumerate(ex.map(one, txns), 1):
                out[tid] = {"body": body, "ms": ms, "cached": cached}
                if sys.stdout.isatty():
                    print(f"\r  Jev decisions: {n}/{len(txns)}", end="", flush=True)
        print()
    if not mock:
        json.dump(cache, open(CACHE_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    return out


# ---------------------------------------------------------------------------
# Controls 3–5: transfer matching, rules/reviews, materiality-based thresholds
# ---------------------------------------------------------------------------
def match_transfers(txns: list[dict], window_days: int = 5) -> dict:
    """Pair an outflow in one loaded account with the equal inflow in another, within a few days."""
    pairs, open_in = {}, [t for t in txns if t["amount"] > 0]
    for t in (t for t in txns if t["amount"] < 0):
        cands = [u for u in open_in if u["id"] not in pairs and u["account"] != t["account"]
                 and u["currency"] == t["currency"] and abs(u["amount"] + t["amount"]) < 0.005
                 and abs((u["date"] - t["date"]).days) <= window_days]
        if cands:
            u = min(cands, key=lambda u: abs((u["date"] - t["date"]).days))
            pairs[t["id"]], pairs[u["id"]] = u["id"], t["id"]
    return pairs


def required_confidence(amount_base: float):
    for limit, need in MATERIALITY:
        if abs(amount_base) < limit:
            return need
    return None


def load_map(path: str | None, key: str, val: str) -> list[tuple[str, str]]:
    if not path:
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [(r[key].strip(), (r.get(val) or "").strip()) for r in csv.DictReader(f)]
    bad = [v for _, v in rows if v and v not in LINES]
    if bad:
        sys.exit(f"{path}: unknown line(s) {sorted(set(bad))}. Use one of: {', '.join(LINES)}")
    return [(k, v) for k, v in rows if k and v]


def rule_for(desc: str, rules):
    d, k = desc.upper(), merchant_key(desc)
    return next((line for pat, line in rules if pat.upper() in d or pat.upper() in k), None)


def decide(txns, jev, pairs, rules, reviews, ex_ids=frozenset()):
    reviews = dict(reviews)
    by_id = {t["id"]: t for t in txns}
    for t in txns:
        t.update(jev_line=None, confidence=None, runner_up=None, p_transfer=None, reasons=[], pair=pairs.get(t["id"]))
        if t["id"] in ex_ids:
            t["line"], t["by"] = EXCLUDED, "Excluded"
            continue
        if t["id"] in reviews:
            t["line"], t["by"] = reviews[t["id"]], "You"
            continue
        rule = rule_for(t["description"], rules)
        if rule:
            t["line"], t["by"] = rule, "Saved rule"
            continue
        a = jev[t["id"]]["body"].get("answers", {})
        ch = a.get("line", {})
        ranked = sorted((ch.get("probabilities") or {}).items(), key=lambda kv: -kv[1])
        t["jev_line"] = ch.get("choice") or (ranked[0][0] if ranked else None)
        t["confidence"] = float(ch.get("confidence") or (ranked[0][1] if ranked else 0))
        t["runner_up"] = ranked[1] if len(ranked) > 1 else None
        t["p_transfer"] = float(a.get("own_transfer", {}).get("noul") or 0)
        t["model"] = jev[t["id"]]["body"].get("model", MODEL)
        t["latency_ms"], t["cached"] = jev[t["id"]]["ms"], jev[t["id"]]["cached"]
        t["tokens"] = (jev[t["id"]]["body"].get("usage") or {}).get("input_tokens", 0)

        says_transfer = t["jev_line"] == TRANSFER
        if says_transfer != (t["p_transfer"] >= 0.5):
            t["reasons"].append("Jev's two answers disagree on whether this is a transfer")
        if t["pair"] and not says_transfer:
            other = by_id[t["pair"]]
            t["reasons"].append(f"Matching amount found in {other['account']} on {other['date']:%b %d}, but Jev says it isn't a transfer")
        two_signals = t["pair"] and says_transfer and not t["reasons"]
        if not two_signals:
            need = required_confidence(t["base"])
            if need is None:
                t["reasons"].append(f"Over the {MATERIALITY[-2][0]:,} materiality limit: always reviewed"
                                    + (" (other side not in your data)" if says_transfer else ""))
            elif t["confidence"] < need:
                t["reasons"].append(f"Confidence {t['confidence']:.1%} is below the {need:.0%} needed at this amount")
        if t["reasons"]:
            t["line"], t["by"] = "Pending review", "Review queue"
        else:
            t["line"], t["by"] = t["jev_line"], "Jev + code agree" if two_signals else "Jev"
    return txns


# ---------------------------------------------------------------------------
# Statements (all arithmetic in code)
# ---------------------------------------------------------------------------
DOMESTIC_FX = {"CANADIAN DOLLAR", "US DOLLAR"}


def apply_exclusions(txns, patterns, foreign):
    """Your scope decisions. Excluded items are never sent to Jev and never listed in outputs;
    they appear only as one total so the statements still tie."""
    out = []
    for t in txns:
        d = t["description"].upper()
        hit = any(p.upper() in d for p in patterns)
        if foreign:
            hit = hit or (t.get("fx_currency") and t["fx_currency"] not in DOMESTIC_FX) \
                  or bool(re.search(r"[^\x00-\x7F•]", t["description"]))
        if hit:
            out.append(t)
    return out


def timing_notes(txns, accounts):
    """Cutoff: a transfer whose other account is loaded but whose statement period ends before
    (or starts after) this date. Like an outstanding item on a bank reconciliation."""
    for t in txns:
        t["note"] = ""
        if t["pair"] or TRANSFER not in (t["line"], t["jev_line"]):
            continue
        for a in accounts.values():
            last4 = re.search(r"••(\d{4})$", a["name"])
            if a["name"] != t["account"] and last4 and last4.group(1) in t["description"] and a["end"]:
                if t["date"] > a["end"]:
                    t["note"] = f"Timing: {a['name']} statement ends {a['end']:%b %d}; expect the other side on its next statement"
                elif t["date"] < a["start"]:
                    t["note"] = f"Timing: before {a['name']} statement starts {a['start']:%b %d}; the other side is on its previous statement"
                else:
                    t["note"] = f"{a['name']} is loaded and covers this date, but no matching amount was found"


def month_key(d: date) -> str:
    return f"{d:%Y-%m}"


def income_statement(txns):
    months = sorted({month_key(t["date"]) for t in txns})
    grid = defaultdict(lambda: defaultdict(float))
    for t in txns:
        grid[t["line"]][month_key(t["date"])] += t["base"]
    return months, grid


def analytical_review(months, grid, pct=0.30, min_abs=75):
    flags = []
    for line in EXPENSE_LINES:
        for a, b in zip(months, months[1:]):
            prev, cur = -grid[line].get(a, 0), -grid[line].get(b, 0)
            change = cur - prev
            if abs(change) >= min_abs and (prev == 0 or abs(change) / prev >= pct):
                flags.append({"line": line, "from": a, "to": b, "prev": prev, "cur": cur, "change": change})
    return flags


def calibration(txns):
    lab = [t for t in txns if t["my_label"] and t["confidence"] is not None]
    if not lab:
        return None
    right = lambda t: t["jev_line"] == t["my_label"]
    rows = []
    for th in [0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95]:
        s = [t for t in lab if t["confidence"] >= th]
        if s:
            rows.append((th, len(s), sum(map(right, s)) / len(s), len(s) / len(lab)))
    rec = next((r for r in rows if r[2] >= 0.98 and r[1] >= 10), None)
    misses = [t for t in lab if not right(t)]
    return {"n": len(lab), "acc": sum(map(right, lab)) / len(lab), "rows": rows, "rec": rec, "misses": misses}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def esc(x) -> str:
    return html.escape(str(x))


def fmt(x: float, base: str, signed=False) -> str:
    if abs(x) < 0.005:
        return "–"
    s = f"{abs(x):,.2f}"
    return ("−" if x < 0 else "+" if signed else "") + s


def native(t) -> str:
    return f"{t['amount']:,.2f} {t['currency']}"


def conf_txt(t) -> str:
    return "" if t["confidence"] is None else f"{t['confidence']:.0%}"


def build_report(ctx) -> str:
    ALL, base, accounts = ctx["txns"], ctx["base"], ctx["accounts"]
    T = [t for t in ALL if t["by"] != "Excluded"]
    X = [t for t in ALL if t["by"] == "Excluded"]
    excluded_total = sum(t["base"] for t in X)
    months, grid = ctx["months"], ctx["grid"]
    by = defaultdict(int)
    for t in T:
        by[t["by"]] += 1
    review = [t for t in T if t["by"] == "Review queue"]
    jev_answered = [t for t in T if t["confidence"] is not None]

    # --- income statement table
    def is_row(line, cls=""):
        vals = [grid[line].get(m, 0) for m in months]
        if not any(abs(v) > 0.004 for v in vals):
            return ""
        return f"<tr class='{cls}'><td>{esc(line)}</td>" + "".join(f"<td class=num>{fmt(v, base)}</td>" for v in vals) + \
               f"<td class='num tot'>{fmt(sum(vals), base)}</td></tr>"

    def total_row(label, lines, cls="sub"):
        vals = [sum(grid[l].get(m, 0) for l in lines) for m in months]
        return f"<tr class={cls}><td>{label}</td>" + "".join(f"<td class=num>{fmt(v, base)}</td>" for v in vals) + \
               f"<td class='num tot'>{fmt(sum(vals), base)}</td></tr>"

    head = "<tr><th></th>" + "".join(f"<th class=num>{datetime.strptime(m, '%Y-%m'):%b %Y}</th>" for m in months) + "<th class=num>Total</th></tr>"
    inc = "".join(is_row(l) for l in INCOME_LINES)
    exp = "".join(is_row(l) for l in EXPENSE_LINES)
    is_html = (f"<table>{head}<tr class=sec><td colspan={len(months) + 2}>Income</td></tr>{inc}{total_row('Total income', INCOME_LINES)}"
               f"<tr class=sec><td colspan={len(months) + 2}>Expenses</td></tr>{exp}{total_row('Total expenses', EXPENSE_LINES)}"
               f"{total_row('Net income (what you kept)', list(INCOME_LINES) + list(EXPENSE_LINES), 'grand')}"
               f"{is_row('Pending review', 'pend')}{is_row(EXCLUDED, 'pend')}</table>")
    net_income = sum(grid[l].get(m, 0) for l in list(INCOME_LINES) + list(EXPENSE_LINES) for m in months)
    pending = sum(grid["Pending review"].values())
    transfers_net = sum(grid[TRANSFER].values())

    # --- balance sheet
    def acct_rows(types):
        rows, o, c = "", 0, 0
        for a in accounts.values():
            if a["type"] in types:
                ob, cb = to_base(a["opening"], a["currency"], base), to_base(a["closing"], a["currency"], base)
                o, c = o + ob, c + cb
                nat = f"<br><small>{a['closing']:,.2f} {a['currency']}</small>" if a["currency"] != base else ""
                rows += f"<tr><td>{esc(a['name'])}</td><td class=num>{fmt(ob, base)}</td><td class=num>{fmt(cb, base)}{nat}</td><td class=num>{fmt(cb - ob, base, True)}</td></tr>"
        return rows, o, c
    ar, ao, ac = acct_rows({"cash", "investment"})
    lr, lo, lc = acct_rows({"credit"})
    nw_o, nw_c = ao + lo, ac + lc
    bs_html = (f"<table><tr><th>Account</th><th class=num>Opening</th><th class=num>Closing</th><th class=num>Change</th></tr>"
               f"<tr class=sec><td colspan=4>What you own</td></tr>{ar}<tr class=sub><td>Total assets</td><td class=num>{fmt(ao, base)}</td><td class=num>{fmt(ac, base)}</td><td class=num>{fmt(ac - ao, base, True)}</td></tr>"
               + (f"<tr class=sec><td colspan=4>What you owe</td></tr>{lr}<tr class=sub><td>Total liabilities</td><td class=num>{fmt(lo, base)}</td><td class=num>{fmt(lc, base)}</td><td class=num>{fmt(lc - lo, base, True)}</td></tr>" if lr else "")
               + f"<tr class=grand><td>Net worth</td><td class=num>{fmt(nw_o, base)}</td><td class=num>{fmt(nw_c, base)}</td><td class=num>{fmt(nw_c - nw_o, base, True)}</td></tr></table>")
    unmatched_tr = sum(t["base"] for t in T if t["line"] == TRANSFER and not t["pair"])
    matched_tr = transfers_net - unmatched_tr
    recon_diff = (nw_c - nw_o) - (net_income + pending + transfers_net + excluded_total)
    recon_ok = abs(recon_diff) < 0.05 + 0.01 * len(accounts)
    recon_html = (f"<table class=recon><tr><td>Net income, from the income statement</td><td class=num>{fmt(net_income, base, True)}</td></tr>"
                  f"<tr><td>Still in the review queue</td><td class=num>{fmt(pending, base, True)}</td></tr>"
                  + (f"<tr><td>Excluded from this analysis by your settings ({len(X)} items)</td><td class=num>{fmt(excluded_total, base, True)}</td></tr>" if X else "")
                  + f"<tr><td>Transfers between loaded accounts (should net to zero)</td><td class=num>{fmt(matched_tr, base, True) if abs(matched_tr) > .004 else '0.00'}</td></tr>"
                  f"<tr><td>Transfers whose other side isn't in this data (other accounts or timing)</td><td class=num>{fmt(unmatched_tr, base, True)}</td></tr>"
                  f"<tr class=grand><td>= Change in net worth {'✓ ties to the balance sheet' if recon_ok else '✗ does not tie'}</td><td class=num>{fmt(nw_c - nw_o, base, True)}</td></tr></table>")

    # --- cash flow (bank/cash accounts only)
    cf = defaultdict(float)
    acct_type = {a["name"]: a["type"] for a in accounts.values()}
    by_id = {t["id"]: t for t in T}
    for t in ALL:
        if t["acct_type"] != "cash":
            continue
        if t["line"] == EXCLUDED:
            k = EXCLUDED
        elif t["line"] == TRANSFER:
            if t["pair"]:
                ot = acct_type[by_id[t["pair"]]["account"]]
                k = {"credit": "Paid credit card bills", "investment": "Saved & invested", "cash": "Moved between bank accounts"}[ot]
            else:
                k = "Transfers to/from accounts not loaded"
        elif t["line"] in INCOME_LINES:
            k = "Income received"
        elif t["line"] in EXPENSE_LINES:
            k = "Spent directly from bank accounts"
        else:
            k = "Pending review"
        cf[k] += t["base"]
    cash_o = sum(to_base(a["opening"], a["currency"], base) for a in accounts.values() if a["type"] == "cash")
    cash_c = sum(to_base(a["closing"], a["currency"], base) for a in accounts.values() if a["type"] == "cash")
    order = ["Income received", "Spent directly from bank accounts", "Paid credit card bills", "Saved & invested",
             "Moved between bank accounts", "Transfers to/from accounts not loaded", "Pending review", EXCLUDED]
    cf_html = (f"<table><tr><td>Cash in bank accounts at start</td><td class=num>{fmt(cash_o, base)}</td></tr>"
               + "".join(f"<tr><td>{k}</td><td class=num>{fmt(cf[k], base, True)}</td></tr>" for k in order if abs(cf[k]) > .004)
               + f"<tr class=grand><td>Cash at end {'✓' if abs(cash_o + sum(cf.values()) - cash_c) < .05 + .01 * len(accounts) else '✗'}</td><td class=num>{fmt(cash_c, base)}</td></tr></table>")

    # --- controls
    c = []
    read_f = ctx["read_findings"]
    c.append(("Completeness: every transaction read",
              "Before Jev sees anything, code proves the statements were read correctly using the bank's own numbers.",
              read_f))
    c.append(("Cutoff: no missing statements", "Consecutive statements for the same account must link with no gaps.",
              ctx["link_findings"]))
    mat_txt = ", ".join(f"<{lim:,.0f}: {need:.0%}" for lim, need in MATERIALITY if need) + f", ≥{MATERIALITY[-2][0]:,}: always you"
    over = [t for t in review if any("materiality" in r for r in t["reasons"])]
    low = [t for t in review if any("Confidence" in r for r in t["reasons"])]
    c.append(("Materiality: bigger amounts need more certainty", f"Required confidence by amount ({base}): {mat_txt}.",
              [{"ok": None, "text": f"{len(low)} below the confidence needed for their amount; {len(over)} over the materiality limit"}]))
    n_pairs = len(ctx["pairs"]) // 2
    disagree = [t for t in review if any("transfer" in r.lower() and "materiality" not in r for r in t["reasons"])]
    unm = [t for t in T if (t["line"] == TRANSFER or t["jev_line"] == TRANSFER) and not t["pair"]]
    c.append(("Transfers: two independent signals", "Code matches equal-and-opposite amounts across your loaded accounts; Jev separately judges if it's a transfer. Disagreements go to you.",
              [{"ok": True, "text": f"{n_pairs} transfer pair(s) matched across loaded accounts"},
               {"ok": None if disagree else True, "text": f"{len(disagree)} disagreement(s) sent for review"},
               {"ok": None if unm else True, "text": f"{len(unm)} transfer(s) without a matched other side: "
                f"{sum(1 for t in unm if t['note'].startswith('Timing'))} explained by statement timing, the rest go to accounts not loaded"}]))
    if X:
        c.append(("Scope: your exclusions", "Transactions you chose to leave out. Not sent to Jev, not listed anywhere, shown only as one total so everything still ties.",
                  [{"ok": True, "text": f"{len(X)} transaction(s) excluded, net {fmt(excluded_total, base, True)} {base}"}]))
    c.append(("Your rules and decisions win", "Anything you've decided once is applied by code next time, so Jev isn't asked again.",
              [{"ok": True, "text": f"{by['Saved rule']} by saved rule · {by['You']} by your review · {by['Jev'] + by['Jev + code agree']} by Jev · {by['Review queue']} waiting for you"}]))
    flags = ctx["flags"]
    c.append(("Analytical review: unusual swings", "Expense lines moving ≥30% and ≥75 month over month are flagged, the way an auditor scans for surprises.",
              [{"ok": None if flags else True, "text": f"{len(flags)} swing(s) flagged" + (" (note: partial months distort this)" if ctx["partial"] else "")}]))
    c.append(("Statements tie", "Net income + review queue + transfers must equal the change in net worth on the balance sheet.",
              [{"ok": recon_ok, "text": "Ties exactly" if recon_ok else f"Off by {recon_diff:,.2f} {base}"}]))
    cal = ctx["cal"]
    c.append(("Calibration: is Jev's confidence honest?", "Compare Jev to your own labels; pick the threshold from evidence.",
              [{"ok": None, "text": (f"{cal['acc']:.0%} agreement on {cal['n']} labelled; " +
                                     (f"≥98% accurate at confidence ≥{cal['rec'][0]:.0%} (covers {cal['rec'][3]:.0%})" if cal["rec"] else "no threshold reached 98% on ≥10 items"))
                if cal else "Add a my_label column to measure this"}]))

    def chip(ok):
        return "<span class='chip ok'>✓</span>" if ok is True else "<span class='chip bad'>✗</span>" if ok is False else "<span class='chip info'>•</span>"
    ctrl_html = "".join(
        f"<div class=ctrl><div class=ch>{'<span class=\"st ok\">Passed</span>' if all(f['ok'] is not False for f in fs) and all(f['ok'] for f in fs) else ('<span class=\"st bad\">Failed</span>' if any(f['ok'] is False for f in fs) else '<span class=\"st info\">Review</span>')}"
        f"<b>{esc(name)}</b></div><p>{esc(desc)}</p><ul>" + "".join(f"<li>{chip(f['ok'])}{esc(f['text'])}</li>" for f in fs) + "</ul></div>"
        for name, desc, fs in c)

    # --- review queue
    rq = "".join(
        f"<tr><td class=nw>{t['date']}</td><td>{esc(t['shown'])}<br><small>{esc(t['account'])} · id {t['id']}</small></td>"
        f"<td class=num>{native(t)}</td><td>{esc(t['jev_line'])} <span class=pill>{t['confidence']:.0%}</span>"
        + (f"<br><small>then {esc(t['runner_up'][0])} {t['runner_up'][1]:.0%}</small>" if t["runner_up"] else "")
        + f"</td><td>{'<br>'.join(esc(r) for r in t['reasons'])}{'<br><small>' + esc(t['note']) + '</small>' if t['note'] else ''}</td></tr>" for t in sorted(review, key=lambda t: -abs(t["base"])))

    flags_html = "".join(f"<tr><td>{esc(f['line'])}</td><td>{f['from']} → {f['to']}</td><td class=num>{fmt(f['prev'], base)}</td>"
                         f"<td class=num>{fmt(f['cur'], base)}</td><td class=num>{fmt(f['change'], base, True)}</td></tr>" for f in flags)

    cal_html = ""
    if cal:
        cal_html = ("<table><tr><th>Auto-accept at confidence ≥</th><th class=num>Txns that qualify</th><th class=num>Share of labelled</th><th class=num>Actually right</th></tr>"
                    + "".join(f"<tr class='{'hl' if cal['rec'] and r[0] == cal['rec'][0] else ''}'><td>{r[0]:.0%}</td><td class=num>{r[1]}</td><td class=num>{r[3]:.0%}</td><td class=num>{r[2]:.0%}</td></tr>" for r in cal["rows"])
                    + "</table>"
                    + ("<h3>Where Jev and I disagreed</h3><table><tr><th>Transaction</th><th>Jev</th><th>Me</th><th class=num>Conf.</th><th>Caught by</th></tr>"
                       + "".join(f"<tr><td>{esc(t['shown'])}</td><td>{esc(t['jev_line'])}</td><td>{esc(t['my_label'])}</td><td class=num>{t['confidence']:.0%}</td>"
                                 f"<td>{'Review queue ✓' if t['by'] == 'Review queue' else '<b>Not caught</b>'}</td></tr>" for t in cal["misses"]) + "</table>"
                       if cal["misses"] else "<p>No disagreements.</p>"))

    all_rows = "".join(
        f"<tr class='{'rv' if t['by'] == 'Review queue' else ''}'><td class=nw>{t['date']}</td><td>{esc(t['account'])}</td><td>{esc(t['shown'])}</td>"
        f"<td class=num>{native(t)}</td><td>{esc(t['line'])}</td><td>{esc(t['by'])}</td>"
        f"<td class=num>{conf_txt(t)}</td>"
        f"<td>{'↔ ' + by_id[t['pair']]['account'] if t['pair'] else ''}</td></tr>" for t in T)

    lat = [t["latency_ms"] for t in jev_answered if not t.get("cached") and t.get("latency_ms")]
    d0, d1 = min(t["date"] for t in T), max(t["date"] for t in T)
    banner = "<div class=banner>MOCK RUN: answers come from keyword rules, not Jev. Set TYPESAFE_API_KEY and drop --mock for real results.</div>" if ctx["mock"] else ""
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Jev Ledger: audited personal statements</title>
<style>
:root{{--ink:#16181d;--mute:#5d6470;--line:#e4e6ea;--bg:#fbfaf7;--card:#fff;--acc:#2f5bd3;--ok:#1f8a4c;--warn:#b7791f;--bad:#c0392b}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif}}
main{{max-width:1000px;margin:0 auto;padding:44px 24px 80px}}h1{{font-size:32px;letter-spacing:-.02em;margin:0 0 4px}}h2{{font-size:20px;margin:0 0 6px}}h3{{font-size:15px;margin:20px 0 8px}}
.meta,small{{color:var(--mute);font-size:12.5px}}.lede{{color:var(--mute);max-width:740px;margin:0 0 14px}}
section{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:24px 26px;margin-top:20px}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-top:20px}}.kpi{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}}.kpi b{{display:block;font-size:22px}}.kpi span{{color:var(--mute);font-size:12.5px}}
.ctrls{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}.ctrl{{background:var(--bg);border-radius:10px;padding:14px 16px}}.ctrl p{{margin:4px 0 6px;color:var(--mute);font-size:13px}}.ctrl ul{{margin:0;padding:0;list-style:none;font-size:13px}}.ctrl li{{margin:3px 0;display:flex;gap:7px}}
.ch{{display:flex;gap:8px;align-items:center}}.st{{font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;text-transform:uppercase;letter-spacing:.04em}}.st.ok{{background:#e6f4ec;color:var(--ok)}}.st.bad{{background:#fbe9e7;color:var(--bad)}}.st.info{{background:#fdf3e1;color:var(--warn)}}
.chip{{font-weight:700;width:14px;flex:none}}.chip.ok{{color:var(--ok)}}.chip.bad{{color:var(--bad)}}.chip.info{{color:var(--warn)}}
table{{width:100%;border-collapse:collapse;font-size:13.5px}}th,td{{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}}th{{color:var(--mute);font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.04em}}
.num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}.nw{{white-space:nowrap}}.tot{{font-weight:600}}
tr.sec td{{font-weight:700;padding-top:14px;border-bottom:none;color:var(--mute);font-size:12px;text-transform:uppercase;letter-spacing:.05em}}tr.sub td{{font-weight:600}}tr.grand td{{font-weight:700;border-top:2px solid var(--ink)}}tr.pend td{{background:#fff8ec;color:var(--warn)}}tr.rv td{{background:#fff8ec}}tr.hl td{{background:#eef3ff;font-weight:600}}
table.recon{{max-width:640px}}.pill{{display:inline-block;padding:0 7px;border-radius:999px;font-size:12px;font-weight:600;background:#fdf3e1;color:var(--warn)}}
.banner{{background:#fdecea;border:1px solid #f5c6c0;color:#8a2c22;padding:12px 16px;border-radius:10px;margin-bottom:18px;font-weight:600}}
.split{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}.split>div{{background:var(--bg);border-radius:10px;padding:14px 16px}}.split ul{{margin:6px 0 0;padding-left:18px}}
.scroll{{overflow-x:auto}}footer{{color:var(--mute);font-size:12px;margin-top:26px}}
@media(max-width:760px){{.ctrls,.split{{grid-template-columns:1fr}}}}
</style></head><body><main>{banner}
<h1>Jev Ledger</h1>
<div class=meta>{len(T)} transactions · {len(accounts)} account(s) · {d0:%b %d, %Y} – {d1:%b %d, %Y} · reported in {base} at fixed FX · model {esc('MOCK' if ctx['mock'] else MODEL)}</div>
<p class=lede style="margin-top:12px">Personal financial statements where AI does the judgment and accounting does the checking.
Jev decides which line each transaction belongs on and whether it's money moving between my own accounts. Code reads the statements,
does all the math, and runs the controls an auditor would before any of Jev's answers are trusted.</p>
<div class=kpis>
 <div class=kpi><b>{fmt(net_income, base)}</b><span>net income ({base}), excluding review queue</span></div>
 <div class=kpi><b>{(len(T) - len(review)) / len(T):.0%}</b><span>classified without you</span></div>
 <div class=kpi><b>{len(review)}</b><span>waiting for your review</span></div>
 <div class=kpi><b>{n_pairs}</b><span>transfer pairs matched by code</span></div>
 <div class=kpi><b>{f'{statistics.median(lat):,.0f} ms' if lat else '–'}</b><span>median Jev latency</span></div>
</div>

<section><h2>Controls report</h2><p class=lede>What had to be true before these statements could be trusted.</p><div class=ctrls>{ctrl_html}</div></section>

<section><h2>Review queue</h2><p class=lede>Nothing here is in the income statement yet. Fill in <code>your_line</code> in <code>review_queue.csv</code> and re-run with <code>--reviews</code>; each decision also becomes a suggested rule.</p>
<div class=scroll><table><tr><th>Date</th><th>Transaction</th><th class=num>Amount</th><th>Jev's pick</th><th>Why it's here</th></tr>{rq or '<tr><td colspan=5>Nothing to review.</td></tr>'}</table></div></section>

<section><h2>Income statement</h2><p class=lede>In {base}. Transfers between your own accounts are excluded; they aren't income or spending.</p><div class=scroll>{is_html}</div></section>

<section><h2>Balance sheet</h2><p class=lede>Balances come straight from your statements, not from Jev. Statement periods: {'; '.join(f"{esc(a['name'])} {a['start']:%b %d} – {a['end']:%b %d}" for a in accounts.values() if a['start'])}.</p><div class=scroll>{bs_html}</div>
<h3>Does it tie?</h3>{recon_html}</section>

<section><h2>Cash flow: bank accounts</h2><p class=lede>Where the cash in your bank accounts went. Card spending shows up here only when you pay the card.</p>{cf_html}</section>

<section><h2>Analytical review</h2><p class=lede>Month-over-month swings worth a second look, flagged by code. Confidently wrong answers can hide here.</p>
<table><tr><th>Line</th><th>Months</th><th class=num>Before</th><th class=num>After</th><th class=num>Change</th></tr>{flags_html or '<tr><td colspan=5>No unusual swings.</td></tr>'}</table></section>

{'<section><h2>Calibration: should I trust the confidence score?</h2><p class=lede>I labelled these transactions myself, then compared. The highlighted row is the lowest threshold that keeps accuracy at 98% or better.</p>' + cal_html + '</section>' if cal else ''}

<section><h2>Who decides what</h2><div class=split>
<div><b>Jev (one call per transaction)</b><ul><li>Which statement line (choice, {len(LINES)} options)</li><li>Is this my own money moving? (yes/no probability)</li></ul></div>
<div><b>Code</b><ul><li>Reading statements and proving the read</li><li>Dates, FX, every sum; Jev never sees a date</li><li>Matching transfer pairs</li><li>Materiality thresholds and the review queue</li><li>Your saved rules and decisions, which always win</li></ul></div></div></section>

<section><h2>Every transaction</h2><div class=scroll><table><tr><th>Date</th><th>Account</th><th>Description</th><th class=num>Amount</th><th>Line</th><th>Decided by</th><th class=num>Conf.</th><th>Pair</th></tr>{all_rows}</table></div></section>
<footer>Generated {date.today():%Y-%m-%d} by jev_ledger.py. Long account and reference numbers are masked. Full audit trail in audit_trail.csv.</footer>
</main></body></html>"""


# ---------------------------------------------------------------------------
def write_csvs(out_dir, txns, base):
    txns = [t for t in txns if t["by"] != "Excluded"]
    with open(os.path.join(out_dir, "audit_trail.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "date", "account", "description", "amount", "currency", f"amount_{base}", "line", "decided_by",
                    "jev_line", "jev_confidence", "jev_runner_up", "p_own_transfer", "matched_with", "review_reasons", "note", "model"])
        for t in txns:
            w.writerow([t["id"], t["date"], t["account"], t["shown"], t["amount"], t["currency"], t["base"], t["line"], t["by"],
                        t["jev_line"] or "", "" if t["confidence"] is None else round(t["confidence"], 4),
                        f"{t['runner_up'][0]} ({t['runner_up'][1]:.2f})" if t["runner_up"] else "",
                        "" if t["p_transfer"] is None else round(t["p_transfer"], 4), t["pair"] or "",
                        "; ".join(t["reasons"]), t.get("note", ""), t.get("model", "")])
    with open(os.path.join(out_dir, "review_queue.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "date", "account", "description", "amount", "currency", "jev_pick", "confidence", "why", "your_line"])
        for t in txns:
            if t["by"] == "Review queue":
                w.writerow([t["id"], t["date"], t["account"], t["shown"], t["amount"], t["currency"], t["jev_line"],
                            f"{t['confidence']:.2f}", "; ".join(t["reasons"]), ""])
    with open(os.path.join(out_dir, "suggested_rules.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["contains", "line", "note"])
        seen = set()
        for t in txns:
            k = merchant_key(t["description"])
            if t["by"] == "You" and k and k not in seen:
                seen.add(k)
                w.writerow([k, t["line"], f"From your review of {t['date']}"])


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("inputs", nargs="+", help="Chase PDF statements and/or folders with accounts.csv + transactions.csv")
    p.add_argument("--base", default="USD", help="reporting currency (default USD)")
    p.add_argument("--exclude", action="append", default=[], metavar="TEXT",
                   help="leave out transactions whose description contains TEXT (repeatable). Not sent to Jev, not listed in outputs")
    p.add_argument("--exclude-foreign", action="store_true",
                   help="leave out card charges billed in a foreign currency (everything except USD and CAD) and any non-Latin text")
    p.add_argument("--rules", help="CSV with columns contains,line: your saved rules")
    p.add_argument("--reviews", help="review_queue.csv with your_line filled in")
    p.add_argument("--out-dir", default="jev_output")
    p.add_argument("--mock", action="store_true", help="no API calls; fake answers to test the pipeline")
    a = p.parse_args()

    base = a.base.upper()
    if base not in FX_TO_CAD:
        sys.exit(f"No FX rate for base currency {base}")
    key = os.environ.get("TYPESAFE_API_KEY")
    if not a.mock and not key:
        sys.exit("Set TYPESAFE_API_KEY (create one at console.typesafe.ai), or use --mock.")

    stmts = load_inputs(a.inputs)
    read_findings = check_reads(stmts)
    accounts, link_findings = merge_accounts(stmts)
    txns = flatten(stmts, base)
    print(f"Read {len(txns)} transactions from {len(stmts)} statement(s).")
    for f in read_findings + link_findings:
        print(("  ✓ " if f["ok"] else "  ✗ ") + f["text"])
    if not all(f["ok"] for f in read_findings):
        print("  ! The read doesn't tie to the bank's numbers. Fix this before trusting anything below.")

    excluded = apply_exclusions(txns, a.exclude, a.exclude_foreign)
    ex_ids = {t["id"] for t in excluded}
    if excluded:
        print(f"Excluded by your settings: {len(excluded)} transaction(s), not sent to Jev.")
    rules = load_map(a.rules, "contains", "line")
    reviews = load_map(a.reviews, "id", "your_line")
    reviewed_ids = {i for i, _ in reviews}
    to_ask = [t for t in txns if t["id"] not in reviewed_ids and t["id"] not in ex_ids
              and not rule_for(t["description"], rules)]
    print(f"Asking Jev ({'MOCK' if a.mock else MODEL}) about {len(to_ask)} transaction(s); "
          f"{len(txns) - len(to_ask) - len(excluded)} already decided by your rules or reviews.")
    jev = run_jev(to_ask, key, a.mock)
    pairs = match_transfers([t for t in txns if t["id"] not in ex_ids])
    decide(txns, jev, pairs, rules, reviews, ex_ids)
    timing_notes(txns, accounts)

    months, grid = income_statement(txns)
    partial = any((acc["start"] and acc["start"].day != 1) for acc in accounts.values())
    ctx = {"txns": txns, "base": base, "accounts": accounts, "months": months, "grid": grid, "pairs": pairs,
           "flags": analytical_review(months, grid), "cal": calibration(txns), "read_findings": read_findings,
           "link_findings": link_findings, "mock": a.mock, "partial": partial}
    os.makedirs(a.out_dir, exist_ok=True)
    with open(os.path.join(a.out_dir, "report.html"), "w", encoding="utf-8") as f:
        f.write(build_report(ctx))
    write_csvs(a.out_dir, txns, base)
    n_rev = sum(t["by"] == "Review queue" for t in txns)
    print(f"Done. {len(txns) - n_rev - len(excluded)} classified, {n_rev} in your review queue"
          + (f", {len(excluded)} excluded" if excluded else "") + f". Open {a.out_dir}/report.html")


if __name__ == "__main__":
    main()
