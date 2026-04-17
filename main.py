import json as _json
import re
import unicodedata
from datetime import datetime, timedelta
from urllib.parse import quote
import httpx
from fastapi import Depends, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import desc, func, or_
from sqlalchemy.orm import Session
from database import engine, get_db, migrate
from fetcher import sync_shop, parse_excel
from models import (
    Base,
    ExternalOrderConfig,
    ExternalOrderConfigHoang,
    ExternalOrderTracking,
    ExternalOrderTrackingHoang,
    Order,
    ShopMeta,
    TakenOrder,
)
from shops_config import BLOCKED_SHOPS, SELLER_ID, SELLER_TOKEN, SHOP_NAME_MAP, get_shops_map

# ── Key management cho order-info ─────────────────────────
VALID_KEYS = {
    "HOANG5611": 0,
    "Hoang5611": 0,
    "PHONE-KEY-PHUONG2000": 0,
}
KEY_LIMIT = 10
# Lưu lịch sử tra cứu: {key: [{"order_code": ..., "time": ...}]}
KEY_HISTORY: dict = {k: [] for k in VALID_KEYS}
# Lưu lịch sử đăng nhập: {key: [{"event": "login/logout", "time": ...}]}
LOGIN_HISTORY: list = []  # [{key, event, time}]
# Database setup
Base.metadata.create_all(bind=engine)
migrate()
UNLIMITED_KEYS = {"HOANG5611", "Hoang5611", "PHONE-KEY-PHUONG2000"}

app = FastAPI(title="Chiaki Order Dashboard")
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── Helper functions ───────────────────────────────────────
FULL_ACCESS_IDS = {"HOANG5611"}
TAKEN_ORDER_ADMIN_KEY = "HOANG5611"
SENSITIVE_SHOPS = {"4647", "4732"}
SENSITIVE_TOTAL_THRESHOLD = 2_500_000
PHONE_KEY_PHUONG_ALLOWED_SHOPS = {"4917", "4940", "5096", "5125", "5114"}
LOGIN_ID_META = {
    "LOGIN-KEY-PHUONG2000": {"hours": 1, "label": "Phương"},
    "LOGIN-KEY-CHANGTESTUSER": {"hours": 9999999999, "label": "Hoàng"},
    "Hoang5611": {"hours": 9999999999, "label": "Hoàng"},
    "HOANG5611": {"hours": 9999999999, "label": "Hoàng"},
    "unlimited_id": {"hours": 9999999999, "label": "Unlimited"},
}

def is_full_access_user(user_id: str) -> bool:
    return str(user_id or "").strip().upper() in FULL_ACCESS_IDS


def is_taken_orders_admin(user_id: str) -> bool:
    return str(user_id or "").strip().upper() == TAKEN_ORDER_ADMIN_KEY


def can_delete_taken_orders(user_id: str) -> bool:
    return str(user_id or "").strip() == TAKEN_ORDER_ADMIN_KEY

def should_hide_order(order, user_id: str) -> bool:
    if not order or is_full_access_user(user_id):
        return False
    shop_id = str(getattr(order, "shop_id", "") or "").strip()
    total = getattr(order, "total", 0) or 0
    try:
        total = float(total)
    except Exception:
        total = 0
    return shop_id in SENSITIVE_SHOPS or total >= SENSITIVE_TOTAL_THRESHOLD

def serialize_order(o):
    return {
        "order_code":    o.order_code,
        "sync_id":       o.sync_id,
        "order_date":    o.order_date,
        "shop_id":       o.shop_id,
        "shop_name":     o.shop_name,
        "buyer_name":    o.buyer_name,
        "customer_name": o.customer_name,
        "address":       o.address,
        "product":       o.product,
        "quantity":      o.quantity,
        "total":         o.total,
        "status":        o.status,
        "fetched_at":    o.fetched_at.isoformat() if o.fetched_at else None,
        "restricted":    False,
    }


def aggregate_orders(rows: list[Order]) -> list[dict]:
    grouped: dict[str, dict] = {}

    for row in rows:
        code = str(getattr(row, "order_code", "") or "").strip()
        if not code:
            continue

        product_name = str(getattr(row, "product", "") or "").strip()
        quantity = getattr(row, "quantity", 0) or 0
        try:
            quantity = int(quantity)
        except Exception:
            quantity = 0

        product_line = product_name or "—"
        if quantity:
            product_line = f"{product_line} x{quantity}"

        current_total = getattr(row, "total", 0) or 0
        try:
            current_total = float(current_total)
        except Exception:
            current_total = 0

        fetched_at = getattr(row, "fetched_at", None)
        status = str(getattr(row, "status", "") or "").strip()

        if code not in grouped:
            base = serialize_order(row)
            base["quantity"] = quantity
            base["total"] = current_total
            base["_product_lines"] = [product_line]
            base["_statuses"] = [status] if status else []
            base["_sync_ids"] = [str(getattr(row, "sync_id", "") or "").strip()] if getattr(row, "sync_id", None) else []
            grouped[code] = base
            continue

        item = grouped[code]
        item["quantity"] = int(item.get("quantity") or 0) + quantity
        item["total"] = float(item.get("total") or 0) + current_total

        if product_line not in item["_product_lines"]:
            item["_product_lines"].append(product_line)
        if status and status not in item["_statuses"]:
            item["_statuses"].append(status)
        sync_id = str(getattr(row, "sync_id", "") or "").strip()
        if sync_id and sync_id not in item["_sync_ids"]:
            item["_sync_ids"].append(sync_id)

        existing_fetched_at = item.get("fetched_at")
        existing_dt = None
        if existing_fetched_at:
            try:
                existing_dt = datetime.fromisoformat(existing_fetched_at)
            except Exception:
                existing_dt = None
        if fetched_at and (existing_dt is None or fetched_at > existing_dt):
            item["fetched_at"] = fetched_at.isoformat()

    result = []
    for item in grouped.values():
        product_lines = item.pop("_product_lines", [])
        statuses = item.pop("_statuses", [])
        sync_ids = item.pop("_sync_ids", [])
        item["product"] = "<br>".join(product_lines) if product_lines else "—"
        if statuses:
            item["status"] = " | ".join(statuses)
        if sync_ids:
            item["sync_id"] = " | ".join(sync_ids)
        result.append(item)

    return result


def sort_aggregated_orders(rows: list[dict], sort: str) -> list[dict]:
    if sort == "total_desc":
        return sorted(rows, key=lambda item: float(item.get("total") or 0), reverse=True)
    if sort == "total_asc":
        return sorted(rows, key=lambda item: float(item.get("total") or 0))
    if sort == "date_desc":
        return sorted(rows, key=lambda item: str(item.get("order_date") or ""), reverse=True)
    if sort == "date_asc":
        return sorted(rows, key=lambda item: str(item.get("order_date") or ""))
    return sorted(rows, key=lambda item: str(item.get("fetched_at") or ""), reverse=True)


def build_sync_delta(old_codes: set[str], new_codes: set[str]) -> dict:
    removed_codes = sorted(old_codes - new_codes)
    added_codes = sorted(new_codes - old_codes)
    return {
        "removed_count": len(removed_codes),
        "added_count": len(added_codes),
        "removed_codes": removed_codes,
        "added_codes": added_codes,
    }


def can_manage_external_orders(user_id: str) -> bool:
    return str(user_id or "").strip().upper() == TAKEN_ORDER_ADMIN_KEY


def can_view_hoang_orders(user_id: str) -> bool:
    return str(user_id or "").strip().upper() == TAKEN_ORDER_ADMIN_KEY


def get_user_capabilities(user_id: str) -> dict:
    normalized = str(user_id or "").strip()
    normalized_upper = normalized.upper()
    return {
        "full_access": is_full_access_user(normalized),
        "manage_external_orders": can_manage_external_orders(normalized),
        "view_hoang_orders": can_view_hoang_orders(normalized),
        "admin_tools": normalized_upper == TAKEN_ORDER_ADMIN_KEY or normalized == "LOGIN-KEY-PHUONG2000",
        "view_history_panel": can_view_hoang_orders(normalized),
        "manage_taken_orders": is_taken_orders_admin(normalized),
        "delete_taken_orders": can_delete_taken_orders(normalized),
        "view_taken_orders": bool(normalized),
    }


def normalize_lookup_order_code(full_code: str | None) -> str:
    raw = str(full_code or "").strip()
    if not raw:
        return ""
    parts = [part.strip() for part in raw.split("_") if part.strip()]
    return parts[-1] if parts else raw


def build_payment_status_text(payment_type: str | None, is_approved: str | None, prepaid_time: str | None) -> str:
    normalized_type = str(payment_type or "").strip().lower()
    approved = str(is_approved or "").strip()
    prepaid_time_text = str(prepaid_time or "").strip()

    if normalized_type in {"home", "cod"}:
        return "COD — Thu tiền khi nhận hàng"
    if normalized_type in {"atm", "online", "bank"} and approved == "1":
        text = f"Đã thanh toán online ({normalized_type.upper()})"
        if prepaid_time_text:
            text += f" lúc {prepaid_time_text}"
        return text
    if normalized_type in {"atm", "online", "bank"} and approved != "1":
        return f"Chờ xác nhận thanh toán ({normalized_type.upper()})"
    return f"Không rõ ({payment_type or 'Không rõ'})"


TAKEN_ORDER_STATUS_LABELS = {
    "waiting_waybill": "Chờ tạo MVĐ",
    "created_waybill": "Đã tạo MVĐ",
}


