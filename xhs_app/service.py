# -*- coding: utf-8 -*-
"""应用服务：把 resolver+collector 串成一次抓取任务（在工作线程内执行）

三种输入逻辑（capture_job）：
1) 纯数字小红书号 -> 站内搜索解析 -> 打开作者主页 -> 捕获作品列表
2) /user/profile/ 主页链接 -> 打开主页 -> 捕获列表；其他 http(s) 链接 -> 仅在内嵌浏览器打开
3) 输入为空        -> 按当前内嵌浏览器所在页面抓取（须是作者主页）
"""
import random
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from curl_cffi import requests as creq

from xhs_app import config
from xhs_app.collector import fill_meta_from_dom, open_and_collect
from xhs_app.detail import detail_from_html, fetch_detail
from xhs_app.download import build_target, ext_for, has_existing, remove_prefix
from xhs_app.models import AppError, LoginRequired, ProfileMeta
from xhs_app.queue import DONE, FAILED, QUEUED, RUNNING, SKIPPED, TaskItem
from xhs_app.resolver import resolve


def meta_from_current(page) -> ProfileMeta:
    """按当前页面构造目标（须为作者主页）"""
    url = page.url or ""
    if "/user/profile/" not in url:
        # 先判断“当前在什么页面”，给可操作的引导，而不是笼统报错
        low = url.lower()
        guide = "请先在左侧浏览器打开一位作者的「主页」（头像/作品列表页），再点抓取。"
        if not url:
            guide = "内嵌浏览器尚未打开页面。" + guide
        elif "/login" in low:
            guide = "当前停在登录页，请先扫码登录。" + guide
        elif "/search_result" in low:
            guide = "当前在搜索结果页（不是作者主页）。" + guide
        elif "/explore" in low or "feed" in low:
            guide = "当前在首页信息流（不是作者主页）。" + guide
        else:
            guide = "当前是笔记/其他页面，不是作者主页。" + guide
        raise AppError(guide)
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
        # 失败信息统一由 UI 的 done 处理器输出一次，避免重复打两遍
        bridge.done.emit(None, "需要登录：" + str(e) + " 请在内嵌浏览器中登录后再点抓取。")
    except AppError as e:
        bridge.done.emit(None, str(e))
    except Exception as e:
        traceback.print_exc()
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


# ============================================================
# 并发下载：HTTP 通道 + 后台任务队列
# 详情与文件都走 curl_cffi 直连（带浏览器 Cookie 快照），不再导航
# 可见浏览器页 → 下载期间左侧可继续浏览/抓取/追加任务。
# ============================================================

def cookie_header_from_dp(dp) -> str:
    """从已挂接的内嵌引擎读取 xiaohongshu 域 Cookie，拼成 HTTP Cookie 头。"""
    parts = []
    try:
        for c in dp.cookies():
            if not isinstance(c, dict):
                continue
            dom = str(c.get("domain") or "")
            if "xiaohongshu.com" in dom and c.get("name") and c.get("value"):
                parts.append(f"{c['name']}={c['value']}")
    except Exception:
        pass
    return "; ".join(parts)


def fetch_detail_http(cookie: str, note):
    """HTTP 直取笔记页 SSR 并解析媒体（借 detail_from_html），不占可见浏览器。

    返回 {'kind': 'image'|'video', 'urls': [...]}；登录失效抛 LoginRequired。
    """
    from urllib.parse import quote

    base = f"{config.HOME}/explore/{note.note_id}"
    cands = []
    if note.xsec_token:
        cands.append(base + f"?xsec_token={quote(note.xsec_token)}&xsec_source=pc_profile")
    cands.append(base)
    headers = {"Referer": config.HOME + "/", "User-Agent": config.UA, "Cookie": cookie}
    last = None
    for url in cands:
        try:
            r = creq.get(url, headers=headers, impersonate="chrome", timeout=20)
            r.raise_for_status()
            html = r.text or ""
        except Exception as e:
            last = e
            continue
        if "/login" in (r.url or ""):
            raise LoginRequired("会话已过期：详情请求被重定向到登录页，需要重新登录")
        got = detail_from_html(html)
        if got:
            kind, urls = got
            return {"kind": kind, "urls": urls}
        last = "页面未包含 SSR 数据"
    raise AppError(f"HTTP 获取详情失败（可能需登录或被风控）：{last}")


