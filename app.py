import telebot
from telebot import types
from flask import Flask, request
import os
import sys
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

# منع أخطاء ascii عند طباعة العربية في بيئات بدون UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def safe_print(*args, **kwargs):
    text = " ".join(str(a) for a in args)
    try:
        print(text, **kwargs)
        return
    except Exception:
        pass
    try:
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace") + b"\n")
    except Exception:
        try:
            print(text.encode("ascii", errors="replace").decode("ascii"), **kwargs)
        except Exception:
            pass


def ascii_error(e):
    return f"{type(e).__name__}: {e}".encode("ascii", errors="replace").decode("ascii")

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
GMAIL_EMAIL = os.getenv("GMAIL_EMAIL") or os.getenv("EMAIL")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD") or os.getenv("PASSWORD")
IMAP_SERVER = os.getenv("IMAP_SERVER", "imap.gmail.com")
IMAP_TIMEOUT_SECONDS = int(os.getenv("IMAP_TIMEOUT_SECONDS", "20"))
MAIL_SEARCH_LIMIT = int(os.getenv("MAIL_SEARCH_LIMIT", "20"))
INITIAL_ADMIN = (os.getenv("INITIAL_ADMIN") or "").strip().lstrip("@")
bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)


def open_gmail_connection():
    if not GMAIL_EMAIL or not GMAIL_APP_PASSWORD:
        raise RuntimeError(
            "GMAIL_EMAIL and GMAIL_APP_PASSWORD are not set in .env. "
            "Use a Google App Password."
        )

    socket.setdefaulttimeout(IMAP_TIMEOUT_SECONDS)
    try:
        conn = imaplib.IMAP4_SSL(IMAP_SERVER, timeout=IMAP_TIMEOUT_SECONDS)
    except TypeError:
        conn = imaplib.IMAP4_SSL(IMAP_SERVER)
    conn.login(GMAIL_EMAIL, GMAIL_APP_PASSWORD)
    try:
        conn.enable("UTF8=ACCEPT")
    except Exception:
        pass
    return conn


def init_db():
    """
    تهيئة وإضافة فهارس (indexes) فريدة لتحسين أداء MongoDB
    """
    admins_coll.create_index("username", unique=True)
    users_coll.create_index("username", unique=True)
    accounts_for_sale_coll.create_index("account")
    subscribers_coll.create_index("chat_id", unique=True)
    if INITIAL_ADMIN:
        add_admin(INITIAL_ADMIN)
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
# تخزين مؤقت للرموز والروابط لتسريع الاستجابة ومنع تكرار البحث
# ----------------------------------
recent_code_cache = {}
CACHE_TTL = 20  # ثانية

def get_cached_result(account, req_type):
    key = (str(account).lower().strip(), str(req_type))
    if key in recent_code_cache:
        val, ts = recent_code_cache[key]
        if time.time() - ts < CACHE_TTL and val and val != "طلبك غير موجود.":
            return val
    return None

def set_cached_result(account, req_type, result):
    if result and result != "طلبك غير موجود." and not str(result).startswith("Error"):
        key = (str(account).lower().strip(), str(req_type))
        recent_code_cache[key] = (result, time.time())

# ----------------------------------
# دوال مساعدة
# ----------------------------------

def clean_text(text):
    if not text:
        return ""
    return str(text).strip()


def get_telegram_username(user) -> str:
    if not user or not user.username:
        return ""
    return clean_text(user.username).lstrip("@")

