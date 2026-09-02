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

from PySide6.QtCore import QObject, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QFileDialog, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from xhs_app import config
from xhs_app.embed import EngineView
from xhs_app.models import fmt_like
from xhs_app.service import capture_job, download_job

THUMB = 56
KIND_COLOR = {"图文": QColor(23, 158, 82), "视频": QColor(255, 92, 30)}


class JobBridge(QObject):
    """工作线程 -> UI 的信号桥"""
    log = Signal(str)
    done = Signal(object, str)              # (CollectResult|None, error|None)；error="" 表示“仅打开页面”成功
    note_update = Signal(str, str)          # (note_id, 状态文本)
    notice = Signal(str)                    # 下载类任务的结束/异常提示


class LoginProbe(QObject):
    """登录态检测结果（工作线程 -> UI）"""
    result = Signal(bool)


class ThumbFetcher(QObject):
    """异步下载封面缩略图"""
    loaded = Signal(str, QPixmap)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._nam = QNetworkAccessManager(self)
        self._nam.finished.connect(self._on_finish)
        self._pending = {}

    def get(self, note_id: str, url: str):
        if not url:
            return
        req = QNetworkRequest(QUrl(url))
        req.setRawHeader(b"Referer", b"https://www.xiaohongshu.com/")
        req.setRawHeader(b"User-Agent", b"Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
        reply = self._nam.get(req)
        self._pending[reply] = note_id

    def _on_finish(self, reply: QNetworkReply):
        note_id = self._pending.pop(reply, None)
        if reply.error() == QNetworkReply.NoError and note_id:
            pm = QPixmap()
            if pm.loadFromData(reply.readAll()) and not pm.isNull():
                pm = pm.scaled(THUMB, THUMB, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.loaded.emit(note_id, pm)
        reply.deleteLater()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("小红书作品采集")
        if config.ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(config.ICON_PATH)))
        self.resize(1320, 760)
        self._busy = False
        self._notes = {}
        self._row_of = {}
        self._bridge = JobBridge()
        self._bridge.log.connect(self._log)
        self._bridge.done.connect(self._on_done)
        self._bridge.note_update.connect(self._on_note_status)
        self._bridge.notice.connect(self._on_notice)
        self._meta = None
        self._target_dir = ""
        self._suppress = False
        self._probe = LoginProbe()
        self._probe.result.connect(self._on_login_probe)
        self._checking_login = False
        self._thumbs = ThumbFetcher(self)
        self._thumbs.loaded.connect(self._on_thumb)
        self._build_ui()
        self._restore_settings()

    def _build_ui(self):
        central = QSplitter(Qt.Horizontal)

        # ---- 左侧：内嵌浏览器 ----
        self.engine = EngineView(central)
        central.addWidget(self.engine)
        self.engine.urlChanged.connect(lambda _u: QTimer.singleShot(6000, self._check_login))

        # ---- 右侧：操作与数据区 ----
        right = QWidget()
        rlay = QVBoxLayout(right)
        rlay.setContentsMargins(6, 6, 6, 6)

        hint = QLabel("抓取：输入小红书号 / 主页链接 后点「抓取」；输入为空则抓取左侧当前作者主页。"
                      "登录 / 验证码请在左侧内嵌浏览器中直接操作。")
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
        self.btn_login = QPushButton("登录/扫码")
        self.btn_login.clicked.connect(lambda: self.engine.open_url(config.LOGIN_URL))
        self.btn_opendir = QPushButton("打开目录")
        self.btn_opendir.setToolTip("在文件管理器中打开下载目录")
        self.btn_opendir.clicked.connect(self._open_dir)
        self.lb_state = QLabel("")
        self.lb_state.setStyleSheet("color:#1a7f37; font-weight:bold;")
        act.addWidget(self.btn_home)
        act.addWidget(self.btn_login)
        act.addWidget(self.btn_opendir)
        act.addStretch(1)
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
        self.table.itemChanged.connect(self._on_table_item_changed)
        rlay.addWidget(self.table, 1)

        bot = QHBoxLayout()
        self.chk_all = QCheckBox("全选")
        self.chk_all.stateChanged.connect(self._on_select_all)
        self.lb_selected = QLabel("")
        bot.addWidget(self.chk_all)
        bot.addWidget(self.lb_selected)
        bot.addStretch(1)
        rlay.addLayout(bot)

        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)
        self.logbox.setMaximumHeight(130)
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
        self._row_of.clear()
        self.table.setRowCount(0)
        self.chk_all.blockSignals(True)
        self.chk_all.setChecked(False)
        self.chk_all.blockSignals(False)
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

    # ---------- 登录态检测 ----------
    def showEvent(self, ev):
        super().showEvent(ev)
        QTimer.singleShot(3000, self._check_login)

    def _check_login(self):
        """非侵入检测：挂接内嵌引擎读取 web_session cookie，避免打断当前浏览"""
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

    def _on_login_probe(self, ok):
        self._checking_login = False
        self.btn_login.setEnabled(not ok)
        self.btn_login.setToolTip(
            "登录态有效，无需重复登录" if ok else "打开登录页（登录 / 重新登录）")

    def _refresh_stats(self):
        img = sum(1 for it in self._notes.values() if not it.is_video)
        vid = len(self._notes) - img
        s = 0
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it and it.checkState() == Qt.Checked:
                s += 1
        self.lb_stats.setText(f"共 {len(self._notes)} 篇 · 图文 {img} · 视频 {vid} · 已选 {s}")
        self.lb_selected.setText(f"选中 {s} 篇")
        self.btn_dl.setEnabled(bool(not self._busy and self._target_dir and s > 0 and self._meta))

    def _append_rows(self, notes):
        self._suppress = True
        try:
            for it in notes:
                r = self.table.rowCount()
                self.table.insertRow(r)
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
            self._suppress = False
        self._refresh_stats()

    def _on_select_all(self, state):
        # state 可能为 int 或枚举；统一按非 0 判断，避免 int==枚举 恒 False 导致全选失效
        checked = Qt.Checked if int(state) != 0 else Qt.Unchecked
        self._suppress = True
        try:
            for r in range(self.table.rowCount()):
                item = self.table.item(r, 0)
                if item:
                    item.setCheckState(checked)
        finally:
            self._suppress = False
        self._refresh_stats()

    def _on_table_item_changed(self, item):
        """行内勾选变化 → 刷新统计/下载可用性（含全选引发的逐行变化）"""
        if item is not None and item.column() == 0 and not self._suppress:
            self._refresh_stats()

    def _on_thumb(self, note_id, pix):
        r = self._row_of.get(note_id)
        if r is not None:
            item = self.table.item(r, 2)
            if item:
                item.setIcon(QIcon(pix))

    def _on_done(self, result, err):
        self._busy = False
        self.btn_get.setEnabled(True)
        self.btn_get.setText("抓取")
        QTimer.singleShot(1600, self._check_login)  # 任务后复检登录态
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
        tag = meta.nickname or meta.red_id or meta.user_id or "作者"
        self.lb_state.setText(f"{tag} · {len(result.notes)} 篇")
        self._log(f"完成：{result.stopped}，共 {len(result.notes)} 篇。勾选作品并选目标目录后可下载。")
        self._refresh_stats()  # meta 就绪后立即评估下载按钮可用性

    # ---------- 勾选下载 ----------
    def _selected_items(self):
        # 收集勾选行的 NoteItem
        out = []
        for r in range(self.table.rowCount()):
            ck = self.table.item(r, 0)
            if ck and ck.checkState() == Qt.Checked:
                for nid, row in self._row_of.items():
                    if row == r:
                        it = self._notes.get(nid)
                        if it:
                            out.append(it)
                        break
        return out

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

    def _start_download(self):
        if self._busy:
            return
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
        self._busy = True
        self.btn_get.setEnabled(False)
        self.btn_get.setText("下载中…")
        self.btn_dl.setEnabled(False)
        self.lb_state.setText("下载中…")
        self._log(f"勾选 {len(items)} 篇开始下载。")
        th = threading.Thread(target=download_job,
                              args=(self._meta, items, self._target_dir, self._bridge, self.engine.attach),
                              daemon=True)
        th.start()

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
        QTimer.singleShot(1500, self._check_login)
        self._refresh_stats()


def main():
    app = QApplication([])
    if config.ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(config.ICON_PATH)))
    w = MainWindow()
    w.show()
    app.exec()


if __name__ == "__main__":
    main()
