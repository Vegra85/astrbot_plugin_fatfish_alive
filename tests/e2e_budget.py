#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端测试（活人感版：原 省 token 插件的 64 项，配置在 budget 分组里、存档在 state.json 的 budget 块、面板接口 action 带 m=budget）：真的 AstrBot（4.29.3）+ 这个插件 + 假 QQ 端（假 NapCat 反向 WebSocket）+ 假模型（OpenAI 兼容，回话带 usage）
另外装一个测试用的小插件：按消息开头写 fatfish_kind（「接话:」→ follow、「插话:」→ chat、「冒泡:」→ extra），再注册一个工具 e2e_ping。
为了结果确定，测试里把曲线调得很陡：花一点点钱意愿就到 0（找它 / 插话 / 冒泡的次方 1000），私聊、接话的次方 0（永远回）。
第一轮：
  A 记账：第一次 @（还没花钱）照常回；DeepSeek 的 prompt_cache_hit_tokens / miss 记对了、折成美元对了
  B 拦：再 @ 一次 → 不调模型、不回话、不报错；日志里有「[省token] … 这次不回」；
    测试小插件在同一个钩子（默认优先级 0）里发「正在思考中」：被拦的那条它也没发出去
  C 私聊照常回；管理员 @ 不限
  D 场合从别的插件写的 fatfish_kind 读：接话（次方 0）回；插话、冒泡（插件 yield request_llm 的）被拦
  E 指令：非管理员哪个都用不了；/省token（光发）= 状态（没有「参数不足」）；火力全开 2 小时 → 找它 / 插话都回
    （没拦的「正在思考中」照发）；省着点 → 又拦；火力全开 一直；重新算 → 意愿回到 100%、回了一次又花了钱 → 又拦
  F 记账：OpenAI 格式（prompt_tokens_details.cached_tokens）；一轮里调了工具（两次调用）两次都记上；
    别的插件调 record_usage / ask（给别的插件用的两个接口）；模型不回 usage → 日志里提醒
  G 面板接口：状态、开关火力全开、开 10 秒到点自己关、重新算 / 恢复、没登录读不到、面板页认得出；
    WebUI 改配置（插件热重载：老的 terminate、新的读存档）→ 新配置生效、状态都在、照常工作
第二轮（重启：AstrBot 被结束时不调 terminate）：花费、火力全开（面板开的「一直」）、今天的数都还在；火力全开时 @ 照常回；
  重启前手改坏一条存档：读进来时扔掉，掷骰子、面板都不出错
用法：python tests/e2e_astrbot.py [插件目录 默认上一层] [--keep]
  需要装了 astrbot（测过 4.29.3）的 Python；另外要 aiohttp、pyjwt、websockets（装 astrbot 时都会带上）
  只在临时文件夹里跑：模型是假的、QQ 端是假的，不连外网
