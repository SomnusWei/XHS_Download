# -*- coding: utf-8 -*-
"""应用服务：把 resolver+collector 串成一次抓取任务（在工作线程内执行）

三种输入逻辑（capture_job）：
1) 纯数字小红书号 -> 站内搜索解析 -> 打开作者主页 -> 捕获作品列表
2) /user/profile/ 主页链接 -> 打开主页 -> 捕获列表；其他 http(s) 链接 -> 仅在内嵌浏览器打开
3) 输入为空        -> 按当前内嵌浏览器所在页面抓取（须是作者主页）
"""
import random
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from curl_cffi import requests as creq

from xhs_app import config
from xhs_app.collector import fill_meta_from_dom, open_and_collect
from xhs_app.detail import fetch_detail
from xhs_app.download import build_target, ext_for, has_existing, remove_prefix
from xhs_app.models import AppError, LoginRequired, ProfileMeta
from xhs_app.resolver import resolve


def meta_from_current(page) -> ProfileMeta:
    """按当前页面构造目标（须为作者主页）"""
    url = page.url or ""
    if "/user/profile/" not in url:
        raise AppError(
            "当前页面不是作者主页。请在左侧浏览器打开作者主页后再抓，"
            "或在上方输入小红书号 / 主页链接。")
    m = re.search(r"/user/profile/([0-9A-Za-z]+)", url)
    return ProfileMeta(user_id=m.group(1) if m else "", href=url, source="current")


def capture_job(raw, bridge, attach):
    """执行一次抓取（attach：内嵌引擎挂接函数，在工作线程内调用）"""
    dp = None
    try:
        dp = attach()
        raw = (raw or "").strip().strip('"').strip("'")
        bridge.log.emit("已挂接内嵌浏览器。")
        if not raw:
            bridge.log.emit("输入为空：抓取当前内嵌页面（作者主页）…")
            meta = meta_from_current(dp)
            result = open_and_collect(dp, meta, bridge.log.emit)
            fill_meta_from_dom(dp, result.meta)
            bridge.done.emit(result, None)
            return
        if re.fullmatch(r"\d{4,20}", raw):
            bridge.log.emit(f"按小红书号 {raw} 解析并抓取…")
            meta = resolve(dp, raw, bridge.log.emit)
            result = open_and_collect(dp, meta, bridge.log.emit)
            fill_meta_from_dom(dp, result.meta)
            bridge.done.emit(result, None)
            return
        if raw.lower().startswith(("http://", "https://")):
            if "/user/profile/" in raw:
                bridge.log.emit("打开主页链接并抓取…")
                meta = resolve(dp, raw, bridge.log.emit)
                result = open_and_collect(dp, meta, bridge.log.emit)
                fill_meta_from_dom(dp, result.meta)
                bridge.done.emit(result, None)
                return
            bridge.log.emit("打开链接：" + raw)
            dp.get(raw)
            bridge.done.emit(None, "")  # 仅打开成功（无结果数据）
            return
        raise AppError("无法识别输入：请输入纯数字小红书号、/user/profile/ 主页链接，或留空抓取当前页面")
    except LoginRequired as e:
        bridge.log.emit(str(e))
        bridge.done.emit(None, "需要登录：" + str(e) + " 请在内嵌浏览器中登录后再点抓取。")
    except AppError as e:
        bridge.log.emit("任务失败：" + str(e))
        bridge.done.emit(None, str(e))
    except Exception as e:
        traceback.print_exc()
        bridge.log.emit(f"内部错误：{type(e).__name__}: {e}")
        bridge.done.emit(None, f"内部错误：{type(e).__name__}: {e}")


def _save_file(url: str, dest_dir, name: str):
    """流式下载并按真实 content-type 修正扩展名（curl_cffi 模拟 Chrome 指纹，CDN 免 Cookie）"""
    r = creq.get(url, headers={"Referer": config.HOME + "/", "User-Agent": config.UA},
                 impersonate="chrome", timeout=120, stream=True)
    try:
        r.raise_for_status()
        ext = ext_for(dict(r.headers), url)
        path = dest_dir / f"{name}{ext}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
    finally:
        r.close()
    if path.stat().st_size == 0:
        path.unlink(missing_ok=True)
        raise RuntimeError("下载到 0 字节")
    return path.name


