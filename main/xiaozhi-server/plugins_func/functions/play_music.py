"""
播放音乐 —— 网易云在线实时转 Opus(不先下载)

主模型只负责给出歌名,搜索 / 可用性探测 / 最终 CDN URL 都由本工具完成:
  1. 网易云搜索拿候选;
  2. 对每条候选做"8 字节 GET 探测",确认为真实音频(audio/* 且非 HTML)
     并拿到最终 CDN URL,顺带按 Content-Length 排除明显是 30s 试听的片段;
  3. 把"引导语 + STREAM 消息"排进 TTS 队列 —— TTS 文本线程收到 STREAM 后
     用 ffmpeg 边下边转成 16k 单声道 s16le PCM,再增量编码为 opus 帧,
     由 AudioRateController 按 60ms 实时节流推给设备,不需要整首落盘。

协议上与原生本地播歌一致:返回 Action.RECORD,音频由本模块经 tts 队列自理;
`ContentType.STREAM` 在 core/providers/tts/base.py 中处理(与 FILE 同槽位)。
"""
import re
import random
import asyncio

import requests

from core.providers.tts.dto.dto import TTSMessageDTO, SentenceType, ContentType
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__

# ---- 网易云接口(公开接口,需要 UA + Referer)----
_NETEASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://music.163.com",
}
_SEARCH_API = "https://music.163.com/api/search/get"
_PLAYER_URL = "https://music.163.com/song/media/outer/url?id={}.mp3"
_TIMEOUT = 8            # 单次请求超时(秒)
_SEARCH_LIMIT = 8       # 搜索取前 N 条
_MAX_CANDIDATES = 6     # 依序探测前 N 条候选

# “随机播放”用:已实测可免费整曲播放的网易云 id
FALLBACK_PLAYLIST = [
    {"id": 478507889, "name": "卡农（经典钢琴版）", "artists": "dylanf"},
    {"id": 541326593, "name": "菊次郎的夏天（纯钢琴）", "artists": "Masque_Jupiter"},
    {"id": 864711417, "name": "天空之城（经典钢琴版）", "artists": "dylanf"},
    {"id": 2620353504, "name": "虫儿飞", "artists": "三妹"},
]

play_music_function_desc = {
    "type": "function",
    "function": {
        "name": "play_music",
        "description": "当用户要求播放音乐、歌曲时调用。歌曲由网易云在线提供,点歌成功后设备会先播报歌名再播放。",
        "parameters": {
            "type": "object",
            "properties": {
                "song_name": {
                    "type": "string",
                    "description": "歌曲名称。用户没指定具体歌名时传 'random'。 示例: ```用户:播放两只老虎\n参数：两只老虎``` ```用户:播放音乐 \n参数：random ```",
                }
            },
            "required": ["song_name"],
        },
    },
}


def _is_audio_head(head: bytes) -> bool:
    """判断响应前几个字节是否像真实音频(排除 HTML/404)"""
    if len(head) < 4:
        return False
    if head[:3] == b"ID3":  # mp3 带 ID3 标签
        return True
    if head[0] == 0xFF and head[1] & 0xE0 == 0xE0:  # mp3 帧同步字 0xFFEx/0xFFFx
        return True
    return head[:4] in (b"OggS", b"fLaC", b"RIFF") or head[4:8] == b"ftyp"


def _resolve_playable(song_id: int, duration_ms: int = 0):
    """GET 跟随重定向读前 ~16 字节:确为音频才返回最终 CDN URL,否则 None

    网易云外链对可免费播放的歌会 302 到真实 CDN(audio/mpeg),
    对 VIP/下架的歌会 404 或返回 HTML —— HEAD 只看状态码不可靠,必须看实体。
    """
    try:
        resp = requests.get(
            _PLAYER_URL.format(song_id),
            headers=_NETEASE_HEADERS,
            timeout=_TIMEOUT,
            stream=True,
            allow_redirects=True,
        )
        with resp:
            head = b"".join(resp.iter_content(8192))[:16]
            content_type = (resp.headers.get("Content-Type") or "").lower()
            content_length = resp.headers.get("Content-Length")
        if not resp.url or not resp.url.startswith("http") or not head:
            return None
        if not content_type.startswith("audio/"):
            return None
        if not _is_audio_head(head):
            return None
        # 排除明显是 30s 试听/截断的片段(能同时拿到长度与时长时才判断)
        if content_length and content_length.isdigit() and duration_ms:
            if int(content_length) < duration_ms * 6:
                return None
        return resp.url
    except Exception:
        return None


def _search(keyword: str):
    """网易云搜索,返回 [{id,name,artists,duration}]"""
    resp = requests.get(
        _SEARCH_API,
        params={"s": keyword, "type": 1, "limit": _SEARCH_LIMIT},
        headers=_NETEASE_HEADERS,
        timeout=_TIMEOUT,
    )
    songs = ((resp.json() or {}).get("result") or {}).get("songs") or []
    out = []
    for s in songs:
        artists = ",".join(
            a.get("name", "") for a in (s.get("artists") or []) if a.get("name")
        )
        out.append(
            {
                "id": s.get("id"),
                "name": s.get("name", ""),
                "artists": artists,
                "duration": s.get("duration") or 0,
            }
        )
    return out


