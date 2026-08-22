# ted_bot 🛰️

Daily automated scanner for **EU TED (Tenders Electronic Daily)** Contract Award
Notices. It detects major public-procurement awards won by publicly-traded
European small-caps, computes the **financial materiality** of each award, and
fires an instant **Telegram** push when an award is large relative to the
winner's revenue.

Designed for a zero-cost **OCI Always-Free `VM.Standard.E2.1.Micro`** running
**Oracle Linux 9**, an embedded **SQLite** database, and outbound-only network
access (no inbound service, no framework, one cron line).

---

## 1. Architecture Overview

A single Python process runs once a day from cron. It streams the previous day's
Contract Award Notices from the TED v3 API, filters by high-alpha CPV divisions,
fuzzy-matches winner names against a local watchlist, sanity-checks financials
via `yfinance`, and alerts only when both the materiality and market-cap gates
pass. Every examined notice is written to `processed_notices`, so re-runs are
idempotent and never double-alert.

```mermaid
flowchart TD
    A["cron @ 07:45 UTC"] --> B["ted_scanner.py"]
    B --> C["EU TED v3 API<br/>POST /v3/notices/search"]
    C -->|"CPV + notice-type + date filter"| D["Filtering Engine"]
    D -->|"heuristics empty? recover fields"| L["Nemotron LLM adapter<br/>extract winner / value / currency"]
    L --> E
    D --> E{"SQLite<br/>small_caps_whitelist"}
    E -->|"difflib fuzzy match<br/>(borderline → Nemotron confirms)"| F{"Winner on watchlist?"}
    F -->|no| G["record in processed_notices → next"]
    F -->|yes| H["yfinance sanity check<br/>market cap / revenue / FX"]
    H --> I{"Materiality ≥ 15%<br/>AND €100M ≤ cap ≤ €2B ?"}
    I -->|no| G
    I -->|yes| J["Telegram Notification API<br/>sendMessage"]
    J --> G

    subgraph LOCAL["OCI Always-Free micro instance"]
        B
        D
        E
        G
    end
```

**Decision rule (both must hold):**

| Gate | Condition |
|------|-----------|
| Materiality | `contract_value_EUR / annual_revenue_EUR ≥ 15%` |
| Size band | `€100M ≤ market_cap ≤ €2B` |

**CPV divisions watched:** Tech/Software `72000000`, Defense/Security
`35000000`, Biotech/Medical `33000000`, Green Energy/Infra `09330000`.

> ℹ️ **API calibration.** The query grammar, the `fields` vocabulary, and the
> winner/value/currency extraction are validated against the live TED v3 API — a
> notice's `winner-name` and `notice-title` are language-keyed dicts, values are
> string-numbers, and `fields` is required from a 1830-term eForms vocabulary. If
> TED renames a term later, the scanner retries with a minimal field set and falls
> back to key-name search (and the LLM); re-inspect with
> `python3 ted_scanner.py --dump 3 | less` and adjust `REQUEST_FIELDS` if needed.

> 🧠 **Adapt-in-the-loop (optional).** Set `NVIDIA_API_KEY` to enable a small
> LLM (`nvidia/nemotron-3-ultra-550b-a55b`, served by
> `integrate.api.nvidia.com`) that makes the pipeline self-healing on two fronts:
> when the key-name heuristics can't read a notice (schema drift), the model extracts
> `winner` / `value` / `currency` straight from the raw JSON; and when a fuzzy
> match lands in the grey zone (difflib ratio `0.70–0.87`), the model decides whether
> the TED winner and the watchlist company are the same entity — catching
> translations, abbreviations, and holding-company names difflib misses. **No key
> set → the LLM calls return `None` and the scanner runs pure-heuristic, at zero
> cost.** Override the model with `TED_LLM_MODEL` — it must be an NVIDIA id of the
> form `vendor/model`; a bare name is rejected by the endpoint, and the scanner now
> exits non-zero (and withholds the heartbeat) rather than reporting a green,
> alert-less run.

