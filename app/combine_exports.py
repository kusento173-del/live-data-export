from __future__ import annotations

import argparse
import calendar
import csv
from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook

from .api import PROJECT_ROOT
BACKENDS = ("新心", "中鼎")
BACKEND_ORDER = {name: index for index, name in enumerate(BACKENDS)}


def month_value(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m").date().replace(day=1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("月份格式必须是 YYYY-MM") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="合并新心和中鼎指定月份的每日导出。")
    parser.add_argument("--month", type=month_value, default=date.today().replace(day=1))
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def daily_directories(root: Path, backend: str, begin: date, end: date) -> list[Path]:
    backend_root = root / backend
    if not backend_root.is_dir():
        return []
    directories = []
    for path in backend_root.iterdir():
        if not path.is_dir():
            continue
        if len(path.name) != 10 or path.name[4] != "-" or path.name[7] != "-":
            continue
        try:
            current = date.fromisoformat(path.name)
        except ValueError:
            continue
        if begin <= current <= end:
            directories.append(path)
    return sorted(directories)


def with_backend(name: str, row: dict[str, Any]) -> dict[str, Any]:
    return {"后台": name, **{key: value for key, value in row.items() if key != "后台"}}


def deduplicate(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        unique[tuple(str(row.get(key, "")) for key in keys)] = row
    return list(unique.values())


def rank_value(row: dict[str, Any]) -> int:
    try:
        return int(row.get("当日音浪排名") or 999999)
    except (TypeError, ValueError):
        return 999999


def backend_value(row: dict[str, Any]) -> int:
    return BACKEND_ORDER.get(str(row.get("后台", "")), len(BACKEND_ORDER))


def write_workbook(
    path: Path,
    daily: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
    hourly: list[dict[str, Any]],
) -> None:
    workbook = Workbook()
    workbook.remove(workbook.active)
    for title, rows in (("月日汇总", daily), ("月场次汇总", sessions), ("月小时汇总", hourly)):
        sheet = workbook.create_sheet(title)
        if not rows:
            sheet.append(["无数据"])
            continue
        headers = list(rows[0])
        sheet.append(headers)
        for row in rows:
            sheet.append([row.get(header, "") for header in headers])
    temporary = path.with_suffix(".tmp.xlsx")
    try:
        workbook.save(temporary)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    args = parse_args()
    begin = args.month
    end = date(begin.year, begin.month, calendar.monthrange(begin.year, begin.month)[1])
    output_root = PROJECT_ROOT / "output"

    daily: list[dict[str, Any]] = []
    sessions: list[dict[str, Any]] = []
    hourly: list[dict[str, Any]] = []
    source_count = 0

    for backend in BACKENDS:
        for directory in daily_directories(output_root, backend, begin, end):
            source_count += 1
            daily.extend(with_backend(backend, row) for row in read_csv(directory / "日汇总.csv"))
            sessions.extend(with_backend(backend, row) for row in read_csv(directory / "场次汇总.csv"))
            hourly.extend(with_backend(backend, row) for row in read_csv(directory / "小时汇总.csv"))

    daily = deduplicate(daily, ("后台", "统计日期", "主播ID"))
    sessions = deduplicate(sessions, ("后台", "直播间ID"))
    hourly = deduplicate(hourly, ("后台", "小时开始", "主播ID", "直播间ID"))
    daily.sort(
        key=lambda row: (
            row.get("统计日期", ""),
            backend_value(row),
            rank_value(row),
            row.get("主播ID", ""),
        )
    )
    sessions.sort(
        key=lambda row: (
            str(row.get("开播时间", ""))[:10],
            backend_value(row),
            rank_value(row),
            row.get("主播ID", ""),
            row.get("开播时间", ""),
        )
    )
    hourly.sort(
        key=lambda row: (
            row.get("统计日期", ""),
            backend_value(row),
            rank_value(row),
            row.get("主播ID", ""),
            row.get("小时开始", ""),
        )
    )

    summary_dir = output_root / "汇总"
    summary_dir.mkdir(parents=True, exist_ok=True)
    workbook_path = summary_dir / f"{begin.year}年{begin.month:02d}月直播数据汇总.xlsx"
    write_workbook(workbook_path, daily, sessions, hourly)

    print(f"每日目录: {source_count}")
    print(f"月日汇总: {len(daily)}，月场次汇总: {len(sessions)}，月小时汇总: {len(hourly)}")
    print(f"已生成: {workbook_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
