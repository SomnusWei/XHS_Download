# -*- coding: utf-8 -*-
"""进程单实例守卫：重复启动时激活已有窗口（QLocalServer 唤醒 + QLockFile 防残留）

锁文件固定放 %LOCALAPPDATA%\\XHSCollector\\，使「开发版 python main.py」与
「打包版 XHSCollector.exe」互斥——二者共用 QtWebEngine CDP 端口 9347，不能同时运行。
"""
import os
from pathlib import Path

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket


class SingleInstance:
    """主实例 = 持有锁并监听唤醒 socket；次实例 = 发一条 show 消息后退出。"""

    def __init__(self, app_id: str):
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "XHSCollector"
        try:
            base.mkdir(parents=True, exist_ok=True)
        except Exception:
            base = Path.home()
        self._lock = QLockFile(str(base / f"{app_id}.lock"))
        self._lock.setStaleLockTime(30 * 1000)
        self._server = None
        self._name = f"{app_id}-qt-engine"
        self.on_activate = None

    def become_primary(self) -> bool:
        """尝试成为主实例；失败说明已有实例在运行。返回 True 表示当前进程是主实例。"""
        if self._lock.tryLock(200):
            return self._listen()
        # 上次异常退出可能残留锁文件：清掉再试一次
        self._lock.removeStaleLockFile()
        if self._lock.tryLock(200):
            return self._listen()
        return False

    def _listen(self) -> bool:
        # 已持有锁（无并发主实例），此时清同名 socket 是安全的
        QLocalServer.removeServer(self._name)
        srv = QLocalServer()
        if not srv.listen(self._name):
            return False
        srv.newConnection.connect(self._on_conn)
        self._server = srv
        return True

    def _on_conn(self):
        conn = self._server.nextPendingConnection()
        if conn is None:
            return
        try:
            conn.waitForReadyRead(300)
            bytes(conn.readAll())
        except Exception:
            pass
        conn.close()
        if self.on_activate:
            QTimer.singleShot(0, self.on_activate)

    def request_activate(self):
        """非主实例：通知主实例弹窗置顶，然后本进程退出。"""
        sock = QLocalSocket()
        sock.connectToServer(self._name)
        if sock.waitForConnected(800):
            sock.write(b"show")
            sock.flush()
            sock.waitForBytesWritten(300)
            sock.disconnectFromServer()
        sock.close()
