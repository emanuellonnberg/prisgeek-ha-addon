"""Live HTTP client for PrisGeek (Supabase PostgREST + SSR HTML & TanStack Router data)."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, quote_plus, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

PRISGEEK_BASE = "https://prisgeek.se"
DEFAULT_SUPABASE_URL = "https://xnzqsditjjzxeinbpilo.supabase.co"
DEFAULT_SUPABASE_KEY = "sb_publishable_wu9XYY1fkqp-V-TnLxtg9g_ZyEOULtt"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "sv-SE,sv;q=0.9,en-US;q=0.8,en;q=0.7",
}

OUT_OF_STOCK_VALUES = {
    "out_of_stock",
    "outofstock",
    "slut",
    "slut_i_lager",
    "unavailable",
    "discontinued",
    "sold_out",
}

REFURB_CONDITION_VALUES = {
    "refurbished",
    "used",
    "demo",
    "begagnad",
    "fyndvara",
    "outlet",
    "open_box",
    "b-stock",
}

REFURB_NAME_RE = re.compile(
    r"(refurb|renoverad|rekonditionerad|renewed|recertified|återb\.|återbrukad|"
    r"begagnad|\bgrade [abc]\b|\bklass [abc]\b|skick: grade)",
    re.IGNORECASE,
)

PRODUCT_SELECT = (
    "id,public_id,slug,name,brand,model,image_url,mpn,specs,release_date,"
    "category_id,subcategory,categories(slug,name),"
    "offers(id,price,list_price,shipping,stock_status,retailer_id,condition,"
    "delivery_days,url,retailers(name,slug,dispatch_country))"
)


def unwrap_store_url(raw_url: str | None) -> str | None:
    """Resolve relative store links and unwrap affiliate wrapper URLs when inner URL is present."""
    if not raw_url:
        return None
    cleaned = str(raw_url).strip()
    if not cleaned:
        return None
    if cleaned.startswith("/") or cleaned.startswith("//"):
        cleaned = urljoin(PRISGEEK_BASE, cleaned)
    if not cleaned.startswith(("http://", "https://")):
        return None
    try:
        query_params = parse_qs(urlsplit(cleaned).query)
        for key in ("url", "target", "u"):
            for candidate in query_params.get(key, []):
                cand = str(candidate).strip()
                if cand.startswith(("http://", "https://")):
                    return cand
    except Exception:
        pass
    return cleaned


def parse_sek(text: str | None) -> float | None:
    """Extract a numeric SEK price from Swedish formatted text like '1 990 kr' or '75,50 kr'."""
    if not text:
        return None
    cleaned = text.replace("\xa0", " ").replace("\u202f", " ")
    match = re.search(r"(\d[\d\s]*(?:[.,]\d+)?)", cleaned)
    if not match:
        return None
    num_str = match.group(1).replace(" ", "").replace(",", ".")
    try:
        return float(num_str)
    except ValueError:
        return None


def parse_drop_pct(text: str | None) -> float | None:
    """Extract discount percentage from text like '− 61,8 %' or '-25%'."""
    if not text:
        return None
    cleaned = text.replace("\xa0", " ").replace("−", "-").replace(",", ".")
    match = re.search(r"-?\s*(\d+(?:\.\d+)?)\s*%", cleaned)
    if not match:
        return None
    try:
        return abs(float(match.group(1)))
    except ValueError:
        return None


def extract_public_id(url_or_id: str | int) -> int | None:
    """Extract an integer PrisGeek public_id from an ID or /produkt/{id}/{slug} URL."""
    if isinstance(url_or_id, int):
        return url_or_id
    raw = str(url_or_id).strip()
    if raw.isdigit():
        return int(raw)
    match = re.search(r"/produkt/(\d+)", raw)
    if match:
        return int(match.group(1))
    return None


def _unescape_js_string(raw: str) -> str:
    """Safely unescape a JS double-quoted string fragment."""
    try:
        return json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return raw.replace('\\"', '"').replace("\\\\", "\\")


def _is_offer_in_stock(stock_status: str | None) -> bool:
    """Determine if an offer's stock_status indicates in-stock availability."""
    if not stock_status:
        return True
    normalized = stock_status.strip().lower().replace(" ", "_")
    return normalized not in OUT_OF_STOCK_VALUES


def _is_offer_new(condition: str | None) -> bool:
    """Return True when the offer condition represents a brand-new item."""
    if not condition:
        return True
    normalized = condition.strip().lower().replace(" ", "_")
    return normalized not in REFURB_CONDITION_VALUES


def summarize_offers(
    offers: list[dict[str, Any]] | None,
    *,
    in_stock_only: bool = False,
    exclude_refurbished: bool = True,
) -> dict[str, Any]:
    """Filter, normalize, and sort store offers to determine best price and retailer."""
    normalized_offers: list[dict[str, Any]] = []
    for offer in offers or []:
        raw_price = offer.get("price")
        if raw_price is None:
            continue
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue

        condition = offer.get("condition")
        if exclude_refurbished and not _is_offer_new(condition):
            continue

        stock_status = offer.get("stock_status")
        in_stock = _is_offer_in_stock(stock_status)
        if in_stock_only and not in_stock:
            continue

        raw_shipping = offer.get("shipping")
        try:
            shipping = float(raw_shipping) if raw_shipping is not None else 0.0
        except (TypeError, ValueError):
            shipping = 0.0

        retailer_info = offer.get("retailers") or {}
        retailer_name = retailer_info.get("name") or "Okänd butik"
        retailer_slug = retailer_info.get("slug") or ""

        normalized_offers.append(
            {
                "price": round(price, 2),
                "shipping": round(shipping, 2),
                "total_price": round(price + shipping, 2),
                "retailer_name": retailer_name,
                "retailer_slug": retailer_slug,
                "dispatch_country": retailer_info.get("dispatch_country"),
                "stock_status": stock_status,
                "in_stock": in_stock,
                "condition": condition or "new",
                "delivery_days": offer.get("delivery_days"),
                "store_url": unwrap_store_url(offer.get("url")),
            }
        )

    # Sort in-stock offers before out-of-stock offers, then lowest price first.
    normalized_offers.sort(key=lambda o: (not o["in_stock"], o["price"], o["total_price"]))

    in_stock_offers = [o for o in normalized_offers if o["in_stock"]]
    active_pool = in_stock_offers if in_stock_offers else normalized_offers

    best_offer = active_pool[0] if active_pool else None
    lowest_total = min(o["total_price"] for o in active_pool) if active_pool else None

    return {
        "offers": normalized_offers,
        "offer_count": len(normalized_offers),
        "best_offer": best_offer,
        "lowest_price": best_offer["price"] if best_offer else None,
        "lowest_total_price": round(lowest_total, 2) if lowest_total is not None else None,
        "best_retailer": best_offer["retailer_name"] if best_offer else None,
        "best_store_url": best_offer.get("store_url") if best_offer else None,
        "in_stock": bool(in_stock_offers),
    }


