"""Home Assistant AI Task advisor for spec vetting, deal verdicts, alternatives, and digests."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import httpx

from db import (
    get_setting,
    list_all_watches,
    save_product_ai_verdict,
    save_query_ai_summary,
    set_setting,
    utc_now_iso,
)
from notifier import _get_ha_token, load_notifier_config, send_price_alert
from prisgeek_client import PrisGeekClient

logger = logging.getLogger(__name__)

DEFAULT_AI_PROFILE = (
    "PC-komponenter, grafikkort (t.ex. RTX 5060 Ti / 5070), snabb lagring "
    "(NVMe SSD & A2/V30 microSD för Jetson/SBC), 3D-skrivare & filament, "
    "hemautomation (Zigbee, Matter, Home Assistant), nätverk (UniFi/2.5GbE) "
    "och prisvärd elektronik."
)

PREFERRED_AI_ENTITIES = (
    "ai_task.openai_ai_task",
    "ai_task.google_ai_task",
)


def _select_ai_entity(
    client: httpx.Client, ha_core_url: str, headers: dict[str, str], cfg: dict[str, Any]
) -> str:
    """Resolve the Home Assistant ai_task entity ID to invoke."""
    explicit = str(
        os.environ.get("HA_AI_ENTITY_ID") or cfg.get("ai_entity_id") or ""
    ).strip()
    if explicit:
        return explicit

    resp = client.get(f"{ha_core_url}/states", headers=headers)
    resp.raise_for_status()
    states = resp.json()
    available = {
        str(s.get("entity_id"))
        for s in (states if isinstance(states, list) else [])
        if str(s.get("entity_id", "")).startswith("ai_task.")
        and (int((s.get("attributes") or {}).get("supported_features", 1) or 0) & 1)
    }
    for candidate in PREFERRED_AI_ENTITIES:
        if candidate in available:
            return candidate
    if available:
        return sorted(available)[0]
    raise RuntimeError("No ai_task.* entity found in Home Assistant Core.")


def _call_ha_ai_task(task_name: str, instructions: str) -> str:
    """Invoke Home Assistant's ai_task.generate_data service and return its text response."""
    cfg = load_notifier_config()
    token = _get_ha_token()
    if not token:
        raise RuntimeError(
            "No Home Assistant token (SUPERVISOR_TOKEN or HA_TOKEN) available for AI Task."
        )

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    with httpx.Client(timeout=45.0) as client:
        entity_id = _select_ai_entity(client, cfg["ha_core_url"], headers, cfg)
        url = f"{cfg['ha_core_url']}/services/ai_task/generate_data?return_response=true"
        resp = client.post(
            url,
            headers=headers,
            json={
                "entity_id": entity_id,
                "task_name": task_name,
                "instructions": instructions,
            },
        )
        if not resp.is_success:
            raise RuntimeError(
                f"HA ai_task error ({resp.status_code}) from {entity_id}: {resp.text}"
            )
        resp.raise_for_status()
        body = resp.json()

    service_resp = body.get("service_response") or {}
    data = service_resp.get("data")
    if data is None:
        raise RuntimeError(f"Empty ai_task response from {entity_id}: {body}")
    return data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)


def _extract_json(text: str) -> Any:
    """Strip markdown code fences if present and parse JSON cleanly."""
    cleaned = text.strip()
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL | re.IGNORECASE)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        obj_match = re.search(r"(\{.*\}|\[.*\])", cleaned, re.DOTALL)
        if obj_match:
            return json.loads(obj_match.group(1))
        raise


