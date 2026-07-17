#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Antigravity「跑完了 / 需要你」-> 手表震动 守护进程。

背景:为什么不像 Claude/Codex 那样做？
  Claude Code / Codex 是 CLI,有「执行前同步调外部脚本、并听返回值」的 hook,
  watch_approve.py 就插在那个口子上(手表上点批准)。Antigravity 是 GUI IDE,
  agent 是闭源 language_server + Google 后端,**没有这种 hook 口子**——既收不到
  审批事件、也无法把决定送回去。而且你这台是全自动模式(啥都不问直接跑),
  所以根本没有「批准」这件事,只剩「跑完了」值得提醒。

怎么做到的(关键发现,2026-06-29):
  Antigravity 每次 agent 回合结束都会**发一条 macOS 系统通知**
  (bundle: com.google.antigravity / -ide,标题 "Antigravity",正文 = 会话名)。
  这条通知落在 macOS 通知库(SQLite),确定、可靠、自带会话名。
  我们就监听这个库:出现新的 Antigravity 通知 -> 转推到手表
  (复用 watch.env 同一套 Pushcut/ntfy 通道)。
  —— 比起读 Electron 的辅助功能(AX)树(实测非确定、闪烁、残留,会漏报误报),
     监听通知库稳得多,也不需要辅助功能权限、不抢前台。

权限:读通知库需要「完全磁盘访问」(Full Disk Access);宿主进程(本机=Claude.app)
  已有 FDA。若改成 launchd 独立常驻,需给跑它的解释器/包装单独授 FDA(见 README/接线说明)。

设计原则(与 watch_approve.py / watch_done.py 一致):
  * 只用 Python 3 标准库;出网显式走 HTTPS_PROXY;配置全部来自环境变量(watch.env 兜底)。
  * fire-and-forget:任何异常/超时一律吞掉继续轮询,绝不崩。
  * 冷启动不补发历史:首次运行把基线设为「当前最新一条」,只推之后新出现的。
