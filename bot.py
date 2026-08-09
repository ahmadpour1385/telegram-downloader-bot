from flask import Flask
from threading import Thread
from threading import Thread
import os
import re
import uuid
import asyncio
import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor
import requests
import yt_dlp
from dotenv import load_dotenv
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

# ---------------- تنظیمات ----------------
load_dotenv()  # مقادیر رو از فایل .env می‌خونه

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN پیدا نشد. یه فایل .env کنار bot.py بساز و توش بنویس:\n"
        "BOT_TOKEN=توکن_ربات_تو"
    )

DOWNLOAD_DIR = "downloads"
PERSISTENCE_FILE = "bot_data.pkl"

# ترد پول اختصاصی برای دانلود، مستقل از تعداد هسته‌ی CPU سرور
DOWNLOAD_EXECUTOR = ThreadPoolExecutor(max_workers=20)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------- متن‌های دو زبانه ----------------
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
        "type_saved": "باشه! حالا لینکت رو برام بفرست 🔗",
        "start_choice": "دوباره خوش اومدی! 👋\nمی‌خوای همون لینک قبلی رو دوباره دانلود کنم یا یه لینک جدید برام بفرستی؟",
        "prev_link_btn": "🔁 لینک قبلی",
        "new_link_btn": "🆕 لینک جدید",
        "send_new_link": "باشه، لینک جدیدت رو برام بفرست 🔗",
        "redownloading_prev": "باشه، دارم همون لینک قبلی رو دوباره برات دانلود می‌کنم 🔄",
        "invalid_link": "این یه لینک معتبر نیست. یه لینک درست بفرست 🔗",
        "need_setup": "اول باید /start رو بزنی و زبان و نوع دانلود رو انتخاب کنی.",
        "downloading": "⏳ در حال دانلود عکس... چند لحظه صبر کن",
        "downloading_video_audio": "⏳🎧 در حال دانلود ویدیو و آماده‌سازی فایل صدا... چند لحظه صبر کن",
        "done": "✅ دانلود انجام شد!",
        "failed": "❌ نشد دانلودش کنم.\nخطا: {err}",
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
        "type_saved": "Got it! Now send me the link 🔗",
        "start_choice": "Welcome back! 👋\nDo you want the same previous link again, or will you send a new one?",
        "prev_link_btn": "🔁 Previous link",
        "new_link_btn": "🆕 New link",
        "send_new_link": "Okay, send me your new link 🔗",
        "redownloading_prev": "Okay, downloading the same previous link again 🔄",
        "invalid_link": "This isn't a valid link. Send a proper link 🔗",
        "need_setup": "First run /start and choose your language and download type.",
        "downloading": "⏳ Downloading the photo... please wait",
        "downloading_video_audio": "⏳🎧 Downloading the video and preparing the audio file... please wait",
        "done": "✅ Download complete!",
        "failed": "❌ Couldn't download it.\nError: {err}",
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


# ==================================================================
# ------- ابزار پاکسازی چت: ردیابی و حذف پیام‌های متنی ربات -------
# ==================================================================
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


# ---------------- دستور /start: انتخاب زبان، یا اگه قبلاً زبان داشته مستقیم بره سراغ انتخاب نوع دانلود ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # هر بار /start زده بشه، از اول شروع می‌کنیم و دوباره سؤال زبان پرسیده می‌شه
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


# ---------------- جواب به سوال «لینک قبلی یا لینک جدید» ----------------
async def start_choice_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action = START_CHOICE_LABEL_TO_ACTION[update.message.text]

    if action == "prev":
        last_url = context.user_data.get("last_url")
        msg = await update.message.reply_text(t(context, "redownloading_prev"), reply_markup=ReplyKeyboardRemove())
        await track_msg(context, msg)
        await process_download(update, context, last_url)
    else:
        msg = await update.message.reply_text(t(context, "send_new_link"), reply_markup=ReplyKeyboardRemove())
        await track_msg(context, msg)


# ---------------- انتخاب زبان (پیام متنی از کیبورد پایین) ----------------
async def lang_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
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