def retry_gmail_connection():
    global mail
    for attempt in range(3):
        try:
            mail = open_gmail_connection()
            safe_print("Gmail IMAP connected.")
            return True
        except Exception as e:
            safe_print(f"Gmail connect failed (attempt {attempt + 1}): {ascii_error(e)}")
            time.sleep(2)
    safe_print("Gmail reconnect failed after retries.")
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

        # دائماً utf-8 أولاً لتجنب مشاكل charset=ascii مع محتوى عربي
        text = payload.decode("utf-8", errors="ignore")
        if not text.strip():
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="ignore")
            except Exception:
                text = payload.decode("latin-1", errors="ignore")

        if content_type == "text/html":
            try:
                text = BeautifulSoup(text, "html.parser").get_text(" ")
            except Exception:
                text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
                text = re.sub(r"(?s)<[^>]+>", " ", text)
                text = re.sub(r"&\w+;", " ", text)
        texts.append(text)
    return "\n".join(texts)


def extract_message_text_plain(msg):
    """استخراج نص الإيميل بدون BeautifulSoup (مسار آمن لرموز التحقق)."""
    texts = []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        text = payload.decode("utf-8", errors="ignore")
        if content_type == "text/html":
            text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
            text = re.sub(r"(?s)<[^>]+>", " ", text)
            text = re.sub(r"&nbsp;", " ", text, flags=re.I)
            text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))) if int(m.group(1)) < 0x110000 else " ", text)
            text = re.sub(r"&\w+;", " ", text)
        texts.append(text)
    return "\n".join(texts)


def extract_code_from_body(body_text, code_length=None):
    if not body_text:
        return None

    if code_length == 6:
        # كود 6 أرقام مفصول بمسافات: 1 8 1 0 7 9
        spaced = re.search(r"(?<!\d)(?:\d[ \t\u00a0]+){5}\d(?!\d)", body_text)
        if spaced:
            return re.sub(r"\D", "", spaced.group(0))
        # كود 6 أرقام متصل
        matches = re.findall(r"(?<!\d)(\d{6})(?!\d)", body_text)
        if matches:
            return matches[0]

    if code_length == 4:
        # كود 4 أرقام متصل أو مفصول بمسافات مع استبعاد السنوات
        spaced = re.search(r"(?<!\d)(?:\d[ \t\u00a0]+){3}\d(?!\d)", body_text)
        if spaced:
            c = re.sub(r"\D", "", spaced.group(0))
            if c not in ("2023", "2024", "2025", "2026", "2027"):
                return c
        matches = re.findall(r"(?<!\d)(\d{4})(?!\d)", body_text)
        for m in matches:
            if m not in ("2023", "2024", "2025", "2026", "2027"):
                return m

    # في حال لم يحدد الطول أو لم نجد
    matches_6 = re.findall(r"(?<!\d)(\d{6})(?!\d)", body_text)
    if matches_6:
        return matches_6[0]

    matches_4 = re.findall(r"(?<!\d)(\d{4})(?!\d)", body_text)
    for m in matches_4:
        if m not in ("2023", "2024", "2025", "2026", "2027"):
            return m

    match = re.search(r"(?<!\d)(\d{4,8})(?!\d)", body_text)
    if match and match.group(1) not in ("2023", "2024", "2025", "2026", "2027"):
        return match.group(1)

    spaced = re.search(r"(?<!\d)(?:\d(?:[ \t\u00a0]+)){3,7}\d(?!\d)", body_text)
    if spaced:
        c = re.sub(r"\D", "", spaced.group(0))
        if c not in ("2023", "2024", "2025", "2026", "2027"):
            return c

    return None


def extract_verification_code(body_text):
    return extract_code_from_body(body_text)


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
    body_lower = body_text.lower() if isinstance(body_text, str) else ""
    return account in body_lower or account in header_text


def subject_has_keyword(subject, keywords):
    subject = (subject or "").lower()
    return any((keyword or "").lower() in subject for keyword in keywords)


