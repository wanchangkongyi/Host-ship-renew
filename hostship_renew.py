import os
import re
import time
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from proxy_utils import start_proxy

PANEL_URL = "https://panel.host-ship.com/"
LOGIN_URL = PANEL_URL  # 面板首页会自动展示登录表单（如未跳转，改成 https://panel.host-ship.com/login）
SERVER_LIST_URL = "https://panel.host-ship.com/server"


def log(msg):
    print(f"[INFO] {msg}")


def warn(msg):
    print(f"[WARN] {msg}")


def err(msg):
    print(f"[ERROR] {msg}")


# ----------------------------------------------------------------------------
# Cloudflare 过盾相关
# ----------------------------------------------------------------------------
CF_TITLE_HINTS = ("just a moment", "attention required", "请稍候", "执行安全验证")


def _cf_challenge_active(page):
    """判断当前是否停留在 Cloudflare 全页挑战（Just a moment...）"""
    try:
        title = (page.title() or "").lower()
    except Exception:
        return False
    if any(h in title for h in CF_TITLE_HINTS):
        return True
    try:
        return page.locator("#challenge-running, #challenge-form, .cf-browser-verification").count() > 0
    except Exception:
        return False


def _click_turnstile(page):
    """尝试点击 Turnstile 复选框（best effort）。点到返回 True"""
    for frame in page.frames:
        if "challenges.cloudflare.com" not in (frame.url or ""):
            continue
        try:
            box = frame.frame_element().bounding_box()
            if not box or box["width"] < 100:
                continue
            # 复选框位于 iframe 左侧，垂直居中
            page.mouse.click(box["x"] + 28, box["y"] + box["height"] / 2)
            log("已尝试点击 Cloudflare Turnstile 复选框")
            return True
        except Exception:
            continue
    return False


def wait_for_cloudflare(page, timeout=60):
    """
    等待 Cloudflare 挑战通过。没有挑战则立即返回 True；超时返回 False。
    期间如果出现 Turnstile 复选框，会尝试点击。
    """
    if not _cf_challenge_active(page):
        return True
    log("检测到 Cloudflare 验证页，等待通过...")
    end = time.time() + timeout
    clicked_at = 0
    while time.time() < end:
        page.wait_for_timeout(2000)
        if not _cf_challenge_active(page):
            log("Cloudflare 验证已通过")
            return True
        if time.time() - clicked_at > 8 and _click_turnstile(page):
            clicked_at = time.time()
    warn("等待 Cloudflare 验证超时（可能 IP 信誉差，建议换一个代理节点）")
    try:
        page.screenshot(path="cf_blocked.png", full_page=True)
    except Exception:
        pass
    return False


def goto_safe(page, url, timeout=60000):
    """page.goto + 自动等待 Cloudflare 挑战"""
    page.goto(url, timeout=timeout)
    wait_for_cloudflare(page)


def solve_embedded_turnstile(page, timeout=20):
    """登录表单内嵌 Turnstile 小组件时，点击并等待其生成 token（没有该组件则直接返回）"""
    if page.locator('iframe[src*="challenges.cloudflare.com"], input[name="cf-turnstile-response"]').count() == 0:
        return
    log("登录页包含 Turnstile 组件，等待其完成...")
    end = time.time() + timeout
    last_click = 0
    while time.time() < end:
        try:
            token = page.locator('input[name="cf-turnstile-response"]')
            if token.count() > 0 and token.first.input_value():
                log("Turnstile 已通过")
                return
        except Exception:
            pass
        if time.time() - last_click > 6 and _click_turnstile(page):
            last_click = time.time()
        page.wait_for_timeout(1500)
    warn("Turnstile 未在限定时间内完成，仍尝试继续登录")


def log_exit_ip(page):
    """通过浏览器访问 IP 查询接口，确认出口 IP（用于验证代理是否生效）"""
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            page.goto(url, timeout=20000)
            ip = page.inner_text("body", timeout=5000).strip()
            if re.fullmatch(r"[0-9a-fA-F:.]{7,45}", ip):
                log(f"当前浏览器出口 IP: {ip}")
                return ip
        except Exception:
            continue
    warn("无法获取出口 IP（代理可能不通或查询接口被拦截）")
    return None


