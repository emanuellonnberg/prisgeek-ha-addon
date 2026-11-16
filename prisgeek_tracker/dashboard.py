"""Local Starlette/Uvicorn web dashboard for viewing and managing PrisGeek price watches."""

from __future__ import annotations

import logging
import os
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from ai_advisor import (
    DEFAULT_AI_PROFILE,
    analyze_product_deal,
    find_cross_tier_alternatives,
    generate_ai_deal_digest,
    vet_interest_watch_matches,
)
from db import (
    get_recent_history,
    get_setting,
    init_db,
    list_all_watches,
    remove_watch,
    update_product_watch_config,
    update_query_watch_config,
)
from mcp_server import (
    add_interest_watch,
    add_product_watch,
    browse_fyndvaror,
    browse_medlemsfynd,
    browse_prisfall,
    inspect_product,
    optimize_shopping_basket,
    search_products,
)
from notifier import load_notifier_config, send_price_alert
from watcher import run_check_cycle

logger = logging.getLogger(__name__)

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="sv">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>PrisGeek Price Tracker — Dashboard</title>
  <style>
    :root {
      --bg: #0b0f17;
      --surface: #131a26;
      --surface-2: #1a2333;
      --border: #263248;
      --text: #e6edf7;
      --muted: #92a4c0;
      --accent: #38bdf8;
      --accent-hover: #0ea5e9;
      --green: #22c55e;
      --green-bg: rgba(34, 197, 94, 0.14);
      --amber: #f59e0b;
      --amber-bg: rgba(245, 158, 11, 0.14);
      --red: #ef4444;
      --red-bg: rgba(239, 68, 68, 0.14);
      --purple: #a855f7;
      --purple-bg: rgba(168, 85, 247, 0.14);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.45;
    }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
    .container { max-width: 1320px; margin: 0 auto; padding: 20px; }

    header {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      padding-bottom: 18px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 20px;
    }
    .title-wrap h1 { margin: 0; font-size: 1.45rem; letter-spacing: -0.02em; }
    .webhook-badge {
      display: inline-block;
      margin-top: 6px;
      font-size: 0.78rem;
      color: var(--muted);
      background: var(--surface);
      border: 1px solid var(--border);
      padding: 3px 10px;
      border-radius: 999px;
    }
    .header-actions { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }

    button {
      cursor: pointer;
      border: 1px solid transparent;
      border-radius: 8px;
      padding: 9px 14px;
      font-size: 0.88rem;
      font-weight: 600;
      transition: background 0.15s, border-color 0.15s, opacity 0.15s;
    }
    button:disabled { opacity: 0.55; cursor: not-allowed; }
    .btn-primary { background: var(--accent); color: #06131f; }
    .btn-primary:hover:not(:disabled) { background: var(--accent-hover); }
    .btn-secondary {
      background: var(--surface-2);
      color: var(--text);
      border-color: var(--border);
    }
    .btn-secondary:hover:not(:disabled) { border-color: var(--accent); }
    .btn-danger {
      background: var(--red-bg);
      color: #fca5a5;
      border-color: rgba(239, 68, 68, 0.35);
      padding: 5px 10px;
      font-size: 0.8rem;
    }
    .btn-danger:hover:not(:disabled) { background: rgba(239, 68, 68, 0.25); }
    .btn-sm { padding: 5px 10px; font-size: 0.78rem; }
    .actions-cell { display: flex; flex-wrap: wrap; gap: 6px; }

    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 14px;
      margin-bottom: 22px;
    }
    .kpi-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px;
    }
    .kpi-label { font-size: 0.8rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
    .kpi-value { font-size: 1.85rem; font-weight: 700; margin-top: 6px; }
    .kpi-value.good { color: var(--green); }

    .tabs {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      border-bottom: 1px solid var(--border);
      padding-bottom: 12px;
      margin-bottom: 20px;
    }
    .tab-btn {
      background: var(--surface);
      color: var(--muted);
      border: 1px solid var(--border);
    }
    .tab-btn.active {
      background: var(--accent);
      color: #06131f;
      border-color: var(--accent);
    }
    .tab-panel { display: none; }
    .tab-panel.active { display: block; }

    .forms-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
      gap: 16px;
      margin-bottom: 22px;
    }
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 18px;
      margin-bottom: 18px;
    }
    .card h2, .card h3 { margin-top: 0; font-size: 1.05rem; }

    .form-row { display: flex; flex-direction: column; gap: 5px; margin-bottom: 11px; }
    .form-grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .form-inline { display: flex; flex-wrap: wrap; gap: 10px; align-items: end; margin-bottom: 14px; }
    label { font-size: 0.82rem; color: var(--muted); }
    input, select, textarea {
      background: var(--surface-2);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 8px 11px;
      font-size: 0.9rem;
      font-family: inherit;
    }
    input:focus, select:focus, textarea:focus { outline: none; border-color: var(--accent); }
    .checkbox-label {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      color: var(--text);
      font-size: 0.86rem;
      cursor: pointer;
    }

    table { width: 100%; border-collapse: collapse; font-size: 0.87rem; }
    th, td {
      text-align: left;
      padding: 10px 9px;
      border-bottom: 1px solid var(--border);
      vertical-align: middle;
    }
    th {
      color: var(--muted);
      font-weight: 600;
      font-size: 0.76rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    tbody tr:hover { background: rgba(255, 255, 255, 0.02); }
    .table-wrap { overflow-x: auto; }

    .badge {
      display: inline-block;
      padding: 3px 8px;
      border-radius: 999px;
      font-size: 0.74rem;
      font-weight: 600;
      white-space: nowrap;
    }
    .badge-green { background: var(--green-bg); color: #86efac; }
    .badge-amber { background: var(--amber-bg); color: #fcd34d; }
    .badge-red { background: var(--red-bg); color: #fca5a5; }
    .badge-purple { background: var(--purple-bg); color: #d8b4fe; }
    .badge-muted { background: var(--surface-2); color: var(--muted); }

    .deals-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(290px, 1fr));
      gap: 14px;
    }
    .deal-card {
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 14px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 10px;
    }
    .deal-meta { display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; }
    .deal-price { font-size: 1.25rem; font-weight: 700; }
    .deal-old { color: var(--muted); text-decoration: line-through; font-size: 0.85rem; margin-left: 6px; }
    .subtext { font-size: 0.78rem; color: var(--muted); margin-top: 2px; }

    .yaml-box {
      background: #080c13;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.8rem;
      white-space: pre;
      overflow-x: auto;
      color: #c9d7ee;
    }

    .toast {
      position: fixed;
      right: 20px;
      bottom: 20px;
      background: var(--surface-2);
      border: 1px solid var(--accent);
      color: var(--text);
      padding: 11px 16px;
      border-radius: 10px;
      font-size: 0.88rem;
      box-shadow: 0 10px 25px rgba(0, 0, 0, 0.45);
      display: none;
      z-index: 1000;
      max-width: 440px;
    }
    .toast.error { border-color: var(--red); color: #fecaca; }

    dialog.modal {
      background: var(--surface);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px;
      width: min(460px, 94vw);
    }
    dialog.modal-wide { width: min(760px, 95vw); }
    dialog.modal::backdrop { background: rgba(0, 0, 0, 0.7); }
    .modal-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 14px; }
    .empty-msg { color: var(--muted); padding: 14px 4px; font-size: 0.9rem; }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="title-wrap">
        <h1>PrisGeek Price Tracker</h1>
        <div class="webhook-badge" id="webhook-badge">Laddar konfiguration...</div>
      </div>
      <div class="header-actions">
        <button class="btn-secondary" id="test-notify-btn">Testa notis</button>
        <button class="btn-secondary" id="refresh-btn">Uppdatera vy</button>
        <button class="btn-primary" id="run-check-btn">Kör priskontroll nu</button>
      </div>
    </header>

    <section class="kpi-grid">
      <div class="kpi-card">
        <div class="kpi-label">Produktbevakningar</div>
        <div class="kpi-value" id="kpi-products">0</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Intressebevakningar</div>
        <div class="kpi-value" id="kpi-interests">0</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Fynd under målpris</div>
        <div class="kpi-value good" id="kpi-deals">0</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Loggade prispunkter</div>
        <div class="kpi-value" id="kpi-history">0</div>
      </div>
    </section>

    <nav class="tabs" role="tablist">
      <button class="tab-btn active" data-tab="watches">Bevakningar</button>
      <button class="tab-btn" data-tab="search">Sök på PrisGeek</button>
      <button class="tab-btn" data-tab="prisfall">Dagens Prisfall</button>
      <button class="tab-btn" data-tab="medlemsfynd">Medlemsfynd</button>
      <button class="tab-btn" data-tab="fyndvaror">Fyndvaror / Outlet</button>
      <button class="tab-btn" data-tab="basket">Inköpslista &amp; HA</button>
      <button class="tab-btn" data-tab="ai">AI-Rådgivare &amp; Digest</button>
    </nav>

    <section id="tab-watches" class="tab-panel active">
      <div class="forms-grid">
        <form class="card" id="product-watch-form">
          <h3>Ny produktbevakning</h3>
          <div class="form-row">
            <label for="pw-id">PrisGeek URL eller Produkt-ID</label>
            <input id="pw-id" required placeholder="t.ex. 183011 eller https://prisgeek.se/produkt/..." />
          </div>
          <div class="form-grid-2">
            <div class="form-row">
              <label for="pw-target">Målpris (kr)</label>
              <input id="pw-target" type="number" step="1" min="1" required placeholder="1500" />
            </div>
            <div class="form-row">
              <label for="pw-pct30">Smart Deal (% under 30d-lägsta)</label>
              <input id="pw-pct30" type="number" step="1" min="0" max="90" value="0" />
            </div>
          </div>
          <div class="form-row">
            <label for="pw-ai-req">AI-kravspec (valfritt)</label>
            <input id="pw-ai-req" placeholder="t.ex. Tyst kylare, minst 10 års garanti" />
          </div>
          <div class="form-row">
            <label for="pw-notes">Anteckning (valfritt)</label>
            <input id="pw-notes" placeholder="t.ex. Black Friday till Jetson" />
          </div>
          <div class="form-row">
            <label class="checkbox-label">
              <input type="checkbox" id="pw-alltime" checked /> Larma även vid rekordlågt pris (All-Time Low)
            </label>
          </div>
          <button class="btn-primary" type="submit">Lägg till produktbevakning</button>
        </form>

        <form class="card" id="interest-watch-form">
          <h3>Ny intressebevakning (Nyckelord)</h3>
          <div class="form-row">
            <label for="iw-name">Namn på bevakning</label>
            <input id="iw-name" required placeholder="t.ex. 1TB MicroSD" />
          </div>
          <div class="form-grid-2">
            <div class="form-row">
              <label for="iw-query">Sökfråga på PrisGeek</label>
              <input id="iw-query" required placeholder="t.ex. microSD 1TB" />
            </div>
            <div class="form-row">
              <label for="iw-target">Maxpris (kr)</label>
              <input id="iw-target" type="number" step="1" min="1" required placeholder="1600" />
            </div>
          </div>
          <div class="form-grid-2">
            <div class="form-row">
              <label for="iw-keywords">Kräv nyckelord (komma)</label>
              <input id="iw-keywords" placeholder="t.ex. 1TB, V30" />
            </div>
            <div class="form-row">
              <label for="iw-exclude">Exkludera (komma)</label>
              <input id="iw-exclude" placeholder="t.ex. demo, A1" />
            </div>
          </div>
          <div class="form-row">
            <label for="iw-ai-req">AI-kravspec (valfritt)</label>
            <input id="iw-ai-req" placeholder="t.ex. Endast A2/V30 med bra kylning, inga budgetfällor" />
          </div>
          <div class="form-row">
            <label class="checkbox-label">
              <input type="checkbox" id="iw-outlet" /> Inkludera fyndvaror / outlet
            </label>
          </div>
          <button class="btn-primary" type="submit">Lägg till intressebevakning</button>
        </form>
      </div>

      <div class="card">
        <h2>Aktiva produktbevakningar</h2>
        <div class="table-wrap" id="product-watches-container"></div>
      </div>

      <div class="card">
        <h2>Aktiva intressebevakningar</h2>
        <div class="table-wrap" id="interest-watches-container"></div>
      </div>
    </section>

    <section id="tab-search" class="tab-panel">
      <div class="card">
        <h2>Sök direkt på PrisGeek</h2>
        <form class="form-inline" id="search-form">
          <div class="form-row" style="flex: 2; min-width: 220px; margin: 0;">
            <label for="search-q">Sökord (t.ex. 5060 ti 16gb eller 16 gb)</label>
            <input id="search-q" required placeholder="t.ex. microSD 1TB, 5060 ti 16gb, Noctua" />
          </div>
          <div class="form-row" style="width: 140px; margin: 0;">
            <label for="search-max">Maxpris (kr)</label>
            <input id="search-max" type="number" min="1" placeholder="Valfritt" />
          </div>
          <label class="checkbox-label" style="padding-bottom: 8px;">
            <input type="checkbox" id="search-stock" checked /> Endast i lager
          </label>
          <button class="btn-primary" type="submit">Sök</button>
        </form>
        <div class="table-wrap" id="search-results-container">
          <div class="empty-msg">Skriv ett sökord och klicka på Sök för att hämta live-priser.</div>
        </div>
      </div>
    </section>

    <section id="tab-prisfall" class="tab-panel">
      <div class="card">
        <h2>Dagens Prisfall</h2>
        <form class="form-inline" id="prisfall-form">
          <div class="form-row" style="width: 150px; margin: 0;">
            <label for="pf-period">Period</label>
            <select id="pf-period">
              <option value="1">24 timmar</option>
              <option value="3">3 dagar</option>
              <option value="7" selected>7 dagar</option>
              <option value="30">30 dagar</option>
            </select>
          </div>
          <div class="form-row" style="width: 150px; margin: 0;">
            <label for="pf-min">Minsta prisfall</label>
            <select id="pf-min">
              <option value="10">Minst 10%</option>
              <option value="15" selected>Minst 15%</option>
              <option value="25">Minst 25%</option>
              <option value="35">Minst 35%</option>
            </select>
          </div>
          <button class="btn-primary" type="submit">Uppdatera</button>
        </form>
        <div id="prisfall-container">
          <div class="empty-msg">Klicka på fliken eller Uppdatera för att hämta prisfall...</div>
        </div>
      </div>
    </section>

    <section id="tab-medlemsfynd" class="tab-panel">
      <div class="card">
        <h2>SweClockers Medlemsfynd</h2>
        <form class="form-inline" id="medlemsfynd-form">
          <div class="form-row" style="width: 200px; margin: 0;">
            <label for="mf-cat">Kategori (valfritt)</label>
            <input id="mf-cat" placeholder="t.ex. Grafikkort, Ljud" />
          </div>
          <div class="form-row" style="width: 140px; margin: 0;">
            <label for="mf-score">Minsta röster</label>
            <input id="mf-score" type="number" value="0" />
          </div>
          <button class="btn-primary" type="submit">Uppdatera</button>
        </form>
        <div class="table-wrap" id="medlemsfynd-container">
          <div class="empty-msg">Hämtar medlemsfynd...</div>
        </div>
      </div>
    </section>

    <section id="tab-fyndvaror" class="tab-panel">
      <div class="card">
        <h2>Fyndvaror / Outlet &amp; Demo</h2>
        <form class="form-inline" id="fyndvaror-form">
          <div class="form-row" style="flex: 2; min-width: 200px; margin: 0;">
            <label for="fv-query">Filtrera på sökord (valfritt)</label>
            <input id="fv-query" placeholder="t.ex. RTX, SSD, Samsung" />
          </div>
          <div class="form-row" style="width: 150px; margin: 0;">
            <label for="fv-min">Minsta rabatt %</label>
            <input id="fv-min" type="number" min="0" max="90" value="5" />
          </div>
          <button class="btn-primary" type="submit">Sök / Uppdatera</button>
        </form>
        <div class="table-wrap" id="fyndvaror-container">
          <div class="empty-msg">Hämtar fyndvaror...</div>
        </div>
      </div>
    </section>

    <section id="tab-basket" class="tab-panel">
      <div class="card">
        <div class="deal-meta" style="margin-bottom: 12px;">
          <h2 style="margin: 0;">Inköpslista-optimerare (inkl. frakt)</h2>
          <button class="btn-primary btn-sm" id="basket-refresh-btn">Beräkna bästa kombination</button>
        </div>
        <div id="basket-container">
          <div class="empty-msg">Beräknar bästa butikskombination för dina bevakade produkter...</div>
        </div>
      </div>

      <div class="card">
        <div class="deal-meta" style="margin-bottom: 10px;">
          <h2 style="margin: 0;">Home Assistant Lovelace YAML</h2>
          <button class="btn-secondary btn-sm" id="copy-yaml-btn">Kopiera YAML</button>
        </div>
        <div class="yaml-box" id="ha-yaml-box"></div>
      </div>

      <div class="card">
        <h2>Utlösta Larm</h2>
        <div class="table-wrap" id="alerts-container"></div>
      </div>

      <div class="card">
        <h2>Prishistorik (Senaste 50)</h2>
        <div class="table-wrap" id="history-container"></div>
      </div>
    </section>

    <section id="tab-ai" class="tab-panel">
      <div class="card">
        <h2>Min AI-intresseprofil &amp; Black Week Digest</h2>
        <div class="form-row">
          <label for="ai-profile-input">Beskriv vad du är intresserad av (sparas automatiskt)</label>
          <textarea id="ai-profile-input" rows="3" placeholder="t.ex. RTX 5060 Ti/5070, snabb microSD A2/V30 till Jetson, 3D-skrivare/filament, Zigbee/Matter..."></textarea>
        </div>
        <div class="form-inline" style="margin-bottom: 12px;">
          <label class="checkbox-label">
            <input type="checkbox" id="ai-digest-notify" /> Skicka sammanfattning till iPhone
          </label>
          <button class="btn-primary" id="ai-digest-btn" type="button">Generera Topp 5 AI-fynd</button>
        </div>
        <div id="ai-digest-container">
          <div class="empty-msg">Klicka "Generera Topp 5 AI-fynd" för att låta Home Assistant AI välja dagens bästa teknikfynd.</div>
        </div>
      </div>

      <div class="card">
        <div class="deal-meta" style="margin-bottom: 10px;">
          <h2 style="margin: 0;">Kors-alternativ &amp; Uppgraderingar (nära dina bevakningar)</h2>
          <button class="btn-secondary btn-sm" id="ai-alt-btn" type="button">Sök bättre alternativ</button>
        </div>
        <div id="ai-alt-container">
          <div class="empty-msg">Klicka "Sök bättre alternativ" för att skanna prisfall och medlemsfynd efter högre-tier-uppgraderingar i liknande prisklass.</div>
        </div>
      </div>
    </section>
  </div>

  <dialog class="modal" id="watch-modal">
    <form method="dialog" id="watch-modal-form">
      <h3 id="modal-title" style="margin-top: 0;">Lägg till bevakning</h3>
      <p id="modal-subtitle" style="font-size: 0.88rem; color: var(--muted);"></p>
      <div class="form-row">
        <label for="modal-target">Målpris (kr)</label>
        <input id="modal-target" type="number" step="1" min="1" required />
      </div>
      <div class="modal-actions">
        <button type="button" class="btn-secondary" id="modal-cancel">Avbryt</button>
        <button type="submit" class="btn-primary">Spara bevakning</button>
      </div>
    </form>
  </dialog>

  <dialog class="modal" id="edit-product-modal">
    <form method="dialog" id="edit-product-form">
      <h3 style="margin-top: 0;" id="ep-title">Redigera produktbevakning</h3>
      <div class="form-grid-2">
        <div class="form-row">
          <label for="ep-target">Målpris (kr)</label>
          <input id="ep-target" type="number" step="1" min="1" required />
        </div>
        <div class="form-row">
          <label for="ep-pct30">% under 30d-lägsta</label>
          <input id="ep-pct30" type="number" step="1" min="0" max="90" required />
        </div>
      </div>
      <div class="form-row">
        <label for="ep-ai-req">AI-kravspec (valfritt)</label>
        <input id="ep-ai-req" placeholder="t.ex. Minst A2/V30 eller tyst 3-fläktkylare" />
      </div>
      <div class="form-row">
        <label for="ep-notes">Anteckning</label>
        <input id="ep-notes" />
      </div>
      <div class="form-row">
        <label class="checkbox-label"><input type="checkbox" id="ep-stock" /> Endast i lager</label>
        <label class="checkbox-label"><input type="checkbox" id="ep-alltime" /> Larma vid rekordlågt pris</label>
      </div>
      <div class="modal-actions">
        <button type="button" class="btn-secondary" onclick="document.getElementById('edit-product-modal').close()">Avbryt</button>
        <button type="submit" class="btn-primary">Spara ändringar</button>
      </div>
    </form>
  </dialog>

  <dialog class="modal" id="edit-interest-modal">
    <form method="dialog" id="edit-interest-form">
      <h3 style="margin-top: 0;">Redigera intressebevakning</h3>
      <div class="form-row">
        <label for="ei-name">Namn</label>
        <input id="ei-name" required />
      </div>
      <div class="form-grid-2">
        <div class="form-row">
          <label for="ei-query">Sökfråga</label>
          <input id="ei-query" required />
        </div>
        <div class="form-row">
          <label for="ei-target">Maxpris (kr)</label>
          <input id="ei-target" type="number" step="1" min="1" required />
        </div>
      </div>
      <div class="form-row">
        <label for="ei-must">Krävda nyckelord (komma)</label>
        <input id="ei-must" />
      </div>
      <div class="form-row">
        <label for="ei-excl">Exkluderade nyckelord (komma)</label>
        <input id="ei-excl" />
      </div>
      <div class="form-row">
        <label for="ei-ai-req">AI-kravspec (valfritt)</label>
        <input id="ei-ai-req" placeholder="t.ex. Endast A2/V30, undvik budgetmodeller" />
      </div>
      <div class="form-row">
        <label class="checkbox-label"><input type="checkbox" id="ei-stock" /> Endast i lager</label>
        <label class="checkbox-label"><input type="checkbox" id="ei-outlet" /> Inkludera fyndvaror / outlet</label>
      </div>
      <div class="modal-actions">
        <button type="button" class="btn-secondary" onclick="document.getElementById('edit-interest-modal').close()">Avbryt</button>
        <button type="submit" class="btn-primary">Spara ändringar</button>
      </div>
    </form>
  </dialog>

  <dialog class="modal modal-wide" id="detail-modal">
    <div class="deal-meta" style="margin-bottom: 10px;">
      <h3 id="detail-title" style="margin: 0;">Produktdetaljer</h3>
      <button type="button" class="btn-secondary btn-sm" onclick="document.getElementById('detail-modal').close()">Stäng</button>
    </div>
    <div id="detail-body"></div>
  </dialog>

  <dialog class="modal modal-wide" id="top-matches-modal">
    <div class="deal-meta" style="margin-bottom: 10px;">
      <h3 id="tm-title" style="margin: 0;">Topp 5 matchningar</h3>
      <button type="button" class="btn-secondary btn-sm" onclick="document.getElementById('top-matches-modal').close()">Stäng</button>
    </div>
    <div id="tm-body"></div>
  </dialog>

  <div class="toast" id="toast"></div>

  <script>
    const API_BASE = window.location.pathname.replace(/\\/+$/, "");
    const state = {
      watches: null,
      history: [],
      config: null,
      prisfallLoaded: false,
      medlemsfyndLoaded: false,
      fyndvarorLoaded: false,
      basketLoaded: false,
      searchResults: [],
      prisfallDeals: [],
      medlemsfyndDeals: [],
      fyndvarorItems: [],
      topMatches: [],
    };
    let pendingModalPid = null;
    let editingProductId = null;
    let editingInterestId = null;

    function esc(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
    }

    function fmtKr(val) {
      if (val === null || val === undefined || Number.isNaN(Number(val))) return "–";
      return `${Math.round(Number(val)).toLocaleString("sv-SE")} kr`;
    }

    function fmtDate(iso) {
      if (!iso) return "–";
      const d = new Date(iso);
      return Number.isNaN(d.getTime()) ? esc(iso) : d.toLocaleString("sv-SE");
    }

    function showToast(msg, isError = false) {
      const el = document.getElementById("toast");
      el.textContent = msg;
      el.className = isError ? "toast error" : "toast";
      el.style.display = "block";
      clearTimeout(el._timer);
      el._timer = setTimeout(() => { el.style.display = "none"; }, 4200);
    }

    async function apiFetch(url, options = {}) {
      const resolvedUrl = url.startsWith("/") ? `${API_BASE}${url}` : url;
      const resp = await fetch(resolvedUrl, {
        headers: { "Content-Type": "application/json", ...(options.headers || {}) },
        ...options,
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        throw new Error(data.error || `HTTP ${resp.status}`);
      }
      return data;
    }

    function renderHistoryChart(series, targetPrice) {
      if (!series || series.length < 2) {
        return `<div class="empty-msg">Otillräcklig historik från PrisGeek för graf.</div>`;
      }
      const w = 680, h = 190, padL = 56, padR = 16, padT = 16, padB = 28;
      const prices = series.map(p => Number(p.price));
      if (targetPrice) prices.push(Number(targetPrice));
      let minP = Math.min(...prices), maxP = Math.max(...prices);
      if (maxP === minP) { minP -= 50; maxP += 50; }
      const range = maxP - minP;
      const plotW = w - padL - padR, plotH = h - padT - padB;
      const xFor = i => padL + (i / (series.length - 1)) * plotW;
      const yFor = p => padT + (1 - (p - minP) / range) * plotH;
      const pts = series.map((pt, i) => `${xFor(i).toFixed(1)},${yFor(Number(pt.price)).toFixed(1)}`).join(" ");
      const targetLine = targetPrice
        ? `<line x1="${padL}" y1="${yFor(Number(targetPrice)).toFixed(1)}" x2="${w - padR}" y2="${yFor(Number(targetPrice)).toFixed(1)}" stroke="#22c55e" stroke-dasharray="4 4" stroke-width="1.5" />`
        : "";
      return `
        <svg viewBox="0 0 ${w} ${h}" style="width:100%; height:auto; background:#0b0f17; border:1px solid var(--border); border-radius:8px;">
          <line x1="${padL}" y1="${padT}" x2="${w - padR}" y2="${padT}" stroke="#1f2a3d" />
          <line x1="${padL}" y1="${h - padB}" x2="${w - padR}" y2="${h - padB}" stroke="#1f2a3d" />
          <text x="6" y="${padT + 10}" fill="#92a4c0" font-size="11">${Math.round(maxP)} kr</text>
          <text x="6" y="${h - padB}" fill="#92a4c0" font-size="11">${Math.round(minP)} kr</text>
          <text x="${padL}" y="${h - 8}" fill="#92a4c0" font-size="10">${esc(series[0].day)}</text>
          <text x="${w - padR - 64}" y="${h - 8}" fill="#92a4c0" font-size="10">${esc(series[series.length - 1].day)}</text>
          ${targetLine}
          <polyline fill="none" stroke="#38bdf8" stroke-width="2.2" points="${pts}" />
        </svg>`;
    }

    function renderAiVerdictHtml(v) {
      if (!v || !v.verdict_badge) return "";
      const score = Number(v.score || 0);
      const cls = score >= 8 ? "badge-green" : score >= 5 ? "badge-amber" : "badge-red";
      return `
        <div style="padding:12px; border-radius:10px; background:var(--surface-2); border:1px solid var(--border); margin-bottom:14px;">
          <div class="deal-meta" style="margin-bottom:6px;">
            <span class="badge ${cls}">AI-Betyg: ${score}/10 · ${esc(v.verdict_badge)}</span>
            <span class="subtext">${v.analyzed_at ? fmtDate(v.analyzed_at) : ""}</span>
          </div>
          <div style="font-size:0.88rem; margin-bottom:4px;"><strong>Pris &amp; Köpråd:</strong> ${esc(v.buy_or_wait)}</div>
          <div style="font-size:0.88rem;"><strong>Spec &amp; Krav:</strong> ${esc(v.spec_check)}</div>
        </div>`;
    }

    async function runAiAnalyzeProduct(publicId, targetPrice = null, watchId = null) {
      const box = document.getElementById("detail-ai-box");
      if (!box) return;
      const w = watchId ? (state.watches?.product_watches || []).find(x => Number(x.id) === Number(watchId)) : null;
      box.innerHTML = `<div class="empty-msg">Kör AI-analys via Home Assistant (kan ta några sekunder)...</div>`;
      try {
        const res = await apiFetch("/api/ai/analyze-product", {
          method: "POST",
          body: JSON.stringify({
            public_id: Number(publicId),
            target_price: targetPrice,
            ai_requirements: w?.ai_requirements || "",
            watch_id: watchId,
          }),
        });
        box.innerHTML = renderAiVerdictHtml(res);
        if (watchId) await loadState();
      } catch (err) {
        box.innerHTML = `<div class="empty-msg">AI-analys misslyckades: ${esc(err.message)}</div>`;
      }
    }

    async function vetInterestWatch(watchId) {
      showToast("Kör AI-kravgranskning av matchningar...");
      try {
        const res = await apiFetch(`/api/ai/vet-interest/${Number(watchId)}`, { method: "POST", body: "{}" });
        showToast(res.overall_advice || "AI-granskning klar!");
        await loadState();
        openTopMatchesModal(watchId);
      } catch (err) {
        showToast(`AI-granskning misslyckades: ${err.message}`, true);
      }
    }

    async function openProductDetailModal(publicId, targetPrice = null, watchId = null, autoAi = false) {
      const modal = document.getElementById("detail-modal");
      const body = document.getElementById("detail-body");
      document.getElementById("detail-title").textContent = "Hämtar historik & butiker...";
      body.innerHTML = `<div class="empty-msg">Laddar produktdata...</div>`;
      modal.showModal();
      try {
        const det = await apiFetch(`/api/product/${Number(publicId)}`);
        document.getElementById("detail-title").textContent = det.name;
        const chartSvg = renderHistoryChart(det.history_series || [], targetPrice);
        const offers = det.offers || [];
        const reviews = det.extra_context?.reviews || [];
        const w = watchId ? (state.watches?.product_watches || []).find(x => Number(x.id) === Number(watchId)) : null;
        const cachedVerdict = w?.ai_verdict?.verdict_badge ? renderAiVerdictHtml(w.ai_verdict) : "";

        body.innerHTML = `
          <div style="display:flex; gap:12px; flex-wrap:wrap; align-items:center; margin-bottom:12px; font-size:0.86rem;">
            <span>Nuvarande: <strong>${fmtKr(det.lowest_price)}</strong></span>
            <span>30d-lägsta: <strong>${fmtKr(det.lowest_30d_price)}</strong></span>
            <span>Lägsta någonsin: <strong>${fmtKr(det.lowest_all_time_price)}</strong></span>
            <span class="badge badge-muted">${esc(det.deal_assessment)}</span>
            <a href="${esc(det.url)}" target="_blank" rel="noopener">Öppna på PrisGeek ↗</a>
            <button class="btn-primary btn-sm" type="button" onclick="runAiAnalyzeProduct(${Number(publicId)}, ${targetPrice !== null ? Number(targetPrice) : "null"}, ${watchId !== null ? Number(watchId) : "null"})">Kör AI-analys</button>
          </div>
          ${reviews.length ? `<div style="margin-bottom:10px;">${reviews.map(r => `<span class="badge badge-purple" style="margin-right:6px;">SweClockers: ${esc(r.award_label || r.title)}</span>`).join("")}</div>` : ""}
          <div id="detail-ai-box">${cachedVerdict}</div>
          <div style="margin-bottom:14px;">${chartSvg}</div>
          <h4 style="margin:10px 0 6px;">Butikserbjudanden (${offers.length})</h4>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Butik</th><th>Pris</th><th>Frakt</th><th>Totalt</th><th>Status</th></tr></thead>
              <tbody>
                ${offers.map(o => `
                  <tr>
                    <td>${esc(o.retailer_name)}</td>
                    <td><strong>${fmtKr(o.price)}</strong></td>
                    <td>${fmtKr(o.shipping)}</td>
                    <td>${fmtKr(o.total_price)}</td>
                    <td>${o.in_stock ? `<span class="badge badge-green">I lager</span>` : `<span class="badge badge-red">Slut</span>`}</td>
                  </tr>`).join("")}
              </tbody>
            </table>
          </div>`;
        if (autoAi) {
          runAiAnalyzeProduct(publicId, targetPrice, watchId);
        }
      } catch (err) {
        body.innerHTML = `<div class="empty-msg">Fel: ${esc(err.message)}</div>`;
      }
    }

    function openTopMatchesModal(watchId) {
      const qw = (state.watches?.query_watches || []).find(q => Number(q.id) === Number(watchId));
      if (!qw) return;
      document.getElementById("tm-title").textContent = `Topp 5 matchningar: ${qw.name}`;
      const matches = qw.top_matches || [];
      const aiSummary = qw.ai_summary || {};
      const vettedMap = {};
      for (const v of (aiSummary.vetted || [])) {
        vettedMap[Number(v.public_id)] = v;
      }
      state.topMatches = matches;
      const body = document.getElementById("tm-body");
      if (!matches.length) {
        body.innerHTML = `<div class="empty-msg">Inga matchningar loggade ännu. Klicka "Kör priskontroll nu".</div>`;
      } else {
        const adviceHtml = aiSummary.overall_advice
          ? `<div style="margin-bottom:12px; padding:10px; border-radius:8px; background:var(--surface-2); font-size:0.86rem;"><strong>AI-råd:</strong> ${esc(aiSummary.overall_advice)}</div>`
          : "";
        body.innerHTML = `${adviceHtml}
          <table>
            <thead><tr><th>Produkt</th><th>Pris</th><th>Butik</th><th>AI-bedömning</th><th></th></tr></thead>
            <tbody>
              ${matches.map((m, idx) => {
                const v = vettedMap[Number(m.public_id)];
                const aiScore = m.ai_score ?? v?.score;
                const aiApproved = m.ai_approved ?? v?.approved;
                const aiNote = m.ai_note ?? v?.note;
                const aiCell = aiScore !== undefined
                  ? `<span class="badge ${aiApproved === false ? "badge-red" : "badge-green"}">${Number(aiScore)}/10</span><div class="subtext">${esc(aiNote || "")}</div>`
                  : `<span class="subtext">–</span>`;
                return `
                <tr>
                  <td><a href="${esc(m.url)}" target="_blank" rel="noopener">${esc(m.name)}</a></td>
                  <td><strong>${fmtKr(m.lowest_price)}</strong><div class="subtext">${fmtKr(m.lowest_total_price)} inkl.</div></td>
                  <td>${esc(m.best_retailer || "–")}</td>
                  <td>${aiCell}</td>
                  <td class="actions-cell">
                    <button class="btn-secondary btn-sm" onclick="openProductDetailModal(${Number(m.public_id)}, ${Number(qw.target_price)})">Graf</button>
                    <button class="btn-primary btn-sm" onclick="openWatchFromList('topMatches', ${idx})">Bevaka</button>
                  </td>
                </tr>`;
              }).join("")}
            </tbody>
          </table>`;
      }
      document.getElementById("top-matches-modal").showModal();
    }

    function openEditProductModal(watchId) {
      const w = (state.watches?.product_watches || []).find(item => Number(item.id) === Number(watchId));
      if (!w) return;
      editingProductId = w.id;
      document.getElementById("ep-title").textContent = `Redigera: ${w.product_name}`;
      document.getElementById("ep-target").value = String(w.target_price);
      document.getElementById("ep-pct30").value = String(w.alert_below_30d_pct || 0);
      document.getElementById("ep-ai-req").value = w.ai_requirements || "";
      document.getElementById("ep-notes").value = w.notes || "";
      document.getElementById("ep-stock").checked = Boolean(w.in_stock_only);
      document.getElementById("ep-alltime").checked = w.alert_on_all_time_low !== 0;
      document.getElementById("edit-product-modal").showModal();
    }

    function openEditInterestModal(watchId) {
      const q = (state.watches?.query_watches || []).find(item => Number(item.id) === Number(watchId));
      if (!q) return;
      editingInterestId = q.id;
      document.getElementById("ei-name").value = q.name;
      document.getElementById("ei-query").value = q.search_query;
      document.getElementById("ei-target").value = String(q.target_price);
      document.getElementById("ei-must").value = (q.must_include_keywords || []).join(", ");
      document.getElementById("ei-excl").value = (q.exclude_keywords || []).join(", ");
      document.getElementById("ei-ai-req").value = q.ai_requirements || "";
      document.getElementById("ei-stock").checked = Boolean(q.in_stock_only);
      document.getElementById("ei-outlet").checked = Boolean(q.include_outlet);
      document.getElementById("edit-interest-modal").showModal();
    }

    function renderAiDigest(digest) {
      const container = document.getElementById("ai-digest-container");
      if (!digest || !digest.picks || !digest.picks.length) return;
      state.aiPicks = digest.picks;
      container.innerHTML = `
        <div style="margin-bottom:12px; font-size:0.92rem;"><strong>${esc(digest.headline)}</strong> <span class="subtext">(${fmtDate(digest.generated_at)})</span></div>
        <div class="deals-grid">
          ${digest.picks.map((p, idx) => `
            <div class="deal-card">
              <div>
                <div class="deal-meta">
                  <span class="badge badge-green">AI ${Number(p.score || 8)}/10</span>
                  <span class="badge badge-purple">${esc(p.discount_info || "Fynd")}</span>
                </div>
                <div style="margin-top:8px; font-weight:600;">
                  <a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.title)}</a>
                </div>
                <div class="subtext">${esc(p.retailer || "")}</div>
                <div style="font-size:0.84rem; margin-top:6px;">${esc(p.reason || "")}</div>
              </div>
              <div class="deal-meta">
                <span class="deal-price">${fmtKr(p.price)}</span>
                ${p.public_id ? `<button class="btn-primary btn-sm" onclick="openWatchFromList('aiPicks', ${idx})">Bevaka</button>` : ""}
              </div>
            </div>`).join("")}
        </div>`;
    }

    function renderAiAlternatives(alts) {
      const container = document.getElementById("ai-alt-container");
      if (!alts) return;
      const items = alts.alternatives || [];
      state.aiAlternatives = items;
      if (!items.length) {
        container.innerHTML = `<div class="empty-msg">${esc(alts.summary || "Inga bättre kors-alternativ hittades just nu.")}</div>`;
        return;
      }
      container.innerHTML = `
        <div style="margin-bottom:10px; font-size:0.88rem;">${esc(alts.summary || "")}</div>
        <table>
          <thead><tr><th>Alternativ</th><th>Pris</th><th>Butik</th><th>Ersätter / För</th><th>Motivering</th><th></th></tr></thead>
          <tbody>
            ${items.map((a, idx) => `
              <tr>
                <td><a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.title)}</a></td>
                <td><strong>${fmtKr(a.price)}</strong></td>
                <td>${esc(a.retailer || "–")}</td>
                <td>${esc(a.for_watch || "–")}</td>
                <td style="font-size:0.84rem;">${esc(a.reason || "")}</td>
                <td>${a.public_id ? `<button class="btn-primary btn-sm" onclick="openWatchFromList('aiAlternatives', ${idx})">Bevaka</button>` : ""}</td>
              </tr>`).join("")}
          </tbody>
        </table>`;
    }

    function buildLovelaceYaml(pw, qw) {
      const lines = [
        "type: entities",
        "title: PrisGeek Prisbevakning",
        "entities:",
        "  - entity: sensor.prisgeek_tracker_summary",
        "    name: Fynd under målpris",
      ];
      for (const w of pw) {
        lines.push(`  - entity: sensor.prisgeek_${w.public_id}`);
        lines.push(`    name: ${JSON.stringify(w.product_name)}`);
      }
      for (const q of qw) {
        lines.push(`  - entity: sensor.prisgeek_interest_${q.id}`);
        lines.push(`    name: ${JSON.stringify("Intresse: " + q.name)}`);
      }
      return lines.join("\\n");
    }

    function renderState() {
      const pw = state.watches?.product_watches || [];
      const qw = state.watches?.query_watches || [];
      const alerts = state.watches?.recent_alerts || [];
      const hist = state.history || [];

      const dealsCount =
        pw.filter(w => w.last_price !== null && Number(w.last_price) <= Number(w.target_price)).length +
        qw.filter(q => q.best_match_price !== null && Number(q.best_match_price) <= Number(q.target_price)).length;
      document.getElementById("kpi-products").textContent = String(pw.length);
      document.getElementById("kpi-interests").textContent = String(qw.length);
      document.getElementById("kpi-deals").textContent = String(dealsCount);
      document.getElementById("kpi-history").textContent = String(hist.length);

      const cfg = state.config || {};
      const hookStatus = cfg.use_ha_webhook === false ? "Direkt via HA Supervisor" : (cfg.ha_webhook_url || "Aktiverad");
      const notifySvc = cfg.notify_service || "auto (mobile_app)";
      document.getElementById("webhook-badge").textContent = `Notis: ${notifySvc} | ${hookStatus}`;
      document.getElementById("ha-yaml-box").textContent = buildLovelaceYaml(pw, qw);

      const profileEl = document.getElementById("ai-profile-input");
      if (profileEl && !profileEl.value && state.aiProfile) {
        profileEl.value = state.aiProfile;
      }
      if (state.lastDigest) renderAiDigest(state.lastDigest);
      if (state.lastAlternatives) renderAiAlternatives(state.lastAlternatives);

      const pwContainer = document.getElementById("product-watches-container");
      if (!pw.length) {
        pwContainer.innerHTML = `<div class="empty-msg">Inga aktiva produktbevakningar ännu.</div>`;
      } else {
        pwContainer.innerHTML = `
          <table>
            <thead>
              <tr>
                <th>Produkt</th>
                <th>Nuvarande</th>
                <th>Butik</th>
                <th>Målpris &amp; Regler</th>
                <th>30d / All-Time</th>
                <th>Status</th>
                <th>Senast kollad</th>
                <th>Åtgärder</th>
              </tr>
            </thead>
            <tbody>
              ${pw.map(w => {
                const cur = w.last_price;
                const target = w.target_price;
                let badge = `<span class="badge badge-muted">Ej kollad</span>`;
                if (cur !== null && cur !== undefined) {
                  const diff = Number(cur) - Number(target);
                  badge = diff <= 0
                    ? `<span class="badge badge-green">Under målpris!</span>`
                    : `<span class="badge badge-amber">+${Math.round(diff)} kr över</span>`;
                }
                const rules = [];
                if (w.alert_on_all_time_low !== 0) rules.push("All-time");
                if (Number(w.alert_below_30d_pct) > 0) rules.push(`-${Number(w.alert_below_30d_pct)}% vs 30d`);
                const aiBadge = w.ai_verdict?.verdict_badge
                  ? `<div class="subtext"><span class="badge badge-purple">${Number(w.ai_verdict.score || 0)}/10 ${esc(w.ai_verdict.verdict_badge)}</span></div>`
                  : "";
                return `
                  <tr>
                    <td>
                      <a href="${esc(w.product_url)}" target="_blank" rel="noopener">${esc(w.product_name)}</a>
                      ${w.notes ? `<div class="subtext">${esc(w.notes)}</div>` : ""}
                      ${w.ai_requirements ? `<div class="subtext">AI: ${esc(w.ai_requirements)}</div>` : ""}
                    </td>
                    <td><strong>${fmtKr(cur)}</strong></td>
                    <td>${esc(w.last_retailer || "–")}</td>
                    <td>
                      <strong>${fmtKr(target)}</strong>
                      ${rules.length ? `<div class="subtext">${esc(rules.join(" · "))}</div>` : ""}
                    </td>
                    <td>${fmtKr(w.lowest_30d_price)} / ${fmtKr(w.lowest_all_time_price)}</td>
                    <td>${badge}${aiBadge}</td>
                    <td>${fmtDate(w.last_checked_at)}</td>
                    <td class="actions-cell">
                      <button class="btn-secondary btn-sm" onclick="openProductDetailModal(${Number(w.public_id)}, ${Number(w.target_price)}, ${Number(w.id)}, false)">Graf &amp; Butiker</button>
                      <button class="btn-secondary btn-sm" onclick="openProductDetailModal(${Number(w.public_id)}, ${Number(w.target_price)}, ${Number(w.id)}, true)">AI-analys</button>
                      <button class="btn-secondary btn-sm" onclick="openEditProductModal(${Number(w.id)})">Redigera</button>
                      <button class="btn-danger" onclick="deleteWatch('product', ${Number(w.id)})">Ta bort</button>
                    </td>
                  </tr>`;
              }).join("")}
            </tbody>
          </table>`;
      }

      const qwContainer = document.getElementById("interest-watches-container");
      if (!qw.length) {
        qwContainer.innerHTML = `<div class="empty-msg">Inga aktiva intressebevakningar ännu.</div>`;
      } else {
        qwContainer.innerHTML = `
          <table>
            <thead>
              <tr>
                <th>Namn &amp; Sökfråga</th>
                <th>Nyckelord</th>
                <th>Maxpris</th>
                <th>Bästa träff nu</th>
                <th>Senast kollad</th>
                <th>Åtgärder</th>
              </tr>
            </thead>
            <tbody>
              ${qw.map(q => {
                let matchHtml = `<span class="badge badge-muted">Ej kontrollerad</span>`;
                if (q.best_match_price !== null && q.best_match_price !== undefined) {
                  const diff = Number(q.best_match_price) - Number(q.target_price);
                  const diffBadge = diff <= 0
                    ? `<span class="badge badge-green">Under maxpris!</span>`
                    : `<span class="badge badge-amber">+${Math.round(diff)} kr över</span>`;
                  matchHtml = `
                    <div><a href="${esc(q.best_match_url)}" target="_blank" rel="noopener">${esc(q.best_match_name)}</a></div>
                    <div class="subtext"><strong>${fmtKr(q.best_match_price)}</strong> @ ${esc(q.best_match_retailer || "–")} · ${diffBadge} <span class="badge badge-muted">${Number(q.match_count || 0)} träffar</span></div>`;
                } else if (q.last_checked_at) {
                  matchHtml = `<span class="badge badge-red">0 träffar</span>`;
                }
                return `
                  <tr>
                    <td>
                      <strong>${esc(q.name)}</strong>
                      <div class="subtext">Sök: ${esc(q.search_query)}${q.include_outlet ? " · Inkl. outlet" : ""}</div>
                      ${q.ai_requirements ? `<div class="subtext">AI: ${esc(q.ai_requirements)}</div>` : ""}
                    </td>
                    <td>
                      <div>+${esc((q.must_include_keywords || []).join(", ") || "–")}</div>
                      ${(q.exclude_keywords || []).length ? `<div class="subtext">-${esc(q.exclude_keywords.join(", "))}</div>` : ""}
                    </td>
                    <td><strong>${fmtKr(q.target_price)}</strong></td>
                    <td>${matchHtml}</td>
                    <td>${fmtDate(q.last_checked_at)}</td>
                    <td class="actions-cell">
                      <button class="btn-secondary btn-sm" onclick="openTopMatchesModal(${Number(q.id)})">Topp 5</button>
                      <button class="btn-secondary btn-sm" onclick="vetInterestWatch(${Number(q.id)})">AI-granska</button>
                      <button class="btn-secondary btn-sm" onclick="openEditInterestModal(${Number(q.id)})">Redigera</button>
                      <button class="btn-danger" onclick="deleteWatch('query', ${Number(q.id)})">Ta bort</button>
                    </td>
                  </tr>`;
              }).join("")}
            </tbody>
          </table>`;
      }

      const alertsContainer = document.getElementById("alerts-container");
      alertsContainer.innerHTML = !alerts.length
        ? `<div class="empty-msg">Inga utlösta larm loggade ännu.</div>`
        : `<table><thead><tr><th>Tid</th><th>Typ</th><th>Produkt</th><th>Pris</th><th>Butik</th></tr></thead><tbody>${alerts.map(a => `<tr><td>${fmtDate(a.alerted_at)}</td><td><span class="badge badge-green">${esc(a.watch_type)}</span></td><td>${esc(a.product_name)}</td><td><strong>${fmtKr(a.price)}</strong></td><td>${esc(a.retailer || "–")}</td></tr>`).join("")}</tbody></table>`;

      const histContainer = document.getElementById("history-container");
      histContainer.innerHTML = !hist.length
        ? `<div class="empty-msg">Ingen prishistorik loggad ännu.</div>`
        : `<table><thead><tr><th>Tid</th><th>Produkt</th><th>Pris</th><th>Inkl. frakt</th><th>Butik</th></tr></thead><tbody>${hist.map(h => `<tr><td>${fmtDate(h.recorded_at)}</td><td>${esc(h.product_name)}</td><td><strong>${fmtKr(h.price)}</strong></td><td>${fmtKr(h.total_price)}</td><td>${esc(h.retailer || "–")}</td></tr>`).join("")}</tbody></table>`;
    }

    async function loadState() {
      try {
        const data = await apiFetch("/api/state");
        state.watches = data.watches;
        state.history = data.history;
        state.config = data.config;
        state.aiProfile = data.ai_profile;
        state.lastDigest = data.last_digest;
        state.lastAlternatives = data.last_alternatives;
        renderState();
      } catch (err) {
        showToast(`Kunde inte ladda status: ${err.message}`, true);
      }
    }

    async function deleteWatch(type, id) {
      try {
        await apiFetch(`/api/watches/${encodeURIComponent(type)}/${Number(id)}`, { method: "DELETE" });
        showToast("Bevakningen togs bort.");
        await loadState();
      } catch (err) {
        showToast(`Kunde inte ta bort bevakning: ${err.message}`, true);
      }
    }

    function openWatchModal(publicId, name, currentPrice) {
      pendingModalPid = publicId;
      const suggested = currentPrice ? Math.max(1, Math.round(Number(currentPrice) * 0.9)) : 1000;
      document.getElementById("modal-title").textContent = "Bevaka produkt";
      document.getElementById("modal-subtitle").textContent = `${name} (Nuvarande pris: ${fmtKr(currentPrice)})`;
      document.getElementById("modal-target").value = String(suggested);
      document.getElementById("watch-modal").showModal();
    }

    function openWatchFromList(sourceKey, idx) {
      const list = state[sourceKey] || [];
      const item = list[idx];
      if (!item || !item.public_id) return;
      const price = item.lowest_price ?? item.current_price ?? item.price ?? 0;
      const name = item.name || item.title || `Produkt ${item.public_id}`;
      openWatchModal(item.public_id, name, price);
    }

    async function loadPrisfall() {
      const container = document.getElementById("prisfall-container");
      container.innerHTML = `<div class="empty-msg">Hämtar aktuella prisfall från PrisGeek...</div>`;
      const period = document.getElementById("pf-period").value;
      const minPct = document.getElementById("pf-min").value;
      try {
        const data = await apiFetch(`/api/prisfall?period_days=${encodeURIComponent(period)}&min_drop_pct=${encodeURIComponent(minPct)}`);
        const deals = data.deals || [];
        state.prisfallDeals = deals;
        state.prisfallLoaded = true;
        if (!deals.length) {
          container.innerHTML = `<div class="empty-msg">Inga prisfall matchade filtren.</div>`;
          return;
        }
        container.innerHTML = `
          <div class="deals-grid">
            ${deals.map((d, idx) => `
              <div class="deal-card">
                <div>
                  <div class="deal-meta">
                    <span class="badge badge-green">-${Number(d.drop_pct || 0).toFixed(1)}%</span>
                    <span class="badge ${d.deal_badge === "Rekordlågt" ? "badge-green" : "badge-muted"}">${esc(d.deal_badge || "Prisfall")}</span>
                  </div>
                  <div style="margin-top: 8px; font-weight: 600;">
                    <a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.name)}</a>
                  </div>
                  <div class="subtext">${esc(d.brand || "")}</div>
                </div>
                <div class="deal-meta">
                  <div>
                    <span class="deal-price">${fmtKr(d.current_price)}</span>
                    ${d.previous_price ? `<span class="deal-old">${fmtKr(d.previous_price)}</span>` : ""}
                  </div>
                  <div class="actions-cell">
                    <button class="btn-secondary btn-sm" onclick="openProductDetailModal(${Number(d.public_id)})">Graf</button>
                    <button class="btn-primary btn-sm" onclick="openWatchFromList('prisfallDeals', ${idx})">Bevaka</button>
                  </div>
                </div>
              </div>`).join("")}
          </div>`;
      } catch (err) {
        container.innerHTML = `<div class="empty-msg">Fel vid hämtning av prisfall: ${esc(err.message)}</div>`;
      }
    }

    async function loadMedlemsfynd() {
      const container = document.getElementById("medlemsfynd-container");
      container.innerHTML = `<div class="empty-msg">Hämtar medlemsfynd från SweClockers/PrisGeek...</div>`;
      const cat = document.getElementById("mf-cat").value.trim();
      const score = document.getElementById("mf-score").value || "0";
      const qs = new URLSearchParams({ min_score: score });
      if (cat) qs.set("category", cat);
      try {
        const data = await apiFetch(`/api/medlemsfynd?${qs.toString()}`);
        const deals = data.deals || [];
        state.medlemsfyndDeals = deals;
        state.medlemsfyndLoaded = true;
        if (!deals.length) {
          container.innerHTML = `<div class="empty-msg">Inga medlemsfynd matchade filtren.</div>`;
          return;
        }
        container.innerHTML = `
          <table>
            <thead><tr><th>Röster</th><th>Fynd</th><th>Kategori</th><th>Butik</th><th>Pris</th><th>Tipster</th><th>Åtgärder</th></tr></thead>
            <tbody>
              ${deals.map((d, idx) => `
                <tr>
                  <td><span class="badge badge-purple">+${Number(d.score || 0)}</span></td>
                  <td>
                    <a href="${esc(d.product_url)}" target="_blank" rel="noopener">${esc(d.title)}</a>
                    ${d.forum_url ? `<div class="subtext"><a href="${esc(d.forum_url)}" target="_blank" rel="noopener">Diskussion på SweClockers ↗</a></div>` : ""}
                  </td>
                  <td>${esc(d.category || "–")}</td>
                  <td>${esc(d.retailer_name)}</td>
                  <td><strong>${fmtKr(d.price)}</strong></td>
                  <td>${esc(d.tipster || "–")}</td>
                  <td class="actions-cell">
                    ${d.public_id ? `<button class="btn-secondary btn-sm" onclick="openProductDetailModal(${Number(d.public_id)})">Graf</button><button class="btn-primary btn-sm" onclick="openWatchFromList('medlemsfyndDeals', ${idx})">Bevaka</button>` : `<span class="badge badge-muted">Endast länk</span>`}
                  </td>
                </tr>`).join("")}
            </tbody>
          </table>`;
      } catch (err) {
        container.innerHTML = `<div class="empty-msg">Fel: ${esc(err.message)}</div>`;
      }
    }

    async function loadFyndvaror() {
      const container = document.getElementById("fyndvaror-container");
      container.innerHTML = `<div class="empty-msg">Hämtar fyndvaror / outlet...</div>`;
      const q = document.getElementById("fv-query").value.trim();
      const minPct = document.getElementById("fv-min").value || "5";
      const qs = new URLSearchParams({ min_discount_pct: minPct });
      if (q) qs.set("query", q);
      try {
        const data = await apiFetch(`/api/fyndvaror?${qs.toString()}`);
        const items = data.items || [];
        state.fyndvarorItems = items;
        state.fyndvarorLoaded = true;
        if (!items.length) {
          container.innerHTML = `<div class="empty-msg">Inga fyndvaror matchade filtren.</div>`;
          return;
        }
        container.innerHTML = `
          <table>
            <thead><tr><th>Produkt</th><th>Skick</th><th>Outlet-pris</th><th>Nypris</th><th>Rabatt</th><th>Butik</th><th>Åtgärder</th></tr></thead>
            <tbody>
              ${items.map((it, idx) => `
                <tr>
                  <td><a href="${esc(it.url)}" target="_blank" rel="noopener">${esc(it.name)}</a></td>
                  <td><span class="badge badge-amber">${esc(it.condition)}</span></td>
                  <td><strong>${fmtKr(it.price)}</strong></td>
                  <td>${fmtKr(it.new_price)}</td>
                  <td>${it.discount_pct ? `<span class="badge badge-green">-${Number(it.discount_pct).toFixed(1)}%</span>` : "–"}</td>
                  <td>${esc(it.retailer)}</td>
                  <td class="actions-cell">
                    <button class="btn-secondary btn-sm" onclick="openProductDetailModal(${Number(it.public_id)})">Graf</button>
                    <button class="btn-primary btn-sm" onclick="openWatchFromList('fyndvarorItems', ${idx})">Bevaka</button>
                  </td>
                </tr>`).join("")}
            </tbody>
          </table>`;
      } catch (err) {
        container.innerHTML = `<div class="empty-msg">Fel: ${esc(err.message)}</div>`;
      }
    }

    async function loadBasket() {
      const container = document.getElementById("basket-container");
      container.innerHTML = `<div class="empty-msg">Optimerar inköpslista inklusive frakt...</div>`;
      try {
        const data = await apiFetch("/api/basket");
        state.basketLoaded = true;
        const items = data.items || [];
        if (!items.length) {
          container.innerHTML = `<div class="empty-msg">Lägg till minst en produkt- eller intressebevakning först.</div>`;
          return;
        }
        const rec = data.recommended;
        const recText = rec
          ? (rec.strategy === "single_store"
              ? `Rekommenderat: Köp allt hos <strong>${esc(rec.retailer)}</strong> för <strong>${fmtKr(rec.total_with_shipping)}</strong> inkl. frakt (sparar ${fmtKr(rec.savings_vs_split)} mot att splittra).`
              : `Rekommenderat: Splittra mellan <strong>${esc((rec.stores || []).join(", "))}</strong> för lägsta totalpris <strong>${fmtKr(rec.total_with_shipping)}</strong> inkl. frakt.`)
          : "";
        const singleOpts = data.single_store_options || [];
        container.innerHTML = `
          <div style="margin-bottom:14px; padding:10px 14px; border-radius:8px; background:var(--green-bg); color:#bbf7d0;">
            ${recText}
          </div>
          <h4>Enskilda butiker (allt på ett ställe)</h4>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Butik</th><th>I lager</th><th>Delsumma</th><th>Frakt</th><th>Totalt</th><th>Saknas</th></tr></thead>
              <tbody>
                ${singleOpts.map(opt => `
                  <tr>
                    <td><strong>${esc(opt.retailer)}</strong></td>
                    <td>${opt.items_available} / ${opt.total_items}</td>
                    <td>${fmtKr(opt.subtotal_price)}</td>
                    <td>${fmtKr(opt.shipping_cost)}</td>
                    <td><strong>${fmtKr(opt.total_with_shipping)}</strong></td>
                    <td>${opt.missing_items.length ? esc(opt.missing_items.join(", ")) : `<span class="badge badge-green">Allt finns!</span>`}</td>
                  </tr>`).join("")}
              </tbody>
            </table>
          </div>`;
      } catch (err) {
        container.innerHTML = `<div class="empty-msg">Fel: ${esc(err.message)}</div>`;
      }
    }

    document.querySelectorAll(".tab-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
        document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
        btn.classList.add("active");
        const tabName = btn.dataset.tab;
        document.getElementById(`tab-${tabName}`).classList.add("active");
        if (tabName === "prisfall" && !state.prisfallLoaded) loadPrisfall();
        if (tabName === "medlemsfynd" && !state.medlemsfyndLoaded) loadMedlemsfynd();
        if (tabName === "fyndvaror" && !state.fyndvarorLoaded) loadFyndvaror();
        if (tabName === "basket" && !state.basketLoaded) loadBasket();
      });
    });

    document.getElementById("refresh-btn").addEventListener("click", loadState);
    document.getElementById("basket-refresh-btn").addEventListener("click", loadBasket);
    document.getElementById("copy-yaml-btn").addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(document.getElementById("ha-yaml-box").textContent);
        showToast("Lovelace YAML kopierad!");
      } catch {
        showToast("Markera och kopiera YAML-texten manuellt.", true);
      }
    });

    document.getElementById("test-notify-btn").addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      btn.textContent = "Skickar...";
      try {
        const res = await apiFetch("/api/test-notify", { method: "POST", body: "{}" });
        const svcs = (res.notified_services || []).join(", ") || "webhook";
        showToast(`Testnotis skickad (${svcs})!`);
      } catch (err) {
        showToast(`Kunde inte skicka testnotis: ${err.message}`, true);
      } finally {
        btn.disabled = false;
        btn.textContent = "Testa notis";
      }
    });

    document.getElementById("run-check-btn").addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      btn.textContent = "Kontrollerar...";
      try {
        const report = await apiFetch("/api/check", {
          method: "POST",
          body: JSON.stringify({ send_webhooks: false }),
        });
        showToast(
          `Kontroll klar: ${report.checked_product_watches} produkter, ${report.checked_query_watches} intressebevakningar (${report.triggered_alerts.length} larm).`
        );
        await loadState();
      } catch (err) {
        showToast(`Priskontroll misslyckades: ${err.message}`, true);
      } finally {
        btn.disabled = false;
        btn.textContent = "Kör priskontroll nu";
      }
    });

    document.getElementById("product-watch-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const res = await apiFetch("/api/watches/product", {
          method: "POST",
          body: JSON.stringify({
            public_id_or_url: document.getElementById("pw-id").value.trim(),
            target_price: Number(document.getElementById("pw-target").value),
            alert_below_30d_pct: Number(document.getElementById("pw-pct30").value || 0),
            alert_on_all_time_low: document.getElementById("pw-alltime").checked,
            ai_requirements: document.getElementById("pw-ai-req").value.trim(),
            in_stock_only: true,
            notes: document.getElementById("pw-notes").value.trim(),
          }),
        });
        showToast(`Lade till bevakning för ${res.product_name}`);
        e.target.reset();
        document.getElementById("pw-alltime").checked = true;
        document.getElementById("pw-pct30").value = "0";
        await loadState();
      } catch (err) {
        showToast(`Fel: ${err.message}`, true);
      }
    });

    document.getElementById("interest-watch-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const keywords = document.getElementById("iw-keywords").value.split(",").map(s => s.trim()).filter(Boolean);
      const excludeKeywords = document.getElementById("iw-exclude").value.split(",").map(s => s.trim()).filter(Boolean);
      try {
        const res = await apiFetch("/api/watches/interest", {
          method: "POST",
          body: JSON.stringify({
            name: document.getElementById("iw-name").value.trim(),
            search_query: document.getElementById("iw-query").value.trim(),
            target_price: Number(document.getElementById("iw-target").value),
            must_include_keywords: keywords,
            exclude_keywords: excludeKeywords,
            ai_requirements: document.getElementById("iw-ai-req").value.trim(),
            in_stock_only: true,
            include_outlet: document.getElementById("iw-outlet").checked,
          }),
        });
        showToast(`Lade till "${res.watch.name}" (${res.current_matches.length} träffar nu)`);
        e.target.reset();
        await loadState();
      } catch (err) {
        showToast(`Fel: ${err.message}`, true);
      }
    });

    document.getElementById("edit-product-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!editingProductId) return;
      try {
        await apiFetch(`/api/watches/product/${Number(editingProductId)}`, {
          method: "PUT",
          body: JSON.stringify({
            target_price: Number(document.getElementById("ep-target").value),
            alert_below_30d_pct: Number(document.getElementById("ep-pct30").value || 0),
            ai_requirements: document.getElementById("ep-ai-req").value.trim(),
            notes: document.getElementById("ep-notes").value.trim(),
            in_stock_only: document.getElementById("ep-stock").checked,
            alert_on_all_time_low: document.getElementById("ep-alltime").checked,
          }),
        });
        document.getElementById("edit-product-modal").close();
        showToast("Produktbevakning uppdaterad!");
        await loadState();
      } catch (err) {
        showToast(`Fel: ${err.message}`, true);
      }
    });

    document.getElementById("edit-interest-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!editingInterestId) return;
      try {
        await apiFetch(`/api/watches/query/${Number(editingInterestId)}`, {
          method: "PUT",
          body: JSON.stringify({
            name: document.getElementById("ei-name").value.trim(),
            search_query: document.getElementById("ei-query").value.trim(),
            target_price: Number(document.getElementById("ei-target").value),
            must_include_keywords: document.getElementById("ei-must").value.split(",").map(s => s.trim()).filter(Boolean),
            exclude_keywords: document.getElementById("ei-excl").value.split(",").map(s => s.trim()).filter(Boolean),
            ai_requirements: document.getElementById("ei-ai-req").value.trim(),
            in_stock_only: document.getElementById("ei-stock").checked,
            include_outlet: document.getElementById("ei-outlet").checked,
          }),
        });
        document.getElementById("edit-interest-modal").close();
        showToast("Intressebevakning uppdaterad!");
        await loadState();
      } catch (err) {
        showToast(`Fel: ${err.message}`, true);
      }
    });

    document.getElementById("ai-digest-btn").addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      btn.textContent = "Analyserar dagens fynd...";
      try {
        const res = await apiFetch("/api/ai/digest", {
          method: "POST",
          body: JSON.stringify({
            profile: document.getElementById("ai-profile-input").value.trim(),
            send_notification: document.getElementById("ai-digest-notify").checked,
          }),
        });
        state.lastDigest = res;
        renderAiDigest(res);
        showToast("Topp 5 AI-fynd genererade!");
      } catch (err) {
        showToast(`AI-digest misslyckades: ${err.message}`, true);
      } finally {
        btn.disabled = false;
        btn.textContent = "Generera Topp 5 AI-fynd";
      }
    });

    document.getElementById("ai-alt-btn").addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      btn.textContent = "Söker alternativ...";
      try {
        const res = await apiFetch("/api/ai/alternatives", { method: "POST", body: "{}" });
        state.lastAlternatives = res;
        renderAiAlternatives(res);
        showToast("Alternativsökning klar!");
      } catch (err) {
        showToast(`Alternativsökning misslyckades: ${err.message}`, true);
      } finally {
        btn.disabled = false;
        btn.textContent = "Sök bättre alternativ";
      }
    });

    document.getElementById("search-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const container = document.getElementById("search-results-container");
      container.innerHTML = `<div class="empty-msg">Söker på PrisGeek...</div>`;
      const q = document.getElementById("search-q").value.trim();
      const maxPrice = document.getElementById("search-max").value.trim();
      const inStock = document.getElementById("search-stock").checked;
      const qs = new URLSearchParams({ q, in_stock_only: String(inStock) });
      if (maxPrice) qs.set("max_price", maxPrice);
      try {
        const data = await apiFetch(`/api/search?${qs.toString()}`);
        const items = data.results || [];
        state.searchResults = items;
        if (!items.length) {
          container.innerHTML = `<div class="empty-msg">Inga produkter hittades.</div>`;
          return;
        }
        container.innerHTML = `
          <table>
            <thead>
              <tr><th>Produkt</th><th>Märke</th><th>Lägsta pris</th><th>Inkl. frakt</th><th>Butik</th><th>Lager</th><th>Åtgärder</th></tr>
            </thead>
            <tbody>
              ${items.map((item, idx) => `
                <tr>
                  <td><a href="${esc(item.url)}" target="_blank" rel="noopener">${esc(item.name)}</a></td>
                  <td>${esc(item.brand || "–")}</td>
                  <td><strong>${fmtKr(item.lowest_price)}</strong></td>
                  <td>${fmtKr(item.lowest_total_price)}</td>
                  <td>${esc(item.best_retailer || "–")}</td>
                  <td>${item.in_stock ? `<span class="badge badge-green">I lager</span>` : `<span class="badge badge-red">Slut</span>`}</td>
                  <td class="actions-cell">
                    <button class="btn-secondary btn-sm" onclick="openProductDetailModal(${Number(item.public_id)})">Graf &amp; Butiker</button>
                    <button class="btn-primary btn-sm" onclick="openWatchFromList('searchResults', ${idx})">Bevaka</button>
                  </td>
                </tr>`).join("")}
            </tbody>
          </table>`;
      } catch (err) {
        container.innerHTML = `<div class="empty-msg">Sökfel: ${esc(err.message)}</div>`;
      }
    });

    document.getElementById("prisfall-form").addEventListener("submit", (e) => { e.preventDefault(); loadPrisfall(); });
    document.getElementById("medlemsfynd-form").addEventListener("submit", (e) => { e.preventDefault(); loadMedlemsfynd(); });
    document.getElementById("fyndvaror-form").addEventListener("submit", (e) => { e.preventDefault(); loadFyndvaror(); });

    document.getElementById("modal-cancel").addEventListener("click", () => {
      document.getElementById("watch-modal").close();
    });

    document.getElementById("watch-modal-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!pendingModalPid) return;
      const target = Number(document.getElementById("modal-target").value);
      try {
        const res = await apiFetch("/api/watches/product", {
          method: "POST",
          body: JSON.stringify({
            public_id_or_url: String(pendingModalPid),
            target_price: target,
            in_stock_only: true,
          }),
        });
        document.getElementById("watch-modal").close();
        showToast(`Bevakning sparad för ${res.product_name}`);
        await loadState();
      } catch (err) {
        showToast(`Kunde inte spara: ${err.message}`, true);
      }
    });

    loadState();
  </script>