def serialize_taken_order(row: TakenOrder, include_details: bool = False) -> dict:
    payload = {
        "id": row.id,
        "order_code": row.order_code,
        "lookup_order_code": row.lookup_order_code or normalize_lookup_order_code(row.order_code),
        "take_status": row.take_status or "waiting_waybill",
        "take_status_label": TAKEN_ORDER_STATUS_LABELS.get(row.take_status or "waiting_waybill", "Chờ tạo MVĐ"),
    }
    if include_details:
        payload.update({
            "taken_by": row.taken_by or "",
            "taken_at": row.taken_at.isoformat() if row.taken_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "shop_name": row.shop_name or "—",
            "order_date": row.order_date or "—",
            "customer_name": row.customer_name or "—",
            "phone": row.phone or "—",
            "address": row.address or "—",
            "product": row.product or "—",
            "quantity": row.quantity if row.quantity is not None else "—",
            "prepaid_amount": row.prepaid_amount or "—",
            "payment_status": row.payment_status or "—",
        })
    return payload


def normalize_sync_text(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKD", text)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", normalized).strip()


def normalize_view_sync_stage(sync_stage: str | None) -> str:
    stage = str(sync_stage or "").strip().lower()
    if stage in {"confirm", "pending"}:
        return "confirm"
    if stage in {"pickup", "waiting"}:
        return "pickup"
    return stage


def matches_sync_stage(status: str | None, sync_stage: str | None, raw_text: str | None = None) -> bool:
    stage = normalize_view_sync_stage(sync_stage)
    if not stage:
        return True

    normalized_status = normalize_sync_text(status)
    if not normalized_status:
        return False

    if stage == "confirm":
        return normalized_status == "cho x.nhan"

    if stage == "pickup":
        return normalized_status in {
            "da xac nhan(y.cau x.hang)",
            "da tao mvd/cho lay hang",
            "cho lay hang",
            "out_products_in_progress",
            "request_out",
            "receive_wating",
            "receive_waiting",
        }

    return True


def filter_orders_by_sync_stage(rows: list, sync_stage: str | None) -> list:
    stage = normalize_view_sync_stage(sync_stage)
    if not stage:
        return rows
    filtered_rows = []
    for row in rows:
        if isinstance(row, dict):
            status = row.get("status", "")
            raw_text = row.get("raw_data", "")
        else:
            status = getattr(row, "status", "")
            raw_text = getattr(row, "raw_data", "")
        if matches_sync_stage(status, stage, raw_text):
            filtered_rows.append(row)
    return filtered_rows


def apply_sync_stage_filter(query, sync_stage: str | None):
    stage = normalize_view_sync_stage(sync_stage)
    status_col = func.lower(func.coalesce(Order.status, ""))

    if stage == "confirm":
        return query.filter(or_(
            status_col == "chờ x.nhận",
            status_col == "cho x.nhan"
        ))

    if stage == "pickup":
        return query.filter(or_(
            status_col == "đã xác nhận(y.cầu x.hàng)",
            status_col == "da xac nhan(y.cau x.hang)",
            status_col == "đã tạo mvđ/chờ lấy hàng",
            status_col == "da tao mvd/cho lay hang",
            status_col == "chờ lấy hàng",
            status_col == "cho lay hang",
            status_col == "out_products_in_progress",
            status_col == "request_out",
            status_col == "receive_wating",
            status_col == "receive_waiting",
        ))

    return query


def build_shop_download_url(shop_id: str, status: str = "receive_wating") -> str:
    normalized_status = str(status or "").strip().lower()
    if normalized_status == "waiting":
        normalized_status = "receive_wating"

    today = datetime.now()
    since = today - timedelta(days=14)

    def fmt(d):
        return quote(d.strftime("%d/%m/%Y"), safe="")

    range_date = f"{fmt(since)}+-+{fmt(today)}"
    return (
        f"https://api.chiaki.vn/api/{shop_id}/export-excel-order"
        f"?source=seller&page_index=1&page_size=500&status={normalized_status}"
        f"&range_date={range_date}"
        f"&date_type=created_at&order=create-desc"
        f"&Seller_id={SELLER_ID}&Seller_token={SELLER_TOKEN}"
    )


def normalize_seller_access_token(access_token: str | None) -> str:
    token = str(access_token or "").strip()
    if not token:
        return ""
    return token if token.lower().startswith("bearer ") else f"Bearer {token}"


def build_seller_get_order_request(shop_id: str, access_token: str | None) -> tuple[str, dict, dict]:
    today = datetime.now()
    since = today - timedelta(days=30)
    range_date = f"{since.strftime('%d/%m/%Y')} - {today.strftime('%d/%m/%Y')}"
    params = {
        "source": "seller",
        "page_index": "1",
        "page_size": "20",
        "range_date": range_date,
        "order": "create-desc",
    }
    headers = {
        "Host": "api.chiaki.vn",
        "baggage": "sentry-environment=production,sentry-public_key=418f1affd8b5477baa885b6b4da50b79,sentry-transaction=SellerOrdersScreen,sentry-sampled=true",
        "sellerid": shop_id,
        "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
        "platform": "ios",
        "accesstoken": normalize_seller_access_token(access_token),
        "Cache-Control": "no-cache",
        "User-Agent": "chiakiApp/3.6.4",
        "Connection": "keep-alive",
        "Accept": "application/json, text/plain, */*",
    }
    return f"https://api.chiaki.vn/seller/{shop_id}/get-order", params, headers


def first_non_empty(mapping: dict | None, keys: list[str]):
    if not isinstance(mapping, dict):
        return None
    lowered = {str(k).lower(): k for k in mapping.keys()}
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
        real_key = lowered.get(key.lower())
        if real_key is not None and mapping[real_key] not in (None, ""):
            return mapping[real_key]
    return None


def recursive_first_non_empty(value, keys: list[str], max_depth: int = 3):
    if max_depth < 0:
        return None
    if isinstance(value, dict):
        direct = first_non_empty(value, keys)
        if direct not in (None, ""):
            return direct
        for child in value.values():
            found = recursive_first_non_empty(child, keys, max_depth - 1)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for child in value:
            found = recursive_first_non_empty(child, keys, max_depth - 1)
            if found not in (None, ""):
                return found
    return None


def to_int(value, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(str(value).replace(",", "").strip()))
    except Exception:
        return default


