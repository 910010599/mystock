from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pandas as pd
from mootdx.quotes import Quotes


ROOT = Path(__file__).resolve().parent
HTML_FILE = ROOT / "baoding_return_bar_chart.html"
DATA_DIR = ROOT / "data"
STORE_PATH = DATA_DIR / "watchlist.json"
UNIVERSE_PATH = DATA_DIR / "stock_universe.json"
STOCK_DATA_DIR = DATA_DIR / "stocks"
INDEX_DATA_PATH = DATA_DIR / "indices.json"
LEGACY_BACKUP_PATH = DATA_DIR / "watchlist_legacy_backup.json"

DEFAULT_STOCK = {"code": "002552", "name": "宝鼎科技", "group": ""}
SERVER_CANDIDATES = [
    ("110.41.147.114", 7709),
    ("8.129.13.54", 7709),
    ("124.70.176.52", 7709),
    ("47.100.236.28", 7709),
    ("121.36.54.217", 7709),
    ("124.71.85.110", 7709),
]


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_code(code: str) -> str:
    clean = re.sub(r"\D", "", str(code or ""))
    if not re.fullmatch(r"\d{6}", clean):
        raise ValueError("股票代码必须是 6 位数字")
    return clean


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, separators=(",", ":"))
    temp_path.replace(path)


def stock_detail_path(code: str) -> Path:
    return STOCK_DATA_DIR / f"{normalize_code(code)}.json"


def stock_summary(stock: dict, detail: dict | None = None) -> dict:
    source = detail or stock
    rows = source.get("rows") or []
    meta = source.get("meta") or {}
    kline = source.get("kline") or []
    last_return_pct = stock.get("last_return_pct")
    last_return_date = stock.get("last_return_date")

    valid_kline = []
    for item in kline:
        try:
            close = float(item.get("close"))
        except (TypeError, ValueError):
            continue
        if item.get("date") and close:
            valid_kline.append({**item, "close": close})

    if len(valid_kline) >= 2:
        previous = valid_kline[-2]
        latest = valid_kline[-1]
        last_return_pct = round((float(latest["close"]) / float(previous["close"]) - 1) * 100, 2)
        last_return_date = latest["date"]

    row_count = len(rows) if rows else int(stock.get("row_count") or 0)
    end_trade_date = meta.get("end_trade_date") or stock.get("end_trade_date")
    end_close = meta.get("end_close", meta.get("end_close_qfq", stock.get("end_close")))

    return {
        "code": stock.get("code"),
        "name": stock.get("name") or stock.get("code"),
        "group": stock.get("group") or "",
        "last_updated": stock.get("last_updated") or source.get("last_updated"),
        "has_data": bool(rows) or bool(stock.get("has_data")),
        "row_count": row_count,
        "end_trade_date": end_trade_date,
        "end_close": end_close,
        "last_return_pct": last_return_pct,
        "last_return_date": last_return_date,
    }


def stock_detail_from_summary(summary: dict) -> dict:
    return {
        "code": summary.get("code"),
        "name": summary.get("name") or summary.get("code"),
        "group": summary.get("group") or "",
        "last_updated": summary.get("last_updated"),
        "meta": None,
        "rows": [],
        "kline": [],
    }


def load_stock_detail(code: str, summary: dict | None = None) -> dict | None:
    path = stock_detail_path(code)
    if not path.exists():
        return stock_detail_from_summary(summary or {"code": code})

    with path.open("r", encoding="utf-8") as file:
        detail = json.load(file)

    if summary:
        detail["code"] = summary.get("code")
        detail["name"] = summary.get("name") or detail.get("name") or summary.get("code")
        detail["group"] = summary.get("group") or ""
        detail["last_updated"] = summary.get("last_updated") or detail.get("last_updated")
    detail.setdefault("meta", None)
    detail.setdefault("rows", [])
    detail.setdefault("kline", [])
    return detail