# ---------------- دستور /type: تغییر سریع نوع دانلود بدون نیاز به /start ----------------
async def change_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault("lang", "fa")
    keyboard = ReplyKeyboardMarkup(
        [[t(context, "photo_btn"), t(context, "video_audio_btn")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    msg = await update.message.reply_text(t(context, "choose_type"), reply_markup=keyboard)
    await track_msg(context, msg)


# ---------------- انتخاب نوع دانلود (عکس/ویدیو/صدا) از کیبورد پایین ----------------
async def type_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
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


# ---------------- هندلر اصلی: دریافت لینک ----------------
async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # اگه کاربر هنوز زبان/نوع دانلود رو انتخاب نکرده (کاربر جدید یا داده از دست رفته)
    # به‌جای گیر دادن، مقدار پیش‌فرض می‌ذاریم تا لینک همیشه کار کنه
    context.user_data.setdefault("lang", "fa")
    context.user_data.setdefault("download_type", "video_audio")

    url = update.message.text.strip()

    if not url.startswith("http"):
        msg = await update.message.reply_text(t(context, "invalid_link"))
        await track_msg(context, msg)
        return

    # لینک رو ذخیره می‌کنیم تا اگه بعداً کاربر فقط نوع دانلود رو عوض کرد
    # (مثلاً اول صدا خواست، بعد ویدیو)، لازم نباشه لینک رو دوباره بفرسته
    context.user_data["last_url"] = url

    await process_download(update, context, url)


# ---------------- منطق مشترک دانلود و ارسال فایل ----------------
async def process_download(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    file_paths = []
    status_msg = None
    try:
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
            await send_file(update, file_path, result_kind)
        await status_msg.edit_text(t(context, "done"))
    except Exception as e:
        logger.error(f"خطا در دانلود لینک {url}: {e}", exc_info=True)
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


# ---------------- تابع دانلود با yt-dlp ----------------
def download_media(url: str, media_type: str = "video_audio"):
    """
    media_type: 'photo' یا 'video_audio'
    خروجی: لیستی از (مسیر فایل, result_kind) — چون حالت video_audio دو تا فایل
    (ویدیو + صدای جداگانه) برمی‌گردونه ولی حالت photo فقط یکی.
    """
    unique_prefix = uuid.uuid4().hex[:8]

    if media_type == "video_audio":
        return _download_video_with_audio(url, unique_prefix)

    # ---- حالت عکس ----
    ydl_opts = {
        "outtmpl": os.path.join(DOWNLOAD_DIR, f"{unique_prefix}_%(id)s.%(ext)s"),
        "format": "best[filesize<50M]/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "overwrites": True,
        "concurrent_fragment_downloads": 8,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "nocheckcertificate": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        file_path = ydl.prepare_filename(info)

    ext = file_path.split(".")[-1].lower()
    is_video_file = ext in ["mp4", "mkv", "webm", "mov"]

    # اگه کاربر عکس خواسته ولی چیزی که دانلود شده ویدیوئه، تامبنیلش رو به‌جاش می‌گیریم
    if is_video_file:
        thumbnail_url = info.get("thumbnail")
        if thumbnail_url:
            thumb_path = os.path.join(DOWNLOAD_DIR, f"{unique_prefix}_{info.get('id')}_thumb.jpg")
            resp = requests.get(thumbnail_url, timeout=20)
            resp.raise_for_status()
            with open(thumb_path, "wb") as f:
                f.write(resp.content)
            # فایل ویدیوی اصلی رو پاک می‌کنیم چون کاربر عکس خواسته بود
            if os.path.exists(file_path):
                os.remove(file_path)
            return [(thumb_path, "photo")]

    return [(file_path, "photo")]


# ---------------- دانلود ویدیو (با صدای سالم داخلش) + استخراج یه فایل صدای جداگانه ----------------
def _download_video_with_audio(url: str, unique_prefix: str):
    # فرمت رو طوری انتخاب می‌کنیم که بهترین ویدیو و بهترین صدا با هم merge بشن
    # و merge_output_format=mp4 تضمین می‌کنه خروجی نهایی یه mp4 استانداردِ قابل پخش با صدای سالم باشه
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
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        raw_path = ydl.prepare_filename(info)

    # بعد از merge توسط yt-dlp/ffmpeg، پسوند نهایی فایل mp4 هست
    video_path = os.path.splitext(raw_path)[0] + ".mp4"
    if not os.path.exists(video_path):
        # بعضی لینک‌ها (مثلاً پست عکسی) خروجی ویدیو ندارن؛ همون فایل خام رو برمی‌گردونیم
        if os.path.exists(raw_path):
            video_path = raw_path
        else:
            raise FileNotFoundError("فایل ویدیو بعد از دانلود پیدا نشد.")

    results = [(video_path, "video")]

    # حالا از روی همین فایل ویدیوی دانلودشده (نه با دانلود مجدد از اینترنت)
    # یه فایل mp3 جدا و سالم استخراج می‌کنیم
    audio_path = os.path.splitext(video_path)[0] + "_audio.mp3"
    extract_cmd = [
        "ffmpeg", "-y",
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

    if result.returncode == 0 and os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
        results.append((audio_path, "audio"))
    else:
        # اگه لینک اصلاً صدا نداشته (مثلاً یه ویدیوی بی‌صدا)، خطا نمی‌دیم؛
        # فقط بدون فایل صدا ادامه می‌دیم و همون ویدیو رو می‌فرستیم
        stderr_text = result.stderr.decode("utf-8", errors="ignore").lower()
        if "does not contain any stream" not in stderr_text and "output file is empty" not in stderr_text:
            logger.warning(f"استخراج صدا ناموفق بود برای {video_path}: {stderr_text[-300:]}")
        if os.path.exists(audio_path):
            os.remove(audio_path)

    return results


# ---------------- ارسال فایل بر اساس نوعش ----------------
async def send_file(update: Update, file_path: str, result_kind: str = "video"):
    ext = file_path.split(".")[-1].lower()

    with open(file_path, "rb") as f:
        if result_kind == "audio" or ext in ["mp3", "m4a", "wav"]:
            await update.message.reply_audio(f)
        elif result_kind == "photo" or ext in ["jpg", "jpeg", "png", "webp"]:
            await update.message.reply_photo(f)
        elif ext in ["mp4", "mkv", "webm", "mov"]:
            await update.message.reply_video(f)
        else:
            await update.message.reply_document(f)


# ---------------- هندلر سراسری خطا ----------------
async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("خطای مدیریت‌نشده رخ داد", exc_info=context.error)


# ---------------- تنظیم منوی دستورات (☰) کنار باکس تایپ ----------------
# با این کار کاربر همیشه (بدون نیاز به پاک کردن چت) می‌تونه رو آیکون منو
# بزنه و /start رو انتخاب کنه تا زبان/نوع دانلود رو دوباره تنظیم کنه.
async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "🔄 شروع دوباره / تنظیمات جدید | Restart / new settings"),
        BotCommand("type", "🔁 تغییر نوع دانلود | Change download type"),
    ])
    await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())



# Render Keep Alive
keep_alive = Flask(__name__)

@keep_alive.route("/")
def home():
    return "Bot is alive!"

def run_keep_alive():
    keep_alive.run(
    host="0.0.0.0",
    port=int(os.environ.get("PORT", 10000))
)

Thread(target=run_keep_alive, daemon=True).start()

# ---------------- اجرای ربات ----------------
def main():
    Thread(target=run_web_server, daemon=True).start()
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
    app.add_handler(MessageHandler(filters.Regex(lang_regex), lang_chosen))
    app.add_handler(MessageHandler(filters.Regex(type_regex), type_chosen))
    app.add_handler(MessageHandler(filters.Regex(start_choice_regex), start_choice_chosen))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_error_handler(global_error_handler)

    logger.info("ربات روشن شد و منتظر پیامه...")
    app.run_polling()


if __name__ == "__main__":
    main()