def _clean_keyword(song_name: str) -> str:
    """去掉常见"播放动词/引号"等噪音,只留歌名去搜索"""
    kw = song_name.strip().strip("《》「」\"'“”‘’")
    kw = re.sub(r"^(播放|放|来一首|来|点一首|唱|唱一下|听|想听|给我放)\s*", "", kw)
    return kw.strip()


def _prepare(song_name: str):
    """阻塞的网络编排,放线程池跑。返回 dict:
    ok=True: title/artists/url; ok=False: reason ∈ notfound|unavailable|network"""
    kw = (song_name or "").strip()
    if not kw or kw.lower() in ("random", "随机", "随机播放", "来点音乐"):
        # 随机:从已实测免费整曲的列表里挑一首
        choice = random.choice(FALLBACK_PLAYLIST)
        url = _resolve_playable(choice["id"])
        if not url:
            return {"ok": False, "reason": "network"}
        return {
            "ok": True,
            "title": choice["name"],
            "artists": choice["artists"],
            "url": url,
            "name": kw or "音乐",
        }

    kw = _clean_keyword(kw)
    try:
        candidates = _search(kw)
    except Exception as e:
        raise  # 交给上层区分网络失败
    if not candidates:
        return {"ok": False, "reason": "notfound", "name": kw}

    for c in candidates[:_MAX_CANDIDATES]:
        if not c.get("id"):
            continue
        url = _resolve_playable(c["id"], c.get("duration") or 0)
        if url:
            return {
                "ok": True,
                "title": c["name"] or kw,
                "artists": c.get("artists", ""),
                "url": url,
                "name": kw,
                "id": c["id"],
            }
    return {"ok": False, "reason": "unavailable", "name": kw}


def initialize_music_handler(conn: "ConnectionHandler"):
    """兼容旧 intent_llm 模式的入口(该模式会列出本地曲库让模型点歌)。

    曲库已切换为网易云在线,intent_llm 只在提示词里注入歌名,本地已无库,
    返回空列表即可,不会影响 function_call 模式的 play_music 在线播放。
    """
    return {"music_file_names": []}


def _enqueue_stream_play(conn: "ConnectionHandler", title: str, artists: str, url: str):
    """排入 TTS 队列:播报歌名(正常 TTS)+ STREAM(实时转码推流)。

    消息序列与原生本地播歌保持一致:非 intent_llm 不自己包 FIRST/LAST,
    由 function_call 回合的框架负责起止;STREAM 同步在 TTS 文本线程处理,
    会一直把回合级 LAST 压到歌曲播完/被打断为止。
    """
    show = f"{title}，{artists}" if artists else title
    intro_templates = [
        f"正在为您播放，{artists}的《{title}》",
        f"请欣赏{artists}的歌曲，《{title}》",
        f"接下来请欣赏，《{title}》",
        f"现在为您带来，《{title}》",
    ] if artists else [
        f"正在为您播放，《{title}》",
        f"请欣赏歌曲，《{title}》",
        f"接下来请欣赏，《{title}》",
        f"现在为您带来，《{title}》",
    ]
    intro = random.choice(intro_templates)
    conn.tts.store_tts_text(conn.sentence_id, intro)
    if conn.intent_type == "intent_llm":
        conn.tts.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=conn.sentence_id,
                sentence_type=SentenceType.FIRST,
                content_type=ContentType.ACTION,
            )
        )
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.MIDDLE,
            content_type=ContentType.TEXT,
            content_detail=intro,
        )
    )
    conn.logger.bind(tag=TAG).info(f"开始在线播放:《{show}》 url={url}")
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.MIDDLE,
            content_type=ContentType.STREAM,
            content_file=url,
        )
    )
    if conn.intent_type == "intent_llm":
        conn.tts.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=conn.sentence_id,
                sentence_type=SentenceType.LAST,
                content_type=ContentType.ACTION,
            )
        )


@register_function("play_music", play_music_function_desc, ToolType.SYSTEM_CTL)
async def play_music(conn: "ConnectionHandler", song_name: str):
    try:
        # 搜索/探测是阻塞网络,放到线程池,别卡主事件循环
        prepared = await asyncio.to_thread(_prepare, song_name or "random")
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"网易云搜索异常: {e}")
        return ActionResponse(
            action=Action.RESPONSE,
            result=str(e),
            response="网易云音乐暂时连不上，稍后再试一次吧",
        )

    if not prepared.get("ok"):
        reason = prepared.get("reason")
        name = prepared.get("name") or "这首歌"
        if reason == "notfound":
            text = f"抱歉，没有找到叫《{name}》的歌曲，换一首试试吧"
        elif reason == "unavailable":
            text = f"抱歉，《{name}》暂时没有可播放的版本，可能受版权或会员限制，换一首试试吧"
        else:
            text = "抱歉，网易云音乐暂时连不上，稍后再试一次吧"
        return ActionResponse(action=Action.NOTFOUND, result=text, response=text)

    _enqueue_stream_play(
        conn,
        prepared["title"],
        prepared.get("artists", ""),
        prepared["url"],
    )
    return ActionResponse(
        action=Action.RECORD,
        result=f"正在播放:《{prepared['title']}》",
        response=f"正在为您播放《{prepared['title']}》",
    )
