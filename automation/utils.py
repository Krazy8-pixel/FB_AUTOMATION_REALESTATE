# from flask import Blueprint, render_template, request, redirect, url_for, session, flash, send_file, jsonify
# import os, json, uuid, traceback, shutil, time, random, re, tempfile, pickle
# import pandas as pd
# from datetime import datetime, timedelta, timezone
# from io import BytesIO
# from werkzeug.utils import secure_filename
# from selenium.webdriver.common.by import By
# from selenium.webdriver.common.keys import Keys
# from selenium.webdriver.support.ui import WebDriverWait
# from selenium.webdriver.support import expected_conditions as EC

# # ---------- selenium imports ----------
# from selenium import webdriver
# from selenium.webdriver.chrome.options import Options
# from selenium.webdriver.chrome.service import Service
# from selenium.webdriver.support.ui import WebDriverWait
# # from webdriver_manager.chrome import ChromeDriverManager

# # ---------- base path ----------
# BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# SESSION_FILE = "krazy8026_gmail.com.pkl"
# SESSION_PATH = os.path.join(BASE_DIR, "data", "sessions", SESSION_FILE)


# def open_fb_with_saved_session():
#     """Opens Facebook using saved pkl session"""

#     options = Options()
#     options.add_argument("--window-size=1920,1080")
#     options.add_argument("--no-sandbox")
#     options.add_argument("--disable-dev-shm-usage")
#     options.add_argument("--disable-gpu")
#     options.add_argument("--log-level=3")
#     # options.add_argument("--headless=new")  # enable if needed

#     options.add_experimental_option("excludeSwitches", ["enable-logging"])
#     options.add_argument(
#         "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
#         "AppleWebKit/537.36 (KHTML, like Gecko) "
#         "Chrome/116.0.5845.111 Safari/537.36"
#     )

#     prefs = {"profile.managed_default_content_settings.images": 1}
#     options.add_experimental_option("prefs", prefs)

#     driver = webdriver.Chrome(
#         service=Service(ChromeDriverManager().install()),
#         options=options
#     )

#     wait = WebDriverWait(driver, 50)

#     # ---------- open facebook ----------
#     driver.get("https://www.facebook.com/")
#     time.sleep(3)

#     # ---------- load cookies ----------
#     with open(SESSION_PATH, "rb") as f:
#         cookies = pickle.load(f)

#     for cookie in cookies:
#         cookie.pop("sameSite", None)
#         driver.add_cookie(cookie)

#     # ---------- refresh ----------
#     driver.refresh()
#     time.sleep(5)

#     print("✅ Facebook opened using krazy8026_gmail.com session")
#     driver.get("https://www.facebook.com/messages/")
#     time.sleep(3)
#     wait = WebDriverWait(driver, 50)
#     pintab = wait.until(
#     EC.visibility_of_element_located(
#         (By.XPATH, "//div[@role='dialog']")
#     )
# )
#     print("Pin tab found")
    
#     pinarea=pintab.find_element(By.XPATH, ".//div[@role='group']")
#     print("Pin area found")
#     pininput = pinarea.find_element(
#     By.XPATH,
#     "following-sibling::input[@aria-label='PIN']"
# )
#     print("Pin input found")
#     pininput.clear()
#     pininput.send_keys("428111")
#     time.sleep(50)
    
  
#     # div[@role='dialog']  input[@aria-label='PIN']
 

# open_fb_with_saved_session()
 

# import smtplib
# from email.mime.text import MIMEText

# sender_email = "muaazbadar007@gmail.com"
# receiver_email = "krazy8026@gmail.com"
# app_password = "gecs wyrc jxwu qzpd"  # Use App Password, not your real password

# try:
#     server = smtplib.SMTP("smtp.gmail.com", 587, timeout=10)
#     server.ehlo()
#     server.starttls()
#     server.ehlo()
#     server.login(sender_email, app_password)

#     msg = MIMEText("hi sir, it is to inform you that your college fee is due, please pay it as soon as possible.PLease pay fee and tell us as soon as possible. thanks")
#     msg["Subject"] = "Your College Fee is due , Please pay it as soon as possible"
#     msg["From"] = sender_email
#     msg["To"] = receiver_email

#     server.sendmail(sender_email, receiver_email, msg.as_string())
#     server.quit()

#     print("Email sent successfully!")

# except Exception as e:
#     print("Error:", e)
