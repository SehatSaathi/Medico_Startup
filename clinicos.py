

import os, time, random, textwrap, tempfile, requests
from datetime import datetime
from pathlib import Path
import mysql.connector
from mysql.connector import Error
import google.generativeai as genai
from dotenv import load_dotenv
from gtts import gTTS
from twilio.rest import Client as TwilioClient
from twilio.base.exceptions import TwilioRestException



load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# ── Twilio Config (loaded from .env) ──────────────────────────────────────────
TWILIO_SID  = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH = os.getenv("TWILIO_AUTH_TOKEN")
# TWILIO_WHATSAPP_NUMBER in .env is OPTIONAL — if missing, we auto-fetch from Twilio
TWILIO_WHATSAPP_NUM = os.getenv("TWILIO_WHATSAPP_NUMBER", "")


def get_twilio_whatsapp_sender() -> str | None:
    """
    Auto-fetch the correct WhatsApp sandbox sender number from Twilio API.

    Why: Hardcoding +14155238886 often causes error 63007 because:
      - Your account may have a different sandbox number
      - The number may not be activated on your account
      - The whatsapp: prefix may be missing

    This function queries YOUR actual Twilio account for the real sender number.
    Falls back to .env value if API call fails.
    """
    # First try .env override
    env_num = TWILIO_WHATSAPP_NUM.strip()
    if env_num:
        return _format_from(env_num)

    if not TWILIO_SID or not TWILIO_AUTH:
        return None

    try:
        client = TwilioClient(TWILIO_SID, TWILIO_AUTH)

        # ── Try fetching sandbox number from IncomingPhoneNumbers ────────────
        # Look for a number with WhatsApp capability
        numbers = client.incoming_phone_numbers.list(limit=20)
        for num in numbers:
            capabilities = num.capabilities or {}
            if capabilities.get("mms") or "whatsapp" in (num.friendly_name or "").lower():
                return _format_from(num.phone_number)

        # ── Fallback: try the well-known Twilio sandbox number ───────────────
        # Twilio sandbox is always this number — verify it exists on the account
        sandbox_num = "whatsapp:+14155238886"
        print(f"  [Twilio] No WhatsApp number found via API. Trying sandbox default: {sandbox_num}")
        return sandbox_num

    except Exception as e:
        print(f"  [Twilio] Could not auto-fetch sender number: {e}")
        return None

# ── In-memory stores (for non-patient data) ───────────────────────────────────
_reminders: list[dict]          = []
_consultation_notes: list[dict] = []


# ── MySQL Connection ──────────────────────────────────────────────────────────

def get_connection():
    """
    Create and return a MySQL connection.
    Reads credentials from .env file.
    """
    try:
        conn = mysql.connector.connect(
            host     = os.getenv("MYSQL_HOST", "localhost"),
            port     = int(os.getenv("MYSQL_PORT", 3306)),
            user     = os.getenv("MYSQL_USER", "root"),
            password = os.getenv("MYSQL_PASSWORD", ""),
            database = os.getenv("MYSQL_DATABASE", "clinicos_db"),
        )
        return conn
    except Error as e:
        print(f"\n    MySQL Connection Failed: {e}")
        print("      Please check your .env file and make sure MySQL is running.")
        return None


