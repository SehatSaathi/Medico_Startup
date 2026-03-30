"""
NutriAgent India — Personalized Nutrition Agent
------------------------------------------------
Features:
  * Step-by-step onboarding (one question at a time)
  * On EXIT: asks to save -> checks if patient already exists (reuse ID) or creates new one
  * MySQL database: single `patients` table with diet_prescribed as a clickable file:// link
  * `diet_prescribed` column stores a clickable file:// URI to the patient's diet PDF
  * PDF generated with reportlab — fully formatted: Breakfast/Snack/Lunch/Snack/Dinner
  * All config (API key, model, DB name, PDF folder) read from .env only
"""

import os, re, time, random, textwrap, uuid
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
import mysql.connector
from mysql.connector import Error
import google.generativeai as genai

# reportlab
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer,
    HRFlowable, Table, TableStyle,
)

# ─────────────────────────────────────────────────────────────────
# CONFIG — everything from .env
# ─────────────────────────────────────────────────────────────────

# Load .env from the same directory as this script
_SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=_SCRIPT_DIR / ".env", override=True)

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
MODEL   = os.getenv("GEMINI_MODEL",    "gemini-2.5-flash")
PDF_DIR = Path(os.getenv("DIET_PDF_DIR", "diet_pdfs"))
PDF_DIR.mkdir(parents=True, exist_ok=True)

# MySQL connection config — all from .env
DB_CONFIG = {
    "host":     os.getenv("DB_HOST",     "localhost"),
    "port":     int(os.getenv("DB_PORT", "3306")),
    "user":     os.getenv("DB_USER",     "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME_nutriagent", "nutriagent"),
}

def _get_conn():
    """Return a fresh MySQL connection."""
    return mysql.connector.connect(**DB_CONFIG)


