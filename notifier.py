"""Home Assistant notifications, webhooks, and native sensor updates for PrisGeek."""

from __future__ import annotations

from datetime import datetime, time as dt_time, timezone
import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
ADDON_OPTIONS_PATH = Path("/data/options.json")
DEFAULT_HA_WEBHOOK_URL = "http://homeassistant.local:8123/api/webhook/black_friday_alert"
DEFAULT_QUIET_HOURS: dict[str, Any] = {
    "enabled": True,
    "start": "22:00",
    "end": "07:30",
    "mode": "defer",
    "timezone": "Europe/Stockholm",
}


def _parse_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Could not parse %s: %s", path, exc)
        return {}


def _get_ha_token(file_cfg: dict[str, Any] | None = None) -> str:
    """Return Supervisor or Long-Lived Access Token from env or config without exposing it in API responses."""
    sup_token = os.environ.get("SUPERVISOR_TOKEN", "").strip()
    if sup_token:
        return sup_token
    env_token = os.environ.get("HA_TOKEN", "").strip()
    if env_token:
        return env_token
    if file_cfg is None:
        file_cfg = {**_read_json_file(CONFIG_PATH), **_read_json_file(ADDON_OPTIONS_PATH)}
    return str(file_cfg.get("HA_TOKEN", "") or "").strip()


def load_notifier_config() -> dict[str, Any]:
    """Load webhook, Home Assistant API, and add-on settings from options.json, config.json, and env."""
    file_cfg = _read_json_file(CONFIG_PATH)
    addon_cfg = _read_json_file(ADDON_OPTIONS_PATH)
    merged = {**file_cfg, **addon_cfg}

    webhook_id = str(merged.get("webhook_id", "") or "").strip()
    explicit_ha_url = os.environ.get("HA_WEBHOOK_URL") or file_cfg.get("HA_WEBHOOK_URL")
    if explicit_ha_url:
        ha_url = str(explicit_ha_url).strip()
    elif webhook_id:
        ha_url = f"http://homeassistant.local:8123/api/webhook/{webhook_id}"
    else:
        ha_url = DEFAULT_HA_WEBHOOK_URL

    sup_token = os.environ.get("SUPERVISOR_TOKEN", "").strip()
    use_ha_webhook = bool(not sup_token or os.environ.get("FORCE_HA_WEBHOOK"))

    extra_url = str(
        os.environ.get("EXTRA_WEBHOOK_URL", merged.get("EXTRA_WEBHOOK_URL", "")) or ""
    ).strip()

    enabled = _parse_bool(
        os.environ.get("WEBHOOK_ENABLED", merged.get("WEBHOOK_ENABLED")),
        default=True,
    )

    default_core_url = (
        "http://supervisor/core/api"
        if sup_token
        else str(merged.get("HA_CORE_URL") or "http://homeassistant.local:8123/api")
    )
    ha_core_url = str(os.environ.get("HA_CORE_URL", default_core_url) or "").strip().rstrip("/")

    push_ha_sensors = _parse_bool(
        os.environ.get("PUSH_HA_SENSORS", merged.get("push_ha_sensors", merged.get("PUSH_HA_SENSORS"))),
        default=True,
    )

    raw_interval = os.environ.get(
        "CHECK_INTERVAL_MINUTES",
        merged.get("check_interval_minutes", merged.get("CHECK_INTERVAL_MINUTES", 60)),
    )
    try:
        check_interval_minutes = max(5, int(raw_interval))
    except (TypeError, ValueError):
        check_interval_minutes = 60

    notify_service = str(
        os.environ.get(
            "NOTIFY_SERVICE",
            merged.get("notify_service", merged.get("NOTIFY_SERVICE", "auto")),
        )
        or "auto"
    ).strip()
    if (
        addon_cfg.get("notify_service") == "notify.persistent_notification"
        and not os.environ.get("NOTIFY_SERVICE")
    ):
        notify_service = "auto"

    remote_url = str(
        os.environ.get("PRISGEEK_REMOTE_URL", merged.get("PRISGEEK_REMOTE_URL", "")) or ""
    ).strip().rstrip("/")

    return {
        "ha_webhook_url": ha_url,
        "use_ha_webhook": use_ha_webhook,
        "extra_webhook_url": extra_url,
        "webhook_enabled": enabled,
        "ha_core_url": ha_core_url,
        "has_ha_token": bool(_get_ha_token(merged)),
        "push_ha_sensors": push_ha_sensors,
        "check_interval_minutes": check_interval_minutes,
        "notify_service": notify_service,
        "prisgeek_remote_url": remote_url,
    }


