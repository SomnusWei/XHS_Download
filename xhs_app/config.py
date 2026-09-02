# -*- coding: utf-8 -*-
"""全局路径与常量"""
import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    # PyInstaller 打包后：资源在 _MEIPASS；登录态/缓存/配置放 %LOCALAPPDATA% ，
    # 避免重打包/移动 exe 目录导致数据丢失
    APP_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    ROOT = Path(sys.executable).resolve().parent
    ICON_PATH = APP_DIR / "logo.ico"
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(ROOT))) / "XHSCollector"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
else:
    APP_DIR = Path(__file__).resolve().parent          # xhs_app/
    ROOT = APP_DIR.parent                              # 项目根
    ICON_PATH = ROOT / "logo.ico"
    DATA_DIR = ROOT
PROFILE_DIR = ROOT / "user_data"                   # Edge 独立登录态目录（旧驱动控制台回归用）
if getattr(sys, "frozen", False):
    CACHE_DIR = DATA_DIR / ".cache"                 # 缩略图等缓存（打包版放 LOCALAPPDATA）
    QT_PROFILE = DATA_DIR / "user_data_qt"          # QtWebEngine 持久化登录态目录
    SETTINGS_FILE = DATA_DIR / "settings.json"      # GUI 本地配置
else:
    CACHE_DIR = ROOT / ".cache"                     # 开发模式沿用项目目录
    QT_PROFILE = ROOT / "user_data_qt"
    SETTINGS_FILE = ROOT / ".settings.json"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

EDGE_PATH = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
HOME = "https://www.xiaohongshu.com"
LOGIN_URL = HOME + "/login"                        # 登录页（扫码）
HIDDEN = True                                      # 预留：旧 Edge 离屏模式（当前已改内嵌，保留兼容开关）
DEBUG_PORT = 9333                                   # CDP 调试端口（旧 Edge 模式）
QT_CDP_PORT = 9347                                  # QtWebEngine 内嵌引擎 CDP 端口
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
USER_POSTED = "user_posted"                        # 列表接口监听关键字
MAX_NOTES = 400                                    # M1 单次拉取上限
DOWNLOAD_WORKERS = 3                               # 并发下载线程数
MAX_ATTEMPTS = 3                                   # 单文件失败重试次数
RETRY_BASE = 1.5                                   # 重试退避基数（秒）
SCROLL_ROUNDS = 60                                 # 滚动轮数上限（防御死循环）
EMPTY_STOP = 12                                    # 连续无新笔记 N 轮才判定到底（风控会临时假收尾）
