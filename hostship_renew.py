import json
import os
import re
import time
try:
    # patchright 是 playwright 的直接替代品，修补了 CDP 自动化特征泄露，更容易通过 Turnstile
    from patchright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    USING_PATCHRIGHT = True
except ImportError:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    USING_PATCHRIGHT = False

from proxy_utils import start_proxy

PANEL_URL = "https://panel.host-ship.com/"
LOGIN_URL = PANEL_URL  # 面板首页会自动展示登录表单（如未跳转，改成 https://panel.host-ship.com/login）
SERVER_LIST_URL = "https://panel.host-ship.com/server"

# 脚本自己的续期阈值：剩余天数低于该值才续期。
# 面板规则是 "低于 14 天才允许续期，续期后最多补到 14 天"，所以天数越低一次补得越多。
# 默认 7：等剩余不足 7 天再续，每次能补 7 天左右（比 13 天就续、只补 1 天要省事得多）。
# 可用环境变量 RENEW_THRESHOLD_DAYS 调整，最大有效值为 14。
RENEW_THRESHOLD_DAYS = min(int(os.environ.get("RENEW_THRESHOLD_DAYS", "7")), 14)


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


def wait_turnstile_token(page, timeout=45):
    """
    等待登录页内嵌的 Turnstile 自动通过（出现非空 token）。没有该组件则直接返回 True。
    前 12 秒只等待不点击（托管模式通常会自动通过，乱点反而容易被判为机器人），之后才尝试点击。
    """
    sel = 'iframe[src*="challenges.cloudflare.com"], input[name="cf-turnstile-response"]'
    if page.locator(sel).count() == 0:
        return True
    log("登录页包含 Turnstile 组件，等待其自动通过...")
    start = time.time()
    last_click = 0
    while time.time() - start < timeout:
        try:
            token = page.locator('input[name="cf-turnstile-response"]')
            if token.count() > 0 and token.first.input_value():
                log(f"Turnstile 已通过（用时 {time.time() - start:.0f}s）")
                return True
        except Exception:
            pass
        if time.time() - start > 12 and time.time() - last_click > 8 and _click_turnstile(page):
            last_click = time.time()
        page.wait_for_timeout(1000)
    return False


def probe_exit(browser):
    """
    用一个临时上下文访问 IP 查询接口，返回 {"ip":..., "tz":...}。
    ip 用来确认代理生效；tz 是出口 IP 所在时区，后面用来设置浏览器时区，
    避免“浏览器是 UTC、IP 却在别的国家”这种 Turnstile 会检查的不一致。
    失败返回 None。
    """
    ctx = browser.new_context()
    try:
        page = ctx.new_page()
        try:
            page.goto("https://ipwho.is/", timeout=20000)
            d = json.loads(page.inner_text("body", timeout=5000))
            if d.get("success", True) and d.get("ip"):
                tz = (d.get("timezone") or {}).get("id")
                log(f"当前浏览器出口 IP: {d['ip']}  ({d.get('country', '?')}, 时区 {tz})")
                return {"ip": d["ip"], "tz": tz}
        except Exception:
            pass
        try:
            page.goto("https://api.ipify.org", timeout=20000)
            ip = page.inner_text("body", timeout=5000).strip()
            if re.fullmatch(r"[0-9a-fA-F:.]{7,45}", ip):
                log(f"当前浏览器出口 IP: {ip}")
                return {"ip": ip, "tz": None}
        except Exception:
            pass
        warn("无法获取出口 IP（代理可能不通或查询接口被拦截）")
        return None
    finally:
        ctx.close()