def _parse_hhmm(raw: Any, fallback: str) -> str:
    """Validate and normalize an HH:MM string."""
    text = str(raw or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", text)
    if not m:
        return fallback
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return fallback
    return f"{hour:02d}:{minute:02d}"


def load_quiet_hours_config() -> dict[str, Any]:
    """Load persisted quiet-hours configuration merged with defaults."""
    from db import get_setting

    saved = get_setting("quiet_hours", {})
    raw = saved if isinstance(saved, dict) else {}
    mode = str(raw.get("mode") or DEFAULT_QUIET_HOURS["mode"]).strip().lower()
    if mode not in {"defer", "silent"}:
        mode = DEFAULT_QUIET_HOURS["mode"]
    tz_name = str(raw.get("timezone") or DEFAULT_QUIET_HOURS["timezone"]).strip() or "Europe/Stockholm"
    return {
        "enabled": _parse_bool(raw.get("enabled"), bool(DEFAULT_QUIET_HOURS["enabled"])),
        "start": _parse_hhmm(raw.get("start"), DEFAULT_QUIET_HOURS["start"]),
        "end": _parse_hhmm(raw.get("end"), DEFAULT_QUIET_HOURS["end"]),
        "mode": mode,
        "timezone": tz_name,
    }


def save_quiet_hours_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist quiet-hours settings."""
    from db import set_setting

    mode = str(cfg.get("mode") or DEFAULT_QUIET_HOURS["mode"]).strip().lower()
    if mode not in {"defer", "silent"}:
        raise ValueError("mode must be 'defer' or 'silent'")
    tz_name = str(cfg.get("timezone") or DEFAULT_QUIET_HOURS["timezone"]).strip() or "Europe/Stockholm"
    try:
        ZoneInfo(tz_name)
    except Exception:
        tz_name = DEFAULT_QUIET_HOURS["timezone"]
    normalized = {
        "enabled": _parse_bool(cfg.get("enabled"), True),
        "start": _parse_hhmm(cfg.get("start"), DEFAULT_QUIET_HOURS["start"]),
        "end": _parse_hhmm(cfg.get("end"), DEFAULT_QUIET_HOURS["end"]),
        "mode": mode,
        "timezone": tz_name,
    }
    set_setting("quiet_hours", normalized)
    return normalized


def get_quiet_hours_status(now: datetime | None = None) -> dict[str, Any]:
    """Evaluate whether quiet hours are currently active in the configured local timezone."""
    cfg = load_quiet_hours_config()
    try:
        tz = ZoneInfo(cfg["timezone"])
    except Exception:
        tz = timezone.utc

    if now is None:
        local_dt = datetime.now(tz)
    elif now.tzinfo is None:
        local_dt = now.replace(tzinfo=timezone.utc).astimezone(tz)
    else:
        local_dt = now.astimezone(tz)

    start_h, start_m = (int(x) for x in cfg["start"].split(":"))
    end_h, end_m = (int(x) for x in cfg["end"].split(":"))
    start_t = dt_time(start_h, start_m)
    end_t = dt_time(end_h, end_m)
    cur_t = local_dt.time().replace(second=0, microsecond=0)

    active = False
    if cfg["enabled"] and start_t != end_t:
        if start_t < end_t:
            active = start_t <= cur_t < end_t
        else:
            active = (cur_t >= start_t) or (cur_t < end_t)

    return {
        **cfg,
        "active": active,
        "local_time": local_dt.strftime("%H:%M"),
    }


def build_alert_message(alert_data: dict[str, Any]) -> str:
    """Format a human-readable notification message for a price drop alert."""
    name = alert_data.get("product_name", "Okänd produkt")
    price = alert_data.get("price")
    total_price = alert_data.get("total_price")
    target = alert_data.get("target_price")
    retailer = alert_data.get("retailer") or "Okänd butik"
    url = alert_data.get("url") or ""
    low_30d = alert_data.get("lowest_30d_price")
    low_all_time = alert_data.get("lowest_all_time_price")
    assessment = alert_data.get("deal_assessment")

    price_str = f"{price:.0f} kr" if price is not None else "N/A"
    total_str = (
        f" (inkl. frakt: {total_price:.0f} kr)"
        if total_price is not None and price is not None and total_price > price
        else ""
    )
    target_str = f"{target:.0f} kr" if target is not None else "N/A"

    history_parts: list[str] = []
    if low_30d is not None:
        history_parts.append(f"30d-lägsta: {low_30d:.0f} kr")
    if low_all_time is not None:
        history_parts.append(f"Lägsta någonsin: {low_all_time:.0f} kr")
    if assessment:
        history_parts.append(assessment)

    history_suffix = f" [{' | '.join(history_parts)}]" if history_parts else ""
    watch_label = alert_data.get("watch_name")
    trigger_reason = alert_data.get("trigger_reason")
    reason_prefix = f"[{trigger_reason}] " if trigger_reason else ""
    prefix = f"[{watch_label}] " if watch_label else ""

    return (
        f"{prefix}{reason_prefix}Prisbevakning: {name} kostar nu {price_str}{total_str} "
        f"hos {retailer} (Mål: {target_str}){history_suffix} - {url}"
    )


def _resolve_notify_services(
    client: httpx.Client,
    ha_core_url: str,
    auth_headers: dict[str, str],
    configured: str,
) -> list[str]:
    """Resolve explicit notify.* targets or auto-discover mobile_app_* services from Home Assistant."""
    cleaned = (configured or "").strip()
    if cleaned and cleaned.lower() != "auto":
        return [s.strip() for s in cleaned.split(",") if "." in s.strip()]

    try:
        resp = client.get(f"{ha_core_url}/services", headers=auth_headers)
        resp.raise_for_status()
        domains = resp.json()
        if isinstance(domains, list):
            for entry in domains:
                if isinstance(entry, dict) and entry.get("domain") == "notify":
                    services_dict = entry.get("services") or {}
                    return [
                        f"notify.{svc}"
                        for svc in services_dict.keys()
                        if str(svc).startswith("mobile_app_")
                    ]
    except Exception as exc:
        logger.warning("Failed auto-discovering mobile_app notify services: %s", exc)
    return []


def send_price_alert(
    alert_data: dict[str, Any],
    *,
    dry_run: bool = False,
    ignore_quiet_hours: bool = False,
) -> dict[str, Any]:
    """Send a price alert to webhooks and, when authorized, Home Assistant's notify service."""
    cfg = load_notifier_config()
    token = _get_ha_token()
    message = build_alert_message(alert_data)
    payload = {
        "message": message,
        "title": f"PrisGeek Fynd: {alert_data.get('product_name', 'Produkt')}",
        **alert_data,
    }

    result: dict[str, Any] = {
        "message": message,
        "sent_ha": False,
        "sent_ha_notify": False,
        "notified_services": [],
        "sent_extra": False,
        "errors": [],
    }

    if dry_run or not cfg["webhook_enabled"]:
        result["skipped"] = True
        return result

    qh_status = get_quiet_hours_status() if not ignore_quiet_hours else {"active": False, "mode": "defer"}
    if qh_status.get("active") and qh_status.get("mode") == "defer":
        result["skipped"] = True
        result["deferred_quiet_hours"] = True
        return result
    silent_push = bool(qh_status.get("active") and qh_status.get("mode") == "silent")

    targets: list[tuple[str, str, dict[str, Any]]] = []
    if cfg["ha_webhook_url"] and cfg.get("use_ha_webhook", True):
        targets.append(("sent_ha", cfg["ha_webhook_url"], payload))
    if cfg["extra_webhook_url"]:
        extra_payload = {"content": message, "text": message, **payload}
        targets.append(("sent_extra", cfg["extra_webhook_url"], extra_payload))

    with httpx.Client(timeout=5.0) as client:
        for flag_key, url, body in targets:
            try:
                resp = client.post(url, json=body)
                resp.raise_for_status()
                result[flag_key] = True
            except httpx.RequestError as exc:
                err = f"Webhook request error ({url}): {exc}"
                logger.warning(err)
                result["errors"].append(err)
            except httpx.HTTPStatusError as exc:
                err = f"Webhook HTTP {exc.response.status_code} ({url})"
                logger.warning(err)
                result["errors"].append(err)

        if token:
            auth_headers = {"Authorization": f"Bearer {token}"}
            services = _resolve_notify_services(
                client,
                cfg["ha_core_url"],
                auth_headers,
                cfg["notify_service"],
            )
            for full_svc in services:
                domain, service = full_svc.split(".", 1)
                notify_url = f"{cfg['ha_core_url']}/services/{domain}/{service}"
                service_payload: dict[str, Any] = {
                    "title": payload["title"],
                    "message": message,
                }
                if service.startswith("mobile_app_") and alert_data.get("url"):
                    actions: list[dict[str, str]] = []
                    if alert_data.get("store_url"):
                        actions.append(
                            {
                                "action": "URI",
                                "title": f"Köp hos {alert_data.get('retailer') or 'butik'}",
                                "uri": alert_data["store_url"],
                            }
                        )
                    actions.append(
                        {
                            "action": "URI",
                            "title": "Öppna på PrisGeek",
                            "uri": alert_data["url"],
                        }
                    )
                    mobile_data: dict[str, Any] = {
                        "url": alert_data["url"],
                        "clickAction": alert_data["url"],
                        "actions": actions,
                    }
                    if silent_push:
                        mobile_data["push"] = {
                            "interruption-level": "passive",
                            "sound": "none",
                        }
                    service_payload["data"] = mobile_data
                try:
                    resp = client.post(
                        notify_url,
                        headers=auth_headers,
                        json=service_payload,
                    )
                    resp.raise_for_status()
                    result["sent_ha_notify"] = True
                    result["notified_services"].append(full_svc)
                except Exception as exc:
                    err = f"HA notify service error ({full_svc}): {exc}"
                    logger.warning(err)
                    result["errors"].append(err)

            # Always mirror alerts into Home Assistant's persistent notification bell panel.
            try:
                resp = client.post(
                    f"{cfg['ha_core_url']}/services/persistent_notification/create",
                    headers=auth_headers,
                    json={"title": payload["title"], "message": message},
                )
                resp.raise_for_status()
            except Exception as exc:
                logger.debug("HA persistent_notification.create skipped/failed: %s", exc)

    return result


def push_ha_sensors(watches: dict[str, Any]) -> dict[str, Any]:
    """Push native sensor.prisgeek_* states and attributes to Home Assistant REST API."""
    cfg = load_notifier_config()
    token = _get_ha_token()
    if not cfg["push_ha_sensors"] or not token:
        return {"pushed": 0, "skipped": True}

    product_watches = watches.get("product_watches") or []
    query_watches = watches.get("query_watches") or []
    below_count = sum(
        1
        for w in product_watches
        if w.get("last_price") is not None
        and float(w["last_price"]) <= float(w["target_price"])
    )

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    pushed = 0
    errors: list[str] = []

    entities: list[tuple[str, dict[str, Any]]] = [
        (
            "sensor.prisgeek_tracker_summary",
            {
                "state": below_count,
                "attributes": {
                    "friendly_name": "PrisGeek Fynd under målpris",
                    "unit_of_measurement": "st",
                    "icon": "mdi:tag-multiple",
                    "product_watches_count": len(product_watches),
                    "query_watches_count": len(query_watches),
                },
            },
        )
    ]

    for w in product_watches:
        if w.get("last_price") is None:
            continue
        entities.append(
            (
                f"sensor.prisgeek_{w['public_id']}",
                {
                    "state": w["last_price"],
                    "attributes": {
                        "friendly_name": f"PrisGeek: {w['product_name']}",
                        "unit_of_measurement": "kr",
                        "device_class": "monetary",
                        "state_class": "measurement",
                        "icon": "mdi:tag-outline",
                        "target_price": w["target_price"],
                        "below_target": float(w["last_price"]) <= float(w["target_price"]),
                        "retailer": w.get("last_retailer"),
                        "store_url": w.get("last_store_url"),
                        "total_price_with_shipping": w.get("last_total_price"),
                        "lowest_30d_price": w.get("lowest_30d_price"),
                        "lowest_all_time_price": w.get("lowest_all_time_price"),
                        "product_url": w.get("product_url"),
                        "last_checked_at": w.get("last_checked_at"),
                    },
                },
            )
        )

    for q in query_watches:
        if q.get("best_match_price") is None:
            continue
        entities.append(
            (
                f"sensor.prisgeek_interest_{q['id']}",
                {
                    "state": q["best_match_price"],
                    "attributes": {
                        "friendly_name": f"PrisGeek Intresse: {q['name']}",
                        "unit_of_measurement": "kr",
                        "device_class": "monetary",
                        "state_class": "measurement",
                        "icon": "mdi:magnify-scan",
                        "search_query": q["search_query"],
                        "target_price": q["target_price"],
                        "below_target": float(q["best_match_price"]) <= float(q["target_price"]),
                        "best_match_name": q.get("best_match_name"),
                        "best_match_retailer": q.get("best_match_retailer"),
                        "best_match_store_url": q.get("best_match_store_url"),
                        "best_match_url": q.get("best_match_url"),
                        "match_count": q.get("match_count", 0),
                        "last_checked_at": q.get("last_checked_at"),
                    },
                },
            )
        )

    with httpx.Client(timeout=5.0) as client:
        for entity_id, body in entities:
            url = f"{cfg['ha_core_url']}/states/{entity_id}"
            try:
                resp = client.post(url, headers=headers, json=body)
                resp.raise_for_status()
                pushed += 1
            except Exception as exc:
                err = f"Failed updating {entity_id}: {exc}"
                logger.warning(err)
                errors.append(err)

    return {"pushed": pushed, "skipped": False, "errors": errors}
