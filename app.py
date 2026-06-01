import telebot
from telebot import types
from flask import Flask, request
import os
import imaplib
import email
from email.header import decode_header
from bs4 import BeautifulSoup
import re
import time
import threading
import socket

# مكتبة MongoDB
from pymongo import MongoClient
from bson import ObjectId  # لاستخدام ObjectId في الموافقة/الرفض
from dotenv import load_dotenv
import requests

# ----------------------------------
# إعدادات MongoDB
# ----------------------------------
load_dotenv()

MONGO_URI =  os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI)
DB_NAME = "mydatabase"
db = client[DB_NAME]

admins_coll = db["admins"]                 # لتخزين أسماء الأدمن
users_coll = db["users"]                   # بيانات كل مستخدم في مستند واحد {username, accounts:[]}
accounts_for_sale_coll = db["accounts_for_sale"]   # الحسابات المعروضة للبيع
subscribers_coll = db["subscribers"]       # قائمة الـ chat_id للمشتركين
purchase_requests_coll = db["purchase_requests"] 
# طلبات الشراء المعلقة
# ----------------------------------
# إعداد البوت و Flask
# ----------------------------------
TOKEN = os.getenv("TOKEN")
EMAIL = os.getenv("EMAIL")
PASSWORD = os.getenv("PASSWORD")
IMAP_SERVER = os.getenv("IMAP_SERVER")
IMAP_TIMEOUT_SECONDS = int(os.getenv("IMAP_TIMEOUT_SECONDS", "20"))
MAIL_SEARCH_LIMIT = int(os.getenv("MAIL_SEARCH_LIMIT", "10"))
MAIL_PROVIDER = os.getenv("MAIL_PROVIDER", "imap").strip().lower()
INSTADDR_BASE_URL = os.getenv("INSTADDR_BASE_URL", "https://m.kuku.lu").rstrip("/")
INSTADDR_ACCOUNT_ID = os.getenv("INSTADDR_ACCOUNT_ID")
INSTADDR_PASSWORD = os.getenv("INSTADDR_PASSWORD")
INSTADDR_SESSIONHASH = os.getenv("INSTADDR_SESSIONHASH")
INSTADDR_CSRF_TOKEN = os.getenv("INSTADDR_CSRF_TOKEN")
INSTADDR_CSRF_SUBTOKEN = os.getenv("INSTADDR_CSRF_SUBTOKEN")
INSTADDR_SYNC_CONFIRM = os.getenv("INSTADDR_SYNC_CONFIRM", "no")
INSTADDR_SEARCH_BY_ACCOUNT = os.getenv("INSTADDR_SEARCH_BY_ACCOUNT", "no").strip().lower() in ("1", "true", "yes")
INSTADDR_PAGE_COUNT = int(os.getenv("INSTADDR_PAGE_COUNT", "1"))
bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)


def open_imap_connection():
    if not IMAP_SERVER:
        raise RuntimeError("IMAP_SERVER غير مضبوط في ملف .env")
    if not EMAIL or not PASSWORD:
        raise RuntimeError("EMAIL أو PASSWORD غير مضبوطين في ملف .env")

    socket.setdefaulttimeout(IMAP_TIMEOUT_SECONDS)
    try:
        conn = imaplib.IMAP4_SSL(IMAP_SERVER, timeout=IMAP_TIMEOUT_SECONDS)
    except TypeError:
        conn = imaplib.IMAP4_SSL(IMAP_SERVER)
    conn.login(EMAIL, PASSWORD)
    return conn


def init_db():
    """
    تهيئة وإضافة فهارس (indexes) فريدة لتحسين أداء MongoDB
    """
    admins_coll.create_index("username", unique=True)
    users_coll.create_index("username", unique=True)
    accounts_for_sale_coll.create_index("account")
    subscribers_coll.create_index("chat_id", unique=True)
    # لا بأس من ترك purchase_requests بدون unique إذا كل طلب مختلف

# ========== دوال خاصة بالأدمن ==========
def add_admin(username: str):
    """ إضافة أدمن جديد. إذا كان موجودًا مسبقًا، فلن يضيفه مجددًا. """
    try:
        admins_coll.insert_one({"username": username})
    except:
        pass

def is_admin(username: str) -> bool:
    """ التحقق هل المستخدم أدمن أم لا. """
    doc = admins_coll.find_one({"username": username})
    return doc is not None

def remove_admin(username: str):
    """ حذف أدمن من القائمة. """
    admins_coll.delete_one({"username": username})

# ========== دوال خاصة بالمستخدمين (users) ==========
def create_user_if_not_exists(username: str):
    """
    ينشئ مستخدمًا جديدًا بهيكل أساسي إن لم يكن موجودًا:
    {
      "username": "someUser",
      "accounts": []
    }
    """
    user_doc = users_coll.find_one({"username": username})
    if not user_doc:
        users_coll.insert_one({
            "username": username,
            "accounts": []
        })

def add_allowed_user_account(username: str, account: str):
    """
    إضافة حساب واحد لمستخدم داخل قائمة accounts.
    يخزن بشكل كائن {"account": account_string}.
    """
    create_user_if_not_exists(username)
    users_coll.update_one(
        {"username": username},
        {"$push": {"accounts": {"account": account}}}
    )

