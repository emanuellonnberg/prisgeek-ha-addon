"""Standalone headless watcher for PrisGeek product watches and interest discovery queries."""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from typing import Any

from db import (
    get_recent_history,
    init_db,
    list_all_watches,
    log_alert,
    record_price_observation,
    should_send_alert,
    update_product_watch_state,
    update_query_watch_results,
)
from notifier import push_ha_sensors, send_price_alert
from prisgeek_client import PrisGeekClient, _token_variants

logger = logging.getLogger(__name__)


def matches_keyword_rules(
    product: dict[str, Any],
    must_include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> bool:
    """Check whether a product matches all required keywords and avoids excluded keywords."""
    blob_parts = [
        str(product.get("name") or ""),
        str(product.get("brand") or ""),
        str(product.get("model") or ""),
        str(product.get("mpn") or ""),
        str(product.get("category") or ""),
    ]
    if product.get("specs"):
        blob_parts.append(json.dumps(product["specs"], ensure_ascii=False))
    raw_blob = " ".join(blob_parts).lower()
    compact_blob = re.sub(
        r"(\d+(?:[.,]\d+)?)\s+(gb|tb|mb|kb|w|mhz|ghz|hz|mm|cm)\b",
        r"\1\2",
        raw_blob,
    )
    blob = f"{raw_blob} {compact_blob}"

    for kw in must_include or []:
        variants = _token_variants(kw.strip().lower())
        if not any(v in blob for v in variants):
            return False
    for kw in exclude or []:
        variants = _token_variants(kw.strip().lower())
        if any(v in blob for v in variants):
            return False
    return True


def run_check_cycle(*, send_webhooks: bool = True) -> dict[str, Any]:
    """Run one complete check across all active product and query watches."""
    init_db()
    watches = list_all_watches(active_only=True)
    product_watches = watches["product_watches"]
    query_watches = watches["query_watches"]

    summary: dict[str, Any] = {
        "checked_product_watches": 0,
        "checked_query_watches": 0,
        "price_changes": [],
        "triggered_alerts": [],
        "matches_found": [],
        "errors": [],
    }

    client = PrisGeekClient()
    try:
        for watch in product_watches:
            summary["checked_product_watches"] += 1
            pid = int(watch["public_id"])
            try:
                details = client.get_product_details(
                    pid,
                    in_stock_only=bool(watch["in_stock_only"]),
                    exclude_refurbished=bool(watch["exclude_refurbished"]),
                )
            except Exception as exc:
                err_msg = f"Product watch {watch['id']} (public_id={pid}) failed: {exc}"
                logger.warning(err_msg)
                summary["errors"].append(err_msg)
                continue

            current_price = details.get("lowest_price")
            total_price = details.get("lowest_total_price")
            retailer = details.get("best_retailer")
            in_stock = bool(details.get("in_stock"))

            changed = record_price_observation(
                public_id=pid,
                product_name=details["name"],
                price=current_price,
                total_price=total_price,
                retailer=retailer,
                in_stock=in_stock,
            )
            if changed:
                summary["price_changes"].append(
                    {
                        "public_id": pid,
                        "product_name": details["name"],
                        "old_price": watch.get("last_price"),
                        "new_price": current_price,
                        "retailer": retailer,
                    }
                )

            alerted_price: float | None = None
            trigger_reason: str | None = None
            if current_price is not None:
                if current_price <= float(watch["target_price"]):
                    trigger_reason = "Under målpris"
                elif (
                    bool(watch.get("alert_on_all_time_low", 1))
                    and details.get("lowest_all_time_price")
                    and current_price <= float(details["lowest_all_time_price"])
                ):
                    trigger_reason = "Rekordlågt (Lägsta pris någonsin)"
                elif (
                    float(watch.get("alert_below_30d_pct") or 0) > 0
                    and details.get("lowest_30d_price")
                    and current_price
                    <= float(details["lowest_30d_price"])
                    * (1.0 - float(watch["alert_below_30d_pct"]) / 100.0)
                ):
                    trigger_reason = (
                        f"-{float(watch['alert_below_30d_pct']):.0f}% under 30d-lägsta"
                    )

            if (
                current_price is not None
                and trigger_reason is not None
                and should_send_alert("product", watch["id"], pid, current_price)
            ):
                ai_note = ""
                if watch.get("ai_requirements") or send_webhooks:
                    try:
                        from ai_advisor import analyze_product_deal

                        verdict = analyze_product_deal(
                            pid,
                            target_price=float(watch["target_price"]),
                            ai_requirements=str(watch.get("ai_requirements") or ""),
                            watch_id=int(watch["id"]),
                        )
                        if watch.get("ai_requirements") and verdict.get("matches_requirements") is False:
                            logger.info(
                                "Suppressed alert for %s due to AI requirement check: %s",
                                details["name"],
                                verdict.get("spec_check"),
                            )
                            trigger_reason = None
                        elif verdict.get("short_summary"):
                            badge = verdict.get("verdict_badge") or ""
                            ai_note = f"{badge}: {verdict['short_summary']}".strip()
                    except Exception as exc:
                        logger.debug("AI product vetting skipped for %s: %s", pid, exc)

            if (
                current_price is not None
                and trigger_reason is not None
                and should_send_alert("product", watch["id"], pid, current_price)
            ):
                full_reason = f"{trigger_reason} — {ai_note}" if ai_note else trigger_reason
                alert_payload = {
                    "watch_type": "product",
                    "watch_id": watch["id"],
                    "public_id": pid,
                    "product_name": details["name"],
                    "price": current_price,
                    "total_price": total_price,
                    "target_price": watch["target_price"],
                    "retailer": retailer,
                    "lowest_30d_price": details.get("lowest_30d_price"),
                    "lowest_all_time_price": details.get("lowest_all_time_price"),
                    "deal_assessment": details.get("deal_assessment"),
                    "trigger_reason": full_reason,
                    "url": details["url"],
                    "store_url": details.get("best_store_url"),
                }
                notify_res = send_price_alert(alert_payload, dry_run=not send_webhooks)
                log_alert(
                    watch_type="product",
                    watch_id=watch["id"],
                    public_id=pid,
                    product_name=details["name"],
                    price=current_price,
                    retailer=retailer,
                )
                alerted_price = current_price
                summary["triggered_alerts"].append({**alert_payload, "notify": notify_res})

            update_product_watch_state(
                watch_id=watch["id"],
                last_price=current_price,
                last_total_price=total_price,
                last_retailer=retailer,
                last_store_url=details.get("best_store_url"),
                lowest_30d_price=details.get("lowest_30d_price"),
                lowest_all_time_price=details.get("lowest_all_time_price"),
                last_alerted_price=alerted_price,
            )
            time.sleep(0.5)

        for qwatch in query_watches:
            summary["checked_query_watches"] += 1
            include_outlet = bool(qwatch.get("include_outlet", 0))
            try:
                candidates = client.search_products(
                    qwatch["search_query"],
                    max_price=None,
                    in_stock_only=bool(qwatch["in_stock_only"]),
                    exclude_refurbished=not include_outlet,
                    limit=60,
                )
            except Exception as exc:
                err_msg = f"Query watch {qwatch['id']} ({qwatch['name']}) failed: {exc}"
                logger.warning(err_msg)
                summary["errors"].append(err_msg)
                continue

            must_kw = qwatch.get("must_include_keywords") or []
            excl_kw = qwatch.get("exclude_keywords") or []

            matched_items = [
                item
                for item in candidates
                if item.get("lowest_price") is not None
                and matches_keyword_rules(item, must_kw, excl_kw)
            ]
            matched_items.sort(key=lambda item: float(item["lowest_price"]))

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
                for m in matched_items[:5]
            ]

            vetted_map: dict[int, dict[str, Any]] = {}
            if qwatch.get("ai_requirements") and matched_items:
                try:
                    from ai_advisor import vet_interest_watch_matches

                    ai_res = vet_interest_watch_matches(qwatch, matched_items[:8])
                    for entry in ai_res.get("vetted") or []:
                        if entry.get("public_id") is not None:
                            vetted_map[int(entry["public_id"])] = entry
                    for tp in top_preview:
                        v = vetted_map.get(int(tp["public_id"]))
                        if v:
                            tp["ai_score"] = v.get("score")
                            tp["ai_approved"] = v.get("approved")
                            tp["ai_note"] = v.get("note")
                except Exception as exc:
                    logger.debug("AI interest vetting skipped for %s: %s", qwatch["id"], exc)

            update_query_watch_results(
                qwatch["id"],
                match_count=len(matched_items),
                best_match=matched_items[0] if matched_items else None,
                top_matches=top_preview,
            )

            if matched_items:
                best = matched_items[0]
                record_price_observation(
                    public_id=int(best["public_id"]),
                    product_name=best["name"],
                    price=best["lowest_price"],
                    total_price=best.get("lowest_total_price"),
                    retailer=best.get("best_retailer"),
                    in_stock=bool(best.get("in_stock")),
                )

            for item in matched_items:
                pid = int(item["public_id"])
                price = float(item["lowest_price"])
                if price > float(qwatch["target_price"]):
                    continue

                v = vetted_map.get(pid)
                if v and v.get("approved") is False:
                    logger.info(
                        "Suppressed interest alert for %s due to AI vetting: %s",
                        item["name"],
                        v.get("note"),
                    )
                    continue

                record_price_observation(
                    public_id=pid,
                    product_name=item["name"],
                    price=price,
                    total_price=item.get("lowest_total_price"),
                    retailer=item.get("best_retailer"),
                    in_stock=bool(item.get("in_stock")),
                )

                summary["matches_found"].append(
                    {
                        "watch_id": qwatch["id"],
                        "watch_name": qwatch["name"],
                        "public_id": pid,
                        "product_name": item["name"],
                        "price": price,
                        "retailer": item.get("best_retailer"),
                        "url": item["url"],
                    }
                )

                if should_send_alert("query", qwatch["id"], pid, price):
                    ai_suffix = f" — AI ({v.get('score')}/10): {v.get('note')}" if v and v.get("note") else ""
                    alert_payload = {
                        "watch_type": "query",
                        "watch_id": qwatch["id"],
                        "watch_name": qwatch["name"],
                        "public_id": pid,
                        "product_name": item["name"],
                        "price": price,
                        "total_price": item.get("lowest_total_price"),
                        "target_price": qwatch["target_price"],
                        "retailer": item.get("best_retailer"),
                        "trigger_reason": f"Intressebevakning under maxpris{ai_suffix}",
                        "url": item["url"],
                        "store_url": item.get("best_store_url"),
                    }
                    notify_res = send_price_alert(alert_payload, dry_run=not send_webhooks)
                    log_alert(
                        watch_type="query",
                        watch_id=qwatch["id"],
                        public_id=pid,
                        product_name=item["name"],
                        price=price,
                        retailer=item.get("best_retailer"),
                    )
                    summary["triggered_alerts"].append({**alert_payload, "notify": notify_res})

            time.sleep(0.5)
    finally:
        client.close()

    summary["ha_sensors"] = push_ha_sensors(list_all_watches(active_only=True))
    return summary