def analyze_product_deal(
    public_id_or_url: str | int,
    *,
    target_price: float | None = None,
    ai_requirements: str = "",
    watch_id: int | None = None,
) -> dict[str, Any]:
    """Run an AI evaluation of price trajectory, specs, SweClockers awards, and user criteria."""
    client = PrisGeekClient()
    try:
        details = client.get_product_details(public_id_or_url)
    finally:
        client.close()

    offers_compact = [
        {
            "retailer": o["retailer_name"],
            "price": o["price"],
            "shipping": o["shipping"],
            "in_stock": o["in_stock"],
        }
        for o in (details.get("offers") or [])[:8]
    ]
    extra = details.get("extra_context") or {}
    context_payload = {
        "name": details["name"],
        "brand": details.get("brand"),
        "category": details.get("category"),
        "clean_model_query": details.get("clean_model_query"),
        "current_lowest_price_sek": details.get("lowest_price"),
        "best_retailer": details.get("best_retailer"),
        "lowest_30d_price_sek": details.get("lowest_30d_price"),
        "lowest_all_time_price_sek": details.get("lowest_all_time_price"),
        "deal_assessment": details.get("deal_assessment"),
        "target_price_sek": target_price,
        "offers": offers_compact,
        "recent_history_tail": (details.get("history_series") or [])[-15:],
        "specs": details.get("specs"),
        "sweclockers_reviews": extra.get("reviews") or [],
        "sweclockers_member_deals": extra.get("member_deals") or [],
        "external_review_links": details.get("external_review_links") or [],
        "user_requirements": ai_requirements or "Inga extra krav angivna.",
    }

    instructions = (
        "Du är en svensk hårdvaru- och prisexpert. Analysera denna produktdata och prishistorik "
        "och svara ENDAST med giltig JSON (inga extra förklaringar utanför JSON-objektet).\n"
        "JSON-nycklar som krävs:\n"
        '- "score": heltal 1-10 (hur bra köp det är just nu)\n'
        '- "verdict_badge": exakt en av "Köp direkt 🔥", "Bra köp ✅", "Avvakta ⏳", "Undvik ⚠️"\n'
        '- "buy_or_wait": 1-2 korta meningar på svenska om priset mot 30d/all-time och butiksspridning\n'
        '- "spec_check": 1-2 korta meningar på svenska om specifikationer, kvalitet, SweClockers-omdömen och om user_requirements uppfylls\n'
        '- "review_consensus": 1-2 korta meningar på svenska om vad oberoende testare (t.ex. RTINGS, TechPowerUp, Hardware Busters, Notebookcheck, SweClockers) brukar lyfta fram om denna modell/produktserie (styrkor, svagheter och ev. fallgropar)\n'
        '- "matches_requirements": boolean (true om inga angivna user_requirements bryts)\n'
        '- "short_summary": 1 mycket kort mening på svenska anpassad för mobilnotis\n\n'
        f"Produktdata:\n{json.dumps(context_payload, ensure_ascii=False)}"
    )

    raw_reply = _call_ha_ai_task("PrisGeek Produktanalys", instructions)
    parsed = _extract_json(raw_reply)
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected JSON object from AI, got: {parsed}")

    result = {
        "score": int(parsed.get("score") or 5),
        "verdict_badge": str(parsed.get("verdict_badge") or "Avvakta ⏳"),
        "buy_or_wait": str(parsed.get("buy_or_wait") or ""),
        "spec_check": str(parsed.get("spec_check") or ""),
        "review_consensus": str(parsed.get("review_consensus") or ""),
        "matches_requirements": bool(parsed.get("matches_requirements", True)),
        "short_summary": str(parsed.get("short_summary") or ""),
        "external_review_links": details.get("external_review_links") or [],
        "public_id": details["public_id"],
        "product_name": details["name"],
        "current_price": details.get("lowest_price"),
        "analyzed_at": utc_now_iso(),
    }
    if watch_id is not None:
        save_product_ai_verdict(watch_id, result)
    return result