def search_gmail_messages(account, subject_keywords=None):
    account = (account or "").strip().encode("ascii", errors="ignore").decode("ascii").strip()
    if not account:
        return []

    status, _ = mail.select("INBOX")
    if status != "OK":
        raise RuntimeError("Failed to open Gmail INBOX.")

    data = None
    account_clean = account.strip().lower()

    # 1. البحث السريع في رسائل آخر يومين (أسرع بكثير)
    for query in (f"to:{account_clean} newer_than:2d", f"deliveredto:{account_clean} newer_than:2d"):
        safe_print(f"Gmail search: {query}")
        try:
            status, data = mail.search(None, b"X-GM-RAW", query.encode("ascii"))
            if status == "OK" and data and data[0]:
                break
        except Exception as e:
            safe_print(f"Gmail X-GM-RAW failed for {query}: {ascii_error(e)}")
            data = None

    # 2. في حال لم نجد رسائل حديثة، نبحث في كل الرسائل
    if not data or not data[0]:
        for query in (f"to:{account_clean}", f"deliveredto:{account_clean}"):
            safe_print(f"Gmail search all: {query}")
            try:
                status, data = mail.search(None, b"X-GM-RAW", query.encode("ascii"))
                if status == "OK" and data and data[0]:
                    break
            except Exception as e:
                safe_print(f"Gmail search failed for {query}: {ascii_error(e)}")
                data = None

    # 3. خطة بديلة أخيرة
    if not data or not data[0]:
        safe_print(f"Gmail fallback search: TO {account}")
        status, data = mail.search(None, b"TO", account.encode("ascii"))
        if status != "OK" or not data or not data[0]:
            return []

    mail_ids = data[0].split()[-MAIL_SEARCH_LIMIT:]
    safe_print(f"Inspecting {len(mail_ids)} Gmail messages.")
    return list(reversed(mail_ids))


def fetch_gmail_message(mail_id):
    status, msg_data = mail.fetch(mail_id, b"(RFC822)")
    if status != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
        return None
    return email.message_from_bytes(msg_data[0][1])


def extract_link_from_message(msg, button_text):
    button_keywords = button_text if isinstance(button_text, (list, tuple)) else [button_text]
    for part in msg.walk():
        if part.get_content_type() != "text/html":
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        html_content = payload.decode("utf-8", errors="ignore")
        try:
            soup = BeautifulSoup(html_content, "html.parser")
        except Exception:
            continue
        for anchor in soup.find_all("a", href=True):
            txt = anchor.get_text().strip().lower()
            if any(btn.lower() in txt for btn in button_keywords):
                return anchor["href"]
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            if "netflix.com" in href.lower() and any(kw in href.lower() for kw in ("nftoken", "password", "household", "travel", "code")):
                return href
    return None


def extract_approval_link_from_message(msg, button_keywords=None):
    if not msg:
        return None
    if not button_keywords:
        button_keywords = [
            "الموافقة على تسجيل الدخول",
            "الموافقة",
            "approve sign-in",
            "approve sign in",
            "approve"
        ]
    for part in msg.walk():
        if part.get_content_type() != "text/html":
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        html_content = payload.decode("utf-8", errors="ignore")
        try:
            soup = BeautifulSoup(html_content, "html.parser")
        except Exception:
            continue

        # 1. البحث بنص الزر الصريح مع تجنب أزرار الرفض
        for a in soup.find_all("a", href=True):
            txt = a.get_text().strip().lower()
            href = a["href"].lower()
            if any(rej in txt for rej in ["رفض", "reject", "cancel", "deny"]):
                continue
            if any(rej in href for rej in ["reject", "cancel", "deny", "denied", "unsubscribe"]):
                continue
            if any(btn.lower() in txt for btn in button_keywords):
                return a["href"]

        # 2. خطة بديلة: البحث عن روابط netflix المخصصة للموافقة مع استبعاد الرفض
        for a in soup.find_all("a", href=True):
            href = a["href"]
            href_lower = href.lower()
            if "netflix.com" in href_lower:
                if any(rej in href_lower for rej in ["reject", "cancel", "deny", "denied", "unsubscribe", "help", "terms", "privacy"]):
                    continue
                if any(kw in href_lower for kw in ("nftoken", "confirm", "travel", "approve", "sign-in", "signin")):
                    return href
    return None