def get_allowed_accounts(username: str) -> list:
    """
    جلب جميع الحسابات المرتبطة بمستخدم.
    نعيدها كقائمة نصوص فقط.
    """
    user_doc = users_coll.find_one({"username": username})
    if not user_doc or "accounts" not in user_doc:
        return []
    return [acc_obj["account"] for acc_obj in user_doc["accounts"]]

def delete_allowed_accounts(username: str, accounts: list = None):
    """
    حذف حسابات من مستخدم.
    - إذا لم تُمرر accounts -> حذف كل الحسابات.
    - إذا مررت -> حذف الحسابات المحددة فقط.
    """
    user_doc = users_coll.find_one({"username": username})
    if not user_doc:
        return

    if not accounts:
        users_coll.update_one(
            {"username": username},
            {"$set": {"accounts": []}}
        )
    else:
        for acc in accounts:
            users_coll.update_one(
                {"username": username},
                {"$pull": {"accounts": {"account": acc}}}
            )

def get_users_count() -> int:
    """
    إرجاع عدد المستخدمين
    """
    return users_coll.count_documents({})

# ========== دوال خاصة بالحسابات المعروضة للبيع ==========
def add_account_for_sale(account: str):
    accounts_for_sale_coll.insert_one({"account": account})

def add_accounts_for_sale(accounts: list):
    docs = [{"account": acc} for acc in accounts]
    accounts_for_sale_coll.insert_many(docs)

def get_accounts_for_sale() -> list:
    docs = accounts_for_sale_coll.find()
    return [doc["account"] for doc in docs]

def remove_accounts_from_sale(accounts: list):
    for acc in accounts:
        accounts_for_sale_coll.delete_one({"account": acc})

# ========== دوال خاصة بالطلبات (purchase_requests) ==========
def add_purchase_request(username: str, count: int):
    """
    إضافة طلب شراء (معلّق) مستخدم يريد count حساب.
    """
    purchase_requests_coll.insert_one({
        "username": username,
        "count": count,
        "status": "pending",
        "requested_at": time.time()
    })

def get_pending_requests():
    """
    جلب الطلبات بالحالة pending
    """
    return list(purchase_requests_coll.find({"status": "pending"}))

def approve_request(req_id):
    """
    تغيير حالة الطلب إلى "approved"
    """
    purchase_requests_coll.update_one({"_id": req_id}, {"$set": {"status": "approved"}})

def reject_request(req_id):
    """
    تغيير حالة الطلب إلى "rejected"
    """
    purchase_requests_coll.update_one({"_id": req_id}, {"$set": {"status": "rejected"}})

def get_request_by_id(req_id):
    """
    جلب مستند الطلب عبر _id
    """
    return purchase_requests_coll.find_one({"_id": req_id})

# ========== دوال خاصة بالمشتركين (subscribers) ==========
def add_subscriber(chat_id: int):
    subscribers_coll.update_one(
        {"chat_id": chat_id},
        {"$set": {"chat_id": chat_id}},
        upsert=True
    )

def get_subscribers() -> list:
    docs = subscribers_coll.find()
    return [doc["chat_id"] for doc in docs]



# قاموس مؤقت في الذاكرة لتخزين الحساب المحدد لكل مستخدم
user_accounts = {}

# يتم فتح اتصال البريد عند كل طلب حتى لا يعلق اتصال قديم أو منتهي.
mail = None

# ----------------------------------
# دوال مساعدة
# ----------------------------------

def clean_text(text):
    return text.strip()

def retry_imap_connection():
    global mail
    for attempt in range(3):
        try:
            mail = open_imap_connection()
            print("✅ اتصال IMAP ناجح.")
            return True
        except Exception as e:
            print(f"❌ فشل الاتصال (المحاولة {attempt + 1}): {e}")
            time.sleep(2)
    print("❌ فشل إعادة الاتصال بعد عدة محاولات.")
    return False

def retry_on_error(func):
    """ديكورتر لإعادة المحاولة عند حدوث خطأ في جلب الرسائل."""
    def wrapper(*args, **kwargs):
        retries = 3
        for attempt in range(retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if "EOF occurred" in str(e) or "socket" in str(e):
                    time.sleep(2)
                    print(f"Retrying... Attempt {attempt + 1}/{retries}")
                else:
                    return f"Error fetching emails: {e}"
        return "Error: Failed after multiple retries."
    return wrapper


def decode_mime_header(value):
    if not value:
        return ""

    decoded_parts = []
    for part, encoding in decode_header(value):
        if isinstance(part, bytes):
            for charset in (encoding, "utf-8", "windows-1256", "latin-1"):
                if not charset:
                    continue
                try:
                    decoded_parts.append(part.decode(charset, errors="ignore"))
                    break
                except LookupError:
                    continue
        else:
            decoded_parts.append(str(part))
    return "".join(decoded_parts)


def extract_message_text(msg):
    texts = []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue

        payload = part.get_payload(decode=True)
        if not payload:
            continue

        charset = part.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="ignore")
        if content_type == "text/html":
            text = BeautifulSoup(text, 'html.parser').get_text(" ")
        texts.append(text)
    return "\n".join(texts)


