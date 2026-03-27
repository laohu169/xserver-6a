#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XServer GAME 自动登录和续期脚本
"""

import asyncio
import re
import datetime
from datetime import timezone, timedelta
import os
import requests
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async

# =====================================================================
#                          配置区域
# =====================================================================

IS_GITHUB_ACTIONS = os.getenv("GITHUB_ACTIONS") == "true"
USE_HEADLESS = IS_GITHUB_ACTIONS or os.getenv("USE_HEADLESS", "false").lower() == "true"
WAIT_TIMEOUT = 10000
PAGE_LOAD_DELAY = 3

PROXY_SERVER = os.getenv("PROXY_SERVER") or ""
USE_PROXY = bool(PROXY_SERVER)

LOGIN_EMAIL = os.getenv("XSERVER_EMAIL") or ""
LOGIN_PASSWORD = os.getenv("XSERVER_PASSWORD") or ""
TARGET_URL = "https://secure.xserver.ne.jp/xapanel/login/xmgame"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or ""
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or ""

PANEL_URL = "https://upp.bcbc.pp.ua/api/callback"
SERVER_NAME = "xserver"

# =====================================================================
#                        Telegram 推送模块
# =====================================================================

class TelegramNotifier:

    def __init__(self, bot_token=None, chat_id=None):
        self.bot_token = bot_token or TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id or TELEGRAM_CHAT_ID
        self.enabled = bool(self.bot_token and self.chat_id)
        if not self.enabled:
            print("ℹ️ Telegram 推送未启用(缺少 BOT_TOKEN 或 CHAT_ID)")

    def send_photo(self, photo_path, caption=None):
        if not self.enabled:
            return False
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendPhoto"
            with open(photo_path, 'rb') as f:
                files = {"photo": f}
                payload = {"chat_id": self.chat_id}
                if caption:
                    payload["caption"] = caption
                response = requests.post(url, data=payload, files=files, timeout=20)
                result = response.json()
                if result.get("ok"):
                    print(f"✅ Telegram 图片发送成功: {photo_path}")
                    return True
                else:
                    print(f"❌ Telegram 图片发送失败: {result.get('description')}")
                    return False
        except Exception as e:
            print(f"❌ Telegram 推送图片异常: {e}")
            return False

    def send_message(self, message, parse_mode="HTML"):
        if not self.enabled:
            print("⚠️ Telegram 推送未启用,跳过发送")
            return False
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": parse_mode
            }
            response = requests.post(url, json=payload, timeout=10)
            result = response.json()
            if result.get("ok"):
                print("✅ Telegram 消息发送成功")
                return True
            else:
                print(f"❌ Telegram 消息发送失败: {result.get('description')}")
                return False
        except Exception as e:
            print(f"❌ Telegram 推送异常: {e}")
            return False

    def send_renewal_result(self, status, old_time, new_time=None, run_time=None):
        beijing_time = datetime.datetime.now(timezone(timedelta(hours=8)))
        timestamp = run_time or beijing_time.strftime("%Y-%m-%d %H:%M:%S")
        message = f"<b>🎮 XServer GAME 续期通知</b>\n\n"
        message += f"🕐 运行时间: <code>{timestamp}</code>\n"
        message += f"🖥 服务器: <code>🇯🇵 Xserver(MC)</code>\n\n"
        if status == "Success":
            message += f"📊 续期结果: <b>✅ 成功</b>\n"
            message += f"🕛 旧到期: <code>{old_time}</code>\n"
            message += f"🕡 新到期: <code>{new_time}</code>\n"
        elif status == "Unexpired":
            message += f"📊 续期结果: <b>ℹ️ 未到期</b>\n"
            message += f"🕛 到期时间: <code>{old_time}</code>\n"
            message += f"💡 提示: 剩余时间超过24小时,无需续期\n"
        elif status == "Failed":
            message += f"📊 续期结果: <b>❌ 失败</b>\n"
            message += f"🕛 到期时间: <code>{old_time}</code>\n"
            message += f"⚠️ 请检查日志或手动续期\n"
        else:
            message += f"📊 续期结果: <b>❓ 未知</b>\n"
            message += f"🕛 到期时间: <code>{old_time}</code>\n"
        return self.send_message(message)


# =====================================================================
#                        XServer 自动登录类
# =====================================================================

class XServerAutoLogin:

    def __init__(self):
        self.browser = None
        self.context = None
        self.page = None
        self.headless = USE_HEADLESS
        self.email = LOGIN_EMAIL
        self.password = LOGIN_PASSWORD
        self.target_url = TARGET_URL
        self.wait_timeout = WAIT_TIMEOUT
        self.page_load_delay = PAGE_LOAD_DELAY
        self.screenshot_count = 0
        self.old_expiry_time = None
        self.new_expiry_time = None
        self.renewal_status = "Unknown"
        self.remaining_seconds = 0
        self.telegram = TelegramNotifier()

    def report_status(self, remaining_seconds):
        try:
            payload = {
                "server_name": SERVER_NAME,
                "remaining_time": remaining_seconds,
                "status": "up"
            }
            resp = requests.post(PANEL_URL, json=payload, timeout=10)
            print(f"✅ 上报成功: {resp.json()}")
        except Exception as e:
            print(f"❌ 上报失败: {e}")

    def parse_remaining_seconds(self, time_str):
        try:
            hours = 0
            minutes = 0
            h_match = re.search(r'(\d+)時間', time_str)
            if h_match:
                hours = int(h_match.group(1))
            m_match = re.search(r'(\d+)分', time_str)
            if m_match:
                minutes = int(m_match.group(1))
            return (hours * 3600) + (minutes * 60)
        except Exception as e:
            print(f"⚠️ 解析剩余秒数失败: {e}")
            return 0

    def format_remaining_time(self, raw_str):
        return raw_str.strip()

    def format_expiry_date(self, raw_str):
        return raw_str.strip()

    # =================================================================
    #                       1. 浏览器管理模块
    # =================================================================

    async def setup_browser(self):
        try:
            playwright = await async_playwright().start()
            browser_args = [
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--disable-notifications',
                '--window-size=1920,1080',
                '--lang=ja-JP',
                '--accept-lang=ja-JP,ja,en-US,en'
            ]
            if USE_PROXY and PROXY_SERVER:
                print(f"🌐 使用代理: {PROXY_SERVER}")
                browser_args.append(f'--proxy-server={PROXY_SERVER}')

            self.browser = await playwright.chromium.launch(
                headless=self.headless,
                args=browser_args
            )
            context_options = {
                'viewport': {'width': 1920, 'height': 1080},
                'locale': 'ja-JP',
                'timezone_id': 'Asia/Tokyo',
                'user_agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/120.0.0.0 Safari/537.36'
                )
            }
            if USE_PROXY and PROXY_SERVER:
                context_options['proxy'] = {'server': PROXY_SERVER}

            self.context = await self.browser.new_context(**context_options)
            self.page = await self.context.new_page()
            await stealth_async(self.page)
            print("✅ Stealth 插件已应用")
            print("✅ Playwright 浏览器初始化成功")
            return True
        except Exception as e:
            print(f"❌ Playwright 浏览器初始化失败: {e}")
            return False

    async def take_screenshot(self, step_name=""):
        try:
            if self.page:
                self.screenshot_count += 1
                beijing_time = datetime.datetime.now(timezone(timedelta(hours=8)))
                timestamp = beijing_time.strftime("%H%M%S")
                filename = f"step_{self.screenshot_count:02d}_{timestamp}_{step_name}.png"
                filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
                await self.page.screenshot(path=filename, full_page=True)
                print(f"📸 截图已保存: {filename}")
        except Exception as e:
            print(f"⚠️ 截图失败: {e}")

    def validate_config(self):
        if not self.email or not self.password:
            print("❌ 邮箱或密码未设置!")
            return False
        print("✅ 配置信息验证通过")
        return True

    async def cleanup(self):
        try:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            print("🧹 浏览器已关闭")
        except Exception as e:
            print(f"⚠️ 清理资源时出错: {e}")

    # =================================================================
    #                       2. 页面导航模块
    # =================================================================

    async def navigate_to_login(self):
        try:
            print(f"🌐 正在访问: {self.target_url}")
            await self.page.goto(self.target_url, wait_until='load')
            await self.page.wait_for_selector("body", timeout=self.wait_timeout)

            print("⏰ 等待页面完成跳转（最多15秒）...")
            for i in range(15):
                await asyncio.sleep(1)
                current_url = self.page.url
                try:
                    await self.page.wait_for_selector("input[name='memberid']", timeout=1000)
                    print(f"✅ 登录表单已出现 (耗时约 {i + 1} 秒)")
                    break
                except Exception:
                    print(f"   等待中... ({i + 1}s) URL: {current_url}")

            print("✅ 页面加载成功")
            await self.take_screenshot("login_page_loaded")
            return True
        except Exception as e:
            print(f"❌ 导航失败: {e}")
            return False

    # =================================================================
    #                       3. 登录表单处理模块
    # =================================================================

    async def find_login_form(self):
        try:
            print("🔍 正在查找登录表单...")
            email_selector = "input[name='memberid']"
            await self.page.wait_for_selector(email_selector, timeout=self.wait_timeout)
            print("✅ 找到邮箱输入框")

            password_selector = "input[name='user_password']"
            await self.page.wait_for_selector(password_selector, timeout=self.wait_timeout)
            print("✅ 找到密码输入框")

            login_button_selector = "input[value='ログインする']"
            await self.page.wait_for_selector(login_button_selector, timeout=self.wait_timeout)
            print("✅ 找到登录按钮")

            return email_selector, password_selector, login_button_selector
        except Exception as e:
            print(f"❌ 查找登录表单时出错: {e}")
            return None, None, None

    async def human_type(self, selector, text):
        for char in text:
            await self.page.type(selector, char, delay=100)
            await asyncio.sleep(0.05)

    async def perform_login(self):
        try:
            print("🎯 开始执行登录操作...")
            email_selector, password_selector, login_button_selector = await self.find_login_form()
            if not email_selector or not password_selector:
                return False

            print("📝 正在填写登录信息...")
            await self.page.fill(email_selector, "")
            await self.human_type(email_selector, self.email)
            print("✅ 邮箱已填写")
            await asyncio.sleep(2)

            await self.page.fill(password_selector, "")
            await self.human_type(password_selector, self.password)
            print("✅ 密码已填写")
            await asyncio.sleep(2)

            if login_button_selector:
                print("🖱️ 点击登录按钮...")
                await self.page.click(login_button_selector)
            else:
                print("⌨️ 使用回车键提交...")
                await self.page.press(password_selector, "Enter")

            # 等待跳离登录页和 loginauth 中间验证页，兼容各种跳转方式
            print("⏳ 等待登录跳转...")
            try:
                await self.page.wait_for_url(
                    lambda url: "login" not in url and "loginauth" not in url,
                    timeout=60000
                )
                print(f"✅ 页面已跳转: {self.page.url}")
            except Exception:
                print("⚠️ wait_for_url 超时，继续向下执行...")
                await asyncio.sleep(5)

            return True
        except Exception as e:
            print(f"❌ 登录操作失败: {e}")
            return False

    # =================================================================
    #                       4. 登录结果处理模块
    # =================================================================

    async def handle_login_result(self):
        try:
            print("🔍 正在检查登录结果...")
            await asyncio.sleep(2)

            # 处理 loginauth 中间跳转页，等待其自动跳转完成（最多20秒）
            for i in range(20):
                current_url = self.page.url
                if "loginauth" in current_url:
                    print(f"🔄 检测到中间验证页，等待自动跳转... ({i + 1}s) URL: {current_url}")
                    await asyncio.sleep(1)
                else:
                    break

            current_url = self.page.url
            print(f"🔍 当前URL: {current_url}")

            success_url = "https://secure.xserver.ne.jp/xapanel/xmgame/index"

            if current_url == success_url or success_url in current_url:
                print("✅ 登录成功!已跳转到XServer GAME管理页面")
                await asyncio.sleep(3)

                print("🔍 正在查找ゲーム管理按钮...")
                try:
                    game_button_selector = "a:has-text('ゲーム管理')"
                    await self.page.wait_for_selector(game_button_selector, timeout=self.wait_timeout)
                    print("✅ 找到ゲーム管理按钮")

                    await self.page.click(game_button_selector)
                    print("✅ 已点击ゲーム管理按钮")
                    await asyncio.sleep(3)

                    current_url = self.page.url
                    if "jumpvps" in current_url:
                        print("🔄 检测到中间跳转页面 (jumpvps)，等待最终跳转...")
                        for i in range(15):
                            await asyncio.sleep(1)
                            final_url = self.page.url
                            if "xmgame/game/index" in final_url:
                                print(f"✅ 成功跳转到游戏管理页面 (耗时 {i + 1} 秒)")
                                break
                            if i == 14:
                                print("⚠️ 等待跳转超时，继续执行...")
                    else:
                        await asyncio.sleep(3)

                    final_url = self.page.url
                    print(f"🔍 最终页面URL: {final_url}")

                    expected_game_url = "https://secure.xserver.ne.jp/xmgame/game/index"
                    if expected_game_url in final_url:
                        print("✅ 成功到达游戏管理页面")
                        await self.take_screenshot("game_page_loaded")
                        await self.get_server_time_info()
                        await self.click_upgrade_button()
                    else:
                        print(f"⚠️ 当前URL不是预期的游戏管理页面，尝试继续执行...")
                        await self.take_screenshot("game_page_unexpected_url")
                        await self.get_server_time_info()
                        await self.click_upgrade_button()

                except Exception as e:
                    print(f"❌ 查找或点击ゲーム管理按钮时出错: {e}")
                    await self.take_screenshot("game_button_error")

                return True
            else:
                print(f"❌ 登录失败!")
                print(f"   预期URL: {success_url}")
                print(f"   实际URL: {current_url}")
                return False

        except Exception as e:
            print(f"❌ 检查登录结果时出错: {e}")
            return False

    # =================================================================
    #                    5A. 服务器信息获取模块
    # =================================================================

    async def get_server_time_info(self):
        try:
            print("🕒 正在获取服务器时间信息...")
            await asyncio.sleep(3)

            elements = await self.page.locator("text=/残り\\d+時間\\d+分/").all()

            for element in elements:
                element_text = await element.text_content()
                element_text = element_text.strip() if element_text else ""

                if element_text and len(element_text) < 200 and "残り" in element_text and "時間" in element_text:
                    print(f"✅ 找到时间元素: {element_text}")

                    remaining_match = re.search(r'残り(\d+時間\d+分)', element_text)
                    if remaining_match:
                        remaining_raw = remaining_match.group(1)
                        remaining_formatted = self.format_remaining_time(remaining_raw)
                        print(f"⏰ 剩余时间: {remaining_formatted}")
                        self.remaining_seconds = self.parse_remaining_seconds(remaining_formatted)

                    expiry_match = re.search(r'\((\d{4}-\d{2}-\d{2}[^)]*)まで\)', element_text)
                    if expiry_match:
                        expiry_raw = expiry_match.group(1).strip()
                        expiry_formatted = self.format_expiry_date(expiry_raw)
                        print(f"📅 到期时间: {expiry_formatted}")
                        self.old_expiry_time = expiry_formatted
                    break

            if not self.old_expiry_time:
                print("⚠️ 未能通过正则匹配获取到期时间，尝试备用方案...")
                page_text = await self.page.inner_text("body")
                expiry_match = re.search(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})', page_text)
                if expiry_match:
                    self.old_expiry_time = expiry_match.group(1)
                    print(f"📅 备用方案获取到期时间: {self.old_expiry_time}")
                else:
                    self.old_expiry_time = "未知"
                    print("⚠️ 无法获取到期时间，设为未知")

            self.report_status(self.remaining_seconds)

        except Exception as e:
            print(f"❌ 获取服务器时间信息时出错: {e}")
            self.old_expiry_time = "未知"

    # =================================================================
    #                    5B. 续期按钮点击模块
    # =================================================================

    async def click_upgrade_button(self):
        try:
            print("🔘 正在查找续期按钮...")
            await asyncio.sleep(2)

            # 判断是否需要续期（剩余时间超过24小时则跳过）
            if self.remaining_seconds > 86400:
                hours_left = self.remaining_seconds // 3600
                print(f"ℹ️ 剩余时间 {hours_left} 小时，超过24小时，无需续期")
                self.renewal_status = "Unexpired"
                return

            button_selectors = [
                "input[value='無料延長する']",
                "button:has-text('無料延長')",
                "a:has-text('無料延長')",
                "input[type='submit']",
            ]

            clicked = False
            for selector in button_selectors:
                try:
                    await self.page.wait_for_selector(selector, timeout=3000)
                    await self.page.click(selector)
                    print(f"✅ 点击续期按钮成功: {selector}")
                    clicked = True
                    break
                except Exception:
                    continue

            if not clicked:
                print("❌ 未找到续期按钮")
                self.renewal_status = "Failed"
                await self.take_screenshot("no_renewal_button")
                return

            await asyncio.sleep(5)
            await self.take_screenshot("after_renewal_click")

            # 获取续期后的新到期时间
            try:
                page_text = await self.page.inner_text("body")
                expiry_match = re.search(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})', page_text)
                if expiry_match:
                    self.new_expiry_time = expiry_match.group(1)
                    print(f"📅 新到期时间: {self.new_expiry_time}")
                else:
                    self.new_expiry_time = "未知"
            except Exception:
                self.new_expiry_time = "未知"

            self.renewal_status = "Success"
            print("✅ 续期操作完成")

        except Exception as e:
            print(f"❌ 续期按钮点击失败: {e}")
            self.renewal_status = "Failed"

    # =================================================================
    #                       6. 主流程模块
    # =================================================================

    async def run(self):
        beijing_time = datetime.datetime.now(timezone(timedelta(hours=8)))
        run_time = beijing_time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"🚀 XServer GAME 自动续期脚本启动 [{run_time}]")

        try:
            if not self.validate_config():
                return False

            if not await self.setup_browser():
                return False

            if not await self.navigate_to_login():
                await self.cleanup()
                return False

            if not await self.perform_login():
                await self.take_screenshot("login_failed")
                await self.cleanup()
                return False

            await self.take_screenshot("after_login")

            result = await self.handle_login_result()

            await self.take_screenshot("final_state")

            print(f"\n{'='*50}")
            print(f"📊 执行结果汇总")
            print(f"{'='*50}")
            print(f"🕐 运行时间 : {run_time}")
            print(f"📋 续期状态 : {self.renewal_status}")
            print(f"🕛 旧到期   : {self.old_expiry_time}")
            print(f"🕡 新到期   : {self.new_expiry_time}")
            print(f"{'='*50}\n")

            self.telegram.send_renewal_result(
                status=self.renewal_status,
                old_time=self.old_expiry_time,
                new_time=self.new_expiry_time,
                run_time=run_time
            )

            return result

        except Exception as e:
            print(f"❌ 运行时发生未预期错误: {e}")
            await self.take_screenshot("unexpected_error")
            return False
        finally:
            await self.cleanup()


# =====================================================================
#                            入口
# =====================================================================

async def main():
    bot = XServerAutoLogin()
    success = await bot.run()
    if success:
        print("✅ 脚本执行成功")
    else:
        print("❌ 脚本执行失败")


if __name__ == "__main__":
    asyncio.run(main())
