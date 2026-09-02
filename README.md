# XHS_Download（小红书作品批量采集下载器）

个人使用的 Windows 桌面工具：输入**小红书号**或**作者主页链接**，即可抓取该作者主页作品列表（区分图文/视频），勾选后**批量下载原图/视频**到本地。

## 功能

- 内嵌浏览器界面（左侧实时页面，右侧操作与数据），登录/滑块验证码直接在窗口内处理，登录态自动持久化（重启免登录）
- 三种抓取方式：输入纯数字小红书号 / 粘贴作者主页链接 / 留空抓取当前内嵌页面的作者
- 作品列表：类型徽标（图文/视频）、封面缩略图、标题、点赞数、勾选、统计
- 下载：原图（图文全部图片）与视频文件，并发限速、单文件失败重试、断点续传
- 落盘结构：`目标目录/作者昵称/标题_笔记ID/`（图片 `01.jpg…`、视频 `视频.mp4`）
- 二次运行自动跳过已下载；下载目录本地持久化，可一键打开

## 运行方式

### 打包版（推荐）

从 [Releases](../../releases) 下载最新 `XHSCollector-vX.Y.Z.zip`，解压后双击 `XHSCollector.exe` 即可（无需 Python 环境）。

> 首次运行需在窗口内扫码登录一次；登录态保存在系统用户目录 `%LOCALAPPDATA%\XHSCollector\`，不会因覆盖/重打包丢失。

### 源码运行

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
.\.venv\Scripts\python.exe main.py
```

## 使用说明

1. 启动后左侧为内嵌浏览器（自动打开小红书主页）
2. 在右侧输入框：
   - 输入纯数字**小红书号** → 点「抓取」
   - 或粘贴 `/user/profile/` 开头的**主页链接** → 点「抓取」
   - 或**留空** → 点「抓取」收集左侧当前作者主页作品
3. 列表出现后勾选作品（支持全选），「浏览…」选择目标目录 → 点「下载勾选」
4. 状态列显示 获取详情→完成/已存在(跳过)/失败(部分)；点「打开目录」查看结果

## 技术要点

- 浏览器自动化：DrissionPage + QtWebEngine（内嵌），页面自签名，无需自行实现 x-s 逆向
- 列表采集：监听 `user_posted` 响应 + 自动滚动分页；小作者主页由 SSR 直接渲染时自动解析 `__INITIAL_STATE__` 兜底
- 详情采集：打开笔记页解析 SSR `__INITIAL_STATE__`（PyYAML），提取原图/视频流地址
- 媒体下载：curl_cffi 模拟浏览器指纹直连 CDN（免 Cookie），无需 x-s
- GUI：PySide6 + QtWebEngine；打包：PyInstaller

## 合规声明

本项目仅供**个人学习、研究及已获授权内容的备份**使用；请勿用于商业或侵权用途，请控制请求频率并遵守平台规则。使用者应自行承担使用本项目产生的全部责任。

## 目录结构

```
├── main.py                 # GUI 入口
├── xhs_app/
│   ├── embed.py            # 内嵌浏览器引擎（QtWebEngine + CDP）
│   ├── resolver.py         # 输入解析：小红书号/主页链接
│   ├── collector.py        # 列表采集（监听 + SSR 兜底）
│   ├── detail.py           # 笔记详情（SSR 解析原图/视频）
│   ├── download.py         # 下载工具（命名/跳过/续传）
│   ├── service.py          # 业务流程编排
│   ├── ui.py               # 主界面
│   ├── models.py / config.py / browser.py
├── test_*.py               # 自动化回归脚本
```
