"""SQLite persistence for SKU mappings, webhook events, orders, and tracking."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping

def _now() -> str:
    return datetime.now(UTC).isoformat()


class FulfillmentStore:
    """Small SQLite store designed for local deployment and low-volume shops."""

    def __init__(self, db_path: str | Path = "data/fulfillment.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sku_mappings (
                    mapping_key TEXT PRIMARY KEY,
                    platform TEXT NOT NULL DEFAULT 'shopee',
                    platform_sku TEXT NOT NULL,
                    supplier_name TEXT NOT NULL DEFAULT '',
                    supplier_url TEXT NOT NULL,
                    supplier_spec TEXT NOT NULL DEFAULT '',
                    supplier_sku_id TEXT NOT NULL DEFAULT '',
                    purchase_cost_cny REAL NOT NULL,
                    shipping_cny REAL NOT NULL DEFAULT 0,
                    notes TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS webhook_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(platform, event_id),
                    UNIQUE(platform, payload_hash)
                );
                CREATE TABLE IF NOT EXISTS orders (
                    platform TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    profit_cny REAL NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    confirmation_status TEXT NOT NULL DEFAULT 'PENDING',
                    confirmed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (platform, order_id)
                );
                CREATE TABLE IF NOT EXISTS tracking_updates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    tracking_code TEXT NOT NULL,
                    sync_status TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_orders_status
                    ON orders(platform, status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_tracking_order
                    ON tracking_updates(platform, order_id, updated_at DESC);
                """
            )
    def import_mappings_from_json(self, mapping_path: str | Path) -> int:
        path = Path(mapping_path)
        if not path.exists():
            return 0
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("mappings"), dict):
            payload = payload["mappings"]
        if not isinstance(payload, Mapping):
            return 0

        from order_fulfillment import SkuMappingRecord

        

        imported = 0
        for sku, value in payload.items():
            if not isinstance(value, Mapping):
                continue
            record = SkuMappingRecord.model_validate(value)
            self.upsert_mapping(record, platform_sku=str(sku))
            imported += 1
        return imported

    def upsert_mapping(
        self,
        record: SkuMappingRecord,
        *,
        platform: str = "shopee",
        platform_sku: str | None = None,
    ) -> None:
        sku = platform_sku or record.shopee_sku or record.supplier_sku_id
        mapping_key = f"{platform}:{sku}"
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO sku_mappings (
                    mapping_key, platform, platform_sku, supplier_name, supplier_url,
                    supplier_spec, supplier_sku_id, purchase_cost_cny, shipping_cny,
                    notes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mapping_key) DO UPDATE SET
                    supplier_name=excluded.supplier_name,
                    supplier_url=excluded.supplier_url,
                    supplier_spec=excluded.supplier_spec,
                    supplier_sku_id=excluded.supplier_sku_id,
                    purchase_cost_cny=excluded.purchase_cost_cny,
                    shipping_cny=excluded.shipping_cny,
                    notes=excluded.notes,
                    updated_at=excluded.updated_at
                """,
                (
                    mapping_key,
                    platform,
                    sku,
                    record.supplier_name,
                    record.supplier_url,
                    record.supplier_spec,
                    record.supplier_sku_id,
                    record.purchase_cost_cny,
                    record.shipping_cny,
                    record.notes,
                    _now(),
                ),
            )

    def get_mapping(self, platform_sku: str, *, platform: str = "shopee") -> SkuMappingRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM sku_mappings
                WHERE platform = ? AND platform_sku = ? COLLATE NOCASE
                ORDER BY updated_at DESC LIMIT 1
                """,
                (platform, platform_sku),
            ).fetchone()
        if row is None:
            return None
        from order_fulfillment import SkuMappingRecord
        
        return SkuMappingRecord(
            shopee_sku=row["platform_sku"],
            supplier_name=row["supplier_name"],
            supplier_url=row["supplier_url"],
            supplier_spec=row["supplier_spec"],
            supplier_sku_id=row["supplier_sku_id"],
            purchase_cost_cny=row["purchase_cost_cny"],
            shipping_cny=row["shipping_cny"],
            notes=row["notes"],
        )
    def register_webhook_event(
        self,
        *,
        platform: str,
        event_id: str,
        payload_hash: str,
        order_id: str,
        payload: Mapping[str, Any],
    ) -> bool:
        """Return True for a new event and False when it is a duplicate."""

        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO webhook_events (
                        platform, event_id, payload_hash, order_id, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        platform,
                        event_id,
                        payload_hash,
                        order_id,
                        json.dumps(payload, ensure_ascii=False, default=str),
                        _now(),
                    ),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def save_order(
        self,
        *,
        platform: str,
        order_id: str,
        status: str,
        profit_cny: float,
        payload: Mapping[str, Any],
        decision: Mapping[str, Any],
    ) -> None:
        timestamp = _now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO orders (
                    platform, order_id, status, profit_cny, payload_json,
                    decision_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, order_id) DO UPDATE SET
                    status=excluded.status,
                    profit_cny=excluded.profit_cny,
                    payload_json=excluded.payload_json,
                    decision_json=excluded.decision_json,
                    updated_at=excluded.updated_at
                """,
                (
                    platform,
                    order_id,
                    status,
                    profit_cny,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    json.dumps(decision, ensure_ascii=False, default=str),
                    timestamp,
                    timestamp,
                ),
            )
    def get_order(self, platform: str, order_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM orders WHERE platform = ? AND order_id = ?",
                (platform, order_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "platform": row["platform"],
            "order_id": row["order_id"],
            "status": row["status"],
            "profit_cny": row["profit_cny"],
            "payload": json.loads(row["payload_json"]),
            "decision": json.loads(row["decision_json"]),
            "confirmation_status": row["confirmation_status"],
            "confirmed_at": row["confirmed_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_orders(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT platform, order_id, status, profit_cny, confirmation_status,
                       created_at, updated_at
                FROM orders ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def confirm_order(self, platform: str, order_id: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE orders
                SET confirmation_status='CONFIRMED', confirmed_at=?, updated_at=?
                WHERE platform=? AND order_id=?
                """,
                (_now(), _now(), platform, order_id),
            )
        return cursor.rowcount > 0

    def record_tracking(
        self,
        *,
        platform: str,
        order_id: str,
        tracking_code: str,
        sync_status: str,
        response: Mapping[str, Any],
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO tracking_updates (
                    platform, order_id, tracking_code, sync_status, response_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    platform,
                    order_id,
                    tracking_code,
                    sync_status,
                    json.dumps(response, ensure_ascii=False, default=str),
                    _now(),
                ),
            )


class SQLiteMappingStore:
    """SQLite-backed mapping store compatible with SkuMappingStore.get()."""

    def __init__(
        self,
        store: FulfillmentStore,
        *,
        import_json_path: str | Path | None = None,
        platform: str = "shopee",
    ) -> None:
        self.store = store
        self.platform = platform
        if import_json_path:
            self.store.import_mappings_from_json(import_json_path)

    def get(self, sku_id: str) -> SkuMappingRecord:
        record = self.store.get_mapping(sku_id, platform=self.platform)
        if record is None:
            from order_fulfillment import MappingNotFoundError

            raise MappingNotFoundError(f"No mapping found for SKU: {sku_id}")
        return record