def save_stock_detail(detail: dict) -> None:
    code = normalize_code(detail.get("code"))
    payload = {
        "code": code,
        "name": detail.get("name") or code,
        "group": detail.get("group") or "",
        "last_updated": detail.get("last_updated"),
        "meta": detail.get("meta"),
        "rows": detail.get("rows") or [],
        "kline": detail.get("kline") or [],
    }
    write_json_atomic(stock_detail_path(code), payload)


def load_indices() -> dict:
    if not INDEX_DATA_PATH.exists():
        return {}

    with INDEX_DATA_PATH.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    return payload.get("indices", payload) or {}


def save_indices(indices: dict) -> None:
    write_json_atomic(INDEX_DATA_PATH, {"last_updated": now_text(), "indices": indices or {}})


def latest_index_date(indices: dict) -> str | None:
    dates = []
    for item in (indices or {}).values():
        kline = item.get("kline") or []
        if kline and kline[-1].get("date"):
            dates.append(kline[-1]["date"])
    return max(dates) if dates else None


def store_needs_migration(store: dict) -> bool:
    if store.get("version") != 2:
        return True
    return any(
        any(key in stock for key in ("rows", "kline", "indices", "meta"))
        for stock in store.get("stocks", [])
    )


def migrate_legacy_store(store: dict) -> dict:
    if STORE_PATH.exists() and not LEGACY_BACKUP_PATH.exists():
        shutil.copy2(STORE_PATH, LEGACY_BACKUP_PATH)

    summaries = []
    migrated_indices = load_indices()
    for stock in store.get("stocks", []):
        code = normalize_code(stock.get("code"))
        stock["code"] = code

        if stock.get("indices") and not migrated_indices:
            migrated_indices = stock.get("indices") or {}

        detail = {
            "code": code,
            "name": stock.get("name") or code,
            "group": stock.get("group") or "",
            "last_updated": stock.get("last_updated"),
            "meta": stock.get("meta"),
            "rows": stock.get("rows") or [],
            "kline": stock.get("kline") or [],
        }
        if detail["rows"] or detail["kline"] or detail["meta"]:
            save_stock_detail(detail)
        summaries.append(stock_summary(stock, detail))

    if migrated_indices:
        save_indices(migrated_indices)

    return {"version": 2, "stocks": summaries}


def load_store() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    STOCK_DATA_DIR.mkdir(exist_ok=True)

    if not STORE_PATH.exists():
        store = {"version": 2, "stocks": [stock_summary({**DEFAULT_STOCK, "last_updated": None})]}
        save_store(store)
        return store

    with STORE_PATH.open("r", encoding="utf-8") as file:
        store = json.load(file)

    store.setdefault("stocks", [])
    if store_needs_migration(store):
        store = migrate_legacy_store(store)
        save_store(store)
        return store

    changed = False
    summaries = []
    for stock in store["stocks"]:
        if "group" not in stock:
            stock["group"] = ""
            changed = True
        summary = stock_summary(stock)
        summaries.append(summary)
        if summary != stock:
            changed = True
    store["stocks"] = summaries

    if not any(stock.get("code") == DEFAULT_STOCK["code"] for stock in store["stocks"]):
        store["stocks"].insert(0, stock_summary({**DEFAULT_STOCK, "last_updated": None}))
        changed = True

    if changed:
        save_store(store)

    return store


def save_store(store: dict) -> None:
    payload = {
        "version": 2,
        "stocks": [stock_summary(stock) for stock in store.get("stocks", [])],
    }
    write_json_atomic(STORE_PATH, payload)


def get_stock(store: dict, code: str) -> dict | None:
    return next((stock for stock in store["stocks"] if stock.get("code") == code), None)


def stock_response(summary: dict) -> dict:
    detail = load_stock_detail(summary.get("code"), summary)
    detail.update(summary)
    detail["indices"] = load_indices()
    return detail


def connect_quotes_client():
    last_error: Exception | None = None

    for server in SERVER_CANDIDATES:
        try:
            client = Quotes.factory(
                market="std",
                server=server,
                timeout=5,
                heartbeat=False,
                raise_exception=True,
            )
            return client, server
        except Exception as exc:  # noqa: BLE001 - surface the last connection error.
            last_error = exc

    detail = f": {last_error}" if last_error else ""
    raise RuntimeError(f"无法连接通达信行情服务器{detail}")