def to_float(value, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(str(value).replace(",", "").replace("đ", "").strip())
    except Exception:
        return default


def score_order_candidate(item: dict) -> int:
    if not isinstance(item, dict):
        return 0
    keys = {str(k).lower() for k in item.keys()}
    score = 0
    if keys & {"order_code", "ordercode", "code", "order_id", "orderid"}:
        score += 5
    if keys & {"sync_id", "syncid"}:
        score += 5
    if keys & {"status", "status_name", "order_status"}:
        score += 2
    if keys & {"create_time", "created_at", "verified_time", "order_date"}:
        score += 2
    if keys & {"delivery_address", "address", "receiver_name", "related_user_name"}:
        score += 2
    if keys & {"products", "items", "order_items", "product_items"}:
        score += 2
    return score


def extract_json_order_rows(payload) -> list[dict]:
    candidates: list[tuple[int, list[dict]]] = []

    def visit(value, depth: int = 0):
        if depth > 8:
            return
        if isinstance(value, list):
            dict_items = [item for item in value if isinstance(item, dict)]
            if dict_items:
                score = sum(score_order_candidate(item) for item in dict_items[:5])
                if score > 0:
                    candidates.append((score, dict_items))
            for item in value[:20]:
                visit(item, depth + 1)
            return
        if isinstance(value, dict):
            for child in value.values():
                visit(child, depth + 1)

    visit(payload)
    if not candidates:
        return []
    candidates.sort(key=lambda item: (item[0], len(item[1])), reverse=True)
    return candidates[0][1]


def extract_product_rows(order: dict) -> list[dict]:
    product_keys = ["products", "items", "order_items", "product_items", "details", "cart_items"]
    for key in product_keys:
        value = first_non_empty(order, [key])
        if isinstance(value, list):
            products = [item for item in value if isinstance(item, dict)]
            if products:
                return products
    direct_product = first_non_empty(order, ["product_name", "productName", "product", "product_title", "title"])
    if direct_product:
        return [{"product_name": direct_product, "quantity": first_non_empty(order, ["quantity", "qty"])}]
    return [{}]


def format_json_order_code(shop_id: str, raw_code, index: int) -> str:
    code = str(raw_code or "").strip()
    if not code:
        code = str(index + 1)
    return code if code.startswith(f"{shop_id}_") else f"{shop_id}_{code}"


def parse_orders_json_payload(payload, shop_id: str, shop_name: str) -> list[dict]:
    order_rows = extract_json_order_rows(payload)
    if not order_rows:
        return []

    parsed: list[dict] = []
    for index, order in enumerate(order_rows):
        raw_code = first_non_empty(order, ["order_code", "orderCode", "code", "order_id", "orderId", "id"])
        order_code = format_json_order_code(shop_id, raw_code, index)
        sync_id = first_non_empty(order, ["sync_id", "syncId", "syncID"])
        status = first_non_empty(order, ["status_name", "statusName", "order_status", "orderStatus", "status"])
        if not status:
            status = "Đã xác nhận(y.cầu x.hàng)"

        order_date = first_non_empty(order, [
            "verified_time", "verifiedTime", "create_time", "createTime",
            "created_at", "createdAt", "order_date", "orderDate", "date",
        ])
        customer_name = recursive_first_non_empty(order, [
            "related_user_name", "receiver_name", "customer_name", "customerName",
            "buyer_name", "buyerName", "full_name", "fullName",
        ])
        buyer_name = recursive_first_non_empty(order, ["buyer_name", "buyerName", "receiver_name", "full_name", "fullName"])
        phone = recursive_first_non_empty(order, ["phone", "receiver_phone", "customer_phone", "telephone"])
        address = recursive_first_non_empty(order, ["delivery_address", "full_address", "address"])
        order_total = to_float(first_non_empty(order, [
            "total", "total_money", "totalMoney", "total_price", "totalPrice",
            "total_amount", "totalAmount", "pay_money", "payMoney", "amount", "cod_amount",
        ]))
        raw_data = _json.dumps(order, ensure_ascii=False)

        product_rows = extract_product_rows(order)
        for product_index, product in enumerate(product_rows):
            product_name = recursive_first_non_empty(product, [
                "product_name", "productName", "name", "title", "product_title", "display_name",
            ], max_depth=2) or recursive_first_non_empty(order, ["product_name", "productName", "product"], max_depth=1)
            quantity = to_int(first_non_empty(product, ["quantity", "qty", "amount", "product_quantity"]), 0)
            line_total = to_float(first_non_empty(product, [
                "total", "total_price", "totalPrice", "amount", "final_price",
                "price", "sell_price", "sale_price",
            ]))
            if not line_total and product_index == 0:
                line_total = order_total
            parsed.append({
                "order_code": order_code,
                "sync_id": str(sync_id or "").strip(),
                "shop_id": shop_id,
                "shop_name": shop_name,
                "buyer_name": str(buyer_name or "").strip(),
                "customer_name": str(customer_name or "").strip(),
                "phone": str(phone or "").strip(),
                "address": str(address or "").strip(),
                "product": str(product_name or "").strip(),
                "quantity": quantity,
                "total": line_total,
                "status": str(status or "").strip(),
                "order_date": str(order_date or "").strip(),
                "raw_data": raw_data,
            })
    return parsed


def sanitize_download_filename_part(value: str, default: str = "Shop") -> str:
    text = unicodedata.normalize("NFC", str(value or "").strip())
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text or default


def build_shop_download_filename(shop_id: str, shop_name: str) -> str:
    normalized_shop_id = re.sub(r"[^0-9A-Za-z_-]+", "", str(shop_id or "").strip()) or "SHOP"
    normalized_shop_name = sanitize_download_filename_part(shop_name)
    return f"{normalized_shop_id}_{normalized_shop_name}.xlsx"


def build_ascii_fallback_filename(filename: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", str(filename or ""))
    ascii_name = ascii_name.encode("ascii", "ignore").decode("ascii")
    ascii_name = re.sub(r'[^A-Za-z0-9._ -]+', "", ascii_name)
    ascii_name = re.sub(r"\s+", " ", ascii_name).strip(" .")
    return ascii_name or "download.xlsx"


def escape_for_shell_double_quotes(value: str) -> str:
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
    )


async def fetch_order_info_data(order_code: str, key: str, user_id: str, db: Session):
    if not order_code or not key:
        raise ValueError("Thiếu mã đơn hàng hoặc key.")
    if key not in VALID_KEYS:
        raise PermissionError("Key không hợp lệ.")
    if len(order_code) < 9:
        raise ValueError("Mã đơn hàng không hợp lệ.")

    input_id = order_code[2:9]
    url = f"https://ec.megaads.vn/service/inoutput/find-promotion-codes-api?inoutputId={input_id}"
    session = "eyJpdiI6ImIra2pmWitCVVRRTlp2K3pRUUZOZ1E9PSIsInZhbHVlIjoibXpYaFhkQmVZU1VMRFRKWWhEcXRCdnBFSWdycVNzNFlSVHpGWjVYT0hTVDFpdlErVWxDSWhEaVdcL3JyT2RvSjZIcDNkMVJSYTllZDJMMTlsR2ZIQ3BnPT0iLCJtYWMiOiI2MDc2MTFlNDg0MTg4M2IyNDBiNDAzMDE4ZWE0MTk0ZTFkNDdlNGU3MjQ0ZjA3ODFkYTlkYzZiMjcyOTEyMzNmIn0%3D"

    async with httpx.AsyncClient(timeout=15) as client:
        res = await client.get(url, headers={
            "Accept": "application/json, text/plain, */*",
            "platform": "ios",
            "Cookie": f"laravel_session={session}",
            "User-Agent": "chiakiApp/3.6.2"
        })
    data = res.json()
    d = data.get("result") or data.get("data") or {}
    if isinstance(d, list):
        d = d[0] if d else {}

    def g(*keys):
        for k in keys:
            v = d.get(k)
            if v:
                return str(v)
        return "—"

    phone = next((x for x in d.get("search", "").split() if x.isdigit() and len(x) >= 9), "—")
    db_order = db.query(Order).filter(Order.order_code.like(f"%_{order_code}")).first()
    hide_order = should_hide_order(db_order, user_id)
    db_product = db_order.product if db_order else "—"
    shop_id_from_api = g("store_code", "creator_name")
    db_shop_name = (
        SHOP_NAME_MAP.get(shop_id_from_api)
        or (db_order.shop_name if db_order else None)
        or shop_id_from_api
    )
    db_total = f"{int(db_order.total):,} đ".replace(",", ".") if db_order and db_order.total else "—"
    try:
        meta_raw = d.get("meta_data", "{}")
        meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else (meta_raw or {})
    except Exception:
        meta = {}

    effective_shop_id = str((db_order.shop_id if db_order and db_order.shop_id else shop_id_from_api) or "").strip()
    if key == "PHONE-KEY-PHUONG2000" and effective_shop_id not in PHONE_KEY_PHUONG_ALLOWED_SHOPS:
        raise LookupError("Không có thông tin đơn hàng.")
    if hide_order:
        raise LookupError("Không tìm thấy đơn hàng hoặc bạn không có quyền xem đơn này.")

    return {
        "raw": d,
        "meta": meta,
        "db_order": db_order,
        "payment_type": d.get("payment_type", ""),
        "payment_status": build_payment_status_text(
            d.get("payment_type", ""),
            d.get("is_approved_prepaid", "0"),
            d.get("prepaid_time"),
        ),
        "shop_name": db_shop_name,
        "sync_id": (db_order.sync_id if db_order else None) or d.get("sync_id"),
        "product": db_product,
        "phone": phone,
        "prepaid_amount_text": db_total,
        "customer_id": meta.get("customer_id") or d.get("related_user_id"),
        "delivery_location_id": g("delivery_location_id"),
        "district_delivery_id": g("district_delivery_id"),
        "commune_delivery_id": g("commune_delivery_id"),
        "order_date": g("verified_time", "create_time"),
        "address": g("delivery_address"),
        "customer_name": g("related_user_name", "receiver_name"),
        "email": g("email_id"),
        "shipping_code": g("shipping_code"),
    }


def normalize_external_scope(scope: str | None) -> str:
    value = str(scope or "phuong").strip().lower()
    return value if value in {"phuong", "hoang"} else "phuong"


def get_external_models(scope: str):
    normalized = normalize_external_scope(scope)
    if normalized == "hoang":
        return ExternalOrderTrackingHoang, ExternalOrderConfigHoang
    return ExternalOrderTracking, ExternalOrderConfig


def serialize_external_order(o: ExternalOrderTracking):
    return {
        "code": o.order_code,
        "cod": int(o.cod_amount or 0),
        "status": o.status or "unknown",
        "is_paid": bool(o.is_paid),
        "updated_at": o.updated_at.isoformat() if o.updated_at else None,
    }


def get_external_order_config(db: Session, scope: str = "phuong"):
    _, config_model = get_external_models(scope)
    config = db.query(config_model).filter(config_model.id == 1).first()
    if not config:
        config = config_model(id=1, fee_items_json='{"fee_items":[],"payment_history":[]}')
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


def serialize_external_order_config(config: ExternalOrderConfig):
    fee_items = []
    payment_history = []
    try:
        raw = config.fee_items_json or '{"fee_items":[],"payment_history":[]}'
        parsed = _json.loads(raw)
        if isinstance(parsed, list):
            fee_items = parsed
        elif isinstance(parsed, dict):
            fee_items_raw = parsed.get("fee_items")
            payment_history_raw = parsed.get("payment_history")
            if isinstance(fee_items_raw, list):
                fee_items = fee_items_raw
            if isinstance(payment_history_raw, list):
                payment_history = payment_history_raw
    except Exception:
        fee_items = []
        payment_history = []
    return {
        "fee_items": fee_items,
        "payment_history": payment_history,
        "updated_at": config.updated_at.isoformat() if config.updated_at else None,
    }

# ── API Endpoints ──────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    with open("static/index.html", encoding="utf-8") as f:
        return f.read()

@app.get("/order", response_class=HTMLResponse)
@app.get("/order/", response_class=HTMLResponse)
async def order_lookup_page():
    with open("static/order.html", encoding="utf-8") as f:
        return f.read()

@app.get("/SPXP", response_class=HTMLResponse)
@app.get("/SPXP/", response_class=HTMLResponse)
async def spxp_dashboard_page():
    with open("static/spxp.html", encoding="utf-8") as f:
        return f.read()

@app.get("/SPXHOANG", response_class=HTMLResponse)
@app.get("/SPXHOANG/", response_class=HTMLResponse)
async def spxhoang_dashboard_page():
    with open("static/spxhoang.html", encoding="utf-8") as f:
        return f.read()

@app.get("/api/summary")
def get_summary(sync_stage: str = Query(""), db: Session = Depends(get_db)):
    shops = db.query(ShopMeta).all()
    filtered_orders = filter_orders_by_sync_stage(db.query(Order).all(), sync_stage)
    total = len({str(getattr(order, "order_code", "") or "").strip() for order in filtered_orders if getattr(order, "order_code", None)})
    order_count_by_shop: dict[str, set[str]] = {}
    for order in filtered_orders:
        shop_id = str(getattr(order, "shop_id", "") or "").strip()
        order_code = str(getattr(order, "order_code", "") or "").strip()
        if not shop_id or not order_code:
            continue
        order_count_by_shop.setdefault(shop_id, set()).add(order_code)

    shop_entries = []
    for s in shops:
        order_count = len(order_count_by_shop.get(s.shop_id, set()))
        shop_entries.append({
            "shop_id": s.shop_id,
            "shop_name": s.shop_name,
            "order_count": order_count,
            "last_sync": s.last_sync.isoformat() if s.last_sync else None,
        })
    return {
        "total_orders": total,
        "total_shops": len(shop_entries),
        "shops": shop_entries
    }

@app.get("/api/orders")
def get_orders(
    request: Request,
    shop_id: str = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(200, le=200),
    sort: str = Query("default"),
    sync_stage: str = Query(""),
    db: Session = Depends(get_db)
):
    user_id = request.headers.get('X-User-ID', '')

    if shop_id and shop_id in BLOCKED_SHOPS and not is_full_access_user(user_id):
        return {
            "total": 0, "page": page, "data": [],
            "blocked": True,
            "message": "Shop này bị chặn trích xuất đơn hàng"
        }

    q = db.query(Order)
    if shop_id:
        q = q.filter(Order.shop_id == shop_id)
    if not is_full_access_user(user_id):
        q = q.filter(~Order.shop_id.in_(SENSITIVE_SHOPS)).filter(
            or_(Order.total == None, Order.total < SENSITIVE_TOTAL_THRESHOLD)
        )
    orders = filter_orders_by_sync_stage(q.all(), sync_stage)
    aggregated_orders = sort_aggregated_orders(aggregate_orders(orders), sort)
    total = len(aggregated_orders)
    page_rows = aggregated_orders[(page - 1) * limit: page * limit]

    return {
        "total": total,
        "page": page,
        "data": page_rows
    }

@app.get("/api/orders/search-products")
def search_orders_by_product(
    request: Request,
    q: str = Query(..., min_length=1),
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db)
):
    user_id = request.headers.get('X-User-ID', '')
    normalized_q = q.strip().lower()
    tokens = [token.strip().lower() for token in normalized_q.split() if token.strip()]
    if not normalized_q or not tokens:
        return {"total": 0, "data": []}

    filters = [func.lower(Order.product).contains(normalized_q)]
    filters.extend(func.lower(Order.product).contains(token) for token in tokens)

    q_orders = db.query(Order).filter(or_(*filters))
    if not is_full_access_user(user_id):
        q_orders = q_orders.filter(~Order.shop_id.in_(SENSITIVE_SHOPS)).filter(
            or_(Order.total == None, Order.total < SENSITIVE_TOTAL_THRESHOLD)
        )
    orders = q_orders.order_by(desc(Order.order_date), desc(Order.fetched_at)).limit(limit).all()
    aggregated_orders = sort_aggregated_orders(aggregate_orders(orders), "date_desc")

    return {
        "total": len(aggregated_orders),
        "data": aggregated_orders
    }

