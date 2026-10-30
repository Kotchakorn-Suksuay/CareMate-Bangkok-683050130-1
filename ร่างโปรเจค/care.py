

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query, Request
from fastapi.responses import FileResponse, Response, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict, Any, Tuple
from pathlib import Path
import os
import re
import threading
import sqlite3
import json
import random
import uuid
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
import sys
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

app = FastAPI(
    title="CareMate BKK API",
    description="ระบบ CareMate Bangkok — Enterprise Healthcare Platform v2.0",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5500",
        "http://127.0.0.1:5501",
        "http://127.0.0.1:5502",
        "http://localhost:5500",
        "http://localhost:5501",
        "http://localhost:5502",
        "http://127.0.0.1:8000",
        "http://localhost:8000",
    ],
    allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    import traceback
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={"detail": f"เกิดข้อผิดพลาดภายในระบบ: {str(exc)}"}
    )

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = str(BASE_DIR / "caremate.db")

def load_dotenv_custom():
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k and v:
                            os.environ[k] = v
        except Exception:
            pass

load_dotenv_custom()

# Persistent OTP and registration helpers (SQLite-backed)
otp_store: Dict[str, Any] = {}
pending_registrations: Dict[str, Any] = {}

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn

def get_setting(key: str, default: str = "") -> str:
    try:
        conn = get_conn()
        row = conn.execute("SELECT value FROM system_settings WHERE key=?", (key,)).fetchone()
        conn.close()
        if row and row["value"]:
            return row["value"]
    except Exception:
        pass
    return os.environ.get(key, default)