def message_matches_account(account, msg, body_text):
    account = account.lower().strip()
    header_values = []
    for header in (
        "To",
        "Delivered-To",
        "X-Original-To",
        "Envelope-To",
        "X-Envelope-To",
        "Apparently-To",
        "Cc",
        "Bcc",
        "Received",
    ):
        header_values.extend(msg.get_all(header, []))

    header_text = " ".join(decode_mime_header(value) for value in header_values).lower()
    return account in body_text.lower() or account in header_text


def subject_has_keyword(subject, keywords):
    subject = subject.lower()
    return any(keyword.lower() in subject for keyword in keywords)


def create_instaddr_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Referer": f"{INSTADDR_BASE_URL}/en.php",
    })

    if INSTADDR_SESSIONHASH:
        session.cookies.set("cookie_sessionhash", INSTADDR_SESSIONHASH, domain="m.kuku.lu")

    response = session.get(f"{INSTADDR_BASE_URL}/en.php", timeout=IMAP_TIMEOUT_SECONDS)
    response.raise_for_status()

    csrf_token = INSTADDR_CSRF_TOKEN or session.cookies.get("cookie_csrf_token")
    csrf_subtoken = INSTADDR_CSRF_SUBTOKEN

    token_match = re.search(r"csrf_token_check=([A-Za-z0-9_-]+)", response.text)
    subtoken_match = re.search(r"csrf_subtoken_check=([A-Za-z0-9_-]+)", response.text)
    if not csrf_token and token_match:
        csrf_token = token_match.group(1)
    if not csrf_subtoken and subtoken_match:
        csrf_subtoken = subtoken_match.group(1)

    if INSTADDR_ACCOUNT_ID and INSTADDR_PASSWORD:
        login_response = session.post(
            f"{INSTADDR_BASE_URL}/index.php",
            data={
                "action": "checkLogin",
                "confirmcode": "",
                "nopost": "1",
                "csrf_token_check": csrf_token or "",
                "csrf_subtoken_check": csrf_subtoken or "",
                "number": INSTADDR_ACCOUNT_ID,
                "password": INSTADDR_PASSWORD,
                "syncconfirm": INSTADDR_SYNC_CONFIRM,
            },
            timeout=IMAP_TIMEOUT_SECONDS,
        )
        if not login_response.text.startswith("OK:"):
            raise RuntimeError(f"فشل تسجيل دخول InstAddr: {login_response.text[:120]}")

    if not csrf_token:
        csrf_token = session.cookies.get("cookie_csrf_token")
    if not csrf_token:
        raise RuntimeError("تعذر جلب CSRF token من InstAddr.")

    return session, csrf_token, csrf_subtoken


def fetch_instaddr_messages(account):
    session, csrf_token, csrf_subtoken = create_instaddr_session()
    mail_ids = []

    print(f"🔎 InstAddr: بدء البحث للحساب {account}")
    for page in range(max(INSTADDR_PAGE_COUNT, 1)):
        params = {
            "page": str(page),
            "nopost": "1",
            "csrf_token_check": csrf_token,
        }
        if INSTADDR_SEARCH_BY_ACCOUNT:
            params["q"] = account
        if csrf_subtoken:
            params["csrf_subtoken_check"] = csrf_subtoken

        inbox_response = session.get(
            f"{INSTADDR_BASE_URL}/recv._ajax.php",
            params=params,
            timeout=IMAP_TIMEOUT_SECONDS,
        )
        inbox_response.raise_for_status()

        soup = BeautifulSoup(inbox_response.text, "html.parser")
        page_mail_ids = [
            link["id"].replace("link_maildata_", "")
            for link in soup.select('a[id^="link_maildata_"]')
        ]

        for mail_id in page_mail_ids:
            if mail_id not in mail_ids:
                mail_ids.append(mail_id)
            if len(mail_ids) >= MAIL_SEARCH_LIMIT:
                break

        if len(mail_ids) >= MAIL_SEARCH_LIMIT or not page_mail_ids:
            break

    print(f"🔎 InstAddr: سيتم فحص {len(mail_ids)} رسالة من الصندوق العام.")

    messages = []
    for mail_id in mail_ids:
        mail_response = session.get(
            f"{INSTADDR_BASE_URL}/datagen.php",
            params={
                "action": "downloadMailData",
                "type": "recv",
                "mailnum": mail_id,
            },
            timeout=IMAP_TIMEOUT_SECONDS,
        )
        if mail_response.status_code != 200 or not mail_response.content:
            continue

        msg = email.message_from_bytes(mail_response.content)
        messages.append({
            "id": mail_id,
            "message": msg,
            "subject": decode_mime_header(msg["Subject"]),
            "body": extract_message_text(msg),
        })

    return messages


