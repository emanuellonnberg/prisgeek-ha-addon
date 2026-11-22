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

SIMILAR_DIRECTIONS: dict[str, tuple[str, str, float, float]] = {
    "cheaper": (
        "Billigare alternativ",
        "Hitta billigare likvärdiga alternativ som behåller de viktigaste egenskaperna men kostar märkbart mindre.",
        0.40,
        0.96,
    ),
    "same_tier": (
        "Bättre värde i samma prisklass",
        "Hitta starkare konkurrenter i ungefär samma prisklass med bättre prestanda, omdömen eller valuta för pengarna.",
        0.75,
        1.18,
    ),
    "upgrade": (
        "Steget upp (uppgradering)",
        "Hitta tydliga uppgraderingar ett steg upp i prestanda, kapacitet, specifikationer eller generation som är prisvärda.",
        1.00,
        1.65,
    ),
    "quality": (
        "Tystare / högre kvalitet",
        "Prioritera lägre ljudnivå, bättre kylning, byggkvalitet, komponenter, hållbarhet eller längre garanti.",
        0.75,
        1.45,
    ),
    "custom": (
        "Anpassad riktning",
        "Följ användarens egna önskemål noggrant.",
        0.40,
        1.80,
    ),
}


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


def _plan_similar_search_queries(
    source_info: dict[str, Any],
    direction_label: str,
    guidance: str,
    custom_prompt: str,
) -> list[str]:
    """Ask HA AI for 3-5 concise PrisGeek search queries targeting competing models."""
    instructions = (
        "Du hjälper användaren hitta liknande eller konkurrerande produkter i en svensk prisdatabas (PrisGeek).\n"
        "Utifrån källprodukten och användarens önskade riktning, ta fram 3 till 5 korta söktermer "
        "(2–4 ord per sökning, bara produktnamn/serie/nyckelspec – inga priser eller butiksnamn) som hittar relevanta konkurrenter eller rätt uppgraderingar.\n"
        'Svara ENDAST med giltig JSON: {"queries": ["sökterm 1", "sökterm 2", "sökterm 3"]}\n\n'
        f"Källprodukt: {json.dumps(source_info, ensure_ascii=False)}\n"
        f"Riktning: {direction_label} ({guidance})\n"
        f"Extra önskemål från användaren: {custom_prompt or 'Inga extra önskemål.'}"
    )
    try:
        raw_reply = _call_ha_ai_task("PrisGeek Liknande Sökplan", instructions)
        parsed = _extract_json(raw_reply)
        if isinstance(parsed, dict) and isinstance(parsed.get("queries"), list):
            queries = [str(q).strip() for q in parsed["queries"] if str(q).strip()]
            return list(dict.fromkeys(queries))[:5]
    except Exception as exc:
        logger.debug("Stage 1 similar query planning skipped: %s", exc)
    return []


