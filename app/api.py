from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .browser import BrowserClient, RequestBatch, RETRY_COOLDOWNS


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(__file__).with_name("browser_config.json")

LEADERBOARD_API = "/ark/api/data/pugna_component/data/v2/faction/realtime_dashboard/anchor_income_rank"
DAILY_TREND_API = (
    "/ark/api/uanchor/pugna_component/data/anchor/portrait/"
    "core_day_trend/c80eh2jc77uelq3kif80"
)
ANCHOR_DETAIL_API = "/ark/api/data/anchor/detail_v2/get_anchor_detail"
ROOM_LIST_API = "/ark/api/uanchor/pugna_component/data/v2/faction/anchor_detail/room_list_with_tag"
MINUTE_API = "/ark_api_tinker_proxy/lego/native/webcast_api/room/replay/minute_trend"
REQUEST_INTERVAL_MS = 150
DAILY_TREND_FIELDS = (
    "score",
    "validLiveDuration",
    "showUv",
    "showCnt",
    "watchUv",
    "watchCnt",
    "enterRoomRate",
    "avgWatchDuration",
    "consumeUv",
    "consumeCnt",
    "newFansUv",
    "acu",
    "consumeRate",
    "gameIncome",
    "gameDividedIncome",
)


def cdp_url_for(backend: str) -> str:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    for item in config["backends"].values():
        if item["display_name"] == backend:
            return str(item["cdp_url"])
    raise ValueError(f"未配置后台浏览器: {backend}")


