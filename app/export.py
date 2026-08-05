from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .api import PROJECT_ROOT, UnionApi, cdp_url_for
from .report import (
    aggregate_hourly,
    anchor_columns,
    daily_row,
    minute_rows,
    safe_name,
    session_row,
    update_minute_parquet_anchor,
    write_csv,
    write_minute_parquet,
)


ANCHOR_CACHE_VERSION = 9
SUMMARY_CACHE_VERSION = 8
MINUTE_CACHE_VERSION = 4


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="分阶段导出指定日期的场次、日、分钟和小时数据。")
    parser.add_argument("--backend-name", required=True, choices=("新心", "中鼎"))
    parser.add_argument("--date", type=date.fromisoformat, default=date.today() - timedelta(days=1))
    parser.add_argument("--phase", choices=("summary", "minutes", "all"), default="all")
    parser.add_argument("--minute-interval-ms", type=int, default=150, help="分钟请求启动间隔，默认 150 毫秒")
    return parser.parse_args()


def prevent_console_pause() -> None:
    if sys.platform != "win32":
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetStdHandle(-10)
    mode = ctypes.c_uint()
    if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        kernel32.SetConsoleMode(handle, (mode.value | 0x80) & ~0x40)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def valid_cache(path: Path, version: int, day: str, required_file: Path | None = None) -> dict[str, Any] | None:
    if required_file is not None and not required_file.is_file():
        return None
    data = read_json(path)
    if data and data.get("version") == version and data.get("date") == day:
        return data
    return None


def add_backend(rows: list[dict[str, Any]], backend: str) -> list[dict[str, Any]]:
    return [{"后台": backend, **row} for row in rows]


def sync_cached_anchor(cached: dict[str, Any], anchor: dict[str, Any], rows_key: str) -> bool:
    columns = anchor_columns(anchor)
    changed = cached.get("anchor") != anchor
    cached["anchor"] = anchor
    for row in cached.get(rows_key, []):
        if any(row.get(column) != value for column, value in columns.items()):
            changed = True
        row.update(columns)
    return changed


def inaccessible(message: str) -> bool:
    return "用户没有权限" in message or "主播不属于" in message


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, second = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours:02d}:{minute:02d}:{second:02d}"


def show_progress(label: str, done: int, total: int, started: float) -> None:
    if done < total and done % 10:
        return
    elapsed = time.monotonic() - started
    percent = done / total * 100 if total else 100
    eta = elapsed / done * (total - done) if done else 0
    print(
        f"{label}: {done}/{total} ({percent:5.1f}%) | 已用 {format_duration(elapsed)} | 预计剩余 {format_duration(eta)}",
        flush=True,
    )


def anchor_error(anchor: dict[str, Any], message: str, phase: str) -> dict[str, Any]:
    return {
        "阶段": phase,
        "当日音浪排名": anchor["rank"],
        "主播昵称": anchor["nick_name"],
        "主播ID": anchor["anchor_id"],
        "错误": message,
    }


def split_rooms(rooms: list[dict[str, Any]], day: date) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    day_start = datetime.combine(day, datetime.min.time())
    day_end = day_start + timedelta(days=1)
    sessions = []
    overlapping = []
    for room in rooms:
        try:
            start = datetime.fromisoformat(str(room.get("liveStartTime", "")))
        except ValueError:
            continue
        try:
            end = datetime.fromisoformat(str(room.get("liveEndTime", "")))
        except ValueError:
            try:
                end = start + timedelta(seconds=float(room.get("liveDuration") or 0))
            except (TypeError, ValueError):
                end = start
        if start.date() == day:
            sessions.append(room)
        if start < day_end and end > day_start:
            overlapping.append(room)
    def key(room: dict[str, Any]) -> str:
        return str(room.get("liveStartTime", ""))

    return sorted(sessions, key=key), sorted(overlapping, key=key)


