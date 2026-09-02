# -*- coding: utf-8 -*-
"""内嵌浏览器引擎：PySide6 QtWebEngine（Chromium 内核）

- 作为 GUI 左侧常驻页面，登录/滑块验证等人工操作直接在内嵌页完成；
- 通过 CDP（QTWEBENGINE_REMOTE_DEBUGGING）让 DrissionPage 挂接同一页面做自动化；
- Cookie 持久化到独立 profile（user_data_qt），重启免登录。

注意：CDP 端口与 Chromium 启动参数必须在 QtWebEngine 首次初始化前设置，
本模块在 import 时即设置环境变量。
"""
import os
import threading
import time

from xhs_app import config

# GPU 默认启用（硬件合成）。早期为稳妥加过 --disable-gpu，代价是整页变
# 软件渲染、滚动/动效卡顿；现代 Edge/GPU 驱动下默认即可，异常时再由
# config.CHROMIUM_FLAGS 覆盖关掉。
os.environ.setdefault("QTWEBENGINE_REMOTE_DEBUGGING", str(config.QT_CDP_PORT))
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                      getattr(config, "CHROMIUM_FLAGS", "--remote-allow-origins=*"))

from PySide6.QtCore import QUrl  # noqa: E402
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile  # noqa: E402
from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: E402


class EngineView(QWebEngineView):
    """内嵌浏览器视图（必须创建在 Qt 主线程）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._profile = QWebEngineProfile("xhs_main")
        self._profile.setPersistentStoragePath(str(config.QT_PROFILE))
        self._profile.setCachePath(str(config.QT_PROFILE / "cache"))
        self._profile.setHttpCacheType(QWebEngineProfile.DiskHttpCache)
        self._profile.setHttpCacheMaximumSize(512 * 1024 * 1024)
        self._profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)
        # 默认 UA 带 QtWebEngine 标识，易被平台判定为自动化而"登录即掉"，
        # 这里覆盖成普通 Chrome UA 以降低指纹
        try:
            self._profile.setHttpUserAgent(config.UA)
        except Exception:
            pass
        # 非活动页后台节流会让 XHS 滚动加载变慢/中断，这里禁掉以保抓取稳定
        try:
            self._profile.setBackgroundTimerThrottlingPolicy(
                QWebEngineProfile.DisallowTimerThrottlingForBackgroundPages)
        except Exception:
            pass
        page = QWebEnginePage(self._profile, self)
        # 小红书大量卡片用 target=_blank/新窗口跳转；单窗口体验改为在当前视图内打开
        page.newWindowRequested.connect(self._on_new_window)
        self.setPage(page)
        self.load(QUrl(config.HOME + "/explore"))

    def _on_new_window(self, request):
        try:
            request.openIn(self.page())
        except Exception:
            pass  # 打开失败则忽略，避免阻断页面内其它操作

    def open_url(self, url):
        self.load(QUrl(url))

    def attach(self):
        """在工作线程中挂接本引擎的 CDP，返回 DrissionPage 控制对象。

        引擎跑在 Qt 主线程，这里只是通过 CDP 会话下发指令，二者互不阻塞。
        """
        from DrissionPage import ChromiumOptions, ChromiumPage

        co = ChromiumOptions().set_address(f"127.0.0.1:{config.QT_CDP_PORT}")
        last = None
        for i in range(25):
            try:
                return ChromiumPage(co)
            except Exception as e:  # 引擎 DevTools 可能尚未就绪，重试
                last = e
                time.sleep(0.4)
        raise RuntimeError(f"无法挂接内嵌浏览器：{last}")
