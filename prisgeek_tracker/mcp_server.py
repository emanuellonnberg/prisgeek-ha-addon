"""MCP stdio server exposing PrisGeek search, deal discovery, basket optimization, and watch management."""

from __future__ import annotations

import logging
import sys
from typing import Any

import httpx

# Configure logging strictly to stderr so stdout stays clean for JSON-RPC stdio frames.
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[no-redef]

from db import (
    add_query_watch,
    init_db,
    list_all_watches,
    record_price_observation,
    remove_watch as db_remove_watch,
    update_query_watch_results,
    upsert_product_watch,
)
from notifier import load_notifier_config
from prisgeek_client import PrisGeekClient
from watcher import matches_keyword_rules, run_check_cycle

init_db()
mcp = MCPServer("PrisGeek Price Tracker")


def _remote_url() -> str:
    """Return configured remote dashboard/add-on base URL if proxying to Home Assistant."""
    return str(load_notifier_config().get("prisgeek_remote_url") or "").strip().rstrip("/")


def _remote_request(
    method: str,
    path: str,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> Any:
    """Forward an operation to the remote PrisGeek Home Assistant add-on API."""
    base = _remote_url()
    with httpx.Client(timeout=timeout) as client:
        resp = client.request(method, f"{base}{path}", json=json_body, params=params)
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
def search_products(
    query: str,
    max_price: float | None = None,
    in_stock_only: bool = False,
    exclude_refurbished: bool = True,
    limit: int = 15,
) -> list[dict[str, Any]]:
    """Search PrisGeek for products matching a query and return current Swedish store offers."""
    client = PrisGeekClient()
    try:
        return client.search_products(
            query,
            max_price=max_price,
            in_stock_only=in_stock_only,
            exclude_refurbished=exclude_refurbished,
            limit=limit,
        )
    finally:
        client.close()


@mcp.tool()
def inspect_product(public_id_or_url: str) -> dict[str, Any]:
    """Inspect full store offers, specs, and 30-day/all-time/90-day price history for a PrisGeek product."""
    client = PrisGeekClient()
    try:
        return client.get_product_details(public_id_or_url)
    finally:
        client.close()


@mcp.tool()
def browse_prisfall(
    category: str | None = None,
    min_drop_pct: int = 15,
    period_days: int = 7,
    max_price: float | None = None,
    in_stock_only: bool = True,
    sort: str = "procent",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Browse PrisGeek's daily price drops (Prisfall) filtered by category, drop %, and price."""
    client = PrisGeekClient()
    try:
        return client.get_prisfall(
            category=category,
            min_drop_pct=min_drop_pct,
            period_days=period_days,
            in_stock_only=in_stock_only,
            max_price=max_price,
            sort=sort,
            limit=limit,
        )
    finally:
        client.close()


@mcp.tool()
def browse_medlemsfynd(
    category: str | None = None,
    min_score: int = 0,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Browse active SweClockers community member deals (Medlemsfynd) on PrisGeek."""
    client = PrisGeekClient()
    try:
        return client.get_medlemsfynd(
            category=category,
            min_score=min_score,
            limit=limit,
        )
    finally:
        client.close()


@mcp.tool()
def browse_fyndvaror(
    query: str | None = None,
    min_discount_pct: int = 10,
    max_price: float | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Browse open-box, demo, and outlet bargains (Fyndvaror) on PrisGeek."""
    client = PrisGeekClient()
    try:
        return client.get_fyndvaror(
            query=query,
            min_discount_pct=min_discount_pct,
            max_price=max_price,
            limit=limit,
        )
    finally:
        client.close()


@mcp.tool()
def optimize_shopping_basket(public_ids: list[int] | None = None) -> dict[str, Any]:
    """Compare split-store cheapest totals vs. single-store consolidation incl. shipping for watched items."""
    if _remote_url() and not public_ids:
        return _remote_request("GET", "/api/basket")

    target_ids = list(public_ids or [])
    if not target_ids:
        watches = list_watches()
        for w in watches.get("product_watches") or []:
            if w.get("public_id"):
                target_ids.append(int(w["public_id"]))
        for q in watches.get("query_watches") or []:
            if q.get("best_match_public_id"):
                target_ids.append(int(q["best_match_public_id"]))

    client = PrisGeekClient()
    try:
        return client.optimize_basket(target_ids)
    finally:
        client.close()


@mcp.tool()
def add_product_watch(
    public_id_or_url: str,
    target_price: float,
    in_stock_only: bool = True,
    exclude_refurbished: bool = True,
    alert_on_all_time_low: bool = True,
    alert_below_30d_pct: float = 0.0,
    ai_requirements: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Add or update a target-price & Smart Deal watch for a specific PrisGeek product ID or URL."""
    if _remote_url():
        return _remote_request(
            "POST",
            "/api/watches/product",
            {
                "public_id_or_url": public_id_or_url,
                "target_price": target_price,
                "in_stock_only": in_stock_only,
                "exclude_refurbished": exclude_refurbished,
                "alert_on_all_time_low": alert_on_all_time_low,
                "alert_below_30d_pct": alert_below_30d_pct,
                "ai_requirements": ai_requirements,
                "notes": notes,
            },
        )

    client = PrisGeekClient()
    try:
        details = client.get_product_details(
            public_id_or_url,
            in_stock_only=in_stock_only,
            exclude_refurbished=exclude_refurbished,
        )
    finally:
        client.close()

    pid = int(details["public_id"])
    current_price = details.get("lowest_price")
    watch = upsert_product_watch(
        public_id=pid,
        product_uuid=details.get("uuid"),
        product_name=details["name"],
        product_url=details["url"],
        target_price=target_price,
        in_stock_only=in_stock_only,
        exclude_refurbished=exclude_refurbished,
        alert_on_all_time_low=alert_on_all_time_low,
        alert_below_30d_pct=alert_below_30d_pct,
        ai_requirements=ai_requirements,
        last_price=current_price,
        last_total_price=details.get("lowest_total_price"),
        last_retailer=details.get("best_retailer"),
        last_store_url=details.get("best_store_url"),
        lowest_30d_price=details.get("lowest_30d_price"),
        lowest_all_time_price=details.get("lowest_all_time_price"),
        notes=notes,
    )

    if current_price is not None:
        record_price_observation(
            public_id=pid,
            product_name=details["name"],
            price=current_price,
            total_price=details.get("lowest_total_price"),
            retailer=details.get("best_retailer"),
            in_stock=bool(details.get("in_stock")),
        )

    is_below_target = current_price is not None and current_price <= float(target_price)
    return {
        "watch": watch,
        "public_id": pid,
        "product_name": details["name"],
        "url": details["url"],
        "current_price": current_price,
        "current_total_price": details.get("lowest_total_price"),
        "best_retailer": details.get("best_retailer"),
        "best_store_url": details.get("best_store_url"),
        "target_price": float(target_price),
        "already_below_target": is_below_target,
        "lowest_30d_price": details.get("lowest_30d_price"),
        "lowest_all_time_price": details.get("lowest_all_time_price"),
        "deal_assessment": details.get("deal_assessment"),
    }


@mcp.tool()
def add_interest_watch(
    name: str,
    search_query: str,
    target_price: float,
    must_include_keywords: list[str] | None = None,
    exclude_keywords: list[str] | None = None,
    in_stock_only: bool = True,
    include_outlet: bool = False,
    ai_requirements: str = "",
) -> dict[str, Any]:
    """Create a keyword/interest discovery watch and immediately populate best & top-5 matching products."""
    if _remote_url():
        return _remote_request(
            "POST",
            "/api/watches/interest",
            {
                "name": name,
                "search_query": search_query,
                "target_price": target_price,
                "must_include_keywords": must_include_keywords or [],
                "exclude_keywords": exclude_keywords or [],
                "in_stock_only": in_stock_only,
                "include_outlet": include_outlet,
                "ai_requirements": ai_requirements,
            },
        )

    watch = add_query_watch(
        name=name,
        search_query=search_query,
        target_price=target_price,
        must_include_keywords=must_include_keywords,
        exclude_keywords=exclude_keywords,
        in_stock_only=in_stock_only,
        exclude_refurbished=not include_outlet,
        include_outlet=include_outlet,
        ai_requirements=ai_requirements,
    )

    client = PrisGeekClient()
    try:
        candidates = client.search_products(
            search_query,
            in_stock_only=in_stock_only,
            exclude_refurbished=not include_outlet,
            limit=60,
        )
    finally:
        client.close()

    must_kw = watch.get("must_include_keywords") or []
    excl_kw = watch.get("exclude_keywords") or []
    filtered = [
        item for item in candidates if matches_keyword_rules(item, must_kw, excl_kw)
    ]
    filtered.sort(key=lambda item: float(item["lowest_price"]))

    top_preview = [
        {
            "public_id": m["public_id"],
            "name": m["name"],
            "lowest_price": m["lowest_price"],
            "lowest_total_price": m.get("lowest_total_price"),
            "best_retailer": m.get("best_retailer"),
            "store_url": m.get("best_store_url"),
            "url": m["url"],
        }
        for m in filtered[:5]
    ]
    update_query_watch_results(
        watch["id"],
        match_count=len(filtered),
        best_match=filtered[0] if filtered else None,
        top_matches=top_preview,
    )

    deals_now = [
        item
        for item in filtered
        if item.get("lowest_price") is not None
        and float(item["lowest_price"]) <= float(target_price)
    ]

    return {
        "watch": watch,
        "current_matches": filtered[:10],
        "deals_below_target_now": deals_now,
    }


@mcp.tool()
def list_watches() -> dict[str, Any]:
    """List all active product watches, interest discovery rules, and recent alerts."""
    if _remote_url():
        data = _remote_request("GET", "/api/state")
        return data.get("watches", data)
    return list_all_watches(active_only=True)


@mcp.tool()
def remove_watch(watch_type: str, watch_id: int) -> dict[str, Any]:
    """Deactivate a watch rule by watch_type ('product' or 'query') and watch_id."""
    if _remote_url():
        res = _remote_request("DELETE", f"/api/watches/{watch_type}/{watch_id}")
        return {"removed": bool(res.get("removed")), "watch_type": watch_type, "watch_id": watch_id}
    removed = db_remove_watch(watch_type, watch_id)
    return {"removed": removed, "watch_type": watch_type, "watch_id": watch_id}


@mcp.tool()
def run_price_check_now(send_webhooks: bool = False) -> dict[str, Any]:
    """Run a live price check across all active watches right now and return the report."""
    if _remote_url():
        return _remote_request("POST", "/api/check", {"send_webhooks": send_webhooks})
    return run_check_cycle(send_webhooks=send_webhooks)


@mcp.tool()
def ai_analyze_product(
    public_id_or_url: str,
    target_price: float | None = None,
    ai_requirements: str = "",
) -> dict[str, Any]:
    """Run an AI evaluation of price trajectory, specs, SweClockers awards, and user criteria."""
    if _remote_url():
        return _remote_request(
            "POST",
            "/api/ai/analyze-product",
            {
                "public_id": public_id_or_url,
                "target_price": target_price,
                "ai_requirements": ai_requirements,
            },
            timeout=60.0,
        )
    from ai_advisor import analyze_product_deal

    return analyze_product_deal(
        public_id_or_url,
        target_price=target_price,
        ai_requirements=ai_requirements,
    )


@mcp.tool()
def ai_generate_deal_digest(
    profile: str | None = None,
    send_notification: bool = False,
) -> dict[str, Any]:
    """Generate a curated Top 5 AI deal digest across Prisfall, Medlemsfynd, and Fyndvaror."""
    if _remote_url():
        return _remote_request(
            "POST",
            "/api/ai/digest",
            {"profile": profile, "send_notification": send_notification},
            timeout=60.0,
        )
    from ai_advisor import generate_ai_deal_digest

    return generate_ai_deal_digest(
        custom_profile=profile,
        send_notification=send_notification,
    )


@mcp.tool()
def ai_find_alternatives() -> dict[str, Any]:
    """Scan today's price drops and member deals for cross-tier upgrades matching active watches."""
    if _remote_url():
        return _remote_request("POST", "/api/ai/alternatives", {}, timeout=60.0)
    from ai_advisor import find_cross_tier_alternatives

    return find_cross_tier_alternatives()


@mcp.tool()
def ai_suggest_similar_products(
    public_id_or_url: str,
    direction: str = "same_tier",
    custom_prompt: str = "",
    max_price: float | None = None,
    in_stock_only: bool = True,
) -> dict[str, Any]:
    """Suggest live PrisGeek alternatives for a product tailored to direction ('cheaper', 'same_tier', 'upgrade', 'quality', 'custom') and custom requirements."""
    if _remote_url():
        return _remote_request(
            "POST",
            "/api/ai/similar-products",
            {
                "public_id": public_id_or_url,
                "direction": direction,
                "custom_prompt": custom_prompt,
                "max_price": max_price,
                "in_stock_only": in_stock_only,
            },
            timeout=90.0,
        )
    from ai_advisor import suggest_similar_products

    return suggest_similar_products(
        public_id_or_url,
        direction=direction,
        custom_prompt=custom_prompt,
        max_price=max_price,
        in_stock_only=in_stock_only,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
