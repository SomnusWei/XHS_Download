# -*- coding: utf-8 -*-
"""ListCollector：监听 user_posted + 滚动分页，解析为 NoteItem 列表

2026-09 实测注意事项：
- 必须在 page.get(主页) 之前 listen.start，分页链条才不会丢。
- 滚动采用“单次较大步长 + 停顿”节奏（探针实证可稳定拉到全量；
  过快的小幅连续滚动易触发风控导致服务端提前返回 has_more=false）。
- 页面 DOM 读写放在采集之后进行，避免干扰懒加载。
"""
import json
import re
import time

from xhs_app import config, detail as detail_mod
from xhs_app.models import (
    AppError, CollectResult, LoginRequired, parse_note, parse_notes_body,
)


def notes_from_profile_ssr(tab):
    """小作者主页由 SSR 直接渲染（不发 user_posted）→ 从 __INITIAL_STATE__.user.notes 兜底解析"""
    try:
        html = tab.html or ""
    except Exception:
        return []
    if not html or "__INITIAL_STATE__" not in html:
        return []
    data = detail_mod.ssr_parse(html)
    if not data:
        return []
    pages = detail_mod.ssr_deep_get(data, ["user", "notes"])
    out, seen = [], set()
    if isinstance(pages, list):
        for page in pages:
            if not isinstance(page, list):
                continue
            for it in page:
                if not isinstance(it, dict) or not it.get("id"):
                    continue
                card = it.get("noteCard") or {}
                raw = {
                    "note_id": it.get("id"),
                    "type": card.get("type") or "",
                    "display_title": card.get("displayTitle") or card.get("title") or "",
                    "cover": card.get("cover") or {},
                    "interact_info": card.get("interactInfo") or {},
                    "xsec_token": it.get("xsecToken") or it.get("xsec_token") or "",
                }
                n = parse_note(raw)
                if n and n.note_id not in seen:
                    seen.add(n.note_id)
                    out.append(n)
    return out


def _drain(tab, timeout=0.7, cap=800):
    """取队列中已捕获的数据包；空闲 timeout 秒即返回"""
    got = []
    try:
        for p in tab.listen.steps(timeout=timeout):
            got.append(p)
            if len(got) >= cap:
                break
    except Exception:
        pass
    return got


