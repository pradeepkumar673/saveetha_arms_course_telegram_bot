import requests
import time
import hashlib
import json
import os
import threading
import logging
import random
import atexit
from collections import deque
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from flask import Flask, jsonify
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ================ CONFIGURATION ================
BOT_TOKEN = "8741720386:AAGR38shauyIWWtP8A0D13hm5DrxuSJBNVo"
GROUP_CHAT_ID = -1003377710237
LOGIN_URL = "https://arms.sse.saveetha.com/Login.aspx?s=unauth"
ENROLLMENT_URL = "https://arms.sse.saveetha.com/StudentPortal/Enrollment.aspx"
HANDLER_URL = "https://arms.sse.saveetha.com/Handler/Student.ashx"
USERNAME = "192425016"
PASSWORD = "787673ATYTmustang"
CHECK_INTERVAL = 45          # base interval (seconds) - increased to reduce risk
STATE_FILE = "seen_courses.json"
LOG_FILE = "bot.log"
# ================================================

# ---------- Logging setup ----------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ---------- User agent rotation ----------
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
]

# ---------- Session with retry strategy ----------
session = requests.Session()
retries = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[500, 502, 503, 504],
    allowed_methods=["HEAD", "GET", "POST", "PUT", "DELETE", "OPTIONS", "TRACE"]
)
adapter = HTTPAdapter(max_retries=retries)
session.mount('http://', adapter)
session.mount('https://', adapter)
session.headers.update({'User-Agent': random.choice(USER_AGENTS)})

# ---------- Global state ----------
previous_courses = {}        # slot_id -> set of signatures
last_check_timestamp = None
session_alive = False
login_failures = 0           # consecutive login failures
message_timestamps = deque(maxlen=100)   # for rate monitoring

# Mapping slot ID to slot name (from dropdown)
SLOT_MAP = {
    1: "Slot A", 2: "Slot B", 3: "Slot C", 4: "Slot D", 5: "Slot E",
    6: "Slot F", 7: "Slot G", 8: "Slot H", 9: "Slot I", 10: "Slot J",
    11: "Slot K", 12: "Slot L", 13: "Slot M", 14: "Slot N", 15: "Slot O",
    16: "Slot P", 17: "Slot Q", 18: "Slot R", 19: "Slot S", 20: "Slot T"
}

# ---------- State persistence ----------
def load_state():
    global previous_courses
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                data = json.load(f)
                previous_courses = {int(k): set(v) for k, v in data.items()}
            logger.info(f"Loaded previous courses from {STATE_FILE}")
        else:
            for slot_id in SLOT_MAP:
                previous_courses.setdefault(slot_id, set())
            logger.info("No state file found, starting fresh.")
    except Exception as e:
        logger.error(f"Failed to load state: {e}")
        previous_courses = {slot_id: set() for slot_id in SLOT_MAP}

def save_state():
    try:
        data = {str(k): list(v) for k, v in previous_courses.items()}
        with open(STATE_FILE, 'w') as f:
            json.dump(data, f, indent=2)
        logger.debug("State saved.")
    except Exception as e:
        logger.error(f"Failed to save state: {e}")

atexit.register(save_state)

# ---------- Telegram message sender with rate limiting ----------
last_telegram_time = 0
def send_telegram_message(message, parse_mode='HTML'):
    global last_telegram_time, message_timestamps
    # rate limit: at least 1.2 seconds between messages
    now = time.time()
    if now - last_telegram_time < 1.2:
        time.sleep(1.2 - (now - last_telegram_time))
    # daily/hourly cap check
    message_timestamps.append(now)
    # count messages in last hour
    one_hour_ago = now - 3600
    recent = [t for t in message_timestamps if t > one_hour_ago]
    if len(recent) > 50:
        logger.warning(f"More than 50 messages in last hour ({len(recent)}). Sleeping 5 minutes.")
        time.sleep(300)   # cooldown
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {'chat_id': GROUP_CHAT_ID, 'text': message, 'parse_mode': parse_mode}
    try:
        resp = session.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        last_telegram_time = time.time()
        logger.info(f"Telegram message sent: {message[:50]}...")
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")

# ---------- HTTP request wrapper with retry ----------
def http_request(method, url, **kwargs):
    for attempt in range(3):
        try:
            if method.lower() == 'get':
                response = session.get(url, **kwargs)
            elif method.lower() == 'post':
                response = session.post(url, **kwargs)
            else:
                raise ValueError(f"Unsupported method: {method}")
            response.raise_for_status()
            return response
        except Exception as e:
            wait = (2 ** attempt) + random.uniform(0, 1)
            logger.warning(f"HTTP {method} to {url} failed (attempt {attempt+1}/3): {e}. Retrying in {wait:.2f}s")
            time.sleep(wait)
    logger.error(f"All retries exhausted for {method} {url}")
    return None

