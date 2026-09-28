#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import time
import subprocess
import requests
from datetime import datetime, timezone, timedelta
from seleniumbase import Driver

# ====================== 配置区域 ======================
HIDENCLOUD = os.getenv("HIDENCLOUD", "")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
PROXY_SERVER = os.getenv("PROXY_SERVER", "")

# HIDENCLOUD secret 格式:
#   email-----password                        （仅账号密码）
#   email-----password-----remember_cookie    （Cookie 优先）
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

# 跨重试共享：记录第一次尝试时的续订前到期时间，
# 用于识别"上一次其实已续期成功，只是后续超时"的情况
RENEW_STATE = {"before_std": None, "before_raw": None}


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


def safe_get(driver, url):
    """页面加载超时时不抛异常（页面通常已可用），其他异常照常抛出"""
    try:
        driver.get(url)
    except Exception as e:
        msg = str(e).lower()
        if "timeout" in msg or "timed out" in msg:
            print(f"[WARN] 页面加载超时，继续后续操作: {url}")
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
        else:
            raise


def js_click(driver, element):
    """滚动到视口中央后用 JS 点击，绕开遮挡层，也不会阻塞等待页面加载"""
    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center', inline:'center'});", element
    )
    time.sleep(1)
    driver.execute_script("arguments[0].click();", element)


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
        "window_size": "1920,1080",
        "disable_csp": True,
        # 不再写死 UA，避免与真实 Chrome 版本不一致
    }
    if PROXY_SERVER:
        kwargs["proxy"] = PROXY_SERVER
        print(f"[INFO] 🌐 使用代理: {PROXY_SERVER}")
    driver = Driver(**kwargs)
    driver.set_page_load_timeout(60)
    driver.set_script_timeout(60)
    return driver


# ====================== Cookie 管理 ======================
def get_latest_cookies(driver):
    remember_value = ""
    session_value  = ""
    try:
        cookies = driver.get_cookies()
        for c in cookies:
            if c.get("name") == COOKIE_NAME:
                remember_value = c["value"]
            if c.get("name") == "laravel_session":
                session_value = c["value"]
    except Exception as e:
        print(f"[WARN] 提取 cookie 失败: {e}")
    return remember_value, session_value


def refresh_cookie_to_secret(driver):
    repo = os.getenv("GITHUB_REPOSITORY", "")
    gh_token = os.getenv("GH_TOKEN", "")
    if not repo or not gh_token:
        print("[WARN] 未配置 GH_TOKEN 或 GITHUB_REPOSITORY，跳过 cookie 刷新")
        return

    remember_value, _ = get_latest_cookies(driver)
    if not remember_value:
        print("[WARN] 浏览器中未找到 remember me cookie，跳过刷新")
        return

    if remember_value == HIDEN_COOKIE:
        print("[INFO] Cookie 未变化，无需刷新")
        return

    new_secret = f"{HIDEN_EMAIL}-----{HIDEN_PWD}-----{remember_value}"
    try:
        result = subprocess.run(
            ["gh", "secret", "set", "HIDENCLOUD",
             "--body", new_secret,
             "--repo", repo],
            capture_output=True, text=True,
            env={**os.environ, "GH_TOKEN": gh_token}
        )
        if result.returncode == 0:
            print("[INFO] 🔄 Cookie 已自动刷新到 GitHub Secret")
        else:
            print(f"[WARN] Cookie 刷新失败: {result.stderr.strip()}")
    except FileNotFoundError:
        print("[WARN] gh CLI 未找到，跳过 cookie 刷新")
    except Exception as e:
        print(f"[WARN] Cookie 刷新异常: {e}")


# ====================== 登录方式 ======================
def inject_cookies(driver, remember_value, session_value=""):
    if remember_value:
        driver.execute_script(
            f"document.cookie = '{COOKIE_NAME}={remember_value}; "
            f"path=/; domain=dash.hidencloud.com; secure; SameSite=Lax';"
        )
        print("[INFO] 🍪 remember me cookie 已注入")

    if session_value:
        driver.execute_script(
            f"document.cookie = 'laravel_session={session_value}; "
            f"path=/; domain=dash.hidencloud.com; secure; SameSite=Lax';"
        )
        print("[INFO] 🍪 session cookie 已注入")


def inject_cookie_and_verify(driver):
    print("[INFO] 🍪 尝试 Cookie 登录...")

    safe_get(driver, f"{BASE_URL}/auth/login")
    time.sleep(2)

    inject_cookies(driver, HIDEN_COOKIE)

    safe_get(driver, f"{BASE_URL}/dashboard")
    time.sleep(3)
    take_screenshot(driver, "cookie-verify")

    if "/auth/login" not in driver.current_url and "/dashboard" in driver.current_url:
        print("[INFO] ✅ Cookie 登录成功")
        return True

    print("[WARN] ⚠️ Cookie 失效或已过期，回退至账号密码登录")
    return False


def do_login_with_credentials(driver):
    TURNSTILE_RETRY = 3
    for attempt in range(1, TURNSTILE_RETRY + 1):
        print(f"[INFO] 🔒 账号密码登录尝试 {attempt}/{TURNSTILE_RETRY}")
        safe_get(driver, f"{BASE_URL}/auth/login")
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
    safe_get(driver, f"{BASE_URL}/dashboard")
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
    safe_get(driver, f"{BASE_URL}/dashboard")
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
    safe_get(driver, manage_url)
    time.sleep(3)
    take_screenshot(driver, "manage-page")

    # ---------- 3. 续订前到期时间 ----------
    due_date_before_raw, due_date_before_std = get_current_due_date(driver)
    print(f"[INFO] 续订前到期时间: {due_date_before_raw}")

    # 重试时如果到期时间已比第一次尝试前更晚，说明上一次其实续期成功了
    if RENEW_STATE["before_std"] is None:
        RENEW_STATE["before_std"] = due_date_before_std
        RENEW_STATE["before_raw"] = due_date_before_raw
    elif (due_date_before_std and RENEW_STATE["before_std"]
          and due_date_before_std > RENEW_STATE["before_std"]):
        print("[INFO] ✅ 检测到上一次尝试已续期成功，不再重复操作")
        print(f"到期时间(标准): {due_date_before_std}")
        save_due_date(due_date_before_std)
        refresh_cookie_to_secret(driver)
        return (
            "✅ 续订成功", RENEW_STATE["before_raw"], due_date_before_raw,
            due_date_before_std, take_screenshot(driver, "final-due-date"),
            sid, False, None, None
        )

    # 先保底写入当前到期时间，即使后面异常，workflow 也能更新 cron
    save_due_date(due_date_before_std)

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

        js_click(driver, renew_btn)
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
                ok_btn = driver.find_element("xpath", "//button[contains(text(),'OK')]")
                js_click(driver, ok_btn)
                time.sleep(1)
            except:
                pass
        else:
            modal_selector = f"div#renewService-{sid}"
            driver.wait_for_element_visible(modal_selector, timeout=10)
            take_screenshot(driver, "renew-modal")

            submit_btn = driver.find_element(
                by="css selector", value=f"{modal_selector} button[type='submit']"
            )
            js_click(driver, submit_btn)
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
    safe_get(driver, manage_url)
    time.sleep(3)
    due_date_after_raw, due_date_after_std = get_current_due_date(driver)
    print(f"[INFO] 续订后到期时间: {due_date_after_raw}")
    final_screenshot = take_screenshot(driver, "final-due-date")

    print(f"到期时间(标准): {due_date_after_std or due_date_after_raw}")
    save_due_date(due_date_after_std)

    refresh_cookie_to_secret(driver)

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