def connect_client(symbol: str):
    last_error: Exception | None = None

    for server in SERVER_CANDIDATES:
        try:
            client = Quotes.factory(
                market="std",
                server=server,
                timeout=5,
                heartbeat=False,
                raise_exception=True,
            )
            bars = client.bars(symbol=symbol, frequency=9, offset=800)
            if bars is not None and not bars.empty:
                return client, server, bars
        except Exception as exc:  # noqa: BLE001 - surface the last connection error.
            last_error = exc

    detail = f": {last_error}" if last_error else ""
    raise RuntimeError(f"无法连接通达信行情服务器{detail}")


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", "", clean_stock_name(name)).lower()


def clean_stock_name(name: str) -> str:
    return str(name or "").replace("\x00", "").strip()


def preferred_market_for_code(code: str) -> int:
    return 1 if code.startswith(("5", "6", "9")) else 0


def is_common_a_share_code(code: str) -> bool:
    return code.startswith(
        (
            "000",
            "001",
            "002",
            "003",
            "300",
            "301",
            "600",
            "601",
            "603",
            "605",
            "688",
            "689",
        )
    )


def is_subsequence(needle: str, haystack: str) -> bool:
    if not needle:
        return True

    index = 0
    for char in haystack:
        if char == needle[index]:
            index += 1
            if index == len(needle):
                return True
    return False


def load_stock_universe(refresh: bool = False) -> list[dict]:
    DATA_DIR.mkdir(exist_ok=True)

    if not refresh and UNIVERSE_PATH.exists():
        with UNIVERSE_PATH.open("r", encoding="utf-8") as file:
            return json.load(file)

    client, _server = connect_quotes_client()
    stocks: dict[str, dict] = {}

    for market in (0, 1):
        frame = client.stocks(market=market)
        if frame is None or frame.empty:
            continue

        for item in frame.to_dict("records"):
            code = str(item.get("code") or "").strip()
            name = clean_stock_name(item.get("name"))

            if (
                not re.fullmatch(r"\d{6}", code)
                or not name
                or not is_common_a_share_code(code)
                or market != preferred_market_for_code(code)
            ):
                continue

            next_stock = {
                "code": code,
                "name": name,
                "market": market,
            }
            existing = stocks.get(code)

            if existing is None:
                stocks[code] = next_stock
                continue

            preferred_market = preferred_market_for_code(code)
            if existing["market"] != preferred_market and market == preferred_market:
                stocks[code] = next_stock

    result = sorted(stocks.values(), key=lambda stock: stock["code"])
    with UNIVERSE_PATH.open("w", encoding="utf-8") as file:
        json.dump(result, file, ensure_ascii=False, indent=2)
    return result


def search_stock_candidates(query: str, limit: int = 12) -> list[dict]:
    term = str(query or "").strip()
    if not term:
        return []

    normalized_term = normalize_name(term)
    digit_term = re.sub(r"\D", "", term)
    candidates = []

    for stock in load_stock_universe():
        code = stock["code"]
        name = stock["name"]
        normalized_name = normalize_name(name)
        score = 0

        if digit_term and code == digit_term:
            score = 120
        elif digit_term and code.startswith(digit_term):
            score = 90 - (len(code) - len(digit_term))
        elif normalized_name == normalized_term:
            score = 110
        elif normalized_name.startswith(normalized_term):
            score = 80
        elif normalized_term in normalized_name:
            score = 65
        elif is_subsequence(normalized_term, normalized_name):
            score = 45

        if score:
            candidates.append({**stock, "score": score})

    candidates.sort(key=lambda stock: (-stock["score"], stock["code"]))
    return [{key: stock[key] for key in ("code", "name", "market")} for stock in candidates[:limit]]