def fetch_instaddr_with_link(account, subject_keywords, button_text):
    try:
        for item in fetch_instaddr_messages(account):
            msg = item["message"]
            if not subject_has_keyword(item["subject"], subject_keywords):
                continue
            if not message_matches_account(account, msg, item["body"]):
                continue

            parts = msg.walk() if msg.is_multipart() else [msg]
            for part in parts:
                if part.get_content_type() != "text/html":
                    continue
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                charset = part.get_content_charset() or "utf-8"
                html_content = payload.decode(charset, errors="ignore")
                soup = BeautifulSoup(html_content, 'html.parser')
                for a in soup.find_all('a', href=True):
                    if button_text in a.get_text():
                        return a['href']
        return "طلبك غير موجود."
    except Exception as e:
        print(f"❌ خطأ InstAddr أثناء البحث عن الرابط: {e}")
        return f"Error fetching InstAddr emails: {e}"


def fetch_instaddr_with_code(account, subject_keywords):
    try:
        for item in fetch_instaddr_messages(account):
            msg = item["message"]
            if not subject_has_keyword(item["subject"], subject_keywords):
                continue
            if not message_matches_account(account, msg, item["body"]):
                print(f"⚠️ InstAddr: عنوان مناسب لكن الحساب غير مطابق: {item['subject']}")
                continue

            code_match = re.search(r'\b\d{4,8}\b', item["body"])
            if code_match:
                return code_match.group(0)
        return "طلبك غير موجود."
    except Exception as e:
        print(f"❌ خطأ InstAddr أثناء البحث عن الكود: {e}")
        return f"Error fetching InstAddr emails: {e}"


@retry_on_error
def fetch_email_with_link(account, subject_keywords, button_text):
    if MAIL_PROVIDER == "instaddr":
        return fetch_instaddr_with_link(account, subject_keywords, button_text)

    if not retry_imap_connection():
        return "تعذر الاتصال بالبريد. تأكد من إعدادات EMAIL و PASSWORD و IMAP_SERVER."
    try:
        print(f"🔎 بدء البحث عن رابط للحساب: {account}")
        status, _ = mail.select("inbox")
        if status != "OK":
            return "تعذر فتح صندوق الوارد."

        status, data = mail.search(None, 'ALL')
        if status != "OK" or not data or not data[0]:
            return "لم يتم العثور على رسائل في صندوق الوارد."

        mail_ids = data[0].split()[-MAIL_SEARCH_LIMIT:]
        print(f"🔎 سيتم فحص {len(mail_ids)} رسالة.")
        for mail_id in reversed(mail_ids):
            status, msg_data = mail.fetch(mail_id, "(RFC822)")
            if status != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            subject = decode_mime_header(msg["Subject"])

            if subject_has_keyword(subject, subject_keywords):
                body_text = extract_message_text(msg)
                if not message_matches_account(account, msg, body_text):
                    continue

                for part in msg.walk():
                    if part.get_content_type() == "text/html":
                        html_content = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                        soup = BeautifulSoup(html_content, 'html.parser')
                        for a in soup.find_all('a', href=True):
                            if button_text in a.get_text():
                                return a['href']
        return "طلبك غير موجود."
    except Exception as e:
        print(f"❌ خطأ أثناء البحث عن الرابط: {e}")
        return f"Error fetching emails: {e}"
    finally:
        try:
            mail.logout()
        except Exception:
            pass

@retry_on_error
def fetch_email_with_code(account, subject_keywords):
    if MAIL_PROVIDER == "instaddr":
        return fetch_instaddr_with_code(account, subject_keywords)

    if not retry_imap_connection():
        return "تعذر الاتصال بالبريد. تأكد من إعدادات EMAIL و PASSWORD و IMAP_SERVER."
    try:
        print(f"🔎 بدء البحث عن كود للحساب: {account}")
        status, _ = mail.select("inbox")
        if status != "OK":
            return "تعذر فتح صندوق الوارد."

        status, data = mail.search(None, 'ALL')
        if status != "OK" or not data or not data[0]:
            return "لم يتم العثور على رسائل في صندوق الوارد."

        mail_ids = data[0].split()[-MAIL_SEARCH_LIMIT:]
        print(f"🔎 سيتم فحص {len(mail_ids)} رسالة.")
        for mail_id in reversed(mail_ids):
            status, msg_data = mail.fetch(mail_id, "(RFC822)")
            if status != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            subject = decode_mime_header(msg["Subject"])

            if subject_has_keyword(subject, subject_keywords):
                body_text = extract_message_text(msg)
                if not message_matches_account(account, msg, body_text):
                    print(f"⚠️ تم العثور على عنوان مناسب لكن الحساب غير مطابق: {subject}")
                    continue

                code_match = re.search(r'\b\d{4,8}\b', body_text)
                if code_match:
                    return code_match.group(0)
        return "طلبك غير موجود."
    except Exception as e:
        print(f"❌ خطأ أثناء البحث عن الكود: {e}")
        return f"Error fetching emails: {e}"
    finally:
        try:
            mail.logout()
        except Exception:
            pass