def init_database():
    """
    Create the 'patients' table if it doesn't already exist.
    Called once when the program starts.
    """
    conn = get_connection()
    if not conn:
        return False

    try:
        cursor = conn.cursor()
        # ── patients table ────────────────────────────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS patients (
                patient_id    VARCHAR(10)  PRIMARY KEY,
                name          VARCHAR(100) NOT NULL,
                age           VARCHAR(10)  NOT NULL,
                gender        VARCHAR(20)  NOT NULL,
                phone         VARCHAR(20)  NOT NULL UNIQUE,
                disease       VARCHAR(200),
                symptoms      TEXT,
                registered_at DATETIME     DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── appointments table ────────────────────────────────────────────────
        # Schema:
        #   appt_id    → PRIMARY KEY  (unique per appointment e.g. A1234)
        #   patient_id → FOREIGN KEY  (maps to patients.patient_id)
        #                              one patient can have many appointments
        #   doctor     → doctor name
        #   appt_date  → appointment date
        #   appt_time  → appointment time
        #   status     → scheduled / completed / cancelled
        #   booked_at  → auto timestamp
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS appointments (
                appt_id    VARCHAR(10)  PRIMARY KEY,
                patient_id VARCHAR(10)  NOT NULL,
                doctor     VARCHAR(100) NOT NULL,
                appt_date  DATE         NOT NULL,
                appt_time  TIME         NOT NULL,
                status     VARCHAR(20)  DEFAULT 'scheduled',
                booked_at  DATETIME     DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT fk_appt_patient
                    FOREIGN KEY (patient_id)
                    REFERENCES patients(patient_id)
                    ON DELETE CASCADE
            )
        """)
        conn.commit()
        cursor.close()
        conn.close()
        print("    Tables ready: patients, appointments")
        return True
    except Error as e:
        print(f"\n    Failed to create tables: {e}")
        return False


# ── Helpers ───────────────────────────────────────────────────────────────────

def clear():
    os.system("cls" if os.name == "nt" else "clear")

def banner():
    print("=" * 55)
    print("           ClinicOS — Clinic Management System")
    print("=" * 55)

def divider():
    print("-" * 55)

def ask(prompt: str, required: bool = True) -> str:
    """Ask a question and keep asking until answered (if required)."""
    while True:
        value = input(f"  ➤  {prompt}: ").strip()
        if value:
            return value
        if not required:
            return ""
        print("       This field is required. Please enter a value.\n")

def ask_choice(prompt: str, choices: list[str]) -> str:
    """Show numbered choices and return the chosen value."""
    print(f"\n  {prompt}")
    for i, c in enumerate(choices, 1):
        print(f"    [{i}] {c}")
    while True:
        sel = input("  ➤  Enter number: ").strip()
        if sel.isdigit() and 1 <= int(sel) <= len(choices):
            return choices[int(sel) - 1]
        print("       Invalid choice. Try again.")

def success(msg: str):
    print(f"\n    {msg}")

def error(msg: str):
    print(f"\n    {msg}")

def info(label: str, value: str):
    print(f"    {label}: {value}")

def pause():
    input("\n  Press Enter to return to the main menu...")


# ── Gemini AI call ────────────────────────────────────────────────────────────

def _call_gemini(prompt: str) -> str:
    model = genai.GenerativeModel(MODEL)
    for attempt in range(5):
        try:
            return model.generate_content(prompt).text.strip()
        except Exception as e:
            if "429" in str(e):
                time.sleep((2 ** attempt) + random.uniform(0, 0.5))
            else:
                raise
    raise RuntimeError("Rate limit exceeded.")


# ── 1. REGISTER NEW PATIENT (saves to MySQL) ──────────────────────────────────

def register_patient():
    clear()
    banner()
    print("\n    REGISTER NEW PATIENT\n")
    divider()
    print("  Fill in the patient details below. (* = required)\n")

    print("  STEP 1 of 6 — Patient Name")
    name = ask("Full Name *")

    print("\n  STEP 2 of 6 — Age")
    age = ask("Age *")

    print("\n  STEP 3 of 6 — Gender")
    gender = ask_choice("Select Gender *", ["Male", "Female", "Other"])

    print("\n  STEP 4 of 6 — Phone Number")
    phone = ask("Phone Number * (10 digits)")

    print("\n  STEP 5 of 6 — Known Disease / Condition")
    disease = ask("Disease / Condition (Press Enter to skip)", required=False)

    print("\n  STEP 6 of 6 — Current Symptoms")
    symptoms = ask("Symptoms (Press Enter to skip)", required=False)

    # Confirmation Screen
    divider()
    print("\n  🔍  Review Before Saving:\n")
    info("Name",     name)
    info("Age",      age)
    info("Gender",   gender)
    info("Phone",    phone)
    info("Disease",  disease  or "Not specified")
    info("Symptoms", symptoms or "Not specified")
    print()

    confirm = ask_choice("  Do you want to save this patient?", ["Yes, Save to Database", "No, Cancel"])
    if "No" in confirm:
        print("\n  ℹ️  Registration cancelled. Nothing was saved.")
        pause()
        return

    pid = f"P{random.randint(10000, 99999)}"

    conn = get_connection()
    if not conn:
        error("Could not connect to database. Patient not saved.")
        pause()
        return

    try:
        cursor = conn.cursor()

        # Check for duplicate phone
        cursor.execute("SELECT patient_id FROM patients WHERE phone = %s", (phone,))
        existing = cursor.fetchone()
        if existing:
            error(f"A patient with phone {phone} already exists (ID: {existing[0]}).")
            cursor.close()
            conn.close()
            pause()
            return

        # Ensure unique patient_id
        while True:
            cursor.execute("SELECT patient_id FROM patients WHERE patient_id = %s", (pid,))
            if not cursor.fetchone():
                break
            pid = f"P{random.randint(10000, 99999)}"

        sql = """
            INSERT INTO patients (patient_id, name, age, gender, phone, disease, symptoms)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """
        cursor.execute(sql, (pid, name, age, gender, phone, disease, symptoms))
        conn.commit()
        cursor.close()
        conn.close()

        divider()
        success("Patient registered and saved to MySQL database!")
        print()
        info("Patient ID",   pid)
        info("Name",         name)
        info("Phone",        phone)
        info("Saved At",     datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        print()
        print("    NOTE: Save this Patient ID — you'll need it later!")
        divider()

    except Error as e:
        error(f"Database error: {e}")

    pause()


# ── 2. FIND PATIENT (reads from MySQL) ───────────────────────────────────────

def get_patient():
    clear()
    banner()
    print("\n    FIND PATIENT\n")
    divider()

    method = ask_choice("Search patient by", ["Patient ID (e.g. P12345)", "Phone Number"])
    identifier = ask("Enter Patient ID" if "ID" in method else "Enter Phone Number")

    conn = get_connection()
    if not conn:
        error("Could not connect to database.")
        pause()
        return

    try:
        cursor = conn.cursor(dictionary=True)
        if "ID" in method:
            cursor.execute("SELECT * FROM patients WHERE patient_id = %s", (identifier,))
        else:
            cursor.execute("SELECT * FROM patients WHERE phone = %s", (identifier,))

        row = cursor.fetchone()
        cursor.close()
        conn.close()

        divider()
        if not row:
            error(f"No patient found for '{identifier}'.")
        else:
            success("Patient Found in Database!")
            print()
            info("Patient ID",   row["patient_id"])
            info("Name",         row["name"])
            info("Age",          row["age"])
            info("Gender",       row["gender"])
            info("Phone",        row["phone"])
            info("Disease",      row.get("disease")  or "Not specified")
            info("Symptoms",     row.get("symptoms") or "Not specified")
            info("Registered At",str(row.get("registered_at", "—")))

    except Error as e:
        error(f"Database error: {e}")

    divider()
    pause()


# ── 3. GENERATE AI SYMPTOM SUMMARY ───────────────────────────────────────────

def generate_symptom_summary():
    clear()
    banner()
    print("\n    GENERATE AI SYMPTOM SUMMARY\n")
    divider()

    patient_id = ask("Enter Patient ID (e.g. P12345)")

    conn = get_connection()
    if not conn:
        error("Could not connect to database.")
        pause()
        return

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM patients WHERE patient_id = %s", (patient_id,))
        rec = cursor.fetchone()
        cursor.close()
        conn.close()
    except Error as e:
        error(f"Database error: {e}")
        pause()
        return

    if not rec:
        error("Patient not found. Please register first.")
        pause()
        return

    info("Patient", f"{rec['name']} | Age {rec['age']} | {rec['gender']}")
    divider()

    extra = ask("Any new symptoms today? (Press Enter to skip)", required=False)
    print("\n  ⏳  Generating AI summary, please wait...\n")
    divider()

    prompt = textwrap.dedent(f"""
        You are a clinical AI assistant generating a pre-consultation summary for a doctor.
        Patient: {rec['name']}, Age: {rec['age']}, Gender: {rec['gender']}
        Known disease: {rec.get('disease') or 'Unknown'}
        Recorded symptoms: {rec.get('symptoms') or 'None'}
        New symptoms today: {extra or 'None'}

        Produce a structured pre-consultation brief covering:
        - Summary of condition
        - Key symptoms to focus on
        - Suggested questions for the doctor to ask
        - Any red flags to watch for
        Keep it concise and clinical.
    """)

    try:
        summary = _call_gemini(prompt)
        success("Summary Generated!")
        divider()
        print()
        print(summary)
        print()
    except Exception as e:
        error(f"AI generation failed: {e}")

    divider()
    pause()


# ── 4. BOOK APPOINTMENT ───────────────────────────────────────────────────────

def book_appointment():
    clear()
    banner()
    print("\n    BOOK APPOINTMENT\n")
    divider()

    patient_id = ask("Enter Patient ID (e.g. P12345)")

    conn = get_connection()
    if not conn:
        error("Could not connect to database.")
        pause()
        return

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT name, phone FROM patients WHERE patient_id = %s", (patient_id,))
        rec = cursor.fetchone()
        cursor.close()
        conn.close()
    except Error as e:
        error(f"Database error: {e}")
        pause()
        return

    if not rec:
        error("Patient not found. Please register first.")
        pause()
        return

    info("Patient", f"{rec['name']} | {rec['phone']}")
    divider()

    doctor = ask("Doctor's Name (e.g. Dr. Mehta)")

    print("\n    Enter Appointment Date")
    print("      Format: YYYY-MM-DD  →  Example: 2026-03-25")
    date = ask("Date")

    print("\n    Enter Appointment Time")
    print("      Format: HH:MM  →  Example: 10:30 or 14:00")
    time_slot = ask("Time")

    divider()
    print("\n    Confirm Appointment:\n")
    info("Patient", rec["name"])
    info("Doctor",  doctor)
    info("Date",    date)
    info("Time",    time_slot)

    confirm = ask_choice("\n  Confirm Booking?", ["Yes, Book Appointment", "No, Cancel"])
    if "No" in confirm:
        print("\n  ℹ Booking cancelled.")
        pause()
        return

    # ── Validate date and time format before saving ───────────────────────────
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        error("Invalid date format. Please use YYYY-MM-DD  (e.g. 2026-03-25)")
        pause()
        return

    try:
        datetime.strptime(time_slot, "%H:%M")
    except ValueError:
        error("Invalid time format. Please use HH:MM  (e.g. 10:30 or 14:00)")
        pause()
        return

    # ── Generate unique appointment ID ────────────────────────────────────────
    conn2 = get_connection()
    if not conn2:
        error("Could not connect to database. Appointment not saved.")
        pause()
        return

    try:
        cursor2 = conn2.cursor()

        # Ensure appt_id is unique
        appt_id = f"A{random.randint(1000, 9999)}"
        while True:
            cursor2.execute("SELECT appt_id FROM appointments WHERE appt_id = %s", (appt_id,))
            if not cursor2.fetchone():
                break
            appt_id = f"A{random.randint(1000, 9999)}"

        # ── INSERT into appointments table ────────────────────────────────────
        cursor2.execute("""
            INSERT INTO appointments (appt_id, patient_id, doctor, appt_date, appt_time)
            VALUES (%s, %s, %s, %s, %s)
        """, (appt_id, patient_id, doctor, date, time_slot))
        conn2.commit()
        cursor2.close()
        conn2.close()

        divider()
        success("Appointment saved to database!")
        print()
        info("Appointment ID", appt_id)
        info("Patient ID",     patient_id)
        info("Patient Name",   rec["name"])
        info("Doctor",         doctor)
        info("Date",           date)
        info("Time",           time_slot)
        info("Status",         "Scheduled")
        print()
        print("  📌  This appointment is linked to Patient ID as a Foreign Key.")
        divider()

    except Error as e:
        error(f"Database error: {e}")

    pause()


# ── WhatsApp + Voice Note Helpers ────────────────────────────────────────────

def _format_phone(phone: str) -> str:
    """
    Convert any phone number to WhatsApp-ready E.164 format.
    Handles all common input styles:
        9876543210     → whatsapp:+919876543210  (10-digit India)
        919876543210   → whatsapp:+919876543210  (12-digit with 91)
        +919876543210  → whatsapp:+919876543210  (already E.164)
        whatsapp:+91…  → whatsapp:+919876543210  (already formatted)
    """
    phone = phone.strip().replace(" ", "").replace("-", "")
    # Already fully formatted
    if phone.startswith("whatsapp:"):
        return phone
    # Strip leading + if present to normalise
    digits = phone.lstrip("+")
    # 10-digit Indian number — prepend 91
    if len(digits) == 10:
        digits = f"91{digits}"
    # Now digits should be full number without +
    return f"whatsapp:+{digits}"


def _format_from(number: str) -> str:
    """
    Ensure the Twilio FROM number has the whatsapp: prefix.
    Accepts:
        +14155238886        → whatsapp:+14155238886
        whatsapp:+14155238886 → whatsapp:+14155238886  (unchanged)
        14155238886         → whatsapp:+14155238886
    """
    number = number.strip()
    if number.startswith("whatsapp:"):
        return number
    if not number.startswith("+"):
        number = f"+{number}"
    return f"whatsapp:{number}"


def generate_voice_note(message: str, patient_name: str) -> str | None:
    """
    Generate a voice note MP3 from the reminder message using gTTS.
    Returns the local file path, or None on failure.

    The voice note text is slightly friendlier than the raw message:
    e.g. "Hello Rahul, this is a reminder from ClinicOS. Take insulin 500mg after dinner."
    """
    try:
        spoken_text = (
            f"Hello {patient_name}, this is a reminder from your clinic. "
            f"{message}. "
            f"Please follow your doctor's instructions. Thank you."
        )
        tts = gTTS(text=spoken_text, lang="en", slow=False)

        # Save to a named temp file so we can upload it
        tmp = tempfile.NamedTemporaryFile(
            delete=False, suffix=".mp3", prefix="clinicos_reminder_"
        )
        tts.save(tmp.name)
        tmp.close()
        return tmp.name

    except Exception as e:
        print(f"\n  [gTTS] Voice note generation failed: {e}")
        return None


def upload_audio_for_twilio(file_path: str) -> str | None:
    """
    Upload the MP3 to tmpfiles.org (free, no-auth public file host)
    and return a publicly accessible download URL.

    Twilio requires a public URL to send media over WhatsApp — it cannot
    read files from your local machine.

    tmpfiles.org auto-deletes files after 1 day, which is perfect for reminders.
    """
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(
                "https://tmpfiles.org/api/v1/upload",
                files={"file": ("reminder.mp3", f, "audio/mpeg")},
                timeout=30,
            )
        if resp.status_code == 200:
            data = resp.json()
            # tmpfiles returns: {"status":"success","data":{"url":"https://tmpfiles.org/XXXXXX/reminder.mp3"}}
            raw_url = data["data"]["url"]
            # Convert to direct download URL (replace /dl/ prefix)
            direct_url = raw_url.replace("tmpfiles.org/", "tmpfiles.org/dl/")
            return direct_url
        else:
            print(f"\n  [Upload] Failed with status {resp.status_code}")
            return None
    except Exception as e:
        print(f"\n  [Upload] File upload failed: {e}")
        return None


def send_whatsapp_reminder(phone: str, patient_name: str, message: str, audio_url: str | None) -> dict:
    """
    Send WhatsApp text message + voice note to the patient via Twilio.

    Steps:
      1. Send the text reminder message
      2. If audio_url is available, send the voice note as a media message

    Returns a dict with status and message SIDs.
    """
    # ── Validate credentials ─────────────────────────────────────────────────
    if not TWILIO_SID or not TWILIO_AUTH:
        return {"status": "error", "reason": "Twilio credentials missing in .env"}

    # ── Auto-fetch correct FROM number from Twilio account ───────────────────
    from_number = get_twilio_whatsapp_sender()
    if not from_number:
        return {"status": "error", "reason": (
            "Could not determine WhatsApp sender number.\n"
            "  Fix: Add TWILIO_WHATSAPP_NUMBER=whatsapp:+<your_number> to .env"
        )}

    # ── Normalise TO number ───────────────────────────────────────────────────
    to_number = _format_phone(phone)

    # ── Print debug so you can verify exact numbers ───────────────────────────
    print(f"  [Twilio] FROM : {from_number}")
    print(f"  [Twilio] TO   : {to_number}")

    # ── Sanity check: both must start with whatsapp: ──────────────────────────
    if not from_number.startswith("whatsapp:"):
        return {"status": "error", "reason": f"FROM not WhatsApp format: {from_number}"}
    if not to_number.startswith("whatsapp:"):
        return {"status": "error", "reason": f"TO not WhatsApp format: {to_number}"}

    client = TwilioClient(TWILIO_SID, TWILIO_AUTH)
    result = {"status": "ok", "text_sid": None, "audio_sid": None,
              "from": from_number, "to": to_number}

    # ── Step 1: Send text message ─────────────────────────────────────────────
    try:
        text_msg = client.messages.create(
            from_  = from_number,
            to     = to_number,
            body   = (
                f"*ClinicOS Reminder*\n\n"
                f"Hello {patient_name},\n"
                f"{message}\n\n"
                f"_Please follow your doctor's instructions._"
            ),
        )
        result["text_sid"] = text_msg.sid
        print(f"  [Twilio] Text message sent  → SID: {text_msg.sid}")

    except TwilioRestException as e:
        result["status"] = "partial_error"
        result["text_error"] = str(e)
        print(f"  [Twilio] Text message FAILED: {e}")

    # ── Step 2: Send voice note as audio media message ────────────────────────
    if audio_url:
        try:
            audio_msg = client.messages.create(
                from_      = from_number,
                to         = to_number,
                body       = "Voice reminder from ClinicOS",
                media_url  = [audio_url],
            )
            result["audio_sid"] = audio_msg.sid
            print(f"  [Twilio] Voice note sent    → SID: {audio_msg.sid}")

        except TwilioRestException as e:
            result["audio_error"] = str(e)
            if result["status"] == "ok":
                result["status"] = "partial_error"
            print(f"  [Twilio] Voice note FAILED: {e}")

    return result


# ── Twilio Diagnostics ───────────────────────────────────────────────────────

def run_twilio_diagnostics():
    """
    Checks your Twilio account setup and prints a step-by-step fix guide.
    Called automatically when sending fails with error 63007.
    """
    divider()
    print ("TWILIO DIAGNOSTICS")

    # Check 1: Credentials present
    sid_ok   = bool(TWILIO_SID)
    auth_ok  = bool(TWILIO_AUTH)
    print(f"  [1] TWILIO_ACCOUNT_SID  : {'Found' if sid_ok else 'MISSING in .env'}")
    print(f"  [2] TWILIO_AUTH_TOKEN   : {'Found' if auth_ok else 'MISSING in .env'}")
    print(f"  [3] TWILIO_WHATSAPP_NUMBER (env): '{TWILIO_WHATSAPP_NUM or 'not set — will auto-fetch'}'")

    if not sid_ok or not auth_ok:
        print("FIX: Add your Twilio credentials to .env:")
        print("    TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxx")
        print("    TWILIO_AUTH_TOKEN=your_token_here")
        divider()
        return

    # Check 2: Try connecting to Twilio
    try:
        client = TwilioClient(TWILIO_SID, TWILIO_AUTH)
        account = client.api.accounts(TWILIO_SID).fetch()
        print(f"  [4] Twilio account      : Connected ({account.friendly_name})")
    except Exception as e:
        print(f"  [4] Twilio account      : FAILED — {e}")
        print("FIX: Check your SID and AUTH TOKEN at console.twilio.com")
        divider()
        return

    # Check 3: List WhatsApp senders
    try:
        numbers = client.incoming_phone_numbers.list(limit=20)
        print(f"  [5] Phone numbers found : {len(numbers)}")
        for n in numbers:
            print(f"       → {n.phone_number}  ({n.friendly_name})")
        if not numbers:
            print("       (none found — you may be on a free trial with no numbers)")
    except Exception as e:
        print(f"  [5] Phone numbers       : Could not fetch — {e}")

    # Check 4: Print sandbox join instructions
    print()
    print("  ─── SANDBOX SETUP (if you haven't done this) ───────────────")
    print("  For Twilio WhatsApp Sandbox to work, the patient's phone")
    print("  must first send a JOIN message to activate the sandbox.")
    print()
    print("  Steps:")
    print("  1. Go to: https://console.twilio.com/us1/develop/sms/try-it-out/whatsapp-learn")
    print("  2. Note the sandbox number and join keyword shown there")
    print("  3. From the patient's WhatsApp, send:")
    print("       join <your-sandbox-keyword>")
    print("     to the sandbox number (e.g. +14155238886)")
    print("  4. Add the EXACT sandbox number to your .env:")
    print("       TWILIO_WHATSAPP_NUMBER=whatsapp:+14155238886")
    print("  5. The patient's number must match what's registered in sandbox")
    print()
    print("  ─── ALTERNATIVE: Use your actual Twilio number ──────────────")
    print("  If you have a Twilio number with WhatsApp enabled:")
    print("    TWILIO_WHATSAPP_NUMBER=whatsapp:+<your_twilio_number>")
    divider()


# ── 5. SCHEDULE REMINDER ──────────────────────────────────────────────────────

def schedule_reminder():
    clear()
    banner()
    print("\n    SCHEDULE WHATSAPP REMINDER\n")
    divider()
    print("  This will send a WhatsApp text message + voice note to the patient.\n")
    divider()

    patient_id = ask("Enter Patient ID (e.g. P12345)")

    conn = get_connection()
    if not conn:
        error("Could not connect to database.")
        pause()
        return

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT name, phone FROM patients WHERE patient_id = %s", (patient_id,))
        rec = cursor.fetchone()
        cursor.close()
        conn.close()
    except Error as e:
        error(f"Database error: {e}")
        pause()
        return

    if not rec:
        error("Patient not found. Please register first.")
        pause()
        return

    divider()
    info("Patient", rec["name"])
    info("Phone",   rec["phone"])
    info("Will send to", _format_phone(rec["phone"]))
    divider()

    message = ask("Reminder Message (e.g. Take insulin 500mg after dinner)")

    # ── When to send? ─────────────────────────────────────────────────────────
    timing = ask_choice(
        "When do you want to send this reminder?",
        ["Send Now (immediately)", "Schedule for Later (pick date & time)"]
    )

    if "Now" in timing:
        remind_dt = datetime.now()
        remind_at = remind_dt.isoformat()
        send_now  = True
        print(f"\n  Reminder will be sent immediately at {remind_dt.strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        send_now = False
        print("\n  Enter Reminder Date & Time")
        print("  Format: YYYY-MM-DD HH:MM  ->  Example: 2026-03-20 08:00")
        remind_raw = ask("Date & Time")
        try:
            remind_dt = datetime.strptime(remind_raw, "%Y-%m-%d %H:%M")
            remind_at = remind_dt.isoformat()
        except ValueError:
            error("Invalid format. Use YYYY-MM-DD HH:MM  (e.g. 2026-03-20 08:00)")
            pause()
            return

    # ── Confirm before sending ────────────────────────────────────────────────
    divider()
    print("\n  Confirm Reminder:\n")
    info("Patient",  rec["name"])
    info("Phone",    rec["phone"])
    info("Message",  message)
    info("Send At",  "RIGHT NOW" if send_now else remind_at)
    info("Channels", "WhatsApp Text + Voice Note (gTTS)")
    print()

    confirm = ask_choice("  Confirm and send?", ["Yes, Confirm", "No, Cancel"])
    if "No" in confirm:
        print("\n  Reminder cancelled.")
        pause()
        return

    # ── STEP 1: Generate voice note ───────────────────────────────────────────
    divider()
    print("\n  STEP 1/3  Generating voice note with gTTS...")
    audio_path = generate_voice_note(message, rec["name"])

    if audio_path:
        print(f"  Voice note created → {Path(audio_path).name}")
    else:
        print("  Voice note generation failed. Will send text only.")

    # ── STEP 2: Upload audio to get public URL ────────────────────────────────
    audio_url = None
    if audio_path:
        print("\n  STEP 2/3  Uploading audio to get public URL...")
        audio_url = upload_audio_for_twilio(audio_path)
        if audio_url:
            print(f"  Uploaded  → {audio_url}")
        else:
            print("  Upload failed. Will send text message only.")

        # Clean up local temp file
        try:
            os.remove(audio_path)
        except Exception:
            pass
    else:
        print("\n  STEP 2/3  Skipped (no audio to upload).")

    # ── STEP 3: Send via Twilio WhatsApp ──────────────────────────────────────
    print("\n  STEP 3/3  Sending via Twilio WhatsApp...")
    result = send_whatsapp_reminder(rec["phone"], rec["name"], message, audio_url)

    # ── Save reminder record in memory ────────────────────────────────────────
    r_id = f"R{random.randint(1000, 9999)}"
    _reminders.append({
        "reminder_id": r_id,
        "patient_id":  patient_id,
        "message":     message,
        "remind_at":   remind_at,
        "channel":     "whatsapp",
        "audio_url":   audio_url,
        "twilio_text_sid":  result.get("text_sid"),
        "twilio_audio_sid": result.get("audio_sid"),
        "status":      result["status"],
    })

    # ── Show result ───────────────────────────────────────────────────────────
    divider()
    if result["status"] == "ok":
        print("\n    Reminder sent successfully!\n")
        info("Reminder ID",      r_id)
        info("WhatsApp Text SID", result.get("text_sid", "N/A"))
        info("Voice Note SID",    result.get("audio_sid", "N/A"))
        info("Delivered To",      _format_phone(rec["phone"]))
        print()
        print("  The patient received:")
        print("    [1]  A WhatsApp text message with the reminder")
        print("    [2]  A voice note of the same reminder (gTTS audio)")

    elif result["status"] == "partial_error":
        print("\n  Reminder partially sent.\n")
        if result.get("text_sid"):
            info("Text message", f"Sent (SID: {result['text_sid']})")
        else:
            info("Text message", f"Failed — {result.get('text_error', 'unknown error')}")
        if result.get("audio_sid"):
            info("Voice note",   f"Sent (SID: {result['audio_sid']})")
        else:
            info("Voice note",   f"Failed — {result.get('audio_error', 'unknown error')}")
        # If BOTH failed it is almost always a sender/sandbox config issue
        if not result.get("text_sid") and not result.get("audio_sid"):
            run_twilio_diagnostics()

    else:
        error(f"Failed to send reminder: {result.get('reason', 'unknown error')}")
        run_twilio_diagnostics()

    divider()
    pause()


# ── 6. TODAY'S APPOINTMENTS ───────────────────────────────────────────────────

def list_todays_appointments():
    clear()
    banner()
    print("\n   TODAY'S APPOINTMENTS\n")
    divider()

    today = datetime.now().strftime("%Y-%m-%d")

    conn = get_connection()
    if not conn:
        error("Could not connect to database.")
        pause()
        return

    try:
        cursor = conn.cursor(dictionary=True)

        # JOIN appointments with patients to get patient name & phone
        cursor.execute("""
            SELECT
                a.appt_id,
                a.patient_id,
                p.name        AS patient_name,
                p.phone       AS patient_phone,
                a.doctor,
                a.appt_date,
                a.appt_time,
                a.status,
                a.booked_at
            FROM appointments a
            JOIN patients p ON a.patient_id = p.patient_id
            WHERE a.appt_date = %s
            ORDER BY a.appt_time ASC
        """, (today,))

        todays = cursor.fetchall()
        cursor.close()
        conn.close()

    except Error as e:
        error(f"Database error: {e}")
        pause()
        return

    info("Date",  today)
    info("Total Appointments", str(len(todays)))
    divider()

    if not todays:
        print("\n    No appointments scheduled for today.\n")
    else:
        # ── Print table header ────────────────────────────────────────────────
        print()
        print(f"  {'#':<4} {'Appt ID':<10} {'Patient ID':<12} {'Patient Name':<20} {'Doctor':<18} {'Time':<8} {'Status'}")
        print("  " + "─" * 88)

        for i, a in enumerate(todays, 1):
            t = str(a["appt_time"])          # timedelta → HH:MM:SS
            t = t[:5] if len(t) >= 5 else t  # trim to HH:MM
            print(
                f"  {i:<4} "
                f"{a['appt_id']:<10} "
                f"{a['patient_id']:<12} "
                f"{a['patient_name']:<20} "
                f"{a['doctor']:<18} "
                f"{t:<8} "
                f"{a['status'].capitalize()}"
            )

        print("  " + "─" * 88)
        print(f"\n    All appointments are fetched from MySQL with patient JOIN.\n")

    divider()
    pause()


# ── 7. VIEW ALL APPOINTMENTS FOR A PATIENT ───────────────────────────────────

def view_patient_appointments():
    clear()
    banner()
    print("\n  SCHEDULE: VIEW APPOINTMENTS BY PATIENT\n")
    divider()
    print("  Shows all appointments linked to a Patient ID.")
    print("  (patient_id is the Foreign Key in the appointments table)\n")
    divider()

    patient_id = ask("Enter Patient ID (e.g. P12345)")

    conn = get_connection()
    if not conn:
        error("Could not connect to database.")
        pause()
        return

    try:
        cursor = conn.cursor(dictionary=True)

        # Verify patient exists
        cursor.execute("SELECT name, phone, disease FROM patients WHERE patient_id = %s", (patient_id,))
        patient = cursor.fetchone()

        if not patient:
            error(f"No patient found with ID '{patient_id}'.")
            cursor.close()
            conn.close()
            pause()
            return

        # Fetch all appointments for this patient, newest first
        cursor.execute("""
            SELECT
                appt_id,
                patient_id,
                doctor,
                appt_date,
                appt_time,
                status,
                booked_at
            FROM appointments
            WHERE patient_id = %s
            ORDER BY appt_date DESC, appt_time DESC
        """, (patient_id,))

        appointments = cursor.fetchall()
        cursor.close()
        conn.close()

    except Error as e:
        error(f"Database error: {e}")
        pause()
        return

    # ── Patient Summary ───────────────────────────────────────────────────────
    divider()
    print("\n  PATIENT DETAILS\n")
    info("Patient ID", patient_id)
    info("Name",       patient["name"])
    info("Phone",      patient["phone"])
    info("Disease",    patient.get("disease") or "Not specified")

    # ── Appointment Table ─────────────────────────────────────────────────────
    divider()
    print(f"\n  APPOINTMENTS TABLE  (Total: {len(appointments)})\n")
    print(f"  {'Appt ID (PK)':<14}  {'Patient ID (FK)':<16}  {'Doctor':<18}  {'Date':<12}  {'Time':<8}  {'Status':<12}  Booked At")
    print("  " + "=" * 100)

    if not appointments:
        print("\n  No appointments found for this patient.\n")
    else:
        for a in appointments:
            t = str(a["appt_time"])
            t = t[:5] if len(t) >= 5 else t
            booked = str(a["booked_at"])[:16]
            status = a["status"].capitalize()
            print(
                f"  {a['appt_id']:<14}  "
                f"{a['patient_id']:<16}  "
                f"{a['doctor']:<18}  "
                f"{str(a['appt_date']):<12}  "
                f"{t:<8}  "
                f"{status:<12}  "
                f"{booked}"
            )

    print("  " + "=" * 100)
    print("\n  NOTE: appt_id = Primary Key | patient_id = Foreign Key -> patients.patient_id\n")
    divider()

    # ── Option to update appointment status ───────────────────────────────────
    if appointments:
        action = ask_choice(
            "What would you like to do?",
            ["Update an Appointment Status", "Go Back to Menu"]
        )

        if "Update" in action:
            appt_id_to_update = ask("Enter Appointment ID to update (e.g. A1234)")

            valid_ids = [a["appt_id"] for a in appointments]
            if appt_id_to_update not in valid_ids:
                error(f"Appointment '{appt_id_to_update}' not found for this patient.")
                pause()
                return

            new_status = ask_choice("Set new status", ["scheduled", "completed", "cancelled"])

            conn3 = get_connection()
            if not conn3:
                error("Could not connect to database.")
                pause()
                return

            try:
                cur3 = conn3.cursor()
                cur3.execute(
                    "UPDATE appointments SET status = %s WHERE appt_id = %s",
                    (new_status, appt_id_to_update)
                )
                conn3.commit()
                cur3.close()
                conn3.close()
                divider()
                success(f"Appointment {appt_id_to_update} updated to '{new_status}'.")
                divider()
            except Error as e:
                error(f"Database error: {e}")

    pause()


# ── 8. ADD CONSULTATION NOTE ──────────────────────────────────────────────────

def add_consultation_note():
    clear()
    banner()
    print("\n    ADD CONSULTATION NOTE\n")
    divider()

    patient_id = ask("Enter Patient ID (e.g. P12345)")

    conn = get_connection()
    if not conn:
        error("Could not connect to database.")
        pause()
        return

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT name, age FROM patients WHERE patient_id = %s", (patient_id,))
        rec = cursor.fetchone()
        cursor.close()
        conn.close()
    except Error as e:
        error(f"Database error: {e}")
        pause()
        return

    if not rec:
        error("Patient not found. Please register first.")
        pause()
        return

    info("Patient", f"{rec['name']} | Age {rec['age']}")
    divider()

    doctor = ask("Doctor's Name")
    print("\n    Type consultation note below.")
    print("      Press Enter twice when you're done.\n")

    lines = []
    while True:
        line = input("     ")
        if line == "" and lines and lines[-1] == "":
            break
        lines.append(line)
    note = "\n".join(lines).strip()

    if not note:
        error("Note cannot be empty.")
        pause()
        return

    divider()
    print("\n    Confirm Note:\n")
    info("Patient", rec["name"])
    info("Doctor",  doctor)
    print(f"\n  Note:\n    {note}\n")

    confirm = ask_choice("  Save this note?", ["Yes, Save", "No, Cancel"])
    if "No" in confirm:
        print("\n   Note discarded.")
        pause()
        return

    note_id = f"N{random.randint(1000, 9999)}"
    _consultation_notes.append({
        "note_id": note_id, "patient_id": patient_id,
        "doctor": doctor, "note": note,
        "timestamp": datetime.now().isoformat(),
    })

    divider()
    success("Consultation note saved!")
    info("Note ID", note_id)
    divider()
    pause()


# ── Main Menu ─────────────────────────────────────────────────────────────────

MENU = [
    ("Register New Patient",                    register_patient),
    ("Find Patient",                            get_patient),
    ("Generate AI Symptom Summary",             generate_symptom_summary),
    ("Book Appointment",                        book_appointment),
    ("View All Appointments for a Patient",     view_patient_appointments),
    ("Schedule Reminder",                       schedule_reminder),
    ("Today's Appointments (All Doctors)",      list_todays_appointments),
    ("Add Consultation Note",                   add_consultation_note),
    ("Exit",                                    None),
]

def main():
    clear()
    banner()
    print("\n  Connecting to MySQL database...")

    if not init_database():
        print("\n  Could not initialize database.")
        print("  Please check your .env settings and try again.")
        return

    print("  Database connected and ready!\n")
    time.sleep(1.2)

    while True:
        clear()
        banner()
        print("\n  What would you like to do?\n")
        for i, (label, _) in enumerate(MENU, 1):
            print(f"    [{i:>2}]  {label}")
        divider()

        total = len(MENU)
        choice = input(f"\n  Enter your choice (1-{total}): ").strip()
        if not choice.isdigit() or not (1 <= int(choice) <= total):
            print(f"  Invalid choice. Please enter a number between 1 and {total}.")
            time.sleep(1.5)
            continue

        idx = int(choice) - 1
        _, fn = MENU[idx]

        if fn is None:
            clear()
            banner()
            print("\n  Thank you for using ClinicOS. Goodbye!\n")
            divider()
            break

        fn()

if __name__ == "__main__":
    main()