def resolve_stock_input(body: dict) -> tuple[str, str]:
    raw_code = body.get("code")
    if raw_code:
        code = normalize_code(raw_code)
        name = str(body.get("name") or code).strip()[:32] or code
        return code, name

    query = str(body.get("query") or body.get("name") or "").strip()
    if not query:
        raise ValueError("Please enter a stock code or name")

    candidates = search_stock_candidates(query)
    exact = [
        stock
        for stock in candidates
        if stock["code"] == query or normalize_name(stock["name"]) == normalize_name(query)
    ]

    if len(exact) == 1:
        return exact[0]["code"], exact[0]["name"]

    if len(candidates) == 1:
        return candidates[0]["code"], candidates[0]["name"]

    raise LookupError(json.dumps({"candidates": candidates}, ensure_ascii=False))


def frame_to_kline(frame: pd.DataFrame) -> list[dict]:
    prices = frame.sort_index().copy()
    for column in ("open", "high", "low", "close"):
        prices[column] = prices[column].astype(float)

    kline = []
    for trade_dt, item in prices.iterrows():
        volume_value = item.get("volume", item.get("vol", 0))
        amount_value = item.get("amount", 0)
        kline.append(
            {
                "date": trade_dt.date().isoformat(),
                "open": round(float(item["open"]), 4),
                "high": round(float(item["high"]), 4),
                "low": round(float(item["low"]), 4),
                "close": round(float(item["close"]), 4),
                "volume": round(float(volume_value or 0), 4),
                "amount": round(float(amount_value or 0), 4),
            }
        )
    return kline


def fetch_index_klines(client) -> dict:
    indexes = {
        "sh": {"code": "000001", "name": "上证指数"},
        "sz": {"code": "399001", "name": "深证成指"},
    }

    result = {}
    for key, info in indexes.items():
        try:
            frame = client.index(symbol=info["code"], frequency=9, offset=800)
            result[key] = {
                **info,
                "kline": frame_to_kline(frame) if frame is not None and not frame.empty else [],
            }
        except Exception as exc:  # noqa: BLE001 - keep stock refresh usable if one index fails.
            result[key] = {**info, "kline": [], "error": str(exc)}

    return result


def get_index_klines(client, stock_end_date: str) -> dict:
    cached = load_indices()
    cached_latest = latest_index_date(cached)
    if cached and cached_latest and cached_latest >= stock_end_date:
        return cached

    indices = fetch_index_klines(client)
    if indices:
        save_indices(indices)
    return indices


def calculate_returns(symbol: str, indices: dict | None = None) -> dict:
    client, server, raw = connect_client(symbol)
    prices = raw.sort_index().copy()
    for column in ("open", "high", "low", "close"):
        prices[column] = prices[column].astype(float)
    latest = prices.iloc[-1]
    latest_dt = prices.index[-1]
    latest_date = latest_dt.normalize()
    latest_close = float(latest["close"])
    rows = []
    kline = frame_to_kline(prices)

    def append_row(
        period_type: str,
        period_value: int,
        label: str,
        target,
        candidates: pd.DataFrame,
    ) -> None:
        if candidates.empty:
            return

        base = candidates.iloc[-1]
        base_dt = candidates.index[-1]
        base_close = float(base["close"])
        return_pct = (latest_close / base_close - 1) * 100

        row = {
            "period_type": period_type,
            "period_value": period_value,
            "label": label,
            "target_date": target.date().isoformat(),
            "base_trade_date": base_dt.date().isoformat(),
            "base_close": round(base_close, 4),
            "end_trade_date": latest_dt.date().isoformat(),
            "end_close": round(latest_close, 4),
            "return_pct": round(return_pct, 2),
        }
        if period_type == "month":
            row["months"] = period_value

        rows.append(row)

    for days in (5, 10, 15):
        if len(prices) <= days:
            continue

        target_dt = prices.index[-days]
        base_candidates = prices[prices.index < target_dt]
        append_row(
            period_type="trading_day",
            period_value=days,
            label=f"{days}交易日",
            target=target_dt.normalize(),
            candidates=base_candidates,
        )

    for months in range(1, 25):
        target = latest_date - pd.DateOffset(months=months)
        candidates = prices[prices.index.normalize() < target]
        append_row(
            period_type="month",
            period_value=months,
            label=f"{months}月",
            target=target,
            candidates=candidates,
        )

    quote = None
    try:
        quote_df = client.quotes(symbol=[symbol])
        if quote_df is not None and not quote_df.empty:
            first = quote_df.iloc[0]
            quote = {
                "price": round(float(first.get("price")), 4),
                "last_close": round(float(first.get("last_close")), 4),
                "servertime": str(first.get("servertime")),
            }
    except Exception:
        quote = None

    return {
        "rows": rows,
        "kline": kline,
        "indices": indices if indices is not None else get_index_klines(client, latest_dt.date().isoformat()),
        "meta": {
            "symbol": symbol,
            "server": f"{server[0]}:{server[1]}",
            "raw_start_date": prices.index[0].date().isoformat(),
            "raw_end_date": prices.index[-1].date().isoformat(),
            "raw_rows": int(len(prices)),
            "end_trade_date": latest_dt.date().isoformat(),
            "end_close": round(latest_close, 4),
            "quote": quote,
            "adjust": "none",
        },
    }


