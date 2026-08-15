"""
ربات تلگرامی دانلودر اینستاگرام (عکس / ویدیو + صدا)
======================================================
ساختار فایل:
    1) تنظیمات و پیکربندی (env, مسیرها, لاگ)
    2) متن‌های چندزبانه (i18n) + نگاشت دکمه‌ها
    3) ابزار پاکسازی چت (حذف پیام‌های قبلی ربات/کاربر)
    4) هندلرهای تلگرام (start / انتخاب زبان / انتخاب نوع / دریافت لینک ...)
    5) موتور دانلود:
         - تشخیص پلتفرم (فعلاً فقط اینستاگرام پشتیبانی می‌شود)
         - مدیریت کوکی اینستاگرام
         - دانلود عکس اینستاگرام (چند لایه fallback، بدون کرش)
         - دانلود ویدیو + استخراج صدا (بدون تغییر منطق قبلی)
    6) ارسال فایل به کاربر
    7) هندلر خطای سراسری
    8) Keep-Alive (Flask) برای Render + UptimeRobot
    9) اجرای ربات (main)
"""

# ============================================================
# 1) تنظیمات و پیکربندی
# ============================================================
import os
import re
import json
import time
import uuid
import shutil
import asyncio
import logging
import subprocess
import http.cookiejar
from threading import Thread
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor

import requests
import yt_dlp
from dotenv import load_dotenv
from flask import Flask
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, BotCommand, MenuButtonCommands
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    PicklePersistence,
    AIORateLimiter,
    filters,
)

load_dotenv()  # مقادیر رو از فایل .env می‌خونه

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN پیدا نشد. یه فایل .env کنار bot.py بساز و توش بنویس:\n"
        "BOT_TOKEN=توکن_ربات_تو"
    )

DOWNLOAD_DIR = "downloads"
PERSISTENCE_FILE = "bot_data.pkl"

# مسیر کوکی اینستاگرام: روی Render از Secret File خونده و توی DOWNLOAD_DIR کپی می‌شه
COOKIE_SOURCE = "/etc/secrets/cookies.txt"
COOKIE_FILE = os.path.join(DOWNLOAD_DIR, "cookies.txt")

# ترد پول اختصاصی برای دانلود، مستقل از تعداد هسته‌ی CPU سرور
DOWNLOAD_EXECUTOR = ThreadPoolExecutor(max_workers=20)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

if os.path.exists(COOKIE_SOURCE):
    shutil.copy2(COOKIE_SOURCE, COOKIE_FILE)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

logger.info("بررسی کوکی اینستاگرام | source=%s copied=%s", os.path.exists(COOKIE_SOURCE), os.path.exists(COOKIE_FILE))

# پلتفرم‌هایی که فعلاً پشتیبانی می‌شن. هر پلتفرم جدید بعداً فقط باید اینجا اضافه بشه.
SUPPORTED_DOMAINS = ("instagram.com", "instagr.am")