def _token_variants(token: str) -> list[str]:
    """Expand capacity/unit tokens so e.g. '16gb' matches both '16gb' and '16 gb'."""
    match = re.match(r"^(\d+(?:[.,]\d+)?)(gb|tb|mb|kb|w|mhz|ghz|hz|mm|cm)$", token.lower())
    if match:
        num, unit = match.group(1), match.group(2)
        return [f"{num}{unit}", f"{num} {unit}"]
    return [token]


def _product_searchable_blob(product: dict[str, Any]) -> str:
    """Build a lowercase string across name, brand, model, mpn, category, and specs."""
    parts = [
        str(product.get("name") or ""),
        str(product.get("brand") or ""),
        str(product.get("model") or ""),
        str(product.get("mpn") or ""),
        str(product.get("subcategory") or ""),
    ]
    cat = product.get("categories")
    if isinstance(cat, dict):
        parts.append(str(cat.get("name") or ""))
    specs = product.get("specs")
    if specs:
        parts.append(json.dumps(specs, ensure_ascii=False))
    raw_blob = " ".join(parts).lower()
    compact_blob = re.sub(
        r"(\d+(?:[.,]\d+)?)\s+(gb|tb|mb|kb|w|mhz|ghz|hz|mm|cm)\b",
        r"\1\2",
        raw_blob,
    )
    return f"{raw_blob} {compact_blob}"


SWEDISH_NOISE_WORDS_RE = re.compile(
    r"(?<!\w)(?:"
    r"80\+\s*(?:gold|bronze|silver|platinum|titanium)|"
    r"atx\s*3\.[01]|pcie\s*[45]\.0|m\.2|"
    r"[23]-pack|midi-tower|mid\s+tower|"
    r"svart|vit|grå|silver|blå|röd|grön|rosa|guld|"
    r"chassi|miditower|fulltower|minitower|"
    r"spelmus|gamingmus|datormus|trådlös|trådlösa|trådbunden|"
    r"mekaniskt|mekanisk|tangentbord|nätaggregat|"
    r"helmodulär|helmodulärt|halvmodulär|halvmodulärt|modulärt|"
    r"grafikkort|bildskärm|gamingskärm|datorskärm|processor|moderkort|"
    r"internminne|hårddisk|fläkt|vattenkylning|processorkylare|"
    r"hörlurar|headset|webbkamera|projektor|pack|"
    r"inkl|med|för|dator|series|nvme"
    r")(?!\w)",
    re.IGNORECASE,
)