@retry_on_error
def fetch_email_with_link(account, subject_keywords, button_text):
    cache_tag = button_text[0] if isinstance(button_text, (list, tuple)) else button_text
    cached = get_cached_result(account, f"link_{cache_tag}")
    if cached:
        return cached

    if not retry_gmail_connection():
        return "تعذر الاتصال بـ Gmail. تأكد من GMAIL_EMAIL و GMAIL_APP_PASSWORD."
    try:
        safe_print(f"Searching link for account: {account}")
        for mail_id in search_gmail_messages(account, subject_keywords):
            msg = fetch_gmail_message(mail_id)
            if not msg:
                continue

            subject = decode_mime_header(msg.get("Subject", ""))
            if not subject_has_keyword(subject, subject_keywords):
                continue

            body_text = extract_message_text(msg)
            if not message_matches_account(account, msg, body_text):
                continue

            link = extract_link_from_message(msg, button_text)
            if link:
                set_cached_result(account, f"link_{cache_tag}", link)
                return link
        return "طلبك غير موجود."
    except Exception as e:
        safe_print(f"Error while searching Gmail link: {ascii_error(e)}")
        return f"Error fetching Gmail emails: {ascii_error(e)}"
    finally:
        try:
            mail.logout()
        except Exception:
            pass


@retry_on_error
def fetch_email_approval_link(account):
    cached = get_cached_result(account, "approve_link")
    if cached:
        return cached

    subject_keywords = [
        "الموافقة على طلب تسجيل الدخول",
        "طلب تسجيل الدخول الجديد",
        "الموافقة على تسجيل الدخول الجديد",
        "الموافقة على تسجيل الدخول",
        "طلب تسجيل دخول جديد",
        "موافقة على تسجيل الدخول",
        "sign-in request",
        "approve sign-in",
        "approve sign in",
        "new sign-in request",
        "approve new sign-in"
    ]
    if not retry_gmail_connection():
        return "تعذر الاتصال بـ Gmail. تأكد من GMAIL_EMAIL و GMAIL_APP_PASSWORD."
    try:
        safe_print(f"Searching approval link for account: {account}")
        for mail_id in search_gmail_messages(account, subject_keywords):
            msg = fetch_gmail_message(mail_id)
            if not msg:
                continue

            subject = decode_mime_header(msg.get("Subject", ""))
            if not subject_has_keyword(subject, subject_keywords):
                continue

            body_text = extract_message_text(msg)
            if not message_matches_account(account, msg, body_text):
                continue

            link = extract_approval_link_from_message(msg)
            if link:
                set_cached_result(account, "approve_link", link)
                return link
        return "طلبك غير موجود."
    except Exception as e:
        safe_print(f"Error while searching Gmail approval link: {ascii_error(e)}")
        return f"Error fetching Gmail emails: {ascii_error(e)}"
    finally:
        try:
            mail.logout()
        except Exception:
            pass


@retry_on_error
def fetch_email_with_code(account, subject_keywords, code_length=None):
    cache_tag = f"code_{code_length or 'general'}"
    cached = get_cached_result(account, cache_tag)
    if cached:
        return cached

    if not retry_gmail_connection():
        return "تعذر الاتصال بـ Gmail. تأكد من GMAIL_EMAIL و GMAIL_APP_PASSWORD."
    try:
        safe_print(f"Searching code (len={code_length}) for account: {account}")
        for mail_id in search_gmail_messages(account, subject_keywords):
            msg = fetch_gmail_message(mail_id)
            if not msg:
                continue

            subject = decode_mime_header(msg.get("Subject", ""))
            if not subject_has_keyword(subject, subject_keywords):
                continue

            body_text = extract_message_text_plain(msg)
            if not message_matches_account(account, msg, body_text):
                safe_print("Subject matched but account mismatch")
                continue

            code = extract_code_from_body(body_text, code_length)
            if code:
                set_cached_result(account, cache_tag, code)
                return code
        return "طلبك غير موجود."
    except Exception as e:
        import traceback
        tb_tail = traceback.format_exc()[-400:].encode("ascii", errors="replace").decode("ascii")
        safe_print(f"Error while searching Gmail code: {ascii_error(e)}")
        return f"Error fetching Gmail emails: {ascii_error(e)} | {tb_tail}"
    finally:
        try:
            mail.logout()
        except Exception:
            pass


