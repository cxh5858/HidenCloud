#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import time
import requests
from datetime import datetime, timezone, timedelta
from seleniumbase import Driver

# ====================== 配置区域 ======================
HIDENCLOUD = os.getenv("HIDENCLOUD", "")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
PROXY_SERVER = os.getenv("PROXY_SERVER", "")

# HIDENCLOUD secret 格式:
#   email-----password            （仅账号密码，走 Turnstile 登录）
#   email-----password-----cookie （优先用 cookie 跳过 Turnstile，失败再回退密码）
parts = HIDENCLOUD.split("-----")
if len(parts) >= 2:
    HIDEN_EMAIL  = parts[0].strip()
    HIDEN_PWD    = parts[1].strip()
    HIDEN_COOKIE = parts[2].strip() if len(parts) >= 3 else ""
else:
    raise ValueError("❌ HIDENCLOUD 格式错误，应为 email-----password 或 email-----password-----cookie")

COOKIE_NAME    = "remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d"
BASE_URL       = "https://dash.hidencloud.com"
STATE_DIR      = "browser_state"
SCREENSHOT_DIR = "screenshots"

os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

USER_DATA_DIR = os.path.abspath(os.path.join(STATE_DIR, "selenium_profile"))

MAX_RETRY = 3


# ====================== 工具函数 ======================
def get_bj_time():
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')


def send_tg_notification(message, photo_path=None):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("[WARN] 未配置 TG 信息，跳过发送")
        return
    try:
        if photo_path and os.path.exists(photo_path):
            url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto"
            with open(photo_path, 'rb') as f:
                requests.post(url, files={'photo': f}, data={
                    'chat_id': TG_CHAT_ID, 'caption': message, 'parse_mode': 'Markdown'
                }, timeout=30)
        else:
            requests.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "Markdown"},
                timeout=10
            )
        print("[INFO] 📡 TG 通知已发送")
    except Exception as e:
        print(f"[ERROR] TG 发送失败: {e}")


def take_screenshot(driver, name):
    timestamp = datetime.now().strftime('%H%M%S')
    filename = f"{SCREENSHOT_DIR}/{timestamp}-{name}.png"
    try:
        driver.save_screenshot(filename)
        print(f"[INFO] 📸 截图 → {filename}")
    except Exception as e:
        print(f"[WARN] 截图失败: {e}")
    return filename


def wait_for_turnstile_token(driver, timeout=90):
    print("[INFO] ⏳ 等待 Turnstile 验证通过...")
    start = time.time()
    while time.time() - start < timeout:
        token = driver.execute_script(
            'return document.querySelector("[name=cf-turnstile-response]")?.value'
        )
        if token and len(token) > 20:
            print("[INFO] ✅ Turnstile token 已生成")
            return True
        time.sleep(1)
    return False


def wait_for_url_contains(driver, keyword, timeout=45):
    start = time.time()
    while time.time() - start < timeout:
        if keyword in driver.current_url:
            return True
        time.sleep(0.5)
    return False


def check_login_error(driver):
    try:
        for sel in [".text-red-500", ".alert-danger", "[role='alert']", ".error", ".invalid-feedback"]:
            elem = driver.find_element(sel, by="css selector")
            if elem and elem.is_displayed() and elem.text.strip():
                return elem.text.strip()
    except:
        pass
    return None


def mask_email(email):
    if '@' in email:
        local, domain = email.split('@', 1)
        return f"{local[:3]}***@{domain}"
    return f"{email[:3]}***"


def parse_due_date(text):
    if not text:
        return None
    match = re.search(r'(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})', text)
    if match:
        day, month_str, year = match.groups()
        try:
            return datetime.strptime(f"{day} {month_str} {year}", "%d %b %Y").strftime("%Y-%m-%d")
        except:
            pass
    if re.match(r'\d{4}-\d{2}-\d{2}', text):
        return text
    return None


def get_current_due_date(driver):
    try:
        due_elem = driver.find_element(
            "xpath", "//h6[contains(text(),'Due date')]/following-sibling::div"
        )
        raw = due_elem.text.strip()
        return raw, parse_due_date(raw)
    except:
        return "N/A", None


