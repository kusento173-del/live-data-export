from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

SESSION_FIELDS = {
    "roomID": "直播间ID",
    "roomTitle": "直播标题",
    "liveStartTime": "开播时间",
    "liveEndTime": "关播时间",
    "liveDuration": "直播时长秒",
    "showUV": "曝光人数",
    "showPV": "曝光次数",
    "watchUV": "进房人数",
    "watchPV": "进房次数",
    "commentUV": "评论人数",
    "payUV": "送礼人数",
    "payPV": "送礼次数",
    "liveFollowUV": "新增粉丝",
    "fanTicket": "音浪",
    "newGifterUcnt": "新送礼人数",
    "newGifterFanTicket": "新送礼音浪",
    "repeatGifterUcnt": "二次送礼人数",
    "acu": "平均在线人数",
    "avgWatchDuration": "人均观看时长秒",
    "pkCnt": "PK次数",
    "pkScore": "PK获得分值",
    "likePV": "点赞次数",
}

MINUTE_FIELDS = {
    "timeMinute": "分钟时间",
    "earnScore": "收获音浪",
    "watchUcnt": "进房人数",
    "showCnt": "曝光次数",
    "pcuTotal": "在线人数",
    "consumeUcnt": "送礼人数",
    "giftNum": "礼物数量",
    "commentCnt": "评论次数",
    "commentUcnt": "评论人数",
    "likeCnt": "点赞次数",
    "serverLikeUcnt": "点赞人数",
    "followUcnt": "涨粉人数",
    "unfollowUcnt": "掉粉人数",
    "leaveUcnt": "离开人数",
    "shareCnt": "分享次数",
    "fansWatchUcnt": "粉丝观看人数",
    "nonFansWatchUcnt": "非粉丝观看人数",
    "fansConsumeUcnt": "粉丝送礼人数",
    "nonFansConsumeUcnt": "非粉丝送礼人数",
    "subscribeIncome": "会员收入",
    "starGuardEarnScore": "星守护音浪",
    "starGuardUcnt": "星守护付费人数",
    "isLink": "是否嘉宾连线",
}

DAILY_FIELDS = {
    "score": "音浪",
    "validLiveDuration": "有效开播时长（小时）",
    "showUv": "曝光人数",
    "showCnt": "曝光次数",
    "watchUv": "进直播间人数",
    "watchCnt": "进直播间次数",
    "enterRoomRate": "进直播间转化率（%）",
    "avgWatchDuration": "人均观看时长（分钟）",
    "consumeUv": "打赏人数",
    "consumeCnt": "打赏次数",
    "newFansUv": "新增粉丝",
    "acu": "ACU",
    "gameIncome": "直播-游戏流水（分成前）",
    "gameDividedIncome": "直播-主播游戏收入（分成后）",
}

_PARQUET_CONNECTION: Any = None


def safe_name(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value).strip(" .") or "anchor"