@app.post("/api/update-shopname")
def update_shopname(body: dict, db: Session = Depends(get_db)):
    shop_id = body.get("shop_id")
    shop_name = body.get("shop_name")
    if not shop_id or not shop_name:
        return {"ok": False}
    meta = db.query(ShopMeta).filter(ShopMeta.shop_id == shop_id).first()
    if meta:
        meta.shop_name = shop_name
        db.query(Order).filter(Order.shop_id == shop_id).update({"shop_name": shop_name})
        db.commit()
    return {"ok": True, "shop_id": shop_id, "shop_name": shop_name}

@app.get("/api/orders/hanoi")
async def get_hanoi_orders(request: Request, db: Session = Depends(get_db)):
    user_id = request.headers.get('X-User-ID', '')
    keywords = ["hà nội", "ha noi", " hn", "hanoi", "Hà Nội"]
    filters = [func.lower(Order.address).contains(kw.lower()) for kw in keywords]
    q = db.query(Order).filter(or_(*filters))
    if not is_full_access_user(user_id):
        q = q.filter(~Order.shop_id.in_(SENSITIVE_SHOPS)).filter(
            or_(Order.total == None, Order.total < SENSITIVE_TOTAL_THRESHOLD)
        )
    orders = q.order_by(Order.order_date.desc()).all()
    return sort_aggregated_orders(aggregate_orders(orders), "date_desc")


@app.get("/api/orders/nuochoa")
async def get_nuochoa_orders(request: Request, db: Session = Depends(get_db)):
    user_id = request.headers.get('X-User-ID', '')
    keywords = ["nước hoa", "nuoc hoa", "nươc hoa", "nước  hoa"]
    filters = [func.lower(Order.product).contains(kw.lower()) for kw in keywords]
    q = db.query(Order).filter(or_(*filters))
    if not is_full_access_user(user_id):
        q = q.filter(~Order.shop_id.in_(SENSITIVE_SHOPS)).filter(
            or_(Order.total == None, Order.total < SENSITIVE_TOTAL_THRESHOLD)
        )
    orders = q.order_by(Order.order_date.desc()).all()
    return sort_aggregated_orders(aggregate_orders(orders), "date_desc")

@app.get("/api/chart-data")
def get_chart_data(db: Session = Depends(get_db)):
    date_col = func.substr(Order.order_date, 1, 10)
    by_date = (
        db.query(date_col, func.count(Order.id))
        .filter(Order.order_date != None, Order.order_date != '')
        .group_by(date_col)
        .order_by(date_col)
        .all()
    )
    by_shop = (
        db.query(Order.shop_name, func.count(Order.id))
        .filter(Order.shop_name != None, Order.shop_name != '')
        .group_by(Order.shop_name)
        .order_by(func.count(Order.id).desc())
        .all()
    )
    return {
        "by_date": [{"date": d, "count": c} for d, c in by_date if d],
        "by_shop": [{"shop": s, "count": c} for s, c in by_shop if s],
    }


@app.get("/api/external-orders")
def get_external_orders(request: Request, scope: str = Query("phuong"), db: Session = Depends(get_db)):
    user_id = request.headers.get("X-User-ID", "").strip()
    scope = normalize_external_scope(scope)
    if scope == "hoang" and not can_view_hoang_orders(user_id):
        return JSONResponse({"error": "Bạn không có quyền xem thông tin tại đây."}, status_code=403)

    tracking_model, _ = get_external_models(scope)
    rows = db.query(tracking_model).order_by(desc(tracking_model.updated_at), tracking_model.order_code).all()
    config = get_external_order_config(db, scope)
    return {
        "scope": scope,
        "total": len(rows),
        "items": [serialize_external_order(row) for row in rows],
        "config": serialize_external_order_config(config),
    }


@app.post("/api/external-orders")
def save_external_orders(request: Request, body: dict, db: Session = Depends(get_db)):
    user_id = request.headers.get("X-User-ID", "")
    if not can_manage_external_orders(user_id):
        return JSONResponse({"error": "Không có quyền chỉnh cấu hình."}, status_code=403)
    scope = normalize_external_scope(body.get("scope"))

    items = body.get("items")
    if not isinstance(items, list):
        return JSONResponse({"error": "Dữ liệu không hợp lệ."}, status_code=422)

    normalized = []
    seen = set()
    valid_statuses = {"unknown", "in_transit", "delivered", "returned"}
    for item in items:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "")).strip().upper()
        if not code or code in seen:
            continue
        seen.add(code)
        try:
            cod = int(item.get("cod") or 0)
        except Exception:
            cod = 0
        status = str(item.get("status") or "unknown").strip()
        if status not in valid_statuses:
            status = "unknown"
        is_paid = 1 if item.get("is_paid") else 0
        normalized.append({"code": code, "cod": cod, "status": status, "is_paid": is_paid})

    fee_items_in = body.get("fee_items")
    fee_items = []
    if isinstance(fee_items_in, list):
        for item in fee_items_in:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content", "") or "").strip()
            try:
                amount = int(item.get("amount") or 0)
            except Exception:
                amount = 0
            collected = bool(item.get("collected"))
            if not content and amount == 0:
                continue
            fee_items.append({
                "content": content,
                "amount": amount,
                "collected": collected,
            })

    payment_history_in = body.get("payment_history")
    payment_history = []
    if isinstance(payment_history_in, list):
        for item in payment_history_in:
            if not isinstance(item, dict):
                continue
            paid_date = str(item.get("date", "") or "").strip()
            try:
                amount = int(item.get("amount") or 0)
            except Exception:
                amount = 0
            if not paid_date and amount == 0:
                continue
            payment_history.append({
                "date": paid_date,
                "amount": amount,
            })

    tracking_model, _ = get_external_models(scope)
    db.query(tracking_model).delete()
    for item in normalized:
        db.add(tracking_model(
            order_code=item["code"],
            cod_amount=item["cod"],
            status=item["status"],
            is_paid=item["is_paid"],
            updated_at=datetime.now(),
        ))
    config = get_external_order_config(db, scope)
    config.fee_items_json = _json.dumps({
        "fee_items": fee_items,
        "payment_history": payment_history,
    }, ensure_ascii=False)
    config.updated_at = datetime.now()
    db.commit()

    rows = db.query(tracking_model).order_by(desc(tracking_model.updated_at), tracking_model.order_code).all()
    return {
        "ok": True,
        "scope": scope,
        "total": len(rows),
        "items": [serialize_external_order(row) for row in rows],
        "config": serialize_external_order_config(config),
    }


