# PrisGeek Price Tracker & MCP Server

Local-first Swedish hardware price tracker for [prisgeek.se](https://prisgeek.se) combining:
1. **An on-demand MCP server (`mcp_server.py`)** for searching PrisGeek, inspecting historical price context, browsing daily *Prisfall* deals, and adding both exact-product and keyword/interest watches via chat.
2. **A standalone headless watcher (`watcher.py`)** sharing the same SQLite WAL database (`prisgeek_tracker.db`), designed to run on a schedule (Windows Task Scheduler, cron, or background loop) and fire alerts to Home Assistant or other webhooks.

---

## Architecture

| File | Responsibility |
| --- | --- |
| `prisgeek_client.py` | Queries PrisGeek's Supabase PostgREST endpoints + SSR HTML/JSON-LD pages (`search_products`, `get_product_details`, `get_prisfall`). Automatically refreshes the publishable API key from frontend bundles if rotated. |
| `db.py` | SQLite (`WAL` mode) storage for `product_watches`, `query_watches`, `price_history`, and `alert_log` (deduplicates alerts so you aren't spammed for the same price). |
| `notifier.py` | Sends HTTP POST JSON notifications to `HA_WEBHOOK_URL` (and optional `EXTRA_WEBHOOK_URL`). Reads from environment variables or `config.json`. |
| `watcher.py` | Standalone CLI worker checking active product & interest watches (`--once`, `--loop --interval 3600`, `--no-webhook`, `--status`). |
| `mcp_server.py` | `stdio` MCP server exposing tools to Antigravity IDE or other MCP clients. |
| `dashboard.py` | Local Starlette/Uvicorn web dashboard (`http://localhost:8050`) for viewing watches, searching PrisGeek, browsing deals, and inspecting price history. |

---

## Setup

Create the Python 3.13 virtual environment and install dependencies:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\pip.exe install -r requirements.txt
```

### Optional Webhook Configuration

Set environment variables or create `config.json` in `k:\price tracker\`:

```json
{
  "HA_WEBHOOK_URL": "http://homeassistant.local:8123/api/webhook/black_friday_alert",
  "EXTRA_WEBHOOK_URL": "",
  "WEBHOOK_ENABLED": true
}
```

---

## Adding to Antigravity IDE (`mcp_config.json`)

Add this entry to your Antigravity MCP configuration (`C:\Users\<you>\.gemini\antigravity\mcp_config.json`):

```json
{
  "mcpServers": {
    "prisgeek-tracker": {
      "command": "k:\\price tracker\\.venv\\Scripts\\python.exe",
      "args": [
        "k:\\price tracker\\mcp_server.py"
      ]
    }
  }
}
```

### Available MCP Tools
- `search_products(query, max_price, in_stock_only, exclude_refurbished, limit)`
- `inspect_product(public_id_or_url)`
- `browse_prisfall(category, min_drop_pct, period_days, max_price, in_stock_only, sort, limit)`
- `add_product_watch(public_id_or_url, target_price, in_stock_only, exclude_refurbished, notes)`
- `add_interest_watch(name, search_query, target_price, must_include_keywords, exclude_keywords, in_stock_only)`
- `list_watches()`
- `remove_watch(watch_type, watch_id)`
- `run_price_check_now(send_webhooks)`

---

## Running the Standalone Watcher

Run a single check manually:
```powershell
.\.venv\Scripts\python.exe watcher.py --once
```

Check active watches and recent history:
```powershell
.\.venv\Scripts\python.exe watcher.py --status
```

Run continuously every hour (e.g. in a background terminal or service):
```powershell
.\.venv\Scripts\python.exe watcher.py --loop --interval 3600
```

Or register a Windows Task Scheduler job running `k:\price tracker\.venv\Scripts\python.exe` with argument `"k:\price tracker\watcher.py" --once` every 30–60 minutes.

---

## Web Dashboard

Start the local web dashboard:
```powershell
.\.venv\Scripts\python.exe dashboard.py
```

Then open [http://localhost:8050](http://localhost:8050) in your browser to view active watches, add/remove product or keyword watches, search PrisGeek live, browse daily *Prisfall* deals, and inspect price history and triggered alerts. Set the `PORT` environment variable to override the default port (`8050`).

---

## Installing as a Home Assistant OS Add-on (Raspberry Pi 5)

### Option A: One-Click Install via Home Assistant Add-on Store (Recommended)
1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**.
2. Click the **⋮** menu (top right) → **Repositories**.
3. Add `https://github.com/emanuellonnberg/prisgeek-ha-addon` and close the dialog.
4. Click **PrisGeek Price Tracker**, click **Install**, enable **Show in sidebar**, and click **Start**.

### Option B: Manual Local Add-on Copy
You can also run it directly from `/addons` or `/local_apps`:

1. **Copy the folder to your Pi 5:**
   - Using the **Samba share** add-on (or SSH/SCP), copy the `k:\price tracker` files (`config.yaml`, `Dockerfile`, `.dockerignore`, `requirements.txt`, `db.py`, `prisgeek_client.py`, `notifier.py`, `watcher.py`, `mcp_server.py`, `dashboard.py`, `addon_entrypoint.py`) into:
     `\\homeassistant.local\addons\prisgeek_tracker\`
   - *(Tip: Skip `.venv/` and `*.db` — `.dockerignore` excludes them automatically too.)*

2. **Install & Start the Add-on in Home Assistant:**
   - Open **Settings → Add-ons → Add-on Store**.
   - Click the **⋮** menu (top right) → **Check for updates**.
   - Under **Local add-ons**, open **PrisGeek Price Tracker** and click **Install** (this builds the `aarch64` Python image on your Pi 5).
   - Enable **Show in sidebar** and click **Start**.

3. **Connect Antigravity IDE on This PC to the Pi 5 Add-on:**
   - In `k:\price tracker\config.json` on your PC, set:
     ```json
     "PRISGEEK_REMOTE_URL": "http://homeassistant.local:8050"
     ```
   - Antigravity's MCP tools (`add_product_watch`, `add_interest_watch`, `list_watches`, `remove_watch`, `run_price_check_now`) will then manage the Pi 5 add-on directly over LAN.
   - Once migrated, you can disable or delete the local Windows Scheduled Task:
     ```powershell
     schtasks /Delete /TN "PrisGeekPriceTracker" /F
     ```
