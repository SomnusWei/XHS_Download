# -*- coding: utf-8 -*-
"""DetailFetcher：打开笔记页，从 SSR `window.__INITIAL_STATE__` 解析原图/视频流

借鉴 XHS-Downloader 的做法（curl 直取 HTML + PyYAML 解析 SSR，绕开 x-s）：
- 页面 <script> 中含 `window.__INITIAL_STATE__= {...}`；
- 清洗（去控制符、`new Map([])`→`[]`、`undefined`→`null`）后按 YAML 解析；
- 沿 `note.noteDetailMap -> 首项 -> note` 取完整详情（imageList / video.media.stream）。
我们用内嵌引擎打开笔记页（自动带登录态与 xsec_token），读取同一份 SSR 数据。
"""
import re
import time

import yaml

from xhs_app import config
from xhs_app.models import AppError

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _get_initial_script(html: str) -> str:
    if not html:
        return ""
    # SSR 脚本通常形如 <script>window.__INITIAL_STATE__={...};<\/script>
    for m in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.S):
        txt = m.group(1).strip()
        if txt.startswith("window.__INITIAL_STATE__"):
            return txt
    return ""


def _to_dict(text: str) -> dict:
    cleaned = _CTRL.sub("", text.lstrip("window.__INITIAL_STATE__=").rstrip().rstrip(";"))
    cleaned = cleaned.replace("new Map([])", "[]").replace("undefined", "null")
    try:
        return yaml.safe_load(cleaned) or {}
    except Exception:
        # YAML 偶发失败再尝试 JSON
        import json
        try:
            return json.loads(cleaned) or {}
        except Exception:
            return {}


def _deep_get(data, keys):
    """按 ['note','noteDetailMap','[-1]','note'] 式路径取值；dict 用 value 序当索引"""
    cur = data
    for k in keys:
        if k.startswith("[") and k.endswith("]"):
            idx = int(k[1:-1])
            if isinstance(cur, dict):
                vals = list(cur.values())
                cur = vals[idx] if 0 <= idx < len(vals) else None
            elif isinstance(cur, (list, tuple)):
                cur = cur[idx] if 0 <= idx < len(cur) else None
            else:
                cur = None
        elif isinstance(cur, dict):
            cur = cur.get(k)
        else:
            return None
        if cur is None:
            return None
    return cur


def _find_detail(obj):
    """兜底：深度找第一个含 imageList/video 的完整详情对象"""
    if isinstance(obj, dict):
        if (obj.get("imageList") and isinstance(obj.get("imageList"), list)) or obj.get("video"):
            return obj
        for v in obj.values():
            r = _find_detail(v)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_detail(v)
            if r is not None:
                return r
    return None


def detail_from_html(html: str):
    """html -> (kind, urls)。kind: image / video"""
    data = _to_dict(_get_initial_script(html))
    if not data:
        return None
    detail = _deep_get(data, ["note", "noteDetailMap", "[-1]", "note"])
    if not isinstance(detail, dict) or not (detail.get("imageList") or detail.get("video")):
        detail = _find_detail(data)
    if not isinstance(detail, dict):
        return None
    return extract_detail(detail)


def extract_detail(detail: dict):
    """从完整 note 详情对象提取媒体：视频优先（type=video/含流），否则原图列表"""
    video = detail.get("video") or {}
    consumer = video.get("consumer") or {}
    stream = (video.get("media") or {}).get("stream") or {}
    items = []
    if isinstance(stream, dict):
        for _k, arr in stream.items():
            if isinstance(arr, list):
                items.extend(x for x in arr if isinstance(x, dict))
    origin = consumer.get("originVideoKey") or consumer.get("originalVideoKey")
    is_video = (detail.get("type") == "video") or bool(origin) or bool(items)
    if is_video:
        # 原片地址优先
        if origin:
            return "video", [f"https://sns-video-bd.xhscdn.com/{origin}"]
        if items:
            def h(x):
                try:
                    return int(x.get("height") or 0)
                except (TypeError, ValueError):
                    return 0
            items.sort(key=h)
            top = items[-1]
            backups = top.get("backupUrls") or []
            url = backups[0] if backups else top.get("masterUrl")
            if url:
                return "video", [url]
    images = detail.get("imageList") or []
    if isinstance(images, list) and images:
        urls = [clean_image_url(it) for it in images if isinstance(it, dict)]
        urls = [u for u in urls if u]
        if urls:
            return "image", urls
    raise AppError("详情中未解析到图片或视频（可能为仅自己可见/特殊类型）")


def clean_image_url(item: dict) -> str:
    """取原图：仿 XHS-Downloader，把 token 重建到 sns-img-bd CDN（直连 403 的 webpic 原样 URL 不可用）"""
    raw = item.get("urlDefault") or item.get("url") or ""
    token = "/".join(raw.split("/")[5:]).split("!")[0]
    if token and not token.startswith(("http", "/")):
        return f"https://sns-img-bd.xhscdn.com/{token}"
    return raw.split("!")[0] if raw.startswith("http") else raw


def ssr_parse(html: str) -> dict:
    """解析页面 SSR __INITIAL_STATE__（供列表/详情兜底复用）"""
    if not html:
        return {}
    return _to_dict(_get_initial_script(html))


def ssr_deep_get(data, keys):
    return _deep_get(data, keys)


def fetch_detail(dp, note):
    """打开笔记页并解析 SSR 详情，返回 {'kind','urls'}"""
    from xhs_app.models import LoginRequired

    base = f"{config.HOME}/explore/{note.note_id}"
    urls_try = []
    if note.xsec_token:
        from urllib.parse import quote
        urls_try.append(base + f"?xsec_token={quote(note.xsec_token)}&xsec_source=pc_profile")
    urls_try.append(base)

    for url in urls_try:
        dp.get(url)
        end = time.time() + 10
        while time.time() < end:
            try:
                html = dp.html or ""
            except Exception:
                html = ""
            got = detail_from_html(html)
            if got:
                kind, urls = got
                return {"kind": kind, "urls": urls}
            time.sleep(1.2)
        cur = (dp.url or "").lower()
        if "/login" in cur:
            raise LoginRequired("会话已过期，详情获取被重定向到登录页")
    raise AppError("未在页面初始数据中找到该笔记详情（可能已删除或无权限），可稍后重试")