def run_download_task(task: TaskItem, cookie: str, log=None):
    """执行单个下载任务（纯 HTTP，不含可见浏览器）。返回 (state, msg)。

    登录失效时抛 LoginRequired，由队列引擎统一暂停调度。
    """
    meta, note, target_dir = task.meta, task.note, task.target_dir
    try:
        fd = fetch_detail_http(cookie, note)
    except LoginRequired:
        raise
    except AppError as e:
        if log:
            log(f"[{note.note_id}] 详情失败：{e}")
        return FAILED, str(e)
    except Exception as e:
        traceback.print_exc()
        return FAILED, f"内部错误：{type(e).__name__}: {e}"

    folder = build_target(meta, note, target_dir)
    if fd["kind"] == "image":
        total = len(fd["urls"])
        width = max(2, len(str(total)))
        tasks = [(u, folder, f"{i:0{width}d}", f"图 {i}/{total}")
                 for i, u in enumerate(fd["urls"], 1)]
        label = "张图"
    else:
        tasks = [(fd["urls"][0], folder, "视频", "视频")]
        label = "视频"

    if all(has_existing(folder, t[2]) for t in tasks):
        if log:
            log(f"[{note.note_id}] 已存在，跳过：{folder.name}")
        return SKIPPED, "文件已存在，自动跳过"

    def work(t):
        time.sleep(random.uniform(0.05, 0.35))  # 文件级小限速，避免突发
        url, fld, nm, disp = t
        if log:
            log(f"[{note.note_id}] {disp}")
        try:
            return _ensure_file(url, fld, nm)
        except Exception as e:
            if log:
                log(f"[{note.note_id}] {disp}失败：{type(e).__name__}: {e}")
            return "fail"

    workers = max(1, min(config.DOWNLOAD_WORKERS, len(tasks)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(work, tasks))
    new_n = results.count("new")
    fail_n = results.count("fail")
    if fail_n:
        return FAILED, f"失败 {fail_n}/{len(tasks)} 个文件"
    if new_n == 0:
        return SKIPPED, "文件已存在，自动跳过"
    if log:
        log(f"[{note.note_id}] 完成 {new_n} 个{label}")
    return DONE, f"完成 {new_n} 个{label}"


class DownloadQueue:
    """后台下载队列：<=max_workers 个线程并发消费 TaskItem。

    provider() 每次取任务时返回 Cookie 头；为空/登录失效则暂停队列，
    剩余任务保留为 queued 待「继续」。sink 需要线程安全地回调主线程：
    - sink.task_update(task, state, msg)
    - sink.notice(msg)（可选）
    - sink.log(msg)（可选）
    """

    def __init__(self, provider, sink, max_workers=2):
        import queue as _q
        self._q = _q.Queue()
        self._provider = provider
        self._sink = sink
        self._max = max_workers
        self._threads = []
        self._halt = False
        self._lock = threading.Lock()
        self._busy = 0

    # ---- 对外状态 ----
    def pending(self) -> int:
        return self._q.qsize()

    def busy(self) -> int:
        with self._lock:
            return self._busy

    def is_halted(self) -> bool:
        with self._lock:
            return self._halt

    # ---- 任务操作 ----
    def enqueue(self, tasks):
        """把一批任务加入队列并确保 worker 运行。"""
        for t in tasks:
            self._q.put(t)
        self._ensure()

    def pause(self):
        """暂停：当前文件下载完即不再启动新任务。"""
        with self._lock:
            self._halt = True

    def resume(self):
        """继续：清暂停标记并拉起空闲 worker。"""
        with self._lock:
            self._halt = False
        self._ensure()

    def _ensure(self):
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            if self._halt or self._q.empty():
                return
            while len(self._threads) < self._max:
                t = threading.Thread(target=self._work, daemon=True)
                t.start()
                self._threads.append(t)

    def _work(self):
        while True:
            if self._halt:
                return
            try:
                task = self._q.get(timeout=0.5)
            except Exception:
                if self._halt:
                    return
                # 队列空了就退出，等待新任务时由 enqueue 再拉起
                return
            with self._lock:
                self._busy += 1
            try:
                self._run(task)
            finally:
                with self._lock:
                    self._busy -= 1
                self._q.task_done()

    def _run(self, task: TaskItem):
        log = getattr(self._sink, "log", None)
        self._sink.task_update(task, RUNNING, "获取详情…")
        cookie = self._provider()
        if not cookie:
            self._sink.task_update(task, FAILED,
                                   "会话过期：未读到登录 Cookie，请在内嵌浏览器重新登录")
            with self._lock:
                self._halt = True
            notice = getattr(self._sink, "notice", None)
            if notice:
                notice("下载已暂停：登录态丢失，请重新登录后点「继续」")
            return
        try:
            state, msg = run_download_task(task, cookie, log=log)
        except LoginRequired:
            state, msg = FAILED, "会话过期：需要重新登录"
            with self._lock:
                self._halt = True
            notice = getattr(self._sink, "notice", None)
            if notice:
                notice("下载已暂停：会话过期，请重新登录后点「继续」")
        except Exception as e:
            traceback.print_exc()
            state, msg = FAILED, f"内部错误：{type(e).__name__}: {e}"
        self._sink.task_update(task, state, msg)