def login(page, username, password):
    """
    登录面板。选择器根据实际截图（Host Ship Free panel 登录页）确认：
    - 用户名/邮箱输入框：placeholder = "Username or Email"
    - 密码输入框：placeholder = "Password"
    - 登录按钮：文字 = "Sign In"
    """
    log("正在打开登录页...")
    goto_safe(page, LOGIN_URL)
    page.wait_for_timeout(2000)

    username_input = page.get_by_placeholder("Username or Email")
    password_input = page.get_by_placeholder("Password")

    if username_input.count() == 0:
        raise RuntimeError('未找到 placeholder="Username or Email" 的输入框，页面结构可能已变化')
    if password_input.count() == 0:
        raise RuntimeError('未找到 placeholder="Password" 的输入框，页面结构可能已变化')

    username_input.first.fill(username)
    password_input.first.fill(password)
    log("已填入用户名和密码")

    solve_embedded_turnstile(page)

    sign_in_btn = page.get_by_role("button", name="Sign In")
    if sign_in_btn.count() == 0:
        # 兜底：按纯文字匹配（get_by_role 依赖无障碍标签，个别情况下可能取不到）
        sign_in_btn = page.locator('button:has-text("Sign In")')
    if sign_in_btn.count() == 0:
        raise RuntimeError('未找到 "Sign In" 按钮，页面结构可能已变化')

    sign_in_btn.first.click()
    log("已点击 Sign In")

    page.wait_for_timeout(3000)
    wait_for_cloudflare(page)

    # 这个面板未登录/已登录都停留在同一个 URL（未登录显示登录表单，登录后显示 Dashboard），
    # 所以不能用"URL 是否变化"来判断，改用"登录表单（密码框）是否还存在"来判断。
    if page.get_by_placeholder("Password").count() > 0:
        screenshot_path = "login_failed.png"
        try:
            page.screenshot(path=screenshot_path, full_page=True)
            err(f"已保存失败截图: {screenshot_path}（会作为 Actions Artifact 上传，可下载查看）")
        except Exception as e:
            warn(f"截图保存失败: {e}")
        raise RuntimeError("登录后仍能看到密码输入框，可能账号密码错误，或页面上出现了验证码/人机校验等未处理的提示")

    log(f"登录成功，当前 URL: {page.url}")


def find_server_links(page):
    """
    收集所有服务器详情页链接。
    根据实际截图，登录成功后停留的首页（Dashboard）本身就直接展示了服务器卡片
    （"Manage Server" 按钮），/server 这个路径不一定是独立的列表页，
    所以优先在当前页（Dashboard）查找，找不到再尝试单独访问 /server 兜底。
    """
    manage_links = page.locator('a:has-text("Manage Server")')
    count = manage_links.count()
    if count > 0:
        hrefs = []
        for i in range(count):
            href = manage_links.nth(i).get_attribute("href")
            if href and href not in hrefs:
                hrefs.append(href)
        log(f"在当前页（Dashboard）通过 'Manage Server' 按钮找到 {len(hrefs)} 个服务器")
        return hrefs

    log(f"当前页未找到服务器卡片，尝试访问服务器列表页: {SERVER_LIST_URL}")
    goto_safe(page, SERVER_LIST_URL)
    page.wait_for_timeout(3000)

    manage_links = page.locator('a:has-text("Manage Server")')
    count = manage_links.count()
    if count > 0:
        hrefs = []
        for i in range(count):
            href = manage_links.nth(i).get_attribute("href")
            if href and href not in hrefs:
                hrefs.append(href)
        log(f"通过 'Manage Server' 按钮找到 {len(hrefs)} 个服务器")
        return hrefs

    # 兜底：如果按钮不是 <a> 而是别的标签，尝试常见 href 特征
    candidate_selectors = [
        'a[href*="/server/"]',
        'a[href*="/servers/"]',
        'a[href*="/service/"]',
        'a[href*="/services/"]',
    ]
    for sel in candidate_selectors:
        links = page.locator(sel)
        count = links.count()
        if count > 0:
            log(f"使用选择器 '{sel}' 找到 {count} 个服务器链接")
            hrefs = []
            for i in range(count):
                href = links.nth(i).get_attribute("href")
                if href and href not in hrefs:
                    hrefs.append(href)
            return hrefs
    warn("未匹配到任何服务器链接选择器，请检查页面结构并更新 candidate_selectors")
    return []


def get_renewal_days_remaining(page):
    """
    读取页面上 "Renewal in X Days" 卡片的剩余天数。
    读取不到时返回 None（不影响后续续期尝试，只影响日志展示）。
    """
    try:
        el = page.locator('text=/\\d+\\s*Days?/i').first
        if el.count() > 0:
            text = el.inner_text(timeout=3000)
            m = re.search(r'(\d+)\s*Days?', text, re.IGNORECASE)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return None