"""

import asyncio
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

PLUGIN_SRC = Path(
    sys.argv[1]
    if len(sys.argv) > 1 and not sys.argv[1].startswith("--")
    else Path(__file__).resolve().parent.parent
).resolve()
KEEP = "--keep" in sys.argv
PNAME = PLUGIN_SRC.name
ASTRBOT = shutil.which("astrbot") or str(Path(sys.executable).parent / "astrbot")
SELF, HUMAN, HUMAN2, ADMIN = 99999, 30001, 30002, 30009
G1, G2 = 7001, 7002
RES = []
PRICE = {"miss": 0.15, "hit": 0.003, "out": 0.60}


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
ROOT = Path(tempfile.mkdtemp(prefix="astrbot-e2e-budget-"))
SECRET = "e2e-secret-0123456789abcdef0123456789"
chat_calls = []

# ───────────── 测试用的小插件：写 fatfish_kind + 一个工具 ─────────────
HELPER = "astrbot_plugin_e2e_kindsetter"
HELPER_MAIN = '''# -*- coding: utf-8 -*-
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star


class E2EKindSetter(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)

    @filter.on_waiting_llm_request()
    async def thinking(self, event: AstrMessageEvent):
        """像 AstrBot 文档里的例子：要叫模型之前先发一句「正在思考中」（默认优先级 0；只管带「思考测试」的消息）"""
        if "思考测试" in (event.message_str or ""):
            await event.send(MessageChain().message("正在思考中..."))

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=10)
    async def on_group(self, event: AstrMessageEvent):
        text = event.message_str or ""
        for tag, kind in (("接话:", "follow"), ("插话:", "chat"), ("冒泡:", "extra")):
            if text.startswith(tag):
                event.set_extra("fatfish_kind", kind)
                if kind == "extra":
                    yield event.request_llm(prompt=text)        # 插件自己发起的请求
                else:
                    event.is_at_or_wake_command = True          # 让 AstrBot 照常叫模型
                return

    @filter.command("e2e记账")
    async def e2e_record(self, event: AstrMessageEvent):
        """别的插件怎么用省 token：自己调模型的花费算进来、主动发言前问一下"""
        meta = self.context.get_registered_star("astrbot_plugin_fatfish_alive")
        b = meta.star_cls if meta and meta.activated else None
        if b is None:
            yield event.plain_result("没找到省 token 插件")
            return
        usd = b.record_usage({"prompt_tokens": 1000, "completion_tokens": 10, "prompt_cache_hit_tokens": 600})
        ok, why = b.ask("extra", conv=event.unified_msg_origin, uid="", name="定时冒泡", where="测试")
        yield event.plain_result(f"记了 {usd:.9f} 问了 {ok} {why} 火力全开 {b.full_on()}")

    @filter.llm_tool(name="e2e_ping")
    async def e2e_ping(self, event: AstrMessageEvent, word: str):
        """测试用的工具：原样返回。

        Args:
            word(string): 随便一个词
        """
        return f"pong {word}"
'''


def install_helper():
    d = ROOT / "data" / "plugins" / HELPER
    d.mkdir(parents=True, exist_ok=True)
    (d / "main.py").write_text(HELPER_MAIN, encoding="utf-8")
    (d / "metadata.yaml").write_text(
        f"name: {HELPER}\ndesc: e2e 测试用\nversion: v0.0.1\nauthor: e2e\n",
        encoding="utf-8",
    )


# ───────────── 假模型：回话带 usage ─────────────
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


def last_user_text(msgs):
    for m in reversed(msgs):
        if m.get("role") == "user":
            return text_of(m)
    return ""


async def chat_completions(request):
    body = await request.json()
    msgs = body.get("messages") or []
    lu = last_user_text(msgs)
    has_tool_result = any(m.get("role") == "tool" for m in msgs)
    tools = [((t.get("function") or {}).get("name")) for t in body.get("tools") or []]
    chat_calls.append(
        {
            "model": body.get("model"),
            "messages": msgs,
            "user": lu,
            "tools": tools,
            "t": time.time(),
        }
    )
    msg = {"role": "assistant", "content": f"收到第{len(chat_calls)}句"}
    finish = "stop"
    if "OPENAI格式" in lu:  # OpenAI 的写法：缓存数在 prompt_tokens_details 里
        usage = {
            "prompt_tokens": 2000,
            "completion_tokens": 100,
            "total_tokens": 2100,
            "prompt_tokens_details": {"cached_tokens": 1500},
        }
    elif "工具测试" in lu and not has_tool_result and "e2e_ping" in tools:
        msg = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "e2e_ping",
                        "arguments": json.dumps({"word": "hi"}),
                    },
                }
            ],
        }
        finish = "tool_calls"
        usage = {
            "prompt_tokens": 400,
            "completion_tokens": 20,
            "total_tokens": 420,
            "prompt_cache_hit_tokens": 300,
            "prompt_cache_miss_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 300},
        }
    elif "工具测试" in lu and has_tool_result:
        msg = {"role": "assistant", "content": "工具说 pong"}
        usage = {
            "prompt_tokens": 1000,
            "completion_tokens": 30,
            "total_tokens": 1030,
            "prompt_cache_hit_tokens": 900,
            "prompt_cache_miss_tokens": 100,
        }
    elif "无用量" in lu:  # 有的接口不回 usage
        usage = None
    else:  # DeepSeek 的写法：只有 prompt_cache_hit_tokens / prompt_cache_miss_tokens
        usage = {
            "prompt_tokens": 1000,
            "completion_tokens": 50,
            "total_tokens": 1050,
            "prompt_cache_hit_tokens": 800,
            "prompt_cache_miss_tokens": 200,
        }
    out = {
        "id": "x",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", "fake"),
        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
    }
    if usage is not None:
        out["usage"] = usage
    return web.json_response(out)


async def models(request):
    return web.json_response(
        {"object": "list", "data": [{"id": "fake-model", "object": "model"}]}
    )


# ───────────── 假 QQ 端（NapCat 反向 WebSocket） ─────────────
class FakeQQ:
    def __init__(self):
        self.ws = None
        self.mid = 1000 + int(time.time()) % 1000 * 10
        self.sent = []  # (("g", 群) 或 ("p", QQ), 文字, 时间)
        self.names = {SELF: "大肥鱼", HUMAN: "老王", HUMAN2: "小李", ADMIN: "管理员"}
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
                    self.new_sent.set()
                elif act == "get_group_member_info":
                    uid = int(p.get("user_id") or 0)
                    data = {
                        "user_id": uid,
                        "nickname": self.names.get(uid, str(uid)),
                        "card": self.names.get(uid, ""),
                        "role": "member",
                    }
                elif act in ("get_stranger_info", "get_login_info"):
                    uid = int(p.get("user_id") or SELF)
                    data = {"user_id": uid, "nickname": self.names.get(uid, str(uid))}
                await self.ws.send(
                    json.dumps(
                        {"status": "ok", "retcode": 0, "data": data, "echo": echo}
                    )
                )
        except Exception:
            pass

    async def say(self, gid, uid, text, at_bot=False):
        """gid = 0 → 私聊"""
        self.mid += 1
        segs = []
        if at_bot:
            segs.append({"type": "at", "data": {"qq": str(SELF)}})
        segs.append({"type": "text", "data": {"text": (" " if at_bot else "") + text}})
        sender = {
            "user_id": uid,
            "nickname": self.names.get(uid, str(uid)),
            "card": self.names.get(uid, ""),
            "role": "member",
        }
        if gid:
            ev = {
                "post_type": "message",
                "message_type": "group",
                "sub_type": "normal",
                "message_id": self.mid,
                "group_id": gid,
                "user_id": uid,
                "self_id": SELF,
                "time": int(time.time()),
                "message": segs,
                "raw_message": text,
                "font": 0,
                "sender": sender,
            }
        else:
            ev = {
                "post_type": "message",
                "message_type": "private",
                "sub_type": "friend",
                "message_id": self.mid,
                "user_id": uid,
                "self_id": SELF,
                "time": int(time.time()),
                "message": segs,
                "raw_message": text,
                "font": 0,
                "sender": sender,
            }
        await self.ws.send(json.dumps(ev, ensure_ascii=False))
        return self.mid

    async def wait_reply(self, to, since, timeout=12.0):
        end = time.time() + timeout
        while time.time() < end:
            got = [s for s in self.sent if s[0] == to and s[2] >= since]
            if got:
                return got
            self.new_sent.clear()
            try:
                await asyncio.wait_for(
                    self.new_sent.wait(), max(0.05, end - time.time())
                )
            except asyncio.TimeoutError:
                pass
        return []


def setup_root():
    env = dict(os.environ, ASTRBOT_ROOT=str(ROOT))
    subprocess.run(
        [ASTRBOT, "init", "-y"], cwd=ROOT, env=env, check=True, capture_output=True
    )
    shutil.copytree(
        PLUGIN_SRC,
        ROOT / "data" / "plugins" / PNAME,
        ignore=shutil.ignore_patterns("__pycache__", "dev", "*.pyc", ".git", "tests"),
    )
    install_helper()
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
    cfg["admins_id"] = [str(ADMIN)]
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
        {
            "id": "fake",
            **base,
            "model": "fake-model",
            "modalities": ["text", "tool_use"],
        }
    ]
    cfg.setdefault("provider_settings", {})["default_provider_id"] = "fake"
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def set_plugin(group="budget", **kw):
    p = ROOT / "data" / "config" / f"{PNAME}_config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    cfg = json.loads(p.read_text(encoding="utf-8-sig")) if p.exists() else {}
    cfg.setdefault(group, {}).update(kw)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def log_text():
    return (ROOT / "astrbot_stdout.log").read_text(encoding="utf-8", errors="replace")


def usd(miss, hit, out):
    return (miss * PRICE["miss"] + hit * PRICE["hit"] + out * PRICE["out"]) / 1e6


async def main():
    setup_root()
    # 曲线调得很陡：花一点点钱意愿就到 0；私聊、接话的次方 0（永远回）；找它 / 插话 / 冒泡的次方 1000（意愿不到 100% 就几乎回不了）
    set_plugin(
        half_usd=0.000001,
        shape=1.0,
        floor=0.0,
        power_private=0.0,
        power_follow=0.0,
        power_direct=1000.0,
        power_chat=1000.0,
        power_extra=1000.0,
        peak_enable=False,
    )
    # 活人感里的别的模块这套测试不测（原来这个插件是单独装的）：关掉，才跟原来的条件一样（群聊节奏会自己叫模型判断、攒一攒会等）
    for g in ("gather", "rhythm", "guard"):
        set_plugin(g, enable=False)
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
            "http://127.0.0.1:9"  # （v0.4.0 补上）AstrBot 启动时自己想连外网：让它连不出去（测试不连外网）
        )
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    env["ASTRBOT_DISABLE_METRICS"] = "1"
    log = open(ROOT / "astrbot_stdout.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [ASTRBOT, "run"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT
    )
    token = jwt.encode(
        {"username": "astrbot", "exp": int(time.time()) + 3600},
        SECRET,
        algorithm="HS256",
    )
    hdr = {"Authorization": f"Bearer {token}"}
    api = f"http://127.0.0.1:{DASH_PORT}/api/v1/plugins/extensions/{PNAME}"

    async def state(s):
        for _ in range(60):
            try:
                async with s.get(f"{api}/state", headers=hdr) as r:
                    if r.status == 200:
                        return (
                            ((await r.json(content_type=None)) or {}).get("data") or {}
                        ).get("budget") or {}
            except Exception:
                pass
            await asyncio.sleep(0.5)
        return {}

    async def turn(gid, uid, text, at_bot=False, wait=True):
        """说一句 → (回了没有, 这一轮模型被调了几次, 回的话)
        wait=False（应该被拦的）：等日志里出现「这次不回」再多等 2 秒，确认没调模型、没回话（机器忙的时候也不会误判）"""
        nc, t0 = len(chat_calls), time.time()
        await qq.say(gid, uid, text, at_bot=at_bot)
        to = ("g", gid) if gid else ("p", uid)
        if wait:
            got = await qq.wait_reply(to, t0, timeout=20)
            if got:
                await asyncio.sleep(0.3)
            return bool(got), len(chat_calls) - nc, (got[-1][1] if got else "")
        mark = f"「{' '.join(text.split())[:20]}」→ 这次不回"
        for _ in range(60):
            if mark in log_text():
                break
            await asyncio.sleep(0.5)
        else:
            return True, len(chat_calls) - nc, "（日志里没有「这次不回」）"
        got = await qq.wait_reply(to, t0, timeout=2)
        return bool(got), len(chat_calls) - nc, (got[-1][1] if got else "")

    try:
        qq = FakeQQ()
        await qq.connect()
        s = aiohttp.ClientSession()
        st = await state(s)
        text = log_text()
        check(
            PNAME in text and "Traceback" not in text and st.get("will") == 1.0,
            "AstrBot 起来了、插件加载了、没有报错；还没花钱 意愿 100%",
            text[-1500:],
        )

        # ═════════ A 记账 ═════════
        print("\n[A 记账]")
        ok, n, said = await turn(G1, HUMAN, "你好呀", at_bot=True)
        check(ok and n == 1, "还没花钱：@ 它照常回", (ok, n, said))
        st = await state(s)
        p = st.get("parts") or {}
        check(
            (p.get("miss") or {}).get("tok") == 200
            and (p.get("hit") or {}).get("tok") == 800
            and (p.get("out") or {}).get("tok") == 50
            and p.get("calls") == 1,
            "DeepSeek 的用量记对了：没命中 200、命中 800、输出 50（只有 prompt_cache_* 字段也认得）",
            p,
        )
        check(
            abs(st.get("spent", 0) - usd(200, 800, 50)) < 1e-12,
            f"折成美元对了：${usd(200, 800, 50):.7f}",
            st.get("spent"),
        )
        check(
            st.get("will", 1) < 0.05 and (st.get("probs") or {}).get("private") == 1.0,
            "花了钱：意愿掉下去了，私聊还是 100%",
            {k: st.get(k) for k in ("will", "probs")},
        )

        # ═════════ B 拦 ═════════
        print("\n[B 拦]")
        ok, n, said = await turn(G1, HUMAN, "在吗在吗", at_bot=True, wait=False)
        check(not ok and n == 0, "再 @：这次不调模型、不回话", (ok, n, said))
        text = log_text()
        check(
            "[省token] 群 7001 老王「在吗在吗」→ 这次不回" in text and "掷出" in text,
            "日志里写了为什么没回",
            [x for x in text.splitlines() if "省token" in x][-3:],
        )
        check(
            "Traceback" not in text
            and "Error occurred while processing agent" not in text,
            "没报错（AstrBot 没往外发错误）",
        )
        st = await state(s)
        check(
            (st.get("today") or {}).get("skip", {}).get("direct") == 1
            and any(x.get("who") == "老王" for x in st.get("skips") or []),
            "面板：今天跳过 1 次（找它）、记着老王被跳过",
            (st.get("today"), st.get("skips")),
        )
        ok, n, said = await turn(G1, HUMAN, "思考测试 有人吗", at_bot=True, wait=False)
        check(
            not ok and n == 0,
            "别的插件在同一个钩子里发「正在思考中」（默认优先级 0）：被拦的这条它也没发出去（省 token 排在它前面）",
            (ok, n, said),
        )

        # ═════════ C 私聊 / 管理员 ═════════
        print("\n[C 私聊照常回、管理员不限]")
        ok, n, _ = await turn(0, HUMAN, "私聊在吗")
        check(ok and n == 1, "私聊：照常回（次方 0）", (ok, n))
        ok, n, _ = await turn(G1, ADMIN, "管理员来了", at_bot=True)
        check(ok and n == 1, "管理员 @：不限，照常回", (ok, n))

        # ═════════ D 场合从 extra 读 ═════════
        print("\n[D 场合从别的插件写的 fatfish_kind 读]")
        ok, n, _ = await turn(G1, HUMAN2, "接话:那后来呢")
        check(
            ok and n == 1 and "接话:那后来呢" in chat_calls[-1]["user"],
            "接话（follow，次方 0）：没 @ 也回了",
            (ok, n),
        )
        ok, n, _ = await turn(G1, HUMAN2, "插话:今天好热", wait=False)
        check(not ok and n == 0, "插话（chat）：拦住了", (ok, n))
        ok, n, _ = await turn(G1, HUMAN2, "冒泡:来聊天", wait=False)
        check(
            not ok and n == 0,
            "冒泡（extra，插件 yield request_llm 发起的）：也拦得住",
            (ok, n),
        )
        st = await state(s)
        t = st.get("today") or {}
        check(
            t.get("pass", {}).get("follow") == 1
            and t.get("skip", {}).get("chat") == 1
            and t.get("skip", {}).get("extra") == 1
            and t.get("pass", {}).get("private") == 1
            and t.get("admin") == 1,
            "面板：今天回了 / 跳过 分场合记着",
            t,
        )

        # ═════════ E 指令 ═════════
        print("\n[E 管理员指令]")
        ok, n, said = await turn(G1, HUMAN, "/省token 火力全开")
        check(
            n == 0 and (not ok or "权限" in said),
            "非管理员：用不了（AstrBot 回没权限）",
            said,
        )
        ok, n, said = await turn(G1, HUMAN, "/省token")
        check(
            n == 0 and (not ok or "权限" in said),
            "非管理员光发 /省token：也用不了",
            said,
        )
        for sub in ("状态", "省着点", "重新算"):
            ok, n, said = await turn(G1, HUMAN, f"/省token {sub}")
            check(
                n == 0 and (not ok or "权限" in said),
                f"非管理员 /省token {sub}：也用不了",
                said,
            )
        st = await state(s)
        check(
            not (st.get("full") or {}).get("on") and st.get("reset") is None,
            "火力全开没被打开、也没被「重新算」",
            {k: st.get(k) for k in ("full", "reset")},
        )
        ok, n, said = await turn(G1, ADMIN, "/省token")
        check(
            ok
            and n == 0
            and "回复意愿" in said
            and "火力全开：没开" in said
            and "跳过" in said
            and "参数不足" not in said,
            "管理员光发 /省token：回状态（不是「参数不足」）",
            said,
        )
        ok, n, said = await turn(G1, ADMIN, "/省token 状态")
        check(ok and "回复意愿" in said, "/省token 状态：一样", said)
        ok, n, said = await turn(G1, ADMIN, "/省token 火力全开 2")
        check(
            ok
            and n == 0
            and "火力全开！" in said
            and ("还剩 2 小时" in said or "还剩 1 小时 59" in said),
            "/省token 火力全开 2：开了",
            said,
        )
        ok, n, _ = await turn(G1, HUMAN, "现在呢", at_bot=True)
        check(ok and n == 1, "火力全开：@ 照常回", (ok, n))
        ok, n, _ = await turn(G1, HUMAN2, "插话:火力全开了吗")
        check(ok and n == 1, "火力全开：插话也不掷了", (ok, n))
        t0, nc, got = time.time(), len(chat_calls), []
        await qq.say(G1, HUMAN, "思考测试 火力全开", at_bot=True)
        for _ in range(100):
            got = [x[1] for x in qq.sent if x[0] == ("g", G1) and x[2] >= t0]
            if len(got) >= 2 and len(chat_calls) > nc:
                break
            await asyncio.sleep(0.2)
        await asyncio.sleep(0.3)
        check(
            len(got) == 2 and "正在思考" in got[0] and len(chat_calls) - nc == 1,
            "没拦的：别的插件的「正在思考中」照发、模型照常回（上面那条被拦的不是因为它没生效）",
            (got, len(chat_calls) - nc),
        )
        ok, n, said = await turn(G1, ADMIN, "/省token 省着点")
        check(ok and "火力全开关了" in said, "/省token 省着点：关了", said)
        ok, n, _ = await turn(G1, HUMAN, "又不回了？", at_bot=True, wait=False)
        check(not ok and n == 0, "关了以后又拦了", (ok, n))
        ok, n, said = await turn(G1, ADMIN, "/省token 火力全开 一直")
        st = await state(s)
        check(
            ok and "一直开着" in said and (st.get("full") or {}).get("left") == -1,
            "/省token 火力全开 一直：一直开（面板上剩余 -1）",
            (said, st.get("full")),
        )
        ok, n, said = await turn(G1, ADMIN, "/省token 火力全开 abc")
        check(ok and "用法" in said, "小时数写错：回用法", said)
        await turn(G1, ADMIN, "/省token 省着点")
        ok, n, said = await turn(G1, ADMIN, "/省token 重新算")
        check(
            ok and "重新算" in said and "100%" in said,
            "/省token 重新算：意愿回到 100%",
            said,
        )
        ok, n, _ = await turn(G1, HUMAN, "重新算了吧", at_bot=True)
        check(ok and n == 1, "重新算以后：@ 又回了", (ok, n))
        ok, n, _ = await turn(G1, HUMAN, "那现在呢", at_bot=True, wait=False)
        check(not ok and n == 0, "回了一次又花了钱：又拦了", (ok, n))
        st = await state(s)
        check(
            st.get("reset") and st.get("spent_all", 0) > st.get("spent", 0) > 0,
            "面板：重新算的时间点、窗口里一共花的照样看得到",
            {k: st.get(k) for k in ("reset", "spent", "spent_all")},
        )

        # ═════════ F 记账：OpenAI 格式、调工具 ═════════
        print("\n[F 记账：OpenAI 格式、一轮里调了工具]")
        before = (await state(s)).get("parts") or {}
        ok, n, _ = await turn(0, HUMAN, "OPENAI格式 测试")
        after = (await state(s)).get("parts") or {}
        d = {
            k: (after.get(k) or {}).get("tok", 0) - (before.get(k) or {}).get("tok", 0)
            for k in ("miss", "hit", "out")
        }
        check(
            ok and n == 1 and d == {"miss": 500, "hit": 1500, "out": 100},
            "OpenAI 格式（prompt_tokens_details.cached_tokens）：没命中 500、命中 1500",
            d,
        )
        before = after
        ok, n, said = await turn(0, HUMAN, "工具测试 一下")
        after = (await state(s)).get("parts") or {}
        d = {
            k: (after.get(k) or {}).get("tok", 0) - (before.get(k) or {}).get("tok", 0)
            for k in ("miss", "hit", "out")
        }
        tool_used = len(chat_calls) >= 2 and any(
            m.get("role") == "tool" for m in chat_calls[-1]["messages"]
        )
        check(
            ok and n == 2 and tool_used,
            "调工具：模型被调了两次（中间跑了工具）",
            (ok, n, said),
        )
        check(
            d == {"miss": 200, "hit": 1200, "out": 50}
            and after.get("calls", 0) - before.get("calls", 0) == 1,
            "两次调用都记上了（前一次从 AstrBot 的累计里补）：没命中 100+100、命中 300+900、输出 20+30",
            d,
        )

        before = after
        ok, n, said = await turn(0, HUMAN, "/e2e记账")
        after = (await state(s)).get("parts") or {}
        d = {
            k: (after.get(k) or {}).get("tok", 0) - (before.get(k) or {}).get("tok", 0)
            for k in ("miss", "hit", "out")
        }
        check(
            ok
            and n == 0
            and f"记了 {usd(400, 600, 10):.9f}" in said
            and d == {"miss": 400, "hit": 600, "out": 10},
            "别的插件调 record_usage：自己调模型的花费算进来了",
            (said, d),
        )
        check(
            "问了 False" in said
            and "冒泡/主动" in said
            and "冒泡/主动 这次算了" in log_text()
            and "火力全开 False" in said,
            '别的插件调 ask("extra")：花了钱以后冒泡被拦（日志里写了）',
            said,
        )
        ok, n, _ = await turn(0, HUMAN, "无用量 测试")
        await asyncio.sleep(0.5)
        check(
            ok and n == 1 and "没给 token 用量" in log_text(),
            "模型没回 usage：照常回，日志里提醒记不了账（不会悄悄当成 $0）",
            [x for x in log_text().splitlines() if "省token" in x][-3:],
        )

        # ═════════ G 面板接口 ═════════
        print("\n[G 面板接口]")
        st = await state(s)
        check(
            all(
                k in st
                for k in (
                    "will",
                    "probs",
                    "today",
                    "full",
                    "parts",
                    "half_usd",
                    "shape",
                    "floor",
                    "hourly",
                    "recent",
                    "peak",
                )
            ),
            "状态：意愿、各场合概率、今天的数、火力全开、花费、曲线参数、每小时、最近的决定",
            list(st)[:30],
        )
        check(
            len(st.get("hourly") or []) == 24
            and abs(sum(st["hourly"]) - st.get("spent_all", 0)) < 1e-7,
            "每小时的花费加起来 = 窗口里一共花的",
            (sum(st.get("hourly") or []), st.get("spent_all")),
        )
        async with s.post(
            f"{api}/action", headers=hdr, json={"m": "budget", "do": "full", "hours": 1}
        ) as r:
            d = ((await r.json(content_type=None)) or {}).get("data") or {}
        check(
            r.status == 200
            and (d.get("full") or {}).get("on")
            and 3500 < (d.get("full") or {}).get("left", 0) <= 3600,
            "面板：开火力全开 1 小时",
            d.get("full"),
        )
        async with s.post(
            f"{api}/action", headers=hdr, json={"m": "budget", "do": "full", "hours": 0}
        ) as r:
            d = ((await r.json(content_type=None)) or {}).get("data") or {}
        check(not (d.get("full") or {}).get("on"), "面板：关掉", d.get("full"))
        async with s.post(
            f"{api}/action",
            headers=hdr,
            json={"m": "budget", "do": "full", "hours": 0.003},
        ) as r:  # 10.8 秒
            d = ((await r.json(content_type=None)) or {}).get("data") or {}
        f = d.get("full") or {}
        check(
            f.get("on") and 0 < f.get("left", 0) <= 11,
            "面板：火力全开 0.003 小时（10.8 秒）",
            f,
        )
        ok, n, _ = await turn(G1, HUMAN, "短短的火力全开", at_bot=True)
        check(ok and n == 1, "开着：@ 照常回", (ok, n))
        await asyncio.sleep(max(0.0, float(f.get("until") or 0) - time.time()) + 1.0)
        ok, n, _ = await turn(G1, HUMAN, "到点了吗", at_bot=True, wait=False)
        st = await state(s)
        check(
            not ok and n == 0 and not (st.get("full") or {}).get("on"),
            "到点自动关（不用手动关）：又拦了",
            (ok, n, st.get("full")),
        )
        async with s.post(
            f"{api}/action", headers=hdr, json={"m": "budget", "do": "reset_clear"}
        ) as r:
            d = ((await r.json(content_type=None)) or {}).get("data") or {}
        check(
            d.get("reset") is None
            and abs(d.get("spent", 0) - d.get("spent_all", 1)) < 1e-12,
            "面板：恢复按实际花费算",
            d.get("reset"),
        )
        async with s.post(
            f"{api}/action", headers=hdr, json={"m": "budget", "do": "reset"}
        ) as r:
            d = ((await r.json(content_type=None)) or {}).get("data") or {}
        check(
            d.get("reset") and d.get("will") == 1.0,
            "面板：从现在重新算",
            d.get("reset"),
        )
        async with s.post(
            f"{api}/action", headers=hdr, json={"m": "budget", "do": "乱来"}
        ) as r:
            d = await r.json(content_type=None)
        check((d or {}).get("status") == "error", "面板：不认识的操作 → 报错", d)
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
                r2.status == 200 and "省 token" in html and "bridge-sdk.js" in html,
                "面板页 HTML 拿得到、AstrBot 插上了桥接脚本",
                (r2.status, html[:200]),
            )
        async with s.post(
            f"{api}/action",
            headers=hdr,
            json={"m": "budget", "do": "full", "hours": -1},
        ) as r:
            d = ((await r.json(content_type=None)) or {}).get("data") or {}
        check(
            (d.get("full") or {}).get("left") == -1,
            "面板：火力全开「一直」",
            d.get("full"),
        )
        # WebUI 改配置：AstrBot 热重载插件（老的调 terminate、新的从 state.json 读回来）
        snap0 = await state(s)
        cfg_now = json.loads(
            (ROOT / "data" / "config" / f"{PNAME}_config.json").read_text(
                encoding="utf-8-sig"
            )
        )
        cfg_now.setdefault("budget", {})["full_default_hours"] = 4
        async with s.put(
            f"http://127.0.0.1:{DASH_PORT}/api/v1/plugins/{PNAME}/config",
            headers=hdr,
            json={"config": cfg_now},
        ) as r:
            d = await r.json(content_type=None)
        st = {}
        for _ in range(40):
            st = await state(s)
            if st.get("full_default_hours") == 4:
                break
            await asyncio.sleep(0.5)
        check(
            r.status == 200
            and (d or {}).get("status") == "ok"
            and st.get("full_default_hours") == 4
            and abs(st.get("spent_all", 0) - snap0.get("spent_all", -1)) < 1e-12
            and st.get("today") == snap0.get("today")
            and (st.get("full") or {}).get("left") == -1
            and st.get("reset"),
            "WebUI 改配置（插件热重载）：新配置生效；花费、今天的数、火力全开、重新算都还在",
            (
                d,
                {
                    k: (st.get(k), snap0.get(k))
                    for k in ("full_default_hours", "spent_all", "full", "reset")
                },
            ),
        )
        ok, n, _ = await turn(G2, HUMAN, "重载以后呢", at_bot=True)
        check(
            ok and n == 1, "热重载以后照常工作（火力全开一直开着：@ 照常回）", (ok, n)
        )
        text = log_text()
        check(
            "Traceback" not in text and "参数不足" not in text,
            "第一轮 AstrBot 日志里没有 Traceback、没有「参数不足」",
            text[text.find("Traceback") - 300 :][:1500] if "Traceback" in text else "",
        )
        before = await state(s)
        await s.close()

        # ═════════ 第二轮：重启（AstrBot 被结束时不调 terminate） ═════════
        print("\n[第二轮 重启：存档还在]")
        await asyncio.sleep(6)  # 掷骰子的结果最多 5 秒内存盘（面板操作是马上存）
        proc.terminate()
        proc.wait(30)
        await qq.ws.close()
        whole = json.loads(
            (ROOT / "data" / "plugin_data" / PNAME / "state.json").read_text(
                encoding="utf-8"
            )
        )
        saved = whole["budget"]
        check(
            saved.get("ledger")
            and (saved.get("full") or {}).get("until") == -1
            and saved.get("day", {}).get("skip"),
            "关掉的时候存档里有：流水、火力全开、今天的数",
            {k: saved.get(k) for k in ("full", "day")},
        )
        # 手改坏一条跳过记录（数字写成了字）：读回来不能把掷骰子 / 面板弄坏
        saved.setdefault("skips", {})["坏的|1"] = {
            "ts": ["坏"],
            "n": "坏",
            "last": "坏",
            "name": "坏的",
        }
        (ROOT / "data" / "plugin_data" / PNAME / "state.json").write_text(
            json.dumps(whole, ensure_ascii=False), encoding="utf-8"
        )
        proc = subprocess.Popen(
            [ASTRBOT, "run"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT
        )
        qq = FakeQQ()
        await qq.connect()
        s = aiohttp.ClientSession()
        st = await state(s)
        check(
            abs(st.get("spent_all", 0) - before.get("spent_all", -1)) < 1e-12
            and (st.get("full") or {}).get("left") == -1
            and st.get("today") == before.get("today")
            and st.get("reset"),
            "重启以后：花费、火力全开（一直）、重新算、今天的数都在",
            {k: (st.get(k), before.get(k)) for k in ("spent_all", "full", "today")},
        )
        ok, n, _ = await turn(G2, HUMAN2, "重启了吗", at_bot=True)
        check(ok and n == 1, "火力全开（一直）：重启以后 @ 照常回", (ok, n))
        await s.close()
        text = log_text()
        check(
            "掷骰子出错了" not in text
            and "记账出错了" not in text
            and "坏的" not in json.dumps(st, ensure_ascii=False),
            "存档里有手改坏的数：读进来的时候扔掉了，掷骰子、面板都没出错",
            [x for x in text.splitlines() if "省token" in x][-3:],
        )
        check(
            "Traceback" not in text,
            "整个过程 AstrBot 日志里没有 Traceback",
            text[text.find("Traceback") - 300 :][:1500] if "Traceback" in text else "",
        )
    finally:
        proc.terminate()
        try:
            proc.wait(15)
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