# ─────────────────────────────────────────────────────────────────
# DATABASE  (MySQL)
# ─────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Create all tables inside the existing MySQL database if they don't exist."""
    conn = _get_conn()
    c    = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS patients (
            patient_id      VARCHAR(20)  PRIMARY KEY,
            name            VARCHAR(120) NOT NULL,
            age             VARCHAR(10),
            gender          VARCHAR(20),
            weight_kg       VARCHAR(10),
            height_cm       VARCHAR(10),
            region          VARCHAR(80)  NOT NULL,
            diet_type       VARCHAR(30)  DEFAULT 'vegetarian',
            conditions      TEXT,
            symptoms        TEXT,
            budget_inr      VARCHAR(20)  DEFAULT '3000',
            diet_prescribed TEXT,
            created_at      DATETIME,
            updated_at      DATETIME
        )
    """)

    conn.commit()
    c.close()
    conn.close()

    # Confirm
    conn2 = _get_conn()
    c2    = conn2.cursor()
    c2.execute("SHOW TABLES")
    tables = [r[0] for r in c2.fetchall()]
    c2.close()
    conn2.close()
    print(f"[DB] Connected to MySQL '{DB_CONFIG['database']}' — tables: {tables}")

def _new_patient_id() -> str:
    return "NUT-" + uuid.uuid4().hex[:8].upper()


def find_patient_by_name(name: str) -> dict | None:
    """Case-insensitive lookup. Returns existing patient dict or None."""
    conn = _get_conn()
    c    = conn.cursor(dictionary=True)
    c.execute("SELECT * FROM patients WHERE LOWER(name) = LOWER(%s)", (name.strip(),))
    row  = c.fetchone()
    c.close(); conn.close()
    return row


def get_patient(patient_id: str) -> dict | None:
    conn = _get_conn()
    c    = conn.cursor(dictionary=True)
    c.execute("SELECT * FROM patients WHERE patient_id = %s", (patient_id,))
    row  = c.fetchone()
    c.close(); conn.close()
    return row


def upsert_patient(profile: dict, patient_id: str | None = None) -> str:
    """Insert new patient or update existing. Returns patient_id used."""
    pid = patient_id or _new_patient_id()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _get_conn()
    c    = conn.cursor()
    c.execute(
        """
        INSERT INTO patients
            (patient_id, name, age, gender, weight_kg, height_cm,
             region, diet_type, conditions, symptoms, budget_inr,
             diet_prescribed, created_at, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            age=VALUES(age),            gender=VALUES(gender),
            weight_kg=VALUES(weight_kg), height_cm=VALUES(height_cm),
            region=VALUES(region),       diet_type=VALUES(diet_type),
            conditions=VALUES(conditions), symptoms=VALUES(symptoms),
            budget_inr=VALUES(budget_inr), updated_at=VALUES(updated_at)
        """,
        (
            pid,
            profile.get("name", ""),
            profile.get("age", ""),
            profile.get("gender", ""),
            profile.get("weight_kg", ""),
            profile.get("height_cm", ""),
            profile.get("region", ""),
            profile.get("diet_type", "vegetarian"),
            profile.get("conditions", "None"),
            profile.get("symptoms", "None"),
            profile.get("budget_inr", "3000"),
            profile.get("diet_prescribed", ""),
            now, now,
        ),
    )
    conn.commit()
    c.close(); conn.close()
    return pid


def set_diet_prescribed(patient_id: str, pdf_uri: str) -> None:
    conn = _get_conn()
    c    = conn.cursor()
    c.execute("UPDATE patients SET diet_prescribed = %s WHERE patient_id = %s", (pdf_uri, patient_id))
    conn.commit()
    c.close(); conn.close()


# ─────────────────────────────────────────────────────────────────
# PDF GENERATION  (reportlab)
# ─────────────────────────────────────────────────────────────────

_SAFFRON = colors.HexColor("#FF6B35")
_GREEN   = colors.HexColor("#2E7D32")
_CREAM   = colors.HexColor("#FFF8F0")
_DARK    = colors.HexColor("#1A1A1A")


def _ascii(text: str) -> str:
    """Strip non-ASCII chars so the default PDF font doesn't choke."""
    return re.sub(r"[^\x00-\x7F]+", " ", text).strip()


def generate_diet_pdf(patient_id: str, plan_text: str, profile: dict) -> str:
    """
    Build a formatted A4 PDF.
    Returns the absolute  file://  URI so it can be stored and opened by clicking.
    """
    safe   = re.sub(r"[^a-zA-Z0-9_-]", "_", profile.get("name", "patient"))
    fname  = f"{patient_id}_{safe}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    fpath  = PDF_DIR / fname

    doc    = SimpleDocTemplate(
        str(fpath), pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm,
        topMargin=2*cm,  bottomMargin=2*cm,
    )
    ss = getSampleStyleSheet()

    # Custom paragraph styles
    def _ps(name, parent="Normal", **kw):
        return ParagraphStyle(name, parent=ss[parent], **kw)

    s_title   = _ps("T",  "Title",   fontSize=22, textColor=_SAFFRON, alignment=TA_CENTER, spaceAfter=2)
    s_sub     = _ps("S",  "Normal",  fontSize=10, textColor=_GREEN,   alignment=TA_CENTER, spaceAfter=2)
    s_day     = _ps("D",  "Heading2",fontSize=11, textColor=colors.white,
                    backColor=_GREEN, spaceBefore=12, spaceAfter=4,
                    leftIndent=-4, rightIndent=-4, borderPad=5, leading=16)
    s_meal    = _ps("M",  "Normal",  fontSize=10, textColor=_SAFFRON,
                    fontName="Helvetica-Bold", spaceBefore=6, spaceAfter=2)
    s_body    = _ps("B",  "Normal",  fontSize=9,  textColor=_DARK,    leading=13, leftIndent=14, spaceAfter=1)
    s_note    = _ps("N",  "Normal",  fontSize=8,  textColor=colors.HexColor("#555"),
                    leading=12, leftIndent=14)
    s_section = _ps("SC", "Heading1",fontSize=12, textColor=_SAFFRON,  spaceBefore=12, spaceAfter=4)
    s_footer  = _ps("F",  "Normal",  fontSize=7,  textColor=colors.grey,
                    alignment=TA_CENTER, spaceBefore=6)

    story = []

    # Header
    story.append(Paragraph("NutriAgent India", s_title))
    story.append(Paragraph("Personalised 7-Day Indian Diet Plan", s_sub))
    story.append(HRFlowable(width="100%", thickness=2, color=_SAFFRON, spaceAfter=8))

    # Patient info table
    p = profile
    rows = [
        ["Patient ID",   patient_id,                    "Name",         p.get("name", "")],
        ["Region",       p.get("region", ""),            "Diet",         p.get("diet_type","").capitalize()],
        ["Age / Gender", f"{p.get('age','')} yrs / {p.get('gender','')}",
         "Weight / Ht",  f"{p.get('weight_kg','')} kg / {p.get('height_cm','')} cm"],
        ["Conditions",   p.get("conditions", "None"),   "Monthly Budget", f"Rs. {p.get('budget_inr','')}"],
        ["Symptoms",     p.get("symptoms",   "None"),   "Generated",     datetime.now().strftime("%d %b %Y")],
    ]
    tbl = Table(rows, colWidths=[3.5*cm, 5.5*cm, 3.5*cm, 5.5*cm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND",   (0,0), (-1, 0), _GREEN),
        ("TEXTCOLOR",    (0,0), (-1, 0), colors.white),
        ("FONTNAME",     (0,0), (-1,-1), "Helvetica"),
        ("FONTNAME",     (0,0), ( 0,-1), "Helvetica-Bold"),
        ("FONTNAME",     (2,0), ( 2,-1), "Helvetica-Bold"),
        ("FONTSIZE",     (0,0), (-1,-1), 8),
        ("GRID",         (0,0), (-1,-1), 0.4, colors.HexColor("#CCCCCC")),
        ("ROWBACKGROUNDS",(0,0),(-1,-1), [_CREAM, colors.white]),
        ("VALIGN",       (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",   (0,0), (-1,-1), 4),
        ("BOTTOMPADDING",(0,0), (-1,-1), 4),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=1, color=_GREEN, spaceAfter=6))

    # ── Parse & render plan text ──────────────────────────────
    MEAL_KW = {
        "BREAKFAST":      "Breakfast",
        "MID-MORNING":    "Mid-Morning Snack",
        "LUNCH":          "Lunch",
        "EVENING SNACK":  "Evening Snack",
        "EVENING":        "Evening Snack",
        "DINNER":         "Dinner",
        "DAILY WATER":    "Daily Water Intake",
        "TOTAL CALORIE":  "Total Calories",
    }

    in_tips = False
    for raw in plan_text.splitlines():
        line = _ascii(raw)
        if not line:
            continue
        up = line.upper()

        # Day header
        if re.match(r"^(DAY\s*\d|={3,})", up):
            clean = re.sub(r"[=*_]+", "", line).strip()
            if clean:
                story.append(Paragraph(clean, s_day))
            in_tips = False
            continue

        # Weekly tips section header
        if "WEEKLY" in up and ("TIP" in up or "NUTRI" in up):
            story.append(HRFlowable(width="100%", thickness=1, color=_GREEN, spaceAfter=4))
            story.append(Paragraph("Weekly Nutrition Tips", s_section))
            in_tips = True
            continue

        if in_tips:
            tip = re.sub(r"^[-*. ]+", "", line).strip()
            if tip:
                story.append(Paragraph(f"  {tip}", s_body))
            continue

        # Meal keyword label
        matched = None
        for kw, label in MEAL_KW.items():
            if up.startswith(kw):
                matched = label
                break
        if matched:
            story.append(Paragraph(matched, s_meal))
            rest = line.split(":", 1)[1].strip() if ":" in line else ""
            if rest:
                story.append(Paragraph(rest, s_body))
            continue

        # Detail lines (Dish:, Quantity:, Calories:, Key Nutrients:)
        if re.match(r"^\s+(Dish|Quantity|Calories|Key Nutri|Amount)", line, re.I):
            story.append(Paragraph(line.strip(), s_note))
            continue

        # Separator lines
        if re.match(r"^[-=]{5,}$", line.strip()):
            story.append(HRFlowable(width="100%", thickness=0.5,
                                    color=colors.HexColor("#DDDDDD"), spaceAfter=2))
            continue

        # Bullet / generic
        clean_line = re.sub(r"^[-*] ", "", line).strip()
        if clean_line:
            story.append(Paragraph(clean_line, s_body))

    # Footer
    story.append(Spacer(1, 14))
    story.append(HRFlowable(width="100%", thickness=1.5, color=_SAFFRON))
    story.append(Paragraph(
        "Generated by NutriAgent India | For informational purposes only. "
        "Consult a registered dietitian before making dietary changes.",
        s_footer,
    ))

    doc.build(story)
    return fpath.resolve().as_uri()   # file:///abs/path/file.pdf


# ─────────────────────────────────────────────────────────────────
# ONBOARDING STATE MACHINE
# ─────────────────────────────────────────────────────────────────

ONBOARDING_STEPS: list[tuple[str, str]] = [
    ("name",
     "Welcome to NutriAgent India!\n"
     "I will help you build a personalised Indian diet plan.\n\n"
     "Let's start — what is your full name?"),

    ("region",
     "Which state / region of India are you from?\n"
     "  (e.g. Maharashtra, Punjab, Tamil Nadu, Kerala, Bengal)"),

    ("diet_type",
     "What is your diet preference?\n"
     "  Type: vegetarian, vegan, or non-vegetarian"),

    ("age",      "How old are you? (age in years)"),
    ("gender",   "What is your gender?  (Male / Female / Other)"),
    ("weight_kg","What is your current weight in kg?  (e.g. 65)"),
    ("height_cm","What is your height in cm?  (e.g. 165)"),

    ("conditions",
     "Do you have any medical conditions?\n"
     "  (e.g. Diabetes, Hypertension, PCOD, Thyroid)\n"
     "  Type None if you have none."),

    ("symptoms",
     "Are you experiencing any current symptoms?\n"
     "  (e.g. fatigue, hair fall, brittle nails, muscle cramps)\n"
     "  Type None if you feel fine."),

    ("budget_inr",
     "What is your monthly grocery budget in INR?\n"
     "  (e.g. 3000, 5000, 8000)"),
]

_sessions: dict[str, dict] = {}


def _get_session(sid: str) -> dict:
    if sid not in _sessions:
        _sessions[sid] = {
            "step":         0,
            "profile":      {},
            "patient_id":   None,
            "onboarded":    False,
            "diet_text":    "",
            "exit_pending": False,
        }
    return _sessions[sid]


# ─────────────────────────────────────────────────────────────────
# GEMINI WRAPPER
# ─────────────────────────────────────────────────────────────────

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
    raise RuntimeError("Gemini rate-limit exceeded after 5 retries.")


# ─────────────────────────────────────────────────────────────────
# DIET PLAN TEXT GENERATION
# ─────────────────────────────────────────────────────────────────

def generate_formatted_diet_plan(profile: dict, goal: str = "", mood: str = "") -> str:
    prompt = textwrap.dedent(f"""
        You are a certified Indian clinical nutritionist.
        Create a detailed 7-day meal plan for this patient.

        Patient Profile
        ---------------
        Name          : {profile.get('name')}
        Region        : {profile.get('region')}
        Diet          : {profile.get('diet_type', 'vegetarian')}
        Age / Gender  : {profile.get('age')} yrs / {profile.get('gender')}
        Weight / Ht   : {profile.get('weight_kg')} kg / {profile.get('height_cm')} cm
        Conditions    : {profile.get('conditions', 'None')}
        Symptoms      : {profile.get('symptoms', 'None')}
        Monthly Budget: Rs. {profile.get('budget_inr', '3000')}
        Goal          : {goal or 'general health & wellness'}
        Mood          : {mood or 'neutral'}

        Format EVERY day exactly like the template below.
        Use authentic regional dishes from {profile.get('region')}.
        Adapt for the patient's conditions and symptoms.
        Do NOT use any emoji or non-ASCII characters in your response.

        ===================================
        DAY 1 - Monday
        ===================================

        BREAKFAST (7:00 - 8:30 AM)
            Dish      : <dish name>
            Quantity  : <amount>
            Calories  : ~<X> kcal
            Key Nutrients: <list>

        MID-MORNING SNACK (10:30 - 11:00 AM)
            Dish      : <dish name>
            Quantity  : <amount>
            Calories  : ~<X> kcal

        LUNCH (1:00 - 2:00 PM)
            Dish      : <dish name>
            Quantity  : <amount>
            Calories  : ~<X> kcal
            Key Nutrients: <list>

        EVENING SNACK (4:30 - 5:00 PM)
            Dish      : <dish name>
            Quantity  : <amount>
            Calories  : ~<X> kcal

        DINNER (7:30 - 8:30 PM)
            Dish      : <dish name>
            Quantity  : <amount>
            Calories  : ~<X> kcal
            Key Nutrients: <list>

        DAILY WATER INTAKE : <X> glasses
        TOTAL CALORIES     : ~<X> kcal / day

        -----------------------------------

        Repeat this exact format for Day 1 (Monday) through Day 7 (Sunday).
        End with a "WEEKLY NUTRITION TIPS" section containing 4 bullet points.
    """)
    return _call_gemini(prompt)


# ─────────────────────────────────────────────────────────────────
# POST-ONBOARDING TOOL FUNCTIONS
# ─────────────────────────────────────────────────────────────────





def _handle_save(session: dict, answer: str) -> str:
    session["exit_pending"] = False
    if answer.lower().strip() not in ("yes", "y"):
        return "Your session was NOT saved. Goodbye! Stay healthy."

    profile = session["profile"]

    # Look up by name — reuse existing ID or mint a new one
    existing     = find_patient_by_name(profile.get("name", ""))
    is_returning = existing is not None
    pid          = upsert_patient(profile, patient_id=existing["patient_id"] if existing else None)
    session["patient_id"] = pid

    # Generate diet text if somehow missing
    plan_text = session.get("diet_text") or generate_formatted_diet_plan(profile)
    session["diet_text"] = plan_text

    # Build PDF and store link
    pdf_uri = generate_diet_pdf(pid, plan_text, profile)
    set_diet_prescribed(pid, pdf_uri)

    status = "Welcome back! Your record has been updated." if is_returning else "New patient registered!"

    return (
        f"\n{'='*60}\n"
        f"  {status}\n"
        f"{'='*60}\n"
        f"  Patient ID   : {pid}\n"
        f"  Name         : {profile.get('name')}\n"
        f"  Region       : {profile.get('region')}\n"
        f"  Diet PDF     : {pdf_uri}\n"
        f"{'='*60}\n\n"
        f"  The diet_prescribed column in the patients table\n"
        f"  contains a file:// link. In MySQL Workbench:\n"
        f"    1. Right-click the cell -> Open Value in Viewer\n"
        f"    2. Or copy the link and paste in your browser / Finder\n\n"
        f"Goodbye! Stay healthy."
    )


# ─────────────────────────────────────────────────────────────────
# MAIN CONVERSATION HANDLER
# ─────────────────────────────────────────────────────────────────

def run(user_message: str, session_id: str = "default") -> str:
    session = _get_session(session_id)
    msg     = user_message.strip()

    # 1. Waiting for yes/no after exit prompt
    if session["exit_pending"]:
        return _handle_save(session, msg)

    # 2. Exit / quit triggers the save confirmation
    if msg.lower() in ("exit", "quit", "bye"):
        if not session["onboarded"] and session["step"] <= 1:
            return "Goodbye! No data was collected."
        session["exit_pending"] = True
        return (
            "Before you go — would you like to save your session to the database?\n"
            "Your diet plan PDF will be generated and a link stored under\n"
            "the 'diet_prescribed' column in the patients table.\n\n"
            "Type  yes  to save   |   no  to discard."
        )

    # 3. Onboarding — collect one field at a time
    if not session["onboarded"]:
        step  = session["step"]
        total = len(ONBOARDING_STEPS)

        if step == 0:                          # very first call — just show Q1
            session["step"] += 1
            return ONBOARDING_STEPS[0][1]

        # Save previous answer
        session["profile"][ONBOARDING_STEPS[step - 1][0]] = msg

        if step == total:                      # all answers collected
            session["onboarded"] = True
            profile   = session["profile"]
            plan_text = generate_formatted_diet_plan(profile)
            session["diet_text"] = plan_text
            return (
                f"\nHere is your personalised 7-day Indian meal plan, {profile.get('name')}!\n\n"
                f"{plan_text}\n\n"
                f"{'─'*50}\n"
                f"Commands available:\n"
                f"  new plan    - regenerate your diet plan\n"
                f"  my profile  - view your details\n"
                f"  exit        - save to database & quit\n"
            )

        session["step"] += 1
        _, question = ONBOARDING_STEPS[step]
        return f"[{step}/{total - 1}] {question}"

    # 4. Post-onboarding commands
    pid   = session.get("patient_id")   # None until saved
    lower = msg.lower()

    def _prof() -> dict:
        return get_patient(pid) if pid else session["profile"]

    # New / regenerate plan
    if any(k in lower for k in ("new plan", "meal plan", "diet plan", "regenerate")):
        plan = generate_formatted_diet_plan(_prof(), goal=msg)
        session["diet_text"] = plan
        return f"Updated 7-Day Meal Plan\n\n{plan}"

    # Profile card
    if any(k in lower for k in ("my profile", "my details", "patient id", "my id")):
        p = _prof()
        lines = [
            f"\n{'='*46}",
            f"  Patient ID : {pid or '(not saved yet)'}",
            f"  Name       : {p.get('name')}",
            f"  Region     : {p.get('region')}",
            f"  Diet       : {p.get('diet_type','').capitalize()}",
            f"  Age        : {p.get('age')} yrs",
            f"  Weight     : {p.get('weight_kg')} kg",
            f"  Height     : {p.get('height_cm')} cm",
            f"  Conditions : {p.get('conditions')}",
            f"  Symptoms   : {p.get('symptoms')}",
            f"  Budget     : Rs. {p.get('budget_inr')} / month",
        ]
        if pid:
            lines.append(f"  Diet PDF   : {p.get('diet_prescribed', 'not generated yet')}")
        lines.append(f"{'='*46}")
        return "\n".join(lines)

    # General Q&A
    p = _prof()
    return _call_gemini(textwrap.dedent(f"""
        You are NutriAgent India, a specialist in Indian regional nutrition.
        Patient: {p.get('name')}, {p.get('region')}, {p.get('diet_type')} diet,
        conditions: {p.get('conditions')}, symptoms: {p.get('symptoms')}.
        Answer concisely with Indian dietary context.
        Question: {msg}
    """))


# ─────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    sid = f"session_{int(time.time())}"

    print("\n" + "=" * 50)
    print("   NutriAgent India  |  Nutrition Assistant")
    print("   Type 'exit' to save your session and quit")
    print("=" * 50)

    print(f"\nNutriAgent:\n{run('start', sid)}\n")

    while True:
        try:
            msg = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nNutriAgent: Session interrupted. Goodbye!")
            break
        if not msg:
            continue

        reply = run(msg, sid)
        print(f"\nNutriAgent:\n{reply}\n")

        if "Goodbye!" in reply and not _sessions.get(sid, {}).get("exit_pending"):
            break