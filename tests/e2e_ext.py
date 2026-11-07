#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
跟真的第三方同类插件一起装（v0.4.0）：真的 AstrBot + 这个插件 + 旁边的大肥鱼插件（有就装，--alone 不装）
+ 真的 angel_heart / reply_merge / qq_enhance（--ext 指的目录里有哪个装哪个）+ 假 QQ 端 + 假模型。
  0 都加载了；本插件和一起装的大肥鱼插件没有报错堆栈
  1 让路：装了 angel_heart → 攒一攒、群聊节奏、省 token 的拦截让路（照样记账）；reply_merge、qq_enhance（防抖开着）→ 攒一攒让路
  2 私聊有回话（经过它们的防抖 / 扣押）；发消息模块不让路：AstrBot 开着分段回复，发了第一段他撤回了 → 剩下的不发
  3 群里 @ 它：本插件不报错（回不回由 angel_heart 定）
第三方插件的依赖（feedparser、mdit_plain、trafilatura）：先装到一个单独的文件夹，用 --deps 指过来（放进 PYTHONPATH，不动 AstrBot 的环境）。
用法：python tests/e2e_ext.py [插件目录 默认上一层] --ext 第三方插件目录 [--deps 依赖目录] [--keep] [--alone]
"""

import asyncio
import json
import os
import re
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


def _arg(name):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


POS = [
    a
    for i, a in enumerate(sys.argv[1:], 1)
    if not a.startswith("--") and sys.argv[i - 1] not in ("--ext", "--deps")
]
PLUGIN_SRC = Path(POS[0] if POS else Path(__file__).resolve().parent.parent).resolve()
EXT = Path(_arg("--ext")).resolve() if _arg("--ext") else None
DEPS = Path(_arg("--deps")).resolve() if _arg("--deps") else None
KEEP, ALONE = "--keep" in sys.argv, "--alone" in sys.argv
PNAME = PLUGIN_SRC.name
SIBLINGS = [
    n
    for n in (
        "astrbot_plugin_fatfish_aiguard",
        "astrbot_plugin_fatfish_seememe",
        "astrbot_plugin_fatfish_fixup",
    )
    if not ALONE and (PLUGIN_SRC.parent / n / "main.py").is_file()
]
EXTS = [
    n
    for n in (
        "astrbot_plugin_angel_heart",
        "astrbot_plugin_reply_merge",
        "astrbot_plugin_qq_enhance",
    )
    if EXT and (EXT / n / "main.py").is_file()
]
SELF, A, B, ADMIN = 99999, 30001, 30002, 30009
G1 = 7501
RES = []
BIN = Path(sys.executable).parent
ASTRBOT = str(BIN / "astrbot") if (BIN / "astrbot").exists() else "astrbot"


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
ROOT = Path(tempfile.mkdtemp(prefix="astrbot-e2e-ext-"))
SECRET = "e2e-secret-0123456789abcdef0123456789"
chat_calls = []


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


async def chat_completions(request):
    body = await request.json()
    msgs = body.get("messages") or []
    last = text_of(msgs[-1]) if msgs else ""
    chat_calls.append({"last": last, "t": time.time()})
    if "BRAKE" in last:
        text = "刹一开头这一句。刹二这是第二句话。刹三接着第三句。刹四还有第四句。刹五最后一句了。"
    else:
        text = f"收到第{len(chat_calls)}句"
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


class FakeQQ:
    def __init__(self):
        self.ws, self.sent, self.store = None, [], {}
        self.mid = 5000 + int(time.time()) % 1000 * 10
        self.new_sent = asyncio.Event()

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
                if act in ("send_group_msg", "send_msg", "send_private_msg"):
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
                    gid = int(p.get("group_id") or 0) or -int(p.get("user_id") or 0)
                    self.sent.append((gid, text, time.time(), self.mid))
                    data = {"message_id": self.mid}
                    self.new_sent.set()
                elif act == "get_login_info":
                    data = {"user_id": SELF, "nickname": "大头鲢"}
                elif act in ("get_group_member_info", "get_stranger_info"):
                    uid = int(p.get("user_id") or 0)
                    data = {
                        "user_id": uid,
                        "nickname": str(uid),
                        "card": "",
                        "role": "member",
                    }
                elif act == "get_msg":
                    data = self.store.get(int(p.get("message_id") or 0)) or {}
                elif (
                    act == "get_friend_list"
                ):  # qq_enhance 每条私聊都要查一下是不是好友（临时会话限制）
                    data = [
                        {"user_id": u, "nickname": f"群友{u}", "remark": ""}
                        for u in (A, B, ADMIN)
                    ]
                elif act == "get_group_list":
                    data = [
                        {"group_id": G1, "group_name": "测试群 7501", "member_count": 3}
                    ]
                await self.ws.send(
                    json.dumps(
                        {"status": "ok", "retcode": 0, "data": data, "echo": echo}
                    )
                )
        except Exception:
            pass

    async def say(self, uid, text, gid=None, at_bot=False):
        self.mid += 1
        segs = []
        if at_bot:
            segs.append({"type": "at", "data": {"qq": str(SELF)}})
        segs.append({"type": "text", "data": {"text": (" " if at_bot else "") + text}})
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
                "nickname": f"群友{uid}",
                "card": "",
                "role": "member",
            },
        }
        if gid:
            ev.update(message_type="group", sub_type="normal", group_id=gid)
        else:
            ev.update(message_type="private", sub_type="friend")
        self.store[self.mid] = dict(ev)
        await self.ws.send(json.dumps(ev, ensure_ascii=False))
        return self.mid

    async def recall(self, mid, uid, gid=None):
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

    async def wait_n(self, gid, since, n, timeout=30.0):
        end = time.time() + timeout
        while time.time() < end:
            got = [x for x in self.sent if x[0] == gid and x[2] >= since]
            if len(got) >= n:
                return got
            self.new_sent.clear()
            try:
                await asyncio.wait_for(
                    self.new_sent.wait(), max(0.05, end - time.time())
                )
            except (asyncio.TimeoutError, TimeoutError):
                pass
        return [x for x in self.sent if x[0] == gid and x[2] >= since]


def setup_root():
    env = dict(os.environ, ASTRBOT_ROOT=str(ROOT))
    subprocess.run(
        [ASTRBOT, "init", "-y"], cwd=ROOT, env=env, check=True, capture_output=True
    )
    srcs = (
        [PLUGIN_SRC]
        + [PLUGIN_SRC.parent / n for n in SIBLINGS]
        + [EXT / n for n in EXTS]
    )
    for src in srcs:
        shutil.copytree(
            src,
            ROOT / "data" / "plugins" / src.name,
            ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", ".git", "tests", ".ruff_cache"
            ),
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
    ps = cfg["platform_settings"]
    ps["rate_limit"]["count"] = 1000
    ps["segmented_reply"].update(
        enable=True, interval_method="random", interval="0.8,1.0"
    )
    cfg["admins_id"] = [str(ADMIN)]
    cfg.setdefault("dashboard", {}).update(
        port=DASH_PORT, host="127.0.0.1", jwt_secret=SECRET
    )
    cfg["provider"] = [
        {
            "id": "fake",
            "type": "openai_chat_completion",
            "provider_type": "chat_completion",
            "enable": True,
            "key": ["sk-fake"],
            "api_base": f"http://127.0.0.1:{LLM_PORT}/v1",
            "timeout": 30,
            "model": "fake-model",
            "modalities": ["text"],
        }
    ]
    cfg.setdefault("provider_settings", {})["default_provider_id"] = "fake"
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    cdir = ROOT / "data" / "config"
    cdir.mkdir(parents=True, exist_ok=True)
    if "astrbot_plugin_fatfish_aiguard" in SIBLINGS:  # 认 AI：不自动认、不刹车
        (cdir / "astrbot_plugin_fatfish_aiguard_config.json").write_text(
            json.dumps({"enable_detect": False, "brake_enable": False}),
            encoding="utf-8",
        )


def log_text():
    return (ROOT / "astrbot_stdout.log").read_text(encoding="utf-8", errors="replace")


def tbs(text, names):
    return [
        blk[:300]
        for blk in text.split("Traceback (most recent call last)")[1:]
        if any(n in blk[:6000].replace("\\", "/") for n in names)
    ]


async def main():
    print(
        f"一起装的大肥鱼插件：{'、'.join(SIBLINGS) or '没有'}；第三方：{'、'.join(EXTS) or '没有'}"
    )
    if not EXTS:
        check(
            False,
            "找到第三方插件（--ext 指一个有 angel_heart / reply_merge / qq_enhance 的目录）",
            EXT,
        )
        return
    setup_root()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat_completions)
    app.router.add_get("/v1/models", models)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", LLM_PORT).start()
    env = dict(
        os.environ,
        ASTRBOT_ROOT=str(ROOT),
        DASHBOARD_PORT=str(DASH_PORT),
        PYTHONUNBUFFERED="1",
        ASTRBOT_DISABLE_METRICS="1",
    )
    for k in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        env[k] = "http://127.0.0.1:9"  # 不连外网（AstrBot、插件自己想连的也连不出去）
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    if DEPS:
        env["PYTHONPATH"] = str(DEPS) + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
    log = open(ROOT / "astrbot_stdout.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [ASTRBOT, "run"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT
    )
    tok = jwt.encode(
        {"username": "astrbot", "exp": int(time.time()) + 3600},
        SECRET,
        algorithm="HS256",
    )
    hdr = {"Authorization": f"Bearer {tok}"}
    s = None
    try:
        qq = FakeQQ()
        await qq.connect()
        for _ in range(120):
            t = log_text()
            if "[活人感] v" in t and all(n in t for n in EXTS):
                break
            await asyncio.sleep(0.5)
        await asyncio.sleep(5)
        s = aiohttp.ClientSession()
        text = log_text()
        print("\n[0 都加载了]")
        ver = re.search(
            r'^VERSION = "([^"]+)"',
            (PLUGIN_SRC / "main.py").read_text(encoding="utf-8"),
            re.M,
        ).group(1)
        check(f"[活人感] v{ver} 已启用" in text, f"活人感 v{ver} 加载了", text[-1500:])
        async with s.get(
            f"http://127.0.0.1:{DASH_PORT}/api/plugin/get", headers=hdr
        ) as r:
            d = await r.json(content_type=None)
        pl = {
            x.get("name"): x
            for x in ((d or {}).get("data") or [])
            if isinstance(x, dict)
        }
        for n in EXTS + SIBLINGS + [PNAME]:
            check(
                n in pl and pl[n].get("activated", True) is not False,
                f"{n} 装上、启用了",
                {k: pl.get(n, {}).get(k) for k in ("activated", "version")}
                if n in pl
                else list(pl)[:20],
            )
        async with s.get(
            f"http://127.0.0.1:{DASH_PORT}/api/v1/plugins/extensions/{PNAME}/state",
            headers=hdr,
        ) as r:
            st = ((await r.json(content_type=None)) or {}).get("data") or {}
        al = st.get("alive") or {}
        found = {x["label"]: x for x in (al.get("yield") or {}).get("found") or []}
        mods = al.get("modules") or {}
        print("\n[1 让路]")
        if "astrbot_plugin_angel_heart" in EXTS:
            check(
                "angel_heart" in found
                and set(found["angel_heart"]["mods"]) == {"gather", "rhythm", "budget"}
                and mods.get("budget", {}).get("on"),
                "angel_heart：攒一攒、群聊节奏、省 token 的拦截让路（省 token 照样记账）",
                (found.get("angel_heart"), mods.get("budget")),
            )
        if "astrbot_plugin_reply_merge" in EXTS:
            check(
                "reply_merge" in found and "gather" in found["reply_merge"]["mods"],
                "reply_merge：攒一攒让路",
                found.get("reply_merge"),
            )
        if "astrbot_plugin_qq_enhance" in EXTS:
            check(
                "qq_enhance" in found and "gather" in found["qq_enhance"]["mods"],
                "qq_enhance（消息防抖默认开着）：攒一攒让路",
                found.get("qq_enhance"),
            )
        check(
            mods.get("send", {}).get("on") and mods.get("guard", {}).get("on"),
            "发消息、兜底不让路（照常工作）",
            mods,
        )
        print("\n[2 私聊：有回话；发了第一段他撤回了 → 剩下的不发]")
        P = -A
        t0 = time.time()
        await qq.say(A, "你好呀在吗")
        got = await qq.wait_n(P, t0, 1, timeout=40)
        check(
            got,
            "私聊有回话（经过第三方插件的防抖 / 扣押）",
            [x[1] for x in got] or log_text()[-1200:],
        )
        await asyncio.sleep(3.0)
        t0 = time.time()
        m = await qq.say(A, "BRAKE 一起装也叫停")
        got = await qq.wait_n(P, t0, 1, timeout=40)
        if got:
            await qq.recall(m, A)
        await asyncio.sleep(8.0)
        sent = [x[1] for x in qq.sent if x[0] == P and x[2] >= t0]
        check(
            got
            and sent[0].startswith("刹一")
            and not any(x.startswith(("刹三", "刹四", "刹五")) for x in sent),
            "AstrBot 分段发、发了第一段他撤回了：剩下的不发（叫停在发消息模块，不让路）",
            sent,
        )
        print("\n[3 群里 @ 它]")
        t0 = time.time()
        await qq.say(B, "大家好", gid=G1, at_bot=True)
        got = await qq.wait_n(G1, t0, 1, timeout=25)
        print(f"  （群里回了 {len(got)} 条；回不回由 angel_heart 定）")
        await asyncio.sleep(2.0)
        text = log_text()
        mine = tbs(text, [PNAME])
        check(not mine, "AstrBot 日志里没有本插件的报错堆栈", mine[:2])
        oops = [x for x in text.splitlines() if "[活人感]" in x and "出错" in x]
        check(not oops, "日志里没有「[活人感] …出错」", oops[:3])
        if SIBLINGS:
            bad = tbs(text, SIBLINGS)
            check(not bad, "一起装的大肥鱼插件也没有报错堆栈", bad[:2])
        other = tbs(text, EXTS)
        print(f"  （第三方插件自己的报错堆栈：{len(other)} 个；不算本插件的问题）")
    finally:
        if s is not None:
            await s.close()
        proc.terminate()
        try:
            proc.wait(20)
        except Exception:
            proc.kill()
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