---

## 2. Quick Start & Installation (Oracle Linux 9)

```bash
# 1. System packages
sudo dnf install -y python3 python3-pip git sqlite

# 2. Clone
git clone https://github.com/cluster2600/ted_bot.git
cd ted_bot

# 3. Isolated environment + dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 4. Create the SQLite schema
python3 ted_scanner.py --init-db

# 5. Telegram credentials (create a bot via @BotFather, get your chat id)
export TELEGRAM_BOT_TOKEN="123456:ABC-your-token"
export TELEGRAM_CHAT_ID="987654321"
export NVIDIA_API_KEY="nvapi-..."       # optional — enables the Nemotron adapter

# 6. Verify offline logic, then a live no-send dry run
python3 ted_scanner.py --selftest
python3 ted_scanner.py --dry-run -v
```

> Persist the Telegram variables for cron by adding the two `export` lines to
> `~/.bashrc`, or better, to an EnvironmentFile referenced by the cron entry
> (see §4).

---

## 3. Populating the Small-Cap Whitelist

The watchlist lives in `small_caps_whitelist`. The matcher compares the TED
winner text against **`company_name_cleaned`**, which must be the normalised
legal name: lower-case, no punctuation, no legal suffix (`SA`, `GmbH`, `SpA`…).
The script normalises TED text the same way, so keep entries in that form.

| Column | Meaning | Example |
|--------|---------|---------|
| `company_name_cleaned` | normalised name for matching (required) | `exail technologies` |
| `ticker` | yfinance symbol (enables auto-refresh & FX) | `EXA.PA` |
| `isin` | optional exact key | `FR0012160365` |
| `exchange` | informational | `Euronext Paris` |
| `annual_revenue_eur` | materiality denominator | `184000000` |
| `market_cap` | EUR; the €100M–€2B gate | `520000000` |

Insert targets with plain SQL:

```bash
sqlite3 ted.db <<'SQL'
INSERT INTO small_caps_whitelist
  (company_name_cleaned, ticker, isin, exchange, annual_revenue_eur, market_cap)
VALUES
  ('exail technologies', 'EXA.PA', 'FR0012160365', 'Euronext Paris', 184000000, 520000000),
  ('theon international', 'THEON.AS', 'GRW000000009', 'Euronext Amsterdam', 271000000, 1600000000);
SQL
```

Leave `annual_revenue_eur` or `market_cap` **NULL** to have the scanner fetch and
persist them from `yfinance` on the next matching run (a `ticker` is required for
that). Populated rows are reused as-is, so you control accuracy.

> **Materiality math is only as good as `annual_revenue_eur`.** Rows the scanner
> fills from `yfinance` are converted to EUR automatically — market cap from the
> quote currency, revenue from the *reporting* currency (they differ: `AVON.L`
> quotes in GBp but reports in USD). Rows **you** insert by hand are taken at face
> value, so store those already converted to EUR.

> ⚠️ **Upgrading from a build before the FX fix?** Cached rows hold raw
> yfinance figures in the stock's own currency, and a row with both fields set is
> never refetched — so run `python3 ted_scanner.py --reset-financials` once. Until
> you do, every non-EUR company keeps failing the €100M–€2B band by roughly its FX
> rate (a DKK cap reads ~7.5x too large and is silently rejected as "too big").

---

## 4. Deployment — daily cron at 07:45 UTC

Register the scanner to run every morning at **07:45 UTC**, logging (stdout +
stderr) to `~/ted_scanner.log`. Add this with `crontab -e` (edit the path/user to
match your clone; `opc` is the default OCI user):

```cron
CRON_TZ=UTC
45 7 * * * cd /home/opc/ted_bot && /home/opc/ted_bot/.venv/bin/python3 ted_scanner.py >> /home/opc/ted_scanner.log 2>&1
```