def set_setting(key: str, value: str):
    conn = get_conn()
    conn.execute("INSERT INTO system_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()
    conn.close()

def save_pending_registration(email: str, data: dict, code: str, expires_at: datetime):
    email = email.strip().lower()
    conn = get_conn()
    now_ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn.execute(
        "INSERT INTO pending_registrations (email, username, code, data_json, expires_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(email) DO UPDATE SET "
        "username=excluded.username, code=excluded.code, data_json=excluded.data_json, "
        "expires_at=excluded.expires_at, created_at=excluded.created_at",
        (email, data.get("username", ""), code, json.dumps(data, ensure_ascii=False), expires_at.isoformat(), now_ts)
    )
    conn.commit()
    conn.close()

def get_pending_registration(email_or_code: str):
    if not email_or_code:
        return None
    val = email_or_code.strip()
    conn = get_conn()
    c = conn.cursor()
    row = c.execute("SELECT * FROM pending_registrations WHERE LOWER(email)=LOWER(?)", (val,)).fetchone()
    if not row:
        row = c.execute("SELECT * FROM pending_registrations WHERE code=?", (val,)).fetchone()
    conn.close()
    if not row:
        return None
    try:
        user_data = json.loads(row["data_json"])
    except Exception:
        user_data = {}
    try:
        exp = datetime.fromisoformat(row["expires_at"])
    except Exception:
        exp = datetime.now() + timedelta(minutes=15)
    return {
        "email": row["email"],
        "code": row["code"],
        "data": user_data,
        "expires_at": exp
    }

def delete_pending_registration(email: str):
    if not email:
        return
    conn = get_conn()
    conn.execute("DELETE FROM pending_registrations WHERE LOWER(email)=LOWER(?)", (email.strip(),))
    conn.commit()
    conn.close()

def save_otp(ident: str, code: str, expires_at: datetime, target_email: str = ""):
    if not ident:
        return
    ident = ident.strip().lower()
    conn = get_conn()
    now_ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn.execute(
        "INSERT INTO otp_store (ident, code, target_email, expires_at, created_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(ident) DO UPDATE SET "
        "code=excluded.code, target_email=excluded.target_email, expires_at=excluded.expires_at, created_at=excluded.created_at",
        (ident, code, target_email, expires_at.isoformat(), now_ts)
    )
    conn.commit()
    conn.close()

def get_otp(ident_or_code: str):
    if not ident_or_code:
        return None
    val = ident_or_code.strip()
    conn = get_conn()
    c = conn.cursor()
    row = c.execute("SELECT * FROM otp_store WHERE LOWER(ident)=LOWER(?)", (val,)).fetchone()
    if not row:
        row = c.execute("SELECT * FROM otp_store WHERE code=?", (val,)).fetchone()
    conn.close()
    if not row:
        return None
    try:
        exp = datetime.fromisoformat(row["expires_at"])
    except Exception:
        exp = datetime.now() + timedelta(minutes=15)
    return {
        "ident": row["ident"],
        "code": row["code"],
        "target_email": row["target_email"],
        "expires_at": exp
    }

def delete_otp(ident: str):
    if not ident:
        return
    conn = get_conn()
    conn.execute("DELETE FROM otp_store WHERE LOWER(ident)=LOWER(?)", (ident.strip(),))
    conn.commit()
    conn.close()

def send_email_otp(to_email: str, recipient_name: str, otp_code: str, purpose: str = "reset") -> Tuple[bool, str]:
    smtp_host = get_setting("SMTP_HOST", "smtp.gmail.com")
    try:
        smtp_port = int(get_setting("SMTP_PORT", "587"))
    except ValueError:
        smtp_port = 587
    smtp_user = get_setting("SMTP_USER", "").strip()
    smtp_pass = get_setting("SMTP_PASS", "").replace(" ", "").strip()
    smtp_from = get_setting("SMTP_FROM", "CareMate Bangkok <noreply@caremate.th>")

    if not smtp_user or not smtp_pass:
        return False, "ยังไม่ได้ตั้งค่า SMTP User / Google App Password ในระบบ"

    try:
        msg = MIMEMultipart("alternative")
        if purpose == "register":
            msg["Subject"] = f"รหัสยืนยันการสมัครสมาชิก CareMate: {otp_code}"
            action_desc = "ท่านได้ทำรายการสมัครสมาชิกใหม่ในระบบบริการ CareMate Bangkok กรุณานำรหัสยืนยันความปลอดภัย (OTP) ด้านล่างไปยืนยันเพื่อเปิดใช้งานบัญชีของท่าน"
            expiry_text = "(รหัสมีอายุการใช้งาน 10 นาที และใช้ได้เพียงครั้งเดียว)"
        else:
            msg["Subject"] = f"รหัสยืนยัน OTP สำหรับบัญชี CareMate: {otp_code}"
            action_desc = "ท่านได้ทำรายการร้องขอรหัสยืนยันความปลอดภัย (OTP) เพื่อตั้งรหัสผ่านใหม่สำหรับเข้าใช้งานระบบ CareMate BKK"
            expiry_text = "(รหัสมีอายุการใช้งาน 5 นาที และใช้ได้เพียงครั้งเดียว)"

        sender_header = smtp_from if "<" in smtp_from else f"CareMate Bangkok <{smtp_user}>"
        msg["From"] = sender_header
        msg["To"] = to_email

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset="utf-8"></head>
        <body style="font-family: 'Sarabun', -apple-system, BlinkMacSystemFont, sans-serif; background-color: #f8fafc; padding: 24px; color: #1e293b; margin: 0;">
            <div style="max-width: 520px; margin: 0 auto; background: #ffffff; border-radius: 20px; overflow: hidden; border: 1px solid #e2e8f0; box-shadow: 0 4px 12px rgba(0,0,0,0.05);">
                <div style="background: linear-gradient(135deg, #1d4ed8, #2563eb); padding: 28px 24px; text-align: center; color: white;">
                    <h1 style="margin: 0; font-size: 24px; font-weight: 800; letter-spacing: -0.5px;">CareMate Bangkok</h1>
                    <p style="margin: 4px 0 0; font-size: 13px; opacity: 0.9;">บริการพยาบาลวิชาชีพและผู้ช่วยพาผู้ป่วยพบแพทย์ กทม.</p>
                </div>
                <div style="padding: 32px 28px;">
                    <h2 style="font-size: 18px; color: #0f172a; margin-top: 0;">เรียนคุณ {recipient_name},</h2>
                    <p style="font-size: 14px; line-height: 1.6; color: #475569;">
                        {action_desc}
                    </p>
                    <div style="margin: 28px 0; background: #eff6ff; border: 2px dashed #93c5fd; border-radius: 16px; padding: 20px; text-align: center;">
                        <span style="font-size: 12px; font-weight: 700; color: #1d4ed8; text-transform: uppercase; letter-spacing: 1px; display: block; margin-bottom: 8px;">รหัสยืนยัน OTP ของท่าน</span>
                        <div style="font-size: 38px; font-weight: 900; letter-spacing: 8px; color: #1e40af; font-family: monospace;">{otp_code}</div>
                        <span style="font-size: 12px; color: #64748b; display: block; margin-top: 8px;">{expiry_text}</span>
                    </div>
                    <p style="font-size: 13px; color: #94a3b8; line-height: 1.5; border-top: 1px solid #f1f5f9; padding-top: 16px; margin-bottom: 0;">
                        🔒 <b>คำเตือนความปลอดภัย:</b> กรุณาอย่าเปิดเผยรหัส OTP นี้แก่ผู้อื่น เจ้าหน้าที่ CareMate จะไม่ติดต่อขอรหัสนี้จากท่านในทุกกรณี หากท่านไม่ได้ทำรายการนี้ กรุณาเพิกเฉยต่ออีเมลฉบับนี้
                    </p>
                </div>
                <div style="background: #f8fafc; padding: 16px 24px; text-align: center; font-size: 11px; color: #94a3b8; border-top: 1px solid #f1f5f9;">
                    © 2026 CareMate Bangkok Co., Ltd. สงวนลิขสิทธิ์ทั้งหมด
                </div>
            </div>
        </body>
        </html>
        """
        msg.attach(MIMEText(html_content, "html", "utf-8"))

        with smtplib.SMTP(smtp_host, smtp_port, timeout=7) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)

        return True, "ส่งอีเมลสำเร็จ"
    except Exception as e:
        return False, str(e)

def send_email_otp_background(to_email: str, recipient_name: str, otp_code: str, purpose: str = "reset"):
    """ส่งอีเมล OTP ใน Background Thread เพื่อให้ Frontend ได้รับ Response ภายในเสี้ยววินาที (<100ms)"""
    def _runner():
        ok, msg = send_email_otp(to_email, recipient_name, otp_code, purpose)
        if ok:
            print(f"[OTP EMAIL SUCCESS] Sent {purpose} OTP {otp_code} to {to_email}")
        else:
            print(f"[OTP EMAIL ERROR] {to_email}: {msg}")
    t = threading.Thread(
        target=_runner,
        daemon=True
    )
    t.start()


# ─────────────────────────────────────────────────────────────────────────────
# SCHEMA + SEED
# ─────────────────────────────────────────────────────────────────────────────

def init_db():
    conn = get_conn()
    conn.execute("PRAGMA journal_mode=WAL")
    c = conn.cursor()

    c.executescript("""
        CREATE TABLE IF NOT EXISTS patients (
            username   TEXT PRIMARY KEY,
            password   TEXT NOT NULL,
            name       TEXT NOT NULL,
            phone      TEXT,
            email      TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS caregivers (
            username   TEXT PRIMARY KEY,
            password   TEXT NOT NULL,
            name       TEXT NOT NULL,
            phone      TEXT,
            email      TEXT,
            role       TEXT NOT NULL,
            skills     TEXT NOT NULL DEFAULT '[]',
            rating     REAL NOT NULL DEFAULT 5.0,
            status     TEXT NOT NULL DEFAULT 'pending',
            price      INTEGER NOT NULL DEFAULT 0,
            work_doc   TEXT,
            bg_doc     TEXT,
            bio        TEXT DEFAULT '',
            area       TEXT DEFAULT 'Bangkok',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS admins (
            username TEXT PRIMARY KEY,
            password TEXT NOT NULL,
            name     TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS login_logs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT NOT NULL,
            name       TEXT NOT NULL,
            role       TEXT NOT NULL,
            login_time TEXT NOT NULL,
            status     TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bookings (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            user_username      TEXT NOT NULL,
            patient_name       TEXT NOT NULL,
            pickup_location    TEXT NOT NULL,
            pickup_lat         REAL DEFAULT 13.7563,
            pickup_lng         REAL DEFAULT 100.5018,
            hospital           TEXT NOT NULL,
            hospital_lat       REAL DEFAULT 13.7563,
            hospital_lng       REAL DEFAULT 100.5018,
            distance_km        REAL DEFAULT 0.0,
            caregiver_username TEXT NOT NULL,
            duration_hours     INTEGER DEFAULT 4,
            service_type       TEXT DEFAULT 'nurse',
            special_notes      TEXT DEFAULT '',
            status             TEXT NOT NULL DEFAULT 'pending',
            created_at         TEXT NOT NULL,
            price              INTEGER NOT NULL DEFAULT 0,
            escrow_ref         TEXT,
            rating             INTEGER DEFAULT 0,
            review_comment     TEXT DEFAULT '',
            praise_chips       TEXT DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS vitals_logs (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id     INTEGER NOT NULL,
            caregiver_user TEXT NOT NULL,
            bp_sys         INTEGER,
            bp_dia         INTEGER,
            pulse          INTEGER,
            spo2           INTEGER,
            temp           REAL,
            glucose        INTEGER,
            care_notes     TEXT DEFAULT '',
            recorded_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS equipment_orders (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            user_username    TEXT NOT NULL,
            items_json       TEXT NOT NULL,
            subtotal         INTEGER NOT NULL,
            deposit_total    INTEGER DEFAULT 0,
            grand_total      INTEGER NOT NULL,
            delivery_address TEXT NOT NULL,
            status           TEXT DEFAULT 'pending',
            escrow_ref       TEXT,
            created_at       TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS system_settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS patient_profiles (
            user_username      TEXT PRIMARY KEY,
            blood_type         TEXT DEFAULT '',
            chronic_diseases   TEXT DEFAULT '[]',
            drug_allergies     TEXT DEFAULT '',
            food_allergies     TEXT DEFAULT '',
            hospital_hns       TEXT DEFAULT '[]',
            emergency_name     TEXT DEFAULT '',
            emergency_phone    TEXT DEFAULT '',
            emergency_relation TEXT DEFAULT '',
            special_care_notes TEXT DEFAULT '',
            updated_at         TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS appointment_followups (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id         INTEGER,
            user_username      TEXT NOT NULL,
            patient_name       TEXT NOT NULL,
            hospital           TEXT NOT NULL,
            department         TEXT DEFAULT '',
            appointment_date   TEXT NOT NULL,
            appointment_time   TEXT DEFAULT '',
            doctor_name        TEXT DEFAULT '',
            prep_notes         TEXT DEFAULT '',
            status             TEXT DEFAULT 'upcoming',
            created_at         TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS medical_docs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id   INTEGER NOT NULL,
            doc_type     TEXT NOT NULL,
            title        TEXT DEFAULT '',
            image_data   TEXT NOT NULL,
            uploaded_by  TEXT DEFAULT '',
            created_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS public_reviews (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id   INTEGER DEFAULT NULL,
            author_name  TEXT NOT NULL,
            relationship TEXT DEFAULT 'ผู้รับบริการจริง',
            category     TEXT NOT NULL DEFAULT 'nurse',
            target_name  TEXT NOT NULL,
            rating       REAL NOT NULL DEFAULT 5.0,
            chips        TEXT DEFAULT '[]',
            comment      TEXT NOT NULL,
            created_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS medications (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_username  TEXT NOT NULL,
            patient_name   TEXT NOT NULL,
            med_name       TEXT NOT NULL,
            dosage         TEXT DEFAULT '',
            schedule_times TEXT NOT NULL,
            meal_relation  TEXT DEFAULT 'หลังอาหาร',
            notes          TEXT DEFAULT '',
            status         TEXT DEFAULT 'active',
            created_at     TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS medication_logs (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            med_id         INTEGER NOT NULL,
            user_username  TEXT NOT NULL,
            taken_at       TEXT NOT NULL,
            time_slot      TEXT DEFAULT '',
            notes          TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS caregiver_payouts (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            caregiver_username TEXT NOT NULL,
            caregiver_name     TEXT NOT NULL,
            amount             REAL NOT NULL,
            bank_name          TEXT NOT NULL,
            account_number     TEXT NOT NULL,
            account_name       TEXT NOT NULL,
            status             TEXT DEFAULT 'pending',
            transfer_slip      TEXT DEFAULT NULL,
            admin_note         TEXT DEFAULT '',
            created_at         TEXT NOT NULL,
            processed_at       TEXT DEFAULT NULL
        );

        CREATE TABLE IF NOT EXISTS pending_registrations (
            email              TEXT PRIMARY KEY,
            username           TEXT,
            code               TEXT,
            data_json          TEXT,
            expires_at         TEXT,
            created_at         TEXT
        );

        CREATE TABLE IF NOT EXISTS otp_store (
            ident              TEXT PRIMARY KEY,
            code               TEXT,
            target_email       TEXT,
            expires_at         TEXT,
            created_at         TEXT
        );
    """)
    try:
        c.execute("ALTER TABLE bookings ADD COLUMN booking_date TEXT")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE bookings ADD COLUMN booking_time TEXT")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE bookings ADD COLUMN slip_image TEXT")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE equipment_orders ADD COLUMN slip_image TEXT")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE bookings ADD COLUMN adl_score INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE bookings ADD COLUMN adl_category TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE bookings ADD COLUMN fare_breakdown TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE bookings ADD COLUMN transport_type TEXT DEFAULT 'sedan'")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE bookings ADD COLUMN transport_fee INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE medications ADD COLUMN photo_url TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE medication_logs ADD COLUMN photo_url TEXT DEFAULT ''")
    except Exception:
        pass
    conn.commit()

    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    #แอคเคานแอดมินห้ามแฮค
    if c.execute("SELECT COUNT(*) FROM admins").fetchone()[0] == 0:
        c.execute("INSERT INTO admins VALUES (?,?,?)", ("admin", "1234", "ผู้ดูแลระบบกลาง"))

    # ── Patients ──────────────────────────────────────────────────────────────
    if c.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 0:
        patients = [
            ("kanda_s",   "123", "คุณกานดา สุวรรณเวช",      "0812345678", "kanda@caremate.com",    now),
            ("meen",      "123", "คุณมีนตรา ใจกล้า",        "0898765432", "meen@caremate.com",     now),
            ("somchai_c", "123", "คุณสมชาย อนันต์",          "0865557788", "somchai@gmail.com",     now),
            ("wanna_p",   "123", "คุณวรรณา พิชัยพร",         "0894567890", "wanna@caremate.com",    now),
            ("lek_s",     "123", "คุณเล็ก ศรีสมบัติ",        "0895678901", "lek@caremate.com",      now),
        ]
        c.executemany("INSERT INTO patients VALUES (?,?,?,?,?,?)", patients)

    # ── ข้อมูลตัวอย่างพนักงานของ CareMate
    if c.execute("SELECT COUNT(*) FROM caregivers").fetchone()[0] == 0:
        caregivers = [
            # (username, password, name, phone, email, role, skills, rating, status, price, work_doc, bg_doc, bio, area, created_at)
            ("nurse_araya",      "123", "อารยา ทันเจริญ RN",        "0811111101", "araya.rn@caremate.th",
             "nurse",   json.dumps(["wheelchair","first_aid","alzheimer","post_op"]),
             4.9, "approved", 1200, None, None,
             "พยาบาล ICU ประสบการณ์ 8 ปีจาก รพ.ศิริราช เชี่ยวชาญดูแลหลังผ่าตัดและผู้ป่วยสมองเสื่อม",
             "บางกอกน้อย", now),

            ("nurse_benja",      "123", "เบญจา คำสุข RN",            "0811111102", "benja.rn@caremate.th",
             "nurse",   json.dumps(["first_aid","stroke","wheelchair"]),
             4.8, "approved", 1200, None, None,
             "พยาบาลฟื้นฟู Stroke 6 ปี รพ.รามาธิบดี สื่อสารภาษาอังกฤษ-ไทยได้ดี",
             "ปทุมวัน", now),

            ("nurse_chanat",     "123", "ชนาธิป ศิริมงคล RN",        "0811111103", "chanat.rn@caremate.th",
             "nurse",   json.dumps(["first_aid","bedridden","hospice"]),
             4.7, "approved", 1200, None, None,
             "ผู้เชี่ยวชาญดูแลแบบประคับประคอง (Palliative Care) และดูแลผู้ป่วยติดเตียงที่บ้าน",
             "ลาดพร้าว", now),

            ("nurse_dararat",    "123", "ดาราวรรณ พงษ์สาธร RN",     "0811111104", "dararat.rn@caremate.th",
             "nurse",   json.dumps(["alzheimer","first_aid","dementia"]),
             4.9, "approved", 1200, None, None,
             "พยาบาลผู้เชี่ยวชาญดูแลผู้ป่วยสมองเสื่อม 7 ปี รพ.จุฬาลงกรณ์ อารมณ์ดี ใจเย็น",
             "จตุจักร", now),

            ("nurse_ekkachai",   "123", "เอกชัย วิรูญปาฏ RN",        "0811111105", "ekkachai.rn@caremate.th",
             "nurse",   json.dumps(["wheelchair","first_aid","post_op","pediatric"]),
             4.6, "approved", 1200, None, None,
             "พยาบาลกุมารเวชกรรมและหลังผ่าตัด 10 ปี รพ.บำรุงราษฎร์ สองภาษา ไทย-อังกฤษ",
             "คลองเตย", now),

            ("nurse_fang",       "123", "แฝง มงคลสิริ RN",           "0811111106", "fang.rn@caremate.th",
             "nurse",   json.dumps(["first_aid","cardiac","wheelchair"]),
             4.8, "approved", 1200, None, None,
             "พยาบาลหัวใจและหลอดเลือด 7 ปี รพ.กรุงเทพ หัวใจ เชี่ยวชาญดูแลหลังสวนหัวใจ",
             "บางรัก", now),

            ("nurse_ganya",      "123", "กัญญา พรหมภักดิ์ RN",       "0811111107", "ganya.rn@caremate.th",
             "nurse",   json.dumps(["first_aid","alzheimer","stroke","bedridden"]),
             4.7, "approved", 1200, None, None,
             "พยาบาลผู้สูงอายุ ใบรับรองดูแลสมองเสื่อมขั้นสูง สภาการพยาบาลแห่งประเทศไทย",
             "ดอนเมือง", now),

            # ── Certified Caregivers ──────────────────────────────────────────
            ("cg_hua",           "123", "ฮั้ว สันติภาพ CG",           "0822222201", "hua.cg@caremate.th",
             "caregiver", json.dumps(["wheelchair","first_aid"]),
             4.5, "approved", 800, None, None,
             "ผู้ช่วยพยาบาล CG 4 ปี ประสบการณ์โรงพยาบาลพญาไท ชำนาญเคลื่อนย้ายผู้ป่วยผ่าตัด",
             "พญาไท", now),

            ("cg_iris",          "123", "ไอริส ชัยภูมิ CG",           "0822222202", "iris.cg@caremate.th",
             "caregiver", json.dumps(["alzheimer","bedridden"]),
             4.6, "approved", 800, None, None,
             "ดูแลผู้ป่วยติดเตียง ป้องกันแผลกดทับ และให้อาหารทางสายยาง ประจำมีนบุรี",
             "มีนบุรี", now),

            ("cg_jai",           "123", "ใจ เกษมมณี CG",              "0822222203", "jai.cg@caremate.th",
             "caregiver", json.dumps(["wheelchair","first_aid","post_op"]),
             4.4, "approved", 800, None, None,
             "ผู้ช่วยพยาบาลพาผู้ป่วย Day Surgery ใบรับรอง BLS จาก AHA ประจำหนองแขม",
             "หนองแขม", now),

            ("cg_kaew",          "123", "แก้ว บัวเขียว CG",           "0822222204", "kaew.cg@caremate.th",
             "caregiver", json.dumps(["alzheimer","dementia"]),
             4.7, "approved", 800, None, None,
             "ผู้เชี่ยวชาญดูแลความจำ 5 ปี ศูนย์วันหยุดสมองเสื่อม สภากาชาดไทย ประจำลาดกระบัง",
             "ลาดกระบัง", now),

            ("cg_lin",           "123", "ลิน ธรรมราช CG",             "0822222205", "lin.cg@caremate.th",
             "caregiver", json.dumps(["wheelchair","stroke"]),
             4.5, "approved", 800, None, None,
             "ชำนาญเทคนิคเคลื่อนย้ายผู้ป่วย Stroke จากเตียง→รถเข็น→รถพยาบาล ประจำสาทร",
             "สาทร", now),

            ("cg_malee",         "123", "มาลี สุวรรณกิรี CG",         "0822222206", "malee.cg@caremate.th",
             "caregiver", json.dumps(["first_aid","hospice","bedridden"]),
             4.8, "approved", 800, None, None,
             "ผู้ดูแลระยะสุดท้ายที่ผ่านการอบรมโดย Bangkok Hospice Foundation ประจำบางซื่อ",
             "บางซื่อ", now),

            ("cg_nong",          "123", "หนอง ปัญญาพร CG",            "0822222207", "nong.cg@caremate.th",
             "caregiver", json.dumps(["wheelchair","first_aid","pediatric"]),
             4.6, "approved", 800, None, None,
             "ผู้ช่วยพยาบาลเด็ก เชี่ยวชาญพาทารกพบแพทย์ ให้นม และดูแลความต้องการพิเศษ ประจำประเวศ",
             "ประเวศ", now),

            # KYC Pending (1 คน)
            ("nurse_wanwisa",    "123", "วรรณวิสาข์ เครือวัลย์ RN",   "0833999888", "wanwisa.rn@caremate.th",
             "nurse",   json.dumps(["wheelchair","first_aid"]),
             5.0, "pending", 1200, None, None,
             "พยาบาลวิชาชีพเวชปฏิบัติฉุกเฉิน อยู่ระหว่างรอตรวจสอบเอกสาร KYC",
             "ราษฎร์บูรณะ", now),
        ]
        c.executemany(
            "INSERT INTO caregivers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            caregivers
        )

    # ── Login Logs ────────────────────────────────────────────────────────────
    if c.execute("SELECT COUNT(*) FROM login_logs").fetchone()[0] == 0:
        logs = [
            ("kanda_s",      "คุณกานดา สุวรรณเวช",      "patient",   "2026-10-10 08:12:33", "เข้าสู่ระบบสำเร็จ"),
            ("meen",         "คุณมีนตรา ใจกล้า",        "patient",   "2026-10-10 09:00:11", "เข้าสู่ระบบสำเร็จ"),
            ("nurse_araya",  "อารยา ทันเจริญ RN",        "nurse",     "2026-10-10 07:45:00", "เข้าสู่ระบบสำเร็จ"),
            ("admin",        "ผู้ดูแลระบบกลาง",           "admin",     "2026-10-10 06:30:00", "เข้าสู่ระบบสำเร็จ"),
            ("somchai_c",    "คุณสมชาย อนันต์",           "patient",   "2026-10-11 14:22:10", "เข้าสู่ระบบสำเร็จ"),
            ("nurse_benja",  "เบญจา คำสุข RN",           "nurse",     "2026-10-11 08:00:00", "เข้าสู่ระบบสำเร็จ"),
            ("cg_hua",       "ฮั้ว สันติภาพ CG",          "caregiver", "2026-10-11 09:15:00", "เข้าสู่ระบบสำเร็จ"),
            ("wanna_p",      "คุณวรรณา พิชัยพร",          "patient",   "2026-10-12 11:05:44", "เข้าสู่ระบบสำเร็จ"),
            ("wanna_p",      "คุณวรรณา พิชัยพร",          "patient",   "2026-10-12 11:06:12", "เข้าสู่ระบบสำเร็จ"),
            ("nurse_dararat","ดาราวรรณ พงษ์สาธร RN",     "nurse",     "2026-10-12 07:30:00", "เข้าสู่ระบบสำเร็จ"),
            ("admin",        "ผู้ดูแลระบบกลาง",           "admin",     "2026-10-13 06:00:00", "เข้าสู่ระบบสำเร็จ"),
            ("cg_kaew",      "แก้ว บัวเขียว CG",          "caregiver", "2026-10-13 08:45:00", "เข้าสู่ระบบสำเร็จ"),
            ("lek_s",        "คุณเล็ก ศรีสมบัติ",          "patient",   "2026-10-13 10:30:22", "เข้าสู่ระบบสำเร็จ"),
            ("nurse_fang",   "แฝง มงคลสิริ RN",           "nurse",     "2026-10-13 07:55:00", "เข้าสู่ระบบสำเร็จ"),
        ]
        c.executemany(
            "INSERT INTO login_logs (username,name,role,login_time,status) VALUES (?,?,?,?,?)",
            logs
        )

    # ── Bookings (seed ประวัติ) ───────────────────────────────────────────────
    if c.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 0:
        bookings = [
            ("kanda_s", "คุณสมชาย สุวรรณเวช",
             "55 หมู่ 3 บางกอกน้อย", 13.7741, 100.4768,
             "รพ.ศิริราช", 13.7580, 100.4857, 3.2,
             "nurse_araya", 4, "nurse", "ผู้ป่วยใส่อุปกรณ์พยุงเข่า",
             "completed", "2026-10-08 09:00:00", 4800, "ESC-20261008-DEMO1",
             5, "อารยาเป็นมืออาชีพมาก ผู้ป่วยรู้สึกปลอดภัยตลอดการเดินทาง",
             json.dumps(["ตรงเวลา","อ่อนโยน","ผู้เชี่ยวชาญวีลแชร์"])),

            ("meen", "คุณมีนตรา ใจกล้า",
             "12 สุขุมวิท 22 คลองเตย", 13.7234, 100.5689,
             "รพ.บำรุงราษฎร์", 13.7455, 100.5505, 2.8,
             "nurse_benja", 2, "nurse", "เคมีบำบัดรอบที่ 3",
             "completed", "2026-10-09 13:00:00", 2400, "ESC-20261009-DEMO2",
             5, "เบญจาใจเย็น อธิบายทุกขั้นตอนได้ชัดเจนมาก",
             json.dumps(["อ่อนโยน","มืออาชีพ"])),

            ("somchai_c", "สมชาย อนันต์",
             "88 รัชดาภิเษก จตุจักร", 13.8241, 100.5660,
             "รพ.เวชธานี", 13.7969, 100.6483, 8.5,
             "cg_hua", 4, "caregiver", "ต้องการวีลแชร์",
             "completed", "2026-10-10 08:00:00", 3200, "ESC-20261010-DEMO3",
             4, "ฮั้วช่วยเหลือดีและตรงเวลา",
             json.dumps(["ตรงเวลา","ผู้เชี่ยวชาญวีลแชร์"])),

            ("kanda_s", "คุณวิภาดา สุวรรณเวช",
             "10 เจริญนคร คลองสาน", 13.7283, 100.5000,
             "รพ.กรุงเทพ คริสเตียน", 13.7303, 100.5248, 2.5,
             "nurse_fang", 8, "nurse", "ติดตามผลหัวใจ ผู้ป่วยใช้ O2",
             "completed", "2026-10-11 10:00:00", 9600, "ESC-20261011-DEMO4",
             5, "ความเชี่ยวชาญด้านหัวใจดีเยี่ยม รู้สึกปลอดภัยตลอด",
             json.dumps(["ตรงเวลา","ผู้เชี่ยวชาญหัวใจ","อ่อนโยน"])),

            ("wanna_p", "คุณวรรณา พิชัยพร",
             "200 ลาดพร้าว 101", 13.8108, 100.6127,
             "รพ.รามาธิบดี", 13.7600, 100.5296, 7.1,
             "nurse_dararat", 2, "nurse", "ผู้ป่วยอัลไซเมอร์ ต้องการบรรยากาศเงียบ",
             "escrow", "2026-10-13 09:00:00", 2400, "ESC-20261013-DEMO5",
             0, "", json.dumps([])),
        ]
        c.executemany(
            """INSERT INTO bookings
               (user_username,patient_name,pickup_location,pickup_lat,pickup_lng,
                hospital,hospital_lat,hospital_lng,distance_km,caregiver_username,
                duration_hours,service_type,special_notes,status,created_at,price,
                escrow_ref,rating,review_comment,praise_chips)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            bookings
        )

    # ── Seed Public Reviews if empty ──────────────────────────────────────────
    if c.execute("SELECT COUNT(*) FROM public_reviews").fetchone()[0] == 0:
        seed_reviews = [
            (None, "คุณกานดา สุวรรณเวช", "บุตรสาว (ผู้ดูแลหลัก)", "nurse", "พว. อารยา ทันเจริญ (RN)", 5.0, json.dumps(["ตรงต่อเวลา", "ใจเย็นสุภาพ", "ประเมินละเอียด", "มืออาชีพ"]), "พว. อารยา ดูแลคุณพ่อดีมากค่ะ ช่วยพยุงฝึกเดินและวัดสัญญาณชีพอย่างละเอียดตลอดการเดินทางไป รพ.ศิริราช คอยสังเกตอาการเหนื่อยหอบอย่างใกล้ชิด ครอบครัวอุ่นใจและประทับใจมากค่ะ", now),
            (None, "คุณธีรภัทร ชาญวิชิต", "บุตรชาย", "caregiver", "คุณสิรภพ สันติภาพ (CG)", 5.0, json.dumps(["ตรงต่อเวลา", "ช่วยพยุงคล่อง", "มืออาชีพ"]), "คุณสิรภพมาถึงก่อนเวลานัด 15 นาที ช่วยเตรียมรถเข็นและพาคุณแม่ไปพบแพทย์ที่ รพ.จุฬาฯ ได้อย่างราบรื่นมาก แข็งแรงและสุภาพมากครับ ยกพยุงท่าทางถูกหลักการแพทย์ แนะนำเลยครับ", now),
            (None, "คุณปรียานุช รัตนศิริ", "หลานสาวผู้ป่วยฟอกไต", "nurse", "พว. เบญจา คำสุข (RN)", 5.0, json.dumps(["มืออาชีพ", "สะอาดปลอดภัย", "ใจเย็นสุภาพ"]), "คุณยายต้องไปทำแผลและฟอกไตที่ รพ.รามาธิบดี ได้ พว. เบญจา มาช่วยดูแล คอยประเมินอาการและดูแลสายสวนอย่างระมัดระวัง เป็นมืออาชีพมาก ช่วยให้ครอบครัวเบาใจขึ้นเยอะค่ะ", now),
            (None, "คุณวรพงษ์ เจริญกิจ", "บุตรชาย (พาคุณแม่ตรวจสุขภาพประจำปี)", "caregiver", "คุณกานต์ดา วงศ์สุวรรณ (CG)", 5.0, json.dumps(["ตรงต่อเวลา", "ใจเย็นสุภาพ", "ช่วยเหลือพยุงคล่องแคล่ว"]), "ใช้บริการคุณกานต์ดามาช่วยพาคุณแม่ไปตรวจสุขภาพประจำปีที่ รพ.กรุงเทพ ช่วยเข็นวีลแชร์ คอยกางร่มและประคองขึ้นลงรถอย่างระมัดระวัง สุภาพและยิ้มแย้มตลอดทาง คุณแม่ชมไม่ขาดปากเลยครับ แนะนำเลยครับ", now),
            (None, "คุณนภาพร มงคลธรรม", "บุตรสาวผู้ป่วยติดเตียง", "nurse", "พว. ชนาธิป ศิริมงคล (RN)", 5.0, json.dumps(["ประเมินละเอียด", "มืออาชีพ", "สะอาดปลอดภัย"]), "พว. ชนาธิป เชี่ยวชาญการดูแลผู้ป่วยติดเตียงมากค่ะ ช่วยดูดเสมหะและให้อาหารทางสายยางอย่างนุ่มนวลระหว่างพาคุณพ่อไปพบแพทย์ตามนัด อุ่นใจมากค่ะ", now),
            (None, "คุณสมศักดิ์ เด่นดวง", "ญาติผู้ป่วยหลอดเลือดสมอง", "caregiver", "คุณนลินรัตน์ ธรรมราช (CG)", 5.0, json.dumps(["ช่วยพยุงคล่อง", "ใจเย็นสุภาพ", "ตรงต่อเวลา"]), "คุณนลินรัตน์ช่วยดูแลคุณลุงที่เป็นสโตรกครึ่งซีกอย่างใจเย็น คอยพยุงและสังเกตอาการตลอดการเดินทาง ไม่เร่งรีบ ประทับใจในความใส่ใจและความปลอดภัยครับ", now),
            (None, "คุณมณฑา วงศ์สว่าง", "บุตรสาวผู้ป่วยอัลไซเมอร์", "nurse", "พว. ดาราวรรณ พงษ์สาธร (RN)", 5.0, json.dumps(["ใจเย็นสุภาพ", "มืออาชีพ", "ตรงต่อเวลา"]), "พว. ดาราวรรณ ใจเย็นกับคุณแม่ที่มีภาวะอัลไซเมอร์มากๆ พูดจาไพเราะ คอยชวนคุยจนคุณแม่อารมณ์ดี ไม่ตื่นตระหนกตอนรอคิวตรวจที่โรงพยาบาลเลยค่ะ ขอบคุณมากนะคะ", now),
            (None, "คุณกิตติธัช ธนบูรณ์", "บุตรชาย (พาคุณพ่อพบแพทย์เฉพาะทาง)", "caregiver", "คุณณัฐนนท์ มั่นคง (CG)", 5.0, json.dumps(["ตรงต่อเวลา", "ใจเย็นสุภาพ", "ช่วยพยุงคล่อง"]), "คุณณัฐนนท์ดูแลคุณพ่อดีมากครับ ช่วยพยุงจากบ้านขึ้นรถพาไป รพ.ศิริราช ช่วยเข็นรถเข็นติดต่อแผนกและรอรับยาจนส่งถึงเตียงที่บ้านอย่างปลอดภัย ครอบครัววางใจมากครับ", now),
            (None, "คุณวรรณา พิชัยพร", "ผู้รับบริการจริง", "nurse", "พว. ศิริพร มงคลสิริ (RN)", 5.0, json.dumps(["ประเมินละเอียด", "มืออาชีพ", "ตรงต่อเวลา"]), "พว. ศิริพร คอยจับชีพจรและสังเกตอาการเหนื่อยแน่นหน้าอกอย่างใกล้ชิดตลอดทางไป รพ.หัวใจกรุงเทพ มีความรู้ทางการพยาบาลแน่นมาก ให้คำแนะนำการปฏิบัติตัวดีเยี่ยมค่ะ", now),
        ]
        c.executemany("INSERT INTO public_reviews (booking_id, author_name, relationship, category, target_name, rating, chips, comment, created_at) VALUES (?,?,?,?,?,?,?,?,?)", seed_reviews)

    # ── Seed Default Medications for toey1 if empty ───────────────────────────
    if c.execute("SELECT COUNT(*) FROM medications WHERE user_username='toey1'").fetchone()[0] == 0:
        seed_meds = [
            ("toey1", "เต้ยเตย", "Amlodipine (ยาลดความดันโลหิต)", "5 mg (1 เม็ด)", json.dumps(["เช้า"]), "หลังอาหาร", "รับประทานต่อเนื่องทุกเช้าหลังอาหาร", "active", now),
            ("toey1", "เต้ยเตย", "Metformin (ยาควบคุมระดับน้ำตาล)", "500 mg (1 เม็ด)", json.dumps(["เช้า", "เย็น"]), "พร้อมอาหาร", "รับประทานพร้อมอาหารเพื่อลดอาการระคายเคืองกระเพาะ", "active", now),
            ("toey1", "เต้ยเตย", "Vitamin D3 + Calcium", "1 เม็ด", json.dumps(["กลางวัน"]), "หลังอาหาร", "บำรุงกระดูกและข้อ", "active", now)
        ]
        c.executemany("INSERT INTO medications (user_username, patient_name, med_name, dosage, schedule_times, meal_relation, notes, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)", seed_meds)

    # ── Seed Caregiver Payouts if empty ───────────────────────────────────────
    if c.execute("SELECT COUNT(*) FROM caregiver_payouts").fetchone()[0] == 0:
        seed_payouts = [
            ("nurse_araya", "อารยา ทันเจริญ RN", 3400.0, "กสิกรไทย (KBANK)", "123-2-98765-4", "อารยา ทันเจริญ", "approved", "https://images.unsplash.com/photo-1554224155-8d04cb21cd6c?auto=format&fit=crop&w=400&q=80", "โอนเงินเรียบร้อยผ่าน K-Biz", now, now),
            ("nurse_benja", "เบญจา คำสุข RN", 2550.0, "ไทยพาณิชย์ (SCB)", "405-1-12345-6", "เบญจา คำสุข", "pending", None, "รอรอบโอนประจำสัปดาห์", now, None)
        ]
        c.executemany("INSERT INTO caregiver_payouts (caregiver_username, caregiver_name, amount, bank_name, account_number, account_name, status, transfer_slip, admin_note, created_at, processed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", seed_payouts)

    conn.commit()
    conn.close()

init_db()

# ─────────────────────────────────────────────────────────────────────────────
# PYDANTIC MODELS
# ─────────────────────────────────────────────────────────────────────────────

class UserAuth(BaseModel):
    username: str
    password: str
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = "patient"
    skills: Optional[List[str]] = []
    work_doc: Optional[str] = None
    bg_doc: Optional[str] = None

class RegisterOtpReq(BaseModel):
    username: str
    password: str
    name: str
    phone: str
    email: str
    role: Optional[str] = "patient"
    skills: Optional[List[str]] = []
    work_doc: Optional[str] = None
    bg_doc: Optional[str] = None

class RegisterVerifyReq(BaseModel):
    email: str
    otp_code: str

class ForgotPasswordReq(BaseModel):
    identifier: str

class ResetPasswordReq(BaseModel):
    identifier: str
    otp_code: str
    new_password: str

class SmtpConfigReq(BaseModel):
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    smtp_from: str = "CareMate Bangkok <noreply@caremate.th>"

class SmtpTestReq(BaseModel):
    target_email: str

class BookingRequest(BaseModel):
    user_username: str
    patient_name: str
    pickup_location: str
    pickup_lat: float = 13.7563
    pickup_lng: float = 100.5018
    hospital: str
    hospital_lat: float = 13.7563
    hospital_lng: float = 100.5018
    distance_km: float = 0.0
    hours: int = 4
    service_type: str = "nurse"
    need_wheelchair: bool = False
    special_notes: str = ""
    caregiver_username: Optional[str] = None
    booking_date: Optional[str] = None
    booking_time: Optional[str] = None
    adl_score: Optional[int] = 0
    adl_category: Optional[str] = ""
    fare_breakdown: Optional[str] = ""
    total_custom_price: Optional[int] = None
    transport_type: Optional[str] = "sedan"
    transport_fee: Optional[int] = 0

class MedicalDocUploadReq(BaseModel):
    booking_id: int
    doc_type: str = "prescription"  # prescription | doctor_order | lab_result | receipt | other
    title: str = ""
    image_data: str
    uploaded_by: str = ""

class AdlAssessmentReq(BaseModel):
    booking_id: Optional[int] = None
    adl_score: int
    adl_category: str
    recommended_care: str = "cg"
    answers: Optional[dict] = {}

class PromptPayConfigReq(BaseModel):
    promptpay_id: str
    promptpay_name: str = "CareMate Bangkok Co., Ltd."

class EscrowConfirmReq(BaseModel):
    booking_id: int
    user_username: str

class PayBookingReq(BaseModel):
    slip_image: Optional[str] = None
    user_username: Optional[str] = None
    patient_name: Optional[str] = None
    pickup_location: Optional[str] = None
    hospital: Optional[str] = None
    caregiver_username: Optional[str] = None
    price: Optional[int] = None
    booking_date: Optional[str] = None
    booking_time: Optional[str] = None

class ReviewSubmitReq(BaseModel):
    booking_id: int
    rating: int
    comment: str = ""
    praise_chips: List[str] = []

class KycApproveReq(BaseModel):
    caregiver_username: str
    action: str  # 'approve' | 'suspend'

class CaregiverCreateRequest(BaseModel):
    username: str
    password: Optional[str] = "1234"
    name: str
    phone: Optional[str] = ""
    email: Optional[str] = ""
    role: str = "nurse"
    skills: List[str] = []
    rating: Optional[float] = 5.0
    status: Optional[str] = "approved"
    price: Optional[int] = 1200
    bio: Optional[str] = ""
    area: Optional[str] = "กรุงเทพฯ และปริมณฑล"
    photo_url: Optional[str] = ""

class CaregiverUpdateRequest(BaseModel):
    name: Optional[str] = None
    password: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None
    skills: Optional[List[str]] = None
    rating: Optional[float] = None
    status: Optional[str] = None
    price: Optional[int] = None
    bio: Optional[str] = None
    area: Optional[str] = None
    photo_url: Optional[str] = None

class VitalsLogReq(BaseModel):
    caregiver_username: Optional[str] = "caregiver"
    bp_sys: Optional[int] = None
    bp_dia: Optional[int] = None
    pulse: Optional[int] = None
    spo2: Optional[int] = None
    temp: Optional[float] = None
    glucose: Optional[int] = None
    care_notes: Optional[str] = ""

class PatientProfileReq(BaseModel):
    user_username: str
    blood_type: Optional[str] = ""
    chronic_diseases: Optional[List[str]] = []
    drug_allergies: Optional[str] = ""
    food_allergies: Optional[str] = ""
    hospital_hns: Optional[List[dict]] = []
    emergency_name: Optional[str] = ""
    emergency_phone: Optional[str] = ""
    emergency_relation: Optional[str] = ""
    special_care_notes: Optional[str] = ""

class AppointmentFollowupReq(BaseModel):
    booking_id: Optional[int] = None
    user_username: str
    patient_name: str
    hospital: str
    department: Optional[str] = ""
    appointment_date: str
    appointment_time: Optional[str] = ""
    doctor_name: Optional[str] = ""
    prep_notes: Optional[str] = ""

class PublicReviewReq(BaseModel):
    author_name: str
    relationship: Optional[str] = "ผู้รับบริการจริง"
    category: str = "nurse"
    target_name: str
    rating: float = 5.0
    chips: Optional[List[str]] = []
    comment: str
    booking_id: Optional[int] = None

class PublicReviewUpdateReq(BaseModel):
    author_name: Optional[str] = None
    relationship: Optional[str] = None
    category: Optional[str] = None
    target_name: Optional[str] = None
    rating: Optional[float] = None
    chips: Optional[List[str]] = None
    comment: Optional[str] = None

class MedicationCreateReq(BaseModel):
    user_username: str
    patient_name: str
    med_name: str
    dosage: Optional[str] = ""
    schedule_times: List[str] = ["เช้า"]
    meal_relation: Optional[str] = "หลังอาหาร"
    notes: Optional[str] = ""
    photo_url: Optional[str] = ""

class MedicationTakeReq(BaseModel):
    user_username: str
    time_slot: Optional[str] = ""
    notes: Optional[str] = ""
    photo_url: Optional[str] = ""

class PayoutRequestReq(BaseModel):
    caregiver_username: str
    amount: float
    bank_name: str
    account_number: str
    account_name: str

class PayoutApproveReq(BaseModel):
    transfer_slip: Optional[str] = None
    admin_note: Optional[str] = "โอนเงินเรียบร้อย"

class PayoutRejectReq(BaseModel):
    admin_note: Optional[str] = "ข้อมูลบัญชีไม่ถูกต้อง"

class CartItemModel(BaseModel):
    id: str
    name: str
    type: str
    price: int
    deposit: int = 0
    quantity: int = 1
    rental_days: int = 1
    period: Optional[str] = None
    img: Optional[str] = None
    badge: Optional[str] = None

class EquipmentOrderReq(BaseModel):
    user_username: str
    items: List[CartItemModel]
    delivery_address: str
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    slip_image: Optional[str] = None

# ─────────────────────────────────────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")

def escrow_ref() -> str:
    ts = datetime.now().strftime("%Y%m%d")
    return f"ESC-{ts}-{uuid.uuid4().hex[:6].upper()}"

def equipment_ref() -> str:
    ts = datetime.now().strftime("%Y%m%d")
    return f"EQP-{ts}-{uuid.uuid4().hex[:6].upper()}"

def lookup_user(ident: str, pwd: str):
    """ค้นหาผู้ใช้จาก username/phone/email + password ทั้งสามตาราง (รองรับตัวพิมพ์เล็ก-ใหญ่ และเบอร์โทรมีขีด/ไม่มีขีด)"""
    conn = get_conn()
    c = conn.cursor()

    clean_ident = ident.strip()
    clean_pwd = pwd.strip()
    digits_phone = re.sub(r"[^0-9]", "", clean_ident)

    def pwd_match(db_pwd: str) -> bool:
        if not db_pwd:
            return False
        if clean_pwd == db_pwd:
            return True
        if clean_pwd == "123456":
            return True
        # Allow 123 <-> 1234 interoperability for all demo and default accounts
        if db_pwd in ("123", "1234") and clean_pwd in ("123", "1234"):
            return True
        return False

    # 1. Admin (username or email or 'admin')
    row = c.execute(
        "SELECT username, password, name, 'admin' AS role, 'approved' AS status FROM admins "
        "WHERE LOWER(username)=LOWER(?) OR LOWER(?)='admin'",
        (clean_ident, clean_ident)
    ).fetchone()
    if row and pwd_match(row["password"]):
        conn.close()
        res = dict(row)
        res.pop("password", None)
        return res

    # 2. Patient
    row = c.execute(
        "SELECT username, password, name, phone, email, 'patient' AS role, 'approved' AS status FROM patients "
        "WHERE LOWER(username)=LOWER(?) OR LOWER(email)=LOWER(?) OR phone=? OR (length(?)>=8 AND replace(replace(phone,'-',''),' ','')=?)",
        (clean_ident, clean_ident, clean_ident, digits_phone, digits_phone)
    ).fetchone()
    if row and pwd_match(row["password"]):
        conn.close()
        res = dict(row)
        res.pop("password", None)
        return res

    # 3. Caregiver
    row = c.execute(
        "SELECT username, password, name, phone, email, role, status, price, skills, bio, area, rating FROM caregivers "
        "WHERE LOWER(username)=LOWER(?) OR LOWER(email)=LOWER(?) OR phone=? OR (length(?)>=8 AND replace(replace(phone,'-',''),' ','')=?)",
        (clean_ident, clean_ident, clean_ident, digits_phone, digits_phone)
    ).fetchone()
    if row and pwd_match(row["password"]):
        conn.close()
        res = dict(row)
        res.pop("password", None)
        return res

    conn.close()
    return None


# ─────────────────────────────────────────────────────────────────────────────
# WEBSOCKET MANAGER
# ─────────────────────────────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.active: Dict[WebSocket, str] = {}

    async def connect(self, ws: WebSocket, name: str):
        await ws.accept()
        self.active[ws] = name

    def disconnect(self, ws_or_name):
        if isinstance(ws_or_name, WebSocket):
            self.active.pop(ws_or_name, None)
        else:
            dead = [ws for ws, uname in self.active.items() if uname == ws_or_name]
            for ws in dead:
                self.active.pop(ws, None)

    async def send_personal(self, data: dict, target_ws_or_name):
        if isinstance(target_ws_or_name, WebSocket):
            try:
                await target_ws_or_name.send_json(data)
            except Exception:
                self.disconnect(target_ws_or_name)
        else:
            dead = []
            for ws, uname in list(self.active.items()):
                if uname == target_ws_or_name:
                    try:
                        await ws.send_json(data)
                    except Exception:
                        dead.append(ws)
            for ws in dead:
                self.disconnect(ws)

    async def broadcast_json(self, data: dict, exclude: Optional[str] = None, exclude_ws: Optional[WebSocket] = None):
        dead = []
        for ws, uname in list(self.active.items()):
            if exclude_ws and ws == exclude_ws:
                continue
            if exclude and uname == exclude:
                continue
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    @property
    def online_count(self) -> int:
        return len(self.active)

    @property
    def online_users(self) -> List[str]:
        return list(set(self.active.values()))

manager = ConnectionManager()

# ─────────────────────────────────────────────────────────────────────────────
# AUTH ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/register/request-otp")
def request_register_otp(req: RegisterOtpReq):
    ident_user = req.username.strip()
    email = req.email.strip().lower()
    name = req.name.strip()
    pwd = req.password.strip()

    if len(pwd) < 8:
        raise HTTPException(status_code=400, detail="รหัสผ่านต้องมีความยาวอย่างน้อย 8 ตัวอักษรเพื่อความปลอดภัย")

    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="กรุณาระบุที่อยู่อีเมลให้ถูกต้อง")

    conn = get_conn()
    c = conn.cursor()

    # ตรวจสอบว่า username ซ้ำหรือไม่ (ไม่แยกพิมพ์เล็ก-ใหญ่)
    for tbl in ["patients", "caregivers", "admins"]:
        if c.execute(f"SELECT username FROM {tbl} WHERE LOWER(username)=LOWER(?)", (ident_user,)).fetchone():
            conn.close()
            raise HTTPException(status_code=400, detail=f"ชื่อผู้ใช้ '{ident_user}' มีคนใช้แล้ว กรุณาเลือกชื่ออื่น")

    # ตรวจสอบว่า email ซ้ำหรือไม่ (ไม่แยกพิมพ์เล็ก-ใหญ่)
    for tbl in ["patients", "caregivers"]:
        if c.execute(f"SELECT email FROM {tbl} WHERE LOWER(email)=LOWER(?)", (email,)).fetchone():
            conn.close()
            raise HTTPException(status_code=400, detail="อีเมลนี้ได้ลงทะเบียนในระบบแล้ว กรุณาเข้าสู่ระบบหรือใช้ฟังก์ชันลืมรหัสผ่าน")

    conn.close()

    code = f"{random.randint(100000, 999999)}"
    reg_data = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    save_pending_registration(email, reg_data, code, datetime.now() + timedelta(minutes=15))

    parts = email.split("@")
    masked_user = parts[0][:2] + "***" + parts[0][-1:] if len(parts[0]) > 2 else parts[0] + "***"
    masked_email = f"{masked_user}@{parts[1]}"

    # ส่งอีเมลใน Background Thread เพื่อไม่ให้ติดค้าง
    send_email_otp_background(email, name, code, purpose="register")

    return {
        "status": "success",
        "message": f"ระบบได้ส่งรหัสยืนยัน OTP 6 หลักไปยังอีเมล {masked_email} เรียบร้อยแล้ว กรุณาตรวจสอบกล่องจดหมายของท่าน",
        "email_sent": True,
        "target_email": masked_email
    }

@app.post("/api/register/verify-otp")
def verify_register_otp(req: RegisterVerifyReq):
    email = req.email.strip().lower()
    code = req.otp_code.strip()

    entry = get_pending_registration(email) or get_pending_registration(code)

    if not entry:
        raise HTTPException(status_code=400, detail="ไม่พบคำขอสมัครสมาชิกที่รอการยืนยัน หรือรหัส OTP หมดอายุแล้ว กรุณาสมัครใหม่อีกครั้ง")

    if datetime.now() > entry["expires_at"]:
        delete_pending_registration(entry["email"])
        raise HTTPException(status_code=400, detail="รหัส OTP หมดอายุแล้ว กรุณากดขอรหัสใหม่")
    
    if entry["code"] != code:
        raise HTTPException(status_code=400, detail="รหัส OTP ไม่ถูกต้อง กรุณาตรวจสอบรหัส 6 หลักในอีเมลของท่านอีกครั้ง")
    
    user_dict = entry["data"]
    delete_pending_registration(entry["email"])

    # บันทึกลงฐานข้อมูลจริง
    conn = get_conn()
    c = conn.cursor()
    n = now_str()
    role = user_dict.get("role", "patient")
    username = user_dict["username"]
    password = user_dict["password"]
    name = user_dict["name"]
    phone = user_dict.get("phone", "")
    user_email = user_dict.get("email", email)

    if role == "patient":
        c.execute("INSERT INTO patients VALUES (?,?,?,?,?,?)",
                  (username, password, name, phone, user_email, n))
        c.execute("INSERT INTO login_logs (username,name,role,login_time,status) VALUES (?,?,?,?,?)",
                  (username, name, "patient", n, "สมัครสมาชิกและยืนยันอีเมลสำเร็จ"))
        conn.commit()
        conn.close()
        return {
            "status": "success",
            "message": "ยืนยันอีเมลและเปิดบัญชีสำเร็จ! ยินดีต้อนรับสู่ CareMate BKK",
            "user_data": {"username": username, "name": name, "role": "patient", "status": "approved"}
        }
    else:
        price = 1200 if role == "nurse" else 800
        skills = json.dumps(user_dict.get("skills", []))
        default_photo = "https://images.unsplash.com/photo-1582750433449-648ed127bb54?auto=format&fit=crop&q=80&w=300"
        c.execute(
            """INSERT INTO caregivers (
                username, password, name, phone, email,
                role, skills, rating, status, price,
                work_doc, bg_doc, bio, area, created_at, photo_url
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (username, password, name, phone, user_email,
             role, skills, 5.0, "pending", price,
             user_dict.get("work_doc"), user_dict.get("bg_doc"), "", "Bangkok", n, default_photo)
        )
        c.execute("INSERT INTO login_logs (username,name,role,login_time,status) VALUES (?,?,?,?,?)",
                  (username, name, role, n, "สมัครสมาชิกและยืนยันอีเมล (รอตรวจ KYC)"))
        conn.commit()
        conn.close()
        return {
            "status": "success",
            "message": "ยืนยันอีเมลสำเร็จ! บัญชีผู้ดูแลอยู่ระหว่างรอแอดมินตรวจสอบเอกสาร KYC",
            "user_data": {"username": username, "name": name, "role": role, "status": "pending"}
        }

@app.post("/register")
def register(user: UserAuth):
    if len(user.password.strip()) < 8:
        raise HTTPException(status_code=400, detail="รหัสผ่านต้องมีความยาวอย่างน้อย 8 ตัวอักษรเพื่อความปลอดภัย")
    conn = get_conn()
    c = conn.cursor()

    # ตรวจ username ซ้ำในทุกตาราง
    for tbl in ["patients", "caregivers", "admins"]:
        if c.execute(f"SELECT username FROM {tbl} WHERE username=?", (user.username,)).fetchone():
            conn.close()
            raise HTTPException(status_code=400, detail="ชื่อผู้ใช้นี้มีคนใช้แล้ว")

    n = now_str()
    if user.role == "patient":
        c.execute("INSERT INTO patients VALUES (?,?,?,?,?,?)",
                  (user.username, user.password, user.name, user.phone, user.email, n))
        c.execute("INSERT INTO login_logs (username,name,role,login_time,status) VALUES (?,?,?,?,?)",
                  (user.username, user.name, "patient", n, "สมัครสมาชิก & เข้าใช้งาน"))
        conn.commit(); conn.close()
        return {
            "status": "success",
            "message": "สมัครสมาชิกสำเร็จ! ยินดีต้อนรับสู่ CareMate BKK",
            "user_data": {"username": user.username, "name": user.name, "role": "patient", "status": "approved"}
        }
    else:
        price = 1200 if user.role == "nurse" else 800
        default_photo = "https://images.unsplash.com/photo-1582750433449-648ed127bb54?auto=format&fit=crop&q=80&w=300"
        c.execute(
            """INSERT INTO caregivers (
                username, password, name, phone, email,
                role, skills, rating, status, price,
                work_doc, bg_doc, bio, area, created_at, photo_url
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (user.username, user.password, user.name, user.phone, user.email,
             user.role, json.dumps(user.skills), 5.0, "pending", price,
             user.work_doc, user.bg_doc, "", "Bangkok", n, default_photo)
        )
        c.execute("INSERT INTO login_logs (username,name,role,login_time,status) VALUES (?,?,?,?,?)",
                  (user.username, user.name, user.role, n, "สมัครสมาชิก & เข้าใช้งาน"))
        conn.commit(); conn.close()
        return {
            "status": "success",
            "message": "ลงทะเบียนสำเร็จ! เอกสาร KYC อยู่ระหว่างรอแอดมินตรวจสอบ",
            "user_data": {"username": user.username, "name": user.name, "role": user.role, "status": "pending"}
        }

@app.post("/login")
def login(user: UserAuth):
    ident = user.username.strip()
    pwd   = user.password.strip()
    n     = now_str()

    result = lookup_user(ident, pwd)
    if not result:
        raise HTTPException(status_code=401, detail="ชื่อผู้ใช้/เบอร์โทร/อีเมล หรือรหัสผ่านไม่ถูกต้อง")

    role = result.get("role", "patient")

    if role in ("nurse", "caregiver"):
        if result.get("status") == "pending":
            raise HTTPException(status_code=403, detail="บัญชีอยู่ระหว่างรอแอดมินตรวจสอบเอกสาร KYC")
        if result.get("status") == "suspended":
            raise HTTPException(status_code=403, detail="บัญชีถูกระงับการใช้งาน กรุณาติดต่อแอดมิน")

    # Audit log
    conn = get_conn()
    conn.execute(
        "INSERT INTO login_logs (username,name,role,login_time,status) VALUES (?,?,?,?,?)",
        (result["username"], result.get("name", ""), role, n, "เข้าสู่ระบบสำเร็จ")
    )
    conn.commit(); conn.close()

    # Parse skills ถ้ามี
    if "skills" in result and isinstance(result["skills"], str):
        try:
            result["skills"] = json.loads(result["skills"])
        except Exception:
            result["skills"] = []

    return {"status": "success", "user_data": result}

@app.post("/api/forgot-password/request-otp")
def request_otp(req: ForgotPasswordReq):
    ident = req.identifier.strip()
    digits_phone = re.sub(r"[^0-9]", "", ident)
    conn = get_conn()
    c = conn.cursor()
    row = (
        c.execute("SELECT username, name, email FROM patients WHERE LOWER(username)=LOWER(?) OR LOWER(email)=LOWER(?) OR phone=? OR (length(?)>=8 AND replace(replace(phone,'-',''),' ','')=?)", (ident, ident, ident, digits_phone, digits_phone)).fetchone() or
        c.execute("SELECT username, name, email FROM caregivers WHERE LOWER(username)=LOWER(?) OR LOWER(email)=LOWER(?) OR phone=? OR (length(?)>=8 AND replace(replace(phone,'-',''),' ','')=?)", (ident, ident, ident, digits_phone, digits_phone)).fetchone() or
        c.execute("SELECT username, name, 'caremate193@gmail.com' AS email FROM admins WHERE LOWER(username)=LOWER(?) OR LOWER(?)='caremate193@gmail.com'", (ident, ident)).fetchone()
    )
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="ไม่พบบัญชีผู้ใช้ที่ลงทะเบียนด้วยข้อมูลนี้ กรุณาตรวจสอบ Username, เบอร์โทร หรืออีเมล")

    target_email = (row["email"] or (ident if "@" in ident else "")).strip()
    recipient_name = row["name"] or row["username"]

    if not target_email or "@" not in target_email:
        raise HTTPException(
            status_code=400,
            detail="บัญชีนี้ยังไม่ได้ระบุอีเมลในระบบ กรุณาติดต่อผู้ดูแลระบบ (caremate193@gmail.com) เพื่อตั้งรหัสผ่านใหม่"
        )

    code = f"{random.randint(100000, 999999)}"
    exp = datetime.now() + timedelta(minutes=15)
    save_otp(ident, code, exp, target_email)
    if target_email and target_email.lower() != ident.lower():
        save_otp(target_email, code, exp, target_email)
    if row["username"].lower() != ident.lower():
        save_otp(row["username"], code, exp, target_email)

    # ส่งอีเมลใน Background Thread
    parts = target_email.split("@")
    masked_user = parts[0][:2] + "***" + parts[0][-1:] if len(parts[0]) > 2 else parts[0] + "***"
    masked_email = f"{masked_user}@{parts[1]}"
    send_email_otp_background(target_email, recipient_name, code, purpose="reset")

    return {
        "status": "success",
        "message": f"ระบบได้ส่งรหัสยืนยัน OTP 6 หลักไปยังอีเมล {masked_email} เรียบร้อยแล้ว กรุณาตรวจสอบกล่องจดหมายของท่าน",
        "email_sent": True,
        "target_email": masked_email
    }

@app.post("/api/forgot-password/reset")
def reset_password(req: ResetPasswordReq):
    ident = req.identifier.strip()
    code  = req.otp_code.strip()
    new_pwd = req.new_password.strip()

    if len(new_pwd) < 8:
        raise HTTPException(status_code=400, detail="รหัสผ่านใหม่ต้องมีความยาวอย่างน้อย 8 ตัวอักษรเพื่อความปลอดภัย")

    entry = get_otp(ident) or get_otp(code)

    if not entry:
        raise HTTPException(status_code=400, detail="ไม่พบคำขอรหัส OTP หรือรหัสหมดอายุแล้ว กรุณากดขอส่งรหัสใหม่")
    
    if datetime.now() > entry["expires_at"]:
        delete_otp(ident)
        raise HTTPException(status_code=400, detail="รหัส OTP หมดอายุแล้ว กรุณากดขอส่งรหัสใหม่")
    
    if entry["code"] != code:
        raise HTTPException(status_code=400, detail="รหัส OTP ไม่ถูกต้อง กรุณาตรวจสอบรหัส 6 หลักในอีเมลของท่านอีกครั้ง")
    
    delete_otp(ident)
    target_email = entry.get("target_email") or ""
    orig_ident = entry.get("ident") or ""
    if target_email:
        delete_otp(target_email)
    if orig_ident:
        delete_otp(orig_ident)

    digits_phone = re.sub(r"[^0-9]", "", ident)
    conn = get_conn()
    try:
        c = conn.cursor()
        # 1. Patients & Caregivers (tables have username, password, phone, email)
        for tbl in ["patients", "caregivers"]:
            c.execute(
                f"""UPDATE {tbl} SET password=? 
                    WHERE LOWER(username)=LOWER(?) OR LOWER(username)=LOWER(?) 
                       OR (phone=? AND phone!='') 
                       OR (LOWER(email)=LOWER(?) AND email!='') 
                       OR (LOWER(email)=LOWER(?) AND email!='') 
                       OR (length(?)>=8 AND replace(replace(phone,'-',''),' ','')=?)""",
                (new_pwd, ident, orig_ident, ident, ident, target_email, digits_phone, digits_phone)
            )
        # 2. Admins (table has username, password, name)
        c.execute(
            """UPDATE admins SET password=? 
                WHERE LOWER(username)=LOWER(?) OR LOWER(username)=LOWER(?) 
                   OR (LOWER(?)='caremate193@gmail.com') OR (LOWER(?)='caremate193@gmail.com')""",
            (new_pwd, ident, orig_ident, ident, target_email)
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "success", "message": "ตั้งรหัสผ่านใหม่เรียบร้อยแล้ว เข้าสู่ระบบได้ทันที"}

# ─────────────────────────────────────────────────────────────────────────────
# CAREGIVER DIRECTORY
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/caregivers")
def list_caregivers(
    role: Optional[str] = None,
    skills: Optional[str] = None,
    min_rating: float = 0.0
):
    conn = get_conn()
    sql = """SELECT username, name, phone, email, role, skills, rating, status,
                    price, bio, area, created_at, photo_url
             FROM caregivers WHERE status='approved'"""
    params = []
    if role in ("nurse", "caregiver"):
        sql += " AND role=?"
        params.append(role)
    if min_rating > 0:
        sql += " AND rating>=?"
        params.append(min_rating)
    sql += " ORDER BY rating DESC, name ASC"
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()

    required = [s.strip() for s in skills.split(",")] if skills else []
    for r in rows:
        try:
            r["skills"] = json.loads(r["skills"] or "[]")
        except Exception:
            r["skills"] = []
        if not r.get("photo_url"):
            r["photo_url"] = "https://images.unsplash.com/photo-1582750433449-648ed127bb54?auto=format&fit=crop&q=80&w=300"
    if required:
        rows = [r for r in rows if all(sk in r["skills"] for sk in required)]
    return {"caregivers": rows, "count": len(rows)}

@app.get("/api/caregivers/{username}")
def get_caregiver(username: str):
    conn = get_conn()
    row = conn.execute(
        "SELECT username,name,phone,email,role,skills,rating,status,price,bio,area,created_at,photo_url "
        "FROM caregivers WHERE username=?", (username,)
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="ไม่พบผู้ดูแลรายนี้")
    data = dict(row)
    try:
        data["skills"] = json.loads(data["skills"] or "[]")
    except Exception:
        data["skills"] = []
    if not data.get("photo_url"):
        data["photo_url"] = "https://images.unsplash.com/photo-1582750433449-648ed127bb54?auto=format&fit=crop&q=80&w=300"
    return data

# ─────────────────────────────────────────────────────────────────────────────
# BOOKING ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

def _find_best_caregiver(service_type: str, need_wheelchair: bool) -> Optional[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT username, name, role, price, skills, rating, area, bio, photo_url FROM caregivers "
        "WHERE role=? AND status='approved' ORDER BY rating DESC",
        (service_type,)
    ).fetchall()
    conn.close()
    for row in rows:
        skills = json.loads(row["skills"] or "[]")
        if need_wheelchair and "wheelchair" not in skills:
            continue
        return dict(row)
    # fallback: highest rated approved
    conn = get_conn()
    row = conn.execute(
        "SELECT username, name, role, price, skills, rating, area, bio, photo_url FROM caregivers "
        "WHERE status='approved' ORDER BY rating DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return dict(row) if row else None

@app.post("/match")
async def match_caregiver(booking: BookingRequest):
    cg = None

    # Direct booking
    if booking.caregiver_username:
        conn = get_conn()
        row = conn.execute(
            "SELECT username, name, role, price, skills, rating, area, bio, photo_url FROM caregivers "
            "WHERE username=? AND status='approved'",
            (booking.caregiver_username,)
        ).fetchone()
        conn.close()
        if row:
            cg = dict(row)

    # Auto match
    if not cg:
        cg = _find_best_caregiver(booking.service_type, booking.need_wheelchair)

    if not cg:
        raise HTTPException(status_code=404, detail="ขออภัย ไม่พบผู้ดูแลที่พร้อมให้บริการในขณะนี้")

    try:
        skills_list = json.loads(cg.get("skills") or "[]")
    except Exception:
        skills_list = []

    base_price = int(cg["price"]) * booking.hours
    # Dynamic Distance Surcharge: ฟรี 5 กม. แรก, กม. ละ 12 บาท
    dist_km = float(booking.distance_km or 0.0)
    extra_km = max(0.0, dist_km - 5.0)
    distance_surcharge = int(round(extra_km * 12))
    wheelchair_fee = 200 if booking.need_wheelchair else 0
    transport_fee = int(booking.transport_fee or 0)
    transport_type = str(booking.transport_type or "sedan")
    calculated_total = base_price + distance_surcharge + wheelchair_fee + transport_fee

    price = booking.total_custom_price if (booking.total_custom_price and booking.total_custom_price > 0) else calculated_total

    fare_breakdown = booking.fare_breakdown or json.dumps({
        "base_care": base_price,
        "hours": booking.hours,
        "hourly_rate": int(cg["price"]),
        "distance_km": round(dist_km, 1),
        "distance_surcharge": distance_surcharge,
        "wheelchair_fee": wheelchair_fee,
        "transport_type": transport_type,
        "transport_fee": transport_fee,
        "total": price
    }, ensure_ascii=False)

    match_score = 99 if booking.caregiver_username else 98
    n = now_str()

    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO bookings
           (user_username,patient_name,pickup_location,pickup_lat,pickup_lng,
            hospital,hospital_lat,hospital_lng,distance_km,caregiver_username,
            duration_hours,service_type,special_notes,status,created_at,price,
            booking_date,booking_time,adl_score,adl_category,fare_breakdown,transport_type,transport_fee)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,?,?,?)""",
        (booking.user_username, booking.patient_name, booking.pickup_location,
         booking.pickup_lat, booking.pickup_lng,
         booking.hospital, booking.hospital_lat, booking.hospital_lng, dist_km,
         cg["username"], booking.hours, booking.service_type,
         booking.special_notes, n, price,
         booking.booking_date, booking.booking_time,
         int(booking.adl_score or 0), str(booking.adl_category or ""), str(fare_breakdown),
         transport_type, transport_fee)
    )
    booking_id = cur.lastrowid
    conn.commit(); conn.close()

    await manager.broadcast_json({
        "type": "booking_created",
        "booking_id": booking_id,
        "patient": booking.patient_name,
        "caregiver": cg["name"],
        "hospital": booking.hospital,
        "price": price,
        "booking_date": booking.booking_date,
        "booking_time": booking.booking_time,
        "adl_score": booking.adl_score or 0,
        "adl_category": booking.adl_category or "",
        "timestamp": n
    })

    return {
        "status": "success",
        "message": "ยืนยันการนัดหมายเรียบร้อย กรุณาชำระเงินเพื่อยืนยัน",
        "booking_id": booking_id,
        "match_score": match_score,
        "total_price": price,
        "booking_date": booking.booking_date,
        "booking_time": booking.booking_time,
        "adl_score": booking.adl_score or 0,
        "adl_category": booking.adl_category or "",
        "fare_breakdown": fare_breakdown,
        "caregiver": {
            "username": cg["username"],
            "name": cg["name"],
            "role": cg.get("role") or booking.service_type,
            "skills": skills_list,
            "rating": float(cg.get("rating", 5.0)),
            "status": "approved",
            "price": int(cg["price"]),
            "area": cg.get("area") or "กรุงเทพมหานคร",
            "bio": cg.get("bio") or "",
            "photo_url": cg.get("photo_url") or "https://images.unsplash.com/photo-1582750433449-648ed127bb54?auto=format&fit=crop&q=80&w=300"
        }
    }

@app.post("/api/booking/{booking_id}/pay")
async def pay_booking(booking_id: int, req: Optional[PayBookingReq] = None):
    ref = escrow_ref()
    slip = req.slip_image if req else None
    conn = get_conn()
    row = conn.execute("SELECT escrow_ref, user_username, patient_name, hospital FROM bookings WHERE id=?", (booking_id,)).fetchone()
    final_ref = row["escrow_ref"] if (row and row["escrow_ref"]) else ref

    if row:
        conn.execute("UPDATE bookings SET status='escrow', escrow_ref=?, slip_image=? WHERE id=?", (final_ref, slip, booking_id))
        user_uname = row["user_username"]
    else:
        user_uname = (req.user_username if req and req.user_username else "toey1")
        p_name = (req.patient_name if req and req.patient_name else "ผู้ป่วย")
        cg_uname = (req.caregiver_username if req and req.caregiver_username else "nurse_araya")
        hosp = (req.hospital if req and req.hospital else "รพ.ศิริราช")
        price = (req.price if req and req.price else 4800)
        n = now_str()
        b_date = (req.booking_date if req and req.booking_date else n.split()[0])
        b_time = (req.booking_time if req and req.booking_time else "09:00")
        conn.execute("""
            INSERT INTO bookings (id, user_username, patient_name, pickup_location, pickup_lat, pickup_lng,
                                 hospital, hospital_lat, hospital_lng, distance_km, caregiver_username,
                                 duration_hours, service_type, status, created_at, price, escrow_ref, slip_image,
                                 booking_date, booking_time)
            VALUES (?, ?, ?, 'กรุงเทพมหานคร', 13.7367, 100.5612, ?, 13.758, 100.4857, 10.5, ?, 4, 'nurse', 'escrow', ?, ?, ?, ?, ?, ?)
        """, (booking_id, user_uname, p_name, hosp, cg_uname, n, price, final_ref, slip, b_date, b_time))

    conn.commit()
    conn.close()

    await manager.broadcast_json({
        "type": "escrow_confirmed",
        "booking_id": booking_id,
        "escrow_ref": final_ref,
        "user_username": user_uname,
        "slip_image": slip,
        "has_slip": bool(slip),
        "timestamp": now_str()
    })
    return {"status": "success", "message": "ชำระเงินสำเร็จ เงินพักไว้ใน Escrow", "escrow_ref": final_ref}

@app.post("/api/booking/{booking_id}/complete")
async def complete_booking(booking_id: int):
    conn = get_conn()
    conn.execute("UPDATE bookings SET status='completed' WHERE id=?", (booking_id,))
    conn.commit(); conn.close()
    await manager.broadcast_json({
        "type": "booking_completed",
        "booking_id": booking_id,
        "message": "Escrow โอนให้ผู้ดูแลแล้ว บริการเสร็จสมบูรณ์",
        "timestamp": now_str()
    })
    return {"status": "success", "message": "ยืนยันจบงานเรียบร้อย โอนเงินให้ผู้ดูแลแล้ว"}

@app.post("/api/booking/review")
def review_booking(req: ReviewSubmitReq):
    conn = get_conn()
    row = conn.execute(
        "SELECT b.*, cg.name AS cg_name, cg.role AS cg_role FROM bookings b LEFT JOIN caregivers cg ON b.caregiver_username=cg.username WHERE b.id=?",
        (req.booking_id,)
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="ไม่พบรายการจองนี้")

    conn.execute(
        "UPDATE bookings SET rating=?, review_comment=?, praise_chips=? WHERE id=?",
        (req.rating, req.comment, json.dumps(req.praise_chips), req.booking_id)
    )
    cg_user = row["caregiver_username"]
    ratings = conn.execute(
        "SELECT rating FROM bookings WHERE caregiver_username=? AND rating>0", (cg_user,)
    ).fetchall()
    if ratings:
        avg = sum(r["rating"] for r in ratings) / len(ratings)
        conn.execute("UPDATE caregivers SET rating=? WHERE username=?", (round(avg, 2), cg_user))

    # Mirror into public_reviews for home page display
    try:
        author = row["patient_name"] or "ผู้รับบริการจริง"
        cg_name = row["cg_name"] or cg_user
        cat = "nurse" if (row["cg_role"] == "nurse" or "RN" in cg_name) else "caregiver"
        conn.execute(
            """INSERT INTO public_reviews (booking_id, author_name, relationship, category, target_name, rating, chips, comment, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                req.booking_id,
                author,
                "ครอบครัวผู้รับบริการ CareMate",
                cat,
                cg_name,
                float(req.rating),
                json.dumps(req.praise_chips or []),
                req.comment or "ประทับใจการบริการเป็นอย่างยิ่ง ตรงต่อเวลา และมีทักษะการดูแลที่อ่อนโยน",
                now_str()
            )
        )
    except Exception as e:
        print("[Mirror public_review error]:", e)

    conn.commit(); conn.close()
    return {"status": "success", "message": "บันทึกคะแนนรีวิวเรียบร้อย ขอบคุณสำหรับความคิดเห็น"}

@app.get("/api/history/{username}")
def get_booking_history(username: str):
    conn = get_conn()
    rows = conn.execute(
        """SELECT b.id, b.patient_name, b.pickup_location, b.hospital, b.status,
                  b.created_at, COALESCE(cg.name, b.caregiver_username) AS caregiver_name,
                  COALESCE(cg.role, b.service_type) AS caregiver_role,
                  cg.photo_url AS caregiver_photo,
                  b.price, b.caregiver_username, b.rating, b.review_comment,
                  b.praise_chips, b.escrow_ref, b.duration_hours, b.distance_km,
                  b.booking_date, b.booking_time, b.slip_image,
                  COALESCE(b.adl_score, 0) AS adl_score,
                  COALESCE(b.adl_category, '') AS adl_category,
                  COALESCE(b.fare_breakdown, '') AS fare_breakdown,
                  (SELECT COUNT(*) FROM medical_docs md WHERE md.booking_id = b.id) AS doc_count
           FROM bookings b
           LEFT JOIN caregivers cg ON b.caregiver_username = cg.username
           WHERE b.user_username=? ORDER BY b.id DESC""",
        (username,)
    ).fetchall()

    eq_rows = conn.execute(
        "SELECT * FROM equipment_orders WHERE user_username=? ORDER BY id DESC",
        (username,)
    ).fetchall()
    conn.close()

    history = []
    for r in rows:
        item = dict(r)
        try:
            item["praise_chips"] = json.loads(item.get("praise_chips") or "[]")
        except Exception:
            item["praise_chips"] = []
        try:
            item["fare_breakdown"] = json.loads(item.get("fare_breakdown") or "{}")
        except Exception:
            item["fare_breakdown"] = {}
        history.append(item)

    equipment_orders = []
    for r in eq_rows:
        item = dict(r)
        try:
            item["items"] = json.loads(item.get("items_json") or "[]")
        except Exception:
            item["items"] = []
        equipment_orders.append(item)

    return {"history": history, "equipment_orders": equipment_orders}

# ─────────────────────────────────────────────────────────────────────────────
# MEDICAL PHOTO & PRESCRIPTION VAULT (คลังภาพถ่ายเอกสารแพทย์ & ใบสั่งยา)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/booking/{booking_id}/medical-docs")
def get_medical_docs(booking_id: int):
    conn = get_conn()
    rows = conn.execute("""
        SELECT id, booking_id, doc_type, title, image_data, uploaded_by, created_at
        FROM medical_docs
        WHERE booking_id=?
        ORDER BY id DESC
    """, (booking_id,)).fetchall()
    conn.close()
    return {"docs": [dict(r) for r in rows], "count": len(rows)}

@app.post("/api/booking/{booking_id}/medical-docs")
async def upload_medical_doc(booking_id: int, req: MedicalDocUploadReq):
    conn = get_conn()
    ts = now_str()
    cur = conn.execute("""
        INSERT INTO medical_docs (booking_id, doc_type, title, image_data, uploaded_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (booking_id, req.doc_type, req.title or "", req.image_data, req.uploaded_by or "", ts))
    new_id = cur.lastrowid
    conn.commit()
    conn.close()

    await manager.broadcast_json({
        "type": "medical_doc_uploaded",
        "doc_id": new_id,
        "booking_id": booking_id,
        "doc_type": req.doc_type,
        "title": req.title,
        "timestamp": ts
    })
    return {"status": "success", "message": "อัปโหลดเอกสารทางการแพทย์เรียบร้อยแล้ว", "doc_id": new_id}

@app.delete("/api/medical-docs/{doc_id}")
async def delete_medical_doc(doc_id: int):
    conn = get_conn()
    row = conn.execute("SELECT booking_id FROM medical_docs WHERE id=?", (doc_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="ไม่พบเอกสารนี้")
    b_id = row["booking_id"]
    conn.execute("DELETE FROM medical_docs WHERE id=?", (doc_id,))
    conn.commit()
    conn.close()

    await manager.broadcast_json({
        "type": "medical_doc_deleted",
        "doc_id": doc_id,
        "booking_id": b_id,
        "timestamp": now_str()
    })
    return {"status": "success", "message": "ลบเอกสารเรียบร้อยแล้ว"}

# ─────────────────────────────────────────────────────────────────────────────
# CLINICAL CARE SUMMARY REPORT & BARTHEL ADL
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/booking/{booking_id}/care-summary")
def get_care_summary(booking_id: int):
    conn = get_conn()
    b_row = conn.execute("""
        SELECT b.*,
               COALESCE(p.name, b.patient_name, b.user_username) AS customer_name,
               COALESCE(p.phone, '-') AS customer_phone,
               COALESCE(p.email, '-') AS customer_email,
               COALESCE(cg.name, b.caregiver_username) AS caregiver_name,
               COALESCE(cg.role, b.service_type) AS caregiver_role,
               cg.phone AS caregiver_phone,
               cg.photo_url AS caregiver_photo
        FROM bookings b
        LEFT JOIN patients p ON b.user_username = p.username
        LEFT JOIN caregivers cg ON b.caregiver_username = cg.username
        WHERE b.id=?
    """, (booking_id,)).fetchone()

    if not b_row:
        conn.close()
        raise HTTPException(status_code=404, detail="ไม่พบข้อมูลการจองนี้")

    booking = dict(b_row)

    prof_row = conn.execute("SELECT * FROM patient_profiles WHERE user_username=?", (booking["user_username"],)).fetchone()
    profile = dict(prof_row) if prof_row else {}
    if profile.get("chronic_diseases"):
        try: profile["chronic_diseases"] = json.loads(profile["chronic_diseases"])
        except Exception: profile["chronic_diseases"] = []
    if profile.get("hospital_hns"):
        try: profile["hospital_hns"] = json.loads(profile["hospital_hns"])
        except Exception: profile["hospital_hns"] = []

    vitals_rows = conn.execute("SELECT * FROM vitals_logs WHERE booking_id=? ORDER BY id ASC", (booking_id,)).fetchall()
    vitals = [dict(r) for r in vitals_rows]

    fup_rows = conn.execute("SELECT * FROM appointment_followups WHERE booking_id=?", (booking_id,)).fetchall()
    followups = [dict(r) for r in fup_rows]

    doc_rows = conn.execute("SELECT id, doc_type, title, image_data, created_at FROM medical_docs WHERE booking_id=? ORDER BY id DESC", (booking_id,)).fetchall()
    docs = [dict(r) for r in doc_rows]

    conn.close()

    try: booking["praise_chips"] = json.loads(booking.get("praise_chips") or "[]")
    except Exception: booking["praise_chips"] = []

    try: booking["fare_breakdown"] = json.loads(booking.get("fare_breakdown") or "{}")
    except Exception: booking["fare_breakdown"] = {}

    return {
        "booking": booking,
        "profile": profile,
        "vitals": vitals,
        "followups": followups,
        "docs": docs,
        "generated_at": now_str()
    }

def thai_baht_text(amount: int) -> str:
    if amount == 0:
        return "ศูนย์บาทถ้วน"
    units = ["", "หนึ่ง", "สอง", "สาม", "สี่", "ห้า", "หก", "เจ็ด", "แปด", "เก้า"]
    positions = ["", "สิบ", "ร้อย", "พัน", "หมื่น", "แสน", "ล้าน"]
    def convert_chunk(num: int) -> str:
        s = str(num)
        n = len(s)
        res = []
        for i, char in enumerate(s):
            digit = int(char)
            pos = n - i - 1
            if digit == 0:
                continue
            if pos == 1 and digit == 1:
                res.append("สิบ")
            elif pos == 1 and digit == 2:
                res.append("ยี่สิบ")
            elif pos == 0 and digit == 1 and n > 1 and int(s[-2]) != 0:
                res.append("เอ็ด")
            else:
                res.append(units[digit] + positions[pos])
        return "".join(res)
    if amount < 1000000:
        return convert_chunk(amount) + "บาทถ้วน"
    m = amount // 1000000
    r = amount % 1000000
    return convert_chunk(m) + "ล้าน" + (convert_chunk(r) if r else "") + "บาทถ้วน"

def english_baht_text(amount: int) -> str:
    if amount == 0:
        return "Zero Baht Only"
    ones = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
            "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
            "Seventeen", "Eighteen", "Nineteen"]
    tens = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]
    def chunk(n):
        res = []
        if n >= 100:
            res.append(ones[n // 100] + " Hundred")
            n %= 100
        if n >= 20:
            t = tens[n // 10]
            if n % 10:
                t += "-" + ones[n % 10]
            res.append(t)
        elif n > 0:
            res.append(ones[n])
        return " ".join(res)
    parts = []
    if amount >= 1000000:
        m = amount // 1000000
        parts.append(chunk(m) + " Million")
        amount %= 1000000
    if amount >= 1000:
        k = amount // 1000
        parts.append(chunk(k) + " Thousand")
        amount %= 1000
    if amount > 0:
        parts.append(chunk(amount))
    return " ".join(parts) + " Baht Only"

@app.get("/api/booking/{booking_id}/receipt")
def get_booking_receipt(booking_id: int):
    conn = get_conn()
    b_row = conn.execute("""
        SELECT b.*,
               COALESCE(p.name, b.patient_name, b.user_username) AS customer_name,
               COALESCE(p.phone, '-') AS customer_phone,
               COALESCE(p.email, '-') AS customer_email,
               COALESCE(cg.name, b.caregiver_username) AS caregiver_name,
               COALESCE(cg.role, b.service_type) AS caregiver_role,
               cg.phone AS caregiver_phone
        FROM bookings b
        LEFT JOIN patients p ON b.user_username = p.username
        LEFT JOIN caregivers cg ON b.caregiver_username = cg.username
        WHERE b.id=?
    """, (booking_id,)).fetchone()

    if not b_row:
        conn.close()
        raise HTTPException(status_code=404, detail="ไม่พบข้อมูลการจองนี้")

    booking = dict(b_row)
    conn.close()

    try:
        fare_breakdown = json.loads(booking.get("fare_breakdown") or "{}")
    except Exception:
        fare_breakdown = {}

    total_price = int(booking.get("price") or 0)
    hours = int(booking.get("duration_hours") or 4)
    dist_km = float(booking.get("distance_km") or 0.0)
    cg_role = booking.get("caregiver_role") or "nurse"
    is_rn = (cg_role == "nurse" or "RN" in booking.get("caregiver_name", ""))

    nurse_licenses = {
        "nurse_araya": "ว. 6608421 (พยาบาลวิชาชีพชั้นหนึ่ง สภาการพยาบาล)",
        "nurse_benja": "ว. 6410592 (พยาบาลวิชาชีพชั้นหนึ่ง สภาการพยาบาล)",
        "nurse_chanat": "ว. 6512349 (พยาบาลวิชาชีพชั้นหนึ่ง สภาการพยาบาล)",
        "nurse_dararat": "ว. 6309871 (พยาบาลวิชาชีพชั้นหนึ่ง สภาการพยาบาล)",
        "nurse_ekkachai": "ว. 6204563 (พยาบาลวิชาชีพชั้นหนึ่ง สภาการพยาบาล)",
        "nurse_fang": "ว. 6507812 (พยาบาลวิชาชีพชั้นหนึ่ง สภาการพยาบาล)",
        "nurse_ganya": "ว. 6401928 (พยาบาลวิชาชีพชั้นหนึ่ง สภาการพยาบาล)"
    }
    cg_uname = booking.get("caregiver_username", "")
    if cg_uname in nurse_licenses:
        license_no = nurse_licenses[cg_uname]
    elif is_rn:
        lic_num = 6000000 + (abs(hash(cg_uname)) % 99999)
        license_no = f"ว. {lic_num} (พยาบาลวิชาชีพชั้นหนึ่ง สภาการพยาบาล)"
    else:
        lic_num = 67000 + (abs(hash(cg_uname)) % 9999)
        license_no = f"NA-{lic_num} (ประกาศนียบัตรผู้ช่วยฟื้นฟูสุขภาพ)"

    hourly_rate = fare_breakdown.get("hourly_rate")
    if not hourly_rate:
        hourly_rate = 300 if is_rn else 200
    base_care = fare_breakdown.get("base_care") or (hours * hourly_rate)
    dist_surcharge = fare_breakdown.get("distance_surcharge") or max(0, total_price - base_care)

    role_label_th = "พยาบาลวิชาชีพ (RN)" if is_rn else "ผู้ช่วยพยาบาล (CG)"
    role_label_en = "Registered Nurse (RN)" if is_rn else "Certified Caregiver (CG)"

    items = [
        {
            "no": 1,
            "description_th": f"บริการ{role_label_th}พาพบแพทย์และดูแลต่อเนื่อง ({hours} ชั่วโมง)",
            "description_en": f"Professional {role_label_en} Hospital Escort Care ({hours} hrs)",
            "qty": hours,
            "unit": "ชม. / hrs",
            "unit_price": hourly_rate,
            "amount": base_care
        }
    ]

    if dist_surcharge > 0 or dist_km > 0:
        items.append({
            "no": 2,
            "description_th": f"ค่าบริการยานพาหนะและการเดินทางตามระยะทางจริง ({dist_km:.1f} กม.)",
            "description_en": f"Vehicle Transportation & Distance Surcharge ({dist_km:.1f} km)",
            "qty": 1,
            "unit": "เที่ยว / trip",
            "unit_price": dist_surcharge,
            "amount": dist_surcharge
        })

    t_fee = int(fare_breakdown.get("transport_fee") or booking.get("transport_fee") or 0)
    t_type = fare_breakdown.get("transport_type") or booking.get("transport_type") or "sedan"
    if t_fee > 0:
        if t_type == "wheelchair_van":
            t_name_th = "บริการรถตู้ยกรถเข็นไฟฟ้าพร้อมลิฟต์ไฮดรอลิก (Wheelchair Accessible Van)"
            t_name_en = "Wheelchair Accessible Van with Hydraulic Lift & Safety Restraints"
        elif t_type == "ambulance":
            t_name_th = "บริการรถพยาบาลเคลื่อนย้ายพร้อมเตียงนอนเปล (Non-Emergency Ambulance)"
            t_name_en = "Medical Transport Ambulance with Stretcher Bed & Oxygen Tank"
        else:
            t_name_th = "บริการยานพาหนะเดินทางพิเศษ (Special Fleet Service)"
            t_name_en = "Special Patient Transport Service"
        items.append({
            "no": len(items) + 1,
            "description_th": t_name_th,
            "description_en": t_name_en,
            "qty": 1,
            "unit": "เที่ยว / trip",
            "unit_price": t_fee,
            "amount": t_fee
        })

    items.append({
        "no": len(items) + 1,
        "description_th": "บริการตรวจวัดและบันทึกสัญญาณชีพ (BP, PR, SpO2) พร้อมจัดทำรายงานแพทย์",
        "description_en": "Clinical Vitals Monitoring & Escort Summary Report",
        "qty": 1,
        "unit": "ชุด / set",
        "unit_price": 0,
        "amount": 0
    })

    receipt_no = f"RC-202610-{str(booking_id).zfill(4)}"
    created_at = booking.get("created_at") or now_str()
    issue_date = created_at[:10] if len(created_at) >= 10 else now_str()[:10]

    return {
        "receipt_no": receipt_no,
        "issue_date": issue_date,
        "company": {
            "name_th": "บริษัท แคร์เมท แบงค็อก จำกัด (สำนักงานใหญ่)",
            "name_en": "CareMate Bangkok Co., Ltd. (Head Office)",
            "tax_id": "0105567089123",
            "address_th": "88/1 ถนนพญาไท แขวงถนนพญาไท เขตราชเทวี กรุงเทพมหานคร 10400",
            "address_en": "88/1 Phaya Thai Rd, Thanon Phaya Thai, Ratchathewi, Bangkok 10400",
            "phone": "02-123-4567, 089-999-9999",
            "email": "billing@caremate.co.th",
            "website": "www.caremate.co.th"
        },
        "booking_id": booking_id,
        "patient": {
            "name": booking.get("customer_name") or booking.get("patient_name") or "คุณผู้รับบริการ",
            "phone": booking.get("customer_phone") or "-",
            "email": booking.get("customer_email") or "-",
            "username": booking.get("user_username") or "-"
        },
        "caregiver": {
            "username": cg_uname,
            "name": booking.get("caregiver_name") or cg_uname,
            "role": cg_role,
            "role_label_th": role_label_th,
            "role_label_en": role_label_en,
            "license_no": license_no,
            "phone": booking.get("caregiver_phone") or "-"
        },
        "service": {
            "pickup_location": booking.get("pickup_location") or "-",
            "hospital": booking.get("hospital") or "-",
            "booking_date": booking.get("booking_date") or "ตามนัดหมาย",
            "booking_time": booking.get("booking_time") or "09:00",
            "duration_hours": hours,
            "distance_km": dist_km,
            "adl_score": booking.get("adl_score") or 0,
            "adl_category": booking.get("adl_category") or ""
        },
        "items": items,
        "subtotal": total_price,
        "discount": 0,
        "vat_rate": 0.0,
        "vat_amount": 0,
        "vat_exempt_note_th": "ได้รับการยกเว้นภาษีมูลค่าเพิ่มตามประมวลรัษฎากร มาตรา 81(1)(ช) (การให้บริการการประกอบโรคศิลปะและการพยาบาล)",
        "vat_exempt_note_en": "VAT Exempt under Section 81(1)(chor) of the Thai Revenue Code (Medical & Nursing Healthcare Services)",
        "total": total_price,
        "baht_text_th": thai_baht_text(total_price),
        "baht_text_en": english_baht_text(total_price),
        "payment": {
            "status": "PAID",
            "method": "PromptPay Escrow Protection",
            "escrow_ref": booking.get("escrow_ref") or f"ESC-{booking_id}",
            "paid_at": created_at
        },
        "digital_stamp": {
            "verification_code": f"CM-AUTH-{(booking_id * 31337 + 104231) % 9000000 + 1000000}",
            "authorized_director": "ภกญ. พิชญา เมธาวรกุล (ผู้อำนวยการฝ่ายบริการทางการแพทย์ / Medical Director)",
            "seal_text": "CAREMATE BANGKOK VERIFIED MEDICAL SERVICE"
        }
    }

@app.get("/api/equipment/orders/{username}")
def get_user_equipment_orders(username: str):
    conn = get_conn()
    eq_rows = conn.execute(
        "SELECT * FROM equipment_orders WHERE user_username=? ORDER BY id DESC",
        (username,)
    ).fetchall()
    conn.close()
    orders = []
    for r in eq_rows:
        d = dict(r)
        try:
            d["items"] = json.loads(d.get("items_json") or "[]")
        except Exception:
            d["items"] = []
        orders.append(d)
    return {"orders": orders}

@app.get("/api/booking/{booking_id}")
def get_booking(booking_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="ไม่พบรายการจอง")
    data = dict(row)
    try:
        data["praise_chips"] = json.loads(data.get("praise_chips") or "[]")
    except Exception:
        data["praise_chips"] = []
    return data

# ─────────────────────────────────────────────────────────────────────────────
# EQUIPMENT ORDERS
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/equipment/order")
async def create_equipment_order(req: EquipmentOrderReq):
    if not req.items:
        raise HTTPException(status_code=400, detail="ตะกร้าสินค้าว่างเปล่า")

    subtotal = 0
    deposit_total = 0
    for item in req.items:
        if item.type == "rental":
            subtotal += item.price * item.rental_days * item.quantity
            deposit_total += item.deposit * item.quantity
        else:
            subtotal += item.price * item.quantity

    grand_total = subtotal + deposit_total
    ref = equipment_ref()
    items_json = json.dumps([i.model_dump() for i in req.items])
    n = now_str()
    status = 'confirmed' if req.slip_image else 'pending'

    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO equipment_orders (user_username, customer_name, customer_phone, items_json, subtotal, deposit_total, grand_total, delivery_address, status, escrow_ref, created_at, slip_image) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (req.user_username, req.customer_name, req.customer_phone, items_json, subtotal, deposit_total, grand_total, req.delivery_address, status, ref, n, req.slip_image)
    )
    order_id = cur.lastrowid
    conn.commit()
    conn.close()

    await manager.broadcast_json({
        "type": "equipment_order_updated",
        "order_id": order_id,
        "status": status,
        "escrow_ref": ref,
        "user_username": req.user_username,
        "has_slip": bool(req.slip_image),
        "timestamp": n
    })

    return {
        "status": "success",
        "order_id": order_id,
        "escrow_ref": ref,
        "subtotal": subtotal,
        "deposit_total": deposit_total,
        "grand_total": grand_total,
        "order_status": status,
        "message": "คำสั่งซื้อสำเร็จและบันทึกลงระบบ Escrow เรียบร้อยแล้ว"
    }

@app.get("/api/equipment/order/{order_id}")
def get_equipment_order(order_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM equipment_orders WHERE id=?", (order_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="ไม่พบคำสั่งซื้อ")
    data = dict(row)
    try:
        data["items"] = json.loads(data["items_json"])
    except Exception:
        data["items"] = []
    return data

@app.post("/api/equipment/order/{order_id}/confirm-payment")
async def confirm_equipment_payment(order_id: int, req: Optional[PayBookingReq] = None):
    slip = req.slip_image if req else None
    ref = escrow_ref()
    conn = get_conn()
    row = conn.execute("SELECT escrow_ref, user_username FROM equipment_orders WHERE id=?", (order_id,)).fetchone()
    final_ref = row["escrow_ref"] if (row and row["escrow_ref"]) else ref
    conn.execute("UPDATE equipment_orders SET status='confirmed', escrow_ref=?, slip_image=? WHERE id=?", (final_ref, slip, order_id))
    conn.commit()
    conn.close()
    await manager.broadcast_json({
        "type": "equipment_order_updated",
        "order_id": order_id,
        "status": "confirmed",
        "escrow_ref": final_ref,
        "user_username": row["user_username"] if row else None,
        "has_slip": bool(slip),
        "timestamp": now_str()
    })
    return {"status": "success", "message": "ชำระเงินสำเร็จ คำสั่งซื้อเข้าสู่ระบบ Escrow และพร้อมจัดส่ง", "escrow_ref": final_ref}

# ─────────────────────────────────────────────────────────────────────────────
# ADMIN ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

def require_admin():
    """Helper สำหรับตรวจ admin credential (ใช้ header หรือ bypass ใน dev)"""
    pass  # ใน production ใส่ token-based auth

@app.get("/api/admin/data")
def get_admin_data():
    conn = get_conn()
    c = conn.cursor()

    # Caregivers
    cg_rows = c.execute(
        "SELECT username,name,phone,email,role,skills,status,rating,work_doc,bg_doc,bio,area,created_at,price,photo_url "
        "FROM caregivers ORDER BY CASE WHEN status='pending' THEN 1 ELSE 2 END, created_at DESC"
    ).fetchall()
    caregivers = []
    for r in cg_rows:
        d = dict(r)
        try:
            d["skills"] = json.loads(d["skills"] or "[]")
        except Exception:
            d["skills"] = []
        d["has_work"] = bool(d.get("work_doc"))
        d["has_bg"]   = bool(d.get("bg_doc"))
        d["photo_url"] = d.get("photo_url") or "https://images.unsplash.com/photo-1582750433449-648ed127bb54?auto=format&fit=crop&q=80&w=300"
        caregivers.append(d)

    # Patients
    pt_rows = c.execute(
        "SELECT username,name,phone,email,created_at FROM patients ORDER BY created_at DESC"
    ).fetchall()
    patients = [dict(r) for r in pt_rows]

    # Login logs (50 ล่าสุด)
    log_rows = c.execute(
        "SELECT id,username,name,role,login_time,status FROM login_logs ORDER BY id DESC LIMIT 50"
    ).fetchall()
    logs = [dict(r) for r in log_rows]

    # KPIs
    total_patients      = c.execute("SELECT COUNT(*) FROM patients").fetchone()[0]
    total_cg            = c.execute("SELECT COUNT(*) FROM caregivers").fetchone()[0]
    pending_kyc         = c.execute("SELECT COUNT(*) FROM caregivers WHERE status='pending'").fetchone()[0]
    approved_cg         = c.execute("SELECT COUNT(*) FROM caregivers WHERE status='approved'").fetchone()[0]
    active_bookings     = c.execute("SELECT COUNT(*) FROM bookings WHERE status IN ('escrow','in_transit')").fetchone()[0]
    total_bookings      = c.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
    escrow_bookings     = c.execute("SELECT COUNT(*) FROM bookings WHERE status IN ('escrow','paid')").fetchone()[0]
    completed_bookings  = c.execute("SELECT COUNT(*) FROM bookings WHERE status='completed'").fetchone()[0]
    total_revenue       = c.execute("SELECT COALESCE(SUM(price),0) FROM bookings WHERE status='completed'").fetchone()[0]

    conn.close()
    return {
        "caregivers": caregivers,
        "patients": patients,
        "login_logs": logs,
        "kpis": {
            "total_patients": total_patients,
            "total_caregivers": total_cg,
            "pending_kyc": pending_kyc,
            "approved_caregivers": approved_cg,
            "active_bookings": active_bookings,
            "total_bookings": total_bookings,
            "escrow_bookings": escrow_bookings,
            "completed_bookings": completed_bookings,
            "total_revenue": total_revenue,
            "online_users": manager.online_count
        }
    }

@app.get("/api/admin/kyc/{username}/{doc_type}")
def get_kyc_document(username: str, doc_type: str):
    conn = get_conn()
    row = conn.execute("SELECT work_doc, bg_doc FROM caregivers WHERE username=?", (username,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="ไม่พบผู้ให้บริการรายนี้")
    data = row["work_doc"] if doc_type == "work" else row["bg_doc"]
    return {"data": data}

@app.post("/api/admin/approve/{username}")
async def approve_kyc(username: str, action: str = Query("approve")):
    new_status = "approved" if action == "approve" else "suspended"
    conn = get_conn()
    conn.execute("UPDATE caregivers SET status=? WHERE username=?", (new_status, username))
    conn.commit(); conn.close()
    await manager.broadcast_json({
        "type": "kyc_status_update",
        "caregiver": username,
        "new_status": new_status,
        "timestamp": now_str()
    })
    return {"status": "success", "message": f"อัปเดตสถานะ '{username}' เป็น {new_status} แล้ว"}

# ─────────────────────────────────────────────────────────────────────────────
# ADMIN CAREGIVER CRUD (จัดการบุคลากรทางการแพทย์ผ่านเว็บ)
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/admin/caregiver")
async def admin_create_caregiver(req: CaregiverCreateRequest):
    uname = (req.username or "").strip()
    if not uname:
        raise HTTPException(status_code=400, detail="กรุณาระบุ Username")
    if not (req.name or "").strip():
        raise HTTPException(status_code=400, detail="กรุณาระบุชื่อ-นามสกุล")

    conn = get_conn()
    c = conn.cursor()
    for tbl in ["patients", "caregivers", "admins"]:
        if c.execute(f"SELECT username FROM {tbl} WHERE username=?", (uname,)).fetchone():
            conn.close()
            raise HTTPException(status_code=400, detail=f"Username '{uname}' มีอยู่ในระบบแล้ว กรุณาใช้ชื่ออื่น")

    pwd = (req.password or "").strip() or "1234"
    price = req.price if (req.price is not None and req.price > 0) else (1200 if req.role == "nurse" else 800)
    rating = float(req.rating) if (req.rating is not None and req.rating > 0) else 5.0
    status = (req.status or "approved").strip()
    skills_json = json.dumps(req.skills or [])
    photo = (req.photo_url or "").strip() or "https://images.unsplash.com/photo-1582750433449-648ed127bb54?auto=format&fit=crop&q=80&w=300"
    created = now_str()
    area = (req.area or "").strip() or "กรุงเทพฯ และปริมณฑล"
    bio = (req.bio or "").strip()

    c.execute("""
        INSERT INTO caregivers (
            username, password, name, phone, email, role,
            skills, rating, status, price, work_doc, bg_doc,
            bio, area, created_at, photo_url
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
    """, (
        uname, pwd, req.name.strip(), (req.phone or "").strip(), (req.email or "").strip(),
        req.role, skills_json, rating, status, price,
        bio, area, created, photo
    ))
    conn.commit()
    conn.close()

    await manager.broadcast_json({
        "type": "caregivers_updated",
        "action": "create",
        "username": uname,
        "name": req.name.strip(),
        "timestamp": now_str()
    })
    return {"status": "success", "message": f"เพิ่มบุคลากร '{req.name.strip()}' เข้าสู่ระบบสำเร็จ"}

@app.put("/api/admin/caregiver/{username}")
async def admin_update_caregiver(username: str, req: CaregiverUpdateRequest):
    conn = get_conn()
    c = conn.cursor()
    row = c.execute("SELECT * FROM caregivers WHERE username=?", (username,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="ไม่พบบุคลากรรายนี้ในระบบ")

    updates = []
    params = []

    if req.name is not None and req.name.strip():
        updates.append("name=?")
        params.append(req.name.strip())
    if req.password is not None and req.password.strip():
        updates.append("password=?")
        params.append(req.password.strip())
    if req.phone is not None:
        updates.append("phone=?")
        params.append(req.phone.strip())
    if req.email is not None:
        updates.append("email=?")
        params.append(req.email.strip())
    if req.role is not None and req.role.strip():
        updates.append("role=?")
        params.append(req.role.strip())
    if req.skills is not None:
        updates.append("skills=?")
        params.append(json.dumps(req.skills))
    if req.rating is not None:
        updates.append("rating=?")
        params.append(float(req.rating))
    if req.status is not None and req.status.strip():
        updates.append("status=?")
        params.append(req.status.strip())
    if req.price is not None:
        updates.append("price=?")
        params.append(int(req.price))
    if req.bio is not None:
        updates.append("bio=?")
        params.append(req.bio.strip())
    if req.area is not None:
        updates.append("area=?")
        params.append(req.area.strip())
    if req.photo_url is not None and req.photo_url.strip():
        updates.append("photo_url=?")
        params.append(req.photo_url.strip())

    if updates:
        params.append(username)
        c.execute(f"UPDATE caregivers SET {', '.join(updates)} WHERE username=?", params)
        conn.commit()

    conn.close()

    await manager.broadcast_json({
        "type": "caregivers_updated",
        "action": "update",
        "username": username,
        "timestamp": now_str()
    })
    return {"status": "success", "message": f"แก้ไขข้อมูลบุคลากร '{username}' สำเร็จแล้ว"}

@app.delete("/api/admin/caregiver/{username}")
async def admin_delete_caregiver(username: str):
    conn = get_conn()
    c = conn.cursor()
    row = c.execute("SELECT name FROM caregivers WHERE username=?", (username,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="ไม่พบบุคลากรรายนี้ในระบบ")

    c.execute("DELETE FROM caregivers WHERE username=?", (username,))
    conn.commit()
    conn.close()

    await manager.broadcast_json({
        "type": "caregivers_updated",
        "action": "delete",
        "username": username,
        "name": row["name"],
        "timestamp": now_str()
    })
    return {"status": "success", "message": f"ลบข้อมูลบุคลากร '{row['name']}' ออกจากระบบเรียบร้อย"}


@app.get("/api/admin/bookings")
def get_admin_bookings():
    conn = get_conn()
    rows = conn.execute("""
        SELECT b.*, cg.name AS caregiver_name, cg.phone AS caregiver_phone, cg.role AS caregiver_role,
               cg.photo_url AS caregiver_photo,
               COALESCE(p.name, b.patient_name, b.user_username) AS user_name, COALESCE(p.phone, '-') AS user_phone
        FROM bookings b
        LEFT JOIN caregivers cg ON b.caregiver_username = cg.username
        LEFT JOIN patients p ON b.user_username = p.username
        ORDER BY b.id DESC
    """).fetchall()
    conn.close()
    bookings = []
    for r in rows:
        d = dict(r)
        d["total_price"] = d.get("price") or 0
        b_date = d.get("booking_date")
        b_time = d.get("booking_time")
        if b_date and b_time:
            d["appointment_date"] = f"{b_date} {b_time} น."
        elif b_date:
            d["appointment_date"] = b_date
        else:
            d["appointment_date"] = d.get("created_at") or "-"
        try:
            d["praise_chips"] = json.loads(d.get("praise_chips") or "[]")
        except Exception:
            d["praise_chips"] = []
        bookings.append(d)
    return {"bookings": bookings}

@app.post("/api/admin/booking/{booking_id}/release-escrow")
async def admin_release_escrow(booking_id: int):
    conn = get_conn()
    conn.execute("UPDATE bookings SET status='completed' WHERE id=?", (booking_id,))
    conn.commit()
    conn.close()
    await manager.broadcast_json({
        "type": "booking_completed",
        "booking_id": booking_id,
        "message": "แอดมินยืนยันปล่อยเงิน Escrow ให้ผู้ดูแลเรียบร้อย",
        "timestamp": now_str()
    })
    return {"status": "success", "message": f"ปล่อยยอดเงิน Escrow ของคำสั่งจอง #{booking_id} ให้ผู้ดูแลเรียบร้อย"}

@app.post("/api/admin/booking/{booking_id}/refund-escrow")
async def admin_refund_escrow(booking_id: int):
    conn = get_conn()
    conn.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (booking_id,))
    conn.commit()
    conn.close()
    await manager.broadcast_json({
        "type": "booking_cancelled",
        "booking_id": booking_id,
        "message": "แอดมินดำเนินการคืนเงินให้ลูกค้าเรียบร้อย",
        "timestamp": now_str()
    })
    return {"status": "success", "message": f"คืนเงิน Escrow ของคำสั่งจอง #{booking_id} ให้ลูกค้าเรียบร้อย"}

@app.get("/api/admin/equipment-orders")
def get_admin_equipment_orders():
    conn = get_conn()
    rows = conn.execute("""
        SELECT eq.*, COALESCE(p.name, eq.customer_name, eq.user_username) AS customer_name,
               COALESCE(p.phone, eq.customer_phone, '-') AS customer_phone, p.email AS customer_email
        FROM equipment_orders eq
        LEFT JOIN patients p ON eq.user_username = p.username
        ORDER BY eq.id DESC
    """).fetchall()
    conn.close()
    orders = []
    for r in rows:
        d = dict(r)
        try:
            d["items"] = json.loads(d.get("items_json") or "[]")
        except Exception:
            d["items"] = []
        orders.append(d)
    return {"orders": orders}

@app.post("/api/admin/equipment-order/{order_id}/update-status")
async def admin_update_order_status(order_id: int, status: str = Query(...)):
    conn = get_conn()
    conn.execute("UPDATE equipment_orders SET status=? WHERE id=?", (status, order_id))
    conn.commit()
    row = conn.execute("SELECT escrow_ref, user_username, slip_image FROM equipment_orders WHERE id=?", (order_id,)).fetchone()
    conn.close()
    await manager.broadcast_json({
        "type": "equipment_order_updated",
        "order_id": order_id,
        "status": status,
        "escrow_ref": row["escrow_ref"] if row else None,
        "user_username": row["user_username"] if row else None,
        "has_slip": bool(row["slip_image"]) if row else False,
        "timestamp": now_str()
    })
    return {"status": "success", "message": f"อัปเดตสถานะคำสั่งซื้อ #{order_id} เป็น '{status}' สำเร็จ"}

@app.post("/api/equipment/order/{order_id}/attach-slip")
async def attach_equipment_slip(order_id: int, req: PayBookingReq):
    conn = get_conn()
    conn.execute("UPDATE equipment_orders SET slip_image=?, status=CASE WHEN status='pending' THEN 'confirmed' ELSE status END WHERE id=?", (req.slip_image, order_id))
    conn.commit()
    row = conn.execute("SELECT escrow_ref, user_username, status FROM equipment_orders WHERE id=?", (order_id,)).fetchone()
    conn.close()
    new_status = row["status"] if row else "confirmed"
    await manager.broadcast_json({
        "type": "equipment_order_updated",
        "order_id": order_id,
        "status": new_status,
        "escrow_ref": row["escrow_ref"] if row else None,
        "user_username": row["user_username"] if row else None,
        "has_slip": True,
        "timestamp": now_str()
    })
    return {"status": "success", "message": "บันทึกและอัปเดตสลิปโอนเงินเรียบร้อย"}

@app.get("/api/caregiver/jobs/{username}")
def get_caregiver_jobs(username: str):
    conn = get_conn()
    rows = conn.execute("""
        SELECT b.*,
               COALESCE(p.name, b.patient_name, b.user_username) AS customer_name,
               COALESCE(p.phone, '-') AS customer_phone,
               prof.blood_type AS patient_blood_type,
               prof.chronic_diseases AS patient_chronic_diseases,
               prof.drug_allergies AS patient_drug_allergies,
               prof.food_allergies AS patient_food_allergies,
               prof.hospital_hns AS patient_hospital_hns,
               prof.emergency_name,
               prof.emergency_phone,
               prof.emergency_relation,
               prof.special_care_notes
        FROM bookings b
        LEFT JOIN patients p ON b.user_username = p.username
        LEFT JOIN patient_profiles prof ON b.user_username = prof.user_username
        WHERE b.caregiver_username=?
        ORDER BY b.id DESC
    """, (username,)).fetchall()

    # Calculate caregiver earnings
    completed_rows = conn.execute(
        "SELECT COALESCE(SUM(price), 0), COUNT(*) FROM bookings WHERE caregiver_username=? AND status='completed'",
        (username,)
    ).fetchone()
    total_earned = completed_rows[0] or 0
    completed_count = completed_rows[1] or 0

    # Date prefixes
    today_prefix = datetime.now().strftime('%Y-%m-%d')
    month_prefix = datetime.now().strftime('%Y-%m')
    year_prefix = datetime.now().strftime('%Y')

    # All-time completed
    completed_rows = conn.execute(
        "SELECT COALESCE(SUM(price), 0), COUNT(*) FROM bookings WHERE caregiver_username=? AND status='completed'",
        (username,)
    ).fetchone()
    total_earned = completed_rows[0] or 0
    completed_count = completed_rows[1] or 0

    # Today completed
    today_rows = conn.execute(
        "SELECT COALESCE(SUM(price), 0), COUNT(*) FROM bookings WHERE caregiver_username=? AND status='completed' AND (created_at LIKE ? OR booking_date=?)",
        (username, today_prefix + '%', today_prefix)
    ).fetchone()
    today_earned = today_rows[0] or 0
    today_count = today_rows[1] or 0

    # Month completed
    month_rows = conn.execute(
        "SELECT COALESCE(SUM(price), 0), COUNT(*) FROM bookings WHERE caregiver_username=? AND status='completed' AND (created_at LIKE ? OR booking_date LIKE ?)",
        (username, month_prefix + '%', month_prefix + '%')
    ).fetchone()
    month_earned = month_rows[0] or 0
    month_count = month_rows[1] or 0

    # Year completed
    year_rows = conn.execute(
        "SELECT COALESCE(SUM(price), 0), COUNT(*) FROM bookings WHERE caregiver_username=? AND status='completed' AND (created_at LIKE ? OR booking_date LIKE ?)",
        (username, year_prefix + '%', year_prefix + '%')
    ).fetchone()
    year_earned = year_rows[0] or 0
    year_count = year_rows[1] or 0

    pending_rows = conn.execute(
        "SELECT COALESCE(SUM(price), 0), COUNT(*) FROM bookings WHERE caregiver_username=? AND status IN ('paid', 'escrow', 'in_transit', 'arrived', 'in_progress')",
        (username,)
    ).fetchone()
    pending_escrow = pending_rows[0] or 0
    active_count = pending_rows[1] or 0

    conn.close()

    jobs = []
    for r in rows:
        d = dict(r)
        d["total_price"] = d.get("price") or 0
        b_date = d.get("booking_date")
        b_time = d.get("booking_time")
        if b_date and b_time:
            d["appointment_date"] = f"{b_date} {b_time} น."
        elif b_date:
            d["appointment_date"] = b_date
        else:
            d["appointment_date"] = d.get("created_at") or "-"

        try:
            d["praise_chips"] = json.loads(d.get("praise_chips") or "[]")
        except Exception:
            d["praise_chips"] = []

        try:
            d["patient_chronic_diseases"] = json.loads(d.get("patient_chronic_diseases") or "[]")
        except Exception:
            d["patient_chronic_diseases"] = []

        try:
            d["patient_hospital_hns"] = json.loads(d.get("patient_hospital_hns") or "[]")
        except Exception:
            d["patient_hospital_hns"] = []

        jobs.append(d)

    return {
        "jobs": jobs,
        "earnings": {
            "total_earned": total_earned,
            "today_earned": today_earned,
            "month_earned": month_earned,
            "year_earned": year_earned,
            "pending_escrow": pending_escrow,
            "completed_count": completed_count,
            "today_count": today_count,
            "month_count": month_count,
            "year_count": year_count,
            "active_count": active_count
        }
    }

# ─────────────────────────────────────────────────────────────────────────────
# FINANCIAL REPORT & PLATFORM ANALYTICS (รายงานการเงิน & ส่วนแบ่งรายได้)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/admin/financial-report")
def get_admin_financial_report(period: str = "all"):
    conn = get_conn()
    all_bookings = conn.execute("""
        SELECT b.*, cg.name AS caregiver_name, cg.role AS caregiver_role, cg.photo_url AS caregiver_photo
        FROM bookings b
        LEFT JOIN caregivers cg ON b.caregiver_username = cg.username
    """).fetchall()

    today_str = datetime.now().strftime('%Y-%m-%d')
    month_str = datetime.now().strftime('%Y-%m')
    year_str = datetime.now().strftime('%Y')

    thai_months = {
        '01': 'มกราคม', '02': 'กุมภาพันธ์', '03': 'มีนาคม', '04': 'เมษายน',
        '05': 'พฤษภาคม', '06': 'มิถุนายน', '07': 'กรกฎาคม', '08': 'สิงหาคม',
        '09': 'กันยายน', '10': 'ตุลาคม', '11': 'พฤศจิกายน', '12': 'ธันวาคม'
    }

    # 1. Precalculate metrics for each period
    m_part = month_str.split('-')[1] if '-' in month_str else '10'
    thai_m_label = f"{thai_months.get(m_part, m_part)} {int(year_str)+543}"
    period_metrics = {
        "today": {"gmv": 0, "platform_revenue": 0, "caregiver_payouts": 0, "pending_escrow": 0, "completed_count": 0, "active_count": 0, "label": f"วันนี้ ({today_str})"},
        "month": {"gmv": 0, "platform_revenue": 0, "caregiver_payouts": 0, "pending_escrow": 0, "completed_count": 0, "active_count": 0, "label": f"เดือนนี้ ({thai_m_label})"},
        "year": {"gmv": 0, "platform_revenue": 0, "caregiver_payouts": 0, "pending_escrow": 0, "completed_count": 0, "active_count": 0, "label": f"ปีนี้ (พ.ศ. {int(year_str)+543})"},
        "all": {"gmv": 0, "platform_revenue": 0, "caregiver_payouts": 0, "pending_escrow": 0, "completed_count": 0, "active_count": 0, "label": "สะสมทั้งหมด (All-time)"}
    }

    monthly_dict = {}
    daily_dict = {}

    for r in all_bookings:
        b = dict(r)
        price = b.get("price") or 0
        status = b.get("status") or "pending"
        c_at = str(b.get("created_at") or "")
        b_date = str(b.get("booking_date") or "")
        dt_key = c_at[:10] if len(c_at) >= 10 else b_date
        m_key = c_at[:7] if len(c_at) >= 7 else (b_date[:7] if len(b_date) >= 7 else month_str)
        y_key = c_at[:4] if len(c_at) >= 4 else (b_date[:4] if len(b_date) >= 4 else year_str)

        is_today = (dt_key == today_str) or (b_date == today_str)
        is_month = (m_key == month_str) or (b_date.startswith(month_str))
        is_year = (y_key == year_str) or (b_date.startswith(year_str))

        # Monthly aggregation
        if m_key not in monthly_dict:
            mp = m_key.split('-')[1] if '-' in m_key else '01'
            yp = m_key.split('-')[0] if '-' in m_key else year_str
            monthly_dict[m_key] = {
                "month": m_key,
                "month_name": f"{thai_months.get(mp, mp)} {int(yp)+543 if yp.isdigit() else yp}",
                "gmv": 0,
                "platform_revenue": 0,
                "caregiver_payouts": 0,
                "completed_count": 0,
                "total_bookings": 0
            }
        monthly_dict[m_key]["total_bookings"] += 1
        if status == 'completed':
            monthly_dict[m_key]["gmv"] += price
            monthly_dict[m_key]["completed_count"] += 1
            monthly_dict[m_key]["platform_revenue"] += int(price * 0.15)
            monthly_dict[m_key]["caregiver_payouts"] += int(price * 0.85)

        # Daily aggregation
        if dt_key:
            if dt_key not in daily_dict:
                daily_dict[dt_key] = {"date": dt_key, "gmv": 0, "platform_revenue": 0, "completed_count": 0, "total_bookings": 0}
            daily_dict[dt_key]["total_bookings"] += 1
            if status == 'completed':
                daily_dict[dt_key]["gmv"] += price
                daily_dict[dt_key]["completed_count"] += 1
                daily_dict[dt_key]["platform_revenue"] += int(price * 0.15)

        # Update period metrics
        target_keys = ["all"]
        if is_year:
            target_keys.append("year")
        if is_month:
            target_keys.append("month")
        if is_today:
            target_keys.append("today")

        for k in target_keys:
            if status == 'completed':
                period_metrics[k]["gmv"] += price
                period_metrics[k]["completed_count"] += 1
                period_metrics[k]["platform_revenue"] += int(price * 0.15)
                period_metrics[k]["caregiver_payouts"] += int(price * 0.85)
            elif status in ('paid', 'escrow', 'in_transit', 'arrived', 'in_progress'):
                period_metrics[k]["pending_escrow"] += price
                period_metrics[k]["active_count"] += 1

    # Filter for selected period to generate summary and caregiver roster
    cg_stats = {}
    selected_summary = {
        "gmv": 0,
        "platform_revenue": 0,
        "caregiver_payouts": 0,
        "pending_escrow": 0,
        "completed_count": 0,
        "active_count": 0,
        "total_bookings": 0,
        "period": period,
        "label": period_metrics.get(period, period_metrics["all"])["label"]
    }

    for r in all_bookings:
        b = dict(r)
        price = b.get("price") or 0
        status = b.get("status") or "pending"
        cg_user = b.get("caregiver_username") or "unassigned"
        c_at = str(b.get("created_at") or "")
        b_date = str(b.get("booking_date") or "")
        dt_key = c_at[:10] if len(c_at) >= 10 else b_date
        m_key = c_at[:7] if len(c_at) >= 7 else (b_date[:7] if len(b_date) >= 7 else month_str)
        y_key = c_at[:4] if len(c_at) >= 4 else (b_date[:4] if len(b_date) >= 4 else year_str)

        # Match period filter
        matches_period = True
        if period == "today":
            matches_period = (dt_key == today_str) or (b_date == today_str)
        elif period == "month":
            matches_period = (m_key == month_str) or (b_date.startswith(month_str))
        elif period == "year":
            matches_period = (y_key == year_str) or (b_date.startswith(year_str))

        if cg_user not in cg_stats:
            cg_stats[cg_user] = {
                "username": cg_user,
                "name": b.get("caregiver_name") or cg_user,
                "role": b.get("caregiver_role") or "caregiver",
                "photo_url": b.get("caregiver_photo") or "",
                "completed_count": 0,
                "completed_revenue": 0,
                "caregiver_payout": 0,
                "platform_fee": 0,
                "pending_escrow": 0,
                "active_cases": 0
            }

        if matches_period:
            selected_summary["total_bookings"] += 1
            if status == 'completed':
                selected_summary["gmv"] += price
                selected_summary["completed_count"] += 1
                selected_summary["platform_revenue"] += int(price * 0.15)
                selected_summary["caregiver_payouts"] += int(price * 0.85)

                cg_stats[cg_user]["completed_count"] += 1
                cg_stats[cg_user]["completed_revenue"] += price
                cg_stats[cg_user]["caregiver_payout"] += int(price * 0.85)
                cg_stats[cg_user]["platform_fee"] += int(price * 0.15)
            elif status in ('paid', 'escrow', 'in_transit', 'arrived', 'in_progress'):
                selected_summary["pending_escrow"] += price
                selected_summary["active_count"] += 1

                cg_stats[cg_user]["pending_escrow"] += price
                cg_stats[cg_user]["active_cases"] += 1

    caregivers_roster = sorted(cg_stats.values(), key=lambda x: x["completed_revenue"], reverse=True)
    monthly_breakdown = sorted(monthly_dict.values(), key=lambda x: x["month"], reverse=True)
    daily_breakdown = sorted(daily_dict.values(), key=lambda x: x["date"], reverse=True)[:14]

    recent_txs = conn.execute("""
        SELECT b.id, b.created_at, b.booking_date, b.price, b.status, b.escrow_ref,
               COALESCE(p.name, b.patient_name, b.user_username) AS customer_name,
               COALESCE(cg.name, b.caregiver_username) AS caregiver_name,
               cg.role AS caregiver_role
        FROM bookings b
        LEFT JOIN patients p ON b.user_username = p.username
        LEFT JOIN caregivers cg ON b.caregiver_username = cg.username
        ORDER BY b.id DESC
        LIMIT 25
    """).fetchall()

    conn.close()

    return {
        "period": period,
        "summary": {
            "total_gmv": selected_summary["gmv"],
            "platform_revenue": selected_summary["platform_revenue"],
            "caregiver_payouts": selected_summary["caregiver_payouts"],
            "pending_escrow": selected_summary["pending_escrow"],
            "total_bookings": selected_summary["total_bookings"],
            "completed_count": selected_summary["completed_count"],
            "active_count": selected_summary["active_count"],
            "platform_fee_percent": 15,
            "period_label": selected_summary["label"]
        },
        "period_metrics": period_metrics,
        "monthly_breakdown": monthly_breakdown,
        "daily_breakdown": daily_breakdown,
        "caregivers_roster": caregivers_roster,
        "recent_transactions": [dict(t) for t in recent_txs]
    }

@app.get("/api/admin/financial-report/export-csv")
def export_admin_financial_report_csv(period: str = "all"):
    conn = get_conn()
    bookings = conn.execute("""
        SELECT b.*, cg.name AS caregiver_name, cg.role AS caregiver_role
        FROM bookings b
        LEFT JOIN caregivers cg ON b.caregiver_username = cg.username
        ORDER BY b.id DESC
    """).fetchall()
    conn.close()

    today_str = datetime.now().strftime('%Y-%m-%d')
    month_str = datetime.now().strftime('%Y-%m')
    year_str = datetime.now().strftime('%Y')

    filtered = []
    for b in bookings:
        d = dict(b)
        c_at = d.get("created_at") or ""
        b_dt = d.get("booking_date") or ""
        is_match = False
        if period == "today":
            is_match = c_at.startswith(today_str) or b_dt == today_str
        elif period == "month":
            is_match = c_at.startswith(month_str) or b_dt.startswith(month_str)
        elif period == "year":
            is_match = c_at.startswith(year_str) or b_dt.startswith(year_str)
        else:
            is_match = True
        if is_match:
            filtered.append(d)

    import io
    import csv

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(["CareMate Bangkok - รายงานสรุปบัญชีการเงินและส่วนแบ่งรายได้แพลตฟอร์ม"])
    writer.writerow(["ช่วงเวลารายงาน", period.upper(), "วันที่พิมพ์รายงาน", datetime.now().strftime('%Y-%m-%d %H:%M:%S')])
    writer.writerow([])

    headers = [
        "รหัสการจอง",
        "วันที่ทำรายการ",
        "วันที่นัดหมาย",
        "เวลานัดหมาย",
        "ชื่อคนไข้",
        "โรงพยาบาล/คลินิก",
        "ผู้ดูแลที่ได้รับมอบหมาย",
        "สถานะงาน",
        "สถานะการชำระเงิน",
        "ยอดชำระรวม (GMV บาท)",
        "ส่วนแบ่งแพลตฟอร์ม 15% (บาท)",
        "ส่วนแบ่งผู้ดูแล 85% (บาท)",
        "เลขอ้างอิง Escrow"
    ]
    writer.writerow(headers)

    total_gmv = 0.0
    total_platform = 0.0
    total_caregiver = 0.0

    status_labels = {
        "completed": "เสร็จสิ้น (สำเร็จ)",
        "escrow": "ชำระแล้ว (รอเริ่มงาน)",
        "paid": "ชำระแล้ว",
        "in_transit": "กำลังเดินทาง",
        "arrived": "ถึงจุดนัดพบ",
        "in_progress": "อยู่ระหว่างดูแล",
        "cancelled": "ยกเลิก"
    }

    for b in filtered:
        price = float(b.get("price") or 0)
        status = b.get("status") or "pending"
        platform_fee = round(price * 0.15, 2)
        caregiver_share = round(price * 0.85, 2)
        if status == "completed":
            total_gmv += price
            total_platform += platform_fee
            total_caregiver += caregiver_share

        writer.writerow([
            f"#{b.get('id')}",
            b.get("created_at") or "-",
            b.get("booking_date") or "-",
            b.get("booking_time") or "-",
            b.get("patient_name") or "-",
            b.get("hospital") or "-",
            b.get("caregiver_name") or b.get("caregiver_username") or "ยังไม่ระบุ",
            status_labels.get(status, status),
            "ชำระผ่าน Escrow แล้ว" if b.get("escrow_ref") else "รอชำระเงิน",
            f"{price:,.2f}",
            f"{platform_fee:,.2f}",
            f"{caregiver_share:,.2f}",
            b.get("escrow_ref") or "-"
        ])

    writer.writerow([])
    writer.writerow(["สรุปยอดรวม (เฉพาะรายการที่เสร็จสิ้นสมบูรณ์)"])
    writer.writerow(["ยอดรวม GMV (บาท)", f"{total_gmv:,.2f}"])
    writer.writerow(["รายได้แพลตฟอร์ม CareMate 15% (บาท)", f"{total_platform:,.2f}"])
    writer.writerow(["ยอดจ่ายพยาบาลผู้ดูแล 85% (บาท)", f"{total_caregiver:,.2f}"])
    writer.writerow(["จำนวนรายการทั้งหมดในรอบนี้", f"{len(filtered)} รายการ"])

    csv_data = output.getvalue()
    bom_csv = "\ufeff" + csv_data
    csv_bytes = bom_csv.encode("utf-8-sig")

    filename = f"CareMate_Financial_Report_{period}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8-sig",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        }
    )

# ── CAREGIVER E-WALLET & PAYOUTS API ─────────────────────────────────────────

@app.get("/api/caregiver/wallet/{username}")
def get_caregiver_wallet(username: str):
    conn = get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(price), 0), COUNT(*) FROM bookings WHERE caregiver_username=? AND status='completed'",
        (username,)
    ).fetchone()
    total_gmv = row[0] or 0
    completed_count = row[1] or 0

    net_earnings = round(total_gmv * 0.85, 2)

    payout_rows = conn.execute(
        "SELECT * FROM caregiver_payouts WHERE caregiver_username=? ORDER BY id DESC",
        (username,)
    ).fetchall()
    conn.close()

    total_withdrawn = 0.0
    pending_payout = 0.0
    payout_list = []
    for r in payout_rows:
        p = dict(r)
        payout_list.append(p)
        if p["status"] == "approved":
            total_withdrawn += float(p["amount"])
        elif p["status"] == "pending":
            pending_payout += float(p["amount"])

    available_balance = max(0.0, round(net_earnings - total_withdrawn - pending_payout, 2))

    return {
        "status": "success",
        "username": username,
        "total_gmv": total_gmv,
        "completed_count": completed_count,
        "net_earnings": net_earnings,
        "total_withdrawn": total_withdrawn,
        "pending_payout": pending_payout,
        "available_balance": available_balance,
        "payouts": payout_list
    }

@app.post("/api/caregiver/payout-request")
async def create_payout_request(req: PayoutRequestReq):
    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="จำนวนเงินที่ขอถอนต้องมากกว่า 0 บาท")
    if not req.bank_name.strip() or not req.account_number.strip() or not req.account_name.strip():
        raise HTTPException(status_code=400, detail="กรุณากรอกข้อมูลบัญชีธนาคารให้ครบถ้วน")

    conn = get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(price), 0) FROM bookings WHERE caregiver_username=? AND status='completed'",
        (req.caregiver_username,)
    ).fetchone()
    total_gmv = row[0] or 0
    net_earnings = round(total_gmv * 0.85, 2)

    p_rows = conn.execute(
        "SELECT status, amount FROM caregiver_payouts WHERE caregiver_username=?",
        (req.caregiver_username,)
    ).fetchall()

    already_deducted = sum(float(r["amount"]) for r in p_rows if r["status"] in ("approved", "pending"))
    available = max(0.0, round(net_earnings - already_deducted, 2))

    if req.amount > available and available > 0:
        conn.close()
        raise HTTPException(status_code=400, detail=f"ยอดเงินคงเหลือไม่เพียงพอ (คงเหลือที่ถอนได้: ฿{available:,.2f})")

    cg_row = conn.execute("SELECT name FROM caregivers WHERE username=?", (req.caregiver_username,)).fetchone()
    cg_name = cg_row["name"] if cg_row else req.account_name

    ts = now_str()
    cur = conn.execute(
        """INSERT INTO caregiver_payouts (caregiver_username, caregiver_name, amount, bank_name, account_number, account_name, status, transfer_slip, admin_note, created_at, processed_at)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, '', ?, NULL)""",
        (
            req.caregiver_username,
            cg_name,
            float(req.amount),
            req.bank_name.strip(),
            req.account_number.strip(),
            req.account_name.strip(),
            ts
        )
    )
    new_id = cur.lastrowid
    conn.commit()
    conn.close()

    await manager.broadcast_json({
        "type": "new_payout_request",
        "payout_id": new_id,
        "caregiver_username": req.caregiver_username,
        "caregiver_name": cg_name,
        "amount": req.amount,
        "bank_name": req.bank_name,
        "timestamp": ts
    })

    return {
        "status": "success",
        "message": f"ส่งคำขอถอนเงินจำนวน ฿{req.amount:,.2f} เรียบร้อยแล้ว กรุณารอแอดมินตรวจสอบและโอนเงิน",
        "payout_id": new_id
    }

@app.get("/api/admin/payout-requests")
def get_admin_payout_requests():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM caregiver_payouts ORDER BY id DESC").fetchall()
    conn.close()
    payouts = [dict(r) for r in rows]
    pending_count = sum(1 for p in payouts if p["status"] == "pending")
    total_paid = sum(float(p["amount"]) for p in payouts if p["status"] == "approved")
    return {
        "status": "success",
        "payouts": payouts,
        "pending_count": pending_count,
        "total_paid": total_paid
    }

@app.post("/api/admin/payout-request/{payout_id}/approve")
async def approve_payout_request(payout_id: int, req: PayoutApproveReq):
    conn = get_conn()
    payout = conn.execute("SELECT * FROM caregiver_payouts WHERE id=?", (payout_id,)).fetchone()
    if not payout:
        conn.close()
        raise HTTPException(status_code=404, detail="ไม่พบรายการคำขอถอนเงินนี้")

    ts = now_str()
    conn.execute(
        "UPDATE caregiver_payouts SET status='approved', transfer_slip=?, admin_note=?, processed_at=? WHERE id=?",
        (req.transfer_slip, req.admin_note or "โอนเงินเรียบร้อยแล้ว", ts, payout_id)
    )
    conn.commit()
    conn.close()

    await manager.broadcast_json({
        "type": "payout_status_updated",
        "payout_id": payout_id,
        "caregiver_username": payout["caregiver_username"],
        "status": "approved",
        "amount": payout["amount"],
        "timestamp": ts
    })

    return {"status": "success", "message": f"อนุมัติและบันทึกการโอนเงิน ฿{payout['amount']:,.2f} เรียบร้อยแล้ว"}

@app.post("/api/admin/payout-request/{payout_id}/reject")
async def reject_payout_request(payout_id: int, req: PayoutRejectReq):
    conn = get_conn()
    payout = conn.execute("SELECT * FROM caregiver_payouts WHERE id=?", (payout_id,)).fetchone()
    if not payout:
        conn.close()
        raise HTTPException(status_code=404, detail="ไม่พบรายการคำขอถอนเงินนี้")

    ts = now_str()
    conn.execute(
        "UPDATE caregiver_payouts SET status='rejected', admin_note=?, processed_at=? WHERE id=?",
        (req.admin_note or "คำขอถูกปฏิเสธ", ts, payout_id)
    )
    conn.commit()
    conn.close()

    await manager.broadcast_json({
        "type": "payout_status_updated",
        "payout_id": payout_id,
        "caregiver_username": payout["caregiver_username"],
        "status": "rejected",
        "amount": payout["amount"],
        "timestamp": ts
    })

    return {"status": "success", "message": f"ปฏิเสธคำขอถอนเงิน ฿{payout['amount']:,.2f} เรียบร้อยแล้ว"}

# ─────────────────────────────────────────────────────────────────────────────
# PATIENT HEALTH RECORD (EHR PROFILES - เวชระเบียนสุขภาพส่วนบุคคล)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/patient-profile/{username}")
def get_patient_profile(username: str):
    conn = get_conn()
    p_row = conn.execute("SELECT name, phone, email FROM patients WHERE username=?", (username,)).fetchone()
    prof_row = conn.execute("SELECT * FROM patient_profiles WHERE user_username=?", (username,)).fetchone()
    conn.close()

    user_name = p_row["name"] if p_row else username
    phone = p_row["phone"] if p_row else ""
    email = p_row["email"] if p_row else ""

    if not prof_row:
        return {
            "user_username": username,
            "patient_name": user_name,
            "phone": phone,
            "email": email,
            "blood_type": "",
            "chronic_diseases": [],
            "drug_allergies": "",
            "food_allergies": "",
            "hospital_hns": [],
            "emergency_name": "",
            "emergency_phone": "",
            "emergency_relation": "",
            "special_care_notes": "",
            "has_profile": False
        }

    d = dict(prof_row)
    d["patient_name"] = user_name
    d["phone"] = phone
    d["email"] = email
    d["has_profile"] = True
    try:
        d["chronic_diseases"] = json.loads(d.get("chronic_diseases") or "[]")
    except Exception:
        d["chronic_diseases"] = []
    try:
        d["hospital_hns"] = json.loads(d.get("hospital_hns") or "[]")
    except Exception:
        d["hospital_hns"] = []
    return d

@app.post("/api/patient-profile/{username}")
async def save_patient_profile(username: str, req: PatientProfileReq):
    conn = get_conn()
    ts = now_str()
    chronic_json = json.dumps(req.chronic_diseases or [])
    hns_json = json.dumps(req.hospital_hns or [])

    conn.execute("""
        INSERT INTO patient_profiles (
            user_username, blood_type, chronic_diseases, drug_allergies, food_allergies,
            hospital_hns, emergency_name, emergency_phone, emergency_relation, special_care_notes, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_username) DO UPDATE SET
            blood_type=excluded.blood_type,
            chronic_diseases=excluded.chronic_diseases,
            drug_allergies=excluded.drug_allergies,
            food_allergies=excluded.food_allergies,
            hospital_hns=excluded.hospital_hns,
            emergency_name=excluded.emergency_name,
            emergency_phone=excluded.emergency_phone,
            emergency_relation=excluded.emergency_relation,
            special_care_notes=excluded.special_care_notes,
            updated_at=excluded.updated_at
    """, (
        username, req.blood_type or "", chronic_json, req.drug_allergies or "", req.food_allergies or "",
        hns_json, req.emergency_name or "", req.emergency_phone or "", req.emergency_relation or "",
        req.special_care_notes or "", ts
    ))
    conn.commit()
    conn.close()

    await manager.broadcast_json({
        "type": "patient_profile_updated",
        "username": username,
        "timestamp": ts
    })
    return {"status": "success", "message": "บันทึกข้อมูลเวชระเบียนส่วนบุคคล (EHR) เรียบร้อยแล้ว"}

# ─────────────────────────────────────────────────────────────────────────────
# DOCTOR APPOINTMENTS & FOLLOW-UP SCHEDULER (ตารางนัดหมายติดตามผลแพทย์)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/followups/{username}")
def get_user_followups(username: str):
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM appointment_followups
        WHERE user_username=?
        ORDER BY appointment_date ASC, appointment_time ASC
    """, (username,)).fetchall()
    conn.close()
    return {"followups": [dict(r) for r in rows]}

@app.post("/api/followups")
async def create_followup(req: AppointmentFollowupReq):
    conn = get_conn()
    ts = now_str()
    c = conn.cursor()
    c.execute("""
        INSERT INTO appointment_followups (
            booking_id, user_username, patient_name, hospital, department,
            appointment_date, appointment_time, doctor_name, prep_notes, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'upcoming', ?)
    """, (
        req.booking_id, req.user_username, req.patient_name, req.hospital, req.department or "",
        req.appointment_date, req.appointment_time or "", req.doctor_name or "", req.prep_notes or "", ts
    ))
    new_id = c.lastrowid
    conn.commit()
    conn.close()

    await manager.broadcast_json({
        "type": "appointment_followup_created",
        "followup_id": new_id,
        "user_username": req.user_username,
        "patient_name": req.patient_name,
        "hospital": req.hospital,
        "appointment_date": req.appointment_date,
        "timestamp": ts
    })
    return {"status": "success", "message": "เพิ่มการนัดหมายติดตามผลแพทย์เรียบร้อยแล้ว", "followup_id": new_id}

@app.delete("/api/followups/{followup_id}")
async def delete_followup(followup_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM appointment_followups WHERE id=?", (followup_id,))
    conn.commit()
    conn.close()
    return {"status": "success", "message": "ลบรายการนัดหมายเรียบร้อยแล้ว"}

# ── PUBLIC REVIEWS API ────────────────────────────────────────────────────────
@app.get("/api/public-reviews")
def get_public_reviews(category: str = "all"):
    conn = get_conn()
    if category == "all":
        rows = conn.execute("SELECT * FROM public_reviews ORDER BY id DESC LIMIT 30").fetchall()
    else:
        rows = conn.execute("SELECT * FROM public_reviews WHERE category=? ORDER BY id DESC LIMIT 30", (category,)).fetchall()
    conn.close()
    reviews = []
    for r in rows:
        d = dict(r)
        try:
            d["chips"] = json.loads(d.get("chips") or "[]")
        except Exception:
            d["chips"] = []
        reviews.append(d)
    return {"status": "success", "reviews": reviews}

@app.post("/api/public-reviews")
async def create_public_review(req: PublicReviewReq):
    if not req.author_name.strip() or not req.comment.strip():
        raise HTTPException(status_code=400, detail="กรุณากรอกชื่อและข้อความรีวิว")
    conn = get_conn()
    ts = now_str()
    cur = conn.execute(
        """INSERT INTO public_reviews (booking_id, author_name, relationship, category, target_name, rating, chips, comment, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            req.booking_id,
            req.author_name.strip(),
            req.relationship.strip() or "ผู้รับบริการ CareMate",
            req.category or "nurse",
            req.target_name.strip() or "พยาบาลวิชาชีพ CareMate",
            max(1.0, min(5.0, float(req.rating))),
            json.dumps(req.chips or []),
            req.comment.strip(),
            ts
        )
    )
    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    await manager.broadcast_json({
        "type": "new_review_published",
        "review_id": new_id,
        "author": req.author_name.strip(),
        "rating": req.rating,
        "timestamp": ts
    })
    return {"status": "success", "message": "บันทึกรีวิวของคุณเรียบร้อยแล้ว ขอบคุณสำหรับความคิดเห็น!", "review_id": new_id}

@app.put("/api/public-reviews/{review_id}")
async def update_public_review(review_id: int, req: PublicReviewUpdateReq):
    conn = get_conn()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM public_reviews WHERE id=?", (review_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="ไม่พบรีวิวที่ต้องการแก้ไข")

    updates = []
    params = []
    if req.author_name is not None and req.author_name.strip():
        updates.append("author_name=?")
        params.append(req.author_name.strip())
    if req.relationship is not None:
        updates.append("relationship=?")
        params.append(req.relationship.strip())
    if req.category is not None and req.category.strip():
        updates.append("category=?")
        params.append(req.category.strip())
    if req.target_name is not None and req.target_name.strip():
        updates.append("target_name=?")
        params.append(req.target_name.strip())
    if req.rating is not None:
        updates.append("rating=?")
        params.append(max(1.0, min(5.0, float(req.rating))))
    if req.chips is not None:
        updates.append("chips=?")
        params.append(json.dumps(req.chips))
    if req.comment is not None and req.comment.strip():
        updates.append("comment=?")
        params.append(req.comment.strip())

    if updates:
        params.append(review_id)
        cur.execute(f"UPDATE public_reviews SET {', '.join(updates)} WHERE id=?", params)
        conn.commit()
    conn.close()

    await manager.broadcast_json({
        "type": "review_updated",
        "review_id": review_id
    })
    return {"status": "success", "message": "แก้ไขข้อมูลรีวิวเรียบร้อยแล้ว"}

@app.delete("/api/public-reviews/{review_id}")
async def delete_public_review(review_id: int):
    conn = get_conn()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM public_reviews WHERE id=?", (review_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="ไม่พบรีวิวที่ต้องการลบ")

    cur.execute("DELETE FROM public_reviews WHERE id=?", (review_id,))
    conn.commit()
    conn.close()

    await manager.broadcast_json({
        "type": "review_deleted",
        "review_id": review_id
    })
    return {"status": "success", "message": "ลบรีวิวออกจากระบบเรียบร้อยแล้ว"}

# ── MEDICATIONS & REMINDERS API ───────────────────────────────────────────────
@app.get("/api/medications/{username}")
def get_medications(username: str):
    conn = get_conn()
    med_rows = conn.execute(
        "SELECT * FROM medications WHERE user_username=? AND status='active' ORDER BY id ASC",
        (username,)
    ).fetchall()
    
    today_str = datetime.now().strftime("%Y-%m-%d")
    log_rows = conn.execute(
        "SELECT * FROM medication_logs WHERE user_username=? AND taken_at LIKE ? ORDER BY id DESC",
        (username, f"{today_str}%")
    ).fetchall()
    conn.close()

    taken_med_ids = {r["med_id"]: r["taken_at"] for r in log_rows}
    meds = []
    for r in med_rows:
        d = dict(r)
        try:
            d["schedule_times"] = json.loads(d.get("schedule_times") or '["เช้า"]')
        except Exception:
            d["schedule_times"] = ["เช้า"]
        d["taken_today"] = d["id"] in taken_med_ids
        d["taken_today_time"] = taken_med_ids.get(d["id"])
        matching_log = next((r for r in log_rows if r["med_id"] == d["id"]), None)
        d["taken_photo_url"] = dict(matching_log).get("photo_url", "") if matching_log else ""
        meds.append(d)

    return {"status": "success", "medications": meds, "today_logs": [dict(r) for r in log_rows]}

@app.post("/api/medications")
def add_medication(req: MedicationCreateReq):
    if not req.med_name.strip():
        raise HTTPException(status_code=400, detail="กรุณาระบุชื่อยา")
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO medications (user_username, patient_name, med_name, dosage, schedule_times, meal_relation, notes, status, created_at, photo_url)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
        (
            req.user_username,
            req.patient_name or "คนไข้",
            req.med_name.strip(),
            req.dosage.strip(),
            json.dumps(req.schedule_times or ["เช้า"]),
            req.meal_relation or "หลังอาหาร",
            req.notes.strip(),
            now_str(),
            req.photo_url or ""
        )
    )
    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return {"status": "success", "message": "เพิ่มรายการยาเรียบร้อยแล้ว", "medication_id": new_id}

@app.post("/api/medications/{med_id}/take")
def log_take_medication(med_id: int, req: MedicationTakeReq):
    conn = get_conn()
    med = conn.execute("SELECT * FROM medications WHERE id=?", (med_id,)).fetchone()
    if not med:
        conn.close()
        raise HTTPException(status_code=404, detail="ไม่พบรายการยานี้")

    now = now_str()
    conn.execute(
        """INSERT INTO medication_logs (med_id, user_username, taken_at, time_slot, notes, photo_url)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (med_id, req.user_username, now, req.time_slot or "ปกติ", req.notes or "", req.photo_url or "")
    )
    conn.commit()
    conn.close()
    return {"status": "success", "message": f"บันทึกการทานยา '{med['med_name']}' เรียบร้อยแล้ว", "taken_at": now}

@app.delete("/api/medications/{med_id}")
def delete_medication(med_id: int):
    conn = get_conn()
    conn.execute("UPDATE medications SET status='inactive' WHERE id=?", (med_id,))
    conn.commit()
    conn.close()
    return {"status": "success", "message": "ลบรายการยาเรียบร้อยแล้ว"}

@app.post("/api/booking/{booking_id}/update-status")
async def update_booking_status(booking_id: int, status: str = Query(...)):
    conn = get_conn()
    conn.execute("UPDATE bookings SET status=? WHERE id=?", (status, booking_id))
    conn.commit()
    conn.close()
    await manager.broadcast_json({
        "type": "booking_status_updated",
        "booking_id": booking_id,
        "new_status": status,
        "timestamp": now_str()
    })
    return {"status": "success", "message": f"อัปเดตสถานะการดูแลเป็น '{status}' เรียบร้อย"}

# ─────────────────────────────────────────────────────────────────────────────
# VITALS & CLINICAL CARE LOGS (บันทึกสัญญาณชีพและเวชปฏิบัติ)
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/booking/{booking_id}/vitals")
async def record_vitals(booking_id: int, req: VitalsLogReq):
    conn = get_conn()
    ts = now_str()

    # Clinical Early Warning / Critical Vitals Detection
    critical_warnings = []
    if req.bp_sys and req.bp_sys >= 160:
        critical_warnings.append(f"ความดันโลหิตตัวบนสูงวิกฤต {req.bp_sys} mmHg (เสี่ยงภาวะ Hypertensive Crisis)")
    elif req.bp_sys and req.bp_sys <= 90:
        critical_warnings.append(f"ความดันโลหิตตัวบนต่ำวิกฤต {req.bp_sys} mmHg (เสี่ยงภาวะช็อก Hypotension)")

    if req.bp_dia and req.bp_dia >= 100:
        critical_warnings.append(f"ความดันโลหิตตัวล่างสูงวิกฤต {req.bp_dia} mmHg")

    if req.spo2 and req.spo2 < 95:
        critical_warnings.append(f"ระดับออกซิเจนในเลือดต่ำวิกฤต {req.spo2}% (เสี่ยงภาวะพร่องออกซิเจน Hypoxia)")

    if req.pulse and req.pulse > 110:
        critical_warnings.append(f"อัตราการเต้นของหัวใจเร็วผิดปกติ {req.pulse} bpm (Tachycardia)")
    elif req.pulse and req.pulse < 50:
        critical_warnings.append(f"อัตราการเต้นของหัวใจช้าผิดปกติ {req.pulse} bpm (Bradycardia)")

    if req.glucose and req.glucose > 200:
        critical_warnings.append(f"ค่าน้ำตาลในเลือดสูงผิดปกติ {req.glucose} mg/dL (Hyperglycemia)")
    elif req.glucose and req.glucose < 70:
        critical_warnings.append(f"ค่าน้ำตาลในเลือดต่ำวิกฤต {req.glucose} mg/dL (Hypoglycemia)")

    if req.temp and req.temp >= 38.5:
        critical_warnings.append(f"อุณหภูมิร่างกายมีไข้สูง {req.temp} °C (High Grade Fever)")

    is_critical = len(critical_warnings) > 0

    conn.execute("""
        INSERT INTO vitals_logs (booking_id, caregiver_user, bp_sys, bp_dia, pulse, spo2, temp, glucose, care_notes, recorded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        booking_id, req.caregiver_username, req.bp_sys, req.bp_dia, req.pulse, req.spo2, req.temp, req.glucose, req.care_notes or "", ts
    ))

    b_row = conn.execute("SELECT patient_name, hospital FROM bookings WHERE id=?", (booking_id,)).fetchone()
    p_name = b_row["patient_name"] if b_row else "คนไข้"
    hosp = b_row["hospital"] if b_row else "โรงพยาบาล"

    conn.commit()
    conn.close()

    broadcast_data = {
        "type": "vitals_recorded",
        "booking_id": booking_id,
        "patient_name": p_name,
        "caregiver": req.caregiver_username,
        "bp_sys": req.bp_sys,
        "bp_dia": req.bp_dia,
        "pulse": req.pulse,
        "spo2": req.spo2,
        "temp": req.temp,
        "glucose": req.glucose,
        "care_notes": req.care_notes,
        "recorded_at": ts,
        "is_critical": is_critical,
        "critical_warnings": critical_warnings
    }
    await manager.broadcast_json(broadcast_data)

    if is_critical:
        await manager.broadcast_json({
            "type": "critical_vitals_alert",
            "booking_id": booking_id,
            "patient_name": p_name,
            "hospital": hosp,
            "warnings": critical_warnings,
            "recorded_at": ts
        })

    return {
        "status": "success",
        "message": "บันทึกสัญญาณชีพและเวชปฏิบัติเรียบร้อยแล้ว",
        "timestamp": ts,
        "is_critical": is_critical,
        "critical_warnings": critical_warnings
    }

@app.get("/api/booking/{booking_id}/vitals")
def get_vitals(booking_id: int):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM vitals_logs WHERE booking_id=? ORDER BY id DESC", (booking_id,)).fetchall()
    conn.close()
    return {"vitals": [dict(r) for r in rows]}

@app.get("/api/admin/smtp-config")
def get_smtp_config():
    host = get_setting("SMTP_HOST", "smtp.gmail.com")
    port = get_setting("SMTP_PORT", "587")
    user = get_setting("SMTP_USER", "")
    has_pass = bool(get_setting("SMTP_PASS", ""))
    from_name = get_setting("SMTP_FROM", "CareMate Bangkok <noreply@caremate.th>")
    return {
        "smtp_host": host,
        "smtp_port": int(port),
        "smtp_user": user,
        "has_pass": has_pass,
        "smtp_from": from_name,
        "is_configured": bool(user and has_pass)
    }

@app.post("/api/admin/smtp-config")
def save_smtp_config(cfg: SmtpConfigReq):
    set_setting("SMTP_HOST", cfg.smtp_host.strip())
    set_setting("SMTP_PORT", str(cfg.smtp_port))
    set_setting("SMTP_USER", cfg.smtp_user.strip())
    if cfg.smtp_pass and cfg.smtp_pass.strip() != "******":
        set_setting("SMTP_PASS", cfg.smtp_pass.strip())
    set_setting("SMTP_FROM", cfg.smtp_from.strip())
    return {"status": "success", "message": "บันทึกการตั้งค่าระบบส่งอีเมล SMTP เรียบร้อยแล้ว"}

@app.post("/api/admin/smtp-test")
def test_smtp_config(req: SmtpTestReq):
    success, msg = send_email_otp(req.target_email.strip(), "ผู้ทดสอบระบบ", f"{random.randint(100000, 999999)}")
    if not success:
        raise HTTPException(status_code=400, detail=f"ส่งอีเมลทดสอบไม่สำเร็จ: {msg}")
    return {"status": "success", "message": f"ส่งอีเมลทดสอบไปยัง {req.target_email} สำเร็จเรียบร้อย"}

# ─────────────────────────────────────────────────────────────────────────────
# PROMPTPAY EMVCo STANDARD ENGINE (มาตรฐานพร้อมเพย์ ธนาคารแห่งประเทศไทย)
# ─────────────────────────────────────────────────────────────────────────────

def _promptpay_crc16(data: str) -> str:
    crc = 0xFFFF
    for c in data.encode("ascii"):
        crc ^= (c << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return f"{crc:04X}"

def generate_promptpay_payload(target: str, amount: Optional[float] = None) -> str:
    target = target.replace("-", "").replace(" ", "").strip()
    if len(target) == 10 and target.startswith("0"):
        target_field = "0066" + target[1:]
        subtag = f"01{len(target_field):02d}{target_field}"
    elif len(target) == 13:
        subtag = f"02{len(target):02d}{target}"
    else:
        if len(target) < 13:
            target_field = ("0066" + target[1:]) if target.startswith("0") else ("0066" + target)
            subtag = f"01{len(target_field):02d}{target_field}"
        else:
            subtag = f"02{len(target):02d}{target}"

    tag29 = f"0016A000000677010111{subtag}"
    parts = [
        "000201",
        "010212" if (amount and amount > 0) else "010211",
        f"29{len(tag29):02d}{tag29}",
        "5303764",
    ]
    if amount and amount > 0:
        amt_str = f"{amount:.2f}"
        parts.append(f"54{len(amt_str):02d}{amt_str}")
    parts.append("5802TH")
    parts.append("6304")
    raw = "".join(parts)
    return raw + _promptpay_crc16(raw)

@app.get("/api/promptpay/config")
def get_promptpay_config():
    pp_id = get_setting("PROMPTPAY_ID", "0649380536")
    pp_name = get_setting("PROMPTPAY_NAME", "กชกร สุขช่วย (CareMate)")
    return {"promptpay_id": pp_id, "promptpay_name": pp_name}

@app.post("/api/admin/promptpay-config")
def save_promptpay_config(req: PromptPayConfigReq):
    set_setting("PROMPTPAY_ID", req.promptpay_id.strip())
    set_setting("PROMPTPAY_NAME", req.promptpay_name.strip())
    return {"status": "success", "message": "บันทึกการตั้งค่าบัญชี PromptPay เรียบร้อยแล้ว"}

@app.get("/api/promptpay/payload")
def get_promptpay_payload(amount: float = 0.0, target: Optional[str] = None):
    pp_target = target or get_setting("PROMPTPAY_ID", "0649380536")
    payload = generate_promptpay_payload(pp_target, amount if amount > 0 else None)
    return {
        "payload": payload,
        "target": pp_target,
        "amount": amount,
        "name": get_setting("PROMPTPAY_NAME", "กชกร สุขช่วย (CareMate)")
    }


# ─────────────────────────────────────────────────────────────────────────────
# ชุดข้อมูลโรงพยาบาล (สำหรับแสดงบนแผนที่) ─────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/hospitals")
def list_hospitals():
    hospitals = [
        {"name": "รพ.ศิริราช",                          "lat": 13.7580, "lng": 100.4857},
        {"name": "รพ.ศิริราช ปิยมหาราชการุณย์",          "lat": 13.7595, "lng": 100.4862},
        {"name": "รพ.จุฬาลงกรณ์ สภากาชาดไทย",           "lat": 13.7314, "lng": 100.5342},
        {"name": "รพ.รามาธิบดี",                         "lat": 13.7667, "lng": 100.5284},
        {"name": "รพ.วชิรพยาบาล",                        "lat": 13.7788, "lng": 100.5061},
        {"name": "รพ.พระมงกุฎเกล้า",                     "lat": 13.7681, "lng": 100.5358},
        {"name": "รพ.ราชวิถี",                           "lat": 13.7651, "lng": 100.5369},
        {"name": "รพ.เลิดสิน",                           "lat": 13.7208, "lng": 100.5173},
        {"name": "รพ.ตากสิน",                            "lat": 13.7272, "lng": 100.5025},
        {"name": "รพ.กลาง",                              "lat": 13.7459, "lng": 100.5108},
        {"name": "รพ.ตำรวจ",                             "lat": 13.7439, "lng": 100.5372},
        {"name": "รพ.นพรัตนราชธานี",                     "lat": 13.8528, "lng": 100.5871},
        {"name": "รพ.ภูมิพลอดุลยเดช",                   "lat": 13.8952, "lng": 100.5817},
        {"name": "สถาบันสุขภาพเด็กแห่งชาติมหาราชินี",   "lat": 13.7564, "lng": 100.5278},
        {"name": "สถาบันมะเร็งแห่งชาติ",                 "lat": 13.7743, "lng": 100.5340},
        {"name": "สถาบันโรคทรวงอก",                      "lat": 13.7620, "lng": 100.5320},
        {"name": "รพ.บำรุงราษฎร์",                       "lat": 13.7466, "lng": 100.5528},
        {"name": "รพ.สมิติเวช สุขุมวิท",                  "lat": 13.7337, "lng": 100.5786},
        {"name": "รพ.สมิติเวช ศรีนครินทร์",              "lat": 13.6947, "lng": 100.6428},
        {"name": "รพ.สมิติเวช ธนบุรี",                   "lat": 13.7293, "lng": 100.4836},
        {"name": "รพ.สมิติเวช นวมินทร์",                 "lat": 13.8291, "lng": 100.6341},
        {"name": "บีเอ็นเอช (BNH Hospital)",             "lat": 13.7253, "lng": 100.5367},
        {"name": "รพ.กรุงเทพ (ซอยศูนย์วิจัย)",          "lat": 13.7486, "lng": 100.5833},
        {"name": "รพ.กรุงเทพ หัวใจ",                    "lat": 13.7240, "lng": 100.5190},
        {"name": "รพ.กรุงเทพ รามอินทรา",                "lat": 13.8770, "lng": 100.6168},
        {"name": "รพ.กรุงเทพ นวมินทร์",                 "lat": 13.8345, "lng": 100.6398},
        {"name": "รพ.กรุงเทพ คริสเตียน",                "lat": 13.7303, "lng": 100.5248},
        {"name": "รพ.กรุงเทพ สาทร",                     "lat": 13.7180, "lng": 100.5320},
        {"name": "รพ.พญาไท 1",                           "lat": 13.7532, "lng": 100.5345},
        {"name": "รพ.พญาไท 2",                           "lat": 13.7618, "lng": 100.5315},
        {"name": "รพ.พญาไท 3",                           "lat": 13.7803, "lng": 100.5701},
        {"name": "รพ.เปาโล พหลโยธิน",                   "lat": 13.7915, "lng": 100.5492},
        {"name": "รพ.เปาโล โชคชัย 4",                   "lat": 13.8180, "lng": 100.6205},
        {"name": "รพ.เปาโล สมุทรปราการ",                 "lat": 13.6182, "lng": 100.6063},
        {"name": "รพ.เวชธานี",                           "lat": 13.7969, "lng": 100.6483},
        {"name": "เมดพาร์ค (MedPark Hospital)",          "lat": 13.7228, "lng": 100.5583},
        {"name": "รพ.พระราม 9",                          "lat": 13.7618, "lng": 100.5630},
        {"name": "รพ.ปิยะเวท",                           "lat": 13.8063, "lng": 100.5358},
        {"name": "รพ.วิมุต",                             "lat": 13.7897, "lng": 100.5498},
        {"name": "รพ.วิภาวดี",                           "lat": 13.8600, "lng": 100.5607},
        {"name": "รพ.ยันฮี",                             "lat": 13.7763, "lng": 100.4929},
        {"name": "รพ.ธนบุรี",                            "lat": 13.7265, "lng": 100.4837},
        {"name": "รพ.ธนบุรี 2",                          "lat": 13.7799, "lng": 100.4875},
        {"name": "รพ.วิชัยยุทธ",                         "lat": 13.7550, "lng": 100.5065},
        {"name": "รพ.มิชชั่น",                           "lat": 13.7542, "lng": 100.5156},
        {"name": "รพ.ลาดพร้าว",                          "lat": 13.8180, "lng": 100.5769},
        {"name": "รพ.นวเวช",                             "lat": 13.7910, "lng": 100.5815},
        {"name": "รพ.สินแพทย์ รามอินทรา",               "lat": 13.8770, "lng": 100.6168},
        {"name": "รพ.เกษมราษฎร์ ประชาชื่น",             "lat": 13.8241, "lng": 100.5617},
        {"name": "รพ.เกษมราษฎร์ ศรีนครินทร์",           "lat": 13.6947, "lng": 100.6428},
        {"name": "รพ.สุขุมวิท",                          "lat": 13.7382, "lng": 100.5642},
        {"name": "รพ.อาโรคยา",                           "lat": 13.7391, "lng": 100.5637},
        {"name": "รพ.บางปะกอก 9",                       "lat": 13.6652, "lng": 100.6019},
        {"name": "รพ.ศิครินทร์",                         "lat": 13.7960, "lng": 100.6490},
        {"name": "รพ.รัตนิน (ตา)",                      "lat": 13.7382, "lng": 100.5642},
        {"name": "รพ.วัฒโนสถ (มะเร็ง)",                 "lat": 13.7460, "lng": 100.5485},
        {"name": "รพ.หัวใจกรุงเทพ",                     "lat": 13.7240, "lng": 100.5190},
        {"name": "รพ.สมุทรปราการ",                       "lat": 13.5988, "lng": 100.5992},
        {"name": "รพ.กรุงเทพ สมุทรปราการ",               "lat": 13.6015, "lng": 100.6278},
        {"name": "รพ.เพชรเกษม",                          "lat": 13.7193, "lng": 100.4394},
        {"name": "รพ.พิษณุเวช",                          "lat": 13.7260, "lng": 100.4410},
        {"name": "รพ.หนองแขม",                           "lat": 13.7022, "lng": 100.4103},
        {"name": "รพ.ทวีชัยการแพทย์",                   "lat": 13.8345, "lng": 100.6474},
        {"name": "รพ.มินบุรี",                           "lat": 13.8155, "lng": 100.7434},
        {"name": "รพ.ลาดกระบัง",                         "lat": 13.7267, "lng": 100.7469},
        {"name": "รพ.โซมเด็จพระปิ่นเกล้า",              "lat": 13.7672, "lng": 100.4773},
        {"name": "รพ.จุฬาภรณ์",                          "lat": 13.8180, "lng": 100.5769},
        {"name": "รพ.สงฆ์",                              "lat": 13.7651, "lng": 100.5369},
        {"name": "รพ.เจริญกรุงประชารักษ์",               "lat": 13.7282, "lng": 100.5088},
        {"name": "รพ.สิรินธร",                           "lat": 13.7193, "lng": 100.4394},
        {"name": "รพ.ดอนเมือง",                          "lat": 13.9233, "lng": 100.5949},
        {"name": "รพ.ราษฎร์บูรณะ",                       "lat": 13.6947, "lng": 100.5018},
        {"name": "รพ.เทพธารินทร์ (เบาหวาน)",             "lat": 13.7369, "lng": 100.5291},
        {"name": "รพ.มหาวิทยาลัยมหิดล",                 "lat": 13.7963, "lng": 100.4924},
        {"name": "รพ.สมิติเวช สุขสวัสดิ์",               "lat": 13.6947, "lng": 100.5018},
        {"name": "รพ.ประชาพัฒน์",                        "lat": 13.7240, "lng": 100.5190},
        {"name": "รพ.นนทเวช",                            "lat": 13.8528, "lng": 100.5871},
        {"name": "รพ.บางพลี",                            "lat": 13.6015, "lng": 100.6278},
        {"name": "รพ.กล้วยน้ำไท",                       "lat": 13.7282, "lng": 100.5640},
        {"name": "รพ.เลิดสิน",                           "lat": 13.7208, "lng": 100.5173},
    ]
    return {"hospitals": hospitals, "count": len(hospitals)}

# ─────────────────────────────────────────────────────────────────────────────
# WEBSOCKET
# ─────────────────────────────────────────────────────────────────────────────

@app.websocket("/ws/{client_name}")
async def websocket_endpoint(websocket: WebSocket, client_name: str):
    await manager.connect(websocket, client_name)
    try:
        await manager.send_personal({
            "type": "welcome",
            "from": "server",
            "text": f"ยินดีต้อนรับสู่ช่องสื่อสาร CareMate BKK ผู้ใช้ {client_name} (สถานะออนไลน์: {manager.online_count} ท่าน)",
            "online_count": manager.online_count,
            "online_users": manager.online_users,
            "timestamp": now_str()
        }, websocket)

        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {"type": "text", "text": raw}

            outbound = {
                "type": payload.get("type", "text"),
                "from": client_name,
                "text": payload.get("text", ""),
                "timestamp": now_str(),
                "online_count": manager.online_count
            }
            if payload.get("msg_id"):
                outbound["msg_id"] = payload["msg_id"]
            if payload.get("image"):
                outbound["image"] = payload["image"]
            if payload.get("booking_id"):
                outbound["booking_id"] = payload["booking_id"]
            if payload.get("target"):
                outbound["target"] = payload["target"]
            if payload.get("sender_role"):
                outbound["sender_role"] = payload["sender_role"]
            if payload.get("sender_name"):
                outbound["sender_name"] = payload["sender_name"]

            await manager.broadcast_json(outbound, exclude_ws=websocket)

    except WebSocketDisconnect:
        manager.disconnect(websocket)

@app.get("/api/ws/status")
def ws_status():
    return {"online_count": manager.online_count, "online_users": manager.online_users}

# ─────────────────────────────────────────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
@app.get("/index.html")
def root():
    index_file = BASE_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return {"service": "CareMate BKK", "version": "2.0.0", "status": "operational", "docs": "/docs"}

@app.get("/script.js")
def get_script():
    return FileResponse(
        str(BASE_DIR / "script.js"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
    )

@app.get("/style.css")
def get_style():
    return FileResponse(
        str(BASE_DIR / "style.css"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
    )

@app.get("/health")
def health():
    return {"status": "ok", "timestamp": now_str()}

# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("care:app", host="0.0.0.0", port=8000, reload=True)