def parse_series(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    wrapped = payload.get("data", {}).get("data_string")
    if not wrapped:
        raise RuntimeError("接口未返回 data_string")
    inner = json.loads(wrapped)
    if inner.get("code", 0) != 0:
        raise RuntimeError(inner.get("message") or str(inner))
    data = inner.get("data") or {}
    if isinstance(data, list):
        return data, len(data)
    return data.get("series") or [], int(data.get("total") or 0)


def numeric(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


class UnionApi(BrowserClient):
    def daily_anchors(self, day: date, page_size: int = 500) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page = 1
        while True:
            items = [
                {
                    "key": str(page),
                    "params": {
                        "rankType": 5,
                        "beginDate": day.isoformat(),
                        "endDate": day.isoformat(),
                        "page": page,
                        "size": page_size,
                        "orderField": "income",
                        "orderType": "desc",
                        "filterType": "anchor",
                    },
                }
            ]
            payloads, errors = self.request_many(LEADERBOARD_API, items)
            if errors:
                raise RuntimeError(errors[str(page)])
            page_rows, total = parse_series(payloads[str(page)])
            paid = [row for row in page_rows if numeric(row.get("income")) > 0]
            rows.extend(paid)
            if len(paid) < len(page_rows) or page * page_size >= total:
                break
            page += 1

        anchors = [
            {
                "anchor_id": str(row["anchorID"]),
                "nick_name": row.get("anchorName", ""),
                "douyin_id": str(row.get("displayID") or row.get("hotsoonID") or ""),
                "broker": row.get("brokerName", ""),
                "daily_income": int(numeric(row.get("income"))),
                "daily_duration": int(numeric(row.get("liveDuration"))),
            }
            for row in rows
            if row.get("anchorID")
        ]
        anchors.sort(key=lambda row: (-row["daily_income"], row["anchor_id"]))
        for rank, anchor in enumerate(anchors, 1):
            anchor["rank"] = rank
        self._fill_numeric_douyin_ids(anchors)
        return anchors

    def _fill_numeric_douyin_ids(self, anchors: list[dict[str, Any]]) -> None:
        unresolved = [anchor for anchor in anchors if not anchor["douyin_id"].isdigit()]
        if not unresolved:
            return
        print(f"正在补齐 {len(unresolved)} 名主播的数字抖音号...", flush=True)
        items = [
            {"key": anchor["anchor_id"], "params": {"anchor_id": anchor["anchor_id"]}}
            for anchor in unresolved
        ]
        payloads, _ = self.request_many(ANCHOR_DETAIL_API, items, REQUEST_INTERVAL_MS)
        for anchor in unresolved:
            short_id = str((payloads.get(anchor["anchor_id"], {}).get("data") or {}).get("short_id") or "")
            anchor["douyin_id"] = short_id if short_id.isdigit() else anchor["anchor_id"]

    @staticmethod
    def _daily_items(anchors: list[dict[str, Any]], begin: date, end: date) -> list[dict[str, Any]]:
        return [
            {
                "key": anchor["anchor_id"],
                "params": {
                    "anchorID": anchor["anchor_id"],
                    "beginDate": begin.isoformat(),
                    "endDate": end.isoformat(),
                    "names": DAILY_TREND_FIELDS,
                },
            }
            for anchor in anchors
        ]

    @staticmethod
    def _room_items(anchors: list[dict[str, Any]], day: date) -> list[dict[str, Any]]:
        return [
            {
                "key": anchor["anchor_id"],
                "params": {
                    "orderField": "liveStartTime",
                    "page": 1,
                    "size": 1000,
                    "anchorID": anchor["anchor_id"],
                    "liveID": 1,
                    "beginDate": day.isoformat(),
                    "endDate": (day + timedelta(days=1)).isoformat(),
                },
            }
            for anchor in anchors
        ]

    @staticmethod
    def _daily_rows(
        payloads: dict[str, dict[str, Any]],
        target: str,
    ) -> dict[str, dict[str, Any]]:
        rows = {}
        for anchor_id, payload in payloads.items():
            row = next((item for item in parse_series(payload)[0] if item.get("date") == target), None)
            if row is not None:
                rows[anchor_id] = row
        return rows

    @staticmethod
    def _room_rows(
        payloads: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
        rooms = {}
        empty = set()
        for anchor_id, payload in payloads.items():
            unique = {
                str(room["roomID"]): room
                for room in parse_series(payload)[0]
                if room.get("roomID")
            }
            if unique:
                rooms[anchor_id] = list(unique.values())
            else:
                empty.add(anchor_id)
        return rooms, empty

    def daily_details_and_rooms(
        self,
        anchors: list[dict[str, Any]],
        room_anchors: list[dict[str, Any]],
        day: date,
    ) -> tuple[
        dict[str, dict[str, Any]],
        dict[str, str],
        dict[str, list[dict[str, Any]]],
        dict[str, str],
    ]:
        batches = self.request_batches(
            {
                "日数据": RequestBatch(
                    DAILY_TREND_API,
                    self._daily_items(anchors, day, day),
                    REQUEST_INTERVAL_MS,
                ),
                "场次": RequestBatch(
                    ROOM_LIST_API,
                    self._room_items(room_anchors, day),
                    REQUEST_INTERVAL_MS,
                ),
            }
        )
        daily_payloads, daily_errors = batches["日数据"]
        room_payloads, room_errors = batches["场次"]
        target = day.isoformat()
        daily = self._daily_rows(daily_payloads, target)
        rooms, empty_rooms = self._room_rows(room_payloads)

        missing_daily = [
            anchor
            for anchor in anchors
            if anchor["anchor_id"] not in daily and anchor["anchor_id"] not in daily_errors
        ]
        if missing_daily:
            end = max(day, min(day + timedelta(days=29), date.today() - timedelta(days=1)))
            print(f"日明细单日缺行，改用30天范围复查 {len(missing_daily)} 名主播...", flush=True)
            payloads, errors = self.request_many(
                DAILY_TREND_API,
                self._daily_items(missing_daily, end - timedelta(days=29), end),
                REQUEST_INTERVAL_MS,
            )
            daily.update(self._daily_rows(payloads, target))
            daily_errors.update(errors)
            for anchor in missing_daily:
                anchor_id = anchor["anchor_id"]
                if anchor_id not in daily and anchor_id not in daily_errors:
                    daily_errors[anchor_id] = f"分日趋势未返回 {target} 记录"

        if empty_rooms:
            room_anchors_by_id = {anchor["anchor_id"]: anchor for anchor in room_anchors}
            pending = {anchor_id: room_anchors_by_id[anchor_id] for anchor_id in empty_rooms}
            last_errors: dict[str, str] = {}
            for delay in RETRY_COOLDOWNS:
                print(f"场次接口返回空列表，{delay} 秒后复查 {len(pending)} 名主播...", flush=True)
                time.sleep(delay)
                payloads, errors = self.request_many(
                    ROOM_LIST_API,
                    self._room_items(list(pending.values()), day),
                    REQUEST_INTERVAL_MS,
                    cooldowns=(),
                )
                recovered, still_empty = self._room_rows(payloads)
                rooms.update(recovered)
                unresolved = still_empty | set(errors)
                last_errors = {anchor_id: errors[anchor_id] for anchor_id in errors}
                pending = {anchor_id: pending[anchor_id] for anchor_id in unresolved}
                if not pending:
                    break
            for anchor_id in pending:
                if anchor_id in last_errors:
                    room_errors[anchor_id] = last_errors[anchor_id]
                else:
                    rooms[anchor_id] = []

        return daily, daily_errors, rooms, room_errors

    def minutes(
        self,
        rooms: list[dict[str, Any]],
        interval_ms: int,
        on_complete: Callable[[str, dict[str, Any] | None, str], None] | None = None,
    ) -> None:
        items = [
            {"key": str(room["roomID"]), "params": {"roomID": room["roomID"], "commonParams": "{}"}}
            for room in rooms
        ]
        self.request_many(MINUTE_API, items, interval_ms, on_complete=on_complete)