def vet_interest_watch_matches(
    watch: dict[str, Any], candidates: list[dict[str, Any]]
) -> dict[str, Any]:
    """Score and filter top interest-watch candidates against the user's natural-language specs."""
    compact_candidates = [
        {
            "public_id": c.get("public_id"),
            "name": c.get("name"),
            "brand": c.get("brand"),
            "lowest_price": c.get("lowest_price"),
            "best_retailer": c.get("best_retailer"),
            "specs": c.get("specs"),
        }
        for c in candidates[:8]
    ]
    instructions = (
        "Du är en svensk hårdvaruexpert som granskar matchningar för en prisbevakning. "
        "Bedöm varje kandidat utifrån pris och användarens kravspecifikation. "
        "Svara ENDAST med giltig JSON med exakt denna struktur:\n"
        '{"vetted": [{"public_id": 123, "score": 8, "approved": true, "note": "kort svensk kommentar"}], '
        '"best_recommended_public_id": 123, "overall_advice": "1-2 meningar på svenska"}\n\n'
        f"Bevakning: {watch.get('name')} | Sökfråga: {watch.get('search_query')} | Maxpris: {watch.get('target_price')} kr\n"
        f"Användarens AI-kravspec: {watch.get('ai_requirements') or 'Bäst prestanda per krona, undvik lågbudgetfällor.'}\n"
        f"Kandidater:\n{json.dumps(compact_candidates, ensure_ascii=False)}"
    )

    raw_reply = _call_ha_ai_task("PrisGeek Kravgranskning", instructions)
    parsed = _extract_json(raw_reply)
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected JSON object from AI, got: {parsed}")

    result = {
        "vetted": list(parsed.get("vetted") or []),
        "best_recommended_public_id": parsed.get("best_recommended_public_id"),
        "overall_advice": str(parsed.get("overall_advice") or ""),
        "vetted_at": utc_now_iso(),
    }
    if watch.get("id") is not None:
        save_query_ai_summary(int(watch["id"]), result)
    return result


def find_cross_tier_alternatives() -> dict[str, Any]:
    """Scan daily drops and community deals to find cross-tier upgrades near watched budgets."""
    watches = list_all_watches(active_only=True)
    client = PrisGeekClient()
    try:
        prisfall = client.get_prisfall(min_drop_pct=12, limit=30)
        medlemsfynd = client.get_medlemsfynd(limit=25)
    finally:
        client.close()

    watch_summary = {
        "product_watches": [
            {
                "name": w["product_name"],
                "target_price": w["target_price"],
                "current_price": w.get("last_price"),
                "ai_requirements": w.get("ai_requirements", ""),
            }
            for w in (watches.get("product_watches") or [])
        ],
        "interest_watches": [
            {
                "name": q["name"],
                "search_query": q["search_query"],
                "target_price": q["target_price"],
                "best_match_name": q.get("best_match_name"),
                "best_match_price": q.get("best_match_price"),
                "ai_requirements": q.get("ai_requirements", ""),
            }
            for q in (watches.get("query_watches") or [])
        ],
    }

    candidates = {
        "prisfall": [
            {
                "public_id": d.get("public_id"),
                "title": d.get("name"),
                "price": d.get("current_price"),
                "drop_pct": d.get("drop_pct"),
                "url": d.get("url"),
            }
            for d in prisfall
        ],
        "medlemsfynd": [
            {
                "public_id": d.get("public_id"),
                "title": d.get("title"),
                "category": d.get("category"),
                "price": d.get("price"),
                "retailer": d.get("retailer_name"),
                "score": d.get("score"),
                "url": d.get("product_url"),
            }
            for d in medlemsfynd
        ],
    }

    instructions = (
        "Du är en svensk hårdvarurådgivare. Jämför användarens aktiva prisbevakningar med dagens prisfall och medlemsfynd. "
        "Hitta upp till 4 kors-alternativ eller bättre-tier-uppgraderingar (t.ex. snabbare modell/bättre generation i liknande prisklass). "
        "Om inga rimliga alternativ finns i dagens listor, returnera en tom lista i 'alternatives' och beskriv läget kort i 'summary'. "
        "Svara ENDAST med giltig JSON:\n"
        '{"summary": "kort sammanfattning", "alternatives": [{"public_id": 123, "title": "...", "price": 8490, "retailer": "...", "url": "https://...", "for_watch": "...", "reason": "kort förklaring på svenska"}]}\n\n'
        f"Bevakningar:\n{json.dumps(watch_summary, ensure_ascii=False)}\n\n"
        f"Dagens fynd:\n{json.dumps(candidates, ensure_ascii=False)}"
    )

    raw_reply = _call_ha_ai_task("PrisGeek Alternativ", instructions)
    parsed = _extract_json(raw_reply)
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected JSON object from AI, got: {parsed}")

    result = {
        "summary": str(parsed.get("summary") or ""),
        "alternatives": list(parsed.get("alternatives") or []),
        "generated_at": utc_now_iso(),
    }
    set_setting("last_alternatives", result)
    return result


