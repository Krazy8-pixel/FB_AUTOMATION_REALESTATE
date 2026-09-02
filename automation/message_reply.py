import os
import time
import json
import re
import pickle
import random
import threading
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    InvalidSessionIdException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from automation.browser_settings import apply_background_mode_option
from automation.chromedriver import build_chrome_driver
from automation.runtime_paths import auto_reply_log_dir, sessions_dir
from automation.email_settings import get_smtp_receiver_email

# ------------------------
# Directories
# ------------------------
SESSION_DIR = sessions_dir()
AUTO_REPLY_LOG_DIR = auto_reply_log_dir()

# Fill these values before using SMTP email sending.
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_SENDER_EMAIL = "marketplace.agent11@gmail.com"
SMTP_SENDER_PASSWORD = "hnea qdqj hetz luof"
SPAM_CYCLE_SECONDS = 150


def _is_invalid_session_error(exc):
    return isinstance(exc, InvalidSessionIdException) or "invalid session id" in str(exc or "").lower()


def _apply_browser_safety_patches(driver):
    """
    Best-effort browser normalization. Non-fatal by design.
    """
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                    Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
                    Object.defineProperty(navigator, 'vendor', { get: () => 'Google Inc.' });
                """
            },
        )
    except Exception as e:
        print(f"[AutoListing] Browser safety patch warning: {e}")


class AutoReplyManager:
    """
    Minimal per-account manager for running one auto-reply worker thread per account.
    """

    def __init__(self, account):
        self.account = account
        self.active = False
        self.thread = None
        self.driver = None

        self.reply_message = ""
        self.delay = 5
        self.scope = "all"
        self.last_count = None
        self.pin = None

        self.lock = threading.Lock()
        self.current_run_started_at = None
        self.replied_items = []
        self._handled_conversations = set()
        self._spam_cycle_completed = False
        self._marketplace_clicked_after_spam = False
        self._handled_marketplace_threads = set()
        self._emailed_thread_keys = set()
        self._next_spam_cycle_at = 0.0
        self._force_spam_cycle = False
        self._messenger_initialized = False
        self._initial_pin_wait_done = False
        self._on_crash = None
        self._crash_reported = False

        self.log_file = os.path.join(AUTO_REPLY_LOG_DIR, f"{self.account}.json")
        self._load_log()

        self.get_messages_func = lambda: []

    def _load_log(self):
        self.replied_items = []
        self._emailed_thread_keys = set()
        if not os.path.exists(self.log_file):
            return
        try:
            with open(self.log_file, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            return

        if isinstance(payload, list):
            self.replied_items = [item for item in payload if isinstance(item, dict)]
            return

        if isinstance(payload, dict):
            items = payload.get("replied_items", [])
            if isinstance(items, list):
                self.replied_items = [item for item in items if isinstance(item, dict)]
            emailed_keys = payload.get("emailed_thread_keys", [])
            if isinstance(emailed_keys, list):
                self._emailed_thread_keys = {
                    str(item).strip().lower()
                    for item in emailed_keys
                    if str(item).strip()
                }

    def set_crash_callback(self, callback):
        self._on_crash = callback

    def _report_crash(self, details):
        if self._crash_reported:
            return
        self._crash_reported = True
        if not callable(self._on_crash):
            return
        try:
            self._on_crash(self.account, str(details or "").strip())
        except Exception:
            pass

    def save_log(self):
        with self.lock:
            payload = {
                "replied_items": self.replied_items[-500:],
                "emailed_thread_keys": sorted(self._emailed_thread_keys)[-5000:],
            }
            with open(self.log_file, "w", encoding="utf-8") as f:
                json.dump(payload, f)

    def _send_notification_email(self, sender_name, thread_title, incoming_message):
        receiver_email = get_smtp_receiver_email()
        if not SMTP_SENDER_EMAIL or not SMTP_SENDER_PASSWORD or not receiver_email:
            print(f"[AutoReply-{self.account}] SMTP not configured; email skipped")
            return False
        try:
            clean_sender = self._normalize_text(sender_name, 160) or "Unknown buyer"
            clean_thread = self._normalize_text(thread_title, 220)
            clean_message = self._normalize_text(incoming_message, 500)
            
            msg = EmailMessage()
            msg["From"] = SMTP_SENDER_EMAIL
            msg["To"] = receiver_email
            msg["Subject"] = f"New Marketplace Message from {clean_sender}"
            msg.set_content(
                f"Account: {self.account}\n"
                f"Sender: {clean_sender}\n"
                f"Thread: {clean_thread}\n"
                f"Message: {clean_message}\n"
                f"Captured At: {datetime.now(timezone.utc).isoformat()}\n"
            )
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                server.starttls()
                server.login(SMTP_SENDER_EMAIL, SMTP_SENDER_PASSWORD)
                server.send_message(msg)
            return True
        except Exception as e:
            print(f"[AutoReply-{self.account}] Email send failed: {e}")
            return False

    def _email_notification_if_new(self, thread_key, sender_name, thread_title, incoming_message):
        key = str(thread_key).strip().lower()
        if not key or key in self._emailed_thread_keys:
            return

        sent = self._send_notification_email(sender_name, thread_title, incoming_message)
        if sent:
            self._emailed_thread_keys.add(key)
            self.save_log()
            print(f"[AutoReply-{self.account}] Notification email sent for thread: {key}")

    def _parse_replied_at(self, replied_at):
        raw = str(replied_at or "").strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return None

    def _normalize_text(self, value, max_len=500):
        text = " ".join(str(value or "").split())
        return text[:max_len] if text else ""

    def _sleep_while_active(self, seconds):
        end_time = time.time() + max(0.0, float(seconds))
        while self.active and time.time() < end_time:
            time.sleep(0.2)

    def _safe_click(self, element, close_popup=True):
        if close_popup:
            self._close_members_popup_if_present()
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
                element,
            )
            time.sleep(0.15)
            element.click()
            return True
        except ElementClickInterceptedException:
            if close_popup:
                self._close_members_popup_if_present()
            try:
                self.driver.execute_script("arguments[0].click();", element)
                return True
            except Exception:
                return False
        except Exception:
            return False

    def _extract_name_listing(self, title_text):
        raw = self._normalize_text(title_text, 300)
        sep = "\u00b7"
        if sep not in raw:
            return None, None
        left, right = raw.split(sep, 1)
        sender_name = self._normalize_text(left, 120) or "there"
        listing_name = self._normalize_text(right, 180) or "this listing"
        return sender_name, listing_name

    def _build_marketplace_message(self, sender_name, listing_name):
        if self.reply_message:
            return self.reply_message
        return (
            f"hey {sender_name} , {listing_name} is a ver good choice , "
            "please give us your numebr , we will contact you"
        )

    def _collect_conversation_texts(self):
        text_nodes = self.driver.find_elements(
            By.XPATH,
            "//div[@role='main']//span[@dir='auto' and normalize-space()]",
        )
        texts = []
        for node in text_nodes:
            try:
                t = self._normalize_text(node.text, 500)
            except StaleElementReferenceException:
                continue
            if t:
                texts.append(t)
        return texts

    def _send_reply_text(self, composer_locator, reply_text, timeout=12):
        composer = WebDriverWait(self.driver, timeout).until(
            EC.element_to_be_clickable(composer_locator)
        )
        try:
            composer.click()
        except Exception:
            try:
                self.driver.execute_script("arguments[0].click();", composer)
            except Exception:
                pass
        time.sleep(0.2)
        composer.send_keys(reply_text)
        time.sleep(0.2)
        composer.send_keys(Keys.ENTER)
        return True

    def _get_known_replied_thread_keys(self):
        keys = set()
        with self.lock:
            items = list(self.replied_items)
        for item in items:
            if not isinstance(item, dict):
                continue
            for field in ("thread_id", "thread_url"):
                value = self._normalize_text(item.get(field), 300).lower()
                if value:
                    keys.add(value)
        return keys


    def _scroll_chat_to_bottom(self, steps=8):
        try:
            containers = self.driver.find_elements(
                By.XPATH,
                "//div[@role='main']"
            )

            if not containers:
                return

            chat = containers[0]

            for _ in range(steps):
                self.driver.execute_script(
                    "arguments[0].scrollTop = arguments[0].scrollHeight;",
                    chat,
                )
                time.sleep(0.3)

        except Exception:
            pass

   
  

    def _conversation_has_account_sent_message(self, max_page_ups=None):
        """
        Returns True if this Facebook account has already sent
        at least one message in the currently opened conversation.
        """

        print("Checking conversation for account-sent messages...")

        try:
            # Scroll near the bottom so latest messages are loaded
            self._scroll_chat_to_bottom(steps=8)

            # Find every visible message bubble
            messages = self.driver.find_elements(
                By.XPATH,
                '//div[@aria-roledescription="message"]'
            )

            print(f"Found {len(messages)} message nodes")

            own_patterns = [
                "you:",
                "you sent",
                "you sent:",
                "you"
            ]

            for i, msg in enumerate(messages):
                try:
                    aria = msg.get_attribute("aria-label")

                    if not aria:
                        continue

                    print(f"Raw message {i}: {aria}")

                    normalized = aria.lower()
                    normalized = " ".join(normalized.split())
                    normalized = normalized.replace(" :", ":")
                    normalized = normalized.replace(": ", ":")

                    print(f"Normalized: {normalized}")

                    for pattern in own_patterns:
                        if pattern in normalized:
                            print(
                                f"FOUND account sent message "
                                f"(matched '{pattern}')"
                            )
                            return True

                except StaleElementReferenceException:
                    continue

                except Exception as e:
                    print(f"Message {i} read error: {e}")

        except Exception as e:
            print(f"Conversation scan error: {e}")

        print("No account-sent messages found.")
        return False

    def _process_marketplace_unread_threads(self):
        unread_row_locator = (
            By.XPATH,
            "//div[@role='row']//a[@role='link' and .//div[normalize-space()='Unread message:']]",
        )
        unread_locators = [
            (
                By.XPATH,
                unread_row_locator[1],
            ),
            (
                By.XPATH,
                "//div[@role='row' and .//div[normalize-space()='Unread message:']]//a[@role='link']",
            ),
        ]
        composer_locator = (
            By.XPATH,
            "//div[@role='textbox' and @contenteditable='true']"
            "|//div[@aria-label='Message' and @contenteditable='true']",
        )

        known_replied_keys = self._get_known_replied_thread_keys()

        for _ in range(120):
            if not self.active:
                return

            self._close_members_popup_if_present()
            self._return_to_marketplace_unread_list()

            unread_buttons = []
            for locator in unread_locators:
                try:
                    unread_buttons = self.driver.find_elements(*locator)
                except Exception:
                    unread_buttons = []
                if unread_buttons:
                    break

            target_btn = None
            target_key = ""
            target_text = ""

            for btn in unread_buttons:
                try:
                    if not btn.is_displayed():
                        continue

                    unread_markers = btn.find_elements(
                        By.XPATH,
                        ".//div[normalize-space()='Unread message:']"
                        "|.//*[contains(translate(normalize-space(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'new message')]"
                        "|.//*[@aria-label and contains(translate(@aria-label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'unread')]"
                        "|.//*[@aria-label and contains(translate(@aria-label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'new message')]",
                    )

                    if not unread_markers:
                        continue

                    btn_text = self._normalize_text(btn.text, 300)
                    href = self._normalize_text(btn.get_attribute("href"), 300)
                    title_nodes = btn.find_elements(
                        By.XPATH,
                        ".//span[contains(normalize-space(),'\u00b7')]",
                    )
                    title_text = (
                        self._normalize_text(title_nodes[0].text, 220)
                        if title_nodes
                        else btn_text
                    )

                except StaleElementReferenceException:
                    continue

                if not btn_text:
                    continue

                target_btn = btn
                target_key = (href or title_text or btn_text).lower()
                target_text = title_text
                break

            if target_btn is None:
                return

            if not self._safe_click(target_btn):
                continue

            time.sleep(1)

            sender_name, listing_name = self._extract_name_listing(target_text)

            if not sender_name:
                sender_name = "there"

            if not listing_name:
                listing_name = "this listing"

            alias_key = f"{sender_name.lower()}|{listing_name.lower()}"

            thread_keys = {
                self._normalize_text(target_key, 300).lower(),
                self._normalize_text(target_text, 300).lower(),
                alias_key,
            }

            thread_keys = {k for k in thread_keys if k}

            already_replied = any(k in known_replied_keys for k in thread_keys)

            has_account_sent_message = self._conversation_has_account_sent_message(
                max_page_ups=2000
            )

            
            latest_texts = self._collect_conversation_texts()
            latest_incoming = (
                latest_texts[-1] if latest_texts else "New inquiry message"
            )

            sent_before = has_account_sent_message or already_replied

            if not sent_before:
                send_text = self._build_marketplace_message(
                    sender_name,
                    listing_name,
                )

                try:
                    self._send_reply_text(
                        composer_locator,
                        send_text,
                        timeout=10,
                    )

                    self._record_replied_item_safe(
                        {
                            "thread_id": target_key,
                            "thread_url": target_key,
                            "sender_name": sender_name,
                            "listing_name": listing_name,
                            "incoming_message": latest_incoming,
                        }
                    )

                    known_replied_keys.update(thread_keys)

                    print(
                        f"[AutoReply-{self.account}] Sent initial message in Marketplace chat"
                    )

                    primary_key = list(thread_keys)[0] if thread_keys else target_key

                    self._email_notification_if_new(
                        primary_key,
                        sender_name,
                        target_text,
                        latest_incoming,
                    )

                except Exception:
                    pass

            self._return_to_marketplace_unread_list()
            time.sleep(0.4)


    def _exit_marketplace_view_to_waiting_page(self):
        back_locator = (
            By.XPATH,
            "//header[.//h1[.//span[normalize-space()='Marketplace']]]//div[@role='button' and @aria-label='Back']",
        )
        try:
            back_btn = WebDriverWait(self.driver, 5).until(
                EC.element_to_be_clickable(back_locator)
            )
            if self._safe_click(back_btn):
                print(f"[AutoReply-{self.account}] Exited Marketplace view to waiting page")
                self._marketplace_clicked_after_spam = False
                self._handled_marketplace_threads = set()
                time.sleep(1)
                return True
        except Exception:
            return False
        return False

    def _return_to_marketplace_unread_list(self):
        unread_list_locator = (
            By.XPATH,
            "//div[@role='row']//a[@role='link' and .//div[normalize-space()='Unread message:']]",
        )
        try:
            if self.driver.find_elements(*unread_list_locator):
                return True
        except Exception:
            pass

        back_locators = [
            (
                By.XPATH,
                "//header//div[@role='button' and @aria-label='Back']",
            ),
            (By.XPATH, "//div[@role='button' and @aria-label='Back']"),
        ]
        for locator in back_locators:
            try:
                back_btn = WebDriverWait(self.driver, 4).until(
                    EC.element_to_be_clickable(locator)
                )
                if self._safe_click(back_btn):
                    time.sleep(0.8)
                    if self.driver.find_elements(*unread_list_locator):
                        return True
            except Exception:
                continue
        return False

    def _click_requests_back(self):
        back_locators = [
            (
                By.XPATH,
                "//header[.//h1[.//span[normalize-space()='Requests']]]//div[@role='button' and @aria-label='Back']",
            ),
            (By.XPATH, "//div[@role='button' and @aria-label='Back']"),
        ]
        for locator in back_locators:
            try:
                back_btn = WebDriverWait(self.driver, 8).until(
                    EC.element_to_be_clickable(locator)
                )
                if self._safe_click(back_btn):
                    print(f"[AutoReply-{self.account}] Clicked Back from Requests")
                    return True
            except Exception:
                continue
        return False

    def _click_marketplace_grid_section(self, require_unread=False, wait_after_click=5):
        marketplace_locators = [
            (
                By.XPATH,
                "//div[@role='gridcell']//div[@role='button' and .//span[normalize-space()='Marketplace']]",
            ),
            (
                By.XPATH,
                "//*[@role='gridcell']//*[self::div or self::a][@role='button' and .//span[normalize-space()='Marketplace']]",
            ),
        ]
        unread_pattern = re.compile(r"\b\d+\s+new\s+messages?\b", re.IGNORECASE)
        for locator in marketplace_locators:
            try:
                buttons = self.driver.find_elements(*locator)
            except Exception:
                buttons = []
            for marketplace_btn in buttons:
                try:
                    if not marketplace_btn.is_displayed():
                        continue
                except StaleElementReferenceException:
                    continue

                if require_unread:
                    has_unread = False
                    try:
                        unread_nodes = marketplace_btn.find_elements(
                            By.XPATH,
                            ".//*[contains(translate(normalize-space(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'new message')]"
                            "|ancestor::*[@role='gridcell'][1]//*[contains(translate(normalize-space(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'new message']",
                        )
                    except Exception:
                        unread_nodes = []
                    for node in unread_nodes:
                        try:
                            unread_text = self._normalize_text(node.text, 120)
                        except StaleElementReferenceException:
                            continue
                        if unread_pattern.search(unread_text) or "new message" in unread_text.lower():
                            has_unread = True
                            break
                    if not has_unread:
                        continue

                if self._safe_click(marketplace_btn):
                    print(f"[AutoReply-{self.account}] Clicked Marketplace section")
                    time.sleep(max(0.0, float(wait_after_click)))
                    self._marketplace_clicked_after_spam = True
                    return True
        if require_unread:
            print(f"[AutoReply-{self.account}] Waiting for unread Marketplace messages")
        return False

    def _close_members_popup_if_present(self):
        close_locators = [
            (
                By.XPATH,
                "//h2[.//span[normalize-space()='Members']]/ancestor::div[1]/following-sibling::div//div[@role='button' and @aria-label='Close'][1]",
            ),
            (
                By.XPATH,
                "//span[normalize-space()='Members']/ancestor::div[contains(@class,'html-div')][1]/following-sibling::div//div[@role='button' and @aria-label='Close'][1]",
            ),
            (By.XPATH, "//div[@role='button' and @aria-label='Close']"),
        ]

        for locator in close_locators:
            try:
                buttons = self.driver.find_elements(*locator)
            except Exception:
                buttons = []
            for btn in buttons:
                try:
                    if not btn.is_displayed():
                        continue
                except StaleElementReferenceException:
                    continue
                if self._safe_click(btn, close_popup=False):
                    time.sleep(0.3)
                    print(f"[AutoReply-{self.account}] Closed Members popup")
                    return

        try:
            self.driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            time.sleep(0.2)
        except Exception:
            pass

    def _find_visible_pin_input(self):
        pin_input_xpath = (
            "//input["
            "@id='mw-numeric-code-input-prevent-composer-focus-steal'"
            " or (@aria-label='PIN' and @autocomplete='one-time-code')"
            " or @aria-label='PIN'"
            "]"
        )
        try:
            inputs = self.driver.find_elements(By.XPATH, pin_input_xpath)
        except Exception:
            inputs = []
        for el in inputs:
            try:
                if el.is_displayed() and el.is_enabled():
                    return el
            except StaleElementReferenceException:
                continue
            except Exception:
                continue
        return None

    def _find_any_pin_input(self):
        pin_input_xpath = (
            "//input["
            "@id='mw-numeric-code-input-prevent-composer-focus-steal'"
            " or (@aria-label='PIN' and @autocomplete='one-time-code')"
            " or @aria-label='PIN'"
            "]"
        )
        try:
            inputs = self.driver.find_elements(By.XPATH, pin_input_xpath)
        except Exception:
            inputs = []
        for el in inputs:
            try:
                if el.is_enabled():
                    return el
            except StaleElementReferenceException:
                continue
            except Exception:
                continue
        return None

    def _is_pin_prompt_visible(self):
        prompt_xpath = (
            "//*[contains(normalize-space(),'Enter your PIN to restore chat history')]"
            "|//*[contains(normalize-space(),'Chat history is missing')]"
        )
        try:
            nodes = self.driver.find_elements(By.XPATH, prompt_xpath)
        except Exception:
            nodes = []
        for node in nodes:
            try:
                if node.is_displayed():
                    return True
            except StaleElementReferenceException:
                continue
            except Exception:
                continue
        return False

    def _wait_and_submit_pin(self, appear_timeout=10, close_timeout=30):
        if not self.pin:
            return False

        try:
            WebDriverWait(self.driver, max(1, int(appear_timeout))).until(
                lambda d: (
                    self._find_visible_pin_input() is not None
                    or self._find_any_pin_input() is not None
                    or self._is_pin_prompt_visible()
                )
            )
        except TimeoutException:
            print(f"[AutoReply-{self.account}] PIN dialog not shown in {int(appear_timeout)}s; continuing")
            return False

        pin_input = self._find_visible_pin_input()
        if pin_input is None:
            try:
                pin_input = WebDriverWait(self.driver, 5).until(
                    lambda d: self._find_any_pin_input() or False
                )
            except TimeoutException:
                print(f"[AutoReply-{self.account}] PIN prompt detected but input field not ready")
                return False

        try:
            pin_group = self.driver.find_element(
                By.XPATH,
                "//div[@role='group'][following-sibling::input[@aria-label='PIN' or @id='mw-numeric-code-input-prevent-composer-focus-steal']]",
            )
            try:
                pin_group.click()
            except Exception:
                self.driver.execute_script("arguments[0].click();", pin_group)
        except Exception:
            pass

        try:
            pin_input.click()
        except Exception:
            try:
                self.driver.execute_script("arguments[0].click();", pin_input)
            except Exception:
                pass
        try:
            self.driver.execute_script("arguments[0].focus();", pin_input)
        except Exception:
            pass

        try:
            pin_input.send_keys(Keys.CONTROL, "a")
            pin_input.send_keys(Keys.BACKSPACE)
        except Exception:
            pass
        try:
            pin_input.clear()
        except Exception:
            pass

        pin_digits = re.sub(r"\D", "", str(self.pin).strip()) or str(self.pin).strip()
        try:
            pin_input.send_keys(pin_digits)
            time.sleep(10)
        except Exception:
            pass

        try:
            entered = (pin_input.get_attribute("value") or "").strip()
        except Exception:
            entered = ""
        if len(entered) < len(pin_digits):
            try:
                self.driver.execute_script(
                    "arguments[0].value = arguments[1];"
                    "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
                    "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
                    pin_input,
                    pin_digits,
                )
                time.sleep(5)
            except Exception:
                pass

        try:
            WebDriverWait(self.driver, max(1, int(close_timeout))).until(
                lambda d: self._find_visible_pin_input() is None and not self._is_pin_prompt_visible()
            )
            print(f"[AutoReply-{self.account}] PIN submitted and dialog closed")
            return True
        except TimeoutException:
            print(f"[AutoReply-{self.account}] PIN submitted, but dialog stayed visible after {int(close_timeout)}s")
            return False

    def _process_spam_marketplace_threads(self):
        if self._spam_cycle_completed:
            return

        self._close_members_popup_if_present()
        moved_back = False

        thread_title_locator = (
            By.XPATH,
            "//span[contains(normalize-space(),'\u00b7') and not(ancestor::*[@role='tab'])]",
        )
        accept_btn_locator = (
            By.XPATH,
            "//div[@role='button' and (@aria-label='Accept' or .//span[normalize-space()='Accept'])]",
        )
        composer_locator = (
            By.XPATH,
            "//div[@role='textbox' and @contenteditable='true']"
            "|//div[@aria-label='Message' and @contenteditable='true']",
        )

        idle_rounds = 0
        for _ in range(25):
            if not self.active:
                return
            self._close_members_popup_if_present()

            title_nodes = self.driver.find_elements(*thread_title_locator)
            handled_this_round = 0

            for title_node in title_nodes:
                if not self.active:
                    return
                try:
                    if not title_node.is_displayed():
                        continue
                    title_text = self._normalize_text(title_node.text, 260)
                except StaleElementReferenceException:
                    continue

                sender_name, listing_name = self._extract_name_listing(title_text)
                if not sender_name or not listing_name:
                    continue

                thread_key = f"{sender_name.lower()}|{listing_name.lower()}"
                if thread_key in self._handled_conversations:
                    continue

                try:
                    if not self._safe_click(title_node):
                        continue
                    time.sleep(1)

                    accept_buttons = self.driver.find_elements(*accept_btn_locator)
                    if not accept_buttons:
                        self._handled_conversations.add(thread_key)
                        continue

                    if not self._safe_click(accept_buttons[0]):
                        continue
                    time.sleep(1)

                    reply_text = self._build_marketplace_message(sender_name, listing_name)
                    self._send_reply_text(composer_locator, reply_text, timeout=20)
                    self._record_replied_item_safe(
                        {
                            "thread_id": thread_key,
                            "thread_url": thread_key,
                            "sender_name": sender_name,
                            "listing_name": listing_name,
                            "incoming_message": "Spam inquiry message",
                        }
                    )

                    self._handled_conversations.add(thread_key)
                    handled_this_round += 1
                    print(
                        f"[AutoReply-{self.account}] Accepted and replied in spam: "
                        f"{sender_name} | {listing_name}"
                    )
                    
                    # Email notification for new spam conversation reply
                    self._email_notification_if_new(thread_key, sender_name, title_text, "Spam inquiry message")
                    
                    time.sleep(1)
                except Exception as e:
                    print(
                        f"[AutoReply-{self.account}] Failed to accept/reply in spam thread: {e}"
                    )
                    continue

            if handled_this_round == 0:
                idle_rounds += 1
            else:
                idle_rounds = 0

            if idle_rounds >= 3:
                if self._click_requests_back():
                    self._spam_cycle_completed = True
                    moved_back = True
                break

            try:
                self.driver.find_element(By.TAG_NAME, "body").send_keys(Keys.PAGE_DOWN)
            except Exception:
                pass
            time.sleep(1.2)

        if not moved_back:
            if self._click_requests_back():
                self._spam_cycle_completed = True

    def _run_spam_cycle(self):
        settings_locators = [
            (By.XPATH, "//div[@role='button' and @aria-label='Settings, help and more']"),
            (
                By.XPATH,
                "//a[@aria-label='New message']/ancestor::span[1]/preceding-sibling::span[1]//*[@aria-label='Settings, help and more' and @aria-controls='mw-inbox-settings-menu']",
            ),
            (
                By.XPATH,
                "//h1[normalize-space()='Chats']/ancestor::div[1]/following::div[@aria-label='Settings, help and more' and @aria-controls='mw-inbox-settings-menu'][1]",
            ),
            (
                By.XPATH,
                "//*[@aria-label='Settings, help and more' and @aria-controls='mw-inbox-settings-menu' and (@role='button' or @tabindex='0')]",
            ),
        ]
        settings_btn = None
        selected_locator = None
        for locator in settings_locators:
            try:
                settings_btn = WebDriverWait(self.driver, 10).until(
                    EC.element_to_be_clickable(locator)
                )
                selected_locator = locator
                break
            except TimeoutException:
                continue
        if settings_btn is None:
            raise TimeoutException("Settings button not found with known locators.")

        clicked = False
        for _ in range(3):
            try:
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
                    settings_btn,
                )
                time.sleep(0.2)
                settings_btn.click()
                clicked = True
                break
            except ElementClickInterceptedException:
                try:
                    self.driver.execute_script("arguments[0].click();", settings_btn)
                    clicked = True
                    break
                except Exception:
                    time.sleep(0.3)
                    if selected_locator is not None:
                        try:
                            settings_btn = WebDriverWait(self.driver, 5).until(
                                EC.presence_of_element_located(selected_locator)
                            )
                        except Exception:
                            pass
            except StaleElementReferenceException:
                if selected_locator is not None:
                    settings_btn = WebDriverWait(self.driver, 5).until(
                        EC.presence_of_element_located(selected_locator)
                    )
        if not clicked:
            raise ElementClickInterceptedException(
                "Settings button located but click stayed intercepted after retries."
            )
        print(f"[AutoReply-{self.account}] Clicked Settings button")

        message_requests_item = WebDriverWait(self.driver, 15).until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    "//div[@role='menuitem' and .//span[normalize-space()='Message requests']]",
                )
            )
        )
        try:
            message_requests_item.click()
        except ElementClickInterceptedException:
            self.driver.execute_script("arguments[0].click();", message_requests_item)
        self._close_members_popup_if_present()
        print(f"[AutoReply-{self.account}] Clicked Message requests")
        time.sleep(5)

        spam_tab = WebDriverWait(self.driver, 20).until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    "//div[@role='tab' and @aria-hidden='false' and .//span[normalize-space()='Spam']]",
                )
            )
        )
        try:
            spam_tab.click()
        except ElementClickInterceptedException:
            self.driver.execute_script("arguments[0].click();", spam_tab)
        print(f"[AutoReply-{self.account}] Clicked Spam tab")
        time.sleep(10)
        self._close_members_popup_if_present()
        try:
            self._process_spam_marketplace_threads()
        except StaleElementReferenceException:
            print(f"[AutoReply-{self.account}] Spam list refreshed, will retry in worker loop")

    def _record_replied_item(self, message):
        entry = {
            "account": self.account,
            "thread_id": self._normalize_text(message.get("thread_id") or message.get("thread_url") or ""),
            "thread_url": self._normalize_text(message.get("thread_url") or message.get("thread_id") or ""),
            "sender_name": self._normalize_text(message.get("sender_name"), 160) or "Unknown buyer",
            "listing_name": self._normalize_text(message.get("listing_name"), 220) or "Marketplace Listing",
            "incoming_message": self._normalize_text(message.get("incoming_message")) or "Message preview unavailable",
            "replied_at": datetime.now(timezone.utc).isoformat(),
        }
        with self.lock:
            self.replied_items.append(entry)
            if len(self.replied_items) > 500:
                self.replied_items = self.replied_items[-500:]

    def _record_replied_item_safe(self, message):
        try:
            self._record_replied_item(message)
            self.save_log()
        except Exception as e:
            print(f"[AutoReply-{self.account}] Failed to record replied item: {e}")

    def get_replied_items_snapshot(self):
        with self.lock:
            items = list(reversed(self.replied_items))
            if self.current_run_started_at is not None:
                filtered = []
                for item in items:
                    replied_dt = self._parse_replied_at(item.get("replied_at"))
                    if replied_dt is not None and replied_dt >= self.current_run_started_at:
                        filtered.append(item)
                items = filtered
            return items[:200]

    def delete_replied_item(self, thread_id, replied_at=None):
        removed = False
        with self.lock:
            before = len(self.replied_items)
            if replied_at:
                self.replied_items = [
                    item
                    for item in self.replied_items
                    if not (item.get("thread_id") == thread_id and item.get("replied_at") == replied_at)
                ]
            else:
                self.replied_items = [item for item in self.replied_items if item.get("thread_id") != thread_id]
            removed = len(self.replied_items) != before
        if removed:
            self.save_log()
        return removed

    def clear_replied_items(self):
        with self.lock:
            had_items = bool(self.replied_items)
            self.replied_items = []
        if had_items:
            self.save_log()
        return had_items

    def start(self, reply_message, delay=5, scope="all", last_count=None, pin=None, get_messages_func=None):
        if self.active:
            return False

        self.active = True
        self._crash_reported = False
        self.current_run_started_at = datetime.now(timezone.utc)
        with self.lock:
            self.replied_items = []
        self._handled_conversations = set()
        self._spam_cycle_completed = False
        self._marketplace_clicked_after_spam = False
        self._handled_marketplace_threads = set()
        self._next_spam_cycle_at = 0.0
        self._force_spam_cycle = False
        self._messenger_initialized = False
        self._initial_pin_wait_done = False

        self.reply_message = (reply_message or "").strip()
        self.delay = max(1, int(delay or 5))
        self.scope = scope
        self.last_count = last_count
        submitted_pin = str(pin or "").strip()
        if submitted_pin:
            self.pin = submitted_pin

        raw_get = get_messages_func or get_new_messages

        def get_msgs():
            try:
                return raw_get(self.account, scope=self.scope, last_count=self.last_count, driver=None, pin=self.pin)
            except TypeError:
                return raw_get(self.account, scope=self.scope, last_count=self.last_count)

        self.get_messages_func = get_msgs
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return True

    def stop(self):
        self.active = False
        if self.thread:
            self.thread.join(timeout=1)
        self.thread = None
        self._discard_driver()
        return True

    def _discard_driver(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
        self.driver = None

    def _run(self):
        target_url = "https://www.facebook.com/marketplace/?ref=bookmark"
        for startup_attempt in range(2):
            startup_stage = "creating Chrome driver"
            try:
                pin_submitted_this_init = False
                if self.driver is None:
                    self.driver = get_driver_for_account(self.account)

                startup_stage = "opening Marketplace"
                self.driver.get(target_url)
                self._close_members_popup_if_present()
                print(f"[AutoReply-{self.account}] Opened Marketplace URL")

                if not self._messenger_initialized:
                    startup_stage = "opening Messenger menu"
                    messenger_button = WebDriverWait(self.driver, 15).until(
                        EC.element_to_be_clickable(
                            (
                                By.XPATH,
                                "//div[@role='button' and (@aria-label='Messenger' or (starts-with(@aria-label,'Messenger, ') and contains(@aria-label,' unread')))]",
                            )
                        )
                    )
                    messenger_button.click()
                    self._close_members_popup_if_present()
                    print(f"[AutoReply-{self.account}] Clicked Messenger icon")
                    try:
                        startup_stage = "submitting Messenger PIN"
                        pin_submitted_this_init = self._wait_and_submit_pin(appear_timeout=10, close_timeout=30)
                        time.sleep(5)
                    except Exception as e:
                        print(f"[AutoReply-{self.account}] PIN submit wait skipped: {e}")

                    startup_stage = "opening full Messenger"
                    see_all_btn = WebDriverWait(self.driver, 20).until(
                        EC.element_to_be_clickable((By.XPATH, "//a[@aria-label='See all in Messenger']"))
                    )
                    see_all_btn.click()
                    self._close_members_popup_if_present()
                    WebDriverWait(self.driver, 20).until(EC.url_contains("/messages/"))
                    if not pin_submitted_this_init:
                        try:
                            startup_stage = "submitting Messenger PIN after navigation"
                            pin_submitted_this_init = self._wait_and_submit_pin(appear_timeout=5, close_timeout=30)
                        except Exception as e:
                            print(f"[AutoReply-{self.account}] PIN submit after Messenger load skipped: {e}")
                    self._messenger_initialized = True

                if pin_submitted_this_init and not self._initial_pin_wait_done:
                    print(f"[AutoReply-{self.account}] Waiting 15s for PIN verification")
                    self._sleep_while_active(15)
                    self._initial_pin_wait_done = True
                break
            except Exception as e:
                if _is_invalid_session_error(e) and startup_attempt == 0 and self.active:
                    print(
                        f"[AutoReply-{self.account}] Chrome session died while {startup_stage}; "
                        "restarting Chrome once"
                    )
                    self._messenger_initialized = False
                    self._discard_driver()
                    continue

                print(f"[AutoReply-{self.account}] Startup error while {startup_stage}: {e}")
                self._report_crash(f"Startup error while {startup_stage}: {e}")
                self._discard_driver()
                self.active = False
                return

        while self.active:
            try:
                self._spam_cycle_completed = False
                self._marketplace_clicked_after_spam = False
                self._handled_marketplace_threads = set()
                self._run_spam_cycle()
                if not self.active:
                    break

                clicked = self._click_marketplace_grid_section(require_unread=False, wait_after_click=5)
                if clicked:
                    self._process_marketplace_unread_threads()
                    self._exit_marketplace_view_to_waiting_page()
                # --- ADD THESE LINES FOR THE RANDOM DELAY (15 to 40 minutes) ---
                delay_seconds = random.randint(900, 2400) # 900s = 15 mins, 2400s = 40 mins
                print(f"[AutoReply-{self.account}] Cycle completed. Waiting for {delay_seconds // 60} minutes on Marketplace...")
                self._sleep_while_active(delay_seconds)
                # -------------------------------------------------------------
            except Exception as e:
                print(f"[AutoReply-{self.account}] Worker error: {e}")
                self._sleep_while_active(60)


def get_driver_for_account(account):
    session_file = os.path.join(SESSION_DIR, account + ".pkl")
    if not os.path.exists(session_file):
        raise FileNotFoundError(f"No session file found for account {account}")

    with open(session_file, "rb") as f:
        cookies = pickle.load(f)

    options = Options()
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-notifications")
    options.add_argument("--log-level=3")
    options.add_argument("--disable-blink-features=AutomationControlled")
    apply_background_mode_option(options)
    options.add_experimental_option("excludeSwitches", ["enable-logging", "enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_experimental_option(
        "prefs",
        {
            "profile.default_content_setting_values.notifications": 2
        },
    )

    driver = build_chrome_driver(options)
    _apply_browser_safety_patches(driver)
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")

    driver.get("https://www.facebook.com/")
    time.sleep(2)

    valid = False
    for cookie in cookies:
        if "sameSite" not in cookie:
            cookie["sameSite"] = "Lax"
        cookie["domain"] = ".facebook.com"
        try:
            driver.add_cookie(cookie)
            if cookie.get("name") == "c_user":
                valid = True
        except Exception:
            continue

    if not valid:
        driver.quit()
        raise Exception("Invalid session: c_user cookie missing")

    driver.get("https://www.facebook.com/")
    time.sleep(2)

    if "login" in (driver.current_url or "").lower():
        driver.quit()
        raise Exception(f"Session expired or cookies invalid for {account}")

    return driver


def get_new_messages(account, scope="all", last_count=None, driver=None, pin=None):
    return []


auto_reply_managers = {}