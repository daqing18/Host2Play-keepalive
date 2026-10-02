"""Host2Play 状态检测：读续期倒计时 + 面板电源状态，关机则自动开机。
跑在 GitHub Actions（WARP 换 IP，躲 Cloudflare 1015）。
面板账号走 secrets H2P_PANEL_UN / H2P_PANEL_PW；未配置则跳过面板检查。
只有异常/开机才发 TG，正常时安静。
"""
import json
import os
import re
import sys
import time
import random
import tempfile
import requests
from datetime import datetime, timezone, timedelta
from xvfbwrapper import Xvfb
from DrissionPage import ChromiumPage, ChromiumOptions

RENEW_URL = "https://host2play.gratis/server/renew?i=792fc2f5-d7f8-4342-9b38-23483e4fdd60"
PANEL_LOGIN_URL = "https://cp.host2play.gratis/auth/login"
SERVER_NAME = "mcf7057"
PANEL_USER = os.getenv("H2P_PANEL_UN", "").strip()
PANEL_PASS = os.getenv("H2P_PANEL_PW", "").strip()
TG_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT = os.getenv("TG_CHAT_ID", "").strip()

CN_TZ = timezone(timedelta(hours=8))

result = {
    "time": datetime.now(CN_TZ).isoformat(),
    "server": SERVER_NAME,
    "countdown": None,
    "remaining_s": None,
    "power": "unknown",
    "booted": False,
    "panel_skipped": False,
    "errors": [],
}


def log(msg, level="INFO"):
    prefix = {"INFO": "[INFO]", "WARN": "[WARN]", "ERROR": "[ERROR]"}.get(level, "[INFO]")
    print(f"{prefix} {msg}", flush=True)


def send_tg(text):
    if not TG_TOKEN or not TG_CHAT:
        log("未配置 TG secrets，跳过通知", "WARN")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": text},
            timeout=20,
        )
        log(f"TG 发送 HTTP {r.status_code}")
    except Exception as e:
        log(f"TG 发送异常: {e}", "ERROR")


