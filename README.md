# Jev Ledger: personal financial statements, audited by design

AI does the judgment, and accounting does the checking.

[TypeSafe AI's Jev](https://typesafe.ai) decides which line of my income statement each transaction belongs on, and whether money is just moving between my own accounts. Plain code does everything else: it reads the bank statements, does all the math, and runs the checks an auditor would run before any of Jev's answers are trusted.

The output is a personal **income statement**, **balance sheet** and **bank cash flow**, plus a **controls report** and an **audit trail** for every number.

## Why controls?

Jev returns a calibrated confidence with every answer. That's what makes automation safe, but only if you build around it. Jev also has documented weak spots: it reads dates as text, can't count reliably, and a choice question always returns *some* option, even when it's wrong. So each of Jev's answers has to pass these checks:

| Control | What it checks | Done by |
|---|---|---|
| **Completeness** | Opening balance + every transaction = closing balance. For PDFs, the running balance on every line and the bank's own summary totals must also match. | Code, before Jev is called |
| **Cutoff** | Consecutive statements for an account link with no gaps or missing months. | Code |
| **Materiality** | Bigger amounts need more certainty: under 100 needs 75% confidence, under 1,000 needs 90%, and 1,000 or more is always reviewed by you. | Code policy, applied to Jev's confidence |
| **Transfers, two signals** | Code matches equal and opposite amounts across your accounts. Jev separately judges whether the item is a transfer. If they disagree, you review it. Transfers that sit across statement dates are explained as timing differences. | Code + Jev |
| **Your decisions win** | Reviewed items and saved rules are applied by code. Jev isn't asked again, which also saves cost. | Code |
| **Analytical review** | Flags any expense line that moves 30% or more (and at least 75) month over month, to catch confident mistakes. | Code |
| **Statements tie** | Net income + items in review + transfers = change in net worth. | Code |
| **Calibration** | Against your own labels, finds the lowest confidence level at which Jev stays at least 98% accurate. | Code + your labels |

## Run it

```bash
export TYPESAFE_API_KEY=...          # console.typesafe.ai (new accounts get free credit)

# Sample data: fictional Chase checking, savings and credit card, 3 months
python3 jev_ledger.py sample/ --rules sample/rules.csv

# Your own Chase PDF statements (checking, savings, credit card)
pip install pdfplumber
python3 jev_ledger.py ~/statements/*.pdf

# Leave transactions out of the analysis
python3 jev_ledger.py ~/statements/*.pdf --exclude "SOME PAYEE" --exclude-foreign

# Test the pipeline with fake answers (no API key, no cost)
python3 jev_ledger.py sample/ --mock
```

The output is written to `jev_output/`:
- `report.html`: the statements and the controls report
- `review_queue.csv`: the items Jev and the controls couldn't settle
- `audit_trail.csv`: every transaction with its line, who decided it, Jev's confidence, its matched pair, and the reasons for review
- `suggested_rules.csv`: rules built from your review decisions

### The review loop

1. Open `jev_output/review_queue.csv` and fill in `your_line` for each row.
2. Re-run with `--reviews jev_output/review_queue.csv`. Your answers go into the statements.
3. Copy the rules you want to keep from `suggested_rules.csv` into your rules file. Next month, code handles those merchants and Jev only sees new ones.

Saved rules skip the materiality check, because you've already decided those items. Keep rules for recurring merchants, not one-off amounts.

## Leaving things out

`--exclude TEXT` (repeatable) drops any transaction whose description contains TEXT. `--exclude-foreign` drops card charges billed in a foreign currency (anything other than USD or CAD) and any description in non-Latin script.

Excluded transactions are still counted in the completeness check, so the read is still proven. They are never sent to Jev and never listed in the report or CSVs. They appear only as one total, so the statements still tie.

## Inputs

- **Chase PDFs.** Chase checking/savings statements and Chase credit card statements are read directly, and each is checked against the bank's own totals.
- **CSV folder.** `accounts.csv` (account, type = cash/credit/investment, currency, opening_balance, closing_balance, period_start, period_end) plus `transactions.csv` (date, account, description, amount, optional my_label). Amounts are negative for money out, and credit card balances owed are negative.

Reports are in USD by default (`--base CAD` to switch). FX rates are fixed in `FX_TO_CAD` so every statement ties exactly.

## Privacy

Jev sees only a masked description, the direction and amount, and the account type. It never sees dates, names, or full account or reference numbers. Long numbers are masked in every output. Don't publish a report made from your real statements; the `sample/` data is fictional.