</body>
</html>
"""


def _parse_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


async def homepage(_request: Request) -> HTMLResponse:
    """Serve the self-contained dashboard UI."""
    return HTMLResponse(DASHBOARD_HTML)


async def api_state(_request: Request) -> JSONResponse:
    """Return active watches, recent price history, AI settings/caches, and notifier config."""
    watches = await run_in_threadpool(list_all_watches, True)
    history = await run_in_threadpool(get_recent_history, None, 50)
    config = await run_in_threadpool(load_notifier_config)
    ai_profile = await run_in_threadpool(get_setting, "ai_profile", DEFAULT_AI_PROFILE)
    last_digest = await run_in_threadpool(get_setting, "last_digest", None)
    last_alternatives = await run_in_threadpool(get_setting, "last_alternatives", None)
    return JSONResponse(
        {
            "watches": watches,
            "history": history,
            "config": config,
            "ai_profile": ai_profile,
            "last_digest": last_digest,
            "last_alternatives": last_alternatives,
        }
    )


async def api_add_product_watch(request: Request) -> JSONResponse:
    """Add or update a single-product price watch."""
    try:
        body = await request.json()
        public_id_or_url = str(body.get("public_id_or_url") or "").strip()
        if not public_id_or_url:
            return JSONResponse({"error": "public_id_or_url is required"}, status_code=400)
        target_price = float(body["target_price"])
        pct30 = float(body.get("alert_below_30d_pct") or 0.0)
    except (KeyError, TypeError, ValueError) as exc:
        return JSONResponse({"error": f"Invalid request payload: {exc}"}, status_code=400)

    try:
        result = await run_in_threadpool(
            add_product_watch,
            public_id_or_url=public_id_or_url,
            target_price=target_price,
            in_stock_only=_parse_bool(body.get("in_stock_only"), True),
            exclude_refurbished=_parse_bool(body.get("exclude_refurbished"), True),
            alert_on_all_time_low=_parse_bool(body.get("alert_on_all_time_low"), True),
            alert_below_30d_pct=pct30,
            ai_requirements=str(body.get("ai_requirements") or ""),
            notes=str(body.get("notes") or ""),
        )
        return JSONResponse(result)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except Exception as exc:
        logger.exception("Failed adding product watch")
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_add_interest_watch(request: Request) -> JSONResponse:
    """Add a keyword/interest discovery watch."""
    try:
        body = await request.json()
        name = str(body.get("name") or "").strip()
        search_query = str(body.get("search_query") or "").strip()
        if not name or not search_query:
            return JSONResponse({"error": "name and search_query are required"}, status_code=400)
        target_price = float(body["target_price"])
    except (KeyError, TypeError, ValueError) as exc:
        return JSONResponse({"error": f"Invalid request payload: {exc}"}, status_code=400)

    must_kw = body.get("must_include_keywords") or []
    excl_kw = body.get("exclude_keywords") or []
    try:
        result = await run_in_threadpool(
            add_interest_watch,
            name=name,
            search_query=search_query,
            target_price=target_price,
            must_include_keywords=list(must_kw),
            exclude_keywords=list(excl_kw),
            in_stock_only=_parse_bool(body.get("in_stock_only"), True),
            include_outlet=_parse_bool(body.get("include_outlet"), False),
            ai_requirements=str(body.get("ai_requirements") or ""),
        )
        return JSONResponse(result)
    except Exception as exc:
        logger.exception("Failed adding interest watch")
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_update_watch(request: Request) -> JSONResponse:
    """Update target price, keywords, or alert rules on an existing watch."""
    watch_type = request.path_params["watch_type"].lower().strip()
    watch_id = int(request.path_params["watch_id"])
    try:
        body = await request.json()
        if watch_type == "product":
            updated = await run_in_threadpool(
                update_product_watch_config,
                watch_id,
                target_price=float(body["target_price"]),
                notes=str(body.get("notes") or ""),
                in_stock_only=_parse_bool(body.get("in_stock_only"), True),
                exclude_refurbished=_parse_bool(body.get("exclude_refurbished"), True),
                alert_on_all_time_low=_parse_bool(body.get("alert_on_all_time_low"), True),
                alert_below_30d_pct=float(body.get("alert_below_30d_pct") or 0.0),
                ai_requirements=str(body.get("ai_requirements") or ""),
            )
            return JSONResponse({"watch": updated})
        if watch_type == "query":
            updated = await run_in_threadpool(
                update_query_watch_config,
                watch_id,
                name=str(body["name"]),
                search_query=str(body["search_query"]),
                target_price=float(body["target_price"]),
                must_include_keywords=list(body.get("must_include_keywords") or []),
                exclude_keywords=list(body.get("exclude_keywords") or []),
                in_stock_only=_parse_bool(body.get("in_stock_only"), True),
                include_outlet=_parse_bool(body.get("include_outlet"), False),
                ai_requirements=str(body.get("ai_requirements") or ""),
            )
            return JSONResponse({"watch": updated})
        return JSONResponse({"error": "watch_type must be 'product' or 'query'"}, status_code=400)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except (KeyError, TypeError) as exc:
        return JSONResponse({"error": f"Invalid payload: {exc}"}, status_code=400)


async def api_delete_watch(request: Request) -> JSONResponse:
    """Remove (deactivate) a watch by type ('product' or 'query') and ID."""
    watch_type = request.path_params["watch_type"]
    watch_id = int(request.path_params["watch_id"])
    try:
        removed = await run_in_threadpool(remove_watch, watch_type, watch_id)
        return JSONResponse({"removed": removed})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def api_run_check(request: Request) -> JSONResponse:
    """Trigger an immediate check cycle across all active watches."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    send_webhooks = _parse_bool(body.get("send_webhooks"), False)
    try:
        report = await run_in_threadpool(run_check_cycle, send_webhooks=send_webhooks)
        return JSONResponse(report)
    except Exception as exc:
        logger.exception("Check cycle failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_test_notify(_request: Request) -> JSONResponse:
    """Send a test push notification via configured Home Assistant services and webhooks."""
    test_payload = {
        "product_name": "Testnotis (PrisGeek Price Tracker)",
        "price": 1490.0,
        "total_price": 1490.0,
        "target_price": 1500.0,
        "retailer": "PrisGeek",
        "lowest_30d_price": 1554.0,
        "lowest_all_time_price": 1490.0,
        "deal_assessment": "At all-time low",
        "url": "https://prisgeek.se/prisfall",
    }
    try:
        res = await run_in_threadpool(send_price_alert, test_payload, dry_run=False)
        return JSONResponse(res)
    except Exception as exc:
        logger.exception("Test notification failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_product_details(request: Request) -> JSONResponse:
    """Return full store offers and 90-day history series for one product."""
    pid = int(request.path_params["public_id"])
    try:
        details = await run_in_threadpool(inspect_product, str(pid))
        return JSONResponse(details)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except Exception as exc:
        logger.exception("Product details failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_search(request: Request) -> JSONResponse:
    """Proxy live search queries to PrisGeek."""
    query = str(request.query_params.get("q") or "").strip()
    if not query:
        return JSONResponse({"error": "q parameter is required"}, status_code=400)

    max_price_raw = request.query_params.get("max_price")
    max_price = float(max_price_raw) if max_price_raw else None
    in_stock_only = _parse_bool(request.query_params.get("in_stock_only"), True)

    try:
        results = await run_in_threadpool(
            search_products,
            query=query,
            max_price=max_price,
            in_stock_only=in_stock_only,
            exclude_refurbished=True,
            limit=20,
        )
        return JSONResponse({"results": results})
    except Exception as exc:
        logger.exception("Search failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_prisfall(request: Request) -> JSONResponse:
    """Fetch live daily price drops from PrisGeek."""
    try:
        min_drop = int(request.query_params.get("min_drop_pct", "15"))
        period_days = int(request.query_params.get("period_days", "7"))
        max_price_raw = request.query_params.get("max_price")
        max_price = float(max_price_raw) if max_price_raw else None
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid filter parameter: {exc}"}, status_code=400)

    try:
        deals = await run_in_threadpool(
            browse_prisfall,
            min_drop_pct=min_drop,
            period_days=period_days,
            max_price=max_price,
            limit=25,
        )
        return JSONResponse({"deals": deals})
    except Exception as exc:
        logger.exception("Prisfall fetch failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_medlemsfynd(request: Request) -> JSONResponse:
    """Fetch live SweClockers community member deals from PrisGeek."""
    category = request.query_params.get("category") or None
    try:
        min_score = int(request.query_params.get("min_score", "0"))
    except ValueError:
        min_score = 0
    try:
        deals = await run_in_threadpool(
            browse_medlemsfynd,
            category=category,
            min_score=min_score,
            limit=30,
        )
        return JSONResponse({"deals": deals})
    except Exception as exc:
        logger.exception("Medlemsfynd fetch failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_fyndvaror(request: Request) -> JSONResponse:
    """Fetch open-box/demo outlet bargains from PrisGeek."""
    query = request.query_params.get("query") or None
    try:
        min_discount = int(request.query_params.get("min_discount_pct", "5"))
        max_price_raw = request.query_params.get("max_price")
        max_price = float(max_price_raw) if max_price_raw else None
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid parameter: {exc}"}, status_code=400)

    try:
        items = await run_in_threadpool(
            browse_fyndvaror,
            query=query,
            min_discount_pct=min_discount,
            max_price=max_price,
            limit=30,
        )
        return JSONResponse({"items": items})
    except Exception as exc:
        logger.exception("Fyndvaror fetch failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_basket(request: Request) -> JSONResponse:
    """Optimize shopping basket across watched product_ids or explicit ids query param."""
    raw_ids = str(request.query_params.get("ids") or "").strip()
    ids = [int(x) for x in raw_ids.split(",") if x.strip().isdigit()] if raw_ids else None
    try:
        data = await run_in_threadpool(optimize_shopping_basket, ids)
        return JSONResponse(data)
    except Exception as exc:
        logger.exception("Basket optimization failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_ai_analyze_product(request: Request) -> JSONResponse:
    """Run AI product deal evaluation via Home Assistant AI Task."""
    try:
        body = await request.json()
        public_id = body.get("public_id")
        if not public_id:
            return JSONResponse({"error": "public_id is required"}, status_code=400)
        target_raw = body.get("target_price")
        target_price = float(target_raw) if target_raw is not None else None
        ai_req = str(body.get("ai_requirements") or "")
        watch_id_raw = body.get("watch_id")
        watch_id = int(watch_id_raw) if watch_id_raw is not None else None
    except (TypeError, ValueError) as exc:
        return JSONResponse({"error": f"Invalid payload: {exc}"}, status_code=400)

    try:
        res = await run_in_threadpool(
            analyze_product_deal,
            str(public_id),
            target_price=target_price,
            ai_requirements=ai_req,
            watch_id=watch_id,
        )
        return JSONResponse(res)
    except Exception as exc:
        logger.exception("AI product analysis failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_ai_vet_interest(request: Request) -> JSONResponse:
    """Run AI vetting across top matches for an active interest watch."""
    watch_id = int(request.path_params["watch_id"])
    watches = await run_in_threadpool(list_all_watches, True)
    qw = next((q for q in watches["query_watches"] if int(q["id"]) == watch_id), None)
    if not qw:
        return JSONResponse({"error": f"Interest watch #{watch_id} not found"}, status_code=404)

    candidates = qw.get("top_matches") or []
    if not candidates:
        candidates = await run_in_threadpool(
            search_products,
            qw["search_query"],
            None,
            bool(qw["in_stock_only"]),
            not bool(qw.get("include_outlet", 0)),
            10,
        )
    try:
        res = await run_in_threadpool(vet_interest_watch_matches, qw, candidates)
        return JSONResponse(res)
    except Exception as exc:
        logger.exception("AI interest vetting failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_ai_digest(request: Request) -> JSONResponse:
    """Generate a curated Top 5 daily AI deal digest."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    profile = body.get("profile")
    send_notif = _parse_bool(body.get("send_notification"), False)
    try:
        res = await run_in_threadpool(
            generate_ai_deal_digest,
            custom_profile=profile,
            send_notification=send_notif,
        )
        return JSONResponse(res)
    except Exception as exc:
        logger.exception("AI digest failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_ai_alternatives(_request: Request) -> JSONResponse:
    """Find cross-tier upgrade alternatives for active watches."""
    try:
        res = await run_in_threadpool(find_cross_tier_alternatives)
        return JSONResponse(res)
    except Exception as exc:
        logger.exception("AI alternatives lookup failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


init_db()

app = Starlette(
    routes=[
        Route("/", homepage, methods=["GET"]),
        Route("/api/state", api_state, methods=["GET"]),
        Route("/api/watches/product", api_add_product_watch, methods=["POST"]),
        Route("/api/watches/interest", api_add_interest_watch, methods=["POST"]),
        Route("/api/watches/{watch_type}/{watch_id:int}", api_update_watch, methods=["PUT"]),
        Route("/api/watches/{watch_type}/{watch_id:int}", api_delete_watch, methods=["DELETE"]),
        Route("/api/check", api_run_check, methods=["POST"]),
        Route("/api/test-notify", api_test_notify, methods=["POST"]),
        Route("/api/product/{public_id:int}", api_product_details, methods=["GET"]),
        Route("/api/search", api_search, methods=["GET"]),
        Route("/api/prisfall", api_prisfall, methods=["GET"]),
        Route("/api/medlemsfynd", api_medlemsfynd, methods=["GET"]),
        Route("/api/fyndvaror", api_fyndvaror, methods=["GET"]),
        Route("/api/basket", api_basket, methods=["GET"]),
        Route("/api/ai/analyze-product", api_ai_analyze_product, methods=["POST"]),
        Route("/api/ai/vet-interest/{watch_id:int}", api_ai_vet_interest, methods=["POST"]),
        Route("/api/ai/digest", api_ai_digest, methods=["POST"]),
        Route("/api/ai/alternatives", api_ai_alternatives, methods=["POST"]),
    ]
)

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8050"))
    uvicorn.run(app, host=host, port=port)