def save_due_date(due_date_std):
    """写入 due_date.txt，供 workflow Cron 更新步骤读取"""
    if not due_date_std:
        return
    try:
        with open("due_date.txt", "w") as f:
            f.write(due_date_std)
        print(f"[INFO] 📄 到期时间已写入 due_date.txt: {due_date_std}")
    except Exception as e:
        print(f"[WARN] 写入 due_date.txt 失败: {e}")


def create_driver():
    kwargs = {
        "headless": True,
        "headless2": True,
        "uc": True,
        "user_data_dir": USER_DATA_DIR,
        "window_size": "1280,753",
        "disable_csp": True,
        "agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/147.0.0.0 Safari/537.36"
        ),
    }
    if PROXY_SERVER:
        kwargs["proxy"] = PROXY_SERVER
        print(f"[INFO] 🌐 使用代理: {PROXY_SERVER}")
    driver = Driver(**kwargs)
    driver.set_page_load_timeout(60)
    driver.set_script_timeout(60)
    return driver


# ====================== 登录方式 ======================
def inject_cookie_and_verify(driver):
    """
    注入 remember me cookie 后访问 dashboard 验证是否生效。
    成功返回 True，失败返回 False。
    """
    print("[INFO] 🍪 尝试 Cookie 登录...")

    # 必须先访问同域页面才能写 cookie
    driver.get(f"{BASE_URL}/auth/login")
    time.sleep(2)

    driver.execute_script(
        f"document.cookie = '{COOKIE_NAME}={HIDEN_COOKIE}; "
        f"path=/; domain=dash.hidencloud.com; secure; SameSite=Lax';"
    )
    print("[INFO] 🍪 Cookie 已注入")

    driver.get(f"{BASE_URL}/dashboard")
    time.sleep(3)
    take_screenshot(driver, "cookie-verify")

    if "/auth/login" not in driver.current_url and "/dashboard" in driver.current_url:
        print("[INFO] ✅ Cookie 登录成功")
        return True

    print("[WARN] ⚠️ Cookie 失效或已过期，回退至账号密码登录")
    return False


def do_login_with_credentials(driver):
    """
    账号密码 + Turnstile 登录，Turnstile 失败最多重试 3 次。
    """
    TURNSTILE_RETRY = 3
    for attempt in range(1, TURNSTILE_RETRY + 1):
        print(f"[INFO] 🔒 账号密码登录尝试 {attempt}/{TURNSTILE_RETRY}")
        driver.get(f"{BASE_URL}/auth/login")
        time.sleep(3)
        take_screenshot(driver, f"pwd-login-{attempt}-page")

        driver.type("input#username", HIDEN_EMAIL)
        driver.type("input#password", HIDEN_PWD)

        print("[INFO] ⏳ 等待 Turnstile 加载...")
        time.sleep(5)

        if driver.is_element_present(".cf-turnstile"):
            print("[INFO] 🖱️ 尝试点击 Turnstile...")
            try:
                driver.uc_gui_click_cf(".cf-turnstile")
            except:
                try:
                    driver.click(".cf-turnstile")
                except:
                    pass
            take_screenshot(driver, f"pwd-login-{attempt}-turnstile")

            if not wait_for_turnstile_token(driver, timeout=90):
                take_screenshot(driver, f"pwd-login-{attempt}-turnstile-timeout")
                if attempt < TURNSTILE_RETRY:
                    wait_sec = attempt * 15
                    print(f"[WARN] Turnstile 超时，{wait_sec}s 后重试...")
                    time.sleep(wait_sec)
                    continue
                raise Exception("Turnstile 验证多次超时")
        else:
            print("[WARN] 未找到 Turnstile 元素，直接提交")

        driver.click("button[type='submit']")
        take_screenshot(driver, f"pwd-login-{attempt}-submitted")

        if wait_for_url_contains(driver, "/dashboard", timeout=45):
            print("[INFO] ✅ 账号密码登录成功")
            return True

        error_text = check_login_error(driver)
        if error_text:
            raise Exception(f"账号或密码错误: {error_text}")

        time.sleep(5)
        if "/dashboard" in driver.current_url:
            return True

        if attempt < TURNSTILE_RETRY:
            wait_sec = attempt * 20
            print(f"[WARN] 登录后未跳转，{wait_sec}s 后重试...")
            time.sleep(wait_sec)

    raise Exception("账号密码登录多次失败")


