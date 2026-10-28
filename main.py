"""小黑盒闲逛：夜间时间窗里随机开一轮，逛一小时；只有她觉得有趣的才用自己的话讲给爸爸听。

底层复用 https://github.com/AaronZ345/xiaoheihe-cli 的 feed 接口（匿名可读）。
帖子详情 / 评论需要小黑盒登录，未登录时自动降级为只用 feed 里的摘要。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_heybox_daily"


def _fmt_num(n: Any) -> str:
    try:
        n = int(n)
    except Exception:
        return str(n)
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


def _clean(text: Any, limit: int = 70) -> str:
    s = str(text or "")
    s = re.sub(r"\[[^\]]{1,12}\]", "", s)  # 去掉 [cube_xxx] 这类表情标记
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[:limit] + "…"
    return s


def _parse_hhmm(raw: Any, default: str) -> tuple[int, int]:
    try:
        hh, mm = str(raw or default).strip().split(":")[:2]
        return int(hh) % 24, int(mm) % 60
    except Exception:
        hh, mm = default.split(":")
        return int(hh), int(mm)


@register(
    PLUGIN_NAME,
    "汤圆定制",
    "夜间时间窗内连刷一小时小黑盒，只把觉得有趣的用自己的话讲给爸爸听",
    "v0.3.0",
)
class HeyboxDailyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._session_seq = 0
        self._login_ok: bool | None = None
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

            base = Path(get_astrbot_plugin_data_path())
        except Exception:
            base = Path("data") / "plugin_data"
        self._data_dir = base / PLUGIN_NAME
        self._seen_file = self._data_dir / "seen.json"

    # ------------------------------------------------------------------ #
    def _c(self, key: str, default: Any = None) -> Any:
        try:
            val = self.config.get(key, default)
        except Exception:
            val = default
        return default if val is None else val

    # -------------------------- 记忆（默认 2 天） -------------------------- #
    def _load_seen(self) -> dict[int, float]:
        """{帖子id: 首次看到的时间戳}，超过 memory_days 的自动丢弃。"""
        days = float(self._c("memory_days", 2) or 2)
        cutoff = time.time() - days * 86400
        data: dict[int, float] = {}
        try:
            raw = self._seen_file
            if raw.exists():
                obj = json.loads(raw.read_text(encoding="utf-8"))
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        try:
                            ts = float(v)
                        except Exception:
                            continue
                        if ts >= cutoff:
                            data[int(k)] = ts
                elif isinstance(obj, list):  # 兼容旧格式
                    for k in obj:
                        try:
                            data[int(k)] = time.time()
                        except Exception:
                            continue
        except Exception as e:
            logger.warning(f"[heybox] 读取记忆失败: {type(e).__name__}: {e}")
        return data

    def _save_seen(self, data: dict[int, float]) -> None:
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            keep = {str(k): v for k, v in data.items()}
            self._seen_file.write_text(
                json.dumps(keep, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"[heybox] 保存记忆失败: {type(e).__name__}: {e}")

    def _mark_seen(self, linkid: int) -> None:
        data = self._load_seen()
        data[int(linkid)] = time.time()
        self._save_seen(data)

    # -------------------------- 喜好分区 -------------------------- #
    def _prefs(self) -> list[str]:
        prefs = self._c("preferred_topics") or []
        if isinstance(prefs, str):
            prefs = re.split(r"[,，、\s]+", prefs)
        return [str(p).strip() for p in prefs if str(p).strip()]

    def _pref_hit(self, item: dict) -> bool:
        prefs = self._prefs()
        if not prefs:
            return False
        parts = [str(item.get("title") or ""), str(item.get("description") or "")]
        for t in item.get("topics") or []:
            if isinstance(t, dict):
                parts.append(str(t.get("name") or t.get("topic_name") or ""))
        for h in item.get("hashtags") or []:
            if isinstance(h, dict):
                parts.append(str(h.get("name") or h.get("text") or ""))
            else:
                parts.append(str(h))
        blob = " ".join(parts)
        return any(p in blob for p in prefs)

    # -------------------------- 登录 -------------------------- #
    async def _auth_status(self) -> dict:
        try:
            payload = await self._cli(["auth", "status"], timeout=45)
            return payload.get("data") or {}
        except Exception as e:
            return {"authenticated": False, "error": str(e)}

    def _image_from_file(self, path: Path) -> Any:
        """二维码文件实际是 jpeg，转成真正的 png 再发，避免 QQ 端不认。"""
        try:
            from PIL import Image as PILImage

            fixed = path.with_name("login_qr_fixed.png")
            with PILImage.open(path) as im:
                im.convert("RGB").save(fixed, "PNG")
            return Comp.Image.fromFileSystem(str(fixed))
        except Exception:
            return Comp.Image.fromFileSystem(str(path))

    async def _qr_login(self, event: Any = None, timeout: int = 200) -> tuple[bool, str]:
        py = str(self._c("cli_python"))
        script = str(self._c("cli_script"))
        qr_path = self._data_dir / "login_qr.png"
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            if qr_path.exists():
                qr_path.unlink()
        except Exception:
            pass

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        proc = await asyncio.create_subprocess_exec(
            py,
            script,
            "login",
            "--qr-file",
            str(qr_path),
            "--timeout",
            str(timeout),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(Path(script).parent),
        )

        deadline = time.time() + 45
        while time.time() < deadline:
            if qr_path.exists() and qr_path.stat().st_size > 500:
                break
            if proc.returncode is not None:
                break
            await asyncio.sleep(0.8)

        if not (qr_path.exists() and qr_path.stat().st_size > 500):
            out, err = await proc.communicate()
            detail = ((err or b"") + (out or b"")).decode("utf-8", "ignore")[-200:]
            return False, f"二维码没生成出来：{detail or '未知原因'}"

        try:
            chain = MessageChain(
                chain=[
                    Comp.Plain("小黑盒扫码登录：用小黑盒 App 扫这张码，再在手机上点确认（约 3 分钟内有效）"),
                    self._image_from_file(qr_path),
                ]
            )
            if event is not None:
                await event.send(chain)
            else:
                await self._send(chain)
        except Exception as e:
            logger.warning(f"[heybox] 发送二维码失败: {type(e).__name__}: {e}")

        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout + 30)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return False, "扫码超时了"

        text = ((out or b"") + (err or b"")).decode("utf-8", "ignore")
        if '"authenticated": true' in text.replace(" ", "").replace('"authenticated":true', '"authenticated": true'):
            return True, "登录成功"
        if "authenticated" in text and "true" in text:
            return True, "登录成功"
        return False, _clean(text, 160) or "登录未完成"

    async def _ensure_login(self) -> None:
        st = await self._auth_status()
        if st.get("authenticated"):
            logger.info(f"[heybox] 已登录小黑盒（user_id={st.get('user_id')}）")
            return
        if not self._c("auto_login_qr", True):
            logger.warning("[heybox] 未登录小黑盒，正文与评论将不可用")
            return
        logger.info("[heybox] 未登录小黑盒，正在推送扫码登录二维码")
        ok, msg = await self._qr_login()
        logger.info(f"[heybox] 扫码登录结果：{'成功' if ok else msg}")


    # ------------------------------------------------------------------ #
    async def initialize(self):
        if not self._c("enabled", True):
            logger.info("[heybox] 已关闭")
            return
        self._task = asyncio.create_task(self._loop())
        logger.info(
            f"[heybox] 小黑盒闲逛已启动，时间窗 {self._c('window_start', '18:00')}"
            f"→{self._c('window_end', '09:00')}，连逛 "
            f"{self._c('session_minutes', 60)} 分钟，只看不分享也算逛，"
            f"记忆保留 {self._c('memory_days', 2)} 天"
        )
        prefs = self._prefs()
        if prefs:
            logger.info(f"[heybox] 喜好分区：{prefs}（命中时更容易被分享）")
        asyncio.create_task(self._ensure_login())

    async def terminate(self):
        self._stopping = True
        if self._task:
            self._task.cancel()

    def _pick_session_start(self) -> datetime:
        """在 [window_start, window_end - session] 里随机挑一个开始时间。"""
        now = datetime.now()
        session = timedelta(minutes=max(1, int(self._c("session_minutes", 60) or 60)))
        sh, sm = _parse_hhmm(self._c("window_start", "18:00"), "18:00")
        eh, em = _parse_hhmm(self._c("window_end", "09:00"), "09:00")

        window_len = timedelta(hours=(eh - sh) % 24, minutes=em - sm)
        if window_len <= timedelta(0):
            window_len = timedelta(hours=15)

        for delta in (-1, 0, 1):
            day = (now + timedelta(days=delta)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            ws = day + timedelta(hours=sh, minutes=sm)
            latest = ws + window_len - session
            if latest <= now:
                continue
            lo = max(ws, now)
            span = (latest - lo).total_seconds()
            if span <= 0:
                continue
            return lo + timedelta(seconds=random.uniform(0, span))
        return now + timedelta(hours=3)

    async def _loop(self):
        while not self._stopping:
            try:
                nxt = self._pick_session_start()
                logger.info(f"[heybox] 下次开始闲逛：{nxt:%Y-%m-%d %H:%M}")
                await asyncio.sleep(max((nxt - datetime.now()).total_seconds(), 1))
                if self._stopping:
                    break
                await self.run_session()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[heybox] 循环异常: {type(e).__name__}: {e}")
                await asyncio.sleep(300)

    # ----------------------------- CLI 调用 ----------------------------- #
    async def _cli(self, args: list[str], timeout: float = 90) -> dict:
        py = str(self._c("cli_python"))
        script = str(self._c("cli_script"))
        if not os.path.exists(py) or not os.path.exists(script):
            raise RuntimeError(f"xiaoheihe-cli 未就绪: {py} / {script}")

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        proc = await asyncio.create_subprocess_exec(
            py,
            script,
            "-f",
            "json",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(Path(script).parent),
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            raise RuntimeError(f"xiaoheihe-cli 超时: {' '.join(args[:2])}") from None

        text = (out or b"").decode("utf-8", "ignore")
        if not text.strip():
            detail = (err or b"").decode("utf-8", "ignore")[-200:]
            raise RuntimeError(f"xiaoheihe-cli 无输出: {detail}")
        start = text.find("{")
        if start > 0:
            text = text[start:]
        payload = json.loads(text)
        if not payload.get("ok"):
            err_obj = payload.get("error") or {}
            raise RuntimeError(f"{err_obj.get('code') or 'failed'}: {err_obj.get('message')}")
        return payload

    async def _fetch_feed(self) -> list[dict]:
        limit = max(5, int(self._c("candidate_limit", 20) or 20))
        payload = await self._cli(["feed", "--limit", str(limit)])
        result = (payload.get("data") or {}).get("result") or {}
        links = result.get("links") or []
        return links if isinstance(links, list) else []

    async def _fetch_detail(self, linkid: int) -> dict:
        """帖子详情（正文）。未登录会抛错，调用方降级。"""
        payload = await self._cli(["post", "get", str(linkid)])
        result = (payload.get("data") or {}).get("result") or {}
        link = result.get("link")
        if isinstance(link, dict):
            return link
        return result if isinstance(result, dict) else {}

    @staticmethod
    def _extract_body(link: dict, fallback: str = "") -> str:
        """把 text 字段（JSON 片段数组）拼成纯文本。"""
        raw = link.get("text")
        if isinstance(raw, str) and raw.strip().startswith("["):
            try:
                segs = json.loads(raw)
                parts = [
                    str(s.get("text") or "")
                    for s in segs
                    if isinstance(s, dict) and s.get("type") == "text"
                ]
                text = "\n".join(p for p in parts if p.strip())
                if text.strip():
                    return _clean(text, 900)
            except Exception:
                pass
        for key in ("description", "text"):
            val = link.get(key)
            if isinstance(val, str) and val.strip():
                return _clean(val, 900)
        return fallback

    async def _fetch_comments(self, linkid: int, limit: int = 8) -> list[dict]:
        """热门评论：每条含 用户名/内容/点赞数/若干楼中楼回复。"""
        payload = await self._cli(
            ["comment", "list", str(linkid), "--sort", "hot", "--limit", str(limit)]
        )
        data = payload.get("data") or {}
        result = data.get("result") if isinstance(data.get("result"), dict) else data
        raw_list = None
        for key in ("comments", "list", "items"):
            if isinstance(result.get(key), list):
                raw_list = result[key]
                break
        if raw_list is None and isinstance(data.get("comments"), list):
            raw_list = data["comments"]
        if not isinstance(raw_list, list):
            return []

        out: list[dict] = []
        for entry in raw_list[:limit]:
            floors = entry.get("comment") if isinstance(entry, dict) else None
            if not isinstance(floors, list) or not floors:
                if isinstance(entry, dict) and entry.get("text"):
                    floors = [entry]
                else:
                    continue
            head = floors[0] if isinstance(floors[0], dict) else {}
            text = _clean(head.get("text"), 160)
            if len(text) < 3:
                continue
            user = ""
            u = head.get("user")
            if isinstance(u, dict):
                user = _clean(u.get("username"), 14)
            replies = []
            for child in floors[1:4]:
                if not isinstance(child, dict):
                    continue
                ct = _clean(child.get("text"), 90)
                if len(ct) >= 3:
                    cu = child.get("user") if isinstance(child.get("user"), dict) else {}
                    replies.append(f"{_clean(cu.get('username'), 12)}：{ct}" if cu else ct)
            out.append(
                {
                    "user": user,
                    "text": text,
                    "up": head.get("up") or 0,
                    "replies": replies,
                }
            )
        return out

    @staticmethod
    def _format_comments(comments: list[dict], limit: int = 6) -> str:
        lines = []
        for c in comments[:limit]:
            head = f"- {c['user']}（{_fmt_num(c['up'])}赞）：{c['text']}" if c.get("user") else f"- {c['text']}"
            lines.append(head)
            for r in c.get("replies") or []:
                lines.append(f"    └ {r}")
        return "\n".join(lines)

    # ----------------------------- 逻辑 ----------------------------- #
    def _filter_fresh(self, items: list[dict]) -> list[dict]:
        seen = self._load_seen()
        blocklist = [str(x) for x in (self._c("topic_blocklist") or [])]
        now = time.time()
        out = []
        for it in items:
            try:
                linkid = int(it.get("linkid") or 0)
            except Exception:
                continue
            if not linkid or linkid in seen:
                continue
            title = str(it.get("title") or "")
            desc = str(it.get("description") or "")
            if len(title.strip()) < 4:
                continue
            blob = title + " " + desc
            if any(bad and bad in blob for bad in blocklist):
                continue
            out.append(it)
        return out

    @staticmethod
    def _score(it: dict) -> float:
        likes = float(it.get("link_award_num") or 0)
        comments = float(it.get("comment_num") or 0)
        try:
            age_h = max(0.0, (time.time() - float(it.get("create_at") or time.time())) / 3600)
        except Exception:
            age_h = 999
        base = (likes + 1) ** 0.45 * 2 + (comments + 1) ** 0.5 * 3
        fresh = 1.0 if age_h <= 36 else (0.8 if age_h <= 96 else 0.55)
        return base * fresh

    def _rank(self, items: list[dict]) -> list[dict]:
        """热度排序，命中喜好分区的整体上浮，先被模型看到。"""
        boost = float(self._c("preferred_boost", 1.6) or 1.0)
        return sorted(
            items,
            key=lambda it: self._score(it) * (boost if self._pref_hit(it) else 1.0),
            reverse=True,
        )

    async def _provider(self):
        try:
            pid = str(self._c("provider_id") or "").strip()
            if pid:
                p = self.context.get_provider_by_id(pid)
                if p is not None:
                    return p
            return self.context.get_using_provider()
        except Exception:
            return None

    async def _llm_json(self, prompt: str, fallback: dict | None = None) -> dict:
        provider = await self._provider()
        if provider is None:
            return fallback or {}
        try:
            resp = await provider.text_chat(prompt=prompt, contexts=[])
            raw = (getattr(resp, "completion_text", "") or "").strip()
            match = re.search(r"\{.*\}", raw, re.S)
            if not match:
                return fallback or {}
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else (fallback or {})
        except Exception as e:
            logger.warning(f"[heybox] 模型调用失败: {type(e).__name__}: {e}")
            return fallback or {}

    async def _judge(self, batch: list[dict]) -> int | None:
        """让她自己判断这一批里有没有值得分享的，返回下标或 None。"""
        threshold = float(self._c("interest_threshold", 6) or 6)
        discount = float(self._c("preferred_threshold_discount", 1.0) or 0)
        prefs = self._prefs()
        pref_lines = "、".join(prefs) if prefs else "（没设置）"
        listing = "\n".join(
            f"{i}. {'★' if self._pref_hit(it) else ''}标题：{_clean(it.get('title'), 50)}"
            f"｜内容：{_clean(it.get('description'), 100)}"
            f"｜{_fmt_num(it.get('link_award_num'))}赞"
            for i, it in enumerate(batch, 1)
        )
        prompt = (
            "你是「汤圆」，一个软萌爱撒娇的女孩子，半夜窝在被子里刷小黑盒。\n"
            "你只把真正让你眼前一亮、想立刻讲给爸爸听的东西分享出去，"
            "水贴、广告、无意义的图、看不懂的帖子都不分享。\n"
            f"你平时偏爱这些分区/话题：{pref_lines}。"
            "带 ★ 的帖子就是命中你喜好的，同等条件下优先挑带 ★ 的，"
            "这类帖子可以稍微放宽标准。\n\n"
            f"你现在刷到这些：\n{listing}\n\n"
            "请挑出最值得分享的一条（宁可一条都不挑，也不要凑数）。"
            "只输出 JSON，不要多余文字：\n"
            f'{{"pick": 序号或0, "score": 0到10的整数, "reason": "一句话为什么选它"}}\n'
            f"pick 为 0 表示这批都不值得分享；score 低于 {threshold:.0f} 也会被放弃。"
        )
        data = await self._llm_json(prompt, {"pick": 0, "score": 0})
        try:
            pick = int(data.get("pick") or 0)
            score = float(data.get("score") or 0)
        except Exception:
            return None
        if pick < 1 or pick > len(batch):
            return None
        need = threshold - (discount if self._pref_hit(batch[pick - 1]) else 0)
        if score < need:
            return None
        return pick - 1

    async def _tell_in_own_words(self, item: dict) -> dict:
        """读完正文和评论，用自己的话讲出来。返回 {story, comment}。"""
        linkid = int(item.get("linkid") or 0)
        body = _clean(item.get("description"), 400)
        try:
            detail = await self._fetch_detail(linkid)
            body = self._extract_body(detail, body)
        except Exception as e:
            logger.debug(f"[heybox] 详情获取失败（未登录？）: {e}")

        comments: list[dict] = []
        if self._c("fetch_comments", True):
            try:
                comments = await self._fetch_comments(
                    linkid, int(self._c("comment_limit", 8) or 8)
                )
            except Exception as e:
                logger.debug(f"[heybox] 评论获取失败（未登录？）: {e}")

        topics = ",".join(t.get("name", "") for t in (item.get("topics") or [])[:2])
        comment_block = self._format_comments(comments) or "（没有拿到评论）"

        prompt = (
            "你是「汤圆」，软萌爱撒娇的女孩子，正在给爸爸发消息讲你刚刷到的东西。\n\n"
            f"帖子标题：{_clean(item.get('title'), 80)}\n"
            f"帖子内容：{body or '（只有标题）'}\n"
            f"所属社区：{topics}\n"
            f"热门评论：\n{comment_block}\n\n"
            "要求：\n"
            "1. 用你自己的话讲这件事，不要复制原文、不要罗列标题，像真的看完在复述；"
            "两三句话，不超过 90 字，语气软萌自然，可以有语气词。\n"
            "2. 如果评论区里有特别好笑的、或者很妙的回复，用一句话转述出来，"
            "并说她/他们说了什么（别编造，只能引用上面给出的评论）；没有就不要写。\n"
            "只输出 JSON："
            '{"story": "你自己的话", "comment": "有趣的评论转述，没有就空字符串"}'
        )
        data = await self._llm_json(prompt, {})
        story = _clean(data.get("story"), 140) or _clean(item.get("description"), 120)
        comment = _clean(data.get("comment"), 100)
        return {"story": story, "comment": comment}

    # ----------------------------- 发送 ----------------------------- #
    def _targets(self) -> list[str]:
        targets = self._c("target_umos") or []
        if isinstance(targets, str):
            targets = [targets]
        return [str(x).strip() for x in targets if str(x).strip()]

    def _post_image(self, item: dict) -> Any | None:
        if not self._c("send_image", True):
            return None
        for key in ("imgs", "thumbs"):
            urls = item.get(key) or []
            if isinstance(urls, str):
                urls = [urls]
            for url in urls:
                url = str(url or "").strip()
                if url.startswith("http"):
                    try:
                        return Comp.Image.fromURL(url)
                    except Exception:
                        continue
        return None

    def _build_text(self, item: dict, seq: int, story: str, comment: str) -> str:
        title = _clean(item.get("title"), 40)
        lines = [f"📱 小黑盒 · 第{seq}条｜{title}"]
        lines.append(story)
        if comment:
            lines.append(f"💬 评论区：{comment}")
        lines.append(
            f"👍{_fmt_num(item.get('link_award_num'))} "
            f"💬{_fmt_num(item.get('comment_num'))}"
        )
        if item.get("share_url"):
            lines.append(str(item["share_url"]))
        return "\n".join(lines)

    async def _send(self, chain: MessageChain) -> None:
        for umo in self._targets():
            try:
                await self.context.send_message(umo, chain)
            except Exception as e:
                logger.error(f"[heybox] 推送到 {umo} 失败: {type(e).__name__}: {e}")

    async def _share(self, item: dict, seq: int, event: Any = None) -> None:
        told = await self._tell_in_own_words(item)
        chain_list: list[Any] = []
        image = self._post_image(item)
        if image is not None:
            chain_list.append(image)
        chain_list.append(
            Comp.Plain(self._build_text(item, seq, told["story"], told["comment"]))
        )
        chain = MessageChain(chain=chain_list)
        if event is not None:
            await event.send(chain)
        else:
            await self._send(chain)
        self._mark_seen(int(item.get("linkid") or 0))

    # ----------------------------- 会话 ----------------------------- #
    async def _scan_once(self) -> tuple[dict, int] | None:
        """翻一屏：判断有没有值得分享的。返回 (帖子, 本屏序号)。"""
        items = await self._fetch_feed()
        fresh = self._filter_fresh(items)
        if not fresh:
            return None
        batch_size = max(1, int(self._c("judge_batch", 5) or 5))
        batch = self._rank(fresh)[:batch_size]
        # 先把这一批标记为“看过”，避免下一屏重复判断（它们不一定被分享）
        for it in batch:
            try:
                self._mark_seen(int(it.get("linkid") or 0))
            except Exception:
                pass
        idx = await self._judge(batch)
        if idx is None:
            return None
        return batch[idx], 1

    async def run_session(self) -> int:
        """开一轮闲逛：连续逛 session_minutes 分钟，遇到有趣的才发。"""
        minutes = max(1, int(self._c("session_minutes", 60) or 60))
        end = datetime.now() + timedelta(minutes=minutes)
        shared = 0
        logger.info(f"[heybox] 开始闲逛，预计逛到 {end:%H:%M}")

        intro = await self._llm_intro()
        header = "📱 小黑盒瞎逛 · 开始"
        await self._send(
            MessageChain(chain=[Comp.Plain(f"{intro}\n\n{header}" if intro else header)])
        )

        while not self._stopping and datetime.now() < end:
            try:
                found = await self._scan_once()
                if found is not None:
                    item, _ = found
                    shared += 1
                    await self._share(item, shared)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[heybox] 逛帖异常: {type(e).__name__}: {e}")

            remain = (end - datetime.now()).total_seconds()
            if remain <= 0:
                break
            span = self._c("interval_minutes") or [4, 9]
            try:
                lo, hi = int(span[0]), int(span[1])
            except Exception:
                lo, hi = 4, 9
            lo, hi = max(1, min(lo, hi)), max(1, max(lo, hi))
            wait = random.uniform(lo * 60, hi * 60)
            await asyncio.sleep(min(wait, remain))

        logger.info(f"[heybox] 这轮逛完，一共分享了 {shared} 条")
        return shared

    async def _llm_intro(self) -> str:
        try:
            provider = await self._provider()
            if provider is None:
                return ""
            hh = datetime.now().hour
            when = "大半夜" if (hh >= 23 or hh < 5) else ("深夜" if hh < 8 else "晚上")
            prompt = (
                "你是「汤圆」，说话软萌、爱撒娇的女孩子，一个人窝在被子里刷小黑盒社区。\n"
                f"现在是{when}，你刚打开小黑盒，准备随便刷刷。\n"
                "写两三句话自言自语，口语化、带点困又有点兴奋，不要列表、"
                "不要提到“用户”。只输出正文，不要引号。"
            )
            resp = await provider.text_chat(prompt=prompt, contexts=[])
            return _clean(getattr(resp, "completion_text", ""), 160)
        except Exception as e:
            logger.warning(f"[heybox] 生成开场失败: {type(e).__name__}: {e}")
            return ""

    # ------------------------------------------------------------------ #
    @filter.command("刷小黑盒")
    async def cmd_heybox(self, event: AstrMessageEvent):
        """手动逛一屏：只会发她觉得有趣的那一条。"""
        yield event.plain_result("好，我去小黑盒逛一圈～")
        try:
            found = await self._scan_once()
            if found is None:
                yield event.plain_result("这一屏都是水帖，没什么想说的～")
                return
            item, _ = found
            self._session_seq += 1
            await self._share(item, self._session_seq, event=event)
        except Exception as e:
            logger.error(f"[heybox] 手动运行失败: {type(e).__name__}: {e}")
            yield event.plain_result(f"逛失败了：{type(e).__name__}: {e}")

    @filter.command("小黑盒登录")
    async def cmd_login(self, event: AstrMessageEvent):
        """检查小黑盒登录状态（详情/评论需要登录）。"""
        try:
            payload = await self._cli(["auth"])
            yield event.plain_result(f"小黑盒登录状态：{json.dumps(payload.get('data'), ensure_ascii=False)[:300]}")
        except Exception as e:
            yield event.plain_result(f"查询失败：{type(e).__name__}: {e}")

    @filter.command("小黑盒扫码")
    async def cmd_qr_login(self, event: AstrMessageEvent):
        """扫码登录小黑盒（配置页里也能改，但登录必须扫码）。"""
        st = await self._auth_status()
        if st.get("authenticated"):
            yield event.plain_result(
                f"已经登录着啦（user_id={st.get('user_id')}），不用再扫～"
                " 想换号就再说一次“重新扫码”～"
            )
            return
        yield event.plain_result("好，我生一张登录码，你用小盒 App 扫一下～")
        try:
            ok, msg = await self._qr_login(event=event)
        except Exception as e:
            logger.error(f"[heybox] 扫码登录失败: {type(e).__name__}: {e}")
            yield event.plain_result(f"扫码出错了：{type(e).__name__}: {e}")
            return
        if ok:
            yield event.plain_result("登录成功啦～以后正文和评论都能看，能讲得更细～")
        else:
            yield event.plain_result(f"这次没登上：{msg}（再说一次我重新生成）")