def generate_ai_deal_digest(
    *, custom_profile: str | None = None, send_notification: bool = False
) -> dict[str, Any]:
    """Curate Top 5 daily deals across Prisfall, Medlemsfynd, and Fyndvaror matching user profile."""
    if custom_profile is not None and custom_profile.strip():
        profile = custom_profile.strip()
        set_setting("ai_profile", profile)
    else:
        profile = str(get_setting("ai_profile", DEFAULT_AI_PROFILE) or DEFAULT_AI_PROFILE)

    client = PrisGeekClient()
    try:
        prisfall = client.get_prisfall(min_drop_pct=15, limit=30)
        medlemsfynd = client.get_medlemsfynd(limit=25)
        fyndvaror = client.get_fyndvaror(min_discount_pct=10, limit=20)
    finally:
        client.close()

    pool = {
        "prisfall": [
            {
                "public_id": d.get("public_id"),
                "title": d.get("name"),
                "price": d.get("current_price"),
                "previous_price": d.get("previous_price"),
                "drop_pct": d.get("drop_pct"),
                "url": d.get("url"),
            }
            for d in prisfall
        ],
        "medlemsfynd": [
            {
                "public_id": d.get("public_id"),
                "title": d.get("title"),
                "category": d.get("category"),
                "price": d.get("price"),
                "retailer": d.get("retailer_name"),
                "score": d.get("score"),
                "url": d.get("product_url"),
            }
            for d in medlemsfynd
        ],
        "fyndvaror": [
            {
                "public_id": d.get("public_id"),
                "title": d.get("name"),
                "price": d.get("price"),
                "new_price": d.get("new_price"),
                "discount_pct": d.get("discount_pct"),
                "condition": d.get("condition"),
                "retailer": d.get("retailer"),
                "url": d.get("url"),
            }
            for d in fyndvaror
        ],
    }

    instructions = (
        "Du är en svensk teknikfynd-kurator. Välj ut de Topp 5 mest genuint prisvärda fynden från listan nedan som bäst passar användarens intresseprofil. "
        "Undvik konstiga nischkablar eller uppblåsta falska rabatter. Svara ENDAST med giltig JSON:\n"
        '{"headline": "1 mening sammanfattning på svenska", '
        '"picks": [{"public_id": 123, "title": "...", "price": 990, "retailer": "...", "discount_info": "-35%", "url": "https://...", "score": 9, "reason": "kort svensk motivering"}]}\n\n'
        f"Användarens intresseprofil:\n{profile}\n\n"
        f"Dagens utbud:\n{json.dumps(pool, ensure_ascii=False)}"
    )

    raw_reply = _call_ha_ai_task("PrisGeek Dagens Digest", instructions)
    parsed = _extract_json(raw_reply)
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected JSON object from AI, got: {parsed}")

    picks = list(parsed.get("picks") or [])[:5]
    digest_data = {
        "headline": str(parsed.get("headline") or "Dagens AI-utvalda teknikfynd"),
        "picks": picks,
        "profile_used": profile,
        "generated_at": utc_now_iso(),
    }
    set_setting("last_digest", digest_data)

    if send_notification and picks:
        top = picks[0]
        summary_lines = "; ".join(
            f"{p.get('title')} ({p.get('price')} kr)" for p in picks[:3]
        )
        send_price_alert(
            {
                "product_name": f"AI Deal Digest: {digest_data['headline']}",
                "price": top.get("price"),
                "target_price": top.get("price"),
                "retailer": top.get("retailer") or "PrisGeek",
                "trigger_reason": f"Topp {len(picks)}: {summary_lines}",
                "url": top.get("url") or "https://prisgeek.se/prisfall",
            },
            dry_run=False,
        )

    return digest_data