# ============================================================
# 2) متن‌های چندزبانه (i18n)
# ============================================================
TEXTS = {
    "fa": {
        "choose_lang": "لطفاً زبان مورد نظرت رو انتخاب کن 🌐",
        "welcome": (
            "سلام! 👋\n"
            "حالا نوع دانلودی که می‌خوای رو انتخاب کن 👇"
        ),
        "choose_type": "می‌خوای عکس بگیری یا ویدیو رو همراه با فایل صدای جداگانه‌اش دانلود کنی؟",
        "photo_btn": "📷 عکس",
        "video_audio_btn": "🎬🎵 ویدیو + صدا",
        "type_saved": "باشه! حالا لینک اینستاگرامت رو برام بفرست 🔗",
        "start_choice": "دوباره خوش اومدی! 👋\nمی‌خوای همون لینک قبلی رو دوباره دانلود کنم یا یه لینک جدید برام بفرستی؟",
        "prev_link_btn": "🔁 لینک قبلی",
        "new_link_btn": "🆕 لینک جدید",
        "send_new_link": "باشه، لینک جدیدت رو برام بفرست 🔗",
        "redownloading_prev": "باشه، دارم همون لینک قبلی رو با تنظیمات جدید برات دانلود می‌کنم 🔄",
        "invalid_link": "این یه لینک معتبر نیست. یه لینک درست بفرست 🔗",
        "need_setup": "اول باید /start رو بزنی و زبان و نوع دانلود رو انتخاب کنی.",
        "downloading": "⏳ در حال دانلود عکس... چند لحظه صبر کن",
        "downloading_video_audio": "⏳🎧 در حال دانلود ویدیو و آماده‌سازی فایل صدا... چند لحظه صبر کن",
        "done": "✅ دانلود انجام شد!",
        "failed": "❌ نشد دانلودش کنم.\nخطا: {err}",
        "other_platform": (
            "🚧 فعلاً ربات فقط از اینستاگرام پشتیبانی می‌کنه.\n"
            "پشتیبانی از پلتفرم‌های دیگه به‌زودی اضافه می‌شه، منتظر بمون! 🙌\n\n"
            "لطفاً یه لینک اینستاگرام برام بفرست 🔗"
        ),
    },
    "en": {
        "choose_lang": "Please choose your language 🌐",
        "welcome": (
            "Hi! 👋\n"
            "Now choose what type of download you want 👇"
        ),
        "choose_type": "Do you want a photo, or a video with its audio as a separate file too?",
        "photo_btn": "📷 Photo",
        "video_audio_btn": "🎬🎵 Video + Audio",
        "type_saved": "Got it! Now send me your Instagram link 🔗",
        "start_choice": "Welcome back! 👋\nDo you want the same previous link again, or will you send a new one?",
        "prev_link_btn": "🔁 Previous link",
        "new_link_btn": "🆕 New link",
        "send_new_link": "Okay, send me your new link 🔗",
        "redownloading_prev": "Okay, downloading the previous link again with the new settings 🔄",
        "invalid_link": "This isn't a valid link. Send a proper link 🔗",
        "need_setup": "First run /start and choose your language and download type.",
        "downloading": "⏳ Downloading the photo... please wait",
        "downloading_video_audio": "⏳🎧 Downloading the video and preparing the audio file... please wait",
        "done": "✅ Download complete!",
        "failed": "❌ Couldn't download it.\nError: {err}",
        "other_platform": (
            "🚧 Right now the bot only supports Instagram.\n"
            "Support for other platforms is coming soon, stay tuned! 🙌\n\n"
            "Please send me an Instagram link 🔗"
        ),
    },
}


def t(context: ContextTypes.DEFAULT_TYPE, key: str) -> str:
    lang = context.user_data.get("lang", "fa")
    return TEXTS[lang][key]


# متن دکمه‌های زبان -> کد زبان
LANG_LABEL_TO_CODE = {
    "فارسی 🇮🇷": "fa",
    "English 🇬🇧": "en",
}

# متن دکمه‌های نوع دانلود (هر دو زبان) -> نوع دانلود
TYPE_LABEL_TO_TYPE = {}
for _lang_code, _texts in TEXTS.items():
    TYPE_LABEL_TO_TYPE[_texts["photo_btn"]] = "photo"
    TYPE_LABEL_TO_TYPE[_texts["video_audio_btn"]] = "video_audio"

# متن دکمه‌های «لینک قبلی / لینک جدید» (هر دو زبان) -> اکشن
START_CHOICE_LABEL_TO_ACTION = {}
for _lang_code, _texts in TEXTS.items():
    START_CHOICE_LABEL_TO_ACTION[_texts["prev_link_btn"]] = "prev"
    START_CHOICE_LABEL_TO_ACTION[_texts["new_link_btn"]] = "new"


# ============================================================
# 3) ابزار پاکسازی چت: ردیابی و حذف پیام‌های متنی ربات
# ============================================================
async def track_msg(context: ContextTypes.DEFAULT_TYPE, msg):
    """
    message_id هر پیام متنی/راهنمای ربات رو ذخیره می‌کنه تا سر فرصت
    (وقتی پیام جدیدی از کاربر رسید) پاک بشه.
    برای پیام‌های حاوی فایل دانلودی هرگز این تابع صدا زده نمی‌شه،
    پس اون‌ها همیشه توی چت باقی می‌مونن.
    """
    if msg is not None:
        context.user_data.setdefault("bot_msg_ids", []).append(msg.message_id)
    return msg


