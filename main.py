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

if "-----" in HIDENCLOUD:
    HIDEN_EMAIL, HIDEN_PWD = HIDENCLOUD.split("-----", 1)
else:
    raise ValueError("❌ HIDENCLOUD 格式错误，应为 email-----password")

BASE_URL = "https://dash.hidencloud.com"
STATE_DIR = "browser_state"
SCREENSHOT_DIR = "screenshots"

os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

USER_DATA_DIR = os.path.abspath(os.path.join(STATE_DIR, "selenium_profile"))

# 最大整体重试次数（Turnstile 失败或续期失败时重试）
MAX_RETRY = 3


# ====================== 工具函数 ======================
def get_bj_time():
    """返回北京时间字符串"""
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')


def send_tg_notification(message, photo_path=None):
    """发送 Telegram 通知，可附带截图"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("[WARN] 未配置 TG 信息，跳过发送")
        return
    try:
        if photo_path and os.path.exists(photo_path):
            url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto"
            with open(photo_path, 'rb') as f:
                files = {'photo': f}
                data = {'chat_id': TG_CHAT_ID, 'caption': message, 'parse_mode': 'Markdown'}
                requests.post(url, files=files, data=data, timeout=30)
        else:
            url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "Markdown"}
            requests.post(url, json=payload, timeout=10)
        print("[INFO] 📡 TG 通知已发送")
    except Exception as e:
        print(f"[ERROR] TG 发送失败: {e}")


def take_screenshot(driver, name):
    """截图并返回文件路径"""
    timestamp = datetime.now().strftime('%H%M%S')
    filename = f"{SCREENSHOT_DIR}/{timestamp}-{name}.png"
    try:
        driver.save_screenshot(filename)
        print(f"[INFO] 📸 截图 → {filename}")
    except Exception as e:
        print(f"[WARN] 截图失败: {e}")
    return filename


def wait_for_turnstile_token(driver, timeout=90):
    """
    等待 Cloudflare Turnstile token 生成。
    每秒检查一次，超时返回 False。
    """
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
    """等待当前 URL 包含特定关键字"""
    start = time.time()
    while time.time() - start < timeout:
        if keyword in driver.current_url:
            return True
        time.sleep(0.5)
    return False


def check_login_error(driver):
    """检查页面是否有登录错误信息"""
    try:
        error_selectors = [
            ".text-red-500", ".alert-danger", "[role='alert']", ".error", ".invalid-feedback"
        ]
        for sel in error_selectors:
            elem = driver.find_element(sel, by="css selector")
            if elem and elem.is_displayed() and elem.text.strip():
                return elem.text.strip()
    except:
        pass
    return None


def mask_email(email):
    """邮箱脱敏显示"""
    if '@' in email:
        local, domain = email.split('@', 1)
        return f"{local[:3]}***@{domain}"
    return f"{email[:3]}***"


def parse_due_date(text):
    """将页面显示的日期字符串转换为 YYYY-MM-DD 格式"""
    if not text:
        return None
    # 格式: "28 Apr 2026"
    match = re.search(r'(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})', text)
    if match:
        day, month_str, year = match.groups()
        try:
            dt = datetime.strptime(f"{day} {month_str} {year}", "%d %b %Y")
            return dt.strftime("%Y-%m-%d")
        except:
            pass
    # 已经是标准格式
    if re.match(r'\d{4}-\d{2}-\d{2}', text):
        return text
    return None


def get_current_due_date(driver):
    """获取当前管理页面的到期时间，返回原始字符串和标准化日期"""
    try:
        due_elem = driver.find_element(
            "xpath", "//h6[contains(text(),'Due date')]/following-sibling::div"
        )
        raw = due_elem.text.strip()
        std = parse_due_date(raw)
        return raw, std
    except:
        return "N/A", None


def save_due_date(due_date_std):
    """
    将标准化到期日期写入 due_date.txt，供 workflow 的 Cron 更新步骤读取。
    格式: YYYY-MM-DD
    """
    if not due_date_std:
        return
    try:
        with open("due_date.txt", "w") as f:
            f.write(due_date_std)
        print(f"[INFO] 📄 到期时间已写入 due_date.txt: {due_date_std}")
    except Exception as e:
        print(f"[WARN] 写入 due_date.txt 失败: {e}")


def create_driver():
    """创建并返回浏览器驱动"""
    driver_kwargs = {
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
        driver_kwargs["proxy"] = PROXY_SERVER
        print(f"[INFO] 🌐 使用代理: {PROXY_SERVER}")

    driver = Driver(**driver_kwargs)
    driver.set_page_load_timeout(60)
    driver.set_script_timeout(60)
    return driver


def do_login(driver):
    """
    执行登录流程，包含 Turnstile 重试。
    Turnstile 失败时最多重试 3 次（重新加载页面）。
    成功返回 True，失败抛出异常。
    """
    TURNSTILE_RETRY = 3

    for attempt in range(1, TURNSTILE_RETRY + 1):
        print(f"[INFO] 🔒 登录尝试 {attempt}/{TURNSTILE_RETRY}")

        # 重新加载登录页，确保 Turnstile 刷新
        driver.get(f"{BASE_URL}/auth/login")
        time.sleep(3)
        take_screenshot(driver, f"login-{attempt}-page")

        masked_email = mask_email(HIDEN_EMAIL)
        print(f"[INFO] ✍️ 填写邮箱: {masked_email}")
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
            take_screenshot(driver, f"login-{attempt}-turnstile-clicked")

            token_ok = wait_for_turnstile_token(driver, timeout=90)
            if not token_ok:
                take_screenshot(driver, f"login-{attempt}-turnstile-timeout")
                if attempt < TURNSTILE_RETRY:
                    wait_sec = attempt * 15
                    print(f"[WARN] ⚠️ Turnstile 验证超时，{wait_sec}s 后重试...")
                    time.sleep(wait_sec)
                    continue
                else:
                    raise Exception("Turnstile 验证多次超时，放弃登录")

            take_screenshot(driver, f"login-{attempt}-token-ready")
        else:
            print("[WARN] 未找到 Turnstile 元素，继续提交...")

        print("[INFO] 🚀 提交登录表单")
        driver.click("button[type='submit']")
        take_screenshot(driver, f"login-{attempt}-submitted")

        print("[INFO] ⏳ 等待登录跳转...")
        if wait_for_url_contains(driver, "/dashboard", timeout=45):
            print("[INFO] ✅ 登录成功")
            take_screenshot(driver, f"login-{attempt}-success")
            return True

        # 跳转失败，检查错误信息
        error_text = check_login_error(driver)
        if error_text:
            # 账号密码错误，不重试
            take_screenshot(driver, f"login-{attempt}-error")
            raise Exception(f"登录失败（账号或密码错误）: {error_text}")

        # 可能是 Turnstile 没过，等待后重试
        time.sleep(5)
        if "/dashboard" not in driver.current_url:
            take_screenshot(driver, f"login-{attempt}-stuck")
            if attempt < TURNSTILE_RETRY:
                wait_sec = attempt * 20
                print(f"[WARN] 登录后未跳转，{wait_sec}s 后重试...")
                time.sleep(wait_sec)
                continue
            else:
                raise Exception("登录后多次未能跳转至 Dashboard")

        print("[INFO] ✅ 登录成功")
        return True

    raise Exception("登录重试全部失败")


def do_renew_once(driver):
    """
    执行一次完整的续期流程。
    返回 (result_status, due_date_after_raw, due_date_after_std, final_screenshot, sid)
    失败时抛出异常。
    """
    final_screenshot = None
    due_date_before_raw = "N/A"
    due_date_before_std = None
    due_date_after_raw = "N/A"
    due_date_after_std = None
    sid = None
    restricted = False
    renew_executed = False
    days_left = None
    threshold = None

    # ---------- 1. 访问主页 ----------
    print(f"[INFO] 🌐 访问主页: {BASE_URL}/dashboard")
    driver.get(f"{BASE_URL}/dashboard")
    time.sleep(3)
    take_screenshot(driver, "01-initial")

    # ---------- 2. 登录判断 ----------
    if "/auth/login" in driver.current_url or driver.is_element_visible("input#username"):
        print("[INFO] 🔒 检测到未登录，开始登录流程")
        do_login(driver)
    else:
        print("[INFO] ✅ 已登录，跳过登录流程")
        take_screenshot(driver, "02-already-logged-in")

    # ---------- 3. 提取服务器 ID ----------
    print("[INFO] 🔍 提取服务器 ID...")
    take_screenshot(driver, "08-dashboard")
    time.sleep(3)

    try:
        element = driver.find_element("xpath", "//span[contains(text(),'Free Server #')]")
        text = element.text.strip()
        print("[INFO] 找到服务器文本: Free Server #***")
        match = re.search(r'Free Server #(\d+)', text)
        if match:
            sid = match.group(1)
            print("[INFO] ✅ 提取到服务器 ID: ***")
    except Exception as e:
        print(f"[ERROR] 页面元素定位失败: {e}")

    if not sid:
        take_screenshot(driver, "ERROR-no-server-id")
        raise Exception("无法提取服务器 ID")

    manage_url = f"{BASE_URL}/service/{sid}/manage"
    print(f"[INFO] 🚀 访问管理页面: {BASE_URL}/service/***/manage")
    driver.get(manage_url)
    time.sleep(3)
    take_screenshot(driver, "09-manage-page")

    # ---------- 4. 获取续订前到期时间 ----------
    due_date_before_raw, due_date_before_std = get_current_due_date(driver)
    print(f"[INFO] 续订前到期时间: {due_date_before_raw}")

    # ---------- 5. 续期操作 ----------
    try:
        print("[INFO] 🔄 查找并点击 Renew 按钮...")

        renew_btn = None
        selectors = [
            ("css selector", "button[onclick*='showRenewAlert']"),
            ("xpath", "//button[.//i[contains(@class, 'bx-recycle')]]"),
            ("xpath", "//button[contains(text(),'Renew')]"),
        ]
        for by, value in selectors:
            try:
                renew_btn = driver.find_element(by, value)
                if renew_btn.is_displayed():
                    break
            except:
                continue

        if not renew_btn:
            take_screenshot(driver, "ERROR-renew-button-not-found")
            raise Exception("页面上未找到 Renew 按钮")

        onclick_val = renew_btn.get_attribute("onclick") or ""
        print(f"[INFO] Renew 按钮 onclick: {onclick_val}")

        param_match = re.search(
            r'showRenewAlert\((\d+),\s*(\d+),\s*(true|false)\)', onclick_val
        )
        if param_match:
            days_left = int(param_match.group(1))
            threshold = int(param_match.group(2))
            is_free = param_match.group(3) == "true"
            print(f"[INFO] 到期剩余: {days_left} 天, 续期阈值: ≤{threshold} 天, 免费服务: {is_free}")

        renew_btn.click()
        renew_executed = True
        print("[INFO] ✅ Renew 按钮已点击")
        time.sleep(3)
        take_screenshot(driver, "10-renew-clicked")
        time.sleep(1)

        # 检测限制弹窗
        restriction_h3 = driver.execute_script(
            "var el = document.querySelector('.fixed.inset-0 h3');"
            "return el ? el.textContent.trim() : '';"
        )
        if 'Renewal Restricted' in restriction_h3:
            restricted = True
            alert_text = driver.execute_script(
                "var el = document.querySelector('.fixed.inset-0 p');"
                "return el ? el.textContent.trim() : '';"
            )
            print(f"[INFO] ⚠️ 触发限制弹窗: {alert_text}")
            take_screenshot(driver, "11-renewal-restricted-popup")
            try:
                ok_btn = driver.find_element("xpath", "//button[contains(text(),'OK')]")
                ok_btn.click()
                time.sleep(1)
                print("[INFO] 已关闭限制弹窗")
            except:
                pass
        else:
            print("[INFO] 📦 等待续期模态框...")
            modal_selector = f"div#renewService-{sid}"
            driver.wait_for_element_visible(modal_selector, timeout=10)
            take_screenshot(driver, "11-renew-modal-opened")

            print("[INFO] 📦 点击 Create Invoice...")
            submit_btn = driver.find_element(
                by="css selector", value=f"{modal_selector} button[type='submit']"
            )
            submit_btn.click()
            time.sleep(3)
            take_screenshot(driver, "12-invoice-created")

            print("[INFO] 💳 等待支付页面...")
            time.sleep(5)
            take_screenshot(driver, "13-invoice-page")

            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1)
            take_screenshot(driver, "14-scrolled-to-bottom")

            pay_clicked = driver.execute_script("""
                var btn = document.querySelector('button[type="submit"]');
                if(btn && btn.innerText.includes('Pay')) {
                    btn.click();
                    return true;
                }
                return false;
            """)

            if pay_clicked:
                print("[INFO] ✅ Pay 按钮已点击")
                print("[INFO] ⏳ 等待支付完成...")
                time.sleep(5)
                take_screenshot(driver, "15-pay-clicked")
            else:
                print("[WARN] 未找到 Pay 按钮，可能免费服务自动完成")
                take_screenshot(driver, "15-no-pay-button")

    except Exception as e:
        take_screenshot(driver, "ERROR-renew-process")
        raise e

    # ---------- 6. 获取续订后到期时间 ----------
    driver.get(manage_url)
    time.sleep(3)
    due_date_after_raw, due_date_after_std = get_current_due_date(driver)
    print(f"[INFO] 续订后到期时间: {due_date_after_raw}")
    final_screenshot = take_screenshot(driver, "16-final-due-date")

    # 打印标准格式（供 workflow grep 兜底用）
    std_out = due_date_after_std or due_date_after_raw
    print(f"到期时间(标准): {std_out}")

    # ★ 写入 due_date.txt，供 workflow Cron 更新步骤读取
    save_due_date(due_date_after_std)

    # ---------- 7. 判断结果状态 ----------
    if restricted:
        result_status = "ℹ️ 暂无可续期"
    elif due_date_before_std and due_date_after_std:
        if due_date_after_std > due_date_before_std:
            result_status = "✅ 续订成功"
        else:
            result_status = "❌ 续订失败"
    elif renew_executed:
        result_status = "⚠️ 续期已执行，请确认"
    else:
        result_status = "❌ 续订失败"

    return result_status, due_date_before_raw, due_date_after_raw, due_date_after_std, final_screenshot, sid, restricted, days_left, threshold


# ====================== 主逻辑 ======================
def main():
    print("[INFO] " + "=" * 50)
    print("[INFO] HidenCloud 自动续期脚本 (SeleniumBase)")
    print("[INFO] " + "=" * 50)
    print(f"[INFO] 📂 状态目录: {USER_DATA_DIR}")
    print(f"[INFO] 📸 截图目录: {SCREENSHOT_DIR}")

    last_error = None

    for attempt in range(1, MAX_RETRY + 1):
        print(f"\n[INFO] {'=' * 20} 第 {attempt}/{MAX_RETRY} 次尝试 {'=' * 20}")
        driver = create_driver()

        try:
            driver.get("about:blank")
        except Exception as e:
            print(f"[WARN] 访问 about:blank 失败（可忽略）: {e}")

        time.sleep(2)

        try:
            (
                result_status,
                due_date_before_raw,
                due_date_after_raw,
                due_date_after_std,
                final_screenshot,
                sid,
                restricted,
                days_left,
                threshold,
            ) = do_renew_once(driver)

            # 成功，发送通知并退出
            bj_time = get_bj_time()
            if due_date_before_raw != "N/A" and due_date_after_raw != "N/A":
                change_info = (
                    due_date_after_raw
                    if due_date_before_raw == due_date_after_raw
                    else f"{due_date_before_raw} → {due_date_after_raw}"
                )
            else:
                change_info = due_date_after_raw

            extra_info = ""
            if restricted and days_left is not None and threshold is not None:
                extra_info = f"\n剩余: {days_left} 天 (需 ≤{threshold} 天可续)"

            tg_caption = (
                f"{result_status}\n\n"
                f"账号: `{HIDEN_EMAIL}`\n"
                f"服务器: `Free Server #{sid}`\n"
                f"到期: {change_info}{extra_info}\n"
                f"时间: {bj_time}\n\n"
                f"HidenCloud Auto Renew"
            )
            send_tg_notification(tg_caption, photo_path=final_screenshot)
            print(f"[INFO] 🎉 任务完成 — {result_status}")
            return  # ✅ 正常退出

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
            wait_sec = attempt * 30  # 30s、60s 递增等待
            print(f"[INFO] ⏳ 等待 {wait_sec}s 后进行第 {attempt + 1} 次重试...")
            time.sleep(wait_sec)

    # 所有重试失败
    print(f"[ERROR] ❌ 所有 {MAX_RETRY} 次重试均失败，最后错误: {last_error}")
    send_tg_notification(
        f"❌ HidenCloud 续期失败（已重试 {MAX_RETRY} 次）\n"
        f"最后错误: {str(last_error)[:200]}"
    )
    raise last_error


if __name__ == "__main__":
    main()