# ---------- Login success verification (robust) ----------
def verify_login_success(response_text):
    """Check multiple indicators to confirm we are logged in."""
    soup = BeautifulSoup(response_text, 'html.parser')
    # 1. Student name present
    profile_span = soup.find('span', {'id': 'spprofile'})
    if profile_span and USERNAME in profile_span.text:
        return True
    # 2. Absence of login form fields
    if 'ttxusername' not in response_text and 'ttxpassword' not in response_text and 'btnlogin' not in response_text:
        # 3. Presence of dashboard keywords
        dashboard_keywords = ['Enrollment', 'My Course', 'Academic Record', 'Welcome']
        if any(keyword in response_text for keyword in dashboard_keywords):
            return True
    # 4. Check for a known element like the slot dropdown
    if soup.find('select', {'id': 'cphbody_ddlslot'}):
        return True
    return False

# ---------- Login ----------
def login():
    global session_alive, login_failures
    logger.info("Attempting login...")
    # Rotate user agent on each login attempt
    session.headers.update({'User-Agent': random.choice(USER_AGENTS)})
    try:
        # GET login page
        login_page_resp = http_request('get', LOGIN_URL, timeout=15)
        if not login_page_resp:
            login_failures += 1
            return False
        soup = BeautifulSoup(login_page_resp.text, 'html.parser')

        viewstate = soup.find('input', {'name': '__VIEWSTATE'})
        eventvalidation = soup.find('input', {'name': '__EVENTVALIDATION'})
        viewstategen = soup.find('input', {'name': '__VIEWSTATEGENERATOR'})

        login_payload = {
            'ttxusername': USERNAME,
            'ttxpassword': PASSWORD,
            'btnlogin': 'Login',
        }
        if viewstate:
            login_payload['__VIEWSTATE'] = viewstate['value']
        if eventvalidation:
            login_payload['__EVENTVALIDATION'] = eventvalidation['value']
        if viewstategen:
            login_payload['__VIEWSTATEGENERATOR'] = viewstategen['value']

        # POST login
        post_resp = http_request('post', LOGIN_URL, data=login_payload, timeout=15)
        if not post_resp:
            login_failures += 1
            return False

        # Verify by accessing enrollment page and using robust check
        test_resp = http_request('get', ENROLLMENT_URL, timeout=15)
        if not test_resp:
            login_failures += 1
            return False

        if verify_login_success(test_resp.text):
            session_alive = True
            login_failures = 0
            logger.info("Login successful, session verified.")
            send_telegram_message(f"✅ Bot (re)started and logged in successfully at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            return True
        else:
            logger.warning("Login verification failed: no reliable indicator found.")
            login_failures += 1
            return False
    except Exception as e:
        logger.error(f"Login exception: {e}")
        login_failures += 1
        return False

# ---------- Session aliveness check (non-recursive) ----------
def ensure_session():
    global session_alive
    for attempt in range(3):
        if session_alive:
            try:
                resp = session.get(ENROLLMENT_URL, timeout=10, allow_redirects=False)
                if resp.status_code == 302 or 'Login.aspx' in resp.headers.get('Location', ''):
                    logger.warning("Session appears expired (redirect to login).")
                    session_alive = False
                elif resp.status_code == 200 and 'ttxusername' in resp.text:
                    logger.warning("Login form detected in response, session dead.")
                    session_alive = False
                else:
                    # still possibly alive, but we need deeper check
                    if verify_login_success(resp.text):
                        return True
                    else:
                        logger.warning("Session content missing expected elements, assuming dead.")
                        session_alive = False
            except Exception as e:
                logger.error(f"Session check failed: {e}")
                session_alive = False

        if not session_alive:
            logger.info(f"Session dead, attempting re-login (attempt {attempt+1}/3).")
            if login():
                return True
            time.sleep(5 * (attempt + 1))
    return False

# ---------- Fetch courses for a slot ----------
def get_courses_for_slot(slot_id):
    params = {
        'Page': 'StudentInfobyId',
        'Mode': 'GetCourseBySlot',
        'Id': slot_id
    }
    try:
        resp = http_request('get', HANDLER_URL, params=params, timeout=10)
        if not resp:
            return []
        data = resp.json()
        if not isinstance(data, dict):
            logger.error(f"Handler response not a dict: {type(data)}")
            return []
        table = data.get('Table')
        if not isinstance(table, list):
            logger.warning(f"Handler 'Table' is not a list for slot {slot_id}")
            return []
        return table
    except json.JSONDecodeError:
        logger.error(f"JSON decode error for slot {slot_id}")
        return []
    except Exception as e:
        logger.error(f"Unexpected error fetching slot {slot_id}: {e}")
        return []

# ---------- Parse course ----------
def parse_course(course_json):
    return {
        'code': course_json.get('SubjectCode', 'Unknown'),
        'title': course_json.get('SubjectName', 'Unknown'),
        'faculty': course_json.get('StaffName', 'Unknown'),
        'vacancy': course_json.get('AvailableCount', 0),
        'signature': f"{course_json.get('SubjectCode', '')}|{course_json.get('StaffName', '')}|{course_json.get('SubjectId', 0)}"
    }

# ---------- Main monitoring loop ----------
def monitor():
    global previous_courses, last_check_timestamp, session_alive, login_failures
    logger.info("Starting monitor thread.")
    # Ensure initial login
    if not login():
        send_telegram_message("⚠️ Bot failed to log in on startup. Check credentials.")
        return

    consecutive_failure_cooldown = [2, 5, 15, 30, 60]  # minutes

    while True:
        try:
            # Exponential backoff on login failures
            if login_failures > 0:
                cooldown_index = min(login_failures - 1, len(consecutive_failure_cooldown)-1)
                cooldown_minutes = consecutive_failure_cooldown[cooldown_index]
                if login_failures >= 5:
                    send_telegram_message("🚨 Login blocked? 5 consecutive failures. Sleeping 4 hours.")
                    logger.critical("5 consecutive login failures, sleeping 4 hours.")
                    time.sleep(4 * 3600)
                    login_failures = 0   # reset after long sleep
                    continue
                logger.warning(f"Login failures: {login_failures}. Cooling down for {cooldown_minutes} minutes.")
                time.sleep(cooldown_minutes * 60)

            # Ensure session is alive
            if not ensure_session():
                logger.error("Session dead after re-login attempts, sleeping then retrying.")
                time.sleep(60)
                continue

            all_new_courses = []
            # Check each slot
            for slot_id, slot_name in SLOT_MAP.items():
                courses = get_courses_for_slot(slot_id)
                current_signatures = set()
                new_in_slot = []

                for course_json in courses:
                    parsed = parse_course(course_json)
                    sig = parsed['signature']
                    current_signatures.add(sig)

                    if sig not in previous_courses.get(slot_id, set()):
                        new_in_slot.append({
                            'slot': slot_name,
                            'code': parsed['code'],
                            'title': parsed['title'],
                            'faculty': parsed['faculty'],
                            'vacancy': parsed['vacancy']
                        })

                if new_in_slot:
                    all_new_courses.extend(new_in_slot)
                    previous_courses[slot_id] = current_signatures
                    logger.info(f"New courses in {slot_name}: {len(new_in_slot)}")

            # Save state after processing all slots
            save_state()

            # Send notifications
            if all_new_courses:
                if len(all_new_courses) > 15:
                    # Bulk mode: send summary, split if too long
                    summary_lines = []
                    for course in all_new_courses:
                        summary_lines.append(f"{course['slot']} - {course['code']} ({course['vacancy']} vac)")
                    full_summary = "\n".join(summary_lines)
                    # Split into chunks of max 1000 characters
                    chunk = ""
                    for line in summary_lines:
                        if len(chunk) + len(line) + 2 > 1000:
                            send_telegram_message(f"📊 <b>BULK UPDATE (part):</b>\n{chunk}")
                            chunk = line + "\n"
                        else:
                            chunk += line + "\n"
                    if chunk:
                        send_telegram_message(f"📊 <b>BULK UPDATE:</b> {len(all_new_courses)} new courses.\n{chunk}")
                else:
                    for course in all_new_courses:
                        msg = (
                            f"🆕 <b>NEW COURSE DETECTED!</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━\n"
                            f"📍 <b>Slot:</b> {course['slot']}\n"
                            f"📚 <b>Code:</b> {course['code']}\n"
                            f"📖 <b>Title:</b> {course['title']}\n"
                            f"👨‍🏫 <b>Faculty:</b> {course['faculty']}\n"
                            f"🎫 <b>Vacancies:</b> {course['vacancy']}\n"
                            f"⏰ <b>Time:</b> {datetime.now().strftime('%H:%M:%S')}\n"
                            f"━━━━━━━━━━━━━━━━━━━━━"
                        )
                        send_telegram_message(msg)
                        time.sleep(1.5)
                    send_telegram_message(f"📊 <b>Summary:</b> {len(all_new_courses)} new course(s) added across all slots.")

            last_check_timestamp = datetime.now().isoformat()
            # Jittered sleep
            sleep_time = CHECK_INTERVAL + random.uniform(-12, 12)
            logger.debug(f"Sleeping for {sleep_time:.2f} seconds.")
            time.sleep(max(sleep_time, 5))

        except Exception as e:
            logger.error(f"Monitor loop error: {e}", exc_info=True)
            time.sleep(30)

# ---------- Flask app for health and keep-alive ----------
app = Flask(__name__)

@app.route('/')
def home():
    return "Course monitor bot is running!"

@app.route('/health')
def health():
    return jsonify({
        'status': 'ok',
        'last_check': last_check_timestamp,
        'session_alive': session_alive,
        'login_failures': login_failures
    })

# ---------- Startup ----------
if __name__ == '__main__':
    load_state()
    # Start monitor in background thread
    monitor_thread = threading.Thread(target=monitor, daemon=True)
    monitor_thread.start()
    # Run Flask
    app.run(host='0.0.0.0', port=8080)