async def cleanup_bot_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    این هندلر با اولویت بالا (group=-1) روی هر پیام ورودی از کاربر اجرا می‌شه،
    قبل از این‌که هندلر اصلی همون پیام پردازش بشه.
    - تمام پیام‌های متنی ربات از نوبت قبل رو پاک می‌کنه (فایل‌های دانلودی دست‌نخورده می‌مونن)
    - پیام قبلیِ خود کاربر رو هم پاک می‌کنه تا فقط آخرین پیام کاربر توی چت بمونه
    """
    chat_id = update.effective_chat.id

    # پاک کردن پیام‌های متنی/راهنمای ربات از نوبت قبل
    ids = context.user_data.get("bot_msg_ids", [])
    for msg_id in ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            # پیام ممکنه قبلاً پاک شده باشه یا خیلی قدیمی باشه؛ مشکلی نیست
            pass
    context.user_data["bot_msg_ids"] = []

    # پاک کردن پیام قبلیِ کاربر (فقط آخرین پیامش که همین الان فرستاده باقی می‌مونه)
    current_msg_id = update.effective_message.message_id if update.effective_message else None
    prev_user_msg_id = context.user_data.get("last_user_msg_id")
    if prev_user_msg_id and prev_user_msg_id != current_msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=prev_user_msg_id)
        except Exception:
            pass
    if current_msg_id:
        context.user_data["last_user_msg_id"] = current_msg_id


# ============================================================
# 4) هندلرهای تلگرام
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """هر بار /start زده بشه، از اول شروع می‌کنیم و دوباره سؤال زبان پرسیده می‌شه."""
    keyboard = ReplyKeyboardMarkup(
        [["فارسی 🇮🇷", "English 🇬🇧"]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    msg = await update.message.reply_text(
        "لطفاً زبان مورد نظرت رو انتخاب کن 🌐 / Please choose your language 🌐",
        reply_markup=keyboard,
    )
    await track_msg(context, msg)


async def lang_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """انتخاب زبان (پیام متنی از کیبورد پایین)."""
    lang = LANG_LABEL_TO_CODE[update.message.text]
    context.user_data["lang"] = lang

    keyboard = ReplyKeyboardMarkup(
        [[t(context, "photo_btn"), t(context, "video_audio_btn")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    combined_text = f"{t(context, 'welcome')}\n\n{t(context, 'choose_type')}"
    msg = await update.message.reply_text(combined_text, reply_markup=keyboard)
    await track_msg(context, msg)


async def change_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دستور /type: تغییر سریع نوع دانلود بدون نیاز به /start."""
    context.user_data.setdefault("lang", "fa")
    keyboard = ReplyKeyboardMarkup(
        [[t(context, "photo_btn"), t(context, "video_audio_btn")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    msg = await update.message.reply_text(t(context, "choose_type"), reply_markup=keyboard)
    await track_msg(context, msg)


async def type_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """انتخاب نوع دانلود (عکس/ویدیو+صدا) از کیبورد پایین."""
    media_type = TYPE_LABEL_TO_TYPE[update.message.text]
    context.user_data["download_type"] = media_type

    last_url = context.user_data.get("last_url")
    if last_url:
        # نوع دانلود عوض شد؛ حالا می‌پرسیم همون لینک قبلی رو با فرمت جدید بگیره یا کاربر لینک تازه‌ای می‌خواد
        keyboard = ReplyKeyboardMarkup(
            [[t(context, "prev_link_btn"), t(context, "new_link_btn")]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        msg = await update.message.reply_text(t(context, "start_choice"), reply_markup=keyboard)
    else:
        # بعد از انتخاب نوع، کیبورد پایین رو جمع می‌کنیم چون از این به بعد کاربر فقط لینک می‌فرسته
        msg = await update.message.reply_text(t(context, "type_saved"), reply_markup=ReplyKeyboardRemove())
    await track_msg(context, msg)


async def start_choice_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """جواب به سوال «لینک قبلی یا لینک جدید» (بعد از /start یا تغییر نوع دانلود)."""
    action = START_CHOICE_LABEL_TO_ACTION[update.message.text]

    if action == "prev":
        last_url = context.user_data.get("last_url")
        msg = await update.message.reply_text(t(context, "redownloading_prev"), reply_markup=ReplyKeyboardRemove())
        await track_msg(context, msg)
        await process_download(update, context, last_url)
    else:
        msg = await update.message.reply_text(t(context, "send_new_link"), reply_markup=ReplyKeyboardRemove())
        await track_msg(context, msg)


async def new_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دستور /newlink: ارسال لینک جدید برای همون نوع دانلود قبلی."""
    context.user_data.setdefault("lang", "fa")
    context.user_data.setdefault("download_type", "video_audio")
    msg = await update.message.reply_text(t(context, "send_new_link"), reply_markup=ReplyKeyboardRemove())
    await track_msg(context, msg)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """هندلر اصلی: دریافت لینک از کاربر."""
    # اگه کاربر هنوز زبان/نوع دانلود رو انتخاب نکرده (کاربر جدید یا داده از دست رفته)
    # به‌جای گیر دادن، مقدار پیش‌فرض می‌ذاریم تا لینک همیشه کار کنه
    context.user_data.setdefault("lang", "fa")
    context.user_data.setdefault("download_type", "video_audio")

    url = update.message.text.strip()

    if not url.startswith(("http://", "https://")):
        msg = await update.message.reply_text(t(context, "invalid_link"))
        await track_msg(context, msg)
        return

    # فعلاً فقط اینستاگرام پشتیبانی می‌شه؛ برای بقیه پلتفرم‌ها پیام «به‌زودی» می‌فرستیم
    if not is_supported_platform(url):
        msg = await update.message.reply_text(t(context, "other_platform"))
        await track_msg(context, msg)
        return

    # لینک رو ذخیره می‌کنیم تا اگه بعداً کاربر فقط نوع دانلود رو عوض کرد
    # (مثلاً اول عکس خواست، بعد ویدیو)، لازم نباشه لینک رو دوباره بفرسته
    context.user_data["last_url"] = url

    await process_download(update, context, url)


async def process_download(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    """منطق مشترک دانلود و ارسال فایل."""
    file_paths = []
    status_msg = None
    try:
        # لایه‌ی محافظتی اضافه: حتی برای لینک قبلی (prev) هم پلتفرم رو دوباره چک می‌کنیم
        if not is_supported_platform(url):
            msg = await update.message.reply_text(t(context, "other_platform"))
            await track_msg(context, msg)
            return

        media_type = context.user_data.get("download_type", "video_audio")
        status_key = "downloading_video_audio" if media_type == "video_audio" else "downloading"
        status_msg = await update.message.reply_text(t(context, status_key))
        await track_msg(context, status_msg)
        loop = asyncio.get_running_loop()
        # خروجی download_media یه لیست از (مسیر فایل, نوع فایل) هست
        # چون حالت ویدیو+صدا دو تا فایل برمی‌گردونه، ولی حالت عکس فقط یکی
        results = await loop.run_in_executor(
            DOWNLOAD_EXECUTOR, download_media, url, media_type
        )
        file_paths = [p for p, _ in results]
        for file_path, result_kind in results:
            # توجه: پیام‌های فایل دانلودی track نمی‌شن، پس بعد از پاکسازی همیشه توی چت می‌مونن
            await send_file(update, context, file_path, result_kind)
        await status_msg.edit_text(t(context, "done"))
    except Exception as e:
        logger.error("خطا در دانلود لینک %s: %s", url, e, exc_info=True)
        err_text = t(context, "failed").format(err=str(e)[:300])
        try:
            if status_msg:
                await status_msg.edit_text(err_text)
            else:
                msg = await update.message.reply_text(err_text)
                await track_msg(context, msg)
        except Exception:
            logger.error("حتی ارسال پیام خطا هم شکست خورد", exc_info=True)
    finally:
        # پاک کردن همه‌ی فایل‌ها بعد از ارسال تا حجم سرور پر نشه
        for file_path in file_paths:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)


# ============================================================
# 5) موتور دانلود
# ============================================================
def is_supported_platform(url: str) -> bool:
    """
    فقط لینک‌های اینستاگرام (instagram.com / instagr.am) رو تایید می‌کنه.
    این تابع تنها نقطه‌ایه که باید عوض بشه وقتی پلتفرم جدید اضافه می‌کنیم.
    """
    try:
        netloc = urlparse(url).netloc.lower()
    except Exception:
        return False

    # حذف user:pass@ احتمالی جلوی دامنه
    netloc = netloc.split("@")[-1]
    # حذف پورت احتمالی (مثلاً :443)
    netloc = netloc.split(":")[0]

    return any(netloc == domain or netloc.endswith("." + domain) for domain in SUPPORTED_DOMAINS)


def _load_cookie_jar():
    """
    کوکی‌های اینستاگرام رو (همون فایلی که به yt-dlp هم داده میشه) برای
    درخواست‌های مستقیم requests (گرفتن og:image و خودِ عکس) لود می‌کنه.
    بدون این کوکی‌ها، اینستاگرام گاهی نسخه محدود/ناقصی از صفحه یا عکس رو برمی‌گردونه.
    """
    jar = http.cookiejar.MozillaCookieJar()
    if os.path.exists(COOKIE_FILE):
        try:
            jar.load(COOKIE_FILE, ignore_discard=True, ignore_expires=True)
        except Exception as e:
            logger.warning("لود کردن فایل کوکی شکست خورد: %s", e)
    return jar


DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _download_binary(url: str, dest_path: str, headers=None, cookies=None, timeout=30, retries=4):
    """دانلود stream شده با جلوگیری از ثبت فایل ناقص به‌عنوان فایل نهایی."""
    last_err = None

    base_headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.instagram.com/",
    }
    if headers:
        base_headers.update(headers)

    for attempt in range(1, retries + 1):
        tmp_path = dest_path + ".part"

        try:
            with requests.get(
                url,
                headers=base_headers,
                cookies=cookies,
                timeout=(15, timeout),
                stream=True,
                allow_redirects=True,
            ) as resp:
                resp.raise_for_status()

                content_type = (resp.headers.get("Content-Type") or "").lower()
                if content_type and not content_type.startswith("image/"):
                    raise IOError(f"پاسخ CDN عکس نیست: {content_type}")

                expected_size = int(resp.headers.get("Content-Length", 0) or 0)
                downloaded = 0

                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=128 * 1024):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)

                if expected_size and downloaded != expected_size:
                    raise IOError(
                        f"دانلود ناقص: {downloaded} از {expected_size} بایت دریافت شد"
                    )

                if downloaded < 1024:
                    raise IOError(f"فایل دریافت‌شده خیلی کوچک است: {downloaded} بایت")

            os.replace(tmp_path, dest_path)

            logger.info(
                "دانلود کامل %s: %d بایت دریافت شد (Content-Length: %d)",
                url, downloaded, expected_size
            )
            return dest_path

        except Exception as e:
            last_err = e
            logger.warning(
                "تلاش %d/%d برای دانلود %s ناموفق بود: %s",
                attempt, retries, url, e
            )

            for candidate in (tmp_path, dest_path):
                if os.path.exists(candidate):
                    try:
                        os.remove(candidate)
                    except OSError:
                        pass

            if attempt < retries:
                time.sleep(min(2 * attempt, 6))

    raise last_err


def _strip_instagram_crop(image_url: str) -> str:
    """
    پارامتر stp لینک‌های عکس اینستاگرام یه بخش کراپ (مثل c288.0.864.864a_) داره که
    فقط یه مربع کوچیک از وسط عکس رو نگه می‌داره. با حذف این بخش، معمولاً CDN
    نسخه‌ی کامل و بدون بریدگی عکس رو برمی‌گردونه.
    """
    return re.sub(r"c\d+\.\d+\.\d+\.\d+a_", "", image_url)


def _extract_og_image(url: str):
    """
    Fallback نهایی برای استخراج URL عکس از HTML صفحه‌ی پست (بدون وابستگی به yt-dlp).
    این تابع وقتی صدا زده می‌شه که yt-dlp نتونسته URL عکسی برگردونه یا اصلاً با خطا
    متوقف شده باشه.
    """
    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": DEFAULT_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.instagram.com/",
            },
            cookies=_load_cookie_jar(),
            timeout=(15, 30),
        )
        resp.raise_for_status()
    except Exception as e:
        logger.warning("گرفتن صفحه برای fallback عکس شکست خورد: %s", e)
        return None

    html = resp.text

    patterns = [
        r'"display_url"\s*:\s*"([^"]+)"',
        r'"thumbnail_src"\s*:\s*"([^"]+)"',
        r'"display_src"\s*:\s*"([^"]+)"',
        r'property="og:image"\s+content="([^"]+)"',
        r'content="([^"]+)"\s+property="og:image"',
        r'<meta[^>]+name="twitter:image"[^>]+content="([^"]+)"',
        r'<meta[^>]+property="twitter:image"[^>]+content="([^"]+)"',
    ]

    for pattern in patterns:
        match = re.search(pattern, html, re.I | re.S)
        if match:
            raw_url = match.group(1)
            try:
                return json.loads(f'"{raw_url}"')
            except Exception:
                return raw_url.replace("\\/", "/").replace("&amp;", "&")

    # آخرین تلاش: URLهای مستقیم CDN اینستاگرام که داخل پاسخ HTML/JSON دیده می‌شوند.
    cdn_matches = re.findall(
        r'https?:\\/\\/(?:scontent[^"\'\s<]+|[^"\'\s<]*cdninstagram\.com[^"\'\s<]+)',
        html,
        re.I,
    )
    if cdn_matches:
        cleaned = [
            x.replace("\\/", "/").replace("&amp;", "&")
            for x in cdn_matches
        ]
        return max(cleaned, key=len)

    return None


def _collect_instagram_image_urls(info) -> list:
    """از دیکشنری metadata که yt-dlp برمی‌گردونه، همه‌ی URLهای عکس ممکن رو جمع می‌کنه."""
    image_urls = []

    def add_url(value):
        if isinstance(value, str):
            value = value.strip()
            if value.startswith(("http://", "https://")) and value not in image_urls:
                image_urls.append(value)

    def collect(item):
        if not isinstance(item, dict):
            return

        # URLهای رایج metadata
        add_url(item.get("display_url"))
        add_url(item.get("thumbnail"))
        add_url(item.get("thumbnail_url"))

        for thumb in item.get("thumbnails") or []:
            if isinstance(thumb, dict):
                add_url(thumb.get("url"))

        # برخی پاسخ‌های خام Instagram/yt-dlp
        image_versions = item.get("image_versions2")
        if isinstance(image_versions, dict):
            for candidate in image_versions.get("candidates") or []:
                if isinstance(candidate, dict):
                    add_url(candidate.get("url"))

        # پست‌های چندعکسی (carousel/sidecar)
        sidecar = item.get("edge_sidecar_to_children")
        if isinstance(sidecar, dict):
            for edge in sidecar.get("edges") or []:
                collect((edge or {}).get("node"))

        # اگر metadata ساختار entries داشته باشد (پلی‌لیست/carousel)
        for entry in item.get("entries") or []:
            collect(entry)

    collect(info if isinstance(info, dict) else {})

    # URLهای بزرگ‌تر را جلوتر امتحان می‌کنیم (بر اساس ابعاد داخل خود URL، اگر موجود باشد)
    def image_score(u):
        m = re.search(r"[_-](\d{3,5})x(\d{3,5})", u)
        if m:
            return int(m.group(1)) * int(m.group(2))
        return 0

    image_urls.sort(key=image_score, reverse=True)

    # حذف کراپِ احتمالی هر URL برای گرفتن نسخه‌ی کامل عکس
    return [_strip_instagram_crop(u) for u in image_urls]


def _download_instagram_photo(url: str, unique_prefix: str):
    """
    دانلود عکس اینستاگرام با چند لایه fallback:
      1) استخراج metadata با yt-dlp (بدون دانلود واقعی: download=False)
      2) اگر yt-dlp با خطا متوقف شد یا URL عکسی برنگردوند -> اسکرپ مستقیم HTML صفحه (og:image)
      3) دانلود واقعی فایل با تایید signature عکس (jpg/png/webp/gif) تا هیچ‌وقت
         صفحه‌ی خطا/HTML به‌جای عکس ذخیره نشه
    این تابع طوری نوشته شده که هیچ‌وقت با خطای خام yt-dlp ("No video formats found" و
    مشابه آن) کرش نکنه؛ همیشه اول fallback رو امتحان می‌کنه و در نهایت خطای واضح می‌ده.
    """
    ydl_opts = {
        "outtmpl": os.path.join(DOWNLOAD_DIR, f"{unique_prefix}_%(id)s.%(ext)s"),
        "noplaylist": False,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
        "nocheckcertificate": True,
        "cookiefile": COOKIE_FILE,
        # عمداً هیچ "format" ی نمی‌دیم و ignore_no_formats_error رو فعال می‌کنیم
        # چون پست عکس خالص هیچ فرمت ویدیویی نداره و نباید همین جا کرش کنه.
        "ignore_no_formats_error": True,
    }

    image_urls = []

    try:
        logger.info("Instagram PHOTO: در حال استخراج metadata (بدون دانلود): %s", url)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)  # عمداً download=False
        if info:
            image_urls = _collect_instagram_image_urls(info)
        logger.info("Instagram PHOTO: %d آدرس عکس از metadata پیدا شد", len(image_urls))
    except Exception as e:
        # نکته‌ی مهم: اینجا دیگه raise نمی‌کنیم! هر خطای yt-dlp (مثل خطای فرمت،
        # rate-limit یا نیاز به لاگین) نباید کل عملیات رو متوقف کنه؛ به‌جاش
        # می‌ریم سراغ fallback اسکرپ مستقیم HTML.
        logger.warning("Instagram PHOTO: استخراج metadata با yt-dlp شکست خورد، رفتن سراغ fallback: %s", e)

    # اگر yt-dlp چیزی نداد (چه به‌خاطر خطا، چه چون واقعاً URL عکسی برنگردوند)
    if not image_urls:
        fallback_url = _extract_og_image(url)
        if fallback_url:
            image_urls = [_strip_instagram_crop(fallback_url)]
            logger.info("Instagram PHOTO: از fallback (og:image) یک آدرس عکس پیدا شد")

    if not image_urls:
        raise RuntimeError("نشد آدرس عکس این پست اینستاگرام رو پیدا کنم. لینک رو چک کن یا بعداً دوباره امتحان کن.")

    img_path = os.path.join(DOWNLOAD_DIR, f"{unique_prefix}_photo.jpg")

    common_kwargs = dict(
        headers={
            "User-Agent": DEFAULT_UA,
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.instagram.com/",
        },
        cookies=_load_cookie_jar(),
        timeout=(15, 30),
        retries=3,
    )

    last_error = None

    for image_url in image_urls:
        try:
            logger.info("Instagram PHOTO: در حال دانلود یک آدرس عکس کاندید")
            _download_binary(image_url, img_path, **common_kwargs)

            # جلوگیری از ذخیره HTML/error page به‌عنوان عکس: چک کردن magic bytes
            with open(img_path, "rb") as f:
                header = f.read(16)

            valid_image = (
                header.startswith(b"\xff\xd8\xff")
                or header.startswith(b"\x89PNG\r\n\x1a\n")
                or (header.startswith(b"RIFF") and header[8:12] == b"WEBP")
                or header.startswith(b"GIF87a")
                or header.startswith(b"GIF89a")
            )

            if not valid_image:
                raise IOError("فایل دریافت‌شده signature معتبر تصویر ندارد.")

            if os.path.getsize(img_path) <= 0:
                raise IOError("فایل تصویر خالی دریافت شد.")

            logger.info("Instagram PHOTO: دانلود با موفقیت انجام شد")
            return [(img_path, "photo")]

        except Exception as e:
            last_error = e
            logger.warning("دانلود یک آدرس عکس شکست خورد، رفتن سراغ کاندید بعدی: %s", e)
            if os.path.exists(img_path):
                try:
                    os.remove(img_path)
                except OSError:
                    pass

    raise last_error or RuntimeError("هیچ‌کدام از آدرس‌های عکس این پست قابل دانلود نبودند.")


def download_media(url: str, media_type: str = "video_audio"):
    """
    نقطه‌ی ورودی موتور دانلود.
    media_type: 'photo' یا 'video_audio'
    خروجی: لیستی از (مسیر فایل, result_kind)
    """
    unique_prefix = uuid.uuid4().hex[:8]

    if media_type == "video_audio":
        return _download_video_with_audio(url, unique_prefix)

    if media_type != "photo":
        raise ValueError(f"media_type نامعتبر است: {media_type}")

    return _download_instagram_photo(url, unique_prefix)


# ---------------- دانلود ویدیو (با صدای سالم داخلش) + استخراج یه فایل صدای جداگانه ----------------
# توجه: به درخواست کاربر، منطق این بخش دست‌نخورده باقی مونده و فقط کامنت‌گذاری/نظم بهتر شده.
def _download_video_with_audio(url: str, unique_prefix: str):
    # فرمت رو طوری انتخاب می‌کنیم که بهترین ویدیو و بهترین صدا با هم merge بشن
    ydl_opts = {
        "outtmpl": os.path.join(DOWNLOAD_DIR, f"{unique_prefix}_%(id)s.%(ext)s"),
        "format": "bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "overwrites": True,
        "concurrent_fragment_downloads": 8,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "nocheckcertificate": True,
        "cookiefile": COOKIE_FILE,
    }

    logger.info("YT-DLP: شروع دانلود %s", url)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        logger.info("YT-DLP: شروع extract_info")
        info = ydl.extract_info(url, download=True)
        logger.info("YT-DLP: extract_info موفق شد")

        raw_path = ydl.prepare_filename(info)
        logger.info("YT-DLP: مسیر فایل خام: %s", raw_path)

    # بعد از merge توسط yt-dlp/ffmpeg، پسوند نهایی فایل mp4 هست
    video_path = os.path.splitext(raw_path)[0] + ".mp4"

    if not os.path.exists(video_path):
        # بعضی لینک‌ها ممکنه خروجی نهایی دقیقاً mp4 نباشه
        if os.path.exists(raw_path):
            video_path = raw_path
        else:
            raise FileNotFoundError("فایل ویدیو بعد از دانلود پیدا نشد.")

    results = [(video_path, "video")]

    # استخراج فایل MP3 از روی همان ویدیوی دانلودشده
    audio_path = os.path.splitext(video_path)[0] + "_audio.mp3"

    extract_cmd = [
        "ffmpeg",
        "-y",
        "-i", video_path,
        "-vn",
        "-acodec", "libmp3lame",
        "-ar", "44100",
        "-ab", "192k",
        audio_path,
    ]

    try:
        result = subprocess.run(
            extract_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
        )
    except FileNotFoundError:
        raise FileNotFoundError("ffmpeg روی سیستم پیدا نشد؛ مطمئن شو نصب و در PATH هست.")

    if (
        result.returncode == 0
        and os.path.exists(audio_path)
        and os.path.getsize(audio_path) > 0
    ):
        results.append((audio_path, "audio"))
    else:
        stderr_text = result.stderr.decode("utf-8", errors="ignore").lower()

        if (
            "does not contain any stream" not in stderr_text
            and "output file is empty" not in stderr_text
        ):
            logger.warning(
                "استخراج صدا ناموفق بود برای %s: %s",
                video_path, stderr_text[-300:]
            )

        if os.path.exists(audio_path):
            os.remove(audio_path)

    return results


# ============================================================
# 6) ارسال فایل بر اساس نوعش
# ============================================================
async def send_file(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path: str, result_kind: str = "video"):
    ext = file_path.split(".")[-1].lower()
    caption = f"🤖 @{context.bot.username}"

    with open(file_path, "rb") as f:
        if result_kind == "audio" or ext in ["mp3", "m4a", "wav"]:
            await update.message.reply_audio(f, caption=caption)
        elif result_kind == "photo" or ext in ["jpg", "jpeg", "png", "webp"]:
            await update.message.reply_photo(f, caption=caption)
        elif ext in ["mp4", "mkv", "webm", "mov"]:
            await update.message.reply_video(f, caption=caption)
        else:
            await update.message.reply_document(f, caption=caption)


# ============================================================
# 7) هندلر خطای سراسری
# ============================================================
async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("خطای مدیریت‌نشده رخ داد", exc_info=context.error)


# ---------------- تنظیم منوی دستورات (☰) کنار باکس تایپ ----------------
async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "🔄 شروع دوباره / تنظیمات جدید | Restart / new settings"),
        BotCommand("type", "🔁 تغییر نوع دانلود | Change download type"),
        BotCommand("newlink", "🆕 ارسال لینک جدید (همون عملیات قبلی) | Send new link (same operation)"),
    ])
    await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())


# ============================================================
# 8) Keep-Alive (Flask) — برای Render (پلن رایگان) + UptimeRobot
#    این بخش عیناً حفظ شده تا پینگ‌های UptimeRobot بدون مشکل ادامه پیدا کنن.
# ============================================================
keep_alive = Flask(__name__)


@keep_alive.route("/")
def home():
    return "Bot is alive!"


def run_keep_alive():
    keep_alive.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
    )


Thread(target=run_keep_alive, daemon=True).start()


# ============================================================
# 9) اجرای ربات
# ============================================================
def main():
    persistence = PicklePersistence(filepath=PERSISTENCE_FILE)

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .persistence(persistence)
        .concurrent_updates(True)
        .rate_limiter(AIORateLimiter(max_retries=3))
        .read_timeout(120)
        .write_timeout(120)
        .connect_timeout(60)
        .pool_timeout(60)
        .post_init(post_init)
        .build()
    )

    lang_regex = "^(" + "|".join(re.escape(x) for x in LANG_LABEL_TO_CODE) + ")$"
    type_regex = "^(" + "|".join(re.escape(x) for x in TYPE_LABEL_TO_TYPE) + ")$"
    start_choice_regex = "^(" + "|".join(re.escape(x) for x in START_CHOICE_LABEL_TO_ACTION) + ")$"

    # این هندلر روی هر پیام ورودی (متن، دستور، هرچی) با اولویت بالاتر از همه اجرا می‌شه
    # و قبل از پردازش پیام جدید، پیام‌های متنی ربات از نوبت قبل رو پاک می‌کنه
    app.add_handler(MessageHandler(filters.ALL, cleanup_bot_messages), group=-1)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("type", change_type))
    app.add_handler(CommandHandler("newlink", new_link))
    app.add_handler(MessageHandler(filters.Regex(lang_regex), lang_chosen))
    app.add_handler(MessageHandler(filters.Regex(type_regex), type_chosen))
    app.add_handler(MessageHandler(filters.Regex(start_choice_regex), start_choice_chosen))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_error_handler(global_error_handler)

    logger.info("ربات روشن شد و منتظر پیامه...")
    app.run_polling()


if __name__ == "__main__":
    main()