# ----------------------------------
# دالة لمعالجة الطلبات (Thread)
# ----------------------------------
def handle_request_async(chat_id, account, message_text):
    response = None
    try:
        if message_text == 'طلب رابط تحديث السكن':
            response = fetch_email_with_link(
                account,
                ["تحديث السكن", "household", "primary location"],
                ["نعم، أنا قدمت الطلب", "Yes, this was me", "تحديث السكن", "Update Household", "confirm"]
            )
        elif message_text == 'طلب رمز السكن':
            response = fetch_email_with_link(
                account,
                ["رمز الوصول المؤقت", "temporary access", "household", "رمز الوصول"],
                ["الحصول على الرمز", "Get Code", "احصل على الرمز", "رمز الوصول"]
            )
        elif message_text == 'طلب استعادة كلمة المرور':
            response = fetch_email_with_link(
                account,
                ["إعادة تعيين كلمة المرور", "password reset", "reset your password"],
                ["إعادة تعيين كلمة المرور", "Reset Password"]
            )
        elif message_text == 'طلب رمز تسجيل الدخول':
            response = fetch_email_with_code(
                account,
                ["رمز تسجيل الدخول", "sign-in code", "sign in code", "login code"],
                code_length=4
            )
        elif message_text in ('طلب رمز التحقق', 'طلب رمز التأكيد'):
            response = fetch_email_with_code(
                account,
                [
                    "رمز التحقق",
                    "رمز التأكيد",
                    "verification code",
                    "confirmation code",
                    "verify your account",
                    "netflix verification",
                    "تنتهي صلاحيته"
                ],
                code_length=6
            )
        elif message_text in ('طلب الموافقة على تسجيل الدخول', 'طلب الموافقة', 'طلب رابط الموافقة على الدخول'):
            response = fetch_email_approval_link(account)
        elif message_text == 'طلب رابط عضويتك معلقة':
            response = fetch_email_with_link(
                account,
                ["عضويتك في Netflix معلّقة", "معلّقة", "membership on hold", "membership paused"],
                ["إضافة معلومات الدفع", "Update payment", "إعادة تفعيل العضوية", "Restart Membership"]
            )
        else:
            response = "ليس لديك صلاحية لتنفيذ هذا الطلب."
    except Exception as e:
        safe_print(f"Error handling request {message_text}: {ascii_error(e)}")
        response = f"حدث خطأ أثناء معالجة الطلب: {e}"

    if not response:
        response = "لم يتم العثور على نتيجة لهذا الطلب."

    send_formatted_response(chat_id, message_text, response)


