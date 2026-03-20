"""
Turnstile Solver API 服务

基于 Camoufox（防指纹 Firefox）的本地 Turnstile 验证码解决方案。
通过在真实浏览器中直接注入 Turnstile widget 并等待自动校验来获取 token。

API:
  GET /turnstile?url=<url>&sitekey=<sitekey>  →  创建解题任务
  GET /result?task_id=<task_id>                →  获取解题结果
  GET /                                        →  健康检查

启动: python api_solver.py --browser_type camoufox --thread 3
"""
import os
import sys
import time
import uuid
import random
import logging
import asyncio
import secrets
from typing import Any, Optional
from collections import deque
import argparse
from quart import Quart, request, jsonify
from camoufox.async_api import AsyncCamoufox
from db_results import init_db, save_result, load_result, delete_result, cleanup_old_results, get_stats
from browser_configs import browser_config
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.align import Align
from rich import box


COLORS = {
    'MAGENTA': '\033[35m', 'BLUE': '\033[34m', 'GREEN': '\033[32m',
    'YELLOW': '\033[33m', 'RED': '\033[31m', 'RESET': '\033[0m',
}


class CustomLogger(logging.Logger):
    """带颜色和时间戳的自定义日志"""
    @staticmethod
    def _fmt(level, color, message):
        ts = time.strftime('%H:%M:%S')
        return f"[{ts}] [{COLORS[color]}{level}{COLORS['RESET']}] -> {message}"

    def debug(self, msg, *a, **kw):
        super().debug(self._fmt('DEBUG', 'MAGENTA', msg), *a, **kw)

    def info(self, msg, *a, **kw):
        super().info(self._fmt('INFO', 'BLUE', msg), *a, **kw)

    def success(self, msg, *a, **kw):
        super().info(self._fmt('SUCCESS', 'GREEN', msg), *a, **kw)

    def warning(self, msg, *a, **kw):
        super().warning(self._fmt('WARNING', 'YELLOW', msg), *a, **kw)

    def error(self, msg, *a, **kw):
        super().error(self._fmt('ERROR', 'RED', msg), *a, **kw)


logging.setLoggerClass(CustomLogger)
logger: CustomLogger = logging.getLogger("TurnstileSolver")  # type: ignore
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler(sys.stdout)
logger.addHandler(handler)


# 浏览器实例达到软阈值后不再继续复用持久页面，优先做轻量换血
BROWSER_SOFT_RECYCLE_THRESHOLD = 10
# 浏览器实例超过此次数后自动重启（释放长时间运行累积的污染/泄漏）
BROWSER_RESTART_THRESHOLD = 18
# 持久页面连续复用上限，超过后强制创建干净上下文
PAGE_REUSE_LIMIT = 4
# 连续失败超过此次数后，强制重启浏览器（绕过 Cloudflare 风控标记）
CONSECUTIVE_FAIL_RESTART = 2
# 失败后冷却等待范围（秒），避免被风控快速标记
FAIL_COOLDOWN_RANGE = (2, 5)
# 浏览器重启后的恢复观察期
RECOVERING_GRACE_SECONDS = 12
# 最近结果窗口大小，用于健康分计算
RESULT_WINDOW_SIZE = 40
# 统计近期重启风暴的时间窗口
RESTART_EVENT_WINDOW_SECONDS = 900
# 获取浏览器的最长等待时间（秒），避免任务无限排队
BROWSER_ACQUIRE_TIMEOUT = 10


def _read_env_float(name: str, default: float) -> float:
    raw = str(os.getenv(name, "")).strip()
    try:
        return float(raw) if raw else float(default)
    except Exception:
        return float(default)


DEFAULT_NODE_ID = os.getenv("SOLVER_NODE_ID", "").strip()
DEFAULT_NODE_LABEL = os.getenv("SOLVER_NODE_LABEL", os.getenv("HOSTNAME", "")).strip()
SOLVER_ADMIN_TOKEN = os.getenv("SOLVER_ADMIN_TOKEN", "").strip()
SOLVER_REINIT_COOLDOWN_SECONDS = max(_read_env_float("SOLVER_REINIT_COOLDOWN_SECONDS", 45.0), 1.0)


