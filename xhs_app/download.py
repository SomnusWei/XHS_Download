# -*- coding: utf-8 -*-
"""DownloadManager：httpx 直连下载原图/视频到 {目标目录}/{作者}/{标题}_{noteId}/

媒体 CDN 直连无需 x-s；带上浏览器 Cookie + Referer 即可。
"""
import re
from pathlib import Path

EXT_BY_CTYPE = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/webp": ".webp", "image/gif": ".gif", "image/avif": ".avif",
    "video/mp4": ".mp4", "video/quicktime": ".mov",
}


def sanitize(name: str, maxlen=60) -> str:
    """清洗文件名非法字符；标题里带空格/emoji 都保留，仅去掉路径/控制字符"""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(name or "")).strip()
    s = re.sub(r"\s+", " ", s)
    if len(s) > maxlen:
        s = s[:maxlen].rstrip()
    return s or "note"


def build_target(meta, note, base_dir) -> Path:
    """返回目标目录：{base}/{作者}/{标题}_{noteId}"""
    author = sanitize(meta.nickname or meta.red_id or meta.user_id or "作者")
    folder = sanitize(note.title or note.note_id)
    return Path(base_dir) / author / f"{folder}_{note.note_id}"


def ext_for(headers, url, default=".jpg"):
    ct = (headers or {}).get("content-type", "") or ""
    for k, v in EXT_BY_CTYPE.items():
        if k in ct:
            return v
    if re.search(r"\.(mp4|mov)$", url.split("?")[0], re.I):
        return ".mp4"
    if re.search(r"\.(jpe?g|png|webp|avif|gif)$", url.split("?")[0], re.I):
        return re.search(r"\.(jpe?g|png|webp|avif|gif)$", url.split("?")[0], re.I).group(1)
    return default


def folder_done(dirpath: Path) -> bool:
    """该笔记目录已存在且有文件 → 视为已下载可跳过"""
    return dirpath.is_dir() and any(p.is_file() for p in dirpath.iterdir())


def has_existing(dest_dir, prefix) -> bool:
    """目录里是否已有该前缀文件（断点续传/跳过用；如 01.*、视频.*）"""
    if not dest_dir.is_dir():
        return False
    return any(p.is_file() and p.name.startswith(prefix + ".") for p in dest_dir.iterdir())


def remove_prefix(dest_dir, prefix):
    """删除该前缀的残留文件（重试前清理半成品）"""
    if dest_dir.is_dir():
        for p in dest_dir.iterdir():
            if p.is_file() and p.name.startswith(prefix + "."):
                p.unlink(missing_ok=True)