def login(page, username, password, max_attempts=3):
    """
    登录面板。选择器根据实际截图（Host Ship Free panel 登录页）确认：
    - 用户名/邮箱输入框：placeholder = "Username or Email"
    - 密码输入框：placeholder = "Password"
    - 登录按钮：Turnstile 通过前文字是 "Complete the security check"（禁用），通过后变为 "Sign In"

    Turnstile 偶发 "Verification failed" 时会刷新页面重试，最多 max_attempts 次。
    """
    passed = False
    for attempt in range(1, max_attempts + 1):
        log(f"正在打开登录页...（第 {attempt}/{max_attempts} 次）")
        goto_safe(page, LOGIN_URL)
        page.wait_for_timeout(2000)

        username_input = page.get_by_placeholder("Username or Email")
        password_input = page.get_by_placeholder("Password")
        if username_input.count() == 0:
            raise RuntimeError('未找到 placeholder="Username or Email" 的输入框，页面结构可能已变化')
        if password_input.count() == 0:
            raise RuntimeError('未找到 placeholder="Password" 的输入框，页面结构可能已变化')

        # 像真人一样逐字输入，而不是瞬间填充
        username_input.first.click()
        username_input.first.press_sequentially(username, delay=60)
        password_input.first.click()
        password_input.first.press_sequentially(password, delay=60)
        log("已填入用户名和密码")

        if wait_turnstile_token(page):
            passed = True
            break
        warn("Turnstile 未通过（可能显示 Verification failed），刷新页面重试")
        try:
            page.screenshot(path=f"turnstile_failed_{attempt}.png", full_page=True)
        except Exception:
            pass
        page.wait_for_timeout(3000)

    if not passed:
        raise RuntimeError("Turnstile 多次验证失败：出口 IP 或浏览器指纹被 Cloudflare 拒绝，请更换代理节点或查看 turnstile_failed_*.png")

    # Turnstile 通过后按钮会启用；用提交按钮兜底，避免依赖按钮文字
    sign_in_btn = page.get_by_role("button", name="Sign In")
    if sign_in_btn.count() == 0:
        sign_in_btn = page.locator('button:has-text("Sign In"), form button[type="submit"], button[type="submit"]')
    if sign_in_btn.count() == 0:
        raise RuntimeError('未找到登录按钮，页面结构可能已变化')

    sign_in_btn.first.click()
    log("已点击登录按钮")

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


def _body_text(page):
    try:
        return page.inner_text("body", timeout=5000)
    except Exception:
        return ""