@app.post("/api/external-orders/status")
def update_external_order_status(request: Request, body: dict, db: Session = Depends(get_db)):
    user_id = request.headers.get("X-User-ID", "").strip()
    if not can_manage_external_orders(user_id):
        return JSONResponse({"error": "Không có quyền cập nhật trạng thái."}, status_code=403)
    scope = normalize_external_scope(body.get("scope"))

    code = str(body.get("code", "")).strip().upper()
    status = str(body.get("status", "unknown")).strip()
    valid_statuses = {"unknown", "in_transit", "delivered", "returned"}
    if not code or status not in valid_statuses:
        return JSONResponse({"error": "Dữ liệu không hợp lệ."}, status_code=422)

    tracking_model, _ = get_external_models(scope)
    row = db.query(tracking_model).filter(tracking_model.order_code == code).first()
    if not row:
        return JSONResponse({"error": "Không tìm thấy đơn ngoại sàn."}, status_code=404)

    row.status = status
    row.updated_at = datetime.now()
    db.commit()

    return {
        "ok": True,
        "item": serialize_external_order(row),
    }


@app.post("/api/external-orders/payment")
def update_external_order_payment(request: Request, body: dict, db: Session = Depends(get_db)):
    user_id = request.headers.get("X-User-ID", "").strip()
    if not can_manage_external_orders(user_id):
        return JSONResponse({"error": "Không có quyền cập nhật thanh toán."}, status_code=403)
    scope = normalize_external_scope(body.get("scope"))

    code = str(body.get("code", "")).strip().upper()
    if not code:
        return JSONResponse({"error": "Dữ liệu không hợp lệ."}, status_code=422)

    tracking_model, _ = get_external_models(scope)
    row = db.query(tracking_model).filter(tracking_model.order_code == code).first()
    if not row:
        return JSONResponse({"error": "Không tìm thấy đơn ngoại sàn."}, status_code=404)

    row.is_paid = 1 if body.get("is_paid") else 0
    row.updated_at = datetime.now()
    db.commit()

    return {
        "ok": True,
        "item": serialize_external_order(row),
    }


@app.get("/api/taken-orders")
def get_taken_orders(request: Request, db: Session = Depends(get_db)):
    user_id = request.headers.get("X-User-ID", "").strip()
    if not user_id:
        return JSONResponse({"error": "Chưa có ID truy cập."}, status_code=403)

    include_details = is_taken_orders_admin(user_id)
    rows = db.query(TakenOrder).order_by(desc(TakenOrder.updated_at), desc(TakenOrder.id)).all()
    return {
        "ok": True,
        "manage": include_details,
        "items": [serialize_taken_order(row, include_details=include_details) for row in rows],
    }


@app.post("/api/taken-orders/take")
async def take_order(request: Request, body: dict, db: Session = Depends(get_db)):
    user_id = request.headers.get("X-User-ID", "").strip()
    if not is_taken_orders_admin(user_id):
        return JSONResponse({"error": "Không có quyền lấy đơn."}, status_code=403)

    raw_order_code = str(body.get("order_code", "")).strip()
    lookup_order_code = normalize_lookup_order_code(raw_order_code)
    if not lookup_order_code:
        return JSONResponse({"error": "Thiếu mã đơn hàng."}, status_code=422)

    try:
        order_info = await fetch_order_info_data(lookup_order_code, TAKEN_ORDER_ADMIN_KEY, user_id, db)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    except Exception as e:
        return JSONResponse({"error": f"Lỗi khi lấy thông tin đơn: {e}"}, status_code=500)

    quantity_value = order_info["raw"].get("quantity")
    if quantity_value in (None, "", "—"):
        db_order = order_info.get("db_order")
        quantity_value = db_order.quantity if db_order else 0
    try:
        quantity_value = int(quantity_value or 0)
    except Exception:
        quantity_value = 0

    existing = (
        db.query(TakenOrder)
        .filter(
            or_(
                TakenOrder.order_code == raw_order_code,
                TakenOrder.lookup_order_code == lookup_order_code,
            )
        )
        .first()
    )

    if not existing:
        existing = TakenOrder(
            order_code=raw_order_code or lookup_order_code,
            lookup_order_code=lookup_order_code,
            take_status="waiting_waybill",
            taken_by=user_id,
            taken_at=datetime.now(),
        )
        db.add(existing)

    existing.order_code = raw_order_code or lookup_order_code
    existing.lookup_order_code = lookup_order_code
    existing.shop_name = order_info.get("shop_name") or "—"
    existing.order_date = order_info.get("order_date") or "—"
    existing.customer_name = order_info.get("customer_name") or "—"
    existing.phone = order_info.get("phone") or "—"
    existing.address = order_info.get("address") or "—"
    existing.product = order_info.get("product") or "—"
    existing.quantity = quantity_value
    existing.prepaid_amount = order_info.get("prepaid_amount_text") or "—"
    existing.payment_status = order_info.get("payment_status") or "—"
    existing.taken_by = user_id
    existing.updated_at = datetime.now()

    db.commit()
    db.refresh(existing)

    return {
        "ok": True,
        "item": serialize_taken_order(existing, include_details=True),
    }


@app.post("/api/taken-orders/status")
def update_taken_order_status(request: Request, body: dict, db: Session = Depends(get_db)):
    user_id = request.headers.get("X-User-ID", "").strip()
    if not is_taken_orders_admin(user_id):
        return JSONResponse({"error": "Không có quyền cập nhật trạng thái."}, status_code=403)

    order_id = body.get("id")
    status = str(body.get("status", "")).strip()
    if not order_id or status not in TAKEN_ORDER_STATUS_LABELS:
        return JSONResponse({"error": "Dữ liệu không hợp lệ."}, status_code=422)

    row = db.query(TakenOrder).filter(TakenOrder.id == order_id).first()
    if not row:
        return JSONResponse({"error": "Không tìm thấy đơn đã lấy."}, status_code=404)

    row.take_status = status
    row.updated_at = datetime.now()
    db.commit()
    db.refresh(row)

    return {"ok": True, "item": serialize_taken_order(row, include_details=True)}


@app.post("/api/taken-orders/delete")
def delete_taken_order(request: Request, body: dict, db: Session = Depends(get_db)):
    user_id = request.headers.get("X-User-ID", "").strip()
    if not can_delete_taken_orders(user_id):
        return JSONResponse({"error": "Không có quyền xoá đơn."}, status_code=403)

    order_id = body.get("id")
    if not order_id:
        return JSONResponse({"error": "Thiếu ID đơn."}, status_code=422)

    row = db.query(TakenOrder).filter(TakenOrder.id == order_id).first()
    if not row:
        return JSONResponse({"error": "Không tìm thấy đơn đã lấy."}, status_code=404)

    order_code = str(row.order_code or "").strip()
    lookup_order_code = str(row.lookup_order_code or normalize_lookup_order_code(order_code)).strip()

    taken_rows = (
        db.query(TakenOrder)
        .filter(
            or_(
                TakenOrder.id == order_id,
                TakenOrder.order_code == order_code,
                TakenOrder.lookup_order_code == lookup_order_code,
            )
        )
        .all()
    )
    deleted_taken_count = 0
    for taken_row in taken_rows:
        db.delete(taken_row)
        deleted_taken_count += 1

    deleted_order_count = 0
    if order_code:
        order_rows = db.query(Order).filter(Order.order_code == order_code).all()
        for order_row in order_rows:
            db.delete(order_row)
            deleted_order_count += 1

    db.commit()
    return {
        "ok": True,
        "id": order_id,
        "order_code": order_code,
        "lookup_order_code": lookup_order_code,
        "deleted_order_rows": deleted_order_count,
        "deleted_taken_rows": deleted_taken_count,
    }

