# -*- coding: utf-8 -*-
"""数据模型与解析"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class NoteItem:
    """一篇笔记（列表级数据，封面为预览图）"""
    note_id: str
    kind: str                 # "normal" 图文 / "video" 视频
    title: str
    cover_url: str            # 预览图（url_pre 优先）
    liked: int
    xsec_token: str
    ts: int = 0
    author: str = ""
    status: str = "待下载"    # UI 状态列

    @property
    def is_video(self) -> bool:
        return self.kind == "video"

    @property
    def kind_label(self) -> str:
        return "视频" if self.is_video else "图文"


@dataclass
class ProfileMeta:
    """解析到的作者主页信息"""
    user_id: str
    href: str                 # 带 xsec_token 的可直接打开链接
    kw: Optional[str] = None  # 输入中的小红书号（用于 DOM 校验）
    nickname: str = ""
    red_id: str = ""
    note_total: int = 0
    source: str = ""          # link / number


@dataclass
class CollectResult:
    meta: ProfileMeta
    notes: list = field(default_factory=list)
    stopped: str = ""         # 正常结束原因


class AppError(Exception):
    """业务可预期错误（提示用户用即可，不需堆栈）"""


class LoginRequired(AppError):
    """会话过期/未登录，需要用户扫码续期"""


def fmt_like(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n or "")
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


def pick_cover(cov: dict) -> str:
    """从 cover 字典里挑预览图：url_pre > url_default > url"""
    if not isinstance(cov, dict):
        return ""
    for k in ("url_pre", "url_default", "url", "urlPre", "urlDefault"):
        v = cov.get(k)
        if v:
            return v
    il = cov.get("info_list")
    if isinstance(il, list) and il:
        best = None
        for it in il:
            try:
                w = int(it.get("width") or 0)
            except (TypeError, ValueError):
                w = 0
            if best is None or best[1] < w:
                best = (it.get("url") or "", w)
        if best and best[0]:
            return best[0]
    return ""


def parse_note(raw: dict) -> Optional[NoteItem]:
    """解析 user_posted 响应中一个扁平 note 对象"""
    if not isinstance(raw, dict):
        return None
    nid = raw.get("note_id")
    if not nid:
        return None
    inter = raw.get("interact_info") or {}
    user = raw.get("user") or {}
    return NoteItem(
        note_id=nid,
        kind="video" if raw.get("type") == "video" else "normal",
        title=(raw.get("display_title") or "").strip(),
        cover_url=pick_cover(raw.get("cover") or {}),
        liked=int(inter.get("liked_count") or 0),
        xsec_token=raw.get("xsec_token") or "",
        ts=int(raw.get("time") or 0),
        author=user.get("nickname") or user.get("nick_name") or "",
    )


def parse_notes_body(body) -> tuple:
    """body -> (NoteItem list, has_more, cursor)。结构：data.notes[]（2026-09 实测）"""
    if isinstance(body, (str, bytes)):
        import json
        try:
            body = json.loads(body)
        except Exception:
            return [], False, None
    if not isinstance(body, dict):
        return [], False, None
    data = body.get("data") or {}
    notes = []
    for raw in data.get("notes") or []:
        it = parse_note(raw)
        if it:
            notes.append(it)
    return notes, bool(data.get("has_more", True)), data.get("cursor")