def get_renewal_days_remaining(page):
    """
    读取服务器剩余天数。按可靠程度依次尝试：
    1. 右侧 "EXPIRES 4 NOV / 10 Days" 卡片（实际页面结构）
    2. "Renewal in X Days" / "Current renewal in: X days"
    3. 页面上第一个 "X Days"
    读取不到返回 None。
    """
    text = _body_text(page)
    patterns = [
        r'Expires[^\n]*\n\s*(\d+)\s*Days?',
        r'Renewal\s+in:?\s*(\d+)\s*Days?',
        r'(\d+)\s*Days?',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return None


def get_not_yet_message(page):
    """
    读取面板的"还不能续期"提示，例如
    "You can renew again tomorrow, when it drops below 14 days."
    没有该提示返回 None。
    """
    text = _body_text(page)
    m = re.search(r'You can renew again[^\n.]*\.?', text, re.IGNORECASE)
    return m.group(0).strip() if m else None


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
        log(f"距离到期还剩 {days_left} 天（续期条件：低于 {RENEW_THRESHOLD_DAYS} 天）")
    else:
        warn("未能读取到剩余天数，继续尝试查找续期按钮")

    # 还没到续期窗口：面板会给出 "You can renew again ..." 提示，或剩余天数 >= 阈值
    not_yet = get_not_yet_message(page)
    if not_yet:
        log(f"面板提示暂不可续期: {not_yet}，跳过")
        return "skipped"
    if days_left is not None and days_left >= RENEW_THRESHOLD_DAYS:
        log(f"剩余 {days_left} 天 ≥ {RENEW_THRESHOLD_DAYS} 天，未到续期窗口，跳过")
        return "skipped"

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
        return "failed"

    try:
        if target_btn.is_disabled():
            log(f"续期按钮当前不可点击（可能还没到续期窗口期，剩余 {days_left if days_left is not None else '未知'} 天），跳过")
            return "skipped"
    except Exception:
        # 部分自定义组件不是原生 <button disabled>，is_disabled 可能取不到，忽略继续尝试点击
        pass

    target_btn.scroll_into_view_if_needed()
    target_btn.click()
    log(f"点击了续期按钮 (文字: {matched_text})")
    page.wait_for_timeout(2500)

    # 点击 Renew 后会弹出确认框："This adds 4 days ... [Cancel] [Add 4 days]"，
    # 必须再点 "Add N days" 才会真正续期。
    confirm_btn = page.get_by_role("button", name=re.compile(r"^\s*Add\s+\d+\s+days?\s*$", re.IGNORECASE))
    try:
        confirm_btn.first.wait_for(state="visible", timeout=8000)
    except PlaywrightTimeout:
        confirm_btn = None

    if confirm_btn is not None:
        try:
            info = page.get_by_text(re.compile(r"This adds\s+\d+\s+days?", re.IGNORECASE)).first.inner_text(timeout=2000)
            log(f"续期确认框: {info.strip()}")
        except Exception:
            pass
        btn_text = confirm_btn.first.inner_text().strip()
        confirm_btn.first.click()
        log(f"点击了确认按钮 ({btn_text})")
        page.wait_for_timeout(3000)
    else:
        # 兜底：兼容其他面板常见的确认按钮文字
        for c_text in ["Renew now", "Confirm", "确认"]:
            fallback = page.get_by_role("button", name=c_text)
            if fallback.count() > 0 and fallback.first.is_visible():
                fallback.first.click()
                log(f"点击了确认按钮 (文字: {c_text})")
                page.wait_for_timeout(3000)
                break
        else:
            warn("点击 Renew 后未出现确认弹窗，可能面板直接续期，或页面结构已变化")

    # 不确定面板是用弹窗/toast/静默拒绝哪种方式反馈结果，
    # 统一截图留证，方便人工确认点击后到底发生了什么（成功提示/报错提示/无变化）。
    try:
        page.screenshot(path="renew_result.png", full_page=True)
        log("已保存续期点击后的截图: renew_result.png（会作为 Actions Artifact 上传）")
    except Exception as e:
        warn(f"截图保存失败: {e}")

    # 点击后重新加载页面再读天数，避免读到旧的前端状态
    try:
        page.reload(timeout=60000)
        page.wait_for_timeout(3000)
        wait_for_cloudflare(page)
    except Exception as e:
        warn(f"刷新页面失败: {e}")

    days_left_after = get_renewal_days_remaining(page)
    if days_left_after is not None and days_left is not None:
        if days_left_after > days_left:
            log(f"剩余天数已从 {days_left} 变为 {days_left_after}，续期确认生效")
            return "renewed"
        warn(f"点击后剩余天数仍是 {days_left_after} 天，未增加，续期可能未被面板接受（请查看 renew_result.png 截图确认具体提示）")
        return "failed"

    log("无法对比续期前后天数，已点击续期按钮，请查看 renew_result.png 确认")
    return "renewed"


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
        # 去掉 --enable-automation，并关闭 AutomationControlled 特征；不再手动改 UA / webdriver
        "ignore_default_args": ["--enable-automation"],
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if proxy_handle:
        launch_kwargs["proxy"] = proxy_handle.playwright_proxy

    log(f"自动化引擎: {'patchright' if USING_PATCHRIGHT else 'playwright（建议安装 patchright 以提高过盾率）'}")

    # 优先使用真实 Chrome（指纹比 Playwright 自带 Chromium 更自然），不可用时回退
    browser = None
    for channel in ([os.environ.get("BROWSER_CHANNEL", "chrome")] if os.environ.get("BROWSER_CHANNEL", "chrome") else []) + [None]:
        try:
            kw = dict(launch_kwargs)
            if channel:
                kw["channel"] = channel
            browser = playwright.chromium.launch(**kw)
            log(f"浏览器已启动: {channel or '内置 Chromium'}")
            break
        except Exception as e:
            warn(f"启动 {channel or 'Chromium'} 失败: {str(e).splitlines()[0]}")
    if browser is None:
        err("无法启动浏览器")
        if proxy_handle:
            proxy_handle.stop()
        return

    try:
        exit_info = None
        if proxy_handle:
            exit_info = probe_exit(browser)
            if not exit_info:
                err("已配置代理但无法通过代理访问外网，流程终止（请检查 PROXY_URL 节点是否可用）")
                return

        ctx_kwargs = {"no_viewport": True}   # 不覆盖 UA / 视口，保持真实浏览器指纹
        if exit_info and exit_info.get("tz"):
            ctx_kwargs["timezone_id"] = exit_info["tz"]   # 时区与出口 IP 保持一致
        context = browser.new_context(**ctx_kwargs)
        page = context.new_page()

        login(page, username, password)

        hrefs = find_server_links(page)
        if not hrefs:
            err("未找到任何服务器，流程终止")
            return

        stats = {"renewed": 0, "skipped": 0, "failed": 0}
        for idx, href in enumerate(hrefs):
            log(f"--- 处理第 {idx + 1}/{len(hrefs)} 个服务器 ---")
            try:
                stats[renew_server(page, href)] += 1
            except PlaywrightTimeout:
                warn(f"处理 {href} 超时，跳过")
                stats["failed"] += 1
            except Exception as e:
                warn(f"处理 {href} 时出错: {e}")
                stats["failed"] += 1

        log(f"全部处理完成：续期成功 {stats['renewed']} 个，未到期跳过 {stats['skipped']} 个，"
            f"失败 {stats['failed']} 个（共 {len(hrefs)} 个）")

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