"""

import json
import os
import plistlib
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


# ---------- 兜底配置文件 watch.env(与其它 watch 脚本共用同一份) ----------
def _load_env_file():
    path = os.environ.get("WATCH_ENV_FILE", "").strip() or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "watch.env"
    )
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


_load_env_file()


# ---------- 通知配置:复用 watch.env 里那套 Pushcut/ntfy ----------
PUSHCUT_KEY = os.environ.get("PUSHCUT_KEY", "").strip()
PUSHCUT_NOTIF = os.environ.get("PUSHCUT_NOTIF", "claude").strip() or "claude"
TRANSPORT = os.environ.get("WATCH_TRANSPORT", "pushcut").strip().lower()
if TRANSPORT not in ("pushcut", "ntfy"):
    TRANSPORT = "pushcut"
NTFY_BASE = (os.environ.get("NTFY_BASE", "").strip() or "https://ntfy.sh/").rstrip("/") + "/"
NTFY_NOTIFY_TOPIC = os.environ.get("NTFY_NOTIFY_TOPIC", "").strip()
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "").strip()
PROXY = (
    os.environ.get("HTTPS_PROXY")
    or os.environ.get("https_proxy")
    or os.environ.get("HTTP_PROXY")
    or os.environ.get("http_proxy")
    or ""
).strip()
PUSHCUT_DEVICES = [
    d.strip() for d in os.environ.get("PUSHCUT_DEVICES", "").split(",") if d.strip()
]
DONE_SOUND = os.environ.get(
    "WATCH_AG_SOUND", os.environ.get("WATCH_DONE_SOUND", os.environ.get("PUSHCUT_SOUND", "jobDone"))
).strip()
TIME_SENSITIVE = os.environ.get("PUSHCUT_TIME_SENSITIVE", "1").strip() != "0"

# Antigravity 专属展示。配图(logo)先留 none,确定 logo 后填公开图 URL(或 Pushcut 内置图片名)。
DONE_TITLE = os.environ.get("WATCH_AG_TITLE", "").strip() or "🪐 Antigravity 跑完了"
PUSHCUT_IMAGE = os.environ.get(
    "WATCH_AG_IMAGE", os.environ.get("PUSHCUT_IMAGE", "none")
).strip()

try:
    PUSHCUT_RETRIES = max(1, int(os.environ.get("PUSHCUT_RETRIES", "8")))
except ValueError:
    PUSHCUT_RETRIES = 8
try:
    PUSHCUT_TIMEOUT = max(3, int(os.environ.get("PUSHCUT_TIMEOUT", "6")))
except ValueError:
    PUSHCUT_TIMEOUT = 6

PUSHCUT_URL = "https://api.pushcut.io/v1/notifications/" + urllib.parse.quote(
    PUSHCUT_NOTIF, safe=""
)


# ---------- 轮询参数 ----------
def _f(name, default):
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return float(default)


POLL = _f("WATCH_AG_POLL", 3.0)
ENABLE = os.environ.get("WATCH_AG_ENABLE", "1").strip() != "0"
DEBUG = os.environ.get("WATCH_AG_DEBUG", "0").strip() != "0"

# 监听哪些 bundle 的通知(Antigravity 的 Agent Manager + IDE)。
WATCH_BUNDLES = [
    b.strip()
    for b in os.environ.get(
        "WATCH_AG_BUNDLES", "com.google.antigravity,com.google.antigravity-ide"
    ).split(",")
    if b.strip()
]

# macOS 通知库路径(Mac 绝对时间:自 2001-01-01 起的秒)。
NOTIF_DB = os.environ.get("WATCH_AG_NOTIF_DB", "").strip() or os.path.expanduser(
    "~/Library/Group Containers/group.com.apple.usernoted/db2/db"
)
# 记住「已推到哪条」的游标文件,跨重启不重复推。
CURSOR_FILE = os.environ.get("WATCH_AG_CURSOR", "").strip() or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".watch_antigravity_cursor"
)
MAC_EPOCH_OFFSET = 978307200.0  # 2001-01-01 - 1970-01-01,秒

# ---------- Focus 状态镜像:替没有完全磁盘访问的 hook 宿主看专注 ----------
# watch_approve.py 的自动模式要读 ~/Library/DoNotDisturb/DB(需要 FDA)。
# Claude.app 有 FDA 读得到;ChatGPT.app(com.openai.codex)没有 -> Codex 的
# PermissionRequest hook 读不到 -> Auto-CLI 开着也照样推手表(2026-07-10 的臭毛病)。
# 本守护的宿主 WatchAntigravity.app 有 FDA,就顺手每轮把「触发专注是否活动」写成
# 普通文件 on/off;watch_approve.py 的 AUTO_FOCUS_FLAG 指到这里即可,
# 不用给 ChatGPT.app 开 FDA。内容没变时只 bump mtime,配合 AUTO_FOCUS_MAX_AGE_MIN:
# 守护挂了旗标很快过期 -> fail-safe 退回正常上手表审批,绝不误放行。
FOCUS_MIRROR_FILE = os.environ.get("FOCUS_MIRROR_FILE", "").strip() or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".focus-mirror.flag"
)
# 触发集与 watch_approve.py 同源(同一份 watch.env 的 AUTO_FOCUS_NAMES),默认值也保持一致。
_FOCUS_TRIGGERS_DEFAULT = (
    "Auto-CLI,"
    "com.apple.donotdisturb.mode.heartfill,"   # Auto-CLI
    "com.apple.focus.gaming,"                    # 游戏 Gaming
    "com.apple.sleep.sleep-mode,"               # 睡眠 Sleep
    "com.apple.donotdisturb.mode.default"        # 勿扰 Do Not Disturb
)
FOCUS_TRIGGERS = {
    t.strip()
    for t in (
        os.environ.get("AUTO_FOCUS_NAMES", _FOCUS_TRIGGERS_DEFAULT)
        + "," + os.environ.get("AUTO_FOCUS_NAME", "")
    ).split(",")
    if t.strip()
}
_DND_DB = os.path.expanduser("~/Library/DoNotDisturb/DB")


def _log(*a):
    if DEBUG:
        print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------- 读通知库:返回 (delivered_date, body) 列表,按时间升序,只取 > since ----------
def read_new_notifications(since):
    rows = []
    try:
        # 必须用 mode=ro(可读 WAL),不能加 immutable=1——通知库是 WAL 模式,
        # immutable 会无视 -wal 文件,只读到已 checkpoint 的旧数据(漏掉最新通知)。
        uri = "file:%s?mode=ro" % urllib.parse.quote(NOTIF_DB)
        con = sqlite3.connect(uri, uri=True, timeout=2)
    except Exception as e:
        _log("db open err", e)
        return rows
    try:
        cur = con.cursor()
        qmarks = ",".join("?" * len(WATCH_BUNDLES))
        cur.execute(
            "SELECT app_id FROM app WHERE identifier IN (%s)" % qmarks, WATCH_BUNDLES
        )
        app_ids = [r[0] for r in cur.fetchall()]
        if not app_ids:
            return rows
        idmarks = ",".join("?" * len(app_ids))
        cur.execute(
            "SELECT delivered_date, data FROM record "
            "WHERE app_id IN (%s) AND delivered_date > ? "
            "ORDER BY delivered_date ASC" % idmarks,
            app_ids + [since],
        )
        for dd, blob in cur.fetchall():
            body = ""
            try:
                pl = plistlib.loads(blob)
                req = pl.get("req", {}) if isinstance(pl, dict) else {}
                body = (req.get("body") or "").strip()
            except Exception:
                pass
            rows.append((dd, body))
    except Exception as e:
        _log("db query err", e)
    finally:
        try:
            con.close()
        except Exception:
            pass
    return rows


def latest_notification_date():
    """取当前最新一条 Antigravity 通知的时间(冷启动基线用);没有则返回 0。"""
    rows = read_new_notifications(0.0)
    return rows[-1][0] if rows else 0.0


def load_cursor():
    try:
        with open(CURSOR_FILE, "r") as f:
            return float(f.read().strip())
    except Exception:
        return None


def save_cursor(val):
    try:
        with open(CURSOR_FILE, "w") as f:
            f.write("%r" % val)
    except Exception:
        pass


# ---------- Focus 镜像的判定与落盘 ----------
def focus_active():
    """FOCUS_TRIGGERS 里任一专注(按 modeIdentifier 或显示名)当前活动 -> True。
    读不到/没开/异常 -> False。判定逻辑与 watch_approve.py 的 mac_focus_active() 保持一致。"""
    if not FOCUS_TRIGGERS:
        return False
    try:
        with open(os.path.join(_DND_DB, "Assertions.json")) as f:
            active = json.load(f)["data"][0].get("storeAssertionRecords", [])
        active_ids = {
            (r.get("assertionDetails") or {}).get("assertionDetailsModeIdentifier")
            for r in active
        }
        active_ids.discard(None)
        if not active_ids:
            return False
        if active_ids & FOCUS_TRIGGERS:
            return True
        with open(os.path.join(_DND_DB, "ModeConfigurations.json")) as f:
            modes = json.load(f)["data"][0]["modeConfigurations"]
        for key, cfg in modes.items():
            m = cfg.get("mode") or {}
            mid = m.get("modeIdentifier") or key
            if mid in active_ids and m.get("name") in FOCUS_TRIGGERS:
                return True
    except Exception as e:
        _log("focus read err", e)
        return False
    return False


def mirror_focus():
    """把专注状态写到 FOCUS_MIRROR_FILE(on/off)。内容没变只 bump mtime(供过期检查)。"""
    val = "on" if focus_active() else "off"
    try:
        try:
            with open(FOCUS_MIRROR_FILE, "r") as f:
                old = f.read(8).strip()
        except Exception:
            old = None
        if old != val:
            with open(FOCUS_MIRROR_FILE, "w") as f:
                f.write(val + "\n")
            _log("focus mirror ->", val)
        else:
            os.utime(FOCUS_MIRROR_FILE, None)
    except Exception as e:
        _log("focus mirror err", e)


# ---------- 推送(复用 watch_done.py 的纯提醒载荷,无按钮) ----------
def make_opener():
    if PROXY:
        proxy_handler = urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
    else:
        proxy_handler = urllib.request.ProxyHandler({})
    return urllib.request.build_opener(proxy_handler)


def send_notification(opener, title, text, sound=None):
    snd = (sound if sound is not None else DONE_SOUND).strip()
    if TRANSPORT == "ntfy":
        payload = {
            "topic": NTFY_NOTIFY_TOPIC,
            "title": title,
            "message": text or "(无详情)",
            "priority": 4,
        }
        if PUSHCUT_IMAGE and PUSHCUT_IMAGE.lower() != "none":
            payload["attach"] = PUSHCUT_IMAGE
        headers = {"Content-Type": "application/json"}
        if NTFY_TOKEN:
            headers["Authorization"] = "Bearer " + NTFY_TOKEN
        url = NTFY_BASE
    else:
        payload = {"title": title}
        if text:
            payload["text"] = text
        if PUSHCUT_DEVICES:
            payload["devices"] = PUSHCUT_DEVICES
        if snd and snd.lower() != "none":
            payload["sound"] = snd
        if PUSHCUT_IMAGE and PUSHCUT_IMAGE.lower() != "none":
            payload["image"] = PUSHCUT_IMAGE
        if TIME_SENSITIVE:
            payload["isTimeSensitive"] = True
        headers = {"API-Key": PUSHCUT_KEY, "Content-Type": "application/json"}
        url = PUSHCUT_URL
    body = json.dumps(payload).encode("utf-8")
    for attempt in range(PUSHCUT_RETRIES):
        try:
            req = urllib.request.Request(url, data=body, method="POST", headers=headers)
            with opener.open(req, timeout=PUSHCUT_TIMEOUT) as resp:
                resp.read()
            return True
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500 and e.code != 429:
                return False
        except Exception:
            pass
        if attempt < PUSHCUT_RETRIES - 1:
            time.sleep(0.3)
    return False


def fire(opener, body):
    text = ("「%s」已完成" % body) if body else "Antigravity 当前任务已结束"
    send_notification(opener, DONE_TITLE, text)


# ---------- 主循环 ----------
def main():
    args = sys.argv[1:]
    opener = make_opener()

    if "--selftest" in args:
        ok = send_notification(opener, DONE_TITLE, "✅ 自检:Antigravity 手表提醒已接通")
        print("selftest sent:", ok)
        return
    if "--dump" in args:  # 看最近的 Antigravity 通知
        for dd, body in read_new_notifications(0.0)[-10:]:
            print(round(dd, 1), "|", body)
        return
    if not ENABLE:
        return

    # 冷启动基线:游标文件优先;没有则用「当前最新」,避免补发历史。
    since = load_cursor()
    if since is None:
        since = latest_notification_date()
        save_cursor(since)
    _log("start, since=", since)

    while True:
        mirror_focus()
        try:
            for dd, body in read_new_notifications(since):
                _log("FIRE", round(dd, 1), body)
                fire(opener, body)
                since = dd
                save_cursor(since)
        except Exception as e:
            _log("loop err", e)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