def collect(tab, meta, log=None, on_batch=None) -> CollectResult:
    """在已打开主页的 tab 上滚动监听，返回 CollectResult"""
    if log:
        log("开始捕获作品列表（自动滚动加载）…")

    notes, seen = [], set()
    empty_rounds = 0
    has_more = True
    risk_logged = False
    ssr_mode = False

    def handle_body(body):
        nonlocal has_more, empty_rounds, risk_logged
        if isinstance(body, dict) and body.get("code") not in (0, None):
            if not risk_logged:
                risk_logged = True
                if log:
                    log(f"  user_posted 异常响应 code={body.get('code')} msg={body.get('msg')}"
                        f"（若持续触发说明被风控：请降速或稍后再试/人工过滑块）")
        items, has_more, _cursor = parse_notes_body(body)
        added = [it for it in items if it.note_id not in seen]
        for it in added:
            seen.add(it.note_id)
        notes.extend(added)
        if added:
            empty_rounds = 0
            if on_batch:
                on_batch(added)
            if log:
                log(f"  本页 +{len(added)} 条，累计 {len(notes)} 条")
            return True
        return False

    # 初始收一遍（首屏 user_posted 已入队）
    for p in _drain(tab, timeout=0.3):
        try:
            handle_body(p.response.body)
        except Exception:
            pass

    # 小作者主页 SSR 直接渲染、不发 user_posted → 直接解析 SSR 兜底
    if not notes:
        ssr_notes = notes_from_profile_ssr(tab)
        if ssr_notes:
            notes.extend(ssr_notes)
            for n in ssr_notes:
                seen.add(n.note_id)
            ssr_mode = True
            has_more = False
            if on_batch:
                on_batch(ssr_notes)
            if log:
                log(f"  页面 SSR 渲染 {len(ssr_notes)} 篇（无网络分页请求），采用 SSR 列表")

    if not ssr_mode:
        for _ in range(config.SCROLL_ROUNDS):
            if len(notes) >= config.MAX_NOTES:
                break
            try:
                tab.scroll.down(2400)
            except Exception:
                pass
            time.sleep(1.6)
            got_any = False
            for p in _drain(tab, timeout=0.7):
                try:
                    got_any = handle_body(p.response.body) or got_any
                except Exception:
                    pass
            if not got_any:
                empty_rounds += 1
                if empty_rounds >= config.EMPTY_STOP:
                    break

        # 兜底：直接到底再收一轮
        try:
            tab.scroll.to_bottom()
        except Exception:
            pass
        time.sleep(1.2)
        for p in _drain(tab, timeout=0.8):
            try:
                handle_body(p.response.body)
            except Exception:
                pass

    # 真·空兜底：SSR 里可能换页结构不同，再扫一次 user.notes
    if not notes:
        ssr_notes = notes_from_profile_ssr(tab)
        if ssr_notes:
            notes.extend(ssr_notes)
            ssr_mode = True
            has_more = False
            if log:
                log(f"  页面 SSR 解析到 {len(ssr_notes)} 篇")

    meta.note_total = len(notes)
    reason = ""
    if ssr_mode and notes:
        reason = "已加载全部作品（SSR 渲染列表）"
    elif len(notes) >= config.MAX_NOTES:
        reason = f"达到本次上限 {config.MAX_NOTES} 条（完整拉取在后续阶段完善）"
    elif not has_more:
        reason = "已加载全部作品"
    elif notes:
        reason = "已停止加载"
    else:
        reason = "未获取到作品（可能被风控或主页为空）"
    return CollectResult(meta=meta, notes=notes, stopped=reason)


def open_and_collect(page, meta, log=None, on_batch=None):
    """打开 profile 链接 -> 采集列表（DOM 校验留给调用方在采集后做）"""
    url = meta.href
    if log:
        log("打开作者主页…")
    try:
        page.listen.start(config.USER_POSTED)  # 先监听再导航
    except Exception as e:
        raise AppError(f"启动网络监听失败：{e}")
    page.get(url)
    time.sleep(6)

    cur = (page.url or "").lower()
    if "/login" in cur:
        has_xsec = "xsec_token=" in (meta.href or "")
        if has_xsec:
            raise LoginRequired("会话已过期：主页被重定向到登录页，需要重新登录")
        raise AppError("该主页链接缺少 xsec_token，直开会被平台要求登录。请从浏览器复制含 xsec_token 的完整主页链接。")
    return collect(page, meta, log, on_batch)


def fill_meta_from_dom(page, meta):
    """采集结束后回读作者信息（不干扰采集）"""
    html = ""
    try:
        html = page.html or ""
    except Exception:
        pass
    if html:
        m = re.search(r"小红书号[:：]\s*([0-9A-Za-z]+)", html)
        if m:
            meta.red_id = m.group(1)
        # 昵称：优先取主页标题
        tm = re.search(r"<title>(.*?)</title>", html, re.S)
        if tm:
            nick = re.sub(r"\s*[-–—_]\s*.*$|的主页.*$| - 小红书.*$", "", tm.group(1)).strip()
            nick = re.sub(r"\s+", " ", nick)
            if nick and not nick.lower().startswith("小红书"):
                meta.nickname = nick
    if not meta.user_id:
        m = re.search(r"/user/profile/([0-9A-Za-z]+)", page.url or "")
        if m:
            meta.user_id = m.group(1)
    return meta


def save_sample(notes, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump([n.__dict__ for n in notes[:3]], f, ensure_ascii=False, indent=1)
