#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端测试（活人感版：原 攒一攒 插件的 55 项，配置在 gather 分组里、存档在 state.json 的 gather 块）：真的 AstrBot（4.29.3）+ 这个插件 + 假 QQ 端（假 NapCat 反向 WebSocket）+ 假模型（OpenAI 兼容，记下收到的请求）
另外装一个测试用的小插件（只在临时文件夹里）：「接话」开头的群消息它决定要回（fatfish_kind=follow）、「求回」开头的它自己
request_llm（fatfish_kind=chat）、带「拦住」的它停掉。
  A 私聊 1 秒一条连发 3 条：模型只被叫 1 次、收到 3 条（按顺序）、只回 1 次；聊天记录里只存一遍
  B 私聊图 + 字分两条发：一起回、模型收到图
  B2 图要下载 2.5 秒、字那条先到点：等图下完一起回（不先回半截）
  C 问句等得短：从最后一条到模型被叫，问句约 1.2 秒、普通的一句约 2 秒
  L 模型还在想（3 秒）的时候他又发了两条：先回第一句，那两条并成第二次（AstrBot 的 follow-up 排队 + 这里不回两遍）
  D 私聊一直在发（1 秒一条、17 条）：15 秒上限到了就回，后面的下一轮回，每条都回到、没有回两遍
  E 群里两个人同时 @：各回各的
  F 群里 @ 以后紧接着发不带 @ 的图和话：并进去
  F2 群里 @ 以后他又 @ 别人、引用别人、别人插话：这些都不并；他自己接着说的照样并
  G 群里先发图、几秒后再 @：把先发的图并进去
  H 指令（/sid）不被拦、不被延迟、也不并进别的
  I 别的插件决定要回的（接话）也攒；@ 的和接话的并在一起按「直接找的」算
  I2 别的插件自己叫模型（request_llm，像 AstrBot 自带的主动回复）：请求里的话换成并好的；
     没写 fatfish_kind、也没被唤醒的：并了，但不当成「直接找的」（不改 is_at_or_wake_command）
  J 被别的插件停掉的那条不并进来
  K 面板接口：今天合并了几次、省掉几次、最近几次合并；没登录读不到；面板页认得出
  M 尾巴含糊才问（v0.3.0）：「我跟你说个事，」→ 问一次判断模型 → 它说「等 4」→ 他接着说的并进来；它说「回」→ 照常回；尾巴不含糊不问
  N 私聊「正在输入」（v0.3.0）：QQ 端一直说他在输入 → 接着等、后面那句并进来
  O 还排着没回、他撤回了（v0.4.0）：私聊发了一条马上撤回 → 不叫模型、不回；连发两条撤回后一条 → 只回前一条；
     群里 @ 了马上撤回 → 不回；撤回通知比消息先到 → 这条一到就认出来、不回；面板 / 日志记了
第二轮（重启）：关掉「私聊里攒」→ 私聊连发两条各回各的；「这些群不攒」里的群不攒；今天的数重启以后还在
用法：python tests/e2e_astrbot.py [插件目录 默认上一层] [--keep]
  需要装了 astrbot（测过 4.29.3）的 Python，astrbot 命令在 PATH 里；另外要 aiohttp、pyjwt、websockets、Pillow（装 astrbot 时都会带上）
  只在临时文件夹里跑：模型是假的、QQ 端是假的，不连外网。大约 2～3 分钟（AstrBot 起两次）。
"""

import asyncio
import base64
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
from aiohttp import web
import jwt
import websockets
from PIL import Image

PLUGIN_SRC = Path(
    sys.argv[1]
    if len(sys.argv) > 1 and not sys.argv[1].startswith("--")
    else Path(__file__).resolve().parent.parent
).resolve()
KEEP = "--keep" in sys.argv
PNAME = PLUGIN_SRC.name
SELF, HUMAN, HUMAN2, HUMAN3, HUMAN4, HUMAN5 = 99999, 30001, 30002, 30003, 30004, 30005
HUMAN6, HUMAN7 = 30006, 30007  # （v0.4.0）撤回那几项用
G1, G2, G3, G4, G5 = 7001, 7002, 7003, 7004, 7005
RES = []


def check(cond, msg, why=""):
    RES.append((bool(cond), msg))
    print(
        ("  [过]   " if cond else "  [没过] ")
        + msg
        + ("" if cond or not why else f"\n           {str(why)[:900]}"),
        flush=True,
    )
    return bool(cond)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


LLM_PORT, WS_PORT, DASH_PORT = free_port(), free_port(), free_port()
ROOT = Path(tempfile.mkdtemp(prefix="astrbot-e2e-gather-"))
IMG = ROOT / "img"
SECRET = "e2e-secret-0123456789abcdef0123456789"
chat_calls = []
COLORS = {
    "red": (230, 20, 20),
    "blue": (20, 40, 230),
    "green": (20, 200, 40),
    "yellow": (240, 220, 20),
}
SLOW = {"yellow.png": 2.5}  # 这张图 AstrBot 要下载 2.5 秒

HELPER = """
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart


class E2EHelper(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=5)
    async def on_msg(self, event: AstrMessageEvent):
        s = event.message_str or ""
        if s.startswith("接话"):
            event.is_at_or_wake_command = True          # 别的插件决定要回这条（本来不会回）
            event.set_extra("fatfish_kind", "follow")
        if "拦住" in s:
            event.stop_event()
        if s.startswith("求回"):                         # 别的插件自己叫模型（像 AstrBot 自带的「主动回复」）
            event.set_extra("fatfish_kind", "chat")
            yield event.request_llm(prompt=s)
        if s.startswith("主动"):                         # 跟 AstrBot 自带的「主动回复」一样：自己 request_llm、不写 fatfish_kind
            yield event.request_llm(prompt=s)

    @filter.on_llm_request(priority=-100)
    async def tag(self, event: AstrMessageEvent, req: ProviderRequest):
        req.extra_user_content_parts.append(TextPart(
            text=f"[kind={event.get_extra('fatfish_kind')}][n={event.get_extra('fatfish_gathered')}]"
                 f"[woke={event.is_at_or_wake_command}]").mark_as_temp())