`CRON_TZ=UTC` pins the schedule to UTC regardless of the instance timezone. If
your `cron` build ignores `CRON_TZ`, set the host clock with
`sudo timedatectl set-timezone UTC` and use a plain `45 7 * * *` line.

For Telegram credentials in cron without hard-coding them in the crontab, source
an env file at the start of the command:

```cron
45 7 * * * cd /home/opc/ted_bot && set -a && . /home/opc/ted_bot/.env && set +a && .venv/bin/python3 ted_scanner.py >> /home/opc/ted_scanner.log 2>&1
```

`.env`:

```dotenv
TELEGRAM_BOT_TOKEN=123456:ABC-your-token
TELEGRAM_CHAT_ID=987654321
NVIDIA_API_KEY=nvapi-...
```

> `chmod 600 .env` and keep it out of git (add it to `.gitignore`).

---

## What only you can do (accounts & secrets)

The code and infra are ready; these need your accounts:

1. **Telegram bot** — create one with [@BotFather](https://t.me/BotFather), copy the
   token, send your bot a message, then find your chat id, and verify delivery:
   ```bash
   TELEGRAM_BOT_TOKEN=123:ABC ./deploy/get_chat_id.sh
   TELEGRAM_BOT_TOKEN=123:ABC TELEGRAM_CHAT_ID=987 python3 ted_scanner.py --test-telegram
   ```
2. **Whitelist** — the fast path is to seed from tickers (pulls name/ISIN/revenue/
   market-cap via yfinance, normalising names to match TED text):
   ```bash
   python3 deploy/seed_from_tickers.py EXA.PA THEON.AS SWP.PA > deploy/whitelist.sql
   ```
   Review it (yfinance figures are in the stock's reporting currency; materiality
   assumes EUR), then commit `deploy/whitelist.sql` — cloud-init auto-seeds it.
   `annual_revenue_eur` accuracy drives materiality.
3. **OCI API key** — use a dedicated, least-privilege OCI deployment identity;
   feed its values into `terraform/terraform.tfvars` (see `terraform/README.md`).
4. **`NVIDIA_API_KEY`** *(optional)* — for the Nemotron adapter; get one at
   [build.nvidia.com](https://build.nvidia.com).
5. **Heartbeat** *(optional but recommended)* — create a check at
   [healthchecks.io](https://healthchecks.io), put its ping URL in `TED_HEARTBEAT_URL`
   so a silently-failed cron pages you.

Application secrets are streamed to `deploy/install-secrets.sh` only after the
VM is provisioned. They are not accepted as Terraform variables and therefore
do not enter Terraform state, cloud-init user data, or OCI instance metadata.

## Operations extras

- **Log rotation** — `deploy/ted_bot.logrotate` (installed to `/etc/logrotate.d/`).
- **DB backup** — `deploy/backup.sh` runs weekly (Sun 03:15 UTC), keeps 4 local
  snapshots, and uploads one to `OCI_BACKUP_BUCKET` if set.
- **Heartbeat** — `TED_HEARTBEAT_URL` is pinged with `?notices=&alerts=` after each run.
- **FQDN + TLS** *(only if you expose a service)* — [`docs/dns-tls.md`](docs/dns-tls.md):
  free DuckDNS hostname + Let's Encrypt cert via DNS-01 (no inbound ports).

## Command reference

| Command | Purpose |
|---------|---------|
| `python3 ted_scanner.py --init-db` | create the SQLite schema |
| `python3 ted_scanner.py` | daily scan (cron target) |
| `python3 ted_scanner.py --dry-run -v` | scan and log decisions; send nothing, record nothing |
| `python3 ted_scanner.py --dump 3` | print raw JSON of 3 notices (field discovery) |
| `python3 ted_scanner.py --reset-financials` | clear cached cap/revenue so they refetch in EUR |
| `python3 ted_scanner.py --selftest` | offline logic self-check |

## License

MIT.