def renew_server(page, url):
    """
    进入单个服务器详情页并点击续期。
    该面板的续期是以"天"为单位的倒计时（Renewal in X Days），
    临近到期前 Renew 按钮才可点击，过早点击时按钮可能是禁用状态，
    这里做了 disabled 判断，禁用状态视为正常（还没到续期窗口），而不是报错。
    """
    full_url = url if url.startswith("http") else f"{PANEL_URL.rstrip('/')}{url}"
    log(f"打开服务器详情页: {full_url}")
    goto_safe(page, full_url)
    page.wait_for_timeout(3000)

    days_left = get_renewal_days_remaining(page)
    if days_left is not None:
        log(f"距离到期还剩 {days_left} 天")
    else:
        warn("未能读取到剩余天数，继续尝试查找续期按钮")

    renew_btn_texts = ["Renew", "续期", "Renouveler"]
    target_btn = None
    matched_text = None
    for text in renew_btn_texts:
        btn = page.locator(f'button:has-text("{text}")')
        if btn.count() > 0 and btn.first.is_visible():
            target_btn = btn.first
            matched_text = text
            break

    if target_btn is None:
        warn(f"未在 {full_url} 找到续期按钮，请检查 renew_btn_texts 或页面结构")
        return False

    try:
        if target_btn.is_disabled():
            log(f"续期按钮当前不可点击（可能还没到续期窗口期，剩余 {days_left if days_left is not None else '未知'} 天），跳过")
            return False
    except Exception:
        # 部分自定义组件不是原生 <button disabled>，is_disabled 可能取不到，忽略继续尝试点击
        pass

    target_btn.scroll_into_view_if_needed()
    target_btn.click()
    log(f"点击了续期按钮 (文字: {matched_text})")
    page.wait_for_timeout(2500)

    # 有些面板续期需要二次确认弹窗
    confirm_texts = ["Renew now", "Confirm", "确认", "Yes", "OK"]
    for c_text in confirm_texts:
        confirm_btn = page.locator(f'button:has-text("{c_text}")')
        if confirm_btn.count() > 0 and confirm_btn.first.is_visible(timeout=2000):
            confirm_btn.first.click()
            log(f"点击了确认按钮 (文字: {c_text})")
            page.wait_for_timeout(2000)
            break

    # 不确定面板是用弹窗/toast/静默拒绝哪种方式反馈结果，
    # 统一截图留证，方便人工确认点击后到底发生了什么（成功提示/报错提示/无变化）。
    try:
        page.screenshot(path="renew_result.png", full_page=True)
        log("已保存续期点击后的截图: renew_result.png（会作为 Actions Artifact 上传）")
    except Exception as e:
        warn(f"截图保存失败: {e}")

    days_left_after = get_renewal_days_remaining(page)
    if days_left_after is not None and days_left is not None:
        if days_left_after != days_left:
            log(f"剩余天数已从 {days_left} 变为 {days_left_after}，续期确认生效")
        else:
            warn(f"点击后剩余天数仍是 {days_left_after} 天，未发生变化，续期可能未被面板接受（请查看 renew_result.png 截图确认具体提示）")

    return True


def run(playwright):
    username = os.environ.get("PANEL_USERNAME", "").strip()
    password = os.environ.get("PANEL_PASSWORD", "").strip()

    if not username or not password:
        err("未设置 PANEL_USERNAME 或 PANEL_PASSWORD 环境变量")
        return

    # ---- 代理（可选）：环境变量 PROXY_URL，支持 vless/vmess/trojan/ss/hysteria2/socks5/http ----
    proxy_handle = None
    proxy_url = os.environ.get("PROXY_URL", "").strip()
    if proxy_url:
        try:
            proxy_handle = start_proxy(proxy_url, log=log)
        except Exception as e:
            err(f"代理启动失败: {e}")
            return
    else:
        log("未设置 PROXY_URL，将直连访问（如被 Cloudflare 拦截，请配置代理）")

    # 在 CI 里建议配合 xvfb-run 使用有头模式（HEADLESS=false），更容易通过 Cloudflare
    headless = os.environ.get("HEADLESS", "true").strip().lower() not in ("0", "false", "no")

    launch_kwargs = {
        "headless": headless,
        "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    }
    if proxy_handle:
        launch_kwargs["proxy"] = proxy_handle.playwright_proxy

    browser = playwright.chromium.launch(**launch_kwargs)
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        viewport={"width": 1366, "height": 768},
        locale="en-US",
    )
    context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
    page = context.new_page()

    try:
        if proxy_handle:
            if not log_exit_ip(page):
                err("已配置代理但无法通过代理访问外网，流程终止（请检查 PROXY_URL 节点是否可用）")
                return

        login(page, username, password)

        hrefs = find_server_links(page)
        if not hrefs:
            err("未找到任何服务器，流程终止")
            return

        success_count = 0
        for idx, href in enumerate(hrefs):
            log(f"--- 处理第 {idx + 1}/{len(hrefs)} 个服务器 ---")
            try:
                if renew_server(page, href):
                    success_count += 1
            except PlaywrightTimeout:
                warn(f"处理 {href} 超时，跳过")
            except Exception as e:
                warn(f"处理 {href} 时出错: {e}")

        log(f"全部处理完成，成功续期 {success_count}/{len(hrefs)} 个服务器")

    except Exception as e:
        err(f"执行过程中发生错误: {e}")
        try:
            page.screenshot(path="error.png", full_page=True)
            err("已保存错误截图: error.png（会作为 Actions Artifact 上传，可下载查看）")
        except Exception as shot_err:
            warn(f"错误截图保存失败: {shot_err}")
    finally:
        browser.close()
        if proxy_handle:
            proxy_handle.stop()


with sync_playwright() as playwright:
    run(playwright)