class StockAppHandler(SimpleHTTPRequestHandler):
    server_version = "StockReturnApp/1.0"

    def log_message(self, format: str, *args) -> None:
        print(f"[{now_text()}] {self.address_string()} {format % args}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path in {"/", "/index.html", "/baoding_return_bar_chart.html"}:
            self.send_html()
            return

        if path == "/api/stocks":
            store = load_store()
            self.send_json({"stocks": store["stocks"]})
            return

        if path == "/api/search":
            query = parse_qs(parsed.query).get("q", [""])[0]
            limit_text = parse_qs(parsed.query).get("limit", ["12"])[0]
            try:
                limit = max(1, min(30, int(limit_text)))
            except ValueError:
                limit = 12

            try:
                self.send_json({"candidates": search_stock_candidates(query, limit=limit)})
            except Exception as exc:  # noqa: BLE001 - send readable API error.
                self.send_error_json(HTTPStatus.BAD_GATEWAY, f"Search failed: {exc}")
            return

        match = re.fullmatch(r"/api/stocks/(\d{6})", path)
        if match:
            store = load_store()
            stock = get_stock(store, match.group(1))
            if stock is None:
                self.send_error_json(HTTPStatus.NOT_FOUND, "未找到该股票")
                return
            self.send_json(stock_response(stock))
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path == "/api/stocks":
            self.add_stock()
            return

        if path == "/api/stocks/refresh-all":
            self.refresh_all_stocks()
            return

        match = re.fullmatch(r"/api/stocks/(\d{6})/refresh", path)
        if match:
            self.refresh_stock(match.group(1))
            return

        self.send_error_json(HTTPStatus.NOT_FOUND, "接口不存在")

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        match = re.fullmatch(r"/api/stocks/(\d{6})", path)

        if not match:
            self.send_error_json(HTTPStatus.NOT_FOUND, "接口不存在")
            return

        self.update_stock_info(match.group(1))

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        match = re.fullmatch(r"/api/stocks/(\d{6})", path)

        if not match:
            self.send_error_json(HTTPStatus.NOT_FOUND, "接口不存在")
            return

        code = match.group(1)
        if code == DEFAULT_STOCK["code"]:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "默认股票不允许删除")
            return

        store = load_store()
        before = len(store["stocks"])
        store["stocks"] = [stock for stock in store["stocks"] if stock.get("code") != code]

        if len(store["stocks"]) == before:
            self.send_error_json(HTTPStatus.NOT_FOUND, "未找到该股票")
            return

        save_store(store)
        stock_detail_path(code).unlink(missing_ok=True)
        self.send_json({"stocks": store["stocks"]})

    def add_stock(self) -> None:
        body = self.read_json()
        try:
            code, name = resolve_stock_input(body)
        except ValueError as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return
        except LookupError as exc:
            payload = json.loads(str(exc))
            payload["error"] = "Multiple candidates found. Please select one."
            self.send_json(payload, status=HTTPStatus.CONFLICT)
            return

        store = load_store()
        stock = get_stock(store, code)

        if stock is None:
            stock = stock_summary({"code": code, "name": name, "group": "", "last_updated": None})
            store["stocks"].append(stock)
        else:
            stock["name"] = name
            detail_path = stock_detail_path(code)
            if detail_path.exists():
                detail = load_stock_detail(code, stock)
                detail["name"] = name
                save_stock_detail(detail)

        save_store(store)
        self.send_json({"stock": stock_summary(stock), "stocks": store["stocks"]})

    def update_stock_info(self, code: str) -> None:
        body = self.read_json()
        store = load_store()
        stock = get_stock(store, code)

        if stock is None:
            self.send_error_json(HTTPStatus.NOT_FOUND, "未找到该股票")
            return

        group = str(body.get("group") or "").strip()[:32]
        stock["group"] = group
        detail_path = stock_detail_path(code)
        if detail_path.exists():
            detail = load_stock_detail(code, stock)
            detail["group"] = group
            save_stock_detail(detail)

        save_store(store)
        self.send_json({"stock": stock_summary(stock), "stocks": store["stocks"]})

    def refresh_stock(self, code: str) -> None:
        store = load_store()
        stock = get_stock(store, code)

        if stock is None:
            stock = stock_summary({"code": code, "name": code, "group": "", "last_updated": None})
            store["stocks"].append(stock)

        try:
            result = calculate_returns(code)
        except Exception as exc:  # noqa: BLE001 - send readable API error.
            self.send_error_json(HTTPStatus.BAD_GATEWAY, f"获取行情失败：{exc}")
            return

        detail = self.persist_refresh_result(stock, result)
        save_store(store)
        detail.update(stock)
        detail["indices"] = result["indices"]
        detail["stocks"] = store["stocks"]
        self.send_json(detail)

    def refresh_all_stocks(self) -> None:
        store = load_store()
        stocks = store["stocks"]
        refreshed = []
        failed = []
        indices = None
        index_error = None

        try:
            client, _server = connect_quotes_client()
            indices = fetch_index_klines(client)
            save_indices(indices)
        except Exception as exc:  # noqa: BLE001 - keep stock refresh attempts useful.
            index_error = str(exc)
            indices = load_indices()

        shared_indices = indices or None
        for stock in stocks:
            code = stock.get("code")
            try:
                result = calculate_returns(code, indices=shared_indices)
                if shared_indices is None and result.get("indices"):
                    shared_indices = result["indices"]
                self.persist_refresh_result(stock, result)
                refreshed.append({"code": code, "name": stock.get("name")})
            except Exception as exc:  # noqa: BLE001 - report per-stock failures.
                failed.append({"code": code, "name": stock.get("name"), "error": str(exc)})

        save_store(store)
        self.send_json(
            {
                "stocks": store["stocks"],
                "total": len(stocks),
                "updated_count": len(refreshed),
                "failed_count": len(failed),
                "updated": refreshed,
                "failed": failed,
                "index_error": index_error,
            }
        )

    def persist_refresh_result(self, stock: dict, result: dict) -> dict:
        code = normalize_code(stock.get("code"))
        detail = {
            "code": code,
            "name": stock.get("name") or code,
            "group": stock.get("group") or "",
            "rows": result["rows"],
            "kline": result["kline"],
            "meta": result["meta"],
            "last_updated": now_text(),
        }
        stock.update(stock_summary(stock, detail))
        save_stock_detail(detail)
        return detail

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def send_html(self) -> None:
        content = HTML_FILE.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def send_error_json(self, status: HTTPStatus, message: str) -> None:
        self.send_json({"error": message}, status=status)


def run(port: int) -> None:
    load_store()
    server = ThreadingHTTPServer(("127.0.0.1", port), StockAppHandler)
    print(f"Stock return app running at http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    try:
        run(args.port)
    except Exception as exc:  # noqa: BLE001
        print(f"启动失败：{exc}", file=sys.stderr)
        sys.exit(1)