def send_formatted_response(chat_id, request_type, response):
    if not response or response == "طلبك غير موجود.":
        if "الموافقة" in request_type:
            bot.send_message(
                chat_id,
                "❌ لم يتم العثور على رسالة طلب الموافقة.\n"
                "يرجى التأكد من إرسال الطلب من الشاشة أو الجهاز أولاً."
            )
        elif "التحقق" in request_type or "التأكيد" in request_type:
            bot.send_message(
                chat_id,
                "❌ لم يتم العثور على رمز التحقق.\n"
                "يرجى التأكد من طلب الرمز من Netflix أولاً."
            )
        elif "تسجيل الدخول" in request_type:
            bot.send_message(
                chat_id,
                "❌ لم يتم العثور على رمز تسجيل الدخول.\n"
                "يرجى التأكد من طلب الرمز من Netflix أولاً."
            )
        else:
            bot.send_message(chat_id, "❌ طلبك غير موجود. يرجى التأكد من إرسال الطلب أولاً.")
        return

    if str(response).startswith("Error") or "تعذر الاتصال" in str(response):
        bot.send_message(chat_id, f"⚠️ {response}")
        return

    # إذا كانت النتيجة رابطاً
    if isinstance(response, str) and (response.startswith("http://") or response.startswith("https://")):
        markup = types.InlineKeyboardMarkup()
        btn_text = "🔗 فتح الرابط"
        if "الموافقة" in request_type:
            btn_text = "🔗 اضغط هنا للموافقة على تسجيل الدخول"
        elif "تحديث السكن" in request_type:
            btn_text = "🔗 فتح رابط تحديث السكن"
        elif "استعادة كلمة المرور" in request_type:
            btn_text = "🔗 فتح رابط استعادة كلمة المرور"
        elif "رمز السكن" in request_type:
            btn_text = "🔗 الحصول على الرمز"
        elif "عضويتك معلقة" in request_type:
            btn_text = "🔗 فتح رابط الدفع والتفعيل"

        markup.add(types.InlineKeyboardButton(text=btn_text, url=response))
        bot.send_message(
            chat_id,
            f"✅ تم جلب الرابط بنجاح:\n\n{response}\n\n👆 اضغط على الزر أدناه لفتح الرابط مباشرة:",
            reply_markup=markup
        )
        return

    # إذا كانت النتيجة كود أرقام
    if isinstance(response, str) and response.isdigit():
        bot.send_message(
            chat_id,
            f"✅ رمزك هو:\n\n`{response}`\n\n(اضغط على الرمز لنسخه)",
            parse_mode="Markdown"
        )
        return

    bot.send_message(chat_id, str(response))

# ----------------------------------
# /start
# ----------------------------------
@bot.message_handler(commands=['start'])
def start_message(message):
    telegram_username = get_telegram_username(message.from_user)
    if not telegram_username:
        return bot.send_message(
            message.chat.id,
            "❌ حسابك بدون username في تليجرام.\n"
            "فعّل اسم المستخدم من: الإعدادات → اسم المستخدم"
        )
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

        markup = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
        # أزرار عامة للمستخدم العادي والأدمن
        btns = [
            types.KeyboardButton('طلب رمز تسجيل الدخول'),
            types.KeyboardButton('طلب رمز التحقق'),
            types.KeyboardButton('طلب الموافقة على تسجيل الدخول'),
            types.KeyboardButton('طلب رمز السكن'),
            types.KeyboardButton('طلب رابط تحديث السكن'),
            types.KeyboardButton('طلب استعادة كلمة المرور'),
            types.KeyboardButton('طلب رابط عضويتك معلقة'),
            types.KeyboardButton('عرض الحسابات المرتبطة بي'),
            types.KeyboardButton('تغيير الحساب'),
            # زر شراء حسابات (يطلب موافقة الأدمن)
            types.KeyboardButton('شراء حسابات للبيع')
        ]
        # الأزرار الإضافية للأدمن
        if is_admin(user_name):
            btns.extend([
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
    'طلب رمز التأكيد',
    'طلب رمز التحقق',
    'طلب الموافقة على تسجيل الدخول',
    'طلب الموافقة',
    'طلب رابط الموافقة على الدخول',
    'طلب استعادة كلمة المرور',
    'طلب رمز تسجيل الدخول',
    'طلب رابط عضويتك معلقة'
])
def handle_requests(message):
    user_name = clean_text(message.from_user.username)
    account = user_accounts.get(user_name)
    if not account:
        bot.send_message(message.chat.id, "لم يتم تحديد حساب بعد. أرسل /start لتحديد الحساب.")
        return

    bot.send_message(message.chat.id, "جاري الطلب...")
    thread = threading.Thread(target=handle_request_async, args=(message.chat.id, account, message.text))
    thread.start()

