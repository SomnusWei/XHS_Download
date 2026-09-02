# -*- coding: utf-8 -*-
"""主界面：左侧内嵌浏览器，右侧输入/抓取/数据列表/统计/日志

抓取三逻辑：
- 输入纯数字小红书号  -> 搜索解析该作者主页并抓取列表
- 输入 /user/profile/ 链接 -> 打开该主页并抓取；其他 http 链接 -> 仅在内嵌浏览器打开
- 输入为空            -> 抓取当前内嵌浏览器所在的作者主页
"""
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QCursor, QDesktopServices, QIcon, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QDialog, QFileDialog, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from xhs_app import config
from xhs_app.embed import EngineView
from xhs_app.models import AppError, fmt_like
from xhs_app.queue import (
    CANCELLED, DONE, FAILED, QUEUED, RUNNING, SKIPPED,
    TaskItem, load_tasks, save_tasks,
)
from xhs_app.service import (
    DownloadQueue, capture_job, cookie_header_from_dp, note_id_from_url,
    single_note_from_current,
)

THUMB = 56
KIND_COLOR = {"图文": QColor(23, 158, 82), "视频": QColor(255, 92, 30)}


class JobBridge(QObject):
    """工作线程 -> UI 的信号桥"""
    log = Signal(str)
    done = Signal(object, str)              # (CollectResult|None, error|None)；error="" 表示“仅打开页面”成功
    note_update = Signal(str, str)          # (note_id, 状态文本)
    notice = Signal(str)                    # 下载类任务的结束/异常提示


class LoginProbe(QObject):
    """登录态探测结果（工作线程 -> 主线程）"""
    result = Signal(bool)