@app.post("/api/order-info")
async def get_order_info(request: Request, body: dict, db: Session = Depends(get_db)):
    order_code = body.get("order_code", "").strip()
    key        = body.get("key", "").strip()
    user_id    = request.headers.get('X-User-ID', '')

    if not order_code or not key:
        return JSONResponse({"error": "Thiếu mã đơn hàng hoặc key."}, status_code=400)

    if key not in VALID_KEYS:
        return JSONResponse({"error": "Key không hợp lệ."}, status_code=403)

    if key not in UNLIMITED_KEYS and VALID_KEYS[key] >= KEY_LIMIT:
        return JSONResponse({"error": f"Key đã hết lượt sử dụng ({KEY_LIMIT}/{KEY_LIMIT})."}, status_code=403)

    if len(order_code) < 9:
        return JSONResponse({"error": "Mã đơn hàng không hợp lệ."}, status_code=400)

    VALID_KEYS[key] += 1
    remaining = -1 if key in UNLIMITED_KEYS else KEY_LIMIT - VALID_KEYS[key]

    from datetime import timezone as _tz
    now_vn = datetime.now(_tz(timedelta(hours=7))).strftime("%d/%m/%Y %H:%M")
    if key not in KEY_HISTORY:
        KEY_HISTORY[key] = []
    KEY_HISTORY[key].append({"order_code": order_code, "time": now_vn})

    input_id = order_code[2:9]
    
    url = f"https://ec.megaads.vn/service/inoutput/find-promotion-codes-api?inoutputId={input_id}"
    session = "eyJpdiI6ImIra2pmWitCVVRRTlp2K3pRUUZOZ1E9PSIsInZhbHVlIjoibXpYaFhkQmVZU1VMRFRKWWhEcXRCdnBFSWdycVNzNFlSVHpGWjVYT0hTVDFpdlErVWxDSWhEaVdcL3JyT2RvSjZIcDNkMVJSYTllZDJMMTlsR2ZIQ3BnPT0iLCJtYWMiOiI2MDc2MTFlNDg0MTg4M2IyNDBiNDAzMDE4ZWE0MTk0ZTFkNDdlNGU3MjQ0ZjA3ODFkYTlkYzZiMjcyOTEyMzNmIn0%3D"
    
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.get(url, headers={
                "Accept": "application/json, text/plain, */*",
                "platform": "ios",
                "Cookie": f"laravel_session={session}",
                "User-Agent": "chiakiApp/3.6.2"
            })
        data = res.json()

        d = data.get("result") or data.get("data") or {}
        if isinstance(d, list):
            d = d[0] if d else {}
        def g(*keys):
            for k in keys:
                v = d.get(k)
                if v: return str(v)
            return "—"

        phone = next((x for x in d.get("search", "").split() if x.isdigit() and len(x) >= 9), "—")

        payment_type   = d.get("payment_type", "")
        prepaid_amount = d.get("prepaid_amount")
        is_approved    = d.get("is_approved_prepaid", "0")
        prepaid_time   = d.get("prepaid_time")

        payment_status = build_payment_status_text(payment_type, is_approved, prepaid_time)
        db_order = db.query(Order).filter(
        Order.order_code.like(f"%_{order_code}")
            ).first()
        hide_order = should_hide_order(db_order, user_id)
        db_product   = db_order.product   if db_order else "—"
        shop_id_from_api = g("store_code", "creator_name")
        db_shop_name = (
            SHOP_NAME_MAP.get(shop_id_from_api)
            or (db_order.shop_name if db_order else None)
            or shop_id_from_api
        )
        db_total     = f"{int(db_order.total):,} đ".replace(",", ".") if db_order and db_order.total else "—"
        url_history_parsed = []
        try:
            meta_raw = d.get("meta_data", "{}")
            meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else (meta_raw or {})
            uh = meta.get("url_history", {})
            if isinstance(uh, dict):
                url_history_parsed = [v for _, v in sorted(uh.items(), key=lambda x: int(x[0]))]
            elif isinstance(uh, list):
                url_history_parsed = uh
        except Exception:
            meta = {}
            url_history_parsed = []
        source_from = meta.get("meta_tracking", {}).get("from", "") or g("from") or ""

        effective_shop_id = str((db_order.shop_id if db_order and db_order.shop_id else shop_id_from_api) or "").strip()
        if key == "PHONE-KEY-PHUONG2000" and effective_shop_id not in PHONE_KEY_PHUONG_ALLOWED_SHOPS:
            return JSONResponse({"error": "Không có thông tin đơn hàng."}, status_code=404)

        if hide_order:
            return JSONResponse({"error": "Không tìm thấy đơn hàng hoặc bạn không có quyền xem đơn này."}, status_code=404)

        return {
    "order_code":           g("code"),
    "sync_id":              (db_order.sync_id if db_order else None) or d.get("sync_id") or "—",
    "status":               g("status"),
    "shop_name":            db_shop_name,
    "order_date":           g("verified_time", "create_time"),
    "customer_name":        g("related_user_name", "receiver_name"),
    "customer_id":          meta.get("customer_id") or d.get("related_user_id"),
    "phone":                phone,
    "email":                g("email_id"),
    "address":              g("delivery_address"),
    "source":               g("source", "from"),
    "source_from":          source_from,
    "payment":              payment_status,
    "prepaid_amount":       db_total,
    "shipping_code":        g("shipping_code"),
    "delivery_status":      g("delivery_status"),
    "delivery_location_id": g("delivery_location_id"),
    "district_delivery_id": g("district_delivery_id"),
    "commune_delivery_id":  g("commune_delivery_id"),
    "shipper_receive_time": g("shipper_receive_time"),
    "product":              db_product,
    "quantity":             d.get("quantity") or (db_order.quantity if db_order else None),
    "url_history":          url_history_parsed,
    "remaining":            remaining,
}
    except Exception as e:
        VALID_KEYS[key] -= 1
        return JSONResponse({"error": f"Lỗi khi gọi API: {str(e)}"}, status_code=500)

@app.post("/api/order-info/check-key")
async def check_key(body: dict):
    key = body.get("key", "").strip()
    if key not in VALID_KEYS:
        return JSONResponse({"error": "Key không hợp lệ."}, status_code=403)
    used = VALID_KEYS[key]
    is_unlimited = key in UNLIMITED_KEYS
    return {
        "used": used,
        "remaining": -1 if is_unlimited else KEY_LIMIT - used,
        "limit": -1 if is_unlimited else KEY_LIMIT,
        "unlimited": is_unlimited
}
@app.get("/api/order-info/history")
async def get_key_history(request: Request):
    user_id = request.headers.get('X-User-ID', '')
    if not is_taken_orders_admin(user_id):
        return JSONResponse({"error": "Không có quyền."}, status_code=403)

    result = []
    for key, logs in KEY_HISTORY.items():
        if logs:
            result.append({
                "key":     key,
                "used":    VALID_KEYS.get(key, 0),
                "limit":   KEY_LIMIT,
                "history": logs
            })
    return {"data": result, "total_queries": sum(len(v) for v in KEY_HISTORY.values())}
@app.post("/api/auth/login-log")
async def login_log(body: dict, request: Request):
    key    = body.get("key", "").strip()
    event  = body.get("event", "login")  # "login" hoặc "logout"
    
    from datetime import timezone as _tz
    now_vn = datetime.now(_tz(timedelta(hours=7))).strftime("%H:%M:%S ngày %d/%m/%Y")
    
    LOGIN_HISTORY.append({
        "key":   key,
        "event": event,
        "time":  now_vn,
    })
    return {"ok": True}

@app.get("/api/auth/login-history")
async def get_login_history(request: Request):
    user_id = request.headers.get('X-User-ID', '')
    if not is_taken_orders_admin(user_id):
        return JSONResponse({"error": "Không có quyền."}, status_code=403)
    return {"data": LOGIN_HISTORY, "total": len(LOGIN_HISTORY)}


@app.get("/api/auth/capabilities")
def get_auth_capabilities(request: Request):
    user_id = request.headers.get("X-User-ID", "").strip()
    return {
        "ok": True,
        "capabilities": get_user_capabilities(user_id),
    }
@app.post("/api/auth/verify-id")
async def verify_id(body: dict):
    user_id = body.get("id", "").strip()
    info = LOGIN_ID_META.get(user_id)
    if not info:
        return JSONResponse({"error": "ID không hợp lệ."}, status_code=403)
    
    import time
    first_entry = body.get("firstEntry")  # ms từ frontend
    if not first_entry:
        first_entry = int(time.time() * 1000)
    
    exp_ms = first_entry + info["hours"] * 3600000
    if int(time.time() * 1000) > exp_ms:
        return JSONResponse({"error": "ID đã hết hạn."}, status_code=403)
    
    return {
        "ok": True,
        "label": info["label"],
        "expMs": exp_ms,
        "firstEntry": first_entry,
        "capabilities": get_user_capabilities(user_id),
    }
@app.get("/api/orders/mien-bac")
async def get_mien_bac_orders(request: Request, db: Session = Depends(get_db)):
    user_id = request.headers.get("X-User-ID", "")
    keywords = [
        "hải phòng", "hai phong",
        "bắc giang", "bac giang", "bắc kạn", "bac kan",
        "cao bằng", "cao bang", "hà giang", "ha giang",
        "lạng sơn", "lang son", "phú thọ", "phu tho",
        "quảng ninh", "quang ninh", "thái nguyên", "thai nguyen",
        "tuyên quang", "tuyen quang", "điện biên", "dien bien",
        "hòa bình", "hoa binh", "lai châu", "lai chau",
        "lào cai", "lao cai", "sơn la", "son la",
        "yên bái", "yen bai", "bắc ninh", "bac ninh",
        "hà nam", "ha nam", "hải dương", "hai duong",
        "hưng yên", "hung yen", "nam định", "nam dinh",
        "ninh bình", "ninh binh", "thái bình", "thai binh",
        "vĩnh phúc", "vinh phuc"
    ]
    filters = [func.lower(Order.address).contains(kw.lower()) for kw in keywords]
    q = db.query(Order).filter(or_(*filters))
    if not is_full_access_user(user_id):
        q = q.filter(~Order.shop_id.in_(SENSITIVE_SHOPS)).filter(
            or_(Order.total == None, Order.total < SENSITIVE_TOTAL_THRESHOLD)
        )
    orders = q.order_by(Order.order_date.desc()).all()
    return sort_aggregated_orders(aggregate_orders(orders), "date_desc")

@app.get("/api/shops-list")
def get_shops_list():
    shops = get_shops_map()
    result = []
    for shop_id, (shop_url, shop_name) in shops.items():
        result.append({
            "shop_id": shop_id,
            "shop_name": shop_name,
            "shop_url": shop_url
        })
    return sorted(result, key=lambda x: x["shop_name"])

@app.get("/api/sync-download-links")
def get_sync_download_links(request: Request, status: str = Query("receive_wating")):
    user_id = request.headers.get("X-User-ID", "").strip()
    if not get_user_capabilities(user_id).get("admin_tools"):
        return JSONResponse({"error": "Không có quyền truy cập."}, status_code=403)

    shops = sorted(get_shops_map().items(), key=lambda item: item[1][1])
    return {
        "shops": [
            {
                "shop_id": shop_id,
                "shop_name": shop_name,
                "shop_url": shop_url,
                "download_url": build_shop_download_url(shop_id, status=status),
                "file_name": build_shop_download_filename(shop_id, shop_name),
            }
            for shop_id, (shop_url, shop_name) in shops
        ]
    }