# ----------------------------------
# دالة لمعالجة الطلبات (Thread)
# ----------------------------------
def handle_request_async(chat_id, account, message_text):
    if message_text == 'طلب رابط تحديث السكن':
        response = fetch_email_with_link(account, ["تحديث السكن"], "نعم، أنا قدمت الطلب")
    elif message_text == 'طلب رمز السكن':
        response = fetch_email_with_link(account, ["رمز الوصول المؤقت"], "الحصول على الرمز")
    elif message_text == 'طلب استعادة كلمة المرور':
        response = fetch_email_with_link(account, ["إعادة تعيين كلمة المرور"], "إعادة تعيين كلمة المرور")
    elif message_text == 'طلب رمز تسجيل الدخول':
        response = fetch_email_with_code(account, ["رمز تسجيل الدخول", "login code", "sign in code", "sign-in code"])
    elif message_text == 'طلب رابط عضويتك معلقة':
        response = fetch_email_with_link(account, ["عضويتك في Netflix معلّقة"], "إضافة معلومات الدفع")
    else:
        response = "ليس لديك صلاحية لتنفيذ هذا الطلب."

    bot.send_message(chat_id, response)

# ----------------------------------
# /start
# ----------------------------------
@bot.message_handler(commands=['start'])
def start_message(message):
    telegram_username = clean_text(message.from_user.username)
    create_user_if_not_exists(telegram_username)

    user_accounts_list = get_allowed_accounts(telegram_username)
    if is_admin(telegram_username) or user_accounts_list:
        bot.send_message(message.chat.id, "يرجى إدخال اسم الحساب الذي ترغب في العمل عليه:")
        bot.register_next_step_handler(message, process_account_name)
    else:
        bot.send_message(message.chat.id, "غير مصرح لك باستخدام هذا البوت.")

def process_account_name(message):
    user_name = clean_text(message.from_user.username)
    account_name = clean_text(message.text)
    user_allowed_accounts = get_allowed_accounts(user_name)

    if (account_name in user_allowed_accounts) or is_admin(user_name):
        user_accounts[user_name] = account_name

        markup = types.ReplyKeyboardMarkup(row_width=1)
        # أزرار عامة للمستخدم العادي
        btns = [
            types.KeyboardButton('طلب رابط تحديث السكن'),
            types.KeyboardButton('طلب رمز السكن'),
            types.KeyboardButton('طلب استعادة كلمة المرور'),
            types.KeyboardButton('عرض الحسابات المرتبطة بي'),
            # زر شراء حسابات (يطلب موافقة الأدمن)
            types.KeyboardButton('شراء حسابات للبيع')
        ]
        # الأزرار الإضافية للأدمن
        if is_admin(user_name):
            btns.extend([
                types.KeyboardButton('طلب رمز تسجيل الدخول'),
                types.KeyboardButton('طلب رابط عضويتك معلقة'),
                types.KeyboardButton('إضافة حسابات للبيع'),
                types.KeyboardButton('عرض الحسابات للبيع'),
                types.KeyboardButton('حذف حسابات من المعروضة للبيع'),
                types.KeyboardButton('إرسال رسالة جماعية'),
                types.KeyboardButton('إضافة مستخدم جديد'),
                types.KeyboardButton('إضافة حسابات لمستخدم'),
                types.KeyboardButton('حذف مستخدم مع جميع حساباته'),
                types.KeyboardButton('حذف جزء من حسابات المستخدم'),
                types.KeyboardButton('عرض حسابات مستخدم'),  # زر جديد
                types.KeyboardButton('عرض طلبات الشراء'),    # زر جديد
                types.KeyboardButton('إضافة مشترك'),
                types.KeyboardButton('عرض عدد المستخدمين')
            ])
        markup.add(*btns)
        bot.send_message(message.chat.id, "اختر العملية المطلوبة:", reply_markup=markup)
    else:
        bot.send_message(message.chat.id, "اسم الحساب غير موجود ضمن الحسابات المصرح بها.")

# ----------------------------------
# الطلبات العادية (رابط سكن / رمز سكن / إلخ)
# ----------------------------------
@bot.message_handler(func=lambda message: message.text in [
    'طلب رابط تحديث السكن',
    'طلب رمز السكن',
    'طلب استعادة كلمة المرور',
    'طلب رمز تسجيل الدخول',
    'طلب رابط عضويتك معلقة'
])
def handle_requests(message):
    user_name = clean_text(message.from_user.username)
    account = user_accounts.get(user_name)
    if not account:
        bot.send_message(message.chat.id, "لم يتم تحديد حساب بعد.")
        return

    bot.send_message(message.chat.id, "جاري الطلب...")
    thread = threading.Thread(target=handle_request_async, args=(message.chat.id, account, message.text))
    thread.start()

@bot.message_handler(func=lambda message: message.text == 'عرض الحسابات المرتبطة بي')
def show_user_accounts(message):
    user_name = clean_text(message.from_user.username)
    user_accounts_list = get_allowed_accounts(user_name)
    if user_accounts_list:
        response = "✅ الحسابات المرتبطة بك:\n" + "\n".join(user_accounts_list)
    else:
        response = "❌ لا توجد حسابات مرتبطة بحسابك."
    bot.send_message(message.chat.id, response)


# ----------------------------------
# الحسابات المعروضة للبيع (للأدمن)
# ----------------------------------
@bot.message_handler(func=lambda message: message.text == 'إضافة حسابات للبيع')
def add_accounts_for_sale_handler(message):
    if not is_admin(message.from_user.username):
        return bot.send_message(message.chat.id, "❌ أنت لست أدمن.")
    bot.send_message(message.chat.id, "📝 الرجاء إدخال الحسابات (كل حساب في سطر):")
    bot.register_next_step_handler(message, save_accounts_for_sale)