def load_anchors(api: UnionApi, args: argparse.Namespace, output: Path) -> list[dict[str, Any]]:
    path = output / "主播列表.json"
    saved = read_json(path)
    if (
        saved
        and saved.get("version") == ANCHOR_CACHE_VERSION
        and saved.get("date") == args.date.isoformat()
        and isinstance(saved.get("anchors"), list)
    ):
        anchors = saved["anchors"]
        print(f"复用当日音浪大于 0 主播名单: {len(anchors)} 人", flush=True)
    else:
        if args.phase == "minutes":
            raise RuntimeError("未找到场次阶段生成的主播名单，请先运行 summary 阶段。")
        print(f"正在读取 {args.date.isoformat()} 主播排行榜...", flush=True)
        anchors = api.daily_anchors(args.date)
        write_json(path, {"version": ANCHOR_CACHE_VERSION, "date": args.date.isoformat(), "anchors": anchors})
        print(f"当日音浪大于 0 主播: {len(anchors)} 人，已按当日总音浪排名", flush=True)
    return anchors


def summary_phase(
    api: UnionApi,
    args: argparse.Namespace,
    anchors: list[dict[str, Any]],
    output: Path,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    print("\n========== 阶段 1/2：日数据详情和场次数据 ==========", flush=True)
    cache_root = output / "cache" / "summaries"
    results: list[dict[str, Any]] = []
    pending = []
    for anchor in anchors:
        cache_path = cache_root / f"{safe_name(anchor['anchor_id'])}.json"
        cached = valid_cache(cache_path, SUMMARY_CACHE_VERSION, args.date.isoformat())
        if cached:
            if sync_cached_anchor(cached, anchor, "sessions"):
                write_json(cache_path, cached)
            results.append(cached)
        else:
            pending.append(anchor)
    print(f"有效缓存: {len(results)}，待获取场次: {len(pending)}", flush=True)
    print("正在并行读取日趋势和场次数据...", flush=True)
    started = time.monotonic()
    daily_details, daily_failures, fetched_rooms, room_failures = api.daily_details_and_rooms(
        anchors, pending, args.date
    )

    daily = add_backend(
        [daily_row(anchor, args.date.isoformat(), daily_details.get(anchor["anchor_id"])) for anchor in anchors],
        args.backend_name,
    )
    daily.sort(key=lambda row: int(row["当日音浪排名"]))
    write_csv(output / "日汇总.csv", daily)
    daily_errors = [
        anchor_error(anchor, daily_failures[anchor["anchor_id"]], "日数据")
        for anchor in anchors
        if anchor["anchor_id"] in daily_failures
    ]
    write_csv(output / "日数据失败记录.csv", daily_errors)
    print(f"日数据已保存: {len(daily) - len(daily_errors)}/{len(daily)} 名主播。", flush=True)

    errors: list[dict[str, Any]] = []
    denied: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = [
        anchor_error(result["anchor"], "后台有当日日数据，但未找到与当天重叠的场次", "场次缺失")
        for result in results
        if not result.get("minute_rooms")
    ]
    anchors_by_id = {anchor["anchor_id"]: anchor for anchor in pending}
    completed = 0

    def save_summary(anchor_id: str, rooms: list[dict[str, Any]] | None, message: str) -> None:
        nonlocal completed
        anchor = anchors_by_id[anchor_id]
        if message:
            row = anchor_error(anchor, message, "场次")
            (denied if inaccessible(message) else errors).append(row)
        else:
            day_rooms, minute_rooms = split_rooms(rooms or [], args.date)
            if not minute_rooms:
                missing.append(anchor_error(anchor, "后台有当日日数据，但未找到与当天重叠的场次", "场次缺失"))
            result = {
                "version": SUMMARY_CACHE_VERSION,
                "date": args.date.isoformat(),
                "anchor": anchor,
                "minute_rooms": minute_rooms,
                "sessions": [session_row(anchor, room) for room in day_rooms],
            }
            write_json(cache_root / f"{safe_name(anchor_id)}.json", result)
            results.append(result)
        completed += 1
        show_progress("场次/日进度", completed, len(pending), started)

    for anchor in pending:
        anchor_id = anchor["anchor_id"]
        message = room_failures.get(anchor_id, "")
        rooms = fetched_rooms.get(anchor_id)
        if rooms is None and not message:
            message = "场次接口无返回"
        save_summary(anchor_id, rooms, message)

    sessions = add_backend([row for result in results for row in result["sessions"]], args.backend_name)
    sessions.sort(key=lambda row: (int(row["当日音浪排名"]), row.get("开播时间", "")))
    errors.sort(key=lambda row: int(row["当日音浪排名"]))
    denied.sort(key=lambda row: int(row["当日音浪排名"]))
    missing.sort(key=lambda row: int(row["当日音浪排名"]))
    write_csv(output / "场次汇总.csv", sessions)
    write_csv(output / "场次失败记录.csv", errors)
    write_csv(output / "场次缺失记录.csv", missing)
    write_csv(output / "无权限记录.csv", denied)
    print(
        f"\n*** {args.backend_name} 日数据 {len(daily)} 人；目标日开播场次 {len(sessions)} 个；"
        f"场次接口失败 {len(errors)} 人，场次缺失 {len(missing)} 人 ***\n",
        flush=True,
    )
    return results, daily_errors, errors, denied, missing


def load_summaries(args: argparse.Namespace, anchors: list[dict[str, Any]], output: Path) -> list[dict[str, Any]]:
    cache_root = output / "cache" / "summaries"
    results = []
    for anchor in anchors:
        cache_path = cache_root / f"{safe_name(anchor['anchor_id'])}.json"
        cached = valid_cache(cache_path, SUMMARY_CACHE_VERSION, args.date.isoformat())
        if cached:
            if sync_cached_anchor(cached, anchor, "sessions"):
                write_json(cache_path, cached)
            results.append(cached)
    if not results:
        raise RuntimeError("没有可用的场次缓存，请先完成 summary 阶段。")
    return results


def minute_phase(
    api: UnionApi,
    args: argparse.Namespace,
    summaries: list[dict[str, Any]],
    output: Path,
    data_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    print("\n========== 阶段 2/2：分钟数据和小时数据 ==========", flush=True)
    cache_root = output / "cache" / "minutes"
    results: list[dict[str, Any]] = []
    pending = []
    for summary in summaries:
        anchor = summary["anchor"]
        if not summary.get("minute_rooms"):
            continue
        minute_path = data_root / f"anchor_id={safe_name(anchor['anchor_id'])}" / "minutes.parquet"
        cached = valid_cache(
            cache_root / f"{safe_name(anchor['anchor_id'])}.json",
            MINUTE_CACHE_VERSION,
            args.date.isoformat(),
            minute_path,
        )
        expected_room_ids = sorted(str(room["roomID"]) for room in summary["minute_rooms"])
        if cached and cached.get("room_ids") != expected_room_ids:
            cached = None
        if cached:
            if sync_cached_anchor(cached, anchor, "hourly"):
                update_minute_parquet_anchor(minute_path, anchor_columns(anchor))
                write_json(cache_root / f"{safe_name(anchor['anchor_id'])}.json", cached)
            results.append(cached)
        else:
            pending.append(summary)
    print(f"有效缓存: {len(results)}，待获取分钟: {len(pending)}", flush=True)

    errors: list[dict[str, Any]] = []
    summaries_by_room = {
        str(room["roomID"]): summary
        for summary in pending
        for room in summary["minute_rooms"]
    }
    room_payloads: dict[str, dict[str, Any]] = {}
    room_errors: dict[str, str] = {}
    remaining = {
        summary["anchor"]["anchor_id"]: len(summary["minute_rooms"])
        for summary in pending
    }
    started = time.monotonic()
    completed = 0

    def save_minutes(room_id: str, payload: dict[str, Any] | None, message: str) -> None:
        nonlocal completed
        summary = summaries_by_room[room_id]
        anchor = summary["anchor"]
        anchor_id = anchor["anchor_id"]
        if message or payload is None:
            room_errors[room_id] = message or "无返回"
        else:
            room_payloads[room_id] = payload
        remaining[anchor_id] -= 1
        if remaining[anchor_id]:
            return

        missing_rooms = [
            str(room["roomID"])
            for room in summary["minute_rooms"]
            if str(room["roomID"]) not in room_payloads
        ]
        if missing_rooms:
            details = "; ".join(f"{item}: {room_errors.get(item, '无返回')}" for item in missing_rooms[:3])
            errors.append(anchor_error(anchor, f"分钟接口失败 {len(missing_rooms)} 场: {details}", "分钟/小时"))
        else:
            minutes = [
                row
                for room in summary["minute_rooms"]
                for row in minute_rows(anchor, room, room_payloads[str(room["roomID"])], args.date.isoformat())
            ]
            if not minutes:
                errors.append(anchor_error(anchor, "分钟接口返回空数据", "分钟/小时"))
            else:
                minute_path = data_root / f"anchor_id={safe_name(anchor_id)}" / "minutes.parquet"
                write_minute_parquet(minute_path, minutes)
                result = {
                    "version": MINUTE_CACHE_VERSION,
                    "date": args.date.isoformat(),
                    "anchor": anchor,
                    "room_ids": sorted(str(room["roomID"]) for room in summary["minute_rooms"]),
                    "hourly": aggregate_hourly(minutes),
                    "minute_count": len(minutes),
                }
                write_json(cache_root / f"{safe_name(anchor_id)}.json", result)
                results.append(result)
        completed += 1
        show_progress("分钟/小时进度", completed, len(pending), started)

    rooms = [room for summary in pending for room in summary["minute_rooms"]]
    api.minutes(rooms, args.minute_interval_ms, on_complete=save_minutes)

    hourly = add_backend([row for result in results for row in result["hourly"]], args.backend_name)
    hourly.sort(key=lambda row: (int(row["当日音浪排名"]), row.get("小时开始", "")))
    errors.sort(key=lambda row: int(row["当日音浪排名"]))
    write_csv(output / "小时汇总.csv", hourly)
    write_csv(output / "分钟小时失败记录.csv", errors)
    print(
        f"\n*** {args.backend_name} 分钟和小时数据已完成：{len(results)} 名主播，"
        f"{sum(int(result['minute_count']) for result in results)} 条分钟记录 ***\n",
        flush=True,
    )
    return results, errors


def main() -> int:
    prevent_console_pause()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    args = parse_args()
    if args.minute_interval_ms < 0:
        raise SystemExit("分钟请求间隔不能小于 0。")

    day_text = args.date.isoformat()
    output = PROJECT_ROOT / "output" / args.backend_name / day_text
    data_root = PROJECT_ROOT / "data" / args.backend_name / day_text
    output.mkdir(parents=True, exist_ok=True)
    previous_manifest = read_json(output / "manifest.json") or {}
    summary_errors: list[dict[str, Any]] = []
    daily_errors: list[dict[str, Any]] = []
    minute_errors: list[dict[str, Any]] = []
    denied: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    minutes: list[dict[str, Any]] = []

    with UnionApi(cdp_url_for(args.backend_name)) as api:
        anchors = load_anchors(api, args, output)
        if args.phase in ("summary", "all"):
            summaries, daily_errors, summary_errors, denied, missing = summary_phase(
                api, args, anchors, output
            )
        else:
            summaries = load_summaries(args, anchors, output)
        if args.phase in ("minutes", "all"):
            minutes, minute_errors = minute_phase(api, args, summaries, output, data_root)

    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "backend": args.backend_name,
        "date": day_text,
        "active_anchor_count": len(anchors),
        "daily_row_count": len(anchors),
        "daily_failed_anchor_count": (
            len(daily_errors)
            if args.phase in ("summary", "all")
            else int(previous_manifest.get("daily_failed_anchor_count") or 0)
        ),
        "session_completed_anchor_count": len(summaries),
        "session_failed_anchor_count": (
            len(summary_errors)
            if args.phase in ("summary", "all")
            else int(previous_manifest.get("session_failed_anchor_count") or 0)
        ),
        "session_missing_anchor_count": (
            len(missing)
            if args.phase in ("summary", "all")
            else int(previous_manifest.get("session_missing_anchor_count") or 0)
        ),
        "inaccessible_anchor_count": (
            len(denied)
            if args.phase in ("summary", "all")
            else int(previous_manifest.get("inaccessible_anchor_count") or 0)
        ),
        "minute_completed_anchor_count": len(minutes),
        "minute_failed_anchor_count": len(minute_errors),
        "session_count": sum(len(result["sessions"]) for result in summaries),
        "minute_row_count": sum(int(result["minute_count"]) for result in minutes),
    }
    write_json(output / "manifest.json", manifest)
    (output / "run_error.log").unlink(missing_ok=True)
    print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)
    return 2 if daily_errors or summary_errors or minute_errors else 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception:
        details = traceback.format_exc()
        try:
            failed_args = parse_args()
            log = PROJECT_ROOT / "output" / failed_args.backend_name / failed_args.date.isoformat() / "run_error.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(details, encoding="utf-8")
        except Exception:
            pass
        raise
    raise SystemExit(exit_code)