@app.get("/api/sync-download-file")
async def download_sync_shop_file(request: Request, shop_id: str = Query(...), status: str = Query("receive_wating")):
    user_id = request.headers.get("X-User-ID", "").strip()
    if not get_user_capabilities(user_id).get("admin_tools"):
        return JSONResponse({"error": "Không có quyền truy cập."}, status_code=403)

    normalized_shop_id = str(shop_id or "").strip()
    shops = get_shops_map()
    shop_meta = shops.get(normalized_shop_id)
    if not shop_meta:
        return JSONResponse({"error": "Không tìm thấy gian hàng cần tải."}, status_code=404)

    _, shop_name = shop_meta
    download_url = build_shop_download_url(normalized_shop_id, status=status)
    headers = {
        "accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.ms-excel,application/octet-stream,*/*",
        "accept-language": "vi,en-US;q=0.9,en;q=0.8",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "referer": download_url,
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/135.0.0.0 Safari/537.36"
        ),
    }

    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            response = await client.get(download_url, headers=headers)
    except Exception as exc:
        return JSONResponse(
            {"error": f"Không tải được file từ Chiaki cho shop {normalized_shop_id}: {exc}"},
            status_code=502,
        )

    if response.status_code != 200:
        return JSONResponse(
            {"error": f"Chiaki trả về HTTP {response.status_code} cho shop {normalized_shop_id}."},
            status_code=502,
        )

    content_type = (response.headers.get("content-type") or "").lower()
    if "html" in content_type or "json" in content_type:
        preview = response.text[:300].strip()
        detail = f" Phản hồi: {preview}" if preview else ""
        return JSONResponse(
            {"error": f"Chiaki không trả file Excel cho shop {normalized_shop_id}.{detail}"},
            status_code=502,
        )

    content = response.content
    if not content:
        return JSONResponse(
            {"error": f"Chiaki trả về file rỗng cho shop {normalized_shop_id}."},
            status_code=502,
        )

    filename = build_shop_download_filename(normalized_shop_id, shop_name)
    fallback_filename = build_ascii_fallback_filename(filename)
    media_type = response.headers.get("content-type") or "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": (
                f'attachment; filename="{fallback_filename}"; '
                f"filename*=UTF-8''{quote(filename)}"
            ),
            "X-Download-Filename": filename,
        },
    )


@app.post("/api/tools/cancel-order-curl")
async def create_cancel_order_curl(request: Request, body: dict):
    user_id = request.headers.get("X-User-ID", "").strip()
    if not get_user_capabilities(user_id).get("admin_tools"):
        return JSONResponse({"error": "Không có quyền truy cập."}, status_code=403)

    order_code = str(body.get("order_code", "")).strip()
    if not order_code or len(order_code) < 9:
        return JSONResponse({"error": "Mã đơn không hợp lệ."}, status_code=422)

    order_id = order_code[2:9]
    curl_text = f"""ORDER_ID="{order_id}" && \\
SYNC_ID=$(curl -s "https://ec.megaads.vn/service/inoutput/find-promotion-codes-api?inoutputId=$ORDER_ID" \\
  -H 'Accept: application/json, text/plain, */*' \\
  -H 'platform: ios' \\
  -H 'Cookie: laravel_session=eyJpdiI6ImIra2pmWitCVVRRTlp2K3pRUUZOZ1E9PSIsInZhbHVlIjoibXpYaFhkQmVZU1VMRFRKWWhEcXRCdnBFSWdycVNzNFlSVHpGWjVYT0hTVDFpdlErVWxDSWhEaVdcL3JyT2RvSjZIcDNkMVJSYTllZDJMMTlsR2ZIQ3BnPT0iLCJtYWMiOiI2MDc2MTFlNDg0MTg4M2IyNDBiNDAzMDE4ZWE0MTk0ZTFkNDdlNGU3MjQ0ZjA3ODFkYTlkYzZiMjcyOTEyMzNmIn0%3D' \\
  -H 'User-Agent: chiakiApp/3.6.2' \\
  | python3 -c "import sys,json; print(json.load(sys.stdin)['result'][0]['sync_id'])") && \\
echo "sync_id: $SYNC_ID" && \\
curl -s -X POST "https://api.chiaki.vn/api/order/request-cancel" \\
  -H "Accept: application/json, text/plain, */*" \\
  -H "Accept-Encoding: gzip, deflate" \\
  -H "Content-Type: application/json" \\
  -H "token: YjYTQY7fP4vExhg0a8itBqBEFKzZlmcKWBoxkwc91XNyF3zpCi" \\
  -H "User-Agent: chiakiApp/3.6.2" \\
  --compressed \\
  -d "{{\\"sync_id\\":\\"$SYNC_ID\\",\\"order_id\\":\\"$ORDER_ID\\",\\"cancel_code\\":\\"change_item_order\\",\\"cancel_reason\\":\\"Thay đổi đơn hàng (màu sắc, kích thước, thêm mã giảm giá,...)\\\"}}" \\
  | python3 -m json.tool"""
    return {"ok": True, "order_code": order_code, "curl": curl_text}


@app.post("/api/tools/transfer-order-curl")
async def create_transfer_order_curl(request: Request, body: dict, db: Session = Depends(get_db)):
    user_id = request.headers.get("X-User-ID", "").strip()
    if not get_user_capabilities(user_id).get("admin_tools"):
        return JSONResponse({"error": "Không có quyền truy cập."}, status_code=403)

    old_order_code = str(body.get("old_order_code", "")).strip()
    new_product_code = str(body.get("new_product_code", "")).strip()
    new_product_id = str(body.get("new_product_id", "")).strip()
    quantity = str(body.get("quantity", "")).strip()
    target_shop_id = str(body.get("target_shop_id", "")).strip()
    key = str(body.get("key", "")).strip()

    if not all([old_order_code, new_product_code, new_product_id, quantity, target_shop_id]):
        return JSONResponse({"error": "Vui lòng điền đủ 5 trường."}, status_code=422)
    if not re.fullmatch(r"\d+", new_product_id or "") or not re.fullmatch(r"\d+", quantity or "") or not re.fullmatch(r"\d+", target_shop_id or ""):
        return JSONResponse({"error": "ID sản phẩm mới, số lượng và ID Shop nhận đơn phải là số."}, status_code=422)
    if not key:
        return JSONResponse({"error": "Chưa có key tra cứu."}, status_code=422)

    try:
        order_info = await fetch_order_info_data(old_order_code, key, user_id, db)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    except Exception as e:
        return JSONResponse({"error": f"Lỗi khi lấy thông tin đơn cũ: {e}"}, status_code=500)

    user_id_value = str(order_info.get("customer_id") or "").strip()
    delivery_location_id = str(order_info.get("delivery_location_id") or "").strip()
    district_delivery_id = str(order_info.get("district_delivery_id") or "").strip()
    commune_delivery_id = str(order_info.get("commune_delivery_id") or "").strip()
    order_time = str(order_info.get("order_date") or "").strip()
    address = "" if order_info.get("address") == "—" else str(order_info.get("address") or "").strip()
    full_name = "" if order_info.get("customer_name") == "—" else str(order_info.get("customer_name") or "").strip()
    email = "" if order_info.get("email") == "—" else str(order_info.get("email") or "").strip()
    phone = "" if order_info.get("phone") == "—" else str(order_info.get("phone") or "").strip()

    if not user_id_value or not delivery_location_id or not district_delivery_id or not commune_delivery_id:
        return JSONResponse({"error": "Thiếu dữ liệu giao hàng hoặc customer_id từ đơn hàng cũ."}, status_code=422)

    encoded_time = quote(order_time, safe="")
    add_to_cart_payload = _json.dumps({
        "product_id": int(new_product_id),
        "product_code": new_product_code,
        "quantity": int(quantity),
        "is_buy_now": 0,
        "store_id": int(target_shop_id),
        "user_id": int(user_id_value)
    }, ensure_ascii=False)
    checkout_payload = _json.dumps({
        "user_id": int(user_id_value),
        "delivery_location_id": int(delivery_location_id),
        "district_delivery_id": int(district_delivery_id),
        "commune_delivery_id": int(commune_delivery_id),
        "delivery_address": address,
        "earn": 0,
        "full_name": full_name,
        "email": email,
        "phone": phone,
        "phone2": "",
        "payment_type": "home",
        "prepaid_code": "",
        "related_user_id": int(user_id_value),
        "related_user_name": "Nguyên",
        "require_call_back": 0,
        "campaign_id": "",
        "order_cookie": "",
        "from": "",
        "affiliate_id": "",
        "custom_id": "",
        "point": 0,
        "ads_id": "",
        "ip": "192.168.1.241",
        "platform": "ios",
        "from_flow": "",
        "email_id": "",
        "tracking": "",
        "affiliate_type": "",
        "affiliate_url": "",
        "banner_id": "",
        "flash_sale": "",
        "product_sync": "",
        "delivery_time": "",
        "url_history": _json.dumps([{
            "url": f"https://chiaki.vn/add_to_cart?is_buy_now=0&product_code={quote(new_product_code, safe='')}&product_id={new_product_id}&quantity={quantity}&store_id={target_shop_id}&time={encoded_time}&user_id={user_id_value}&version=3.6.2",
            "time": order_time
        }], ensure_ascii=False),
        "current_warehouse_product": "empty",
        "delivery_note": "other",
        "description": "",
        "promotion_code_used": {},
        "delivery_shipping_mode": "normal",
        "source": "chiaki-app",
        "device": "ios",
        "clicker_data": {"source": None, "sources": "", "trackingData": None},
        "browser": " ",
        "user_device": "Mobile",
        "payment_value": _json.dumps({"bank_code": "home"}, ensure_ascii=False),
        "is_hide_product_name": 0
    }, ensure_ascii=False)

    curl_text = f"""curl 'https://api.chiaki.vn/api/v2/cart_item/push' \\
-X POST \\
-H 'Host: api.chiaki.vn' \\
-H 'Accept: application/json, text/plain, */*' \\
-H 'baggage: sentry-environment=production,sentry-public_key=418f1affd8b5477baa885b6b4da50b79,sentry-trace_id=7f3084679f1944b2ae6208a09319a66b' \\
-H 'Accept-Language: en-GB,en-US;q=0.9,en;q=0.8' \\
-H 'Cache-Control: no-cache' \\
-H 'platform: ios' \\
-H 'imei: A4184509-4A7A-423B-A0EE-044668760C71' \\
-H 'User-Agent: chiakiApp/3.6.2' \\
-H 'sentry-trace: 7f3084679f1944b2ae6208a09319a66b-a1c86e53db19d505' \\
-H 'Connection: keep-alive' \\
-H 'Content-Type: application/json' \\
--data-raw "{escape_for_shell_double_quotes(add_to_cart_payload)}" \\
| python3 -m json.tool

sleep 10

curl 'https://api.chiaki.vn/api/checkout' \\
-X POST \\
-H 'platform: ios' \\
-H 'imei: A4184509-4A7A-423B-A0EE-044668760C71' \\
-H 'User-Agent: chiakiApp/3.6.2' \\
-H 'Accept: application/json, text/plain, */*' \\
-H 'Content-Type: application/json' \\
--data-raw "{escape_for_shell_double_quotes(checkout_payload)}" \\
| python3 -m json.tool"""
    return {
        "ok": True,
        "summary": f"Đơn cũ: {old_order_code} · User ID: {user_id_value} · SP mới: {new_product_code} · Shop nhận: {target_shop_id}",
        "curl": curl_text,
    }