def ensure_logged_in(driver):
    """
    登录总入口（优先级）：
      1. 已有有效 Session → 直接跳过
      2. 配置了 Cookie   → 注入 Cookie
      3. Cookie 失效     → 回退账号密码
      4. 未配置 Cookie   → 直接账号密码
    """
    driver.get(f"{BASE_URL}/dashboard")
    time.sleep(3)

    if "/auth/login" not in driver.current_url:
        print("[INFO] ✅ 已有有效 Session，无需登录")
        take_screenshot(driver, "already-logged-in")
        return

    if HIDEN_COOKIE:
        if inject_cookie_and_verify(driver):
            return
        print("[INFO] 回退至账号密码登录...")

    do_login_with_credentials(driver)


# ====================== 续期流程 ======================
def do_renew_once(driver):
    sid = None
    restricted = False
    renew_executed = False
    days_left = None
    threshold = None
    final_screenshot = None

    # ---------- 1. 确保已登录 ----------
    ensure_logged_in(driver)

    # ---------- 2. 提取服务器 ID ----------
    print("[INFO] 🔍 提取服务器 ID...")
    driver.get(f"{BASE_URL}/dashboard")
    time.sleep(3)
    take_screenshot(driver, "dashboard")

    try:
        element = driver.find_element("xpath", "//span[contains(text(),'Free Server #')]")
        match = re.search(r'Free Server #(\d+)', element.text.strip())
        if match:
            sid = match.group(1)
            print("[INFO] ✅ 提取到服务器 ID: ***")
    except Exception as e:
        print(f"[ERROR] 提取服务器 ID 失败: {e}")

    if not sid:
        take_screenshot(driver, "ERROR-no-server-id")
        raise Exception("无法提取服务器 ID")

    manage_url = f"{BASE_URL}/service/{sid}/manage"
    print("[INFO] 🚀 访问管理页面")
    driver.get(manage_url)
    time.sleep(3)
    take_screenshot(driver, "manage-page")

    # ---------- 3. 续订前到期时间 ----------
    due_date_before_raw, due_date_before_std = get_current_due_date(driver)
    print(f"[INFO] 续订前到期时间: {due_date_before_raw}")

    # ---------- 4. 续期操作 ----------
    try:
        print("[INFO] 🔄 查找 Renew 按钮...")
        renew_btn = None
        for by, value in [
            ("css selector", "button[onclick*='showRenewAlert']"),
            ("xpath", "//button[.//i[contains(@class, 'bx-recycle')]]"),
            ("xpath", "//button[contains(text(),'Renew')]"),
        ]:
            try:
                btn = driver.find_element(by, value)
                if btn.is_displayed():
                    renew_btn = btn
                    break
            except:
                continue

        if not renew_btn:
            take_screenshot(driver, "ERROR-no-renew-btn")
            raise Exception("未找到 Renew 按钮")

        onclick_val = renew_btn.get_attribute("onclick") or ""
        param_match = re.search(r'showRenewAlert\((\d+),\s*(\d+),\s*(true|false)\)', onclick_val)
        if param_match:
            days_left = int(param_match.group(1))
            threshold = int(param_match.group(2))
            print(f"[INFO] 剩余: {days_left} 天，续期阈值: ≤{threshold} 天")

        renew_btn.click()
        renew_executed = True
        time.sleep(3)
        take_screenshot(driver, "renew-clicked")

        restriction_h3 = driver.execute_script(
            "var el=document.querySelector('.fixed.inset-0 h3');"
            "return el?el.textContent.trim():'';"
        )
        if 'Renewal Restricted' in restriction_h3:
            restricted = True
            alert_text = driver.execute_script(
                "var el=document.querySelector('.fixed.inset-0 p');"
                "return el?el.textContent.trim():'';"
            )
            print(f"[INFO] ⚠️ 续期限制: {alert_text}")
            take_screenshot(driver, "renewal-restricted")
            try:
                driver.find_element("xpath", "//button[contains(text(),'OK')]").click()
                time.sleep(1)
            except:
                pass
        else:
            modal_selector = f"div#renewService-{sid}"
            driver.wait_for_element_visible(modal_selector, timeout=10)
            take_screenshot(driver, "renew-modal")

            driver.find_element(
                by="css selector", value=f"{modal_selector} button[type='submit']"
            ).click()
            time.sleep(3)
            take_screenshot(driver, "invoice-created")

            time.sleep(5)
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1)

            pay_clicked = driver.execute_script("""
                var btn=document.querySelector('button[type="submit"]');
                if(btn && btn.innerText.includes('Pay')){btn.click();return true;}
                return false;
            """)
            time.sleep(5)
            take_screenshot(driver, "pay-done" if pay_clicked else "no-pay-btn")
            if not pay_clicked:
                print("[WARN] 未找到 Pay 按钮，可能免费服务自动完成")

    except Exception as e:
        take_screenshot(driver, "ERROR-renew")
        raise e

    # ---------- 5. 续订后到期时间 ----------
    driver.get(manage_url)
    time.sleep(3)
    due_date_after_raw, due_date_after_std = get_current_due_date(driver)
    print(f"[INFO] 续订后到期时间: {due_date_after_raw}")
    final_screenshot = take_screenshot(driver, "final-due-date")

    # 打印标准格式（workflow grep 兜底）
    print(f"到期时间(标准): {due_date_after_std or due_date_after_raw}")

    # ★ 写入文件供 workflow 读取
    save_due_date(due_date_after_std)

    # ---------- 6. 判断结果 ----------
    if restricted:
        result_status = "ℹ️ 暂无可续期"
    elif due_date_before_std and due_date_after_std:
        result_status = "✅ 续订成功" if due_date_after_std > due_date_before_std else "❌ 续订失败"
    elif renew_executed:
        result_status = "⚠️ 续期已执行，请确认"
    else:
        result_status = "❌ 续订失败"

    return (
        result_status, due_date_before_raw, due_date_after_raw,
        due_date_after_std, final_screenshot, sid, restricted, days_left, threshold
    )