def save_accounts_for_sale(message):
    new_accounts = message.text.strip().split('\n')
    add_accounts_for_sale(new_accounts)
    bot.send_message(message.chat.id, "✅ تم إضافة الحسابات إلى قائمة البيع بنجاح.")

@bot.message_handler(func=lambda message: message.text in ['عرض الحسابات للبيع', 'عرض الحسابات المعروضة للبيع'])
def show_accounts_for_sale_handler(message):
    if not is_admin(message.from_user.username):
        return bot.send_message(message.chat.id, "❌ أنت لست أدمن.")
    accounts = get_accounts_for_sale()
    if not accounts:
        bot.send_message(message.chat.id, "❌ لا توجد حسابات متوفرة للبيع حاليًا.")
    else:
        accounts_text = "\n".join(accounts)
        bot.send_message(message.chat.id, f"📋 الحسابات المتوفرة للبيع:\n{accounts_text}")

@bot.message_handler(func=lambda message: message.text == 'حذف حسابات من المعروضة للبيع')
def remove_accounts_from_sale_handler(message):
    if not is_admin(message.from_user.username):
        return bot.send_message(message.chat.id, "❌ أنت لست أدمن.")
    bot.send_message(message.chat.id, "📝 أرسل الحسابات التي تريد حذفها من المعروضة للبيع (حساب في كل سطر):")
    bot.register_next_step_handler(message, process_accounts_removal)

def process_accounts_removal(message):
    accounts_to_remove = message.text.strip().split("\n")
    remove_accounts_from_sale(accounts_to_remove)
    bot.send_message(message.chat.id, "✅ تم حذف الحسابات من قائمة البيع بنجاح.")


# ----------------------------------
# إنشاء طلب شراء (لا ينفذ مباشرة) للمستخدم العادي
# ----------------------------------
@bot.message_handler(func=lambda message: message.text == 'شراء حسابات للبيع')
def buy_account_request_start(message):
    """
    عند النقر على زر "شراء حسابات للبيع"،
    نعرض عدد الحسابات المتوفرة ثم يطلب من المستخدم العدد.
    ثم نضيف طلب شراء pending في purchase_requests_coll
    """
    available_accounts = get_accounts_for_sale()
    if not available_accounts:
        return bot.send_message(message.chat.id, "❌ لا توجد حسابات للبيع حالياً.")

    count_available = len(available_accounts)
    bot.send_message(message.chat.id,
                     f"يوجد حالياً {count_available} حساب معروض للبيع.\n"
                     "كم حساباً ترغب بشرائه؟")
    bot.register_next_step_handler(message, process_buy_accounts_count)

def process_buy_accounts_count(message):
    user_name = message.from_user.username
    available_accounts = get_accounts_for_sale()

    if not available_accounts:
        return bot.send_message(message.chat.id, "❌ لا توجد حسابات للبيع حالياً.")

    try:
        count_to_buy = int(message.text.strip())
    except ValueError:
        return bot.send_message(message.chat.id, "❌ الرجاء إدخال رقم صحيح.")

    if count_to_buy <= 0:
        return bot.send_message(message.chat.id, "❌ لا يمكن شراء عدد صفر أو أقل.")
    if count_to_buy > len(available_accounts):
        return bot.send_message(message.chat.id,
                                f"❌ العدد المطلوب ({count_to_buy}) أكبر من المتوفر حالياً ({len(available_accounts)}).")

    add_purchase_request(user_name, count_to_buy)
    bot.send_message(message.chat.id, f"✅ تم إنشاء طلب شراء لعدد {count_to_buy} حساب/حسابات.\n"
                                      "في انتظار موافقة الأدمن.")


# ----------------------------------
# إدارة الطلبات: عرض طلبات الشراء (للأدمن)
# ----------------------------------
@bot.message_handler(func=lambda message: message.text == 'عرض طلبات الشراء')
def show_purchase_requests_handler(message):
    user_name = message.from_user.username
    if not is_admin(user_name):
        return bot.send_message(message.chat.id, "❌ أنت لست أدمن.")
    
    pending = get_pending_requests()
    if not pending:
        return bot.send_message(message.chat.id, "لا توجد طلبات شراء معلّقة حالياً.")

    msg_text = "الطلبات المعلقة:\n\n"
    for req in pending:
        req_id_str = str(req["_id"])
        req_username = req["username"]
        req_count = req["count"]
        req_time = time.ctime(req["requested_at"])
        msg_text += (
            f"ID: {req_id_str}\n"
            f"User: {req_username}\n"
            f"Count: {req_count}\n"
            f"Requested At: {req_time}\n"
            "---------------------------\n"
        )

    bot.send_message(message.chat.id, msg_text)
    bot.send_message(message.chat.id, "أرسل ID الطلب المراد معالجته أو /cancel للإلغاء:")
    bot.register_next_step_handler(message, handle_request_decision)