@app.post("/api/sync")
async def sync_now(body: dict, db: Session = Depends(get_db)):
    shop_id     = body.get("shop_id", "").strip()
    cf_chl_tk   = body.get("cf_chl_tk", "").strip()
    cf_clearance = body.get("cf_clearance", "").strip()

    if not shop_id:
        return JSONResponse({"error": "Thiếu shop_id"}, status_code=400)
    if not cf_chl_tk or not cf_clearance:
        return JSONResponse({"error": "Thiếu cf_chl_tk hoặc cf_clearance"}, status_code=400)

    shops = get_shops_map()
    if shop_id not in shops:
        return JSONResponse({"error": f"Không tìm thấy shop {shop_id}"}, status_code=404)

    shop_url, shop_name = shops[shop_id]
    old_codes = {
        str(code).strip()
        for (code,) in db.query(Order.order_code).filter(Order.shop_id == shop_id).all()
        if code
    }

    try:
        synced = await sync_shop(shop_id, shop_url, shop_name, db,
                                 cf_chl_tk=cf_chl_tk, cf_clearance=cf_clearance)
        new_codes = {
            str(code).strip()
            for (code,) in db.query(Order.order_code).filter(Order.shop_id == shop_id).all()
            if code
        }
        delta = build_sync_delta(old_codes, new_codes)
        return {
            "ok": True,
            "shop_id": shop_id,
            "shop_name": shop_name,
            "synced": synced,
            **delta,
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.post("/api/sync-json-request")
async def open_sync_json_request(
    request: Request,
    shop_id: str = Form(...),
    access_token: str = Form(...),
    user_id: str = Form(""),
):
    effective_user_id = request.headers.get("X-User-ID", "").strip() or str(user_id or "").strip()
    if not get_user_capabilities(effective_user_id).get("admin_tools"):
        return JSONResponse({"error": "Không có quyền truy cập."}, status_code=403)

    normalized_shop_id = str(shop_id or "").strip()
    token = normalize_seller_access_token(access_token)
    if not normalized_shop_id:
        return JSONResponse({"error": "Thiếu shop_id."}, status_code=422)
    if not token:
        return JSONResponse({"error": "Thiếu accesstoken."}, status_code=422)
    if normalized_shop_id not in get_shops_map():
        return JSONResponse({"error": f"Không tìm thấy shop {normalized_shop_id}."}, status_code=404)

    url, params, headers = build_seller_get_order_request(normalized_shop_id, token)
    try:
        async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
            response = await client.get(url, params=params, headers=headers)
    except Exception as exc:
        return JSONResponse({"error": f"Không gọi được API Chiaki: {exc}"}, status_code=502)

    content_type = response.headers.get("content-type") or "application/json"
    try:
        parsed = response.json()
        body = _json.dumps(parsed, ensure_ascii=False, indent=2)
        content_type = "application/json"
    except Exception:
        body = response.text

    return Response(
        content=body,
        media_type=content_type,
        status_code=response.status_code,
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/sync-json-orders")
async def sync_json_orders(request: Request, body: dict, db: Session = Depends(get_db)):
    user_id = request.headers.get("X-User-ID", "").strip()
    if not get_user_capabilities(user_id).get("admin_tools"):
        return JSONResponse({"error": "Không có quyền truy cập."}, status_code=403)

    shop_id = str(body.get("shop_id", "") or "").strip()
    json_text = str(body.get("json_text", "") or "").strip()
    if not shop_id:
        return JSONResponse({"error": "Vui lòng chọn gian hàng."}, status_code=422)
    if not json_text:
        return JSONResponse({"error": "Vui lòng paste JSON trả về từ Chiaki."}, status_code=422)

    shops = get_shops_map()
    if shop_id not in shops:
        return JSONResponse({"error": f"Không tìm thấy shop {shop_id}."}, status_code=404)

    try:
        payload = _json.loads(json_text)
    except Exception as exc:
        return JSONResponse({"error": f"JSON không hợp lệ: {exc}"}, status_code=422)

    shop_url, shop_name = shops[shop_id]
    existing_shop_orders = db.query(Order).filter(Order.shop_id == shop_id).all()
    old_codes = {
        str(getattr(order, "order_code", "")).strip()
        for order in existing_shop_orders
        if getattr(order, "order_code", None)
    }

    orders = parse_orders_json_payload(payload, shop_id, shop_name)
    if not orders:
        return JSONResponse({"error": "Không tìm thấy danh sách đơn hàng trong JSON."}, status_code=422)

    try:
        deleted = 0
        for existing_order in existing_shop_orders:
            db.delete(existing_order)
            deleted += 1
        for order in orders:
            db.add(Order(**order))

        new_codes = {
            str(item.get("order_code", "")).strip()
            for item in orders
            if item.get("order_code")
        }
        meta = db.query(ShopMeta).filter(ShopMeta.shop_id == shop_id).first()
        if meta:
            meta.shop_name = shop_name
            meta.last_sync = datetime.now()
            meta.order_count = len(new_codes)
        else:
            db.add(ShopMeta(
                shop_id=shop_id,
                shop_name=shop_name,
                shop_url=shop_url,
                last_sync=datetime.now(),
                order_count=len(new_codes),
            ))
        db.commit()
        delta = build_sync_delta(old_codes, new_codes)
        return {
            "ok": True,
            "shop_id": shop_id,
            "shop_name": shop_name,
            "synced": len(orders),
            "unique_orders": len(new_codes),
            "deleted": deleted,
            **delta,
        }
    except Exception as exc:
        db.rollback()
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/sync-upload")
async def sync_upload(
    shop_id: str = Form(...),
    sync_stage: str = Form("waiting"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    try:
        shops = get_shops_map()
        if shop_id not in shops:
            return JSONResponse({"error": f"Không tìm thấy shop {shop_id}"}, status_code=404)

        shop_url, shop_name = shops[shop_id]
        content = await file.read()
        existing_shop_orders = db.query(Order).filter(Order.shop_id == shop_id).all()
        old_codes = {
            str(getattr(order, "order_code", "")).strip()
            for order in existing_shop_orders
            if getattr(order, "order_code", None)
        }

        # Kiểm tra có phải Excel không (magic bytes PK = xlsx)
        if len(content) < 100:
            return JSONResponse({"error": "File quá nhỏ, không hợp lệ"}, status_code=422)

        orders = parse_excel(content, shop_id, shop_name)
        if orders is None:
            return JSONResponse({"error": "Không đọc được dữ liệu từ file Excel. Kiểm tra lại cột header."}, status_code=422)

        deleted = 0
        for existing_order in existing_shop_orders:
            db.delete(existing_order)
            deleted += 1
        for o in orders:
            db.add(Order(**o))
        db.commit()
        new_codes = {
            str(item.get("order_code", "")).strip()
            for item in orders
            if item.get("order_code")
        }

        meta = db.query(ShopMeta).filter(ShopMeta.shop_id == shop_id).first()
        if meta:
            meta.shop_name   = shop_name
            meta.last_sync   = datetime.now()
            meta.order_count = len(new_codes)
        else:
            db.add(ShopMeta(
                shop_id=shop_id, shop_name=shop_name,
                shop_url=shop_url, last_sync=datetime.now(),
                order_count=len(new_codes)
            ))
        db.commit()
        delta = build_sync_delta(old_codes, new_codes)

        return {
            "ok": True,
            "shop_id": shop_id,
            "shop_name": shop_name,
            "synced": len(orders),
            "deleted": deleted,
            **delta
        }

    except Exception as e:
        db.rollback()
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)