@bot.message_handler(func=lambda message: message.text in ['تغيير الحساب', '/change_account'])
def change_account_handler(message):
    bot.send_message(message.chat.id, "يرجى إدخال اسم الحساب الجديد الذي ترغب في العمل عليه:")
    bot.register_next_step_handler(message, process_account_name)

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
# إضافة مستخدم جديد
# ----------------------------------
@bot.message_handler(func=lambda message: message.text == "إضافة مستخدم جديد")
def add_new_user_start(message):
    user_name = get_telegram_username(message.from_user)
    if not is_admin(user_name):
        return bot.send_message(message.chat.id, "❌ أنت لست أدمن.")
    bot.send_message(
        message.chat.id,
        "📝 أرسل username المستخدم في تليجرام (بدون @):\n"
        "مثال: jwdxp1"
    )
    bot.register_next_step_handler(message, process_add_new_user_step1)


def process_add_new_user_step1(message):
    username = clean_text(message.text).lstrip("@")
    if not username:
        return bot.send_message(message.chat.id, "❌ اسم المستخدم غير صالح.")
    create_user_if_not_exists(username)
    bot.send_message(
        message.chat.id,
        f"✅ تم إنشاء المستخدم @{username}.\n\n"
        "📝 أرسل الحسابات المراد ربطها (حساب في كل سطر).\n"
        "أو اكتب: تخطي"
    )
    bot.register_next_step_handler(message, process_add_new_user_step2, username)


def process_add_new_user_step2(message, username):
    if clean_text(message.text).lower() in ("تخطي", "skip", "-"):
        return bot.send_message(
            message.chat.id,
            f"✅ المستخدم @{username} جاهز.\n"
            "يمكنك لاحقاً إضافة حسابات عبر «إضافة حسابات لمستخدم»."
        )

    accounts_to_add = [acc.strip() for acc in message.text.strip().split("\n") if acc.strip()]
    if not accounts_to_add:
        return bot.send_message(message.chat.id, "❌ لم يتم إرسال أي حساب.")

    for acc in accounts_to_add:
        add_allowed_user_account(username, acc)
    bot.send_message(
        message.chat.id,
        f"✅ تم إنشاء المستخدم @{username} وربط {len(accounts_to_add)} حساب/حسابات."
    )


# ----------------------------------
# إضافة مشترك
# ----------------------------------
@bot.message_handler(func=lambda message: message.text == "إضافة مشترك")
def add_subscriber_handler(message):
    user_name = get_telegram_username(message.from_user)
    if not is_admin(user_name):
        return bot.send_message(message.chat.id, "❌ أنت لست أدمن.")
    bot.send_message(
        message.chat.id,
        "📝 أرسل Chat ID رقمي (ليس @username).\n\n"
        "📌 كيف تجيب الـ Chat ID:\n"
        "• من لوج البوت عند إرسال /start (chat.id)\n"
        "• أو من بوت @userinfobot\n\n"
        "مثال: 1760682664"
    )
    bot.register_next_step_handler(message, process_subscriber_id)


def process_subscriber_id(message):
    text = clean_text(message.text)
    if text.startswith("@"):
        return bot.send_message(
            message.chat.id,
            "❌ المشترك يحتاج Chat ID رقمي وليس username.\n"
            "استخدم @userinfobot أو شوف chat.id في لوج البوت."
        )
    try:
        chat_id_to_add = int(text)
        add_subscriber(chat_id_to_add)
        bot.send_message(
            message.chat.id,
            f"✅ تم إضافة المشترك {chat_id_to_add} بنجاح إلى قائمة المشتركين."
        )
    except ValueError:
        bot.send_message(
            message.chat.id,
            "❌ الرجاء إدخال رقم صحيح للـ Chat ID.\nمثال: 1760682664"
        )

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