def print_status() -> None:
    """Print tracked products, discovery queries, and recent price history."""
    data = list_all_watches(active_only=True)
    history = get_recent_history(limit=10)

    print("=== Active Product Watches ===")
    if not data["product_watches"]:
        print("  (none)")
    for w in data["product_watches"]:
        last = f"{w['last_price']:.0f} kr" if w["last_price"] is not None else "N/A"
        print(
            f"  [#{w['id']}] {w['product_name']} (ID {w['public_id']}) | "
            f"Current: {last} ({w['last_retailer'] or 'N/A'}) | "
            f"Target: <= {w['target_price']:.0f} kr | "
            f"All-time low: {w['lowest_all_time_price'] or 'N/A'}"
        )

    print("\n=== Active Interest / Discovery Watches ===")
    if not data["query_watches"]:
        print("  (none)")
    for q in data["query_watches"]:
        print(
            f"  [#{q['id']}] {q['name']} | Query: '{q['search_query']}' | "
            f"Target: <= {q['target_price']:.0f} kr | "
            f"Include: {q['must_include_keywords']} | Exclude: {q['exclude_keywords']}"
        )

    print("\n=== Recent Price History (Last 10) ===")
    if not history:
        print("  (none)")
    for h in history:
        print(
            f"  {h['recorded_at']} | {h['product_name']} ({h['public_id']}) -> "
            f"{h['price']:.0f} kr @ {h['retailer'] or 'N/A'}"
        )


def main() -> None:
    """CLI entry point for running PrisGeek watcher checks or viewing status."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="PrisGeek headless price watcher")
    parser.add_argument("--once", action="store_true", help="Run a single check cycle (default)")
    parser.add_argument("--loop", action="store_true", help="Run continuously on an interval")
    parser.add_argument(
        "--interval",
        type=int,
        default=3600,
        help="Seconds between checks when --loop is used (default: 3600)",
    )
    parser.add_argument(
        "--no-webhook",
        action="store_true",
        help="Do not send HTTP webhook notifications",
    )
    parser.add_argument("--status", action="store_true", help="Display active watches and exit")
    args = parser.parse_args()

    if args.status:
        print_status()
        return

    send_webhooks = not args.no_webhook
    if args.loop:
        logger.info("Starting PrisGeek watcher loop (interval=%ss)", args.interval)
        while True:
            report = run_check_cycle(send_webhooks=send_webhooks)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            time.sleep(max(60, args.interval))
    else:
        report = run_check_cycle(send_webhooks=send_webhooks)
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