def number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def compact(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def anchor_columns(anchor: dict[str, Any]) -> dict[str, Any]:
    return {
        "当日音浪排名": anchor["rank"],
        "当日总音浪": anchor["daily_income"],
        "主播昵称": anchor["nick_name"],
        "抖音号/短ID": anchor["douyin_id"],
        "主播ID": anchor["anchor_id"],
        "对应运营": anchor["broker"],
    }


def session_row(anchor: dict[str, Any], room: dict[str, Any]) -> dict[str, Any]:
    return {**anchor_columns(anchor), **{title: room.get(field, "") for field, title in SESSION_FIELDS.items()}}


def minute_rows(
    anchor: dict[str, Any], room: dict[str, Any], payload: dict[str, Any], target_date: str
) -> list[dict[str, Any]]:
    base = {
        **anchor_columns(anchor),
        "直播间ID": room.get("roomID", ""),
        "直播标题": room.get("roomTitle", ""),
        "开播时间": room.get("liveStartTime", ""),
        "关播时间": room.get("liveEndTime", ""),
    }
    return [
        {**base, **{title: item.get(field, "") for field, title in MINUTE_FIELDS.items()}}
        for item in payload.get("data", {}).get("series") or []
        if str(item.get("timeMinute", ""))[:10] == target_date
    ]


def aggregate_hourly(minutes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sums = {
        "收获音浪": "收获音浪",
        "进房人数": "进房人数（分钟口径）",
        "曝光次数": "曝光次数",
        "送礼人数": "送礼人数（分钟口径）",
        "礼物数量": "礼物数量",
        "评论次数": "评论次数",
        "评论人数": "评论人数（分钟口径）",
        "点赞次数": "点赞次数",
        "涨粉人数": "涨粉人数",
        "掉粉人数": "掉粉人数",
        "分享次数": "分享次数",
    }
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in minutes:
        timestamp = datetime.fromisoformat(str(row["分钟时间"]))
        groups[timestamp.strftime("%Y-%m-%d %H:00:00")].append(row)

    output = []
    for hour_start, rows in sorted(groups.items()):
        start = datetime.fromisoformat(hour_start)
        room_ids = sorted({str(row["直播间ID"]) for row in rows})
        online = [number(row.get("在线人数")) for row in rows]
        room_periods = {
            str(row["直播间ID"]): (row.get("开播时间"), row.get("关播时间"))
            for row in rows
        }
        live_seconds = 0.0
        for room_start, room_end in room_periods.values():
            try:
                live_start = datetime.fromisoformat(str(room_start))
                live_end = datetime.fromisoformat(str(room_end))
            except ValueError:
                continue
            overlap_start = max(live_start, start)
            overlap_end = min(live_end, start + timedelta(hours=1))
            live_seconds += max(0.0, (overlap_end - overlap_start).total_seconds())
        result = {
            "统计日期": start.date().isoformat(),
            "统计小时": start.strftime("%H:00"),
            "小时开始": hour_start,
            "小时结束": (start + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
            **{
                key: rows[0].get(key, "")
                for key in ("当日音浪排名", "当日总音浪", "主播昵称", "抖音号/短ID", "主播ID", "对应运营")
            },
            "直播间ID": ",".join(room_ids),
            "场次数": len(room_ids),
            "有效分钟数": len(rows),
            "开播时段秒（按开关播时间）": compact(live_seconds),
        }
        for source, target in sums.items():
            result[target] = compact(sum(number(row.get(source)) for row in rows))
        result["平均在线人数"] = round(sum(online) / len(online), 2) if online else 0
        result["最高在线人数"] = compact(max(online, default=0))
        output.append(result)
    return output


def daily_row(anchor: dict[str, Any], day: str, metrics: dict[str, Any] | None) -> dict[str, Any]:
    metrics = metrics or {}
    return {
        "统计日期": day,
        "当日音浪排名": anchor["rank"],
        "主播昵称": anchor["nick_name"],
        "抖音号/短ID": anchor["douyin_id"],
        "主播ID": anchor["anchor_id"],
        "对应运营": anchor["broker"],
        **{title: metrics.get(field, "") for field, title in DAILY_FIELDS.items()},
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.unlink(missing_ok=True)
        return
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    content = ("\ufeff" + buffer.getvalue()).encode("utf-8")
    try:
        if path.read_bytes() == content:
            return
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_minute_parquet(path: Path, minutes: list[dict[str, Any]]) -> None:
    import duckdb
    import pandas as pd

    global _PARQUET_CONNECTION
    if _PARQUET_CONNECTION is None:
        _PARQUET_CONNECTION = duckdb.connect()

    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(minutes)
    frame["主播ID"] = frame["主播ID"].astype(str)
    frame["直播间ID"] = frame["直播间ID"].astype(str)
    frame["分钟时间"] = pd.to_datetime(frame["分钟时间"], errors="coerce")
    excluded = {
        "当日音浪排名", "当日总音浪", "主播昵称", "抖音号/短ID", "主播ID", "对应运营",
        "直播间ID", "直播标题", "开播时间", "关播时间", "分钟时间", "是否嘉宾连线",
    }
    for column in set(frame.columns) - excluded:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0)
    temporary = path.with_suffix(".tmp.parquet")
    temporary.unlink(missing_ok=True)
    registered = False
    try:
        _PARQUET_CONNECTION.register("minutes", frame)
        registered = True
        target = temporary.as_posix().replace("'", "''")
        _PARQUET_CONNECTION.execute(f"COPY minutes TO '{target}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        temporary.replace(path)
    finally:
        if registered:
            _PARQUET_CONNECTION.unregister("minutes")
        temporary.unlink(missing_ok=True)


def update_minute_parquet_anchor(path: Path, columns: dict[str, Any]) -> None:
    import duckdb

    connection = duckdb.connect()
    try:
        frame = connection.execute("SELECT * FROM read_parquet(?)", [str(path)]).fetchdf()
    finally:
        connection.close()
    for column, value in columns.items():
        frame[column] = value
    write_minute_parquet(path, frame.to_dict("records"))