def _collect_similar_candidates(
    client: PrisGeekClient,
    details: dict[str, Any],
    queries: list[str],
    *,
    min_p: float | None,
    max_p: float | None,
    explicit_max_price: float | None,
    in_stock_only: bool,
) -> list[dict[str, Any]]:
    """Gather deduplicated live PrisGeek candidates from AI search queries and category peers."""
    target_pid = int(details["public_id"])
    by_pid: dict[int, dict[str, Any]] = {}

    def _add(items: list[dict[str, Any]], *, from_search: bool) -> None:
        for item in items:
            raw_pid = item.get("public_id")
            if raw_pid is None:
                continue
            pid = int(raw_pid)
            if pid == target_pid:
                continue
            existing = by_pid.get(pid)
            if existing is None:
                entry = dict(item)
                entry["_from_search"] = from_search
                by_pid[pid] = entry
            elif from_search and not existing.get("_from_search"):
                existing["_from_search"] = True

    for q in queries[:5]:
        try:
            hits = client.search_products(
                q,
                max_price=max_p,
                in_stock_only=in_stock_only,
                exclude_refurbished=True,
                limit=12,
            )
            _add(hits, from_search=True)
        except Exception as exc:
            logger.debug("Similar candidate search failed for %r: %s", q, exc)

    try:
        peers = client.find_category_peers(
            details.get("category_id"),
            subcategory=details.get("subcategory"),
            min_price=min_p,
            max_price=max_p,
            exclude_public_id=target_pid,
            in_stock_only=in_stock_only,
            exclude_refurbished=True,
            limit=25,
        )
        _add(peers, from_search=False)
    except Exception as exc:
        logger.debug("Category peer lookup failed for %s: %s", target_pid, exc)

    if len(by_pid) < 4:
        try:
            broad_peers = client.find_category_peers(
                details.get("category_id"),
                subcategory=details.get("subcategory"),
                min_price=None,
                max_price=explicit_max_price,
                exclude_public_id=target_pid,
                in_stock_only=in_stock_only,
                exclude_refurbished=True,
                limit=25,
            )
            _add(broad_peers, from_search=False)
        except Exception as exc:
            logger.debug("Broad category fallback failed for %s: %s", target_pid, exc)

    ranked = sorted(
        by_pid.values(),
        key=lambda item: (
            not bool(item.get("_from_search")),
            not bool(item.get("in_stock")),
            -min(int(item.get("offer_count") or 0), 12),
            float(item.get("lowest_price") or 0.0),
        ),
    )
    return ranked[:28]


