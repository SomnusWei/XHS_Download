# -*- coding: utf-8 -*-
"""DrissionPage / Edge 会话工具

- 默认静默模式：窗口被定位到屏幕外（-32000,-32000），对用户不可见但仍“有头”渲染
  （避免 headless 被平台风控识别 / 页面后台节流）。登录续期时用可见窗口。
- 同一 `user_data` profile 持久化登录态：cookie 落在磁盘，跨进程自动续用。
- 会话过期识别：搜索/主页被重定向到 /login 即抛 LoginRequired，由上层弹可见窗口续期。
"""
import time

from xhs_app import config

STEALTH_ARGS = (
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
    "--disable-blink-features=AutomationControlled",
    "--remote-allow-origins=*",
    "--window-size=1366,900",
)


def make_page(hidden=None):
    """启动 Edge。hidden=None 时取 config.HIDDEN；登录续期传 hidden=False 弹可见窗口"""
    from DrissionPage import ChromiumOptions, ChromiumPage

    if hidden is None:
        hidden = config.HIDDEN
    co = ChromiumOptions()
    co.set_browser_path(config.EDGE_PATH)
    co.set_user_data_path(str(config.PROFILE_DIR))
    co.set_local_port(config.DEBUG_PORT)
    for a in STEALTH_ARGS:
        co.set_argument(a)
    if hidden:
        # 静默：窗口渲染保持“有头”（避免 headless 被平台识别/页面后台节流），
        # 但把窗口定位到屏幕外，对用户不可见。
        co.set_argument("--window-position=-32000,-32000")
        co.set_argument("--mute-audio")
    return ChromiumPage(co)


def wait_login(page, timeout=300, log=None):
    """页面停在登录页，轮询等待用户扫码后 XHS 自动跳离 /login。

    返回 True 表示登录成功（url 连续两次采样均离开登录页）。
    """
    end = time.time() + timeout
    stable = 0
    while time.time() < end:
        time.sleep(2.5)
        try:
            url = (page.url or "").lower()
        except Exception:
            continue
        if "/login" not in url:
            stable += 1
            if stable >= 2:
                if log:
                    log("检测到登录成功（已离开登录页）。")
                return True
        else:
            stable = 0
    return False


def browser_cookie_header(page) -> str:
    """拼 Cookie 头，供 httpx 下载媒体用（M2 起）"""
    parts = []
    try:
        for c in page.cookies():
            if c.get("name") and c.get("value") and c.get("domain") and "xiaohongshu" in str(c.get("domain")):
                parts.append(f"{c['name']}={c['value']}")
    except Exception:
        pass
    return "; ".join(parts)