def clean_review_search_query(
    name: str,
    brand: str | None = None,
    model: str | None = None,
) -> str:
    """Produce a clean international product query from Swedish retail product names."""
    raw = str(name or "").strip()
    if not raw:
        raw = " ".join(p.strip() for p in (brand or "", model or "") if p and p.strip())
    raw = re.sub(r"\([^)]*\)", " ", raw)
    raw = re.sub(r"\s+-\s+.*$", "", raw)
    cleaned = SWEDISH_NOISE_WORDS_RE.sub(" ", raw)
    cleaned = re.sub(r"\b(\d+)\s+\1(w|gb|tb|mm|hz)\b", r"\1\2", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.-/")
    if not cleaned:
        cleaned = str(name or model or brand or "").strip()

    brand_clean = str(brand or "").strip()
    if brand_clean and brand_clean.lower() not in cleaned.lower():
        cleaned = f"{brand_clean} {cleaned}".strip()

    words = cleaned.split()
    return " ".join(words[:7])


def build_external_review_links(
    name: str,
    *,
    brand: str | None = None,
    model: str | None = None,
    category: str | None = None,
) -> list[dict[str, str]]:
    """Build category-aware direct search links to specialist independent hardware review sites."""
    q = clean_review_search_query(name, brand, model)
    q_enc = quote_plus(q)
    hay = f"{category or ''} {name}".lower()

    links: list[dict[str, str]] = []
    if any(k in hay for k in ("nätaggregat", "nataggregat", "80+", "atx 3")) or re.search(
        r"\bpsu\b", hay
    ):
        links = [
            {
                "site": "Hardware Busters",
                "url": f"https://hwbusters.com/?s={q_enc}",
                "query": q,
                "focus": "Oscilloskop, transienter, ATX 3.1 & ljudprofil",
            },
            {
                "site": "Cybenetics",
                "url": f"https://www.google.com/search?q={quote_plus('site:cybenetics.com ' + q)}",
                "query": q,
                "focus": "Oberoende ETA-effektivitet & LAMBDA-ljudbetyg",
            },
            {
                "site": "TechPowerUp",
                "url": f"https://www.techpowerup.com/review/?q={q_enc}",
                "query": q,
                "focus": "Kondensatortest, rippel & verkningsgrad",
            },
        ]
    elif any(k in hay for k in ("skärm", "skarm", "monitor")):
        links = [
            {
                "site": "RTINGS",
                "url": f"https://www.rtings.com/search?q={q_enc}",
                "query": q,
                "focus": "Labbmätning: svarstid, ljusstyrka, kontrast & HDR",
            },
            {
                "site": "TFTCentral",
                "url": f"https://tftcentral.co.uk/?s={q_enc}",
                "query": q,
                "focus": "Djupgående panelanalys, kalibrering & rörelseskärpa",
            },
            {
                "site": "TechPowerUp",
                "url": f"https://www.techpowerup.com/review/?q={q_enc}",
                "query": q,
                "focus": "Färgomfång, OSD & input lag",
            },
        ]
    elif any(k in hay for k in ("projektor", "tv & bild")) or re.search(r"\btv\b", hay):
        links = [
            {
                "site": "RTINGS",
                "url": f"https://www.rtings.com/search?q={q_enc}",
                "query": q,
                "focus": "Labbmätning: HDR-toppljusstyrka, svärta & HDMI 2.1",
            },
            {
                "site": "FlatpanelsHD",
                "url": f"https://www.google.com/search?q={quote_plus('site:flatpanelshd.com ' + q)}",
                "query": q,
                "focus": "Nordiska/EU TV-modeller, panelteknik & kalibrering",
            },
        ]
    elif any(
        k in hay
        for k in (
            "headset",
            "ljud",
            "hörlur",
            "tangentbord",
            "webbkamera",
            "gamingmus",
            "spelmus",
        )
    ) or re.search(r"\b(?:möss|mus|mouse)\b", hay):
        links = [
            {
                "site": "RTINGS",
                "url": f"https://www.rtings.com/search?q={q_enc}",
                "query": q,
                "focus": "Frekvenskurva, ANC, klicklatens & sensortester",
            },
            {
                "site": "TechPowerUp",
                "url": f"https://www.techpowerup.com/review/?q={q_enc}",
                "query": q,
                "focus": "Switchar, byggkvalitet & sensorprestanda",
            },
        ]
    elif any(k in hay for k in ("lagring", "nvme", "microsd")) or re.search(
        r"\b(?:ssd|hdd)\b", hay
    ):
        links = [
            {
                "site": "TechPowerUp",
                "url": f"https://www.techpowerup.com/review/?q={q_enc}",
                "query": q,
                "focus": "Kontroller/NAND-ID, DRAM-cache & termisk strypning",
            },
            {
                "site": "Tom's Hardware",
                "url": f"https://www.tomshardware.com/search?searchTerm={q_enc}",
                "query": q,
                "focus": "Sustained write, slumpläsning & uthållighet",
            },
        ]
    elif any(k in hay for k in ("bärbara", "barbara", "laptop", "mobil", "wearable")):
        links = [
            {
                "site": "Notebookcheck",
                "url": f"https://www.google.com/search?q={quote_plus('site:notebookcheck.net ' + q)}",
                "query": q,
                "focus": "Skärm-PWM, batteritid, fläktljud & TDP-gränser",
            },
        ]
        if any(k in hay for k in ("mobil", "wearable")):
            links.append(
                {
                    "site": "GSMArena",
                    "url": f"https://www.gsmarena.com/res.php3?sSearch={q_enc}",
                    "query": q,
                    "focus": "Batteriuthållighet, kameratester & skärmnits",
                }
            )
        else:
            links.append(
                {
                    "site": "RTINGS",
                    "url": f"https://www.rtings.com/search?q={q_enc}",
                    "query": q,
                    "focus": "Skärmkalibrering, tangentbord & batteritest",
                }
            )
    elif any(k in hay for k in ("nätverk", "natverk", "wi-fi", "smarta hemmet")) or re.search(
        r"\b(?:router|mesh|wifi|iot)\b", hay
    ):
        links = [
            {
                "site": "Dong Knows Tech",
                "url": f"https://dongknows.com/?s={q_enc}",
                "query": q,
                "focus": "Verklig Wi-Fi-genomströmning, räckvidd & mesh-tester",
            },
            {
                "site": "M3.se",
                "url": f"https://www.google.com/search?q={quote_plus('site:m3.idg.se ' + q)}",
                "query": q,
                "focus": "Svenska tester av nätverk & smarta hemmet",
            },
        ]
    elif any(k in hay for k in ("foto", "kamera")):
        links = [
            {
                "site": "DPReview",
                "url": f"https://www.dpreview.com/search?query={q_enc}",
                "query": q,
                "focus": "Studioscener, dynamiskt omfång & autofokus",
            },
            {
                "site": "RTINGS",
                "url": f"https://www.rtings.com/search?q={q_enc}",
                "query": q,
                "focus": "Brusprestanda, video-rolling shutter & objektiv",
            },
        ]
    elif any(
        k in hay
        for k in (
            "grafikkort",
            "processor",
            "moderkort",
            "kyl",
            "chassi",
            "minne",
            "fläkt",
            "datorkomponenter",
        )
    ):
        links = [
            {
                "site": "TechPowerUp",
                "url": f"https://www.techpowerup.com/review/?q={q_enc}",
                "query": q,
                "focus": "Kylarbuller, VRM-temperaturer & PCB-analys",
            },
            {
                "site": "Tom's Hardware",
                "url": f"https://www.tomshardware.com/search?searchTerm={q_enc}",
                "query": q,
                "focus": "Spelprestanda, temperaturer & prestanda per krona",
            },
            {
                "site": "GamersNexus",
                "url": f"https://www.youtube.com/results?search_query={quote_plus('GamersNexus ' + q)}",
                "query": q,
                "focus": "Standardiserad termisk & ljudnivåtest",
            },
        ]
    else:
        links = [
            {
                "site": "RTINGS",
                "url": f"https://www.rtings.com/search?q={q_enc}",
                "query": q,
                "focus": "Objektiva labbmätningar och betyg",
            },
            {
                "site": "TechPowerUp",
                "url": f"https://www.techpowerup.com/review/?q={q_enc}",
                "query": q,
                "focus": "Tekniska djupgranskningar av hårdvara",
            },
        ]

    links.append(
        {
            "site": "SweClockers",
            "url": f"https://www.sweclockers.com/sok?q={q_enc}",
            "query": q,
            "focus": "Svenska recensioner & forumdiskussioner",
        }
    )
    links.append(
        {
            "site": "YouTube Review",
            "url": f"https://www.youtube.com/results?search_query={quote_plus(q + ' review')}",
            "query": q,
            "focus": "Videotester, genomgångar & verkliga jämförelser",
        }
    )
    return links


class PrisGeekClient:
    """Client for querying PrisGeek via Supabase PostgREST and SSR HTML/TSR endpoints."""

    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout
        self.supabase_url = DEFAULT_SUPABASE_URL
        self.supabase_key = DEFAULT_SUPABASE_KEY
        self._http = httpx.Client(
            headers=DEFAULT_HEADERS,
            timeout=timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._http.close()

    def _supabase_headers(self) -> dict[str, str]:
        return {
            "apikey": self.supabase_key,
            "Authorization": f"Bearer {self.supabase_key}",
            "Accept": "application/json",
        }

    def _refresh_supabase_credentials(self) -> bool:
        """Fetch https://prisgeek.se and extract current Supabase URL & publishable key from JS bundles."""
        try:
            home_resp = self._http.get(PRISGEEK_BASE)
            home_resp.raise_for_status()
            bundle_paths = re.findall(
                r"(/assets/(?:client|root)-[A-Za-z0-9_-]+\.js)", home_resp.text
            )
            for rel_path in dict.fromkeys(bundle_paths):
                js_resp = self._http.get(urljoin(PRISGEEK_BASE, rel_path))
                if js_resp.status_code != 200:
                    continue
                url_match = re.search(r"https://[a-z0-9]+\.supabase\.co", js_resp.text)
                key_match = re.search(r"sb_publishable_[A-Za-z0-9_-]+", js_resp.text)
                if url_match and key_match:
                    self.supabase_url = url_match.group(0)
                    self.supabase_key = key_match.group(0)
                    logger.info("Refreshed PrisGeek Supabase credentials from %s", rel_path)
                    return True
        except Exception as exc:
            logger.warning("Failed to refresh Supabase credentials: %s", exc)
        return False

    def _supabase_get(self, path: str, params: list[tuple[str, str]]) -> Any:
        """Execute a GET request against Supabase REST API with automatic credential refresh on 401/403."""
        url = f"{self.supabase_url}/rest/v1/{path.lstrip('/')}"
        resp = self._http.get(url, params=params, headers=self._supabase_headers())
        if resp.status_code in (401, 403):
            if self._refresh_supabase_credentials():
                resp = self._http.get(url, params=params, headers=self._supabase_headers())
        resp.raise_for_status()
        return resp.json()

    def _format_product_url(self, public_id: int | str, slug: str | None) -> str:
        clean_slug = (slug or "produkt").strip("/")
        return f"{PRISGEEK_BASE}/produkt/{public_id}/{clean_slug}"

    def _format_search_hit(
        self,
        row: dict[str, Any],
        *,
        in_stock_only: bool = False,
        exclude_refurbished: bool = True,
        min_price: float | None = None,
        max_price: float | None = None,
    ) -> dict[str, Any] | None:
        """Summarize offers, enforce price/stock filters, and format a catalog product hit."""
        name = str(row.get("name") or "")
        if exclude_refurbished and REFURB_NAME_RE.search(name):
            return None

        summary = summarize_offers(
            row.get("offers"),
            in_stock_only=in_stock_only,
            exclude_refurbished=exclude_refurbished,
        )
        lowest_price = summary["lowest_price"]
        if lowest_price is None:
            return None
        if min_price is not None and lowest_price < min_price:
            return None
        if max_price is not None and lowest_price > max_price:
            return None

        cat_obj = row.get("categories")
        category_name = cat_obj.get("name") if isinstance(cat_obj, dict) else None
        effective_category = category_name or row.get("subcategory")
        public_id = row.get("public_id")
        clean_query = clean_review_search_query(
            name, row.get("brand"), row.get("model")
        )
        return {
            "public_id": public_id,
            "uuid": row.get("id"),
            "name": name,
            "brand": row.get("brand"),
            "model": row.get("model"),
            "mpn": row.get("mpn"),
            "category": effective_category,
            "subcategory": row.get("subcategory"),
            "url": self._format_product_url(public_id, row.get("slug")),
            "lowest_price": lowest_price,
            "lowest_total_price": summary["lowest_total_price"],
            "best_retailer": summary["best_retailer"],
            "best_store_url": summary["best_store_url"],
            "in_stock": summary["in_stock"],
            "offer_count": summary["offer_count"],
            "specs": row.get("specs"),
            "offers": summary["offers"][:3],
            "clean_model_query": clean_query,
            "external_review_links": build_external_review_links(
                name,
                brand=row.get("brand"),
                model=row.get("model"),
                category=effective_category,
            ),
        }

    def search_products(
        self,
        query: str,
        *,
        max_price: float | None = None,
        in_stock_only: bool = False,
        exclude_refurbished: bool = True,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search PrisGeek products and summarize live Swedish offers."""
        tokens = [t.strip() for t in re.split(r"\s+", query.strip()) if t.strip()]
        if not tokens:
            return []

        fetch_limit = max(limit * 5, 80)
        base_params: list[tuple[str, str]] = [
            ("select", PRODUCT_SELECT),
            ("offers.market", "eq.se"),
            ("offers.price_flag", "is.null"),
            ("limit", str(fetch_limit)),
        ]

        clause_parts: list[str] = []
        for tok in tokens:
            variants = _token_variants(tok)
            if len(variants) == 1:
                clause_parts.append(f"name.ilike.*{variants[0]}*")
            else:
                or_inner = ",".join(f"name.ilike.*{v}*" for v in variants)
                clause_parts.append(f"or({or_inner})")
        and_expr = ",".join(clause_parts)

        rows: list[dict[str, Any]] = self._supabase_get(
            "products",
            base_params + [("and", f"({and_expr})")],
        )

        # Fallback: search by longest token across name/brand/model and match all tokens locally.
        if len(rows) < limit and len(tokens) > 1:
            anchor = max(tokens, key=len)
            anchor_clauses = ",".join(
                f"name.ilike.*{v}*,brand.ilike.*{v}*,model.ilike.*{v}*"
                for v in _token_variants(anchor)
            )
            fallback_rows: list[dict[str, Any]] = self._supabase_get(
                "products",
                base_params + [("or", f"({anchor_clauses})")],
            )
            seen_ids = {r.get("public_id") for r in rows}
            for row in fallback_rows:
                if row.get("public_id") not in seen_ids:
                    rows.append(row)
                    seen_ids.add(row.get("public_id"))

        results: list[dict[str, Any]] = []
        lower_tokens = [t.lower() for t in tokens]

        for row in rows:
            blob = _product_searchable_blob(row)
            if not all(any(v in blob for v in _token_variants(tok)) for tok in lower_tokens):
                continue
            hit = self._format_search_hit(
                row,
                in_stock_only=in_stock_only,
                exclude_refurbished=exclude_refurbished,
                max_price=max_price,
            )
            if hit is not None:
                results.append(hit)

        results.sort(
            key=lambda item: (not item["in_stock"], item["lowest_price"], item["lowest_total_price"] or 0.0)
        )
        return results[:limit]

    def find_category_peers(
        self,
        category_id: str | None,
        *,
        subcategory: str | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
        exclude_public_id: int | None = None,
        in_stock_only: bool = True,
        exclude_refurbished: bool = True,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """Query products in the same PrisGeek category/subcategory within optional price bounds."""
        if not category_id:
            return []

        inner_select = PRODUCT_SELECT.replace("offers(", "offers!inner(", 1)
        fetch_limit = max(limit * 3, 60)

        def _query(subcat: str | None) -> list[dict[str, Any]]:
            params: list[tuple[str, str]] = [
                ("select", inner_select),
                ("category_id", f"eq.{category_id}"),
                ("offers.market", "eq.se"),
                ("offers.price_flag", "is.null"),
                ("limit", str(fetch_limit)),
            ]
            if subcat:
                params.append(("subcategory", f"eq.{subcat}"))
            if min_price is not None and min_price > 0:
                params.append(("offers.price", f"gte.{int(min_price)}"))
            if max_price is not None and max_price > 0:
                params.append(("offers.price", f"lte.{int(max_price) + 1}"))
            return self._supabase_get("products", params) or []

        rows = _query(subcategory)
        hits: list[dict[str, Any]] = []
        seen_pids: set[int] = set()

        def _collect(raw_rows: list[dict[str, Any]]) -> None:
            for row in raw_rows:
                pid = row.get("public_id")
                if pid is None:
                    continue
                pid_int = int(pid)
                if pid_int == exclude_public_id or pid_int in seen_pids:
                    continue
                hit = self._format_search_hit(
                    row,
                    in_stock_only=in_stock_only,
                    exclude_refurbished=exclude_refurbished,
                    min_price=min_price,
                    max_price=max_price,
                )
                if hit is not None:
                    seen_pids.add(pid_int)
                    hits.append(hit)

        _collect(rows)
        if subcategory and len(hits) < max(8, limit // 2):
            _collect(_query(None))

        hits.sort(
            key=lambda item: (
                not item["in_stock"],
                -min(int(item.get("offer_count") or 0), 12),
                item["lowest_price"],
            )
        )
        return hits[:limit]

    def _extract_ssr_history_metrics(
        self, product_url: str, product_uuid: str | None = None
    ) -> dict[str, Any]:
        """Extract TanStack Router history array and JSON-LD Offer metrics from the product page."""
        metrics: dict[str, Any] = {
            "lowest_30d_price": None,
            "lowest_all_time_price": None,
            "history_points_count": 0,
            "history_series": [],
            "json_ld_low_price": None,
            "json_ld_high_price": None,
        }

        try:
            resp = self._http.get(product_url)
            if resp.status_code == 200:
                html = resp.text

                # Parse TanStack Router dehydrated history:$R[...] array before retailerHistory.
                hist_start = html.find("history:$R[")
                if hist_start != -1:
                    ret_hist_pos = html.find(",retailerHistory:", hist_start)
                    hist_segment = (
                        html[hist_start:ret_hist_pos]
                        if ret_hist_pos != -1
                        else html[hist_start : hist_start + 20000]
                    )
                else:
                    hist_segment = html

                pairs = re.findall(
                    r'\{day:"(\d{4}-\d{2}-\d{2})",min_price:(\d+(?:\.\d+)?)\}',
                    hist_segment,
                )
                if pairs:
                    metrics["history_points_count"] = len(pairs)
                    metrics["history_series"] = [
                        {"day": d, "price": float(p)} for d, p in pairs[-90:]
                    ]
                    metrics["lowest_all_time_price"] = min(float(p) for _, p in pairs)
                    cutoff_30d = (
                        datetime.now(timezone.utc).date() - timedelta(days=30)
                    ).isoformat()
                    recent_prices = [float(p) for d, p in pairs if d >= cutoff_30d]
                    if recent_prices:
                        metrics["lowest_30d_price"] = min(recent_prices)

                # Parse JSON-LD AggregateOffer if present.
                soup = BeautifulSoup(html, "html.parser")
                for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
                    try:
                        data = json.loads(script.string or "")
                    except json.JSONDecodeError:
                        continue
                    nodes = data.get("@graph", [data]) if isinstance(data, dict) else [data]
                    for node in nodes:
                        if isinstance(node, dict) and node.get("@type") == "Product":
                            offers = node.get("offers") or {}
                            if isinstance(offers, dict):
                                if offers.get("lowPrice") is not None:
                                    metrics["json_ld_low_price"] = float(offers["lowPrice"])
                                if offers.get("highPrice") is not None:
                                    metrics["json_ld_high_price"] = float(offers["highPrice"])
        except Exception as exc:
            logger.debug("Failed SSR extraction for %s: %s", product_url, exc)

        # Fallback to Supabase price_history table if SSR history was empty.
        if not metrics["history_series"] and product_uuid:
            try:
                hist_rows = self._supabase_get(
                    "price_history",
                    [
                        ("select", "day,min_price"),
                        ("product_id", f"eq.{product_uuid}"),
                        ("order", "day.asc"),
                        ("limit", "90"),
                    ],
                )
                sb_pairs = [
                    (str(r["day"]), float(r["min_price"]))
                    for r in (hist_rows or [])
                    if r.get("day") and r.get("min_price") is not None
                ]
                if sb_pairs:
                    metrics["history_points_count"] = len(sb_pairs)
                    metrics["history_series"] = [
                        {"day": d, "price": p} for d, p in sb_pairs[-90:]
                    ]
                    metrics["lowest_all_time_price"] = min(p for _, p in sb_pairs)
                    cutoff_30d = (
                        datetime.now(timezone.utc).date() - timedelta(days=30)
                    ).isoformat()
                    recent_prices = [p for d, p in sb_pairs if d >= cutoff_30d]
                    if recent_prices:
                        metrics["lowest_30d_price"] = min(recent_prices)
            except Exception as exc:
                logger.debug("Supabase price_history fallback skipped for %s: %s", product_uuid, exc)

        return metrics

    def get_product_details(
        self,
        identifier: str | int,
        *,
        in_stock_only: bool = False,
        exclude_refurbished: bool = True,
    ) -> dict[str, Any]:
        """Fetch full product details, sorted offers, and price history context."""
        public_id = extract_public_id(identifier)
        base_params: list[tuple[str, str]] = [
            ("select", PRODUCT_SELECT),
            ("offers.market", "eq.se"),
            ("offers.price_flag", "is.null"),
            ("limit", "1"),
        ]

        if public_id is not None:
            rows = self._supabase_get(
                "products", base_params + [("public_id", f"eq.{public_id}")]
            )
        else:
            raw = str(identifier).strip()
            rows = self._supabase_get("products", base_params + [("slug", f"eq.{raw}")])
            if not rows:
                rows = self._supabase_get(
                    "products", base_params + [("name", f"ilike.*{raw}*")]
                )

        if not rows:
            raise ValueError(f"Product not found on PrisGeek: {identifier}")

        row = rows[0]
        pid = int(row["public_id"])
        product_url = self._format_product_url(pid, row.get("slug"))
        summary = summarize_offers(
            row.get("offers"),
            in_stock_only=in_stock_only,
            exclude_refurbished=exclude_refurbished,
        )
        history = self._extract_ssr_history_metrics(product_url, row.get("id"))

        lowest_price = summary["lowest_price"]
        lowest_30d = history.get("lowest_30d_price")
        lowest_all_time = history.get("lowest_all_time_price")

        deal_assessment = "No active offers"
        if lowest_price is not None:
            if lowest_all_time is not None and lowest_price <= lowest_all_time:
                deal_assessment = "At all-time low"
            elif lowest_30d is not None and lowest_price <= lowest_30d:
                deal_assessment = "At or below 30-day low"
            elif lowest_30d is not None and lowest_price <= lowest_30d * 1.05:
                deal_assessment = "Near 30-day low"
            else:
                deal_assessment = "Normal price"

        cat_obj = row.get("categories")
        category_name = cat_obj.get("name") if isinstance(cat_obj, dict) else None
        effective_category = category_name or row.get("subcategory")
        extra_context = self.get_product_extra_context(row.get("id"), pid)
        product_name = str(row.get("name") or "")

        return {
            "public_id": pid,
            "uuid": row.get("id"),
            "name": row.get("name"),
            "brand": row.get("brand"),
            "model": row.get("model"),
            "mpn": row.get("mpn"),
            "category": effective_category,
            "category_id": row.get("category_id"),
            "subcategory": row.get("subcategory"),
            "url": product_url,
            "image_url": row.get("image_url"),
            "lowest_price": lowest_price,
            "lowest_total_price": summary["lowest_total_price"],
            "best_retailer": summary["best_retailer"],
            "best_store_url": summary["best_store_url"],
            "in_stock": summary["in_stock"],
            "offer_count": summary["offer_count"],
            "lowest_30d_price": lowest_30d,
            "lowest_all_time_price": lowest_all_time,
            "history_points_count": history.get("history_points_count", 0),
            "history_series": history.get("history_series", []),
            "deal_assessment": deal_assessment,
            "specs": row.get("specs"),
            "offers": summary["offers"],
            "extra_context": extra_context,
            "clean_model_query": clean_review_search_query(
                product_name, row.get("brand"), row.get("model")
            ),
            "external_review_links": build_external_review_links(
                product_name,
                brand=row.get("brand"),
                model=row.get("model"),
                category=effective_category,
            ),
        }

    def get_product_extra_context(
        self, product_uuid: str | None, public_id: int | None = None
    ) -> dict[str, Any]:
        """Fetch SweClockers review awards and community member deal tips for a product."""
        context: dict[str, Any] = {"reviews": [], "member_deals": []}
        if not product_uuid:
            return context

        try:
            rev_rows = self._supabase_get(
                "product_reviews",
                [
                    ("select", "reviews(id,title,url,category,published_at,award_class,award_label)"),
                    ("product_id", f"eq.{product_uuid}"),
                    ("status", "eq.approved"),
                    ("limit", "5"),
                ],
            )
            for r in rev_rows or []:
                rev = r.get("reviews")
                if isinstance(rev, dict) and rev.get("title"):
                    context["reviews"].append(rev)
        except Exception as exc:
            logger.debug("product_reviews lookup skipped for %s: %s", product_uuid, exc)

        try:
            deal_rows = self._supabase_get(
                "member_deals",
                [
                    ("select", "id,title,price,retailer_name,score,tipster,note,forum_url,posted_at"),
                    ("product_id", f"eq.{product_uuid}"),
                    ("order", "posted_at.desc.nullslast"),
                    ("limit", "5"),
                ],
            )
            context["member_deals"] = list(deal_rows or [])
        except Exception as exc:
            logger.debug("member_deals lookup skipped for %s: %s", product_uuid, exc)

        return context

    def get_prisfall(
        self,
        *,
        category: str | None = None,
        min_drop_pct: int = 10,
        period_days: int = 7,
        in_stock_only: bool = True,
        max_price: float | None = None,
        sort: str = "procent",
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Fetch daily price drops from https://prisgeek.se/prisfall via TSR dehydrated state."""
        valid_period = str(period_days) if period_days in (1, 3, 7, 30) else "7"
        params: dict[str, str] = {
            "period": valid_period,
            "min": str(min_drop_pct),
            "sort": sort,
        }
        if category:
            params["kat"] = category
        if max_price is not None:
            params["pmax"] = str(int(max_price))

        resp = self._http.get(f"{PRISGEEK_BASE}/prisfall", params=params)
        resp.raise_for_status()
        html = resp.text

        items: list[dict[str, Any]] = []

        cur_m = re.search(r"cur:\$R\[\d+\]=\[([0-9.,null]+)\]", html)
        prev_m = re.search(r"prev:\$R\[\d+\]=\[([0-9.,null]+)\]", html)
        prod_start = html.find("prod:$R[")
        cur_start = html.find("cur:$R[")

        if cur_m and prev_m and prod_start != -1 and cur_start != -1 and cur_start > prod_start:
            cur_arr = json.loads(f"[{cur_m.group(1)}]")
            prev_arr = json.loads(f"[{prev_m.group(1)}]")
            prod_block = html[prod_start:cur_start]
            prod_matches = list(
                re.finditer(
                    r'\$R\[\d+\]=\["([0-9a-f-]{36})","(\d+)","([^"]+)","((?:\\.|[^"])*)","((?:\\.|[^"])*)"',
                    prod_block,
                )
            )

            count = min(len(prod_matches), len(cur_arr), len(prev_arr))
            for idx in range(count):
                c_price = cur_arr[idx]
                p_price = prev_arr[idx]
                if c_price is None or p_price is None or c_price <= 0 or p_price < 100:
                    continue

                drop_pct = round((float(p_price) - float(c_price)) / float(p_price) * 100.0, 1)
                if drop_pct < float(min_drop_pct) or drop_pct > 80.0:
                    continue
                if max_price is not None and float(c_price) > float(max_price):
                    continue

                match = prod_matches[idx]
                pid = int(match.group(2))
                slug = match.group(3)
                name = _unescape_js_string(match.group(4))
                brand = _unescape_js_string(match.group(5))

                if REFURB_NAME_RE.search(name):
                    continue

                drop_kr = round(float(p_price) - float(c_price), 2)
                deal_badge = "Rekordlågt" if drop_pct >= 35.0 else "Bra pris"

                items.append(
                    {
                        "public_id": pid,
                        "name": name,
                        "brand": brand,
                        "url": self._format_product_url(pid, slug),
                        "current_price": round(float(c_price), 2),
                        "previous_price": round(float(p_price), 2),
                        "drop_kr": drop_kr,
                        "drop_pct": drop_pct,
                        "deal_badge": deal_badge,
                        "details": f"Nu {c_price:.0f} kr (var {p_price:.0f} kr, -{drop_pct:.1f}%)",
                    }
                )

            if sort == "procent":
                items.sort(key=lambda x: (-x["drop_pct"], -x["drop_kr"]))
            elif sort == "kronor":
                items.sort(key=lambda x: (-x["drop_kr"], -x["drop_pct"]))
            elif sort in ("pris", "pris_asc"):
                items.sort(key=lambda x: (x["current_price"], -x["drop_pct"]))

            if items:
                return items[:limit]

        # Fallback: parse rendered HTML table rows if TSR arrays were unavailable.
        soup = BeautifulSoup(html, "html.parser")
        seen_ids: set[int] = set()
        for row in soup.select("tr.sc-prow") or soup.select("table tbody tr"):
            link = row.select_one('a.sc-rowlink[href^="/produkt/"]') or row.select_one(
                'a[href^="/produkt/"]'
            )
            if not link:
                continue
            href = link.get("href", "")
            pid = extract_public_id(href)
            if pid is None or pid in seen_ids:
                continue
            name_el = link.select_one("span.sc-product-name--desktop") or link
            name = name_el.get_text(" ", strip=True)
            row_text = row.get_text(" ", strip=True).replace("\xa0", " ")
            price_el = row.select_one("span.sc-prow__price")
            current_price = parse_sek(price_el.get_text(" ", strip=True) if price_el else None)
            drop_btn = row.select_one('button[aria-label*="Rabatt"]')
            drop_pct = parse_drop_pct(drop_btn.get_text(" ", strip=True) if drop_btn else row_text)
            seen_ids.add(pid)
            items.append(
                {
                    "public_id": pid,
                    "name": name,
                    "url": urljoin(PRISGEEK_BASE, href),
                    "current_price": current_price,
                    "drop_pct": drop_pct,
                    "deal_badge": next(
                        (b for b in ("Rekordlågt", "Bra pris", "Högre än vanligt") if b in row_text),
                        None,
                    ),
                    "details": row_text[:240],
                }
            )
            if len(items) >= limit:
                break

        return items

    def get_medlemsfynd(
        self,
        *,
        category: str | None = None,
        min_score: int = 0,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Fetch active SweClockers community deals from PrisGeek's member_deals table."""
        params: list[tuple[str, str]] = [
            (
                "select",
                "id,title,category,retailer_name,price,url,tipster,score,forum_url,"
                "posted_at,first_seen_at,note,product_id,"
                "products(public_id,slug,name,brand,image_url)",
            ),
            ("is_active", "eq.true"),
            ("order", "posted_at.desc.nullslast,first_seen_at.desc"),
            ("limit", str(max(limit * 3, 60))),
        ]
        rows = self._supabase_get("member_deals", params) or []

        cat_filter = category.strip().lower() if category and category.strip() else None
        deals: list[dict[str, Any]] = []
        for row in rows:
            score = int(row.get("score") or 0)
            if score < min_score:
                continue
            row_cat = str(row.get("category") or "")
            if cat_filter and cat_filter not in row_cat.lower():
                continue

            prod = row.get("products") or {}
            public_id = prod.get("public_id") if isinstance(prod, dict) else None
            slug = prod.get("slug") if isinstance(prod, dict) else None
            product_url = self._format_product_url(public_id, slug) if public_id else None

            raw_price = row.get("price")
            try:
                price = float(raw_price) if raw_price is not None else None
            except (TypeError, ValueError):
                price = None

            deals.append(
                {
                    "id": row.get("id"),
                    "public_id": public_id,
                    "title": row.get("title") or (prod.get("name") if isinstance(prod, dict) else ""),
                    "product_name": prod.get("name") if isinstance(prod, dict) else None,
                    "brand": prod.get("brand") if isinstance(prod, dict) else None,
                    "category": row_cat,
                    "retailer_name": row.get("retailer_name") or "Okänd butik",
                    "price": price,
                    "score": score,
                    "tipster": row.get("tipster") or "",
                    "note": row.get("note") or "",
                    "store_url": unwrap_store_url(row.get("url")) or "",
                    "forum_url": row.get("forum_url") or "",
                    "product_url": product_url or row.get("url") or f"{PRISGEEK_BASE}/medlemsfynd",
                    "posted_at": row.get("posted_at") or row.get("first_seen_at"),
                }
            )
            if len(deals) >= limit:
                break
        return deals

    def get_fyndvaror(
        self,
        *,
        query: str | None = None,
        min_discount_pct: int = 5,
        max_price: float | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Fetch open-box, demo, and outlet bargains from https://prisgeek.se/fyndvaror."""
        resp = self._http.get(f"{PRISGEEK_BASE}/fyndvaror")
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        query_tokens = (
            [t.strip().lower() for t in re.split(r"\s+", query.strip()) if t.strip()]
            if query and query.strip()
            else []
        )

        items: list[dict[str, Any]] = []
        seen_keys: set[tuple[int, str]] = set()

        for row in soup.select("table tbody tr"):
            link = row.select_one('h2 a[href^="/produkt/"]') or row.select_one(
                'a[href^="/produkt/"]'
            )
            if not link:
                continue
            href = link.get("href", "")
            pid = extract_public_id(href)
            if pid is None:
                continue

            name_el = link.select_one("span.sc-product-name--desktop") or link
            name = name_el.get_text(" ", strip=True)
            row_text = row.get_text(" ", strip=True).replace("\xa0", " ")

            if query_tokens:
                blob = f"{name} {row_text}".lower()
                compact_blob = re.sub(
                    r"(\d+(?:[.,]\d+)?)\s+(gb|tb|mb|kb|w|mhz|ghz|hz|mm|cm)\b",
                    r"\1\2",
                    blob,
                )
                searchable = f"{blob} {compact_blob}"
                if not all(any(v in searchable for v in _token_variants(t)) for t in query_tokens):
                    continue

            till_links = row.select('a[href*="/api/public/till-butik"]')
            store_href = till_links[0].get("href") if till_links else None
            price: float | None = None
            retailer = "Okänd butik"
            for tl in till_links:
                tl_text = tl.get_text(" ", strip=True).replace("\xa0", " ")
                if "kr" in tl_text and price is None:
                    price = parse_sek(tl_text)
                elif tl_text and tl_text.lower() != "till butik":
                    retailer = tl_text

            if price is None:
                price = parse_sek(row_text)
            if price is None:
                continue
            if max_price is not None and price > float(max_price):
                continue

            drop_btn = row.select_one('button[aria-label*="Rabatt"], button[title*="Rabatt"]')
            discount_pct = parse_drop_pct(
                drop_btn.get_text(" ", strip=True) if drop_btn else row_text
            )
            if discount_pct is not None and discount_pct < float(min_discount_pct):
                continue

            body_cell = row.select_one("td.body .sc-prow") or row.select_one("td")
            meta_text = body_cell.get_text(" ", strip=True).replace("\xa0", " ") if body_cell else row_text
            condition = "Fyndvara"
            for cond_kw in ("Fyndvara", "Renoverad", "Begagnad", "Demo", "Open Box", "Klass"):
                cond_match = re.search(rf"({cond_kw}[^·]*)", meta_text, re.IGNORECASE)
                if cond_match:
                    condition = cond_match.group(1).strip()
                    break

            new_price_match = re.search(r"·\s*(\d[\d\s]*)\s*kr\b", meta_text)
            new_price = parse_sek(new_price_match.group(1)) if new_price_match else None

            key = (pid, retailer)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            items.append(
                {
                    "public_id": pid,
                    "name": name,
                    "url": urljoin(PRISGEEK_BASE, href),
                    "store_url": unwrap_store_url(str(store_href) if store_href else None) or "",
                    "price": round(price, 2),
                    "new_price": new_price,
                    "discount_pct": discount_pct,
                    "condition": condition,
                    "retailer": retailer,
                    "details": meta_text[:200],
                }
            )
            if len(items) >= limit:
                break

        # Supplement with outlet/refurbished Supabase search when query is given and SSR had fewer matches.
        if query and len(items) < limit:
            try:
                candidates = self.search_products(
                    query,
                    max_price=max_price,
                    in_stock_only=True,
                    exclude_refurbished=False,
                    limit=limit * 2,
                )
                for item in candidates:
                    is_outlet = bool(REFURB_NAME_RE.search(item["name"])) or any(
                        not _is_offer_new(o.get("condition")) for o in item.get("offers", [])
                    )
                    if not is_outlet:
                        continue
                    pid = int(item["public_id"])
                    retailer = item.get("best_retailer") or "Okänd butik"
                    if (pid, retailer) in seen_keys:
                        continue
                    seen_keys.add((pid, retailer))
                    items.append(
                        {
                            "public_id": pid,
                            "name": item["name"],
                            "url": item["url"],
                            "store_url": item.get("best_store_url") or "",
                            "price": item["lowest_price"],
                            "new_price": None,
                            "discount_pct": None,
                            "condition": "Fyndvara / Outlet",
                            "retailer": retailer,
                            "details": f"{item.get('category') or ''} · {retailer}",
                        }
                    )
                    if len(items) >= limit:
                        break
            except Exception as exc:
                logger.debug("Fyndvaror search supplement skipped: %s", exc)

        return items[:limit]

    def optimize_basket(self, public_ids: list[int]) -> dict[str, Any]:
        """Compare split-store cheapest totals vs. single-store consolidation incl. shipping."""
        unique_ids = list(dict.fromkeys(int(pid) for pid in public_ids if pid))
        if not unique_ids:
            return {
                "items": [],
                "cheapest_split": {"total_with_shipping": 0.0, "stores": []},
                "cheapest_split_total": 0.0,
                "single_store_options": [],
                "recommended": None,
            }

        items_summary: list[dict[str, Any]] = []
        retailer_offers: dict[str, dict[int, dict[str, Any]]] = {}
        split_by_store: dict[str, list[dict[str, Any]]] = {}

        for pid in unique_ids:
            try:
                details = self.get_product_details(
                    pid, in_stock_only=True, exclude_refurbished=True
                )
            except Exception as exc:
                logger.warning("Basket lookup failed for %s: %s", pid, exc)
                continue

            offers = [o for o in (details.get("offers") or []) if o.get("in_stock")]
            if not offers:
                offers = details.get("offers") or []

            best_total_offer = (
                min(offers, key=lambda o: (o["total_price"], o["price"])) if offers else None
            )
            items_summary.append(
                {
                    "public_id": pid,
                    "name": details["name"],
                    "url": details["url"],
                    "best_price": best_total_offer["price"] if best_total_offer else None,
                    "best_shipping": best_total_offer["shipping"] if best_total_offer else None,
                    "best_total_price": best_total_offer["total_price"] if best_total_offer else None,
                    "best_retailer": best_total_offer["retailer_name"] if best_total_offer else None,
                    "offer_count": len(offers),
                }
            )

            if best_total_offer:
                store = best_total_offer["retailer_name"]
                split_by_store.setdefault(store, []).append(
                    {"public_id": pid, "name": details["name"], **best_total_offer}
                )

            for off in offers:
                store = off["retailer_name"]
                store_map = retailer_offers.setdefault(store, {})
                existing = store_map.get(pid)
                if existing is None or off["total_price"] < existing["total_price"]:
                    store_map[pid] = {"name": details["name"], **off}

        split_stores: list[dict[str, Any]] = []
        cheapest_split_total = 0.0
        for store, chosen in split_by_store.items():
            subtotal = round(sum(c["price"] for c in chosen), 2)
            shipping = round(max((c["shipping"] for c in chosen), default=0.0), 2)
            total = round(subtotal + shipping, 2)
            cheapest_split_total = round(cheapest_split_total + total, 2)
            split_stores.append(
                {
                    "retailer": store,
                    "items": [
                        {"public_id": c["public_id"], "name": c["name"], "price": c["price"]}
                        for c in chosen
                    ],
                    "subtotal_price": subtotal,
                    "shipping_cost": shipping,
                    "total_with_shipping": total,
                }
            )
        split_stores.sort(key=lambda s: -len(s["items"]))

        single_store_options: list[dict[str, Any]] = []
        for store, pid_map in retailer_offers.items():
            missing = [
                it["name"] for it in items_summary if it["public_id"] not in pid_map
            ]
            subtotal = round(sum(o["price"] for o in pid_map.values()), 2)
            shipping = round(max((o["shipping"] for o in pid_map.values()), default=0.0), 2)
            total = round(subtotal + shipping, 2)
            single_store_options.append(
                {
                    "retailer": store,
                    "items_available": len(pid_map),
                    "total_items": len(items_summary),
                    "missing_items": missing,
                    "subtotal_price": subtotal,
                    "shipping_cost": shipping,
                    "total_with_shipping": total,
                    "items": [
                        {"public_id": pid, "name": o["name"], "price": o["price"]}
                        for pid, o in pid_map.items()
                    ],
                }
            )
        single_store_options.sort(
            key=lambda opt: (-opt["items_available"], opt["total_with_shipping"])
        )

        recommended: dict[str, Any] | None = None
        if single_store_options and items_summary:
            top_single = single_store_options[0]
            if (
                top_single["items_available"] == len(items_summary)
                and top_single["total_with_shipping"] <= cheapest_split_total
            ):
                recommended = {
                    "strategy": "single_store",
                    "retailer": top_single["retailer"],
                    "total_with_shipping": top_single["total_with_shipping"],
                    "savings_vs_split": round(
                        cheapest_split_total - top_single["total_with_shipping"], 2
                    ),
                }
            else:
                recommended = {
                    "strategy": "split",
                    "stores": [s["retailer"] for s in split_stores],
                    "total_with_shipping": cheapest_split_total,
                }

        return {
            "items": items_summary,
            "cheapest_split": {
                "total_with_shipping": cheapest_split_total,
                "stores": split_stores,
            },
            "cheapest_split_total": cheapest_split_total,
            "single_store_options": single_store_options[:8],
            "recommended": recommended,
        }
