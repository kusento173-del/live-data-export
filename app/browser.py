from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from playwright.sync_api import Page, sync_playwright


REQUEST_TIMEOUT_MS = 60_000
RETRY_COOLDOWNS = (20,) * 7
CompleteCallback = Callable[[str, dict[str, Any] | None, str], None]


@dataclass(frozen=True)
class RequestBatch:
    path: str
    items: list[dict[str, Any]]
    interval_ms: int = 0
    on_complete: CompleteCallback | None = None


class BrowserClient:
    def __init__(self, cdp_url: str):
        self.cdp_url = cdp_url
        self.playwright: Any = None
        self.browser: Any = None
        self.page: Page | None = None

    def __enter__(self) -> BrowserClient:
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.connect_over_cdp(self.cdp_url)
        pages = [page for context in self.browser.contexts for page in context.pages]
        self.page = next((page for page in pages if "union.bytedance.com/open/portal" in page.url), None)
        if self.page is None:
            self.__exit__(None, None, None)
            raise RuntimeError("没有找到已登录的抖音公会后台页面。")
        return self

    def __exit__(self, *_: Any) -> None:
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()

    def request_many(
        self,
        path: str,
        items: list[dict[str, Any]],
        interval_ms: int = 0,
        cooldowns: tuple[int, ...] = RETRY_COOLDOWNS,
        on_complete: CompleteCallback | None = None,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        return self.request_batches(
            {"request": RequestBatch(path, items, interval_ms, on_complete)},
            cooldowns,
        )["request"]

    def request_batches(
        self,
        batches: dict[str, RequestBatch],
        cooldowns: tuple[int, ...] = RETRY_COOLDOWNS,
    ) -> dict[str, tuple[dict[str, dict[str, Any]], dict[str, str]]]:
        pending = {
            name: {str(item["key"]): item for item in batch.items}
            for name, batch in batches.items()
        }
        successes: dict[str, dict[str, dict[str, Any]]] = {name: {} for name in batches}
        errors: dict[str, dict[str, str]] = {name: {} for name in batches}
        totals = {name: len(batch.items) for name, batch in batches.items()}

        def progress(name: str) -> None:
            if len(batches) == 1:
                return
            done = len(successes[name]) + len(errors[name])
            if done == totals[name] or done % 10 == 0:
                print(f"并行进度 | {name}: {done}/{totals[name]}", flush=True)

        for round_index in range(len(cooldowns) + 1):
            retry: dict[str, dict[str, dict[str, Any]]] = {name: {} for name in batches}
            jobs = {
                name: self._start_job(batch, list(pending[name].values()))
                for name, batch in batches.items()
                if pending[name]
            }
            for name, result in self._stream_jobs(jobs):
                key = str(result["key"])
                payload, message = self._decode(result)
                batch = batches[name]
                if payload is not None:
                    successes[name][key] = payload
                    if batch.on_complete:
                        batch.on_complete(key, payload, "")
                    progress(name)
                elif self._transient(message) and round_index < len(cooldowns):
                    retry[name][key] = pending[name][key]
                else:
                    errors[name][key] = message or "接口请求失败"
                    if batch.on_complete:
                        batch.on_complete(key, None, errors[name][key])
                    progress(name)

            retry_count = sum(len(items) for items in retry.values())
            if not retry_count:
                break
            delay = cooldowns[round_index]
            detail = "，".join(f"{name} {len(items)}" for name, items in retry.items() if items)
            print(f"{detail} 连续失败 {round_index + 1} 轮，{delay} 秒后重试...", flush=True)
            time.sleep(delay)
            pending = retry

        return {name: (successes[name], errors[name]) for name in batches}

    @staticmethod
    def _decode(result: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        message = result.get("error", "")
        if message:
            return None, message
        try:
            payload = json.loads(result["text"])
            if payload.get("status_code", 0) != 0 or payload.get("code", 0) != 0:
                raise RuntimeError(payload.get("message") or str(payload))
            return payload, ""
        except (json.JSONDecodeError, RuntimeError) as exc:
            return None, str(exc)

    def _start_job(self, batch: RequestBatch, items: list[dict[str, Any]]) -> str:
        assert self.page is not None
        job_id = f"union-export-{time.time_ns()}"
        self.page.evaluate(
            """({jobId, path, items, intervalMs, requestTimeoutMs}) => {
                window.__unionExportJobs ??= {};
                const job = {results: [], done: false};
                window.__unionExportJobs[jobId] = job;
                (async () => {
                    let cursor = 0;
                    let nextStart = 0;
                    const worker = async () => {
                        while (true) {
                            const index = cursor++;
                            if (index >= items.length) return;
                            const item = items[index];
                            let lastError = "";
                            let completed = false;
                            for (let attempt = 0; attempt < 3; attempt++) {
                                if (attempt) await new Promise(r => setTimeout(r, [1000, 3000][attempt - 1]));
                                let timeout;
                                try {
                                    const now = Date.now();
                                    const wait = Math.max(0, nextStart - now);
                                    nextStart = Math.max(nextStart, now) + intervalMs;
                                    if (wait) await new Promise(r => setTimeout(r, wait));
                                    const controller = new AbortController();
                                    timeout = setTimeout(() => controller.abort(), requestTimeoutMs);
                                    const query = new URLSearchParams();
                                    for (const [key, value] of Object.entries(item.params)) {
                                        if (Array.isArray(value)) value.forEach(v => query.append(key, String(v)));
                                        else query.set(key, String(value));
                                    }
                                    const response = await fetch(`${path}?${query}`, {
                                        credentials: "include",
                                        signal: controller.signal,
                                    });
                                    const text = await response.text();
                                    const logId = response.headers.get("x-tt-logid") || "";
                                    if (response.ok && text.trim()) {
                                        job.results.push({key: item.key, text});
                                        completed = true;
                                        break;
                                    }
                                    lastError = response.ok
                                        ? `empty response body${logId ? `; logid=${logId}` : ""}`
                                        : `HTTP ${response.status}: ${text.slice(0, 300)}${logId ? `; logid=${logId}` : ""}`;
                                    if (![429, 500, 502, 503, 504].includes(response.status)) break;
                                } catch (error) {
                                    lastError = String(error);
                                } finally {
                                    if (timeout) clearTimeout(timeout);
                                }
                            }
                            if (!completed) job.results.push({key: item.key, error: lastError || "request failed"});
                        }
                    };
                    await Promise.all(Array.from({length: items.length}, worker));
                    job.done = true;
                })();
            }""",
            {
                "jobId": job_id,
                "path": batch.path,
                "items": items,
                "intervalMs": max(0, batch.interval_ms),
                "requestTimeoutMs": REQUEST_TIMEOUT_MS,
            },
        )
        return job_id

    def _stream_jobs(self, jobs: dict[str, str]) -> Iterator[tuple[str, dict[str, Any]]]:
        assert self.page is not None
        active = dict(jobs)
        while active:
            states = self.page.evaluate(
                """jobIds => Object.fromEntries(jobIds.map(jobId => {
                    const job = window.__unionExportJobs?.[jobId];
                    if (!job) return [jobId, null];
                    const results = job.results.splice(0, 20);
                    const done = job.done && job.results.length === 0;
                    if (done) delete window.__unionExportJobs[jobId];
                    return [jobId, {results, done}];
                }))""",
                list(active.values()),
            )
            for name, job_id in list(active.items()):
                state = states.get(job_id)
                if state is None:
                    raise RuntimeError("浏览器任务队列意外丢失，请保持后台页面开启并重新运行。")
                for result in state["results"]:
                    yield name, result
                if state["done"]:
                    del active[name]
            if active:
                time.sleep(0.05)

    @staticmethod
    def _transient(message: str) -> bool:
        text = message.lower()
        return any(
            marker in text
            for marker in (
                "限频",
                "服务器开小差",
                "系统繁忙",
                "稍后重试",
                "接口未返回",
                "empty response",
                "expecting value",
                "429",
                "timeout",
                "timed out",
                "abort",
                "failed to fetch",
                "http 5",
                "request failed",
            )
        )