def handle_request_decision(message):
    if message.text == "/cancel":
        return bot.send_message(message.chat.id, "تم الإنهاء.")
    
    req_id_str = message.text.strip()
    try:
        req_id = ObjectId(req_id_str)
    except:
        return bot.send_message(message.chat.id, "❌ ID غير صالح.")

    req = get_request_by_id(req_id)
    if not req or req["status"] != "pending":
        return bot.send_message(message.chat.id, "❌ لا يوجد طلب بهذا ID أو تم التعامل معه مسبقاً.")

    # نسأل الأدمن: موافقة أم رفض
    markup = types.ReplyKeyboardMarkup(one_time_keyboard=True)
    markup.add("موافقة", "رفض")
    bot.send_message(message.chat.id, "هل تريد الموافقة أم الرفض؟", reply_markup=markup)
    # نخزن req_id في lambda
    bot.register_next_step_handler(message, lambda msg: handle_approval_decision(msg, req_id))

def handle_approval_decision(message, req_id):
    decision = message.text.strip().lower()
    req = get_request_by_id(req_id)
    if not req or req["status"] != "pending":
        return bot.send_message(message.chat.id, "❌ الطلب لم يعد متاحاً (ربما تمت معالجته).")

    if decision == "موافقة":
        approve_request(req_id)
        user_name = req["username"]
        count_to_buy = req["count"]
        available_accounts = get_accounts_for_sale()
        
        if count_to_buy > len(available_accounts):
            reject_request(req_id)
            return bot.send_message(message.chat.id,
                                    f"❌ تعذّرت الموافقة: لا يكفي عدد الحسابات المتوفرة حالياً.")
        
        purchased = available_accounts[:count_to_buy]
        remove_accounts_from_sale(purchased)
        for acc in purchased:
            add_allowed_user_account(user_name, acc)

        bot.send_message(message.chat.id,
                         f"✅ تمت الموافقة على الطلب (ID: {req_id}) وأُضيفت الحسابات للمستخدم {user_name}.")

    elif decision == "رفض":
        reject_request(req_id)
        bot.send_message(message.chat.id, f"❌ تم رفض الطلب (ID: {req_id}).")
    else:
        bot.send_message(message.chat.id, "❌ خيار غير مفهوم. أعد الأمر أو اكتب /cancel للإلغاء.")

# ----------------------------------
# زر عرض حسابات مستخدم (للأدمن)
# ----------------------------------
@bot.message_handler(func=lambda message: message.text == 'عرض حسابات مستخدم')
def admin_show_user_accounts_start(message):
    user_name = message.from_user.username
    if not is_admin(user_name):
        return bot.send_message(message.chat.id, "❌ أنت لست أدمن.")
    
    bot.send_message(message.chat.id, "أدخل اسم المستخدم المراد عرض حساباته:")
    bot.register_next_step_handler(message, process_admin_show_user_accounts)

def process_admin_show_user_accounts(message):
    target_user = message.text.strip()
    accounts = get_allowed_accounts(target_user)
    if not accounts:
        bot.send_message(message.chat.id, f"❌ لا توجد حسابات للمستخدم {target_user}.")
    else:
        resp = f"✅ لدى المستخدم {target_user} الحسابات:\n" + "\n".join(accounts)
        bot.send_message(message.chat.id, resp)

# ----------------------------------
# إضافة مشترك
# ----------------------------------
@bot.message_handler(func=lambda message: message.text == "إضافة مشترك")
def add_subscriber_handler(message):
    user_name = message.from_user.username
    if not is_admin(user_name):
        return bot.send_message(message.chat.id, "❌ أنت لست أدمن.")
    bot.send_message(message.chat.id, "📝 الرجاء إدخال الـ Chat ID المراد إضافته للمشتركين:")
    bot.register_next_step_handler(message, process_subscriber_id)

def process_subscriber_id(message):
    try:
        chat_id_to_add = int(message.text.strip())
        add_subscriber(chat_id_to_add)
        bot.send_message(message.chat.id, f"✅ تم إضافة المشترك {chat_id_to_add} بنجاح إلى قائمة المشتركين.")
    except ValueError:
        bot.send_message(message.chat.id, "❌ الرجاء إدخال رقم صحيح للـ Chat ID.")

# ----------------------------------
# زر عرض عدد المستخدمين
# ----------------------------------
@bot.message_handler(func=lambda message: message.text == "عرض عدد المستخدمين")
def show_users_count(message):
    user_name = message.from_user.username
    if not is_admin(user_name):
        return bot.send_message(message.chat.id, "❌ أنت لست أدمن.")
    count = get_users_count()
    bot.send_message(message.chat.id, f"عدد المستخدمين المسجَّلين حالياً هو: {count}")

# ----------------------------------
# إرسال رسالة جماعية (للأدمن)
# ----------------------------------
@bot.message_handler(func=lambda message: message.text == 'إرسال رسالة جماعية')
def handle_broadcast_request(message):
    user_name = message.from_user.username
    if not is_admin(user_name):
        return bot.send_message(message.chat.id, "❌ أنت لست أدمن.")
    bot.send_message(message.chat.id, "اكتب الرسالة التي تريد إرسالها لجميع المشتركين:")
    bot.register_next_step_handler(message, send_broadcast_message)