def _ensure_file(url: str, dest_dir, name: str):
    """单文件：已存在→'exist'；否则带重试下载→'new'；重试仍失败则抛异常（半成品先清理）"""
    if has_existing(dest_dir, name):
        return "exist"
    last = None
    for attempt in range(1, config.MAX_ATTEMPTS + 1):
        remove_prefix(dest_dir, name)
        try:
            _save_file(url, dest_dir, name)
            return "new"
        except Exception as e:
            last = e
            if attempt < config.MAX_ATTEMPTS:
                time.sleep(config.RETRY_BASE * attempt)
    remove_prefix(dest_dir, name)
    raise RuntimeError(f"下载失败（已重试 {config.MAX_ATTEMPTS} 次）：{last}")


def _download_one_note(dp, meta, note, target_dir, bridge):
    """下载单篇（含详情），返回状态串：完成/已存在(跳过)/详情失败/失败/中止"""
    bridge.note_update.emit(note.note_id, "获取详情…")
    try:
        fd = fetch_detail(dp, note)
    except LoginRequired:
        return "中止"
    except AppError as e:
        bridge.log.emit(f"[{note.note_id}] 详情失败：{e}")
        return "详情失败"

    folder = build_target(meta, note, target_dir)
    if fd["kind"] == "image":
        total = len(fd["urls"])
        width = max(2, len(str(total)))
        tasks = [(u, folder, f"{i:0{width}d}", f"图 {i}/{total}") for i, u in enumerate(fd["urls"], 1)]
        label = "张图"
    else:
        tasks = [(fd["urls"][0], folder, "视频", "视频")]
        label = "视频"

    # 全部已存在则整篇跳过（二次运行稳定）
    if all(has_existing(folder, t[2]) for t in tasks):
        bridge.log.emit(f"[{note.note_id}] 已存在，跳过：{folder.name}")
        return "已存在(跳过)"

    def work(t):
        time.sleep(random.uniform(0.05, 0.35))  # 文件级小限速，避免突发
        url, fld, nm, disp = t
        bridge.log.emit(f"[{note.note_id}] {disp}")
        try:
            return _ensure_file(url, fld, nm)
        except Exception as e:
            bridge.log.emit(f"[{note.note_id}] {disp}失败：{type(e).__name__}: {e}")
            return "fail"

    workers = max(1, min(config.DOWNLOAD_WORKERS, len(tasks)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(work, tasks))
    new_n = results.count("new")
    fail_n = results.count("fail")
    if fail_n:
        return "失败(部分)"
    if new_n == 0:
        bridge.log.emit(f"[{note.note_id}] 已存在，跳过：{folder.name}")
        return "已存在(跳过)"
    bridge.log.emit(f"[{note.note_id}] 完成 {new_n} 个{label}")
    return "完成"


def download_job(meta, items, target_dir, bridge, attach):
    """批量下载：逐篇（引擎串行取详情，文件并发限速下载），单文件重试/续传
    bridge 需具备: log / note_update(note_id,status) / notice(msg)
    """
    dp = None
    ok = fail = skip = 0
    try:
        dp = attach()
        bridge.log.emit(f"开始批量下载 {len(items)} 篇 → {target_dir}（并发 {config.DOWNLOAD_WORKERS}）")
        for idx, note in enumerate(items):
            if idx:
                time.sleep(random.uniform(0.5, 1.5))  # 篇间随机限速
            status = _download_one_note(dp, meta, note, target_dir, bridge)
            if status == "中止":
                bridge.log.emit("会话过期，已中止批量下载。请在内嵌浏览器重新登录后再试。")
                bridge.notice.emit("下载中止：会话过期，请重新登录后重试")
                return
            bridge.note_update.emit(note.note_id, status)
            if status == "完成":
                ok += 1
            elif status == "已存在(跳过)":
                skip += 1
            else:
                fail += 1
        bridge.notice.emit(f"批量下载完成：成功 {ok}，跳过 {skip}，失败 {fail}")
    except LoginRequired:
        bridge.log.emit("会话过期，下载中止。")
        bridge.notice.emit("下载中止：会话过期，请重新登录后重试")
    except Exception as e:
        traceback.print_exc()
        bridge.notice.emit(f"批量下载异常：{type(e).__name__}: {e}")