class TurnstileAPIServer:

    def __init__(self, headless: bool, debug: bool, browser_type: str, thread: int):
        self.app = Quart(__name__)
        self.debug = debug
        self.browser_type = browser_type
        self.headless = headless
        self.thread_count = thread
        self.node_id = DEFAULT_NODE_ID or f"solver-{uuid.uuid4().hex[:8]}"
        self.node_label = DEFAULT_NODE_LABEL or self.node_id
        self.boot_id = uuid.uuid4().hex
        self.started_at = round(time.time(), 3)
        self._admin_token = SOLVER_ADMIN_TOKEN
        self.reinit_cooldown_seconds = SOLVER_REINIT_COOLDOWN_SECONDS
        # 浏览器池元素格式: (index, browser, config, task_count)
        self.browser_pool = asyncio.Queue()
        # 保存 Camoufox 管理器引用，用于创建新浏览器
        self._camoufox_manager = None
        self._pw_manager = None
        # 每个浏览器的连续失败计数 {index: fail_count}
        self._consecutive_fails = {}
        # 持久页面缓存 {index: (context, page)}，避免每次重新加载
        self._persistent_pages = {}
        # 浏览器运行态：记录连续失败、页面复用次数、恢复期等
        self._browser_runtime = {}
        self._recent_results = deque(maxlen=RESULT_WINDOW_SIZE)
        self._restart_events = deque(maxlen=RESULT_WINDOW_SIZE)
        self.console = Console()
        self._task_states = {}
        self._admission_lock = asyncio.Lock()
        self._reserved_slots = 0
        self._reinit_lock = asyncio.Lock()
        self.init_count = 0
        self.last_init_at = 0.0
        self.last_init_reason = ""
        self.last_init_result = ""
        self.last_reinit_requested_by = ""
        self.last_reinit_source = ""
        self.last_reinit_request_at = 0.0
        self.last_reinit_finished_at = 0.0
        self.last_reinit_error = ""
        self.last_reinit_status = ""
        self.last_reinit_message = ""
        self.reinit_in_progress = False
        self.reinit_cooldown_until = 0.0
        self._setup_routes()

    def display_welcome(self):
        """显示欢迎界面"""
        self.console.clear()
        text = Text()
        text.append("\n🔧 Turnstile Solver 服务", style="bold white")
        text.append(f"\n🆔 节点编号: ", style="bold white")
        text.append(f"{self.node_id}", style="green")
        text.append(f"\n🏷️ 节点标签: ", style="bold white")
        text.append(f"{self.node_label}", style="cyan")
        text.append(f"\n🧵 浏览器线程: ", style="bold white")
        text.append(f"{self.thread_count}", style="green")
        text.append(f"\n🌐 浏览器类型: ", style="bold white")
        text.append(f"{self.browser_type}", style="cyan")
        text.append(f"\n👻 无头模式: ", style="bold white")
        text.append(f"{'是' if self.headless else '否'}", style="yellow")
        text.append("\n")

        panel = Panel(
            Align.left(text),
            title="[bold blue]Turnstile Solver[/bold blue]",
            subtitle="[bold magenta]Grok 注册机专用[/bold magenta]",
            box=box.ROUNDED, border_style="bright_blue",
            padding=(0, 1), width=50
        )
        self.console.print(panel)
        self.console.print()

    def _setup_routes(self):
        """注册 HTTP 路由"""
        self.app.before_serving(self._startup)
        self.app.route('/turnstile', methods=['GET'])(self.process_turnstile)
        self.app.route('/result', methods=['GET'])(self.get_result)
        self.app.route('/stats', methods=['GET'])(self.stats)
        self.app.route('/admin/reinitialize', methods=['POST'])(self.admin_reinitialize)
        self.app.route('/')(self.index)

    def _effective_available_browsers(self) -> int:
        return max(self.browser_pool.qsize() - self._reserved_slots, 0)

    def _reinit_cooldown_remaining(self) -> float:
        return max(self.reinit_cooldown_until - time.time(), 0.0)

    def _build_node_runtime_snapshot(self) -> dict[str, Any]:
        now = time.time()
        return {
            "node_id": self.node_id,
            "node_label": self.node_label,
            "boot_id": self.boot_id,
            "started_at": self.started_at,
            "uptime_seconds": round(max(now - self.started_at, 0.0), 3),
            "init_count": self.init_count,
            "last_init_at": round(self.last_init_at, 3) if self.last_init_at else 0.0,
            "last_init_reason": self.last_init_reason,
            "last_init_result": self.last_init_result,
            "last_reinit_requested_by": self.last_reinit_requested_by,
            "last_reinit_source": self.last_reinit_source,
            "last_reinit_request_at": round(self.last_reinit_request_at, 3) if self.last_reinit_request_at else 0.0,
            "last_reinit_finished_at": round(self.last_reinit_finished_at, 3) if self.last_reinit_finished_at else 0.0,
            "last_reinit_error": self.last_reinit_error,
            "last_reinit_status": self.last_reinit_status,
            "last_reinit_message": self.last_reinit_message,
            "reinit_in_progress": self.reinit_in_progress,
            "reinit_cooldown_until": round(self.reinit_cooldown_until, 3) if self.reinit_cooldown_until else 0.0,
            "reinit_cooldown_remaining": round(self._reinit_cooldown_remaining(), 3),
            "reinit_supported": bool(self._admin_token),
        }

    def _extract_admin_token(self) -> str:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header.split(" ", 1)[1].strip()
        return str(request.headers.get("X-Solver-Admin-Token", "")).strip()

    def _is_admin_authorized(self) -> bool:
        token = self._extract_admin_token()
        if not self._admin_token or not token:
            return False
        return secrets.compare_digest(token, self._admin_token)

    async def _close_browser_instance(self, browser):
        try:
            await browser.close()
        except Exception:
            pass

    async def _drain_idle_browser_pool(self) -> list[int]:
        drained_indexes: list[int] = []
        while True:
            try:
                index, browser, _config, _task_count = self.browser_pool.get_nowait()
            except asyncio.QueueEmpty:
                break
            await self._cleanup_persistent_page(index)
            await self._close_browser_instance(browser)
            drained_indexes.append(index)
        return drained_indexes

    async def _reset_solver_runtime(self):
        for index in list(self._persistent_pages.keys()):
            await self._cleanup_persistent_page(index)
        self._persistent_pages.clear()
        self._recent_results.clear()
        self._restart_events.clear()
        self._consecutive_fails.clear()
        self._browser_runtime = {}
        async with self._admission_lock:
            self._reserved_slots = 0

    async def _create_browser_entry(self, index: int) -> tuple:
        if self.browser_type == "camoufox":
            if self._camoufox_manager is None:
                self._camoufox_manager = AsyncCamoufox(headless=self.headless)
            browser = await self._camoufox_manager.start()
            config = {
                'browser_name': 'camoufox',
                'browser_version': 'latest',
                'useragent': None,
                'sec_ch_ua': None,
            }
        else:
            if self._pw_manager is None:
                from patchright.async_api import async_playwright
                self._pw_manager = await async_playwright().start()
            _, ver, ua, sec_ch_ua = browser_config.get_random_browser_config(self.browser_type)
            browser = await self._pw_manager.chromium.launch(
                channel=self.browser_type,
                headless=self.headless,
                args=["--window-position=0,0", "--force-device-scale-factor=1", f"--user-agent={ua}"]
            )
            config = {
                'browser_name': 'chrome',
                'browser_version': ver,
                'useragent': ua,
                'sec_ch_ua': sec_ch_ua,
            }
        runtime = self._get_browser_runtime(index)
        runtime["task_count"] = 0
        return (index, browser, config, 0)

    async def _reinitialize_solver(self, reason: str, requested_by: str, source: str) -> dict[str, Any]:
        async with self._reinit_lock:
            if self.reinit_in_progress:
                return {
                    "ok": False,
                    "accepted": False,
                    "message": "reinitialize_already_running",
                    **self._build_node_runtime_snapshot(),
                }

            cooldown_remaining = self._reinit_cooldown_remaining()
            if cooldown_remaining > 0:
                self.last_reinit_status = "cooldown"
                self.last_reinit_message = "reinitialize_cooldown"
                return {
                    "ok": False,
                    "accepted": False,
                    "message": "reinitialize_cooldown",
                    "cooldown_remaining": round(cooldown_remaining, 3),
                    **self._build_node_runtime_snapshot(),
                }

            self.reinit_in_progress = True
            self.last_reinit_requested_by = requested_by
            self.last_reinit_source = source
            self.last_reinit_request_at = time.time()
            self.last_reinit_finished_at = 0.0
            self.last_reinit_error = ""
            self.last_reinit_status = "running"
            self.last_reinit_message = reason or "remote_reinitialize"
            self.last_init_reason = reason or "remote_reinitialize"
            self.last_init_result = "running"
            logger.warning(f"节点 {self.node_id}: 收到远程软初始化请求，原因={reason or 'remote_reinitialize'}，来源={requested_by or 'unknown'}")

            try:
                drained_indexes = await self._drain_idle_browser_pool()
                if not drained_indexes:
                    self.reinit_in_progress = False
                    self.last_reinit_finished_at = time.time()
                    self.last_reinit_status = "skipped"
                    self.last_reinit_message = "no_idle_browsers_to_reinitialize"
                    self.last_init_result = "skipped"
                    return {
                        "ok": False,
                        "accepted": False,
                        "message": "no_idle_browsers_to_reinitialize",
                        **self._build_node_runtime_snapshot(),
                    }

                await self._reset_solver_runtime()
                await self._initialize_browser(indices=drained_indexes)
                self.init_count += 1
                self.last_init_at = time.time()
                self.last_init_result = "success"
                self.last_reinit_finished_at = self.last_init_at
                self.last_reinit_status = "success"
                self.last_reinit_message = f"reinitialized_{len(drained_indexes)}_browsers"
                self.reinit_cooldown_until = time.time() + self.reinit_cooldown_seconds
                logger.success(f"节点 {self.node_id}: 软初始化成功，重建 {len(drained_indexes)} 个空闲浏览器实例")
                return {
                    "ok": True,
                    "accepted": True,
                    "message": self.last_reinit_message,
                    "reinitialized_browsers": len(drained_indexes),
                    **self._build_node_runtime_snapshot(),
                }
            except Exception as e:
                self.last_reinit_error = str(e)
                self.last_reinit_finished_at = time.time()
                self.last_reinit_status = "failed"
                self.last_reinit_message = "reinitialize_failed"
                self.last_init_at = self.last_reinit_finished_at
                self.last_init_result = "failed"
                self.reinit_cooldown_until = time.time() + self.reinit_cooldown_seconds
                logger.error(f"节点 {self.node_id}: 软初始化失败: {e}")
                return {
                    "ok": False,
                    "accepted": False,
                    "message": "reinitialize_failed",
                    "error": str(e),
                    **self._build_node_runtime_snapshot(),
                }
            finally:
                self.reinit_in_progress = False

    def _set_task_state(self, task_id: str, status: str, stage: str, message: str = "", **extra):
        now = time.time()
        state = self._task_states.get(task_id, {})
        created_at = state.get("created_at", now)
        state.update({
            "task_id": task_id,
            "status": status,
            "stage": stage,
            "message": message,
            "created_at": created_at,
            "updated_at": now,
            "solution": None,
        })
        for key, value in extra.items():
            if value is not None:
                state[key] = value
        if extra.get("elapsed_time") is None:
            state["elapsed_time"] = round(max(now - created_at, 0), 3)
        self._task_states[task_id] = state

    def _build_task_state_payload(self, task_id: str):
        state = self._task_states.get(task_id)
        if not state:
            return None
        payload = dict(state)
        created_at = payload.get("created_at")
        if created_at and payload.get("status") not in {"completed", "failed"}:
            payload["elapsed_time"] = round(max(time.time() - created_at, 0), 3)
        payload.setdefault("solution", None)
        return payload

    def _get_browser_runtime(self, index: int) -> dict:
        runtime = self._browser_runtime.get(index)
        if runtime is None:
            runtime = {
                "task_count": 0,
                "page_reuse_count": 0,
                "consecutive_failures": 0,
                "recent_failures": 0,
                "last_success_at": 0.0,
                "last_failure_at": 0.0,
                "last_restart_at": 0.0,
                "recovering_until": 0.0,
                "cooling_until": 0.0,
                "force_recycle_page": False,
                "last_outcome": "",
                "last_message": "",
                "restart_count": 0,
                "recycle_count": 0,
            }
            self._browser_runtime[index] = runtime
        return runtime

    async def _cleanup_persistent_page(self, index: int):
        cached = self._persistent_pages.pop(index, None)
        if not cached:
            runtime = self._get_browser_runtime(index)
            runtime["page_reuse_count"] = 0
            return
        context, page = cached
        try:
            await page.close()
        except Exception:
            pass
        try:
            await context.close()
        except Exception:
            pass
        runtime = self._get_browser_runtime(index)
        runtime["page_reuse_count"] = 0

    def _mark_browser_recovering(self, index: int, seconds: float, reason: str = ""):
        runtime = self._get_browser_runtime(index)
        runtime["recovering_until"] = time.time() + max(seconds, 0)
        if reason:
            runtime["last_message"] = reason

    def _mark_browser_cooling(self, index: int, seconds: float, reason: str = ""):
        runtime = self._get_browser_runtime(index)
        runtime["cooling_until"] = time.time() + max(seconds, 0)
        if reason:
            runtime["last_message"] = reason

    def _record_recent_result(self, index: int, outcome: str, elapsed: float = 0.0, message: str = ""):
        now = time.time()
        self._recent_results.append({
            "ts": now,
            "browser_index": index,
            "outcome": outcome,
            "elapsed": round(max(elapsed or 0.0, 0.0), 3),
            "message": message,
        })
        runtime = self._get_browser_runtime(index)
        runtime["last_outcome"] = outcome
        if message:
            runtime["last_message"] = message

    def _should_recycle_persistent_page(self, index: int, task_count: int) -> str:
        runtime = self._get_browser_runtime(index)
        if runtime.get("force_recycle_page"):
            return "上次任务失败后要求丢弃持久页面"
        if runtime.get("page_reuse_count", 0) >= PAGE_REUSE_LIMIT:
            return f"持久页面已连续复用 {runtime['page_reuse_count']} 次"
        if task_count >= BROWSER_SOFT_RECYCLE_THRESHOLD:
            return f"浏览器已累计执行 {task_count} 次任务，执行软换血"
        if runtime.get("consecutive_failures", 0) > 0:
            return "浏览器存在连续失败记录，跳过页面复用"
        return ""

    def _build_health_snapshot(self) -> dict:
        now = time.time()
        runtimes = [self._get_browser_runtime(i) for i in range(1, self.thread_count + 1)]
        recent_results = list(self._recent_results)
        recent_total = len(recent_results)
        recent_success_count = sum(1 for item in recent_results if item["outcome"] == "success")
        recent_fail_count = recent_total - recent_success_count
        recent_timeout_count = sum(1 for item in recent_results if item["outcome"] == "timeout")
        recent_captcha_fail_count = sum(1 for item in recent_results if item["outcome"] in {"timeout", "exception"})
        elapsed_values = [item["elapsed"] for item in recent_results if item.get("elapsed", 0) > 0]
        avg_solve_seconds_recent = round(sum(elapsed_values) / len(elapsed_values), 3) if elapsed_values else 0.0
        recovering_browsers = sum(1 for runtime in runtimes if runtime.get("recovering_until", 0) > now)
        cooling_browsers = sum(1 for runtime in runtimes if runtime.get("cooling_until", 0) > now)
        page_recycle_pending = sum(1 for runtime in runtimes if runtime.get("force_recycle_page"))
        browser_restart_count_recent = sum(1 for ts in self._restart_events if now - ts <= RESTART_EVENT_WINDOW_SECONDS)
        last_success_at = max((runtime.get("last_success_at", 0.0) for runtime in runtimes), default=0.0)
        last_failure_at = max((runtime.get("last_failure_at", 0.0) for runtime in runtimes), default=0.0)
        fail_ratio = (recent_fail_count / recent_total) if recent_total else 0.0

        health_score = 100
        health_score -= int(fail_ratio * 55)
        if avg_solve_seconds_recent > 12:
            health_score -= min(int((avg_solve_seconds_recent - 12) * 3), 20)
        health_score -= min(recent_timeout_count * 6, 18)
        health_score -= cooling_browsers * 10
        health_score -= recovering_browsers * 8
        health_score -= page_recycle_pending * 6
        health_score -= min(max(browser_restart_count_recent - self.thread_count, 0) * 2, 10)
        health_score = max(min(health_score, 100), 0)

        if recent_total < max(2, self.thread_count):
            node_health_status = "warming"
            node_degraded_reason = "warming_up"
        elif health_score < 60 or fail_ratio >= 0.45 or recent_timeout_count >= max(2, self.thread_count):
            node_health_status = "degraded"
            if recent_timeout_count >= max(2, self.thread_count):
                node_degraded_reason = "recent_timeout_spike"
            elif fail_ratio >= 0.45:
                node_degraded_reason = "recent_fail_spike"
            else:
                node_degraded_reason = "health_score_low"
        elif cooling_browsers > 0:
            node_health_status = "cooling"
            node_degraded_reason = "cooldown_after_failures"
        elif recovering_browsers > 0:
            node_health_status = "recovering"
            node_degraded_reason = "browser_recovering"
        else:
            node_health_status = "healthy"
            node_degraded_reason = ""

        browser_runtime = []
        for index in range(1, self.thread_count + 1):
            runtime = self._get_browser_runtime(index)
            browser_runtime.append({
                "index": index,
                "task_count": runtime.get("task_count", 0),
                "page_reuse_count": runtime.get("page_reuse_count", 0),
                "consecutive_failures": runtime.get("consecutive_failures", 0),
                "recovering": runtime.get("recovering_until", 0) > now,
                "cooling": runtime.get("cooling_until", 0) > now,
                "force_recycle_page": bool(runtime.get("force_recycle_page")),
                "last_outcome": runtime.get("last_outcome", ""),
                "last_message": runtime.get("last_message", ""),
                "last_success_at": runtime.get("last_success_at", 0.0),
                "last_failure_at": runtime.get("last_failure_at", 0.0),
                "restart_count": runtime.get("restart_count", 0),
            })

        return {
            "node_health_score": health_score,
            "node_health_status": node_health_status,
            "node_degraded_reason": node_degraded_reason,
            "recent_success_count": recent_success_count,
            "recent_fail_count": recent_fail_count,
            "recent_captcha_fail_count": recent_captcha_fail_count,
            "recent_timeout_count": recent_timeout_count,
            "avg_solve_seconds_recent": avg_solve_seconds_recent,
            "recovering_browsers": recovering_browsers,
            "cooling_browsers": cooling_browsers,
            "page_recycle_pending": page_recycle_pending,
            "browser_restart_count_recent": browser_restart_count_recent,
            "last_success_at": round(last_success_at, 3) if last_success_at else 0.0,
            "last_failure_at": round(last_failure_at, 3) if last_failure_at else 0.0,
            "browser_runtime": browser_runtime,
        }

    async def _reserve_solver_slot(self):
        async with self._admission_lock:
            raw_available = self.browser_pool.qsize()
            effective_available = max(raw_available - self._reserved_slots, 0)
            if effective_available <= 0:
                return None
            self._reserved_slots += 1
            return {
                "raw_available_browsers": raw_available,
                "available_browsers": max(raw_available - self._reserved_slots, 0),
                "reserved_slots": self._reserved_slots,
            }

    async def _release_solver_slot(self):
        async with self._admission_lock:
            if self._reserved_slots > 0:
                self._reserved_slots -= 1

    async def _startup(self):
        """服务启动时初始化浏览器池"""
        self.display_welcome()
        logger.info("正在初始化浏览器...")
        try:
            await init_db()
            await self._initialize_browser()
            self.init_count += 1
            self.last_init_at = time.time()
            self.last_init_reason = "startup"
            self.last_init_result = "success"
            asyncio.create_task(self._periodic_cleanup())
        except Exception as e:
            self.last_init_at = time.time()
            self.last_init_reason = "startup"
            self.last_init_result = "failed"
            self.last_reinit_error = str(e)
            logger.error(f"浏览器初始化失败: {e}")
            raise

    async def _initialize_browser(self, indices: list[int] | None = None):
        """创建浏览器池（并发初始化，大幅缩短启动时间）"""
        target_indices = [int(i) for i in (indices or list(range(1, self.thread_count + 1)))]

        async def _bootstrap(index: int):
            entry = await self._create_browser_entry(index)
            await self.browser_pool.put(entry)
            if self.debug:
                logger.info(f"浏览器 {index} 并发初始化成功")

        await asyncio.gather(*[_bootstrap(index) for index in target_indices])
        logger.info(f"浏览器池就绪，共 {self.browser_pool.qsize()} 个实例")

    async def _restart_browser(self, index: int, old_browser, config: dict) -> tuple:
        """重启单个浏览器实例，释放长时间运行后的污染与内存泄漏"""
        logger.warning(f"浏览器 {index}: 达到换血条件，正在重启...")
        await self._cleanup_persistent_page(index)
        await self._close_browser_instance(old_browser)
        try:
            index, new_browser, new_config, _ = await self._create_browser_entry(index)
            runtime = self._get_browser_runtime(index)
            runtime["task_count"] = 0
            runtime["page_reuse_count"] = 0
            runtime["consecutive_failures"] = 0
            runtime["force_recycle_page"] = False
            runtime["last_restart_at"] = time.time()
            runtime["restart_count"] += 1
            self._restart_events.append(runtime["last_restart_at"])
            self._mark_browser_recovering(index, RECOVERING_GRACE_SECONDS, "browser_restarted")
            logger.success(f"浏览器 {index}: 重启成功")
            return (index, new_browser, new_config, 0)
        except Exception as e:
            logger.error(f"浏览器 {index}: 重启失败: {e}")
            raise

    async def _periodic_cleanup(self):
        """每小时清理过期结果"""
        while True:
            try:
                await asyncio.sleep(3600)
                deleted = await cleanup_old_results(days_old=1)
                if deleted > 0:
                    logger.info(f"清理了 {deleted} 条过期结果")
            except Exception as e:
                logger.error(f"定期清理出错: {e}")

    # ========== 反检测注入 ==========

    async def _antishadow_inject(self, page):
        """拦截 closed Shadow DOM，使 Turnstile 内部元素可访问"""
        await page.add_init_script("""
          (function() {
            const originalAttachShadow = Element.prototype.attachShadow;
            Element.prototype.attachShadow = function(init) {
              const shadow = originalAttachShadow.call(this, init);
              if (init.mode === 'closed') {
                window.__lastClosedShadowRoot = shadow;
              }
              return shadow;
            };
          })();
        """)

    async def _inject_antibot(self, page):
        """注入反 webdriver 检测脚本"""
        await page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {} };
        """)

    # ========== 资源拦截（加速加载） ==========

    async def _optimized_route_handler(self, route):
        """只放行关键请求，拦截图片/字体等无用资源"""
        url = route.request.url
        resource_type = route.request.resource_type
        allowed_types = {'document', 'script', 'xhr', 'fetch'}
        cf_domains = ['challenges.cloudflare.com', 'static.cloudflareinsights.com', 'cloudflare.com']

        if resource_type in allowed_types:
            await route.continue_()
        elif any(d in url for d in cf_domains):
            await route.continue_()
        else:
            await route.abort()

    async def _block_rendering(self, page):
        await page.route("**/*", self._optimized_route_handler)

    async def _unblock_rendering(self, page):
        await page.unroute("**/*", self._optimized_route_handler)

    # ========== Turnstile 注入与解题 ==========

    async def _inject_captcha_directly(self, page, sitekey: str, index: int = 0):
        """在目标页面直接注入 Turnstile widget 并等待自动校验"""
        script = f"""
        // 清除页面上已有的 Turnstile 元素
        document.querySelectorAll('.cf-turnstile').forEach(el => el.remove());
        document.querySelectorAll('[data-sitekey]').forEach(el => el.remove());

        // 创建 Turnstile widget 容器
        const captchaDiv = document.createElement('div');
        captchaDiv.className = 'cf-turnstile';
        captchaDiv.setAttribute('data-sitekey', '{sitekey}');
        captchaDiv.setAttribute('data-callback', 'onTurnstileCallback');
        captchaDiv.style.position = 'fixed';
        captchaDiv.style.top = '20px';
        captchaDiv.style.left = '20px';
        captchaDiv.style.zIndex = '9999';
        captchaDiv.style.backgroundColor = 'white';
        captchaDiv.style.padding = '15px';
        captchaDiv.style.border = '2px solid #0f79af';
        captchaDiv.style.borderRadius = '8px';
        document.body.appendChild(captchaDiv);

        // 加载 Turnstile 脚本并渲染
        const renderWidget = () => {{
            if (window.turnstile && window.turnstile.render) {{
                try {{
                    window.turnstile.render(captchaDiv, {{
                        sitekey: '{sitekey}',
                        callback: function(token) {{
                            console.log('Turnstile solved');
                            let inp = document.querySelector('input[name="cf-turnstile-response"]');
                            if (!inp) {{
                                inp = document.createElement('input');
                                inp.type = 'hidden';
                                inp.name = 'cf-turnstile-response';
                                document.body.appendChild(inp);
                            }}
                            inp.value = token;
                        }},
                        'error-callback': function(err) {{
                            console.log('Turnstile error:', err);
                        }}
                    }});
                }} catch (e) {{
                    console.log('Turnstile render error:', e);
                }}
            }}
        }};

        // 如果 Turnstile 已加载则直接渲染，否则动态加载脚本
        if (window.turnstile) {{
            renderWidget();
        }} else {{
            const s = document.createElement('script');
            s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js';
            s.async = true;
            s.defer = true;
            s.onload = function() {{ setTimeout(renderWidget, 1000); }};
            document.head.appendChild(s);
        }}

        // 全局回调
        window.onTurnstileCallback = function(token) {{
            console.log('Global callback:', token);
        }};
        """
        await page.evaluate(script)
        if self.debug:
            logger.debug(f"浏览器 {index}: Turnstile widget 已注入，sitekey: {sitekey}")

    async def _find_and_click_checkbox(self, page, index: int):
        """在 Turnstile iframe 内查找并点击 checkbox"""
        try:
            iframe_selectors = [
                'iframe[src*="challenges.cloudflare.com"]',
                'iframe[src*="turnstile"]',
                'iframe[title*="widget"]',
            ]
            for selector in iframe_selectors:
                try:
                    locator = page.locator(selector).first
                    count = await locator.count()
                    if count > 0:
                        el = await locator.element_handle()
                        frame = await el.content_frame()
                        if frame:
                            for cb_sel in ['input[type="checkbox"]', '.cb-lb input[type="checkbox"]']:
                                try:
                                    await frame.locator(cb_sel).first.click(timeout=2000)
                                    if self.debug:
                                        logger.debug(f"浏览器 {index}: 成功点击 iframe 内 checkbox")
                                    return True
                                except Exception:
                                    continue
                            # 回退：直接点击 iframe 本身
                            try:
                                await locator.click(timeout=1000)
                                return True
                            except Exception:
                                pass
                except Exception:
                    continue
        except Exception:
            pass
        return False

    async def _try_click_strategies(self, page, index: int):
        """多种点击策略尝试触发 Turnstile"""
        strategies = [
            ('checkbox', lambda: self._find_and_click_checkbox(page, index)),
            ('cf-turnstile', lambda: page.locator('.cf-turnstile').first.click(timeout=1000)),
            ('iframe', lambda: page.locator('iframe[src*="turnstile"]').first.click(timeout=1000)),
            ('js_click', lambda: page.evaluate("document.querySelector('.cf-turnstile')?.click()")),
            ('sitekey', lambda: page.locator('[data-sitekey]').first.click(timeout=1000)),
        ]
        for name, func in strategies:
            try:
                result = await func()
                if result is True or result is None:
                    if self.debug:
                        logger.debug(f"浏览器 {index}: 点击策略 '{name}' 成功")
                    return True
            except Exception:
                continue
        return False

    # ========== 核心解题逻辑 ==========

    async def _solve_turnstile(self, task_id: str, url: str, sitekey: str):
        """执行 Turnstile 解题的完整流程（受控复用 + 主动换血）"""
        index = None
        browser = None
        bconfig = None
        task_count = 0
        reserved_slot_released = False

        self._set_task_state(task_id, "queued", "waiting_browser", "等待浏览器分配")
        try:
            index, browser, bconfig, task_count = await asyncio.wait_for(
                self.browser_pool.get(), timeout=BROWSER_ACQUIRE_TIMEOUT
            )
            await self._release_solver_slot()
            reserved_slot_released = True
            runtime = self._get_browser_runtime(index)
            runtime["task_count"] = max(runtime.get("task_count", 0), task_count)
            self._set_task_state(
                task_id,
                "processing",
                "browser_acquired",
                f"已分配浏览器 #{index}",
                browser_index=index,
            )
        except asyncio.TimeoutError:
            if not reserved_slot_released:
                await self._release_solver_slot()
            logger.error(f"任务 {task_id[:8]}: {BROWSER_ACQUIRE_TIMEOUT}s 内未分配到浏览器")
            self._set_task_state(
                task_id,
                "failed",
                "browser_acquire_timeout",
                f"{BROWSER_ACQUIRE_TIMEOUT}s 内未分配到浏览器",
                elapsed_time=BROWSER_ACQUIRE_TIMEOUT,
            )
            await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": BROWSER_ACQUIRE_TIMEOUT})
            return
        except Exception as e:
            if not reserved_slot_released:
                await self._release_solver_slot()
            logger.error(f"任务 {task_id[:8]}: 获取浏览器失败: {e}")
            self._set_task_state(task_id, "failed", "browser_acquire_error", f"获取浏览器失败: {e}")
            await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": 0})
            return

        runtime = self._get_browser_runtime(index)

        try:
            if hasattr(browser, 'is_connected') and not browser.is_connected():
                self._set_task_state(task_id, "processing", "browser_recovering", f"浏览器 #{index} 连接断开，尝试自动恢复", browser_index=index)
                logger.warning(f"浏览器 {index}: 连接断开，尝试自动恢复...")
                try:
                    index, browser, bconfig, task_count = await self._restart_browser(index, browser, bconfig)
                    runtime = self._get_browser_runtime(index)
                    self._set_task_state(task_id, "processing", "browser_recovered", f"浏览器 #{index} 已自动恢复", browser_index=index)
                except Exception:
                    self._set_task_state(task_id, "failed", "browser_recover_failed", f"浏览器 #{index} 自动恢复失败", browser_index=index)
                    await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": 0})
                    return
        except Exception:
            pass

        if task_count >= BROWSER_RESTART_THRESHOLD:
            self._set_task_state(task_id, "processing", "browser_restarting", f"浏览器 #{index} 达到硬重启阈值，准备重启", browser_index=index)
            try:
                index, browser, bconfig, task_count = await self._restart_browser(index, browser, bconfig)
                runtime = self._get_browser_runtime(index)
                self._set_task_state(task_id, "processing", "browser_restarted", f"浏览器 #{index} 已完成例行重启", browser_index=index)
            except Exception:
                await self.browser_pool.put((index, browser, bconfig, task_count))
                self._set_task_state(task_id, "failed", "browser_restart_failed", f"浏览器 #{index} 例行重启失败", browser_index=index)
                await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": 0})
                return

        recycle_reason = self._should_recycle_persistent_page(index, task_count)
        if recycle_reason and index in self._persistent_pages:
            if self.debug:
                logger.debug(f"浏览器 {index}: {recycle_reason}，丢弃旧页面")
            await self._cleanup_persistent_page(index)
            runtime["force_recycle_page"] = False
            runtime["recycle_count"] += 1

        is_reuse = False
        context = None
        page = None

        if index in self._persistent_pages:
            try:
                context, page = self._persistent_pages[index]
                await page.evaluate("1")
                is_reuse = True
                if self.debug:
                    logger.debug(f"浏览器 {index}: 复用已有页面")
            except Exception:
                if self.debug:
                    logger.debug(f"浏览器 {index}: 持久页面已失效，重新创建")
                await self._cleanup_persistent_page(index)
                runtime["force_recycle_page"] = False
                is_reuse = False

        if not is_reuse:
            ctx_opts = {}
            if bconfig.get('useragent'):
                ctx_opts["user_agent"] = bconfig['useragent']
            if bconfig.get('sec_ch_ua') and bconfig['sec_ch_ua'].strip():
                ctx_opts['extra_http_headers'] = {'sec-ch-ua': bconfig['sec_ch_ua']}

            context = await browser.new_context(**ctx_opts)
            page = await context.new_page()
            await self._antishadow_inject(page)
            await self._block_rendering(page)
            await self._inject_antibot(page)

            if self.browser_type in ['chromium', 'chrome', 'msedge']:
                await page.set_viewport_size({"width": 500, "height": 100})
            runtime["page_reuse_count"] = 0

        start_time = time.time()

        try:
            if self.debug:
                reuse_tag = "复用" if is_reuse else "新建"
                logger.debug(f"浏览器 {index}: 开始解题 [{reuse_tag}] (第 {task_count + 1} 次)")

            if not is_reuse:
                self._set_task_state(task_id, "processing", "opening_page", "打开目标页面并准备环境", browser_index=index)
                await page.goto(url, wait_until='domcontentloaded', timeout=30000)
                await self._unblock_rendering(page)
            else:
                self._set_task_state(task_id, "processing", "reusing_page", "复用持久页面继续解题", browser_index=index)

            self._set_task_state(task_id, "processing", "injecting_widget", "注入 Turnstile 组件", browser_index=index)
            await self._inject_captcha_directly(page, sitekey, index)
            await asyncio.sleep(1)

            self._set_task_state(task_id, "processing", "waiting_token", "等待 Turnstile 返回 token", browser_index=index)
            locator = page.locator('input[name="cf-turnstile-response"]')
            max_attempts = 30
            click_count = 0
            max_clicks = 10

            for attempt in range(max_attempts):
                try:
                    try:
                        count = await locator.count()
                    except Exception:
                        count = 0

                    if count >= 1:
                        for i in range(count):
                            try:
                                token = await locator.nth(i).input_value(timeout=500)
                                if token and len(token) > 20:
                                    elapsed = round(time.time() - start_time, 3)
                                    logger.success(
                                        f"浏览器 {index}: 解题成功 - "
                                        f"{COLORS['MAGENTA']}{token[:10]}...{COLORS['RESET']} "
                                        f"耗时 {COLORS['GREEN']}{elapsed}s{COLORS['RESET']}"
                                    )
                                    runtime = self._get_browser_runtime(index)
                                    runtime["consecutive_failures"] = 0
                                    runtime["recent_failures"] = 0
                                    runtime["last_success_at"] = time.time()
                                    runtime["recovering_until"] = 0.0
                                    runtime["cooling_until"] = 0.0
                                    runtime["force_recycle_page"] = False
                                    runtime["page_reuse_count"] = runtime.get("page_reuse_count", 0) + 1
                                    self._consecutive_fails[index] = 0
                                    self._persistent_pages[index] = (context, page)
                                    self._record_recent_result(index, "success", elapsed, "token_ready")
                                    self._set_task_state(task_id, "completed", "token_ready", "Turnstile token 已就绪", browser_index=index, elapsed_time=elapsed)
                                    await save_result(task_id, "turnstile", {"value": token, "elapsed_time": elapsed})
                                    return
                            except Exception:
                                continue

                    if attempt % 2 == 0 and click_count < max_clicks:
                        await self._try_click_strategies(page, index)
                        click_count += 1

                    wait = min(0.3 + (attempt * 0.03), 1.0)
                    await asyncio.sleep(wait)

                    if self.debug and attempt % 5 == 0:
                        logger.debug(f"浏览器 {index}: 尝试 {attempt + 1}/{max_attempts} (点击: {click_count}/{max_clicks})")

                except Exception as e:
                    if self.debug:
                        logger.debug(f"浏览器 {index}: 尝试 {attempt + 1} 出错: {e}")
                    continue

            elapsed = round(time.time() - start_time, 3)
            logger.error(f"浏览器 {index}: 解题超时 ({elapsed}s)")
            runtime = self._get_browser_runtime(index)
            runtime["consecutive_failures"] = runtime.get("consecutive_failures", 0) + 1
            runtime["recent_failures"] = runtime.get("recent_failures", 0) + 1
            runtime["last_failure_at"] = time.time()
            runtime["force_recycle_page"] = True
            self._consecutive_fails[index] = runtime["consecutive_failures"]
            fails = runtime["consecutive_failures"]
            logger.warning(f"浏览器 {index}: 连续失败 {fails}/{CONSECUTIVE_FAIL_RESTART}")
            self._record_recent_result(index, "timeout", elapsed, "solve_timeout")
            self._set_task_state(task_id, "failed", "solve_timeout", "等待 Turnstile token 超时", browser_index=index, elapsed_time=elapsed)
            await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": elapsed})

        except Exception as e:
            elapsed = round(time.time() - start_time, 3)
            logger.error(f"浏览器 {index}: 解题异常: {e}")
            runtime = self._get_browser_runtime(index)
            runtime["consecutive_failures"] = runtime.get("consecutive_failures", 0) + 1
            runtime["recent_failures"] = runtime.get("recent_failures", 0) + 1
            runtime["last_failure_at"] = time.time()
            runtime["force_recycle_page"] = True
            self._consecutive_fails[index] = runtime["consecutive_failures"]
            self._record_recent_result(index, "exception", elapsed, f"solve_exception: {e}")
            await self._cleanup_persistent_page(index)
            try:
                await page.close()
                await context.close()
            except Exception:
                pass
            self._set_task_state(task_id, "failed", "solve_exception", f"解题异常: {e}", browser_index=index, elapsed_time=elapsed)
            await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": elapsed})

        finally:
            runtime = self._get_browser_runtime(index)
            fails = runtime.get("consecutive_failures", 0)
            if fails >= CONSECUTIVE_FAIL_RESTART:
                logger.warning(f"浏览器 {index}: 连续失败 {fails} 次，触发强制重启以绕过风控...")
                await self._cleanup_persistent_page(index)
                try:
                    index, browser, bconfig, task_count = await self._restart_browser(index, browser, bconfig)
                    runtime = self._get_browser_runtime(index)
                    self._consecutive_fails[index] = 0
                    cooldown = random.uniform(*FAIL_COOLDOWN_RANGE)
                    logger.info(f"浏览器 {index}: 重启后冷却 {cooldown:.1f}s...")
                    self._mark_browser_cooling(index, cooldown, "restart_cooldown")
                    await asyncio.sleep(cooldown)
                except Exception as e:
                    logger.error(f"浏览器 {index}: 强制重启失败: {e}")
            elif fails > 0:
                await self._cleanup_persistent_page(index)
                cooldown = random.uniform(1, 3)
                self._mark_browser_cooling(index, cooldown, "failure_cooldown")
                await asyncio.sleep(cooldown)

            if browser is not None and index is not None:
                runtime = self._get_browser_runtime(index)
                runtime["task_count"] = task_count + 1
                await self.browser_pool.put((index, browser, bconfig, task_count + 1))

    # ========== HTTP 路由处理 ==========

    async def admin_reinitialize(self):
        """管理接口：执行节点级软初始化"""
        if not self._admin_token:
            return jsonify({
                "ok": False,
                "accepted": False,
                "message": "reinitialize_disabled",
                **self._build_node_runtime_snapshot(),
            }), 503

        if not self._is_admin_authorized():
            return jsonify({
                "ok": False,
                "accepted": False,
                "message": "unauthorized",
            }), 401

        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict):
            payload = {}
        reason = str(payload.get("reason") or "remote_reinitialize").strip()[:120]
        requested_by = str(payload.get("requested_by") or "unknown").strip()[:120]
        source = str(payload.get("source") or "remote_admin").strip()[:120]
        result = await self._reinitialize_solver(reason=reason, requested_by=requested_by, source=source)
        status_code = 200 if result.get("ok") else (202 if result.get("message") == "reinitialize_already_running" else 409)
        if result.get("message") == "reinitialize_disabled":
            status_code = 503
        return jsonify(result), status_code

    async def process_turnstile(self):
        """处理 /turnstile 请求，创建解题任务"""
        url = request.args.get('url')
        sitekey = request.args.get('sitekey')

        if not url or not sitekey:
            return jsonify({"error": "缺少 url 或 sitekey 参数"}), 400

        if self.reinit_in_progress:
            return jsonify({
                "status": "busy",
                "stage": "reinitializing",
                "error": "solver_reinitializing",
                "message": "节点正在执行软初始化，请稍后重试",
                **self._build_node_runtime_snapshot(),
            }), 503

        reservation = await self._reserve_solver_slot()
        if not reservation:
            raw_available = self.browser_pool.qsize()
            effective_available = self._effective_available_browsers()
            logger.warning(f"忙碌拒单: raw_available={raw_available}, reserved={self._reserved_slots}, total={self.thread_count}")
            return jsonify({
                "status": "busy",
                "error": "solver_busy",
                "message": "当前无空闲浏览器，请稍后重试",
                "available_browsers": effective_available,
                "raw_available_browsers": raw_available,
                "reserved_slots": self._reserved_slots,
                "total_browsers": self.thread_count,
            }), 503

        task_id = str(uuid.uuid4())
        self._set_task_state(task_id, "queued", "accepted", "任务已创建，等待浏览器分配", url=url)
        logger.info(f"新任务: {task_id[:8]}... URL={url} sitekey={sitekey[:10]}...")

        # 异步启动解题
        asyncio.create_task(self._solve_turnstile(task_id, url, sitekey))

        return jsonify({
            "task_id": task_id,
            "status": "queued",
            "stage": "accepted",
            "message": "任务已创建，等待浏览器分配",
            **reservation,
        })

    async def get_result(self):
        """处理 /result 请求，返回解题结果（取走后自动删除释放内存）"""
        task_id = request.args.get('task_id')
        if not task_id:
            return jsonify({"error": "缺少 task_id 参数"}), 400

        result = await load_result(task_id)
        if not result:
            payload = self._build_task_state_payload(task_id)
            if payload:
                return jsonify(payload)
            return jsonify({
                "task_id": task_id,
                "status": "CAPTCHA_NOT_READY",
                "stage": "unknown",
                "message": "任务不存在或结果尚未回写",
                "solution": None,
            })

        task_state = self._task_states.pop(task_id, None)

        # 结果已就绪，取走后立即删除释放内存
        await delete_result(task_id)

        token = result.get("value", "")
        if token == "CAPTCHA_FAIL":
            return jsonify({
                "task_id": task_id,
                "status": "failed",
                "stage": (task_state or {}).get("stage", "failed"),
                "message": (task_state or {}).get("message", "解题失败"),
                "solution": {"token": "CAPTCHA_FAIL"},
                "elapsed_time": result.get("elapsed_time", (task_state or {}).get("elapsed_time", 0)),
            })

        return jsonify({
            "task_id": task_id,
            "status": "completed",
            "stage": (task_state or {}).get("stage", "completed"),
            "message": (task_state or {}).get("message", "Turnstile token 已就绪"),
            "solution": {"token": token},
            "elapsed_time": result.get("elapsed_time", (task_state or {}).get("elapsed_time", 0)),
        })

    async def stats(self):
        """统计端点：返回解题统计、运行态与节点健康度"""
        db_stats = await get_stats()
        import psutil
        process = psutil.Process()
        mem_mb = round(process.memory_info().rss / 1024 / 1024, 1)
        raw_available = self.browser_pool.qsize()
        effective_available = self._effective_available_browsers()
        health = self._build_health_snapshot()
        return jsonify({
            **db_stats,
            "available_browsers": effective_available,
            "raw_available_browsers": raw_available,
            "reserved_slots": self._reserved_slots,
            "tracked_tasks": len(self._task_states),
            "total_browsers": self.thread_count,
            "memory_mb": mem_mb,
            "soft_recycle_threshold": BROWSER_SOFT_RECYCLE_THRESHOLD,
            "restart_threshold": BROWSER_RESTART_THRESHOLD,
            "page_reuse_limit": PAGE_REUSE_LIMIT,
            **health,
            **self._build_node_runtime_snapshot(),
        })

    async def index(self):
        """健康检查页面"""
        raw_available = self.browser_pool.qsize()
        effective_available = self._effective_available_browsers()
        health = self._build_health_snapshot()
        return jsonify({
            "status": "reinitializing" if self.reinit_in_progress else "running",
            "browser_type": self.browser_type,
            "available_browsers": effective_available,
            "raw_available_browsers": raw_available,
            "reserved_slots": self._reserved_slots,
            "total_browsers": self.thread_count,
            "node_health_status": health["node_health_status"],
            "node_health_score": health["node_health_score"],
            "node_degraded_reason": health["node_degraded_reason"],
            "usage": "GET /turnstile?url=<url>&sitekey=<sitekey>",
            **self._build_node_runtime_snapshot(),
        })

    def run(self, port: int = 5000):
        """启动服务"""
        logger.info(f"Turnstile Solver 服务启动于 http://127.0.0.1:{port}")
        self.app.run(host='0.0.0.0', port=port, debug=False)


# ========== 入口 ==========

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Turnstile Solver API 服务")
    parser.add_argument("--browser_type", default=os.getenv("SOLVER_BROWSER", "camoufox"), choices=["camoufox", "chromium", "chrome", "msedge"],
                        help="浏览器类型 (默认: camoufox, 环境变量: SOLVER_BROWSER)")
    parser.add_argument("--thread", type=int, default=int(os.getenv("SOLVER_THREADS", 3)), help="浏览器并发数 (默认: 3, 环境变量: SOLVER_THREADS)")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", 5000)), help="服务端口 (默认: 5000, 环境变量: PORT)")
    
    headless_default = os.getenv("SOLVER_HEADLESS", "true").lower() in ("true", "1", "yes")
    parser.add_argument("--headless", action="store_true", default=headless_default, help="无头模式运行 (环境变量: SOLVER_HEADLESS=true)")
    
    debug_default = os.getenv("SOLVER_DEBUG", "false").lower() in ("true", "1", "yes")
    parser.add_argument("--debug", action="store_true", default=debug_default, help="调试模式 (环境变量: SOLVER_DEBUG=true)")
    args = parser.parse_args()

    server = TurnstileAPIServer(
        headless=args.headless,
        debug=args.debug,
        browser_type=args.browser_type,
        thread=args.thread,
    )
    server.run(port=args.port)