class ThumbFetcher(QObject):
    """异步封面缩略图下载：有界并发 + 去重，避免几百张封面同时请求抢占内嵌浏览器的网络带宽（卡顿源）"""
    loaded = Signal(str, QPixmap)
    MAX_CONCURRENT = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self._nam = QNetworkAccessManager(self)
        self._nam.finished.connect(self._on_finish)
        self._inflight = {}        # reply -> note_id（正在下载）
        self._queue = []           # [(note_id, url)]（排队中）
        self._seen = set()         # 已入队或下载中的 note_id（去重）
        self._busy = 0

    def get(self, note_id: str, url: str):
        if not url or note_id in self._seen:
            return
        self._seen.add(note_id)
        self._queue.append((note_id, url))
        self._pump()

    def _pump(self):
        while self._busy < self.MAX_CONCURRENT and self._queue:
            note_id, url = self._queue.pop(0)
            req = QNetworkRequest(QUrl(url))
            req.setRawHeader(b"Referer", b"https://www.xiaohongshu.com/")
            req.setRawHeader(b"User-Agent", b"Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
            reply = self._nam.get(req)
            self._inflight[reply] = note_id
            self._busy += 1

    def _on_finish(self, reply: QNetworkReply):
        note_id = self._inflight.pop(reply, None)
        if note_id:
            self._seen.discard(note_id)
            if reply.error() == QNetworkReply.NoError:
                pm = QPixmap()
                if pm.loadFromData(reply.readAll()) and not pm.isNull():
                    pm = pm.scaled(THUMB, THUMB, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    self.loaded.emit(note_id, pm)
        reply.deleteLater()
        self._busy = max(0, self._busy - 1)
        self._pump()


class HoverFetcher(QObject):
    """封面悬停大图：按需低并发下载原预览图（不经 56px 缩略，便于放大查看）"""
    loaded = Signal(str, QPixmap)
    MAX_CONCURRENT = 2
    MAX_SIDE = 900  # 解码后最长边上限，避免超大原图占内存

    def __init__(self, parent=None):
        super().__init__(parent)
        self._nam = QNetworkAccessManager(self)
        self._nam.finished.connect(self._on_finish)
        self._inflight = {}
        self._queue = []
        self._seen = set()
        self._busy = 0

    def get(self, note_id: str, url: str):
        if not url or note_id in self._seen:
            return
        self._seen.add(note_id)
        self._queue.append((note_id, url))
        self._pump()

    def _pump(self):
        while self._busy < self.MAX_CONCURRENT and self._queue:
            note_id, url = self._queue.pop(0)
            req = QNetworkRequest(QUrl(url))
            req.setRawHeader(b"Referer", b"https://www.xiaohongshu.com/")
            req.setRawHeader(b"User-Agent", b"Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
            reply = self._nam.get(req)
            self._inflight[reply] = note_id
            self._busy += 1

    def _on_finish(self, reply: QNetworkReply):
        note_id = self._inflight.pop(reply, None)
        if note_id:
            self._seen.discard(note_id)
            if reply.error() == QNetworkReply.NoError:
                pm = QPixmap()
                if pm.loadFromData(reply.readAll()) and not pm.isNull():
                    side = max(pm.width(), pm.height())
                    if side > self.MAX_SIDE:
                        pm = pm.scaled(
                            self.MAX_SIDE, self.MAX_SIDE, Qt.KeepAspectRatio,
                            Qt.SmoothTransformation)
                    self.loaded.emit(note_id, pm)
        reply.deleteLater()
        self._busy = max(0, self._busy - 1)
        self._pump()


class QueueBridge(QObject):
    """下载队列 worker 线程 -> UI 信号桥（DownloadQueue 的 sink）"""
    task_state = Signal(object, str, str)   # (TaskItem, state, msg)
    sig_notice = Signal(str)
    sig_log = Signal(str)

    def task_update(self, task, state, msg):
        self.task_state.emit(task, state, msg)

    def notice(self, msg):
        self.sig_notice.emit(msg)

    def log(self, msg):
        self.sig_log.emit(msg)


class SingleBridge(QObject):
    """单篇笔记下载 worker -> UI 信号桥"""
    result = Signal(object, object, str)   # (ProfileMeta|None, NoteItem|None, err)
    log = Signal(str)


class HistoryDialog(QDialog):
    """抓取记录弹窗：用户名 | 小红书号 | 主页链接"""

    def __init__(self, history, parent=None):
        super().__init__(parent)
        self.setWindowTitle("抓取记录")
        self.resize(720, 420)
        lay = QVBoxLayout(self)

        self.lb_cnt = QLabel("")
        self.lb_cnt.setStyleSheet("color:#666;")
        lay.addWidget(self.lb_cnt)

        t = QTableWidget(0, 3)
        t.setHorizontalHeaderLabels(["用户名", "小红书号", "主页链接"])
        t.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setAlternatingRowColors(True)
        lay.addWidget(t, 1)

        for e in history:
            r = t.rowCount()
            t.insertRow(r)
            t.setItem(r, 0, QTableWidgetItem((e.get("nickname") or "").strip() or "—"))
            t.setItem(r, 1, QTableWidgetItem((e.get("red_id") or "").strip() or "—"))
            url_item = QTableWidgetItem((e.get("url") or "").strip())
            url_item.setToolTip(url_item.text())
            t.setItem(r, 2, url_item)
        self._table = t

        bt = QHBoxLayout()
        self.btn_clear = QPushButton("清空记录")
        self.btn_clear.setEnabled(bool(history))
        self.btn_clear.clicked.connect(self._clear)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        bt.addWidget(self.btn_clear)
        bt.addStretch(1)
        bt.addWidget(btn_close)
        lay.addLayout(bt)
        self._cleared = False
        self.lb_cnt.setText(f"共 {len(history)} 条（最新在前）")

    def _clear(self):
        self._table.setRowCount(0)
        self._cleared = True
        self.btn_clear.setEnabled(False)
        self.lb_cnt.setText("记录已清空（关闭弹窗后生效）")

    def is_cleared(self) -> bool:
        return self._cleared


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("小红书作品采集")
        if config.ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(config.ICON_PATH)))
        self.resize(1320, 760)
        self._busy = False
        self._notes = {}
        self._rows = []                              # 与表格行一一对应的 NoteItem
        self._row_of = {}
        self._history = []
        self._bridge = JobBridge()
        self._bridge.log.connect(self._log)
        self._bridge.done.connect(self._on_done)
        self._bridge.note_update.connect(self._on_note_status)
        self._bridge.notice.connect(self._on_notice)
        self._meta = None
        self._target_dir = ""
        self._suppress = False
        self._logged_in = None       # 最近一次登录态探测结果
        self._checking_login = False
        self._probe = LoginProbe(self)
        self._probe.result.connect(self._on_login_state)
        self._thumbs = ThumbFetcher(self)
        self._thumbs.loaded.connect(self._on_thumb)
        self._hover = HoverFetcher(self)
        self._hover.loaded.connect(self._on_hover_pix)
        self._hover_cache = {}          # note_id -> QPixmap（悬停大图缓存，小容量）
        self._hover_order = []
        self._hover_note = None         # 当前悬停行对应的 note_id
        # ---- 单篇笔记下载（读取当前页 SSR，解析后加入队列） ----
        self._single = SingleBridge(self)
        self._single.result.connect(self._on_single_note_result)
        self._single.log.connect(self._log)
        # ---- 下载任务队列（HTTP 通道，不占用可见浏览器） ----
        self._tasks = load_tasks()
        self._qbridge = QueueBridge(self)
        self._qbridge.task_state.connect(self._on_task_state)
        self._qbridge.sig_notice.connect(self._on_queue_notice)
        self._qbridge.sig_log.connect(self._log)
        self._queue = DownloadQueue(self._cookie_provider, self._qbridge, max_workers=2)
        self._build_ui()
        self._restore_settings()
        self._history = self._load_history()
        self._refresh_queue_panel()
        self._resume_pending_tasks()

    def _build_ui(self):
        central = QSplitter(Qt.Horizontal)

        # ---- 左侧：内嵌浏览器 ----
        self.engine = EngineView(central)
        central.addWidget(self.engine)
        self.engine.urlChanged.connect(self._schedule_login_check)

        # ---- 右侧：操作与数据区 ----
        right = QWidget()
        rlay = QVBoxLayout(right)
        rlay.setContentsMargins(6, 6, 6, 6)

        hint = QLabel("抓取：输入小红书号 / 主页链接 后点「抓取」；输入为空则抓取左侧当前作者主页。"
                      "未登录或会话过期时浏览器会自动弹出登录二维码，直接在内嵌浏览器扫码即可。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#888;")
        rlay.addWidget(hint)

        top = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("小红书号 或 主页链接（留空=抓左侧当前作者主页）")
        self.btn_get = QPushButton("抓取")
        self.btn_get.clicked.connect(self.on_get)
        top.addWidget(self.input, 1)
        top.addWidget(self.btn_get)
        rlay.addLayout(top)

        act = QHBoxLayout()
        self.btn_home = QPushButton("小红书主页")
        self.btn_home.clicked.connect(lambda: self.engine.open_url(config.HOME + "/explore"))
        self.btn_history = QPushButton("查看记录")
        self.btn_history.setToolTip("查看最近抓取过的用户（用户名 / 小红书号 / 主页链接）")
        self.btn_history.clicked.connect(self._open_history)
        self.btn_opendir = QPushButton("打开目录")
        self.btn_opendir.setToolTip("在文件管理器中打开下载目录")
        self.btn_opendir.clicked.connect(self._open_dir)
        self.btn_note = QPushButton("下载笔记")
        self.btn_note.setToolTip("下载左侧当前打开的单篇笔记（图文全部图片 / 视频）")
        self.btn_note.clicked.connect(self._download_current_note)
        self.lb_login = QLabel("…")
        self.lb_login.setStyleSheet("color:#999;")
        self.lb_state = QLabel("")
        self.lb_state.setStyleSheet("color:#1a7f37; font-weight:bold;")
        act.addWidget(self.btn_home)
        act.addWidget(self.btn_history)
        act.addWidget(self.btn_opendir)
        act.addWidget(self.btn_note)
        act.addStretch(1)
        act.addWidget(self.lb_login)
        act.addWidget(self.lb_state)
        rlay.addLayout(act)

        # 目标目录 + 下载
        dl = QHBoxLayout()
        self.ed_dir = QLineEdit()
        self.ed_dir.setReadOnly(True)
        self.ed_dir.setPlaceholderText("目标目录（下载到 该目录/作者/标题_笔记ID/）")
        self.btn_dir = QPushButton("浏览…")
        self.btn_dir.clicked.connect(self._choose_dir)
        self.btn_dl = QPushButton("下载勾选")
        self.btn_dl.clicked.connect(self._start_download)
        self.btn_dl.setEnabled(False)
        dl.addWidget(self.ed_dir, 1)
        dl.addWidget(self.btn_dir)
        dl.addWidget(self.btn_dl)
        rlay.addLayout(dl)

        self.lb_stats = QLabel("共 0 篇 · 图文 0 · 视频 0 · 已选 0")
        self.lb_stats.setStyleSheet("color:#666;")
        rlay.addWidget(self.lb_stats)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["选择", "类型", "封面", "标题", "点赞", "状态"])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 52)
        self.table.setColumnWidth(1, 60)
        self.table.setColumnWidth(2, THUMB + 12)
        self.table.setColumnWidth(4, 90)
        self.table.setColumnWidth(5, 90)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(THUMB + 10)
        self.table.setIconSize(QSize(THUMB, THUMB))
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setMouseTracking(True)
        self.table.itemChanged.connect(self._on_table_item_changed)
        self.table.cellClicked.connect(self._on_row_clicked)
        self.table.cellEntered.connect(self._on_cell_hover)
        self.table.viewport().installEventFilter(self)  # 鼠标离开表格时收起悬停大图
        rlay.addWidget(self.table, 1)

        # 悬停封面预览浮窗（跟随鼠标显示大图）
        self._tip = QLabel(self,
                           Qt.ToolTip | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self._tip.setStyleSheet(
            "background:#ffffff; border:1px solid #c9cdd4; padding:4px;")
        self._tip.hide()

        bot = QHBoxLayout()
        self.chk_all = QCheckBox("全选")
        self.chk_all.setTristate(True)
        self.chk_all.stateChanged.connect(self._on_group_select)
        self.chk_img = QCheckBox("全选图文")
        self.chk_img.setTristate(True)
        self.chk_img.stateChanged.connect(self._on_group_select)
        self.chk_vid = QCheckBox("全选视频")
        self.chk_vid.setTristate(True)
        self.chk_vid.stateChanged.connect(self._on_group_select)
        self.lb_selected = QLabel("")
        bot.addWidget(self.chk_all)
        bot.addWidget(self.chk_img)
        bot.addWidget(self.chk_vid)
        bot.addWidget(self.lb_selected)
        bot.addStretch(1)
        rlay.addLayout(bot)

        # ---- 任务队列面板（右下，下载不占用左侧浏览器） ----
        qbox = QGroupBox("任务队列")
        qv = QVBoxLayout(qbox)
        qv.setContentsMargins(8, 4, 8, 6)
        qv.setSpacing(4)
        qh = QHBoxLayout()
        self.lb_qcount = QLabel("")
        self.lb_qcount.setStyleSheet("color:#666;")
        qh.addWidget(self.lb_qcount)
        qh.addStretch(1)
        self.btn_qretry = QPushButton("重下失败")
        self.btn_qretry.setToolTip("把状态为「失败」的任务重新加入队列")
        self.btn_qretry.clicked.connect(self._on_queue_retry)
        self.btn_qpause = QPushButton("暂停")
        self.btn_qpause.clicked.connect(self._on_queue_pause)
        self.btn_qresume = QPushButton("继续")
        self.btn_qresume.clicked.connect(self._on_queue_resume)
        qh.addWidget(self.btn_qretry)
        qh.addWidget(self.btn_qpause)
        qh.addWidget(self.btn_qresume)
        qv.addLayout(qh)
        self.tab_q = QTableWidget(0, 4)
        self.tab_q.setHorizontalHeaderLabels(["状态", "标题", "类型", "说明"])
        hq = self.tab_q.horizontalHeader()
        hq.setSectionResizeMode(1, QHeaderView.Stretch)
        self.tab_q.setColumnWidth(0, 62)
        self.tab_q.setColumnWidth(2, 48)
        self.tab_q.setColumnWidth(3, 170)
        self.tab_q.verticalHeader().setVisible(False)
        self.tab_q.verticalHeader().setDefaultSectionSize(24)
        self.tab_q.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tab_q.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tab_q.setMaximumHeight(168)
        qv.addWidget(self.tab_q, 1)
        rlay.addWidget(qbox)

        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)
        self.logbox.setMaximumHeight(130)
        self.logbox.setMaximumBlockCount(2000)  # 防超长会话内存膨胀
        self.logbox.setPlaceholderText("运行日志…")
        rlay.addWidget(self.logbox)

        central.addWidget(right)
        central.setSizes([760, 520])
        self.setCentralWidget(central)
        self.input.returnPressed.connect(self.on_get)

    # ---------- 抓取 ----------
    def on_get(self):
        if self._busy:
            return
        raw = self.input.text().strip()
        self._busy = True
        self._meta = None
        self._notes.clear()
        self._rows.clear()
        self._row_of.clear()
        self._hover_note = None
        self._hover_cache.clear()
        self._hover_order.clear()
        self._tip.hide()
        self.table.setRowCount(0)
        self._reset_select_boxes()
        self.btn_get.setEnabled(False)
        self.btn_get.setText("抓取中…")
        self.btn_dl.setEnabled(False)
        self.lb_state.setText("抓取中…")
        self._refresh_stats()
        th = threading.Thread(target=capture_job, args=(raw, self._bridge, self.engine.attach), daemon=True)
        th.start()

    def _log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.logbox.appendPlainText(f"[{ts}] {msg}")

    # ---------- 登录态探测（只读，用于状态提示/诊断） ----------
    def showEvent(self, ev):
        super().showEvent(ev)
        QTimer.singleShot(2500, self._check_login)

    def _schedule_login_check(self, *_):
        QTimer.singleShot(6000, self._check_login)

    def _check_login(self):
        if self._busy or self._checking_login:
            return
        self._checking_login = True
        threading.Thread(target=self._login_probe_worker, daemon=True).start()

    def _login_probe_worker(self):
        ok = False
        try:
            dp = self.engine.attach()
            try:
                for c in dp.cookies():
                    if (c.get("name") == "web_session" and c.get("value")
                            and "xiaohongshu" in str(c.get("domain", ""))):
                        ok = True
                        break
            except Exception:
                ok = False
        except Exception:
            ok = False
        self._probe.result.emit(ok)

    def _on_login_state(self, ok):
        self._checking_login = False
        self.lb_login.setText("● 已登录" if ok else "● 未登录")
        self.lb_login.setStyleSheet("color:#1a7f37;" if ok else "color:#c77700;")
        if ok != self._logged_in:
            self._log("登录态：" + ("已登录" if ok else "未登录/会话过期（内嵌浏览器将自动弹登录二维码）"))
            if ok:
                try:
                    cur = self.engine.url().toString().lower()
                except Exception:
                    cur = ""
                # 扫码成功但还停留在登录页时，跳回首页信息流确认登录
                if "/login" in cur or not cur:
                    self.engine.open_url(config.HOME + "/explore")
        self._logged_in = ok

    def _refresh_stats(self):
        img = sum(1 for it in self._rows if not it.is_video)
        vid = len(self._rows) - img
        s = 0
        for r, it in enumerate(self._rows):
            ck = self.table.item(r, 0)
            if ck and ck.checkState() == Qt.Checked:
                s += 1
        self.lb_stats.setText(f"共 {len(self._rows)} 篇 · 图文 {img} · 视频 {vid} · 已选 {s}")
        self.lb_selected.setText(f"选中 {s} 篇")
        self.btn_dl.setEnabled(bool(not self._busy and self._target_dir and s > 0 and self._meta))
        self._sync_select_boxes()

    # ---------- 全选 / 全选图文 / 全选视频（三组联动 + 半选态） ----------
    @staticmethod
    def _row_matches(it, group):
        if group is None:
            return True
        return (group == "video") == it.is_video

    def _on_group_select(self, state):
        if self._suppress:
            return
        cb = self.sender()
        if cb is self.chk_all:
            group = None
        elif cb is self.chk_img:
            group = "image"
        elif cb is self.chk_vid:
            group = "video"
        else:
            return
        checked = int(state) != 0  # 三态框点按只落“选中/取消”，半选由 _sync 反映
        self._suppress = True
        try:
            for r, it in enumerate(self._rows):
                if self._row_matches(it, group):
                    ck = self.table.item(r, 0)
                    if ck:
                        ck.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        finally:
            self._suppress = False
        self._refresh_stats()

    def _sync_select_boxes(self):
        """按当前勾选把三个全选框同步为 勾选/未勾选/半选"""
        if self._suppress or not getattr(self, "chk_all", None):
            return
        cnt = {"all": [0, 0], "image": [0, 0], "video": [0, 0]}  # [总数, 已选]
        for r, it in enumerate(self._rows):
            ck = self.table.item(r, 0)
            sel = bool(ck and ck.checkState() == Qt.Checked)
            for g in cnt:
                if g == "all" or (g == "image" and not it.is_video) or (g == "video" and it.is_video):
                    cnt[g][0] += 1
                    cnt[g][1] += sel
        for cb, g in ((self.chk_all, "all"), (self.chk_img, "image"), (self.chk_vid, "video")):
            total, seln = cnt[g]
            cb.blockSignals(True)
            if total and seln == total:
                cb.setCheckState(Qt.Checked)
            elif seln:
                cb.setCheckState(Qt.PartiallyChecked)
            else:
                cb.setCheckState(Qt.Unchecked)
            cb.blockSignals(False)

    def _reset_select_boxes(self):
        for cb in (self.chk_all, self.chk_img, self.chk_vid):
            cb.blockSignals(True)
            cb.setCheckState(Qt.Unchecked)
            cb.blockSignals(False)

    def _append_rows(self, notes):
        if not notes:
            self._refresh_stats()
            return
        self._suppress = True
        try:
            n0 = self.table.rowCount()
            self.table.setUpdatesEnabled(False)
            try:
                self.table.setRowCount(n0 + len(notes))
                for i, it in enumerate(notes):
                    r = n0 + i
                    self._rows.append(it)
                    self._row_of[it.note_id] = r
                    self._notes[it.note_id] = it
                    ck = QTableWidgetItem()
                    ck.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                    ck.setCheckState(Qt.Unchecked)
                    self.table.setItem(r, 0, ck)
                    kind = QTableWidgetItem(it.kind_label)
                    kind.setForeground(KIND_COLOR.get(it.kind_label, QColor(60, 60, 60)))
                    self.table.setItem(r, 1, kind)
                    thumb = QTableWidgetItem()
                    thumb.setData(Qt.UserRole, it.note_id)
                    self.table.setItem(r, 2, thumb)
                    title = QTableWidgetItem(it.title)
                    title.setToolTip(it.title)
                    self.table.setItem(r, 3, title)
                    self.table.setItem(r, 4, QTableWidgetItem(fmt_like(it.liked)))
                    self.table.setItem(r, 5, QTableWidgetItem(it.status))
            finally:
                self.table.setUpdatesEnabled(True)
        finally:
            self._suppress = False
        self._refresh_stats()

    def _on_table_item_changed(self, item):
        """行内勾选变化 → 刷新统计/下载可用性（含组选引发的逐行变化）"""
        if item is not None and item.column() == 0 and not self._suppress:
            self._refresh_stats()

    def _on_thumb(self, note_id, pix):
        r = self._row_of.get(note_id)
        if r is not None:
            item = self.table.item(r, 2)
            if item:
                item.setIcon(QIcon(pix))

    # ---------- 行点击勾选 / 封面悬停大图 ----------
    def _on_row_clicked(self, row, col):
        """点击整行（复选框列除外）即切换该行勾选"""
        if self._suppress or col == 0:
            return
        item = self.table.item(row, 0)
        if item is None:
            return
        item.setCheckState(
            Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked)

    def _on_cell_hover(self, row, col):
        if col != 2:
            self._hide_tip()
            return
        self._show_hover_for(row)

    def _show_hover_for(self, row):
        item = self.table.item(row, 2)
        note_id = item.data(Qt.UserRole) if item else None
        if not note_id:
            self._hide_tip()
            return
        self._hover_note = note_id
        pix = self._hover_cache.get(note_id)
        if pix is not None:
            self._update_tip(pix)
            return
        # 未缓存：发起一次大图下载，完成后再弹出
        note = self._notes.get(note_id)
        if note and note.cover_url:
            self._hover.get(note_id, note.cover_url)

    def _update_tip(self, pix):
        if pix.width() > 520 or pix.height() > 640:
            pix = pix.scaled(QSize(520, 640), Qt.KeepAspectRatio,
                             Qt.SmoothTransformation)
        self._tip.setPixmap(pix)
        self._tip.adjustSize()
        p = QCursor.pos()
        self._tip.move(p.x() + 14, p.y() + 16)
        self._tip.show()

    def _on_hover_pix(self, note_id, pix):
        # 小容量缓存（超出淘汰最早项）
        if note_id not in self._hover_cache:
            self._hover_cache[note_id] = pix
            self._hover_order.append(note_id)
            if len(self._hover_order) > 12:
                old = self._hover_order.pop(0)
                self._hover_cache.pop(old, None)
        if note_id == self._hover_note:
            self._update_tip(pix)

    def _hide_tip(self):
        self._hover_note = None
        self._tip.hide()

    def eventFilter(self, obj, ev):
        if obj is self.table.viewport() and ev.type() == QEvent.Leave:
            self._hide_tip()
        return super().eventFilter(obj, ev)

    def _on_done(self, result, err):
        self._busy = False
        self.btn_get.setEnabled(True)
        self.btn_get.setText("抓取")
        if result is None:
            if err == "":
                self.lb_state.setText("已打开页面")
                self._log("已在左侧内嵌浏览器打开该链接。可直接在内嵌浏览器里浏览；若为作者主页，点「抓取」(留空) 收集其作品。")
                return
            self.lb_state.setText("失败")
            self._log("任务失败：" + err)
            QMessageBox.warning(self, "未完成", err)
            return
        self._append_rows(result.notes)
        for it in result.notes:
            if it.cover_url:
                self._thumbs.get(it.note_id, it.cover_url)
        self._meta = result.meta
        meta = result.meta
        self._record_history(meta)
        tag = meta.nickname or meta.red_id or meta.user_id or "作者"
        self.lb_state.setText(f"{tag} · {len(result.notes)} 篇")
        self._log(f"完成：{result.stopped}，共 {len(result.notes)} 篇。勾选作品并选目标目录后可下载。")
        self._refresh_stats()  # meta 就绪后立即评估下载按钮可用性

    # ---------- 勾选下载 ----------
    def _selected_items(self):
        # 按行收集勾选 NoteItem（行序即列表顺序）
        out = []
        for r, it in enumerate(self._rows):
            ck = self.table.item(r, 0)
            if ck and ck.checkState() == Qt.Checked:
                out.append(it)
        return out

    # ---------- 抓取记录（查看记录 / 本地持久化） ----------
    def _load_history(self):
        try:
            data = json.loads(config.HISTORY_FILE.read_text(encoding="utf-8"))
            return list(data) if isinstance(data, list) else []
        except Exception:
            return []

    def _save_history(self):
        try:
            config.HISTORY_FILE.write_text(
                json.dumps(self._history[:config.HISTORY_MAX], ensure_ascii=False, indent=1),
                encoding="utf-8")
        except Exception:
            pass

    def _record_history(self, meta):
        """每次成功抓取作者主页后追加一条记录（最新在前，同作者去重置顶）"""
        href = (meta.href or "").strip()
        if not href:
            return
        red_id = (meta.red_id or meta.kw or "").strip()
        nickname = (meta.nickname or "").strip()
        key = red_id or href
        self._history = [
            e for e in self._history
            if isinstance(e, dict)
            and ((e.get("red_id") or "").strip() or (e.get("url") or "").strip()) != key
        ]
        self._history.insert(0, {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "nickname": nickname,
            "red_id": red_id,
            "url": href,
        })
        del self._history[config.HISTORY_MAX:]
        self._save_history()

    def _open_history(self):
        dlg = HistoryDialog(self._history, self)
        dlg.exec()
        if dlg.is_cleared():
            self._history = []
            self._save_history()

    def _choose_dir(self):
        start = self._target_dir or str(Path.home() / "Desktop" / "xhs_download")
        d = QFileDialog.getExistingDirectory(self, "选择下载目录", start)
        if d:
            self._target_dir = d
            self.ed_dir.setText(d)
            self._persist_settings()
            self._refresh_stats()

    def _open_dir(self):
        """在系统文件管理器中打开已保存的下载目录"""
        d = self._target_dir
        if not d or not os.path.isdir(d):
            QMessageBox.information(self, "提示", "尚未设置下载目录（或目录已被删除）")
            self._choose_dir()
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.normpath(d)))

    def _restore_settings(self):
        """启动时恢复上次下载目录"""
        try:
            data = json.loads(config.SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        d = data.get("target_dir") or ""
        if d and os.path.isdir(d):
            self._target_dir = d
            self.ed_dir.setText(d)
        self._refresh_stats()

    def _persist_settings(self):
        try:
            config.SETTINGS_FILE.write_text(
                json.dumps({"target_dir": self._target_dir}, ensure_ascii=False, indent=1),
                encoding="utf-8")
        except Exception:
            pass

    # ---------- 下载任务队列（加入队列，不阻塞浏览/抓取） ----------
    _NOTE_STATE_TEXT = {
        QUEUED: "已排队", RUNNING: "下载中", DONE: "完成",
        SKIPPED: "已存在", FAILED: "失败", CANCELLED: "已取消",
    }

    def _cookie_provider(self):
        """worker 线程调用：读内嵌浏览器 Cookie 拼 HTTP 头（不导航页面）"""
        try:
            dp = self.engine.attach()
            return cookie_header_from_dp(dp)
        except Exception:
            return ""

    def _mark_note_status(self, note_id, text):
        r = self._row_of.get(note_id)
        if r is not None:
            item = self.table.item(r, 5)
            if item:
                item.setText(text)

    # ---------- 单篇笔记下载（左侧当前笔记页） ----------
    def _download_current_note(self):
        """先判断左侧是否在单篇笔记页，是则读取并加入下载队列"""
        cur_url = self.engine.url().toString()
        if not note_id_from_url(cur_url):
            QMessageBox.information(
                self, "提示",
                "当前不是单篇笔记页。请先在左侧浏览器打开一篇笔记"
                "（如 xiaohongshu.com/explore/xxxx…），再点「下载笔记」。")
            return
        if not self._target_dir:
            QMessageBox.information(self, "提示", "请先选择目标目录")
            self._choose_dir()
            if not self._target_dir:
                return
        self.lb_state.setText("解析当前笔记页…")
        self._log("正在读取当前笔记页信息…")
        threading.Thread(target=self._single_note_worker, daemon=True).start()

    def _single_note_worker(self):
        try:
            dp = self.engine.attach()
            meta, note = single_note_from_current(dp)
            self._single.result.emit(meta, note, "")
        except AppError as e:
            self._single.result.emit(None, None, str(e))
        except Exception as e:
            self._single.result.emit(
                None, None, f"内部错误：{type(e).__name__}: {e}")

    def _on_single_note_result(self, meta, note, err):
        if err or meta is None or note is None:
            self.lb_state.setText("未开始下载")
            self._log("单笔记下载未开始：" + err)
            QMessageBox.information(self, "提示", err)
            return
        task = TaskItem(note=note, meta=meta, target_dir=self._target_dir)
        self._tasks.insert(0, task)
        save_tasks(self._tasks)
        title = note.title or note.note_id
        author = meta.nickname or meta.red_id or meta.user_id or "笔记"
        self._log(f"单篇笔记已加入下载队列：{author}《{title}》")
        self._refresh_queue_panel()
        self._queue.enqueue([task])

    def _start_download(self):
        """把勾选作品作为独立任务加入后台队列（可继续浏览/抓取/追加）"""
        if not self._target_dir:
            QMessageBox.information(self, "提示", "请先选择目标目录")
            self._choose_dir()
            return
        items = self._selected_items()
        if not items:
            QMessageBox.information(self, "提示", "请先在列表中勾选要下载的作品")
            return
        if not self._meta:
            QMessageBox.information(self, "提示", "请先抓取一次该作者的作品列表")
            return
        meta = self._meta
        new = []
        for it in items:
            t = TaskItem(note=it, meta=meta, target_dir=self._target_dir)
            self._tasks.insert(0, t)   # 最新任务显示在最上
            new.append(t)
        save_tasks(self._tasks)
        self._log(f"已加入下载队列 {len(new)} 篇（后台执行，浏览器可继续使用）。")
        for t in new:
            self._mark_note_status(t.note.note_id, "已排队")
        self._refresh_queue_panel()
        self._queue.enqueue(new)

    def _refresh_queue_panel(self):
        t = self.tab_q
        t.setUpdatesEnabled(False)
        t.setRowCount(len(self._tasks))
        counts = {QUEUED: 0, RUNNING: 0, DONE: 0, SKIPPED: 0, FAILED: 0, CANCELLED: 0}
        for r, tk in enumerate(self._tasks):
            counts[tk.state] = counts.get(tk.state, 0) + 1
            st = tk.state_label
            it0 = QTableWidgetItem(st)
            if tk.state == DONE or tk.state == SKIPPED:
                it0.setForeground(QColor(23, 158, 82))
            elif tk.state == RUNNING:
                it0.setForeground(QColor(33, 102, 235))
            elif tk.state == FAILED:
                it0.setForeground(QColor(211, 47, 47))
            else:
                it0.setForeground(QColor(90, 90, 90))
            t.setItem(r, 0, it0)
            t.setItem(r, 1, QTableWidgetItem(tk.note.title or tk.note.note_id))
            t.setItem(r, 2, QTableWidgetItem(tk.note.kind_label))
            t.setItem(r, 3, QTableWidgetItem(tk.msg))
        t.setUpdatesEnabled(True)
        waiting = counts.get(QUEUED, 0)
        running = counts.get(RUNNING, 0)
        done = counts.get(DONE, 0) + counts.get(SKIPPED, 0)
        failed = counts.get(FAILED, 0)
        halted = self._queue.is_halted()
        suffix = " · 已暂停（登录态丢失）" if halted else ""
        self.lb_qcount.setText(
            f"等待 {waiting} · 进行 {running} · 完成 {done} · 失败 {failed}{suffix}")
        self.btn_qretry.setEnabled(failed > 0)
        self.btn_qpause.setEnabled((waiting + running) > 0 and not halted)
        self.btn_qresume.setEnabled(halted and (waiting > 0 or running > 0))

    def _on_task_state(self, task, state, msg):
        """队列 worker 的进度回调（主线程槽）"""
        for tk in self._tasks:
            if tk is task:
                tk.state = state
                tk.msg = msg
                if state in (DONE, SKIPPED, FAILED, CANCELLED):
                    tk.finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                break
        save_tasks(self._tasks)
        text = self._NOTE_STATE_TEXT.get(state, "")
        if text:
            self._mark_note_status(task.note.note_id, text)
        self._refresh_queue_panel()
        # 队列彻底空闲且无遗留非终态任务 → 汇总提示
        if (not self._queue.is_halted() and self._queue.busy() == 0
                and self._queue.pending() == 0
                and not any(not tk.terminal() for tk in self._tasks) and self._tasks):
            self.lb_state.setText("任务队列已全部处理")
            self._log("任务队列处理完毕；可继续在左侧浏览、抓取并添加新下载。")

    def _on_queue_notice(self, msg):
        self._log(msg)
        self.lb_state.setText(msg)
        self._refresh_queue_panel()

    def _on_queue_pause(self):
        self._queue.pause()
        self._log("已暂停下载队列（当前任务收尾后停止取新任务）。")
        self._refresh_queue_panel()

    def _on_queue_resume(self):
        self._queue.resume()
        self._log("继续下载队列…")
        self._refresh_queue_panel()

    def _on_queue_retry(self):
        retry = [tk for tk in self._tasks if tk.state == FAILED]
        if not retry:
            return
        for tk in retry:
            tk.state = QUEUED
            tk.msg = ""
            tk.finished = ""
        save_tasks(self._tasks)
        self._log(f"重新加入 {len(retry)} 个失败任务…")
        self._refresh_queue_panel()
        self._queue.resume()
        self._queue.enqueue(retry)

    def _resume_pending_tasks(self):
        """启动时把上次未完成任务（queued/running）重新排队，实现断点续传"""
        pending = [tk for tk in self._tasks if tk.state in (QUEUED, RUNNING)]
        if not pending:
            return
        for tk in pending:
            if tk.state == RUNNING:
                tk.state = QUEUED
                tk.msg = ""
        save_tasks(self._tasks)
        self._log(f"恢复 {len(pending)} 个未完成任务，自动续传…")
        self._refresh_queue_panel()
        self._queue.enqueue(pending)

    def _on_note_status(self, note_id, status):
        r = self._row_of.get(note_id)
        if r is not None:
            item = self.table.item(r, 5)
            if item:
                item.setText(status)
        note = self._notes.get(note_id)
        if note:
            note.status = status

    def _on_notice(self, msg):
        self._busy = False
        self.btn_get.setEnabled(True)
        self.btn_get.setText("抓取")
        self.lb_state.setText(msg)
        self._log("任务结束：" + msg)
        self._refresh_stats()


def main():
    app = QApplication([])
    if config.ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(config.ICON_PATH)))

    # 单实例：已被打开则激活已有窗口并退出本进程
    from xhs_app.single import SingleInstance
    guard = SingleInstance("XHSCollector")
    if not guard.become_primary():
        guard.request_activate()
        return

    w = MainWindow()

    def _show_existing():
        w.setWindowState((w.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
        w.show()
        w.raise_()
        w.activateWindow()

    guard.on_activate = _show_existing
    w.show()
    app.exec()


if __name__ == "__main__":
    main()