"""


def make_images():
    IMG.mkdir(parents=True, exist_ok=True)
    for name, rgb in COLORS.items():
        Image.new("RGB", (64, 64), rgb).save(IMG / f"{name}.png")


def color_of(url):
    try:
        raw = (
            base64.b64decode(url.split(",", 1)[1])
            if url.startswith("data:")
            else Path(url).read_bytes()
        )
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        px = im.getpixel((im.width // 2, im.height // 2))
    except Exception:
        return "?"
    return min(COLORS, key=lambda n: sum((a - b) ** 2 for a, b in zip(COLORS[n], px)))


def text_of(m):
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(
            p.get("text", "")
            for p in c
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def images_in(m):
    c = m.get("content")
    return (
        [
            p["image_url"]["url"]
            for p in c
            if isinstance(p, dict) and p.get("type") == "image_url"
        ]
        if isinstance(c, list)
        else []
    )


# ───────────── 假模型 + 假图床 ─────────────
TAIL = {"answer": "回"}
tail_calls = []


async def chat_completions(request):
    body = await request.json()
    msgs = body.get("messages") or []
    last = msgs[-1] if msgs else {}
    system = "\n".join(text_of(m) for m in msgs if m.get("role") == "system")
    if "对方是不是已经把这一串话说完了" in system:  # 攒一攒「尾巴含糊才问」（v0.3.0）
        tail_calls.append({"t": time.time(), "body": text_of(last)})
        return web.json_response(
            {
                "id": "x",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body.get("model", "fake"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": TAIL["answer"]},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 50,
                    "completion_tokens": 2,
                    "total_tokens": 52,
                },
            }
        )
    chat_calls.append(
        {
            "t": time.time(),
            "messages": msgs,
            "last": text_of(last),
            "imgs": [color_of(u) for u in images_in(last)],
        }
    )
    text = f"收到第{len(chat_calls)}句"
    if "慢慢想" in text_of(last):
        await asyncio.sleep(3.0)  # 这句模型要想 3 秒（测「模型还在想的时候他又发了」）
    return web.json_response(
        {
            "id": "x",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model", "fake"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "total_tokens": 110,
            },
        }
    )


async def models(request):
    return web.json_response(
        {"object": "list", "data": [{"id": "fake-model", "object": "model"}]}
    )


async def serve_img(request):
    name = request.match_info["name"]
    if name in SLOW:
        await asyncio.sleep(SLOW[name])
    p = IMG / name
    if not p.exists():
        return web.Response(status=404)
    return web.Response(body=p.read_bytes(), content_type="image/png")


# ───────────── 假 QQ 端（NapCat 反向 WebSocket） ─────────────
class FakeQQ:
    def __init__(self):
        self.ws = None
        self.mid = 5000 + int(time.time()) % 1000 * 10
        self.sent = []  # (("g", 群) 或 ("p", QQ), 文字, 时间)
        self.store = {}
        self.names = {
            SELF: "大肥鱼",
            HUMAN: "老王",
            HUMAN2: "小李",
            HUMAN3: "阿珍",
            HUMAN4: "阿强",
            HUMAN5: "小美",
        }

    async def connect(self):
        for _ in range(240):
            try:
                self.ws = await websockets.connect(
                    f"ws://127.0.0.1:{WS_PORT}/ws/",
                    additional_headers={
                        "X-Self-ID": str(SELF),
                        "X-Client-Role": "Universal",
                    },
                )
                break
            except Exception:
                await asyncio.sleep(0.5)
        if not self.ws:
            raise RuntimeError("连不上 AstrBot 的反向 WebSocket")
        asyncio.create_task(self.loop())
        await self.ws.send(
            json.dumps(
                {
                    "post_type": "meta_event",
                    "meta_event_type": "lifecycle",
                    "sub_type": "connect",
                    "self_id": SELF,
                    "time": int(time.time()),
                }
            )
        )

    async def loop(self):
        try:
            async for raw in self.ws:
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                if "action" not in d:
                    continue
                act, p, echo = d["action"], d.get("params") or {}, d.get("echo")
                data = {}
                if act in ("send_group_msg", "send_private_msg", "send_msg"):
                    self.mid += 1
                    msg = p.get("message")
                    text = (
                        "".join(
                            (s.get("data") or {}).get("text", "")
                            for s in msg
                            if isinstance(s, dict) and s.get("type") == "text"
                        )
                        if isinstance(msg, list)
                        else str(msg)
                    )
                    to = (
                        ("g", int(p["group_id"]))
                        if p.get("group_id")
                        else ("p", int(p.get("user_id") or 0))
                    )
                    self.sent.append((to, text, time.time()))
                    data = {"message_id": self.mid}
                elif act == "get_group_member_info":
                    uid = int(p.get("user_id") or 0)
                    data = {
                        "user_id": uid,
                        "nickname": self.names.get(uid, str(uid)),
                        "card": self.names.get(uid, ""),
                        "role": "member",
                    }
                elif act == "get_stranger_info":
                    uid = int(p.get("user_id") or 0)
                    data = {"user_id": uid, "nickname": self.names.get(uid, str(uid))}
                elif act == "get_msg":
                    data = self.store.get(int(p.get("message_id") or 0)) or {}
                elif act == "get_login_info":
                    data = {"user_id": SELF, "nickname": "大肥鱼"}
                await self.ws.send(
                    json.dumps(
                        {"status": "ok", "retcode": 0, "data": data, "echo": echo}
                    )
                )
        except Exception:
            pass

    async def say(
        self, uid, text="", gid=None, at_bot=False, image=None, at=None, reply=None
    ):
        """gid=None 是私聊；at = @ 别人（QQ 号）；reply = 引用哪条（消息号）"""
        self.mid += 1
        segs = []
        if reply:
            segs.append({"type": "reply", "data": {"id": str(reply)}})
        if at_bot:
            segs.append({"type": "at", "data": {"qq": str(SELF)}})
        if at:
            segs.append({"type": "at", "data": {"qq": str(at)}})
        if text:
            segs.append(
                {"type": "text", "data": {"text": (" " if at_bot or at else "") + text}}
            )
        if image:
            segs.append(
                {
                    "type": "image",
                    "data": {
                        "file": f"{image.upper()}{self.mid}.png",
                        "url": f"http://127.0.0.1:{LLM_PORT}/img/{image}",
                        "summary": "",
                        "sub_type": 0,
                        "file_size": str((IMG / image).stat().st_size),
                    },
                }
            )
        ev = {
            "post_type": "message",
            "message_id": self.mid,
            "user_id": uid,
            "self_id": SELF,
            "time": int(time.time()),
            "message": segs,
            "raw_message": text,
            "font": 0,
            "sender": {
                "user_id": uid,
                "nickname": self.names.get(uid, str(uid)),
                "card": self.names.get(uid, ""),
                "role": "member",
            },
        }
        if gid:
            ev.update(message_type="group", sub_type="normal", group_id=gid)
        else:
            ev.update(message_type="private", sub_type="friend")
        self.store[self.mid] = dict(ev)
        await self.ws.send(json.dumps(ev, ensure_ascii=False))
        return time.time()

    def replies(self, to, since):
        return [s for s in self.sent if s[0] == to and s[2] >= since]

    async def recall(self, mid, uid, gid=None):
        """（v0.4.0）他撤回了一条（QQ 的撤回通知）"""
        ev = {
            "post_type": "notice",
            "notice_type": "group_recall" if gid else "friend_recall",
            "user_id": uid,
            "operator_id": uid,
            "message_id": mid,
            "self_id": SELF,
            "time": int(time.time()),
        }
        if gid:
            ev["group_id"] = gid
        await self.ws.send(json.dumps(ev, ensure_ascii=False))


def setup_root():
    env = dict(os.environ, ASTRBOT_ROOT=str(ROOT))
    subprocess.run(
        ["astrbot", "init", "-y"], cwd=ROOT, env=env, check=True, capture_output=True
    )
    shutil.copytree(
        PLUGIN_SRC,
        ROOT / "data" / "plugins" / PNAME,
        ignore=shutil.ignore_patterns("__pycache__", "dev", "*.pyc", ".git", "tests"),
    )
    hp = ROOT / "data" / "plugins" / "astrbot_plugin_e2e_helper"
    hp.mkdir(parents=True)
    (hp / "main.py").write_text(HELPER, encoding="utf-8")
    (hp / "metadata.yaml").write_text(
        "name: astrbot_plugin_e2e_helper\ndesc: e2e 测试用\nversion: v0.0.1\nauthor: e2e\n",
        encoding="utf-8",
    )
    code = (
        "from astrbot.core.config.astrbot_config import AstrBotConfig;"
        f"AstrBotConfig(config_path=r'{ROOT / 'data' / 'cmd_config.json'}')"
    )
    subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, check=True, capture_output=True
    )
    p = ROOT / "data" / "cmd_config.json"
    cfg = json.loads(p.read_text(encoding="utf-8-sig"))
    cfg["platform"] = [
        {
            "id": "napcat",
            "type": "aiocqhttp",
            "enable": True,
            "ws_reverse_host": "127.0.0.1",
            "ws_reverse_port": WS_PORT,
            "ws_reverse_token": "",
        }
    ]
    cfg["platform_settings"]["rate_limit"]["count"] = 1000
    cfg.setdefault("dashboard", {})["port"] = DASH_PORT
    cfg["dashboard"]["host"] = "127.0.0.1"
    cfg["dashboard"]["jwt_secret"] = SECRET
    base = {
        "type": "openai_chat_completion",
        "provider_type": "chat_completion",
        "enable": True,
        "key": ["sk-fake"],
        "api_base": f"http://127.0.0.1:{LLM_PORT}/v1",
        "timeout": 30,
    }
    cfg["provider"] = [
        {"id": "fake", **base, "model": "fake-model", "modalities": ["text", "image"]}
    ]
    cfg.setdefault("provider_settings", {})["default_provider_id"] = "fake"
    cfg["provider_settings"]["default_image_caption_provider_id"] = ""
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def set_plugin(group="gather", **kw):
    p = ROOT / "data" / "config" / f"{PNAME}_config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    cfg = json.loads(p.read_text(encoding="utf-8-sig")) if p.exists() else {}
    cfg.setdefault(group, {}).update(kw)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    if group == "gather" and not globals().get("_OFF"):
        globals()["_OFF"] = (
            True  # 活人感里别的模块这套测试不测：关掉，才跟原来单独装攒一攒的条件一样
        )
        for g in ("rhythm", "budget", "guard"):
            set_plugin(g, enable=False)


def log_text():
    return (ROOT / "astrbot_stdout.log").read_text(encoding="utf-8", errors="replace")


def calls_with(*markers, since=0.0):
    return [
        c
        for c in chat_calls
        if c["t"] >= since and all(mk in c["last"] for mk in markers)
    ]


async def wait_calls(markers, n=1, since=0.0, timeout=25.0):
    end = time.time() + timeout
    while time.time() < end:
        got = calls_with(*markers, since=since)
        if len(got) >= n:
            return got
        await asyncio.sleep(0.05)
    return calls_with(*markers, since=since)


async def wait_reply(qq, to, since, n=1, timeout=25.0):
    end = time.time() + timeout
    while time.time() < end:
        got = qq.replies(to, since)
        if len(got) >= n:
            return got
        await asyncio.sleep(0.05)
    return qq.replies(to, since)


def user_texts(call):
    return [text_of(m) for m in call["messages"][:-1] if m.get("role") == "user"]


async def start(env, log):
    proc = subprocess.Popen(
        ["astrbot", "run"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT
    )
    qq = FakeQQ()
    await qq.connect()
    for _ in range(120):  # 等插件都加载好
        if "astrbot_plugin_e2e_helper" in log_text() and PNAME in log_text():
            break
        await asyncio.sleep(0.5)
    await asyncio.sleep(3)
    return proc, qq


async def stop(proc, qq):
    proc.terminate()
    try:
        proc.wait(20)
    except Exception:
        proc.kill()
    try:
        await qq.ws.close()
    except Exception:
        pass


# ───────────── 各项 ─────────────
async def test_private_burst(qq):
    print("\n[A 私聊 1 秒一条连发 3 条]")
    t0 = time.time()
    for i, s in enumerate(("在吗", "问你个事", "这个电脑开不了机怎么弄")):
        await qq.say(HUMAN, s)
        if i < 2:
            await asyncio.sleep(1.0)
    got = await wait_calls(["这个电脑开不了机怎么弄"], since=t0)
    rep = await wait_reply(qq, ("p", HUMAN), t0)
    await asyncio.sleep(3.0)  # 再等一会儿：看会不会又冒出别的回复
    allc = [
        c
        for c in chat_calls
        if c["t"] >= t0 and ("在吗" in c["last"] or "问你个事" in c["last"])
    ]
    check(
        len(allc) == 1,
        f"模型只被叫了 1 次（{len(allc)} 次）",
        [c["last"][:200] for c in allc],
    )
    check(
        got and "在吗\n问你个事\n这个电脑开不了机怎么弄" in got[0]["last"],
        "模型收到了 3 条、按顺序、换行拼起来",
        got and got[0]["last"][:300],
    )
    check(
        got and "[n=3]" in got[0]["last"] and "[kind=None]" in got[0]["last"],
        "告诉别的插件：并了 3 条（fatfish_gathered=3）",
        got and got[0]["last"][-200:],
    )
    rep = qq.replies(("p", HUMAN), t0)
    check(len(rep) == 1, f"只回了 1 次（{len(rep)} 次）", rep)
    print("\n[A2 聊天记录里只存一遍]")
    t1 = time.time()
    await qq.say(HUMAN, "好的谢谢你。")
    got = await wait_calls(["好的谢谢你。"], since=t1)
    hist = user_texts(got[0]) if got else []
    zai = [h for h in hist if "在吗" in h]
    check(
        got and len(zai) == 1 and "在吗\n问你个事\n这个电脑开不了机怎么弄" in zai[0],
        "下一轮的聊天记录里：合并后的那一条，只有一遍",
        hist,
    )
    await wait_reply(qq, ("p", HUMAN), t1)


async def test_tail(qq):
    print("\n[M 尾巴含糊才问（v0.3.0）]")
    TAIL["answer"] = "等 4"
    n0, t0 = len(tail_calls), time.time()
    await qq.say(HUMAN4, "我跟你说个事，")
    await asyncio.sleep(2.6)  # 规则只等 2 秒；问了模型说再等 4 秒 → 这时候还没回
    early = qq.replies(("p", HUMAN4), t0)
    await qq.say(HUMAN4, "明天我们部门聚餐你来吗")
    got = await wait_calls(["明天我们部门聚餐你来吗"], since=t0)
    await wait_reply(qq, ("p", HUMAN4), t0)
    await asyncio.sleep(1.5)
    check(
        len(tail_calls) >= n0 + 1 and "我跟你说个事，" in tail_calls[n0]["body"],
        "尾巴含糊（逗号结尾）→ 问了一次判断模型（只给他这一串话）",
        tail_calls[n0:],
    )
    check(not early, "模型说「等 4」：规则的 2 秒到了也没回", early)
    check(
        got
        and "我跟你说个事，\n明天我们部门聚餐你来吗" in got[0]["last"]
        and len(qq.replies(("p", HUMAN4), t0)) == 1,
        "他接着说的那句并进来了，只回了一次",
        got and got[0]["last"][:200],
    )
    TAIL["answer"] = "回"
    n0, t1 = len(tail_calls), time.time()
    await qq.say(HUMAN4, "好吧那就这样…")
    rep = await wait_reply(qq, ("p", HUMAN4), t1)
    took = (rep[0][2] - t1) if rep else 99
    check(
        len(tail_calls) == n0 + 1 and rep and took < 6.5,
        f"模型说「回」：照常回（{took:.1f} 秒）",
        (tail_calls[n0:], took),
    )
    n0, t2 = len(tail_calls), time.time()
    await qq.say(HUMAN4, "我明白了谢谢你啊。")
    await wait_reply(qq, ("p", HUMAN4), t2)
    check(len(tail_calls) == n0, "尾巴不含糊：不问")


async def test_typing(qq):
    print("\n[N 私聊「正在输入」（v0.3.0）]")
    t0 = time.time()
    await qq.say(HUMAN5, "我跟你讲")
    for _ in range(5):  # QQ 端每秒说一次「对方正在输入」，说 5 秒
        await asyncio.sleep(1.0)
        await qq.ws.send(
            json.dumps(
                {
                    "post_type": "notice",
                    "notice_type": "notify",
                    "sub_type": "input_status",
                    "user_id": HUMAN5,
                    "group_id": 0,
                    "event_type": 1,
                    "status_text": "对方正在输入...",
                    "self_id": SELF,
                    "time": int(time.time()),
                },
                ensure_ascii=False,
            )
        )
    early = qq.replies(("p", HUMAN5), t0)
    await qq.say(HUMAN5, "今天晚上的电影我不去了")
    got = await wait_calls(["今天晚上的电影我不去了"], since=t0)
    await wait_reply(qq, ("p", HUMAN5), t0)
    await asyncio.sleep(1.5)
    check(not early, "QQ 端一直说他在输入：规则的 2 秒到了也没回", early)
    check(
        got
        and "我跟你讲\n今天晚上的电影我不去了" in got[0]["last"]
        and len(qq.replies(("p", HUMAN5), t0)) == 1,
        "输入完发的那句并进来了，只回了一次",
        got and got[0]["last"][:200],
    )


async def test_private_pic(qq):
    print("\n[B 私聊图 + 字分两条发]")
    t0 = time.time()
    await qq.say(HUMAN2, image="red.png")
    await asyncio.sleep(1.0)
    await qq.say(HUMAN2, "这是啥")
    got = await wait_calls(["这是啥"], since=t0)
    await wait_reply(qq, ("p", HUMAN2), t0)
    await asyncio.sleep(2.0)
    allc = [
        c
        for c in chat_calls
        if c["t"] >= t0
        and ("这是啥" in c["last"] or "[图片]" in c["last"] or "red" in c["imgs"])
    ]
    check(
        len(allc) == 1,
        f"模型只被叫了 1 次（{len(allc)} 次）",
        [c["last"][:200] for c in allc],
    )
    check(
        got and "[图片]\n这是啥" in got[0]["last"] and got[0]["imgs"] == ["red"],
        "模型收到了图本身和「[图片]\\n这是啥」",
        got and (got[0]["last"][:200], got[0]["imgs"]),
    )
    check(
        len(qq.replies(("p", HUMAN2), t0)) == 1,
        "只回了 1 次",
        qq.replies(("p", HUMAN2), t0),
    )


async def test_slow_pic(qq):
    print("\n[B2 图还在下载（2.5 秒）的时候就到点了：等它下完一起回]")
    t0 = time.time()
    await qq.say(HUMAN5, "看这个图")
    await asyncio.sleep(1.0)
    await qq.say(HUMAN5, image="yellow.png")
    await wait_calls(["看这个图"], since=t0)
    await wait_reply(qq, ("p", HUMAN5), t0)
    await asyncio.sleep(2.0)
    allc = [
        c
        for c in chat_calls
        if c["t"] >= t0 and ("看这个图" in c["last"] or "yellow" in c["imgs"])
    ]
    check(
        len(allc) == 1
        and "看这个图\n[图片]" in allc[0]["last"]
        and allc[0]["imgs"] == ["yellow"],
        "只叫了 1 次，字和图一起（没有先回半截）",
        [(c["last"][:100], c["imgs"]) for c in allc],
    )
    check(
        len(qq.replies(("p", HUMAN5), t0)) == 1,
        "只回了 1 次",
        qq.replies(("p", HUMAN5), t0),
    )


async def test_timing(qq):
    print("\n[C 问句等得短]")
    res = {}
    for name, s in (("问句", "你今天吃饭了吗"), ("普通", "我刚刚到家")):
        t0 = await qq.say(HUMAN3, s)
        got = await wait_calls([s], since=t0 - 0.5)
        res[name] = (got[0]["t"] - t0) if got else 99
        await wait_reply(qq, ("p", HUMAN3), t0)
        await asyncio.sleep(1.0)
    q, n = res["问句"], res["普通"]
    check(0.9 <= q <= 2.0, f"问句：从发出到模型被叫 {q:.2f} 秒（设的 1.2 秒）", res)
    check(
        1.8 <= n <= 3.2 and n - q >= 0.5,
        f"普通的一句：{n:.2f} 秒（设的 2 秒），比问句长",
        res,
    )


async def test_cap(qq):
    """在后台跑（20 多秒），返回结果，最后再检查"""
    t0 = time.time()
    texts = [f"连发第{i}条" for i in range(1, 18)]
    for i, s in enumerate(texts, 1):
        await qq.say(HUMAN4, s)
        await asyncio.sleep(
            1.5 if i == 15 else 1.0
        )  # 第 15 条以后停半拍：别正好压在 15 秒上限那一刻
    await asyncio.sleep(4.0)
    return t0, texts


def check_cap(qq, t0, texts):
    print("\n[D 一直在发（1 秒一条、17 条，跟上面那些同时跑的）：15 秒上限]")
    calls = [c for c in chat_calls if c["t"] >= t0 and "连发第" in c["last"]]
    first = (calls[0]["t"] - t0) if calls else 99
    got = []
    for c in calls:
        got += [ln for ln in c["last"].split("\n") if ln.startswith("连发第")]
    check(
        len(calls) == 2,
        f"模型被叫了 {len(calls)} 次（到上限回一次，剩下的下一轮）",
        [c["last"][:120] for c in calls],
    )
    check(
        14.3 <= first <= 16.8,
        f"第一次在第一条发出后 {first:.1f} 秒（上限 15 秒）",
        first,
    )
    check(
        sorted(got) == sorted(texts) and len(got) == 17,
        "17 条每条都回到了、没有一条回两遍",
        got,
    )
    check(
        len(qq.replies(("p", HUMAN4), t0)) == 2,
        "回了 2 次",
        qq.replies(("p", HUMAN4), t0),
    )


async def test_group_two(qq):
    print("\n[E 群里两个人同时 @]")
    t0 = time.time()
    await asyncio.gather(
        qq.say(HUMAN, "甲想问问明天的天气", gid=G1, at_bot=True),
        qq.say(HUMAN2, "乙想吃火锅了", gid=G1, at_bot=True),
    )
    a = await wait_calls(["甲想问问明天的天气"], since=t0)
    b = await wait_calls(["乙想吃火锅了"], since=t0)
    await wait_reply(qq, ("g", G1), t0, n=2)
    await asyncio.sleep(1.5)
    check(
        len(a) == 1 and "乙想吃火锅了" not in a[0]["last"],
        "甲：一次，只有自己那句",
        [c["last"][:200] for c in a],
    )
    check(
        len(b) == 1 and "甲想问问明天的天气" not in b[0]["last"],
        "乙：一次，只有自己那句",
        [c["last"][:200] for c in b],
    )
    check(
        len(qq.replies(("g", G1), t0)) == 2,
        "群里回了 2 次（各回各的）",
        qq.replies(("g", G1), t0),
    )


async def test_group_after(qq):
    print("\n[F @ 以后紧接着发不带 @ 的图和话]")
    t0 = time.time()
    await qq.say(HUMAN3, "帮我看看这个", gid=G2, at_bot=True)
    await asyncio.sleep(1.0)
    await qq.say(HUMAN3, gid=G2, image="green.png")
    await asyncio.sleep(1.0)
    await qq.say(HUMAN3, "怎么样", gid=G2)
    got = await wait_calls(["帮我看看这个"], since=t0)
    await wait_reply(qq, ("g", G2), t0)
    await asyncio.sleep(2.0)
    allc = [
        c
        for c in chat_calls
        if c["t"] >= t0 and ("帮我看看这个" in c["last"] or "怎么样" in c["last"])
    ]
    check(
        len(allc) == 1,
        f"模型只被叫了 1 次（{len(allc)} 次）",
        [c["last"][:200] for c in allc],
    )
    check(
        got
        and "帮我看看这个\n[图片]\n怎么样" in got[0]["last"]
        and got[0]["imgs"] == ["green"],
        "后来补的图和话都并进去了，模型看得到图",
        got and (got[0]["last"][:200], got[0]["imgs"]),
    )
    check(len(qq.replies(("g", G2), t0)) == 1, "只回了 1 次", qq.replies(("g", G2), t0))


async def test_group_to_other(qq):
    print(
        "\n[F2 @ 以后：他 @ 别人、引用别人的话、别人插进来的话 —— 都不并；他自己接着说的照样并]"
    )
    t0 = time.time()
    await qq.say(HUMAN2, "饭点了大家饿不饿", gid=G4)
    other_mid = qq.mid
    await asyncio.sleep(0.3)
    await qq.say(HUMAN, "晚上吃点什么好", gid=G4, at_bot=True)
    await asyncio.sleep(0.3)
    await qq.say(HUMAN, "你去不去呀", gid=G4, at=HUMAN2)  # @ 别人：在跟那个人说
    await asyncio.sleep(0.3)
    await qq.say(HUMAN, "哈哈哈确实", gid=G4, reply=other_mid)  # 引用别人的话
    await asyncio.sleep(0.3)
    await qq.say(HUMAN2, "我也饿了", gid=G4)  # 别人插的话（没 @）
    await asyncio.sleep(0.3)
    await qq.say(HUMAN, "我好饿", gid=G4)  # 他自己接着说（没 @）
    await wait_calls(["晚上吃点什么好"], since=t0)
    await wait_reply(qq, ("g", G4), t0)
    await asyncio.sleep(2.0)
    allc = [
        c
        for c in chat_calls
        if c["t"] >= t0
        and any(
            k in c["last"]
            for k in (
                "晚上吃点什么好",
                "你去不去呀",
                "我也饿了",
                "哈哈哈确实",
                "我好饿",
                "饭点了",
            )
        )
    ]
    last = allc[0]["last"] if allc else ""
    check(
        len(allc) == 1 and "晚上吃点什么好\n我好饿" in last,
        "@ 的那句 + 他自己接着说的并在一起，只叫 1 次",
        [c["last"][:200] for c in allc],
    )
    check(
        allc and "你去不去呀" not in last and "哈哈哈确实" not in last,
        "他 @ 别人、引用别人的那两句没并进来",
        last[:300],
    )
    check(
        allc and "我也饿了" not in last and "饭点了" not in last,
        "别人说的没并进来",
        last[:300],
    )
    check(len(qq.replies(("g", G4), t0)) == 1, "只回了 1 次", qq.replies(("g", G4), t0))


async def test_while_thinking(qq):
    print(
        "\n[L 模型还在想（3 秒）的时候他又连着发了两条：这一轮照常回完，那两条并成下一轮，不回两遍]"
    )
    t0 = time.time()
    await qq.say(
        HUMAN3, "你慢慢想一下这道题到底应该怎么做"
    )  # 长句：1.2 秒后叫模型，假模型要想 3 秒
    first = await wait_calls(["你慢慢想一下这道题到底应该怎么做"], since=t0)
    await asyncio.sleep(0.8)
    t1 = await qq.say(HUMAN3, "还有")
    await asyncio.sleep(0.5)
    await qq.say(HUMAN3, "第二个问题是什么呢")
    got = await wait_calls(["第二个问题是什么呢"], since=t0, timeout=30)
    await wait_reply(qq, ("p", HUMAN3), t0, n=2, timeout=30)
    await asyncio.sleep(3.0)
    allc = [
        c
        for c in chat_calls
        if c["t"] >= t0
        and any(k in c["last"] for k in ("慢慢想", "还有", "第二个问题"))
    ]
    check(
        first
        and len(allc) == 2
        and "还有" not in allc[0]["last"]
        and "还有\n第二个问题是什么呢" in allc[1]["last"],
        "模型一共叫了 2 次：先回第一句；想的时候发的两条并成第二次",
        [c["last"][:120] for c in allc],
    )
    check(
        got and got[0]["t"] - t1 < 6.0,
        f"第二次没拖太久（从「还有」到叫模型 {(got[0]['t'] - t1) if got else 99:.1f} 秒）",
        got and got[0]["t"] - t1,
    )
    check(
        len(qq.replies(("p", HUMAN3), t0)) == 2,
        f"回了 2 次（{len(qq.replies(('p', HUMAN3), t0))} 次）",
        qq.replies(("p", HUMAN3), t0),
    )


async def test_group_before(qq):
    print("\n[G 先发图、几秒后再 @]")
    t0 = time.time()
    await qq.say(HUMAN4, gid=G2, image="blue.png")
    await asyncio.sleep(3.0)
    check(not qq.replies(("g", G2), t0), "光发图（没 @）：不回")
    await qq.say(HUMAN4, "这是啥？", gid=G2, at_bot=True)
    got = await wait_calls(["这是啥？"], since=t0)
    await wait_reply(qq, ("g", G2), t0)
    check(
        got and "[图片]\n这是啥？" in got[0]["last"] and got[0]["imgs"] == ["blue"],
        "先发的图并进来了（模型看得到图）",
        got and (got[0]["last"][:200], got[0]["imgs"]),
    )


async def test_command(qq):
    print("\n[H 指令不被拦、不被延迟]")
    t0 = await qq.say(HUMAN5, "/sid")
    rep = await wait_reply(qq, ("p", HUMAN5), t0 - 0.2, timeout=8)
    took = (rep[0][2] - t0) if rep else 99
    check(
        rep and "UMO" in rep[0][1] and took < 1.5,
        f"/sid 马上就回了（{took:.2f} 秒）",
        rep,
    )
    t1 = await qq.say(HUMAN5, "在吗")
    await asyncio.sleep(0.3)
    t2 = await qq.say(HUMAN5, "/sid")
    rep = await wait_reply(qq, ("p", HUMAN5), t2 - 0.1, timeout=8)
    took = (rep[0][2] - t2) if rep else 99
    check(
        rep and "UMO" in rep[0][1] and took < 1.5,
        f"在攒着的时候发 /sid：照样马上回（{took:.2f} 秒）",
        rep,
    )
    got = await wait_calls(["在吗"], since=t1)
    await wait_reply(qq, ("p", HUMAN5), t1, n=2)
    check(
        got and "sid" not in got[0]["last"] and "[n=None]" in got[0]["last"],
        "「在吗」照常回，指令没并进去",
        got and got[0]["last"][:200],
    )
    check(
        not [c for c in chat_calls if c["t"] >= t0 - 0.1 and "sid" in c["last"]],
        "指令没叫模型",
    )


async def test_other_plugin(qq):
    print("\n[I 别的插件决定要回的（接话）]")
    t0 = time.time()
    await qq.say(HUMAN5, "接话一下这个", gid=G3)
    await asyncio.sleep(1.0)
    await qq.say(HUMAN5, "接话再补一句", gid=G3)
    got = await wait_calls(["接话再补一句"], since=t0)
    await wait_reply(qq, ("g", G3), t0)
    await asyncio.sleep(2.0)
    allc = [c for c in chat_calls if c["t"] >= t0 and "接话" in c["last"]]
    check(
        len(allc) == 1
        and "接话一下这个\n接话再补一句" in allc[0]["last"]
        and "[kind=follow]" in allc[0]["last"],
        "两条接话并成一次，还是「接话」（fatfish_kind=follow）",
        [c["last"][-200:] for c in allc],
    )
    t1 = time.time()
    await qq.say(HUMAN, "我想问你个问题哈", gid=G3, at_bot=True)
    await asyncio.sleep(1.0)
    await qq.say(HUMAN, "接话补充一下背景", gid=G3)
    got = await wait_calls(["接话补充一下背景"], since=t1)
    await wait_reply(qq, ("g", G3), t1)
    await asyncio.sleep(2.0)
    allc = [
        c
        for c in chat_calls
        if c["t"] >= t1 and ("我想问你个问题哈" in c["last"] or "接话补充" in c["last"])
    ]
    check(
        len(allc) == 1
        and "我想问你个问题哈\n接话补充一下背景" in allc[0]["last"]
        and "[kind=None]" in allc[0]["last"],
        "@ 的和接话的并在一起：按「直接找的」算（fatfish_kind 清掉）",
        [c["last"][-200:] for c in allc],
    )

    print("\n[I2 别的插件自己叫模型（request_llm，像 AstrBot 自带的主动回复）]")
    t3 = time.time()
    await qq.say(HUMAN4, "求回第一句", gid=G3)
    await asyncio.sleep(1.0)
    await qq.say(HUMAN4, "求回第二句", gid=G3)
    await wait_calls(["求回第二句"], since=t3)
    await wait_reply(qq, ("g", G3), t3)
    await asyncio.sleep(2.0)
    allc = [c for c in chat_calls if c["t"] >= t3 and "求回" in c["last"]]
    check(
        len(allc) == 1
        and "求回第一句\n求回第二句" in allc[0]["last"]
        and "[kind=chat]" in allc[0]["last"],
        "插件请求里的话换成了并好的两句，只叫一次",
        [c["last"][:200] for c in allc],
    )
    check(len(qq.replies(("g", G3), t3)) == 1, "只回了 1 次", qq.replies(("g", G3), t3))
    t4 = time.time()
    await qq.say(HUMAN3, "主动第一句", gid=G3)
    await asyncio.sleep(1.0)
    await qq.say(HUMAN3, "主动第二句", gid=G3)
    await wait_calls(["主动第二句"], since=t4)
    await wait_reply(qq, ("g", G3), t4)
    await asyncio.sleep(2.0)
    allc = [c for c in chat_calls if c["t"] >= t4 and "主动" in c["last"]]
    check(
        len(allc) == 1
        and "主动第一句\n主动第二句" in allc[0]["last"]
        and "[kind=None]" in allc[0]["last"]
        and "[woke=False]" in allc[0]["last"],
        "没写 fatfish_kind、也没被唤醒的插件请求（像 AstrBot 的主动回复）：并了，但不当成「直接找的」",
        [c["last"][-200:] for c in allc],
    )

    print("\n[J 被别的插件停掉的那条不并]")
    t2 = time.time()
    await qq.say(HUMAN2, "你好呀我来了")
    await asyncio.sleep(0.5)
    await qq.say(HUMAN2, "拦住这句别回")
    got = await wait_calls(["你好呀我来了"], since=t2)
    await wait_reply(qq, ("p", HUMAN2), t2)
    await asyncio.sleep(1.5)
    check(
        got
        and "拦住" not in got[0]["last"]
        and len(calls_with("你好呀我来了", since=t2)) == 1,
        "只回「你好呀我来了」，停掉的那条没并进来",
        got and got[0]["last"][:200],
    )


async def test_recall(qq):
    print("\n[O 还排着没回、他撤回了（v0.4.0）]")
    t0 = time.time()
    await qq.say(HUMAN6, "O一帮我查个东西")
    m1 = qq.mid
    await asyncio.sleep(0.6)
    await qq.recall(m1, HUMAN6)
    await asyncio.sleep(4.0)
    check(
        not calls_with("O一", since=t0) and not qq.replies(("p", HUMAN6), t0),
        "私聊发了一条、还在攒的时候他撤回了：不叫模型、不回（以前照样回）",
        (calls_with("O一", since=t0), qq.replies(("p", HUMAN6), t0)),
    )
    t0 = time.time()
    await qq.say(HUMAN6, "O二在吗")
    await asyncio.sleep(0.5)
    await qq.say(HUMAN6, "O二问你个事情好不好呀")
    m2 = qq.mid
    await asyncio.sleep(0.4)
    await qq.recall(m2, HUMAN6)
    got = await wait_calls(["O二在吗"], since=t0)
    await wait_reply(qq, ("p", HUMAN6), t0)
    await asyncio.sleep(2.0)
    check(
        got
        and "O二问你个事情" not in got[0]["last"]
        and len(calls_with("O二", since=t0)) == 1
        and len(qq.replies(("p", HUMAN6), t0)) == 1,
        "私聊连发两条、他把后一条撤回了：只回前一条（模型收到的没有撤回的那句、只回一次）",
        (
            [c["last"][-80:] for c in calls_with("O二", since=t0)],
            qq.replies(("p", HUMAN6), t0),
        ),
    )
    t0 = time.time()
    await qq.say(HUMAN7, "O三帮我看看这个", gid=G5, at_bot=True)
    m3 = qq.mid
    await asyncio.sleep(0.5)
    await qq.recall(m3, HUMAN7, gid=G5)
    await asyncio.sleep(4.0)
    check(
        not calls_with("O三", since=t0) and not qq.replies(("g", G5), t0),
        "群里 @ 了它马上撤回：不叫模型、不回",
        (calls_with("O三", since=t0), qq.replies(("g", G5), t0)),
    )
    t0 = time.time()
    await qq.recall(qq.mid + 1, HUMAN6)  # 撤回通知先到（这条还没到）
    await asyncio.sleep(0.3)
    await qq.say(HUMAN6, "O四这条早就撤回了")
    await asyncio.sleep(4.0)
    check(
        not calls_with("O四", since=t0) and not qq.replies(("p", HUMAN6), t0),
        "撤回通知比消息先到：这条一到就认出来，不回",
        (calls_with("O四", since=t0), qq.replies(("p", HUMAN6), t0)),
    )
    text = log_text()
    check(
        "把还排着的那条撤回了 → 这一轮不回" in text
        and "把最后那条撤回了 → 只回前面的 1 条" in text
        and "撤回通知比消息先到" in text,
        "日志里写了「撤回了 → 不回 / 只回前面的」",
        [x for x in text.splitlines() if "撤回" in x][-5:],
    )


async def test_panel():
    print("\n[K 面板接口]")
    token = jwt.encode(
        {"username": "astrbot", "exp": int(time.time()) + 3600},
        SECRET,
        algorithm="HS256",
    )
    hdr = {"Authorization": f"Bearer {token}"}
    api = f"http://127.0.0.1:{DASH_PORT}/api/v1/plugins/extensions/{PNAME}"
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{api}/state", headers=hdr) as r:
            d = await r.json(content_type=None)
        if os.environ.get("ALIVE_DUMP"):
            Path(os.environ["ALIVE_DUMP"]).write_text(
                json.dumps((d or {}).get("data") or {}, ensure_ascii=False),
                encoding="utf-8",
            )
        st = ((d or {}).get("data") or {}).get("gather") or {}
        today = st.get("today") or {}
        check(
            today.get("recall_skip", 0) >= 3 and today.get("recalled", 0) >= 4,
            f"面板（v0.4.0）：他撤回了 → 少并了 {today.get('recalled')} 条、{today.get('recall_skip')} 轮没回",
            today,
        )
        check(
            r.status == 200
            and today.get("merges", 0) >= 6
            and today.get("saved", 0) >= 8
            and today.get("msgs", 0) >= 20,
            f"今天合并了 {today.get('merges')} 次、省掉 {today.get('saved')} 次调用、并了 {today.get('msgs')} 条",
            str(d)[:500],
        )
        rc = st.get("recent") or []
        check(
            any(
                x.get("n") == 3
                and x.get("where") == "私聊"
                and x.get("who") == "老王"
                and 1.5 <= (x.get("waited") or 0) <= 6
                for x in rc
            )
            and any(x.get("where") == f"群 {G2}" and x.get("pics") == 1 for x in rc),
            "最近几次合并：谁、在哪、几条、等了几秒、几张图",
            rc[:4],
        )
        async with s.get(f"{api}/state") as r:
            check(r.status in (401, 403), f"没登录读不到（{r.status}）")
        async with s.get(
            f"http://127.0.0.1:{DASH_PORT}/api/v1/plugins/{PNAME}/views/panel",
            headers=hdr,
        ) as r:
            d = await r.json(content_type=None)
        url = None

        def _find(o):
            nonlocal url
            if isinstance(o, dict):
                for v in o.values():
                    if isinstance(v, str) and "/_t/" in v and url is None:
                        url = v
                    _find(v)
            elif isinstance(o, list):
                for v in o:
                    _find(v)

        _find(d)
        check(
            r.status == 200 and url,
            "面板页 panel 认出来了",
            json.dumps(d, ensure_ascii=False)[:400],
        )
        if url:
            full = (
                url if url.startswith("http") else f"http://127.0.0.1:{DASH_PORT}{url}"
            )
            async with s.get(full, headers=hdr) as r2:
                html = await r2.text()
            check(
                r2.status == 200 and "攒一攒" in html and "bridge-sdk.js" in html,
                "面板页 HTML 拿得到、AstrBot 插上了桥接脚本",
                (r2.status, html[:200]),
            )
        return today


async def main():
    make_images()
    setup_root()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat_completions)
    app.router.add_get("/v1/models", models)
    app.router.add_get("/img/{name}", serve_img)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", LLM_PORT).start()

    env = dict(
        os.environ,
        ASTRBOT_ROOT=str(ROOT),
        DASHBOARD_PORT=str(DASH_PORT),
        PYTHONUNBUFFERED="1",
    )
    for k in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        env[k] = (
            "http://127.0.0.1:9"  # AstrBot 启动时自己想连外网：让它连不出去（测试不连外网）
        )
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    env["ASTRBOT_DISABLE_METRICS"] = (
        "1"  # 不上报用量统计（AstrBot 启动时只删小写的代理变量，这里再保险一下）
    )
    log = open(ROOT / "astrbot_stdout.log", "w", encoding="utf-8")
    proc, qq = None, None
    try:
        proc, qq = await start(env, log)
        text = log_text()
        check(
            PNAME in text and "Traceback" not in text,
            "AstrBot 起来了、插件加载了、没有报错",
            text[-1500:],
        )
        await test_private_burst(qq)
        await test_private_pic(qq)
        await test_slow_pic(qq)
        await test_timing(qq)
        await test_while_thinking(qq)
        print("\n（D 一直在发：在后台跑 20 多秒，同时测下面群里的；结果最后出来）")
        cap = asyncio.ensure_future(test_cap(qq))  # 不同的人、不同的会话，互不影响
        await asyncio.sleep(0.5)
        await test_group_two(qq)
        await test_group_after(qq)
        await test_group_to_other(qq)
        await test_group_before(qq)
        await test_command(qq)
        await test_other_plugin(qq)
        check_cap(qq, *(await cap))
        await test_tail(qq)
        await test_typing(qq)
        await test_recall(qq)
        today = await test_panel()
        text = log_text()
        check(
            "Traceback" not in text,
            "第一轮 AstrBot 日志里没有 Traceback",
            text[text.find("Traceback") - 300 :][:1500],
        )
        check(
            "[攒一攒]" in text and "连着发了 3 条" in text,
            "日志里有「[攒一攒] … 连着发了 3 条，等了 … 秒，一起回」",
        )

        print("\n[第二轮 重启：关掉「私聊里攒」]")
        await stop(proc, qq)
        state = (
            json.loads(
                (ROOT / "data" / "plugin_data" / PNAME / "state.json").read_text(
                    encoding="utf-8"
                )
            ).get("gather")
            or {}
        )
        check(
            (state.get("stats") or {}).get("merges") == today.get("merges")
            and len(state.get("recent") or []) >= 6,
            "关掉的时候存了档（今天的数、最近几次合并）",
            (state.get("stats"), today),
        )
        set_plugin(private=False, skip_groups=[str(G4)])
        proc, qq = await start(env, log)
        t0 = time.time()
        await qq.say(HUMAN, "第二轮在吗")
        await asyncio.sleep(1.0)
        await qq.say(HUMAN, "第二轮问你个事")
        a = await wait_calls(["第二轮在吗"], since=t0)
        b = await wait_calls(["第二轮问你个事"], since=t0)
        await wait_reply(qq, ("p", HUMAN), t0, n=2)
        check(
            a and b and a[0] is not b[0] and a[0]["t"] - t0 < 1.0,
            "私聊不攒了：两条各叫一次模型，第一条马上就叫",
            (len(a), len(b), a and a[0]["t"] - t0),
        )
        t1 = time.time()
        await qq.say(HUMAN2, "群里照样攒", gid=G1, at_bot=True)
        await asyncio.sleep(1.0)
        await qq.say(HUMAN2, "对吧", gid=G1)
        got = await wait_calls(["群里照样攒"], since=t1)
        check(
            got and "群里照样攒\n对吧" in got[0]["last"],
            "群里照样攒",
            got and got[0]["last"][:200],
        )
        t3 = time.time()
        await qq.say(HUMAN3, "这个群不攒了吧", gid=G4, at_bot=True)
        await asyncio.sleep(1.0)
        await qq.say(HUMAN3, "嗯嗯", gid=G4)
        got = await wait_calls(["这个群不攒了吧"], since=t3)
        await asyncio.sleep(1.5)
        check(
            got and got[0]["t"] - t3 < 1.0 and "嗯嗯" not in got[0]["last"],
            "填进「这些群不攒」的群：@ 了马上叫模型，后面那句不并",
            got and (got[0]["t"] - t3, got[0]["last"][:200]),
        )
        async with aiohttp.ClientSession() as s:
            token = jwt.encode(
                {"username": "astrbot", "exp": int(time.time()) + 3600},
                SECRET,
                algorithm="HS256",
            )
            async with s.get(
                f"http://127.0.0.1:{DASH_PORT}/api/v1/plugins/extensions/{PNAME}/state",
                headers={"Authorization": f"Bearer {token}"},
            ) as r:
                d = await r.json(content_type=None)
        t2 = (((d or {}).get("data") or {}).get("gather") or {}).get("today") or {}
        check(
            t2.get("merges", 0) >= (today.get("merges") or 0) + 1,
            f"今天的数重启以后接着算（{today.get('merges')} → {t2.get('merges')}）",
            t2,
        )
        text = log_text()
        check(
            "Traceback" not in text,
            "整个过程 AstrBot 日志里没有 Traceback",
            text[text.find("Traceback") - 300 :][:1500],
        )
    finally:
        if proc is not None:
            await stop(proc, qq)
        await runner.cleanup()
        log.close()

    ok = sum(1 for c, _ in RES if c)
    bad = [m for c, m in RES if not c]
    print("\n" + "=" * 60)
    print(f"  结果：{ok} 项过了" + (f"，{len(bad)} 项没过" if bad else "，全过"))
    for m in bad:
        print("    -", m)
    print("=" * 60)
    if KEEP or bad:
        print("AstrBot 目录留着：", ROOT)
    else:
        shutil.rmtree(ROOT, ignore_errors=True)
    sys.exit(1 if bad else 0)


asyncio.run(main())