def send_broadcast_message(message):
    broadcast_text = message.text
    all_subscribers = get_subscribers()
    for chat_id in all_subscribers:
        try:
            bot.send_message(chat_id, f"📢 رسالة من الإدارة:\n{broadcast_text}")
        except Exception as e:
            print(f"فشل الإرسال إلى {chat_id}: {e}")
    bot.send_message(message.chat.id, "✅ تم إرسال الرسالة إلى جميع المشتركين بنجاح.")
@bot.message_handler(func=lambda message: message.text == 'حذف مستخدم مع جميع حساباته')
def delete_user_all_accounts_start(message):
    user_name = message.from_user.username
    if not is_admin(user_name):
        return bot.send_message(message.chat.id, "❌ أنت لست أدمن.")
    
    bot.send_message(message.chat.id, "📝 الرجاء إدخال اسم المستخدم الذي تريد حذفه مع حساباته:")
    bot.register_next_step_handler(message, process_delete_user_all)

def process_delete_user_all(message):
    user_to_delete = message.text.strip()
    # تحذف كل حساباته (استدعاء دالتك الحالية delete_allowed_accounts دون تمرير قائمة)
    delete_allowed_accounts(user_to_delete)  
    bot.send_message(message.chat.id, f"✅ تم حذف جميع الحسابات من المستخدم '{user_to_delete}' بنجاح.")
    
    # إذا أردت حذف وثيقة المستخدم كاملة من الـDB (users_coll)، أضف:
    # users_coll.delete_one({"username": user_to_delete})
    # bot.send_message(message.chat.id, f"✅ تم حذف المستخدم '{user_to_delete}' نهائيًا من قاعدة البيانات.")
@bot.message_handler(func=lambda message: message.text == 'حذف جزء من حسابات المستخدم')
def delete_part_of_user_accounts_start(message):
    user_name = message.from_user.username
    if not is_admin(user_name):
        return bot.send_message(message.chat.id, "❌ أنت لست أدمن.")
    
    bot.send_message(message.chat.id, "📝 الرجاء إدخال اسم المستخدم:")
    bot.register_next_step_handler(message, process_delete_part_step1)

def process_delete_part_step1(message):
    user_to_edit = message.text.strip()
    current_accounts = get_allowed_accounts(user_to_edit)
    
    if not current_accounts:
        bot.send_message(message.chat.id, f"❌ لا توجد حسابات للمستخدم '{user_to_edit}' أو المستخدم غير موجود.")
        return  # إنهاء مبكرًا أو يمكنك إعادة الطلب
    
    # عرض الحسابات الحالية
    bot.send_message(message.chat.id,
                     f"✅ لدى المستخدم {user_to_edit} الحسابات التالية:\n"
                     + "\n".join(current_accounts)
                     + "\n📝 أرسل الحسابات التي تريد حذفها (حساب في كل سطر):")
    # الانتقال إلى الخطوة التالية
    bot.register_next_step_handler(message, process_delete_part_step2, user_to_edit)

def process_delete_part_step2(message, user_to_edit):
    accounts_to_delete = message.text.strip().split('\n')
    # استدعاء الدالة التي ستحذف هذه الحسابات
    delete_allowed_accounts(user_to_edit, accounts_to_delete)
    bot.send_message(message.chat.id, f"✅ تم حذف الحسابات المطلوبة من المستخدم '{user_to_edit}' بنجاح.")
@bot.message_handler(func=lambda message: message.text == 'إضافة حسابات لمستخدم')
def add_accounts_to_existing_user_start(message):
    """
    الخطوة الأولى: نسأل الأدمن عن اسم المستخدم
    """
    user_name = message.from_user.username
    if not is_admin(user_name):
        return bot.send_message(message.chat.id, "❌ أنت لست أدمن.")
    
    bot.send_message(message.chat.id, "📝 الرجاء إدخال اسم المستخدم:")
    bot.register_next_step_handler(message, process_add_accounts_step1)

def process_add_accounts_step1(message):
    """
    الخطوة الثانية: بعد إدخال اسم المستخدم، نسأله عن الحسابات التي يريد إضافتها
    """
    user_to_edit = message.text.strip()
    create_user_if_not_exists(user_to_edit)
    
    bot.send_message(message.chat.id,
                     f"أرسل الحسابات التي تريد إضافتها للمستخدم {user_to_edit} (حساب في كل سطر):")
    bot.register_next_step_handler(message, process_add_accounts_step2, user_to_edit)

def process_add_accounts_step2(message, user_to_edit):
    """
    الخطوة الثالثة: نأخذ الحسابات المدخلة ونضيفها للمستخدم في DB
    """
    accounts_to_add = message.text.strip().split('\n')
    for acc in accounts_to_add:
        add_allowed_user_account(user_to_edit, acc.strip())
    bot.send_message(message.chat.id, f"✅ تم إضافة الحسابات للمستخدم {user_to_edit} بنجاح.")

# ----------------------------------
# Webhook (إذا كنت ستستعمله)
# ----------------------------------
@app.route('/webhook', methods=['POST'])
def webhook():
    json_string = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_string)
    bot.process_new_updates([update])
    print("DEBUG: Received an update from Telegram Webhook:", json_string)
    return '', 200

# ----------------------------------
# تشغيل السيرفر Flask + تهيئة DB
# ----------------------------------
if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=6000)
