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
from typing import Optional
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


# 浏览器实例超过此次数后自动重启（释放内存泄漏）
BROWSER_RESTART_THRESHOLD = 25
# 连续失败超过此次数后，强制重启浏览器（绕过 Cloudflare 风控标记）
CONSECUTIVE_FAIL_RESTART = 3
# 失败后冷却等待范围（秒），避免被风控快速标记
FAIL_COOLDOWN_RANGE = (3, 8)
# 获取浏览器的最长等待时间（秒），避免任务无限排队
BROWSER_ACQUIRE_TIMEOUT = 10


class TurnstileAPIServer:

    def __init__(self, headless: bool, debug: bool, browser_type: str, thread: int):
        self.app = Quart(__name__)
        self.debug = debug
        self.browser_type = browser_type
        self.headless = headless
        self.thread_count = thread
        # 浏览器池元素格式: (index, browser, config, task_count)
        self.browser_pool = asyncio.Queue()
        # 保存 Camoufox 管理器引用，用于创建新浏览器
        self._camoufox_manager = None
        self._pw_manager = None
        # 每个浏览器的连续失败计数 {index: fail_count}
        self._consecutive_fails = {}
        # 持久页面缓存 {index: (context, page)}，避免每次重新加载
        self._persistent_pages = {}
        self.console = Console()
        self._task_states = {}
        self._admission_lock = asyncio.Lock()
        self._reserved_slots = 0
        self._setup_routes()

    def display_welcome(self):
        """显示欢迎界面"""
        self.console.clear()
        text = Text()
        text.append("\n🔧 Turnstile Solver 服务", style="bold white")
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
        self.app.route('/')(self.index)

    def _effective_available_browsers(self) -> int:
        return max(self.browser_pool.qsize() - self._reserved_slots, 0)

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
            asyncio.create_task(self._periodic_cleanup())
        except Exception as e:
            logger.error(f"浏览器初始化失败: {e}")
            raise

    async def _initialize_browser(self):
        """创建浏览器池（并发初始化，大幅缩短启动时间）"""
        if self.browser_type == "camoufox":
            self._camoufox_manager = AsyncCamoufox(headless=self.headless)

            async def _start_camoufox(i):
                browser = await self._camoufox_manager.start()
                config = {
                    'browser_name': 'camoufox',
                    'browser_version': 'latest',
                    'useragent': None,
                    'sec_ch_ua': None,
                }
                await self.browser_pool.put((i + 1, browser, config, 0))
                if self.debug:
                    logger.info(f"浏览器 {i + 1} 并发初始化成功")

            # 并发启动所有浏览器，总耗时 ≈ 单个浏览器耗时
            await asyncio.gather(*[_start_camoufox(i) for i in range(self.thread_count)])
        else:
            from patchright.async_api import async_playwright
            self._pw_manager = await async_playwright().start()

            async def _start_chromium(i):
                _, ver, ua, sec_ch_ua = browser_config.get_random_browser_config(self.browser_type)
                browser = await self._pw_manager.chromium.launch(
                    channel=self.browser_type,
                    headless=self.headless,
                    args=["--window-position=0,0", "--force-device-scale-factor=1", f"--user-agent={ua}"]
                )
                config = {
                    'browser_name': 'chrome', 'browser_version': ver,
                    'useragent': ua, 'sec_ch_ua': sec_ch_ua,
                }
                await self.browser_pool.put((i + 1, browser, config, 0))
                if self.debug:
                    logger.info(f"浏览器 {i + 1} 并发初始化成功 (Chrome {ver})")

            # 并发启动所有浏览器
            await asyncio.gather(*[_start_chromium(i) for i in range(self.thread_count)])

        logger.info(f"浏览器池就绪，共 {self.browser_pool.qsize()} 个实例")

    async def _restart_browser(self, index: int, old_browser, config: dict) -> tuple:
        """重启单个浏览器实例，释放累积的内存泄漏"""
        logger.warning(f"浏览器 {index}: 达到 {BROWSER_RESTART_THRESHOLD} 次重启阈值，正在重启...")
        # 清理该浏览器的持久页面
        if index in self._persistent_pages:
            try:
                ctx, pg = self._persistent_pages.pop(index)
                await pg.close()
                await ctx.close()
            except Exception:
                pass
        try:
            await old_browser.close()
        except Exception:
            pass
        try:
            if self.browser_type == "camoufox" and self._camoufox_manager:
                new_browser = await self._camoufox_manager.start()
            elif self._pw_manager:
                ua = config.get('useragent', '')
                new_browser = await self._pw_manager.chromium.launch(
                    channel=self.browser_type,
                    headless=self.headless,
                    args=["--window-position=0,0", "--force-device-scale-factor=1", f"--user-agent={ua}"]
                )
            else:
                raise RuntimeError("无可用浏览器管理器")
            logger.success(f"浏览器 {index}: 重启成功")
            return (index, new_browser, config, 0)
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
        """执行 Turnstile 解题的完整流程（支持页面复用加速）"""
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

        # 检查浏览器连接，断开时自动恢复
        try:
            if hasattr(browser, 'is_connected') and not browser.is_connected():
                self._set_task_state(task_id, "processing", "browser_recovering", f"浏览器 #{index} 连接断开，尝试自动恢复", browser_index=index)
                logger.warning(f"浏览器 {index}: 连接断开，尝试自动恢复...")
                try:
                    index, browser, bconfig, task_count = await self._restart_browser(index, browser, bconfig)
                    self._set_task_state(task_id, "processing", "browser_recovered", f"浏览器 #{index} 已自动恢复", browser_index=index)
                except Exception:
                    self._set_task_state(task_id, "failed", "browser_recover_failed", f"浏览器 #{index} 自动恢复失败", browser_index=index)
                    await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": 0})
                    return
        except Exception:
            pass

        # 检查是否需要定期重启（防止内存泄漏累积）
        if task_count >= BROWSER_RESTART_THRESHOLD:
            self._set_task_state(task_id, "processing", "browser_restarting", f"浏览器 #{index} 达到重启阈值，准备重启", browser_index=index)
            try:
                index, browser, bconfig, task_count = await self._restart_browser(index, browser, bconfig)
                self._set_task_state(task_id, "processing", "browser_restarted", f"浏览器 #{index} 已完成例行重启", browser_index=index)
            except Exception:
                await self.browser_pool.put((index, browser, bconfig, task_count))
                self._set_task_state(task_id, "failed", "browser_restart_failed", f"浏览器 #{index} 例行重启失败", browser_index=index)
                await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": 0})
                return

        # 尝试复用持久页面，避免重复加载（省 3-5s）
        is_reuse = False
        context = None
        page = None

        if index in self._persistent_pages:
            try:
                context, page = self._persistent_pages[index]
                await page.evaluate("1")  # 快速检查页面是否存活
                is_reuse = True
                if self.debug:
                    logger.debug(f"浏览器 {index}: 复用已有页面")
            except Exception:
                # 页面已失效，清理后重新创建
                if self.debug:
                    logger.debug(f"浏览器 {index}: 持久页面已失效，重新创建")
                try:
                    await page.close()
                    await context.close()
                except Exception:
                    pass
                del self._persistent_pages[index]
                is_reuse = False

        if not is_reuse:
            # 首次或页面失效：创建新的上下文和页面
            ctx_opts = {}
            if bconfig.get('useragent'):
                ctx_opts["user_agent"] = bconfig['useragent']
            if bconfig.get('sec_ch_ua') and bconfig['sec_ch_ua'].strip():
                ctx_opts['extra_http_headers'] = {'sec-ch-ua': bconfig['sec_ch_ua']}

            context = await browser.new_context(**ctx_opts)
            page = await context.new_page()

            # 注入反检测脚本
            await self._antishadow_inject(page)
            await self._block_rendering(page)
            await self._inject_antibot(page)

            if self.browser_type in ['chromium', 'chrome', 'msedge']:
                await page.set_viewport_size({"width": 500, "height": 100})

        start_time = time.time()

        try:
            if self.debug:
                reuse_tag = "复用" if is_reuse else "新建"
                logger.debug(f"浏览器 {index}: 开始解题 [{reuse_tag}] (第 {task_count + 1} 次)")

            if not is_reuse:
                self._set_task_state(task_id, "processing", "opening_page", "打开目标页面并准备环境", browser_index=index)
                # 首次：打开目标页面并加载 Turnstile JS
                await page.goto(url, wait_until='domcontentloaded', timeout=30000)
                await self._unblock_rendering(page)
            else:
                self._set_task_state(task_id, "processing", "reusing_page", "复用持久页面继续解题", browser_index=index)

            self._set_task_state(task_id, "processing", "injecting_widget", "注入 Turnstile 组件", browser_index=index)
            # 注入 Turnstile widget（已有 JS 时跳过加载，直接渲染）
            await self._inject_captcha_directly(page, sitekey, index)
            await asyncio.sleep(1)  # 从 3s 缩短到 1s

            self._set_task_state(task_id, "processing", "waiting_token", "等待 Turnstile 返回 token", browser_index=index)
            # 轮询等待 token（加快轮询间隔）
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
                                    # 成功时重置连续失败计数，保留持久页面
                                    self._consecutive_fails[index] = 0
                                    self._persistent_pages[index] = (context, page)
                                    self._set_task_state(task_id, "completed", "token_ready", "Turnstile token 已就绪", browser_index=index, elapsed_time=elapsed)
                                    await save_result(task_id, "turnstile", {"value": token, "elapsed_time": elapsed})
                                    return
                            except Exception:
                                continue

                    # 更早尝试点击（第 1 次就开始），更频繁点击
                    if attempt % 2 == 0 and click_count < max_clicks:
                        await self._try_click_strategies(page, index)
                        click_count += 1

                    # 加快轮询间隔：0.3s 起步，最大 1.0s
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
            # 递增连续失败计数
            self._consecutive_fails[index] = self._consecutive_fails.get(index, 0) + 1
            fails = self._consecutive_fails[index]
            logger.warning(f"浏览器 {index}: 连续失败 {fails}/{CONSECUTIVE_FAIL_RESTART}")
            self._set_task_state(task_id, "failed", "solve_timeout", "等待 Turnstile token 超时", browser_index=index, elapsed_time=elapsed)
            await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": elapsed})

        except Exception as e:
            elapsed = round(time.time() - start_time, 3)
            logger.error(f"浏览器 {index}: 解题异常: {e}")
            self._consecutive_fails[index] = self._consecutive_fails.get(index, 0) + 1
            # 异常时销毁持久页面（可能已损坏）
            if index in self._persistent_pages:
                del self._persistent_pages[index]
            try:
                await page.close()
                await context.close()
            except Exception:
                pass
            self._set_task_state(task_id, "failed", "solve_exception", f"解题异常: {e}", browser_index=index, elapsed_time=elapsed)
            await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": elapsed})

        finally:
            # 连续失败过多时，强制重启浏览器（绕过 Cloudflare 风控标记）
            fails = self._consecutive_fails.get(index, 0)
            if fails >= CONSECUTIVE_FAIL_RESTART:
                logger.warning(f"浏览器 {index}: 连续失败 {fails} 次，触发强制重启以绕过风控...")
                # 销毁持久页面
                if index in self._persistent_pages:
                    try:
                        ctx, pg = self._persistent_pages.pop(index)
                        await pg.close()
                        await ctx.close()
                    except Exception:
                        pass
                try:
                    index, browser, bconfig, task_count = await self._restart_browser(index, browser, bconfig)
                    self._consecutive_fails[index] = 0
                    cooldown = random.uniform(*FAIL_COOLDOWN_RANGE)
                    logger.info(f"浏览器 {index}: 重启后冷却 {cooldown:.1f}s...")
                    await asyncio.sleep(cooldown)
                except Exception as e:
                    logger.error(f"浏览器 {index}: 强制重启失败: {e}")
            elif fails > 0:
                # 失败时销毁持久页面（下次重新创建干净页面）
                if index in self._persistent_pages:
                    try:
                        ctx, pg = self._persistent_pages.pop(index)
                        await pg.close()
                        await ctx.close()
                    except Exception:
                        pass
                cooldown = random.uniform(1, 3)
                await asyncio.sleep(cooldown)

            # 归还浏览器，task_count + 1
            await self.browser_pool.put((index, browser, bconfig, task_count + 1))

    # ========== HTTP 路由处理 ==========

    async def process_turnstile(self):
        """处理 /turnstile 请求，创建解题任务"""
        url = request.args.get('url')
        sitekey = request.args.get('sitekey')

        if not url or not sitekey:
            return jsonify({"error": "缺少 url 或 sitekey 参数"}), 400

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
        """统计端点：返回解题统计和系统状态"""
        db_stats = await get_stats()
        import psutil
        process = psutil.Process()
        mem_mb = round(process.memory_info().rss / 1024 / 1024, 1)
        raw_available = self.browser_pool.qsize()
        effective_available = self._effective_available_browsers()
        return jsonify({
            **db_stats,
            "available_browsers": effective_available,
            "raw_available_browsers": raw_available,
            "reserved_slots": self._reserved_slots,
            "tracked_tasks": len(self._task_states),
            "total_browsers": self.thread_count,
            "memory_mb": mem_mb,
            "restart_threshold": BROWSER_RESTART_THRESHOLD,
        })

    async def index(self):
        """健康检查页面"""
        raw_available = self.browser_pool.qsize()
        effective_available = self._effective_available_browsers()
        return jsonify({
            "status": "running",
            "browser_type": self.browser_type,
            "available_browsers": effective_available,
            "raw_available_browsers": raw_available,
            "reserved_slots": self._reserved_slots,
            "total_browsers": self.thread_count,
            "usage": "GET /turnstile?url=<url>&sitekey=<sitekey>",
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