def make_page():
    co = ChromiumOptions()
    co.set_browser_path("/usr/bin/google-chrome")
    for arg in ("--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                "--disable-setuid-sandbox", "--disable-software-rasterizer",
                "--disable-extensions", "--no-first-run", "--no-default-browser-check",
                "--window-size=1280,720", "--log-level=3", "--silent"):
        co.set_argument(arg)
    co.set_user_data_path(tempfile.mkdtemp())
    co.auto_port()
    co.headless(False)
    page = ChromiumPage(co)
    try:
        page.add_init_js(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
    except Exception:
        pass
    return page


def parse_hms(text):
    m = re.search(r"(\d+):([0-5]?\d):([0-5]?\d)", text or "")
    if not m:
        return None
    h, mi, s = map(int, m.groups())
    return h * 3600 + mi * 60 + s


def read_countdown(page):
    page.get(RENEW_URL, retry=3)
    time.sleep(random.uniform(4, 6))
    try:
        page.run_js(
            "['ins.adsbygoogle','iframe[src*=\"ads\"]','.modal-backdrop'].forEach("
            "s=>document.querySelectorAll(s).forEach(e=>e.remove()));"
        )
    except Exception:
        pass
    text = None
    try:
        ele = page.ele("#expireDate", timeout=5)
        if ele:
            text = (ele.text or "").strip() or None
    except Exception:
        pass
    if not text:
        for sel in ("text:Expires in:", "text:Deletes on:"):
            try:
                ele = page.ele(sel, timeout=2)
                if ele:
                    t = (ele.text or "").strip()
                    text = t.split(":", 1)[1].strip() if ":" in t else t
                    break
            except Exception:
                pass
    secs = parse_hms(text)
    log(f"续期倒计时文本={text!r} 剩余秒={secs}")
    return text, secs


def panel_login(page):
    page.get(PANEL_LOGIN_URL, retry=2)
    time.sleep(4)
    try:
        pwd_probe = page.ele("tag:input@@type=password", timeout=4)
    except Exception:
        pwd_probe = None
    if not pwd_probe:
        log("未见登录表单，视为已登录态")
        return True
    user = None
    for sel in ("tag:input@@type=text", "tag:input@@type=email",
                "xpath://input[not(@type) or @type='text']"):
        try:
            user = page.ele(sel, timeout=3)
            if user:
                break
        except Exception:
            pass
    pwd = None
    try:
        pwd = page.ele("tag:input@@type=password", timeout=5)
    except Exception:
        pass
    if not user or not pwd:
        log("找不到登录输入框", "ERROR")
        return False
    try:
        user.clear()
        user.input(PANEL_USER)
        pwd.clear()
        pwd.input(PANEL_PASS)
    except Exception as e:
        log(f"填写登录表单异常: {e}", "ERROR")
        return False
    clicked = False
    for sel in ("xpath://button[@type='submit']", "text:Log in", "text:Login",
                "text:Sign in", "text:登录"):
        try:
            btn = page.ele(sel, timeout=2)
            if btn:
                btn.click()
                clicked = True
                break
        except Exception:
            pass
    if not clicked:
        try:
            page.run_js("document.querySelector('form').submit()")
            clicked = True
        except Exception:
            pass
    time.sleep(6)
    try:
        still_there = page.ele("tag:input@@type=password", timeout=3)
        if still_there:
            log("登录后仍在登录页，可能失败", "ERROR")
            return False
    except Exception:
        pass
    log("面板登录成功（密码框已消失）")
    return True


def find_start_button(page):
    for sel in ('xpath://button[contains(text(), "Start")]',
                'xpath://a[contains(text(), "Start")]'):
        try:
            btn = page.ele(sel, timeout=4)
            if btn:
                return btn
        except Exception:
            pass
    return None


def is_btn_enabled(btn):
    try:
        if not btn.states.is_enabled:
            return False
    except Exception:
        pass
    try:
        if btn.attr("disabled") is not None:
            return False
        cls = (btn.attr("class") or "").lower()
        if "disabled" in cls:
            return False
    except Exception:
        pass
    return True


def read_power(page):
    try:
        srv = page.ele(f"text:{SERVER_NAME}", timeout=10)
    except Exception:
        srv = None
    if not srv:
        log(f"面板未找到 {SERVER_NAME}", "ERROR")
        return "unknown"
    btn = find_start_button(page)
    if not btn:
        log("未找到 Start 按钮", "ERROR")
        try:
            txt = (page.ele("tag:body", timeout=3).text or "")[:500]
            log(f"页面文本摘要: {txt!r}")
        except Exception:
            pass
        return "unknown"
    enabled = is_btn_enabled(btn)
    log(f"Start 按钮可点={enabled}（可点=已关机，不可点=运行中）")
    return "stopped" if enabled else "running"


def boot_server(page):
    btn = find_start_button(page)
    if not btn:
        return False
    try:
        btn.click()
    except Exception:
        try:
            btn.click(by_js=True)
        except Exception as e:
            log(f"点击 Start 异常: {e}", "ERROR")
            return False
    log("已点击 Start，等待 25 秒后复查")
    time.sleep(25)
    return read_power(page) == "running"


def main():
    vdisplay = Xvfb(width=1280, height=720, colordepth=24)
    vdisplay.start()
    page = None
    try:
        page = make_page()
        try:
            text, secs = read_countdown(page)
            result["countdown"] = text
            result["remaining_s"] = secs
            if secs is None:
                result["errors"].append("续期倒计时读取失败")
        except Exception as e:
            result["errors"].append(f"续期页异常: {e}")

        if not PANEL_USER or not PANEL_PASS:
            result["panel_skipped"] = True
            log("未配置 H2P_PANEL_UN/H2P_PANEL_PW，跳过面板检查", "WARN")
        else:
            try:
                if panel_login(page):
                    power = read_power(page)
                    result["power"] = power
                    if power == "stopped":
                        log("检测到关机，尝试开机", "WARN")
                        ok = boot_server(page)
                        result["booted"] = ok
                        result["power"] = "running" if ok else "stopped"
                        send_tg(
                            f"⚡ Host2Play 状态检测\n服务器 {SERVER_NAME} 检测到关机，"
                            f"已执行开机：{'成功，恢复运行中' if ok else '失败，请人工检查面板'}\n"
                            f"续期倒计时：{result['countdown'] or '未知'}"
                        )
                    elif power == "unknown":
                        result["errors"].append("面板电源状态未知")
                else:
                    result["errors"].append("面板登录失败")
            except Exception as e:
                result["errors"].append(f"面板检查异常: {e}")

        if result["errors"]:
            send_tg(
                "🔴 Host2Play 状态检测异常\n"
                + "\n".join(result["errors"])
                + f"\n倒计时：{result['countdown'] or '未知'}\n电源：{result['power']}"
            )
    finally:
        if page:
            try:
                page.quit()
            except Exception:
                pass
        vdisplay.stop()
        with open("status-result.json", "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)

    if result["remaining_s"] is None and result["power"] == "unknown" and not result["panel_skipped"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