# ====================== 主逻辑 ======================
def main():
    print("[INFO] " + "=" * 50)
    print("[INFO] HidenCloud 自动续期脚本 (SeleniumBase)")
    print("[INFO] " + "=" * 50)
    print(f"[INFO] 📂 状态目录: {USER_DATA_DIR}")
    print(f"[INFO] 🔑 登录方式: {'Cookie 优先，失败回退密码' if HIDEN_COOKIE else '账号密码 + Turnstile'}")

    last_error = None

    for attempt in range(1, MAX_RETRY + 1):
        print(f"\n[INFO] {'=' * 20} 第 {attempt}/{MAX_RETRY} 次尝试 {'=' * 20}")
        driver = create_driver()
        try:
            driver.get("about:blank")
        except:
            pass
        time.sleep(2)

        try:
            (
                result_status, due_date_before_raw, due_date_after_raw,
                due_date_after_std, final_screenshot, sid,
                restricted, days_left, threshold
            ) = do_renew_once(driver)

            bj_time = get_bj_time()
            change_info = (
                due_date_after_raw
                if due_date_before_raw == due_date_after_raw
                else f"{due_date_before_raw} → {due_date_after_raw}"
            ) if due_date_before_raw != "N/A" else due_date_after_raw

            extra_info = (
                f"\n剩余: {days_left} 天 (需 ≤{threshold} 天可续)"
                if restricted and days_left is not None else ""
            )

            send_tg_notification(
                f"{result_status}\n\n"
                f"账号: `{mask_email(HIDEN_EMAIL)}`\n"
                f"服务器: `Free Server #{sid}`\n"
                f"到期: {change_info}{extra_info}\n"
                f"时间: {bj_time}\n\n"
                f"HidenCloud Auto Renew",
                photo_path=final_screenshot
            )
            print(f"[INFO] 🎉 任务完成 — {result_status}")
            return

        except Exception as e:
            last_error = e
            print(f"[ERROR] ❌ 第 {attempt} 次失败: {e}")
            try:
                take_screenshot(driver, f"ERROR-attempt-{attempt}")
            except:
                pass
        finally:
            try:
                driver.quit()
            except:
                pass

        if attempt < MAX_RETRY:
            wait_sec = attempt * 30
            print(f"[INFO] ⏳ {wait_sec}s 后进行第 {attempt + 1} 次重试...")
            time.sleep(wait_sec)

    print(f"[ERROR] ❌ 所有 {MAX_RETRY} 次重试均失败")
    send_tg_notification(
        f"❌ HidenCloud 续期失败（已重试 {MAX_RETRY} 次）\n"
        f"最后错误: {str(last_error)[:200]}"
    )
    raise last_error


if __name__ == "__main__":
    main()
