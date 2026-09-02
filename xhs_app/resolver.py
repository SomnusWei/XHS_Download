# -*- coding: utf-8 -*-
"""UserResolver：小红书号或主页链接 -> 可打开的 profile 链接（user_id + xsec_token）

2026-09 实测要点：
- 纯数字 ID 直开 /user/profile/{id} 会被 XHS 弹登录墙（缺 xsec_token）。
- 解法：站内搜索页 search_result?keyword={数字} 会返回带「小红书号：{数字}」标签的
  用户卡片，其链接含 xsec_token，可作为可靠入口。
- 链接输入：取 URL 原样（含 xsec_token 才能直开）。
"""
import re
import time

from xhs_app.config import HOME
from xhs_app.models import AppError, LoginRequired, ProfileMeta


def normalize(raw: str) -> str:
    s = (raw or "").strip().strip('"').strip("'")
    s = s.replace("小红书号：", "").replace("小红书号:", "")
    return s


def extract_href(raw: str):
    """若输入是主页链接则原样提取（含参数），否则返回 None"""
    s = normalize(raw)
    if "xiaohongshu.com/user/profile/" in s or s.startswith("/user/profile/"):
        if s.startswith("/"):
            s = HOME + s
        return s
    return None


def _wait_load(page, sec):
    time.sleep(sec)


def find_user_card(page, kw: str, log=None):
    """在 search_result 页里找小红书号==kw 的用户卡片，返回其 profile 链接"""
    try:
        anchors = page.eles("css:a[href*='user/profile'][href*='xsec']", timeout=8)
    except Exception:
        anchors = []
    for a in anchors:
        txt = ""
        try:
            txt = a.text or ""
        except Exception:
            pass
        if f"小红书号：{kw}" in txt or f"小红书号:{kw}" in txt:
            href = ""
            try:
                href = a.attr("href") or ""
            except Exception:
                pass
            return href
    return None


def resolve_by_number(page, kw: str, log=None) -> str:
    """通过站内搜索解析数字小红书号 -> 带 xsec_token 的主页链接"""
    if log:
        log(f"站内搜索小红书号 {kw} …")
    page.get(f"{HOME}/search_result?keyword={kw}&source=web_explore_feed")
    _wait_load(page, 6)
    if "/login" in (page.url or "").lower():
        raise LoginRequired("会话已过期：被重定向到登录页，需要重新扫码登录")
    href = find_user_card(page, kw, log)
    if not href:
        raise AppError(f"未找到小红书号为 {kw} 的用户。可改为粘贴该用户主页链接（从浏览器复制，含 xsec_token）。")
    return href if href.startswith("http") else HOME + href


def resolve(page, raw: str, log=None) -> ProfileMeta:
    """输入(小红书号/主页链接) -> ProfileMeta"""
    s = normalize(raw)
    if not s:
        raise AppError("请输入小红书号或主页链接")

    m = re.search(r"/user/profile/([0-9A-Za-z]+)", s)
    user_id = m.group(1) if m else ""

    href = extract_href(s)
    if href:
        meta = ProfileMeta(user_id=user_id, href=href, source="link")
        # 从 URL 参数里取数字段作潜在校验位
        q = re.search(r"[?&]keyword=([0-9]+)", href)
        if q:
            meta.kw = q.group(1)
        if log:
            log("识别为主页链接，将直接打开主页…")
        return meta

    # 纯数字 -> 搜索解析
    if not re.fullmatch(r"\d{4,20}", s):
        raise AppError("无法识别输入：请粘贴 xiaohongshu.com/user/profile/ 开头的链接，或纯数字小红书号")
    href = resolve_by_number(page, s, log)
    return ProfileMeta(user_id="", href=href, kw=s, source="number")


def read_profile_dom(page) -> dict:
    """尽力从主页 DOM 读取昵称/小红书号/作品数（失败返回空）"""
    out = {"nickname": "", "red_id": "", "note_total": 0}
    try:
        t = page.html or ""
    except Exception:
        t = ""
    if not t:
        return out
    m = re.search(r"小红书号[:：]\s*([0-9]+)", t)
    if m:
        out["red_id"] = m.group(1)
    return out