def suggest_similar_products(
    public_id_or_url: str | int,
    *,
    direction: str = "same_tier",
    custom_prompt: str = "",
    max_price: float | None = None,
    in_stock_only: bool = True,
) -> dict[str, Any]:
    """Find and rank live PrisGeek product alternatives tailored to a chosen direction and criteria."""
    dir_key = direction.strip().lower() if direction else "same_tier"
    if dir_key not in SIMILAR_DIRECTIONS:
        dir_key = "custom" if custom_prompt.strip() else "same_tier"
    direction_label, guidance, min_mult, max_mult = SIMILAR_DIRECTIONS[dir_key]
    clean_custom = (custom_prompt or "").strip()

    client = PrisGeekClient()
    try:
        details = client.get_product_details(public_id_or_url)
        source_price = details.get("lowest_price")
        min_p: float | None = round(float(source_price) * min_mult, 2) if source_price else None
        max_p: float | None = round(float(source_price) * max_mult, 2) if source_price else None
        if max_price is not None:
            cap = float(max_price)
            max_p = min(max_p, cap) if max_p is not None else cap
            if min_p is not None and min_p > cap:
                min_p = max(0.0, cap * 0.5)

        source_info = {
            "public_id": details["public_id"],
            "name": details["name"],
            "brand": details.get("brand"),
            "category": details.get("category"),
            "subcategory": details.get("subcategory"),
            "lowest_price_sek": source_price,
            "specs": details.get("specs"),
        }

        queries = _plan_similar_search_queries(
            source_info, direction_label, guidance, clean_custom
        )
        candidates = _collect_similar_candidates(
            client,
            details,
            queries,
            min_p=min_p,
            max_p=max_p,
            explicit_max_price=float(max_price) if max_price is not None else None,
            in_stock_only=in_stock_only,
        )
    finally:
        client.close()

    source_summary = {
        "public_id": details["public_id"],
        "name": details["name"],
        "brand": details.get("brand"),
        "category": details.get("category"),
        "lowest_price": source_price,
        "best_retailer": details.get("best_retailer"),
        "url": details.get("url"),
    }

    if not candidates:
        return {
            "source_product": source_summary,
            "direction": dir_key,
            "direction_label": direction_label,
            "custom_prompt": clean_custom,
            "summary": "Inga tillgängliga alternativ hittades i PrisGeeks katalog inom valt prisintervall.",
            "search_queries_used": queries,
            "candidate_pool_size": 0,
            "suggestions": [],
            "generated_at": utc_now_iso(),
        }

    candidates_by_pid = {int(c["public_id"]): c for c in candidates}
    compact_candidates = [
        {
            "public_id": c["public_id"],
            "name": c["name"],
            "brand": c.get("brand"),
            "category": c.get("category"),
            "lowest_price_sek": c.get("lowest_price"),
            "best_retailer": c.get("best_retailer"),
            "in_stock": c.get("in_stock"),
            "offer_count": c.get("offer_count"),
            "specs": c.get("specs"),
        }
        for c in candidates
    ]

    instructions = (
        "Du är en svensk hårdvarurådgivare. Välj ut upp till 5 av de BÄSTA liknande produkterna/alternativen "
        "från kandidatlistan utifrån användarens riktning och extra önskemål.\n"
        "REGLER:\n"
        "- Välj ENDAST public_id som finns exakt i kandidatlistan (hitta aldrig på egna produkter eller priser).\n"
        "- Rensa bort irrelevanta tillbehör, kablar eller fel produktkategori.\n"
        "- Svara ENDAST med giltig JSON i exakt detta format:\n"
        '{"summary": "1-2 korta meningar på svenska som sammanfattar rekommendationen", '
        '"suggestions": [{"public_id": 123, "match_score": 9, "tag": "Bäst för kraven", '
        '"why_it_fits": "1-2 meningar om varför den passar riktningen", '
        '"tradeoff": "1 kort mening om kompromiss/skillnad mot originalet"}]}\n\n'
        f"Källprodukt:\n{json.dumps(source_info, ensure_ascii=False)}\n\n"
        f"Riktning: {direction_label} — {guidance}\n"
        f"Användarens extra önskemål: {clean_custom or 'Inga extra önskemål.'}\n\n"
        f"Kandidater (live från PrisGeek):\n{json.dumps(compact_candidates, ensure_ascii=False)}"
    )

    raw_reply = _call_ha_ai_task("PrisGeek Liknande Alternativ", instructions)
    parsed = _extract_json(raw_reply)
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected JSON object from AI, got: {parsed}")

    suggestions: list[dict[str, Any]] = []
    seen_out: set[int] = set()
    for raw_sug in (parsed.get("suggestions") or []):
        if not isinstance(raw_sug, dict) or raw_sug.get("public_id") is None:
            continue
        try:
            pid = int(raw_sug["public_id"])
        except (TypeError, ValueError):
            continue
        cand = candidates_by_pid.get(pid)
        if not cand or pid in seen_out:
            continue
        seen_out.add(pid)

        cand_price = cand.get("lowest_price")
        diff_kr: float | None = None
        diff_pct: float | None = None
        if cand_price is not None and source_price:
            diff_kr = round(float(cand_price) - float(source_price), 2)
            diff_pct = round(diff_kr / float(source_price) * 100.0, 1)

        suggestions.append(
            {
                "public_id": pid,
                "name": cand["name"],
                "brand": cand.get("brand"),
                "category": cand.get("category"),
                "lowest_price": cand_price,
                "lowest_total_price": cand.get("lowest_total_price"),
                "best_retailer": cand.get("best_retailer"),
                "best_store_url": cand.get("best_store_url"),
                "url": cand["url"],
                "in_stock": cand.get("in_stock"),
                "offer_count": cand.get("offer_count"),
                "external_review_links": cand.get("external_review_links") or [],
                "price_diff_kr": diff_kr,
                "price_diff_pct": diff_pct,
                "match_score": int(raw_sug.get("match_score") or 8),
                "tag": str(raw_sug.get("tag") or direction_label),
                "why_it_fits": str(raw_sug.get("why_it_fits") or ""),
                "tradeoff": str(raw_sug.get("tradeoff") or ""),
            }
        )
        if len(suggestions) >= 5:
            break

    return {
        "source_product": source_summary,
        "direction": dir_key,
        "direction_label": direction_label,
        "custom_prompt": clean_custom,
        "summary": str(parsed.get("summary") or ""),
        "search_queries_used": queries,
        "candidate_pool_size": len(candidates),
        "suggestions": suggestions,
        "generated_at": utc_now_iso(),
    }


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
