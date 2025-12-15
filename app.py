from flask import Flask, render_template, request, redirect, url_for, jsonify, session
import sqlite3
from pathlib import Path
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta



DB_PATH = Path("ehr_bezugspflege.db")

UPLOAD_FOLDER = Path("static/uploads")
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif"}

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.secret_key = "dev-secret-change-me"

# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------
def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def get_current_nurse(conn=None):
    nurse_id = session.get("current_nurse_id")
    if not nurse_id:
        return None

    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True

    cur = conn.cursor()
    cur.execute("SELECT id, name FROM nurses WHERE id = ?;", (nurse_id,))
    nurse = cur.fetchone()

    if close_conn:
        conn.close()

    return nurse

def get_med_interval_hours(schedule: str | None) -> int:
    if not schedule:
        return 8  # fallback

    s = schedule.lower()

    if "alle 1h" in s or "alle 1 h" in s:
        return 1
    if "alle 2h" in s or "alle 2 h" in s:
        return 2
    if "alle 4h" in s or "alle 4 h" in s:
        return 4

    if "1x täglich" in s or "1 x täglich" in s:
        return 24
    if "2x täglich" in s or "2 x täglich" in s:
        return 6
    if "3x täglich" in s or "3 x täglich" in s:
        return 8

    if "morgens" in s or "abends" in s or "nachts" in s:
        return 24

    return 8

def generate_ai_alerts(conn, patient_id):
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # get the last assessment
    cur.execute("""
        SELECT *
        FROM assessments
        WHERE patient_id = ?
        ORDER BY datetime(created_at) DESC
        LIMIT 1;
    """, (patient_id,))
    a = cur.fetchone()
    if not a:
        return

    alerts = []

    # -------------------------
    # 1. VITAL SIGN WARNINGS
    # -------------------------
    if a["temperature"] and a["temperature"] >= 38.5:
        alerts.append(("Fieber: bitte Infekt abklären", "warning"))

    if a["oxygen_sat"] and a["oxygen_sat"] < 90:
        alerts.append(("Schwere Hypoxie! O₂-Gabe prüfen", "critical"))

    if a["systolic_bp"] and a["systolic_bp"] < 90:
        alerts.append(("Hypotonie – Gefahr einer Schocksituation", "critical"))

    if a["heart_rate"] and a["heart_rate"] > 120:
        alerts.append(("Tachykardie: mögliche Schmerzen, Fieber oder Hypovolämie", "warning"))

    # -------------------------
    # 2. SEPSIS EARLY WARNING
    # -------------------------
    qsofa = 0
    if a["respiration_rate"] and a["respiration_rate"] >= 22:
        qsofa += 1
    if a["systolic_bp"] and a["systolic_bp"] < 100:
        qsofa += 1
    if a["confusion"] and a["confusion"] >= 5:
        qsofa += 1

    if qsofa >= 2:
        alerts.append(("⚠️ Sepsisverdacht! Arzt sofort informieren.", "critical"))

    # -------------------------
    # 3. MEDICATION SAFETY
    # -------------------------
    # Example rule: hypotension + beta blocker
    cur.execute("""
        SELECT name FROM medications
        WHERE patient_id = ?;
    """, (patient_id,))
    meds = [m["name"].lower() for m in cur.fetchall()]

    if "bisoprolol" in meds and a["systolic_bp"] and a["systolic_bp"] < 95:
        alerts.append(("Bisoprolol bei niedrigen RR mit Vorsicht verabreichen!", "warning"))

    # -------------------------
    # SAVE ALERTS (CLEAR OLD ALERTS)
    # -------------------------
    cur.execute("DELETE FROM ai_alerts WHERE patient_id = ?", (patient_id,))

    for text, severity in alerts:
        cur.execute("""
            INSERT INTO ai_alerts (patient_id, alert, severity, created_at)
            VALUES (?, ?, ?, ?);
        """, (patient_id, text, severity, datetime.now().isoformat(timespec="minutes")))

    conn.commit()


def get_default_interval_hours(description: str) -> int:
    """
    Very simple parser: look for 'alle Xh' in the description
    and return X as the interval in hours. Fallback = 4h.
    """
    desc = description.lower()
    if "alle 2h" in desc or "alle 2 h" in desc:
        return 2
    if "alle 4h" in desc or "alle 4 h" in desc:
        return 4
    if "alle 1h" in desc or "alle 1 h" in desc:
        return 1
    # default if nothing specific
    return 4

# ---------------------------------------------------------
# Voice documentation phrase → task mapping
# ---------------------------------------------------------
def map_spoken_phrase_to_task(text: str) -> str | None:
    t = text.lower()

    mappings = {
        "teilgewaschen": "Patient teilgewaschen",
        "ganzgewaschen": "Patient ganzgewaschen",
        "inhaliert": "Inhalation durchgeführt",
        "urin geleert": "Urinflasche geleert",
        "gelagert": "Lagerung alle 2h dokumentieren",
        "mobilisiert" :"Mobilisation nach Standard",
        "zähne geputzt": "Zahnpflege durchgeführt",
        "essen": "Beim Essen geholfen",
        "aufgeklärt": "Patient informiert / aufgeklärt",
        "op geprüft": "Postoperative Kontrolle durchgeführt",
        "hochlagert" : "Oberkörperhochlagerung, atemerleichternde Positionierung",
        "orientiert" : "Orientierungshilfen (Kalender, Uhr, Angehörige) bereitstellen",
        "wunde" : "Wundbehandlung durchgeführt",
    }

    for key, task in mappings.items():
        if key in t:
            return task

    return None


def generate_priorities_and_tasks(conn, patient_id: int) -> None:
    """
    Rule-based 'mini AI':
    - Looks at latest assessment
    - Generates up to 3 priority problems
    - Creates nursing tasks based on these problems
    """
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Latest assessment
    cur.execute("""
        SELECT *
        FROM assessments
        WHERE patient_id = ?
        ORDER BY datetime(created_at) DESC
        LIMIT 1;
    """, (patient_id,))
    a = cur.fetchone()
    if not a:
        return

    problems = []

    # Symptom scales
    if a["pain"] is not None and a["pain"] >= 7:
        problems.append("Starke Schmerzen")
    if a["mobility"] is not None and a["mobility"] <= 3:
        problems.append("Sturz- und Dekubitusrisiko")
    if a["confusion"] is not None and a["confusion"] >= 6:
        problems.append("Akute Verwirrtheit / Delirrisiko")

    # Vital signs
    if a["oxygen_sat"] is not None and a["oxygen_sat"] < 92:
        problems.append("Hypoxie-Risiko / O₂-Überwachung")
    if a["heart_rate"] is not None and a["heart_rate"] > 110:
        problems.append("Tachykardie / Kreislaufbelastung")
    if a["systolic_bp"] is not None and a["systolic_bp"] < 90:
        problems.append("Hypotonie – Kreislauf instabil")

    # Fallback
    if not problems:
        problems.append("Allgemeines Monitoring / Stabilisierung")

    # Take only top 3
    problems = problems[:3]

    # Clear old priorities
    cur.execute("DELETE FROM patient_priorities WHERE patient_id = ?;", (patient_id,))
    for rank, prob in enumerate(problems, start=1):
        cur.execute("""
            INSERT INTO patient_priorities (patient_id, priority_rank, problem)
            VALUES (?, ?, ?);
        """, (patient_id, rank, prob))


    for prob in problems:
        if "Atemnot" in prob or "Hypoxie" in prob:
            task_descriptions = [
                "Vitalzeichenkontrolle 2h dokumentieren",
                "Oberkörperhochlagerung, atemerleichternde Positionierung",
            ]
        elif "Schmerzen" in prob:
            task_descriptions = [
                "Schmerzskala alle 4h erheben",
            ]
        elif "Sturz" in prob or "Dekubitus" in prob:
            task_descriptions = [
                "Lagerung alle 2h dokumentieren",
                "Sturzrisiko einschätzen",
            ]
        elif "Verwirrtheit" in prob:
            task_descriptions = [
                "Orientierungshilfen (Kalender, Uhr, Angehörige) bereitstellen",
            ]
        elif "Hypotonie" in prob:
            task_descriptions = [
                "Vitalzeichenkontrolle alle 2h kontrollieren",
            ]
        else:
            task_descriptions = [
                "Vitalzeichenkontrolle nach Standard",
                "Schmerzen nach Standard nachfragen",
            ]

        for desc in task_descriptions:
            interval_hours = get_default_interval_hours(desc)
            next_due = datetime.now() + timedelta(hours=interval_hours)
            next_due_str = next_due.isoformat(timespec="minutes")

            # Clear old AI tasks
            cur.execute("""
                    DELETE FROM ai_tasks
                    WHERE patient_id = ?
                      AND completed = 0
                      AND description = ?;
                """, (patient_id, desc))

            cur.execute("""
                INSERT INTO ai_tasks (patient_id, description, due_time, completed)
                VALUES (?, ?, ?, 0);
            """, (patient_id, desc, next_due_str))

    generate_ai_alerts(conn, patient_id)
    conn.commit()


def compute_patient_alerts():
    """
    Looks at latest assessments + meds + allergies and returns a list of alerts
    for all patients.
    Each alert is a dict with:
      - patient_id
      - patient_name
      - patient_identifier
      - type
      - severity ('high' | 'medium' | 'low')
      - message
    """
    conn = get_connection()
    cur = conn.cursor()
    alerts = []

    # basic patient info
    cur.execute("""
        SELECT id, patient_identifier, name, diagnosis, allergies
        FROM patients;
    """)
    patients = cur.fetchall()

    for p in patients:
        pid = p["id"]

        # latest assessment
        cur.execute("""
            SELECT *
            FROM assessments
            WHERE patient_id = ?
            ORDER BY datetime(created_at) DESC
            LIMIT 1;
        """, (pid,))
        a = cur.fetchone()
        if not a:
            continue

        # 1) Sepsis-like constellation (very simplified!)
        temp = a["temperature"]
        hr = a["heart_rate"]
        rr = a["respiration_rate"]
        o2 = a["oxygen_sat"]

        if (
            temp is not None and hr is not None and rr is not None
            and (
                (temp >= 38.5 and hr >= 110 and rr >= 22) or
                (temp <= 36.0 and hr >= 110 and rr >= 22)
            )
        ):
            alerts.append({
                "patient_id": pid,
                "patient_name": p["name"],
                "patient_identifier": p["patient_identifier"],
                "type": "Sepsiswarnung",
                "severity": "high",
                "message": "Vitalzeichen-Konstellation mit möglicher Sepsis – Arzt informieren und Sepsis-Screening erwägen."
            })

        # 2) Hypoxie-Risiko
        if o2 is not None and o2 < 90:
            alerts.append({
                "patient_id": pid,
                "patient_name": p["name"],
                "patient_identifier": p["patient_identifier"],
                "type": "Hypoxie-Risiko",
                "severity": "high",
                "message": f"O₂-Sättigung {o2}% – Sauerstoffgabe / Arztkontakt prüfen."
            })

        # 3) Hypotonie
        sys = a["systolic_bp"]
        dia = a["diastolic_bp"]
        if sys is not None and sys < 90:
            alerts.append({
                "patient_id": pid,
                "patient_name": p["name"],
                "patient_identifier": p["patient_identifier"],
                "type": "Hypotonie",
                "severity": "medium",
                "message": f"RR {sys}/{dia or '–'} mmHg – Kreislaufsituation beobachten, ggf. Arzt informieren."
            })

        # 4) Pain escalation
        if a["pain"] is not None and a["pain"] >= 8:
            alerts.append({
                "patient_id": pid,
                "patient_name": p["name"],
                "patient_identifier": p["patient_identifier"],
                "type": "Starke Schmerzen",
                "severity": "medium",
                "message": f"Schmerzskala {a['pain']}/10 – Analgesie / ärztliche Rücksprache prüfen."
            })

        # 5) Very simple allergy–medication check
        allergies = (p["allergies"] or "").lower()
        if allergies:
            cur.execute("""
                SELECT name
                FROM medications
                WHERE patient_id = ?;
            """, (pid,))
            meds = [m["name"].lower() for m in cur.fetchall()]

            # simple keywords – you can expand later
            allergy_keywords = ["penicillin", "ass", "aspirin", "heparin"]
            for allergen in allergy_keywords:
                if allergen in allergies:
                    for med_name in meds:
                        if allergen in med_name:
                            alerts.append({
                                "patient_id": pid,
                                "patient_name": p["name"],
                                "patient_identifier": p["patient_identifier"],
                                "type": "Medikationswarnung",
                                "severity": "high",
                                "message": f"Allergie gegen '{allergen}' und Medikation '{med_name}' – Gabe kritisch prüfen!"
                            })
                            break  # avoid duplicates

        # 6) Infection / sepsis clues from notes (very simple)
        cur.execute("""
            SELECT note
            FROM nurse_notes
            WHERE patient_id = ?
            ORDER BY datetime(created_at) DESC
            LIMIT 5;
        """, (pid,))
        notes = [n["note"].lower() for n in cur.fetchall()]

        infection_keywords = ["fieber", "infekt", "purulent", "eitrig", "sepsis"]
        if any(any(k in note for k in infection_keywords) for note in notes):
            alerts.append({
                "patient_id": pid,
                "patient_name": p["name"],
                "patient_identifier": p["patient_identifier"],
                "type": "Infektionshinweis",
                "severity": "low",
                "message": "Dokumentation mit Infekt-/Sepsis-Hinweisen – Verlauf engmaschig beobachten."
            })

    conn.close()
    return alerts



# ---------------------------------------------------------
# FLOWSHEET – assessment + last 5 assessments
# ---------------------------------------------------------
@app.route("/patient/<int:patient_id>/flowsheet", methods=["GET", "POST"])
def flowsheet(patient_id):
    conn = get_connection()
    cur = conn.cursor()

    if request.method == "POST":
        def to_int(name):
            v = request.form.get(name)
            return int(v) if v not in (None, "",) else None

        def to_float(name):
            v = request.form.get(name)
            return float(v) if v not in (None, "",) else None

        def safe_range(value, min_v, max_v):
            if value is None:
                return None
            return max(min_v, min(max_v, value))

        # Vitals
        temperature = to_float("temperature")
        heart_rate = to_int("heart_rate")
        respiration_rate = to_int("respiration_rate")
        systolic_bp = to_int("systolic_bp")
        diastolic_bp = to_int("diastolic_bp")
        oxygen_sat = to_int("oxygen_sat")
        weight = to_int("weight")

        temperature = safe_range(temperature, 25, 45)
        heart_rate = safe_range(heart_rate, 0, 250)
        respiration_rate = safe_range(respiration_rate, 0, 80)
        systolic_bp = safe_range(systolic_bp, 40, 250)
        diastolic_bp = safe_range(diastolic_bp, 20, 150)
        oxygen_sat = safe_range(oxygen_sat, 50, 100)
        weight = safe_range(weight, 50, 100)


        # Scales
        pain = to_int("pain")
        mobility = to_int("mobility")
        edema = to_int("edema")
        confusion = to_int("confusion")
        nutrition = to_int("nutrition")

        # Body systems notes
        skin = request.form.get("skin") or ""
        cardiac = request.form.get("cardiac") or ""
        respiratory = request.form.get("respiratory") or ""
        endocrine = request.form.get("endocrine") or ""
        lymphatic = request.form.get("lymphatic") or ""
        musculoskeletal = request.form.get("musculoskeletal") or ""
        neuro = request.form.get("neuro") or ""
        gastro = request.form.get("gastro") or ""
        notes = request.form.get("other_notes") or ""

        cur.execute("""
            INSERT INTO assessments (
                patient_id, created_at, author,
                temperature, heart_rate, respiration_rate,
                systolic_bp, diastolic_bp, oxygen_sat,
                pain, weight, mobility, edema, confusion, nutrition,
                skin, cardiac, respiratory, endocrine, lymphatic,
                musculoskeletal, neuro, gastro, other_notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            patient_id, datetime.now().isoformat(timespec="minutes"),
            get_current_nurse(conn)["name"] if get_current_nurse(conn) else "unbekannt",
            temperature, heart_rate, respiration_rate,
            systolic_bp, diastolic_bp,oxygen_sat,
            pain, weight, mobility, edema, confusion, nutrition,
            skin, cardiac, respiratory, endocrine, lymphatic,
            musculoskeletal, neuro, gastro, notes
        ))

        cur.execute("""
               UPDATE ai_tasks
               SET completed = 1
               WHERE patient_id = ?
                 AND description LIKE '%Vitalzeichen%';
           """, (patient_id,))

        cur.execute("""
               UPDATE ai_tasks
               SET completed = 1
               WHERE patient_id = ?
                 AND description LIKE '%Schmerz%';
           """, (patient_id,))

        cur.execute("""
               UPDATE ai_tasks
               SET completed = 1
               WHERE patient_id = ?
                 AND description LIKE '%Sturzrisiko%';
           """, (patient_id,))

        cur.execute("""
              UPDATE ai_tasks
              SET completed = 1
              WHERE patient_id = ?
                AND description LIKE '%Orientierungshilfen%';
          """, (patient_id,))

        cur.execute("""
              UPDATE ai_tasks
              SET completed = 1
              WHERE patient_id = ?
                AND description LIKE '%Oberkörperhochlagerung%';
          """, (patient_id,))

        # 🔴 Write wichtige Beobachtungen auch als pflegerische Notiz
        # write important flowsheet notes into nurse_notes
        if notes and notes.strip():
            current_nurse = get_current_nurse(conn)
            author = current_nurse["name"] if current_nurse else "Flowsheet-Dokumentation"

            cur.execute("""
                INSERT INTO nurse_notes (patient_id, note, created_at, author)
                VALUES (?, ?, ?, ?);
            """, (
                patient_id,
                notes.strip(),
                datetime.now().strftime("%Y-%m-%d %H:%M"),
                author
            ))

        conn.commit()
        generate_priorities_and_tasks(conn, patient_id)
        conn.close()
        return redirect(url_for("flowsheet", patient_id=patient_id))

    # GET: patient + last 5 assessments
    cur.execute("SELECT * FROM patients WHERE id = ?;", (patient_id,))
    patient = cur.fetchone()

    cur.execute("""
            SELECT alert, severity, created_at
            FROM ai_alerts
            WHERE patient_id = ?
            ORDER BY datetime(created_at) DESC;
        """, (patient_id,))
    alerts = cur.fetchall()

    cur.execute("""
        SELECT *
        FROM assessments
        WHERE patient_id = ?
        ORDER BY datetime(created_at) DESC
        LIMIT 5;
    """, (patient_id,))
    recent_assessments = cur.fetchall()

    current_nurse = get_current_nurse(conn)
    conn.close()

    return render_template(
        "flowsheet.html",
        alerts=alerts,
        patient=patient,
        recent_assessments=recent_assessments,
        current_nurse=current_nurse,
    )


# ---------------------------------------------------------
# HOME PAGE – list of all patients
# ---------------------------------------------------------
@app.route("/")
def home():
    if "current_nurse_id" not in session:
        return redirect(url_for("select_nurse"))

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT 
            p.id,
            p.patient_identifier,
            p.name,
            p.gender,
            p.dob,
            p.diagnosis,
            p.allergies,
            p.code_status,
            p.admission_date,
            p.expected_discharge,
            p.primary_doctor,
            p.photo_filename,
            n.name AS bezugspflege_name
        FROM patients p
        LEFT JOIN nurses n ON p.bezugspflege_id = n.id
        ORDER BY p.room;
    """)
    patients = cur.fetchall()

    # Load AI priorities
    cur.execute("""
        SELECT patient_id, priority_rank, problem
        FROM patient_priorities
        ORDER BY patient_id, priority_rank;
    """)
    rows = cur.fetchall()
    priorities = {}
    for r in rows:
        priorities.setdefault(r["patient_id"], []).append(r["problem"])

    current_nurse = get_current_nurse(conn)
    conn.close()

    conn.close()
    return render_template("home.html",
                           patients=patients,
                           current_nurse=current_nurse,
                           priorities=priorities)


# ---------------------------------------------------------
# PATIENT DETAIL PAGE
# ---------------------------------------------------------
@app.route("/patient/<int:patient_id>", methods=["GET", "POST"])
def patient_detail(patient_id):
    conn = get_connection()
    cur = conn.cursor()


    cur.execute("""
        SELECT 
            p.id,
            p.patient_identifier,
            p.name,
            p.gender,
            p.dob,
            p.room,
            p.diagnosis,
            p.bezugspflege_id,
            p.allergies,
            p.code_status,
            p.admission_date,
            p.expected_discharge,
            p.primary_doctor,
            p.photo_filename,
            n.name AS bezugspflege_name
        FROM patients p
        LEFT JOIN nurses n ON p.bezugspflege_id = n.id
        WHERE p.id = ?;
    """, (patient_id,))
    patient = cur.fetchone()

    cur.execute("SELECT id, name FROM nurses ORDER BY name;")
    nurses = cur.fetchall()

    cur.execute("""
        SELECT id, name, dose, route, schedule
        FROM medications
        WHERE patient_id = ?
            AND (given IS NULL OR given = 0)
            AND (not_given IS NULL OR not_given = 0)
        ORDER BY name;
    """, (patient_id,))
    meds = cur.fetchall()

    cur.execute("""
        SELECT description, due_date, status, ordered_by, type
        FROM orders
        WHERE patient_id = ?
        ORDER BY due_date;
    """, (patient_id,))
    orders = cur.fetchall()

    cur.execute("""
        SELECT note, created_at, author
        FROM doctor_notes
        WHERE patient_id = ?
        ORDER BY created_at DESC;
    """, (patient_id,))
    doctor_notes = cur.fetchall()

    cur.execute("""
        SELECT note, created_at, author
        FROM nurse_notes
        WHERE patient_id = ?
        ORDER BY created_at DESC;
    """, (patient_id,))
    nurse_notes = cur.fetchall()

    cur.execute("""
        SELECT alert, severity, created_at
        FROM ai_alerts
        WHERE patient_id = ?
        ORDER BY datetime(created_at) DESC;
    """, (patient_id,))
    alerts = cur.fetchall()

    current_nurse = get_current_nurse(conn)
    conn.close()

    conn.close()

    return render_template(
        "patient_detail.html",
        alerts=alerts,
        current_nurse=current_nurse,
        patient=patient,
        nurses=nurses,
        meds=meds,
        orders=orders,
        doctor_notes=doctor_notes,
        nurse_notes=nurse_notes,
    )


# ---------------------------------------------------------
# TASKS VIEW
# ---------------------------------------------------------
@app.route("/tasks")
def tasks_view():
    patient_id = request.args.get("patient_id", type=int)

    conn = get_connection()
    cur = conn.cursor()

    # For the header (all vs single patient)
    if patient_id:
        cur.execute("""
            SELECT id, name, patient_identifier
            FROM patients
            WHERE id = ?;
        """, (patient_id,))
        patients = cur.fetchall()
        viewing_all_patients = False
        selected_patient = patients[0] if patients else None
    else:
        cur.execute("""
            SELECT id, name, patient_identifier
            FROM patients
            ORDER BY room;
        """)
        patients = cur.fetchall()

        viewing_all_patients = True
        selected_patient = None

    # ---------- AI tasks ----------
    if patient_id:
        cur.execute("""
            SELECT 
                t.id,
                t.patient_id,
                t.description,
                t.due_time,
                t.completed,
                p.name AS patient_name,
                p.patient_identifier AS patient_identifier
            FROM ai_tasks t
            JOIN patients p ON p.id = t.patient_id
            WHERE t.patient_id = ?
            ORDER BY datetime(t.due_time);
        """, (patient_id,))
    else:
        cur.execute("""
            SELECT 
                t.id,
                t.patient_id,
                t.description,
                t.due_time,
                t.completed,
                p.name AS patient_name,
                p.patient_identifier AS patient_identifier
            FROM ai_tasks t
            JOIN patients p ON p.id = t.patient_id
            ORDER BY p.room, datetime(t.due_time);
        """)
    ai_tasks = cur.fetchall()

    ai_tasks_open = [t for t in ai_tasks if not t["completed"]]
    ai_tasks_done = [t for t in ai_tasks if t["completed"]]

    # ---------- Orders ----------
    if patient_id:
        cur.execute("""
            SELECT o.*, p.name AS patient_name, p.patient_identifier
            FROM orders o
            JOIN patients p ON p.id = o.patient_id
            WHERE o.patient_id = ?
            ORDER BY o.due_date;
        """, (patient_id,))
    else:
        cur.execute("""
            SELECT o.*, p.name AS patient_name, p.patient_identifier
            FROM orders o
            JOIN patients p ON p.id = o.patient_id
            ORDER BY o.patient_id, o.due_date;
        """)
    orders = cur.fetchall()

    orders_open = [o for o in orders if (o["status"] or "").lower() != "erledigt"]
    orders_done = [o for o in orders if (o["status"] or "").lower() == "erledigt"]


    # ---------- Medications ----------
    # Ensure "given" column exists
    try:
        cur.execute("ALTER TABLE medications ADD COLUMN given INTEGER DEFAULT 0;"
                    "ALTER TABLE medications ADD COLUMN not_given INTEGER DEFAULT 0;"
                    )
        conn.commit()
    except sqlite3.OperationalError:
        pass

    if patient_id:
        cur.execute("""
            SELECT m.*, p.name AS patient_name, p.patient_identifier
            FROM medications m
            JOIN patients p ON p.id = m.patient_id
            WHERE m.patient_id = ?
            ORDER BY m.next_due;
        """, (patient_id,))
    else:
        cur.execute("""
            SELECT m.*, p.name AS patient_name, p.patient_identifier
            FROM medications m
            JOIN patients p ON p.id = m.patient_id
            ORDER BY m.patient_id, m.next_due;
        """)
    meds = cur.fetchall()

    # instead of only checking m["given"]
    meds_open = [m for m in meds if not (m["given"] or 0) and not (m["not_given"] or 0)]
    meds_done = [m for m in meds if (m["given"] or 0) or (m["not_given"] or 0)]

    # ---- Medication administration history (last 20 entries) ----
    cur.execute("""
        SELECT 
            ma.given_at,
            n.name AS nurse_name,
            m.name AS med_name,
            m.dose
        FROM med_administrations ma
        JOIN medications m ON m.id = ma.med_id
        LEFT JOIN nurses n ON n.id = ma.nurse_id
        WHERE ma.patient_id = ?
        ORDER BY datetime(ma.given_at) DESC
        LIMIT 20;
    """, (patient_id or 0,))
    med_history = cur.fetchall()

    cur.execute("""
               SELECT alert, severity, created_at
               FROM ai_alerts
               WHERE patient_id = ?
               ORDER BY datetime(created_at) DESC;
           """, (patient_id,))
    alerts = cur.fetchall()

    current_nurse = get_current_nurse(conn)
    conn.close()

    conn.close()

    return render_template(
        "tasks.html",
        alerts=alerts,
        current_nurse=current_nurse,
        patients=patients,
        viewing_all_patients=viewing_all_patients,
        selected_patient=selected_patient,
        ai_tasks_open=ai_tasks_open,
        ai_tasks_done=ai_tasks_done,
        orders_open=orders_open,
        orders_done=orders_done,
        meds_open=meds_open,
        meds_done=meds_done,
        med_history=med_history
        # if you already pass alerts, keep that here too
    )



@app.route("/labs", methods=["GET", "POST"])
def labs_view():
    patient_id = request.args.get("patient_id", type=int)

    conn = get_connection()
    cur = conn.cursor()

    # ----- Handle new lab order (POST) -----
    if request.method == "POST":
        # patient_id can come from hidden field or dropdown
        form_pid = request.form.get("patient_id", type=int)
        lab_name = (request.form.get("lab_name") or "").strip()
        priority = request.form.get("priority") or "Routine"

        # Decide which patient to use
        effective_pid = form_pid or patient_id

        if effective_pid and lab_name:
            cur.execute("""
                INSERT INTO lab_orders (patient_id, name, priority, status, ordered_at)
                VALUES (?, ?, ?, ?, ?);
            """, (
                effective_pid,
                lab_name,
                priority,
                "Ausstehend",
                datetime.now().isoformat(timespec="minutes"),
            ))
            conn.commit()

            # Redirect to patient-specific labs view after ordering
            return redirect(url_for("labs_view", patient_id=effective_pid))

    # ----- For GET (or after POST handling), load context -----

    # 1) Patient vs all patients
    if patient_id:
        cur.execute("SELECT * FROM patients WHERE id = ?;", (patient_id,))
        patient = cur.fetchone()
        patients = [patient] if patient else []
    else:
        patient = None
        cur.execute("""
            SELECT id, name, patient_identifier
            FROM patients
            ORDER BY room;
        """)
        patients = cur.fetchall()

    # 2) Pending labs (status Ausstehend / Offen)
    if patient_id:
        cur.execute("""
            SELECT lo.*, p.name AS patient_name, p.patient_identifier
            FROM lab_orders lo
            JOIN patients p ON p.id = lo.patient_id
            WHERE lo.patient_id = ?
              AND lo.status IN ('Ausstehend', 'Offen')
            ORDER BY lo.ordered_at DESC;
        """, (patient_id,))
    else:
        cur.execute("""
            SELECT lo.*, p.name AS patient_name, p.patient_identifier
            FROM lab_orders lo
            JOIN patients p ON p.id = lo.patient_id
            WHERE lo.status IN ('Ausstehend', 'Offen')
            ORDER BY lo.ordered_at DESC;
        """)
    pending_labs = cur.fetchall()

    # 3) Recent labs (last 5 days)
    five_days_ago = (datetime.now() - timedelta(days=5)).isoformat(timespec="minutes")

    if patient_id:
        cur.execute("""
            SELECT lr.*, p.name AS patient_name, p.patient_identifier
            FROM lab_results lr
            JOIN patients p ON p.id = lr.patient_id
            WHERE lr.patient_id = ?
              AND lr.result_datetime >= ?
            ORDER BY lr.result_datetime DESC;
        """, (patient_id, five_days_ago))
    else:
        cur.execute("""
            SELECT lr.*, p.name AS patient_name, p.patient_identifier
            FROM lab_results lr
            JOIN patients p ON p.id = lr.patient_id
            WHERE lr.result_datetime >= ?
            ORDER BY lr.result_datetime DESC;
        """, (five_days_ago,))
    recent_labs = cur.fetchall()

    cur.execute("""
            SELECT alert, severity, created_at
            FROM ai_alerts
            WHERE patient_id = ?
            ORDER BY datetime(created_at) DESC;
        """, (patient_id,))
    alerts = cur.fetchall()

    current_nurse = get_current_nurse(conn)
    conn.close()

    conn.close()
    return render_template(
        "labs.html",
        alerts=alerts,
        current_nurse=current_nurse,
        patient=patient,
        patients=patients,
        patient_id=patient_id,
        recent_labs=recent_labs,
        pending_labs=pending_labs,
    )


@app.get("/api/alerts")
def api_alerts():
    alerts = compute_patient_alerts()
    return jsonify(alerts)


# ---------------------------------------------------------
# PHOTO UPLOAD
# ---------------------------------------------------------
@app.post("/patient/<int:patient_id>/upload_photo")
def upload_photo(patient_id):
    if "photo" not in request.files:
        return redirect(url_for("patient_detail", patient_id=patient_id))

    file = request.files["photo"]
    if file.filename == "":
        return redirect(url_for("patient_detail", patient_id=patient_id))

    if file and allowed_file(file.filename):
        filename = secure_filename(f"patient_{patient_id}_" + file.filename)
        filepath = UPLOAD_FOLDER / filename
        file.save(filepath)

        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE patients SET photo_filename = ? WHERE id = ?;",
            (filename, patient_id),
        )
        conn.commit()
        conn.close()

    return redirect(url_for("patient_detail", patient_id=patient_id))


# ---------------------------------------------------------
# TOGGLE AI TASK COMPLETION (+ create next occurrence)
# ---------------------------------------------------------
@app.post("/tasks/<int:task_id>/toggle")
def toggle_task(task_id):
    """
    Toggle completion for:
      - AI tasks  (task_type=ai)
      - Orders    (task_type=order)
      - Meds      (task_type=med, with 'gegeben' / 'nicht gegeben')
    """

    task_type = request.args.get("task_type", "ai")
    conn = get_connection()
    cur = conn.cursor()

    try:
        # ---------------- AI TASKS (with recurrence) ----------------
        if task_type == "ai":
            cur.execute("""
                SELECT id, patient_id, description, due_time, completed
                FROM ai_tasks
                WHERE id = ?;
            """, (task_id,))
            task = cur.fetchone()

            if task:
                patient_id   = task["patient_id"]
                description  = task["description"]
                due_time_str = task["due_time"]
                completed    = task["completed"]

                # parse base due
                if due_time_str:
                    try:
                        base_due = datetime.fromisoformat(due_time_str)
                    except ValueError:
                        base_due = datetime.now()
                else:
                    base_due = datetime.now()

                if not completed:
                    # mark done
                    cur.execute("""
                        UPDATE ai_tasks
                        SET completed = 1
                        WHERE id = ?;
                    """, (task_id,))

                    # create next occurrence
                    interval_hours = get_default_interval_hours(description)
                    next_due = base_due + timedelta(hours=interval_hours)
                    next_due_str = next_due.isoformat(timespec="minutes")

                    cur.execute("""
                        INSERT INTO ai_tasks (patient_id, description, due_time, completed)
                        VALUES (?, ?, ?, 0);
                    """, (patient_id, description, next_due_str))
                else:
                    # uncheck & remove future copies
                    cur.execute("""
                        UPDATE ai_tasks
                        SET completed = 0
                        WHERE id = ?;
                    """, (task_id,))

                    cur.execute("""
                        DELETE FROM ai_tasks
                        WHERE patient_id = ?
                          AND description = ?
                          AND completed = 0
                          AND datetime(due_time) > datetime(?);
                    """, (
                        patient_id,
                        description,
                        due_time_str or datetime.now().isoformat(timespec="minutes"),
                    ))

        # ---------------- ORDERS (simple status toggle) ----------------
        elif task_type == "order":
            cur.execute("""
                SELECT id, status
                FROM orders
                WHERE id = ?;
            """, (task_id,))
            order = cur.fetchone()

            if order:
                current = (order["status"] or "").lower()
                new_status = "erledigt" if current != "erledigt" else "offen"
                cur.execute("""
                    UPDATE orders
                    SET status = ?
                    WHERE id = ?;
                """, (new_status, task_id,))

        # ---- Medications ----
        # ---------------- MEDICATIONS (gegeben / nicht gegeben + schedule next dose) ----------------
        elif task_type == "med":
            # Was clicked? (default = "given" if nothing provided)
            action = request.form.get("action", "given")

            # get current med row
            cur.execute("""
                SELECT id, patient_id, name, dose, route, schedule,
                       next_due, given, not_given, last_given_by, last_given_at
                FROM medications
                WHERE id = ?;
            """, (task_id,))
            med = cur.fetchone()

            if not med:
                conn.close()
                return redirect(request.referrer or url_for("tasks_view"))

            current_nurse = get_current_nurse(conn)
            given = med["given"] or 0
            not_given = med["not_given"] or 0
            schedule = med["schedule"] or ""
            next_due_str = med["next_due"]

            # ---- parse base time (for calculating the next due) ----
            base_due = datetime.now()
            if next_due_str:
                try:
                    # full timestamp "YYYY-MM-DD HH:MM" or ISO
                    if ("T" in next_due_str) or (" " in next_due_str and ":" in next_due_str):
                        cleaned = next_due_str.replace(" ", "T")
                        base_due = datetime.fromisoformat(cleaned)
                    else:
                        # e.g. "08:00" → today 08:00
                        hours, minutes = map(int, next_due_str.split(":"))
                        from datetime import time as dt_time
                        base_due = datetime.combine(
                            datetime.today().date(),
                            dt_time(hour=hours, minute=minutes)
                        )
                except ValueError:
                    base_due = datetime.now()

            interval_hours = get_med_interval_hours(schedule)
            base_due_str_for_delete = next_due_str or datetime.now().isoformat(timespec="minutes")

            # --------- ACTION: GEGEBEN ---------
            if action == "given":
                if not given:
                    # mark THIS dose as given
                    last_by = current_nurse["name"] if current_nurse else None
                    last_at = datetime.now().strftime("%Y-%m-%d %H:%M")

                    cur.execute("""
                        UPDATE medications
                        SET given = 1,
                            not_given = 0,
                            last_given_by = ?,
                            last_given_at = ?
                        WHERE id = ?;
                    """, (last_by, last_at, task_id))

                    # create NEXT dose as new, open row
                    new_next_due = (base_due + timedelta(hours=interval_hours)).isoformat(timespec="minutes")

                    cur.execute("""
                        INSERT INTO medications (patient_id, name, dose, route, schedule, next_due)
                        VALUES (?, ?, ?, ?, ?, ?);
                    """, (
                        med["patient_id"],
                        med["name"],
                        med["dose"],
                        med["route"],
                        schedule,
                        new_next_due,
                    ))
                else:
                    # UNDO "gegeben": set back to offene Dosis & remove the future open copy
                    cur.execute("""
                        UPDATE medications
                        SET given = 0,
                            not_given = 0,
                            last_given_by = NULL,
                            last_given_at = NULL
                        WHERE id = ?;
                    """, (task_id,))

                    cur.execute("""
                        DELETE FROM medications
                        WHERE patient_id = ?
                          AND name = ?
                          AND schedule = ?
                          AND given = 0
                          AND not_given = 0
                          AND datetime(next_due) > datetime(?);
                    """, (
                        med["patient_id"],
                        med["name"],
                        schedule,
                        base_due_str_for_delete,
                    ))

            # --------- ACTION: NICHT GEGEBEN ---------
            elif action == "not_given":
                if not not_given:
                    # mark THIS dose as not given (but still documented)
                    last_by = current_nurse["name"] if current_nurse else None
                    last_at = datetime.now().strftime("%Y-%m-%d %H:%M")

                    cur.execute("""
                        UPDATE medications
                        SET not_given = 1,
                            given = 0,
                            last_given_by = ?,
                            last_given_at = ?
                        WHERE id = ?;
                    """, (last_by, last_at, task_id))

                    # plan NEXT dose anyway
                    new_next_due = (base_due + timedelta(hours=interval_hours)).isoformat(timespec="minutes")

                    cur.execute("""
                        INSERT INTO medications (patient_id, name, dose, route, schedule, next_due)
                        VALUES (?, ?, ?, ?, ?, ?);
                    """, (
                        med["patient_id"],
                        med["name"],
                        med["dose"],
                        med["route"],
                        schedule,
                        new_next_due,
                    ))
                else:
                    # UNDO "nicht gegeben": set back to offene Dosis & remove the future open copy
                    cur.execute("""
                        UPDATE medications
                        SET not_given = 0,
                            given = 0,
                            last_given_by = NULL,
                            last_given_at = NULL
                        WHERE id = ?;
                    """, (task_id,))

                    cur.execute("""
                        DELETE FROM medications
                        WHERE patient_id = ?
                          AND name = ?
                          AND schedule = ?
                          AND given = 0
                          AND not_given = 0
                          AND datetime(next_due) > datetime(?);
                    """, (
                        med["patient_id"],
                        med["name"],
                        schedule,
                        base_due_str_for_delete,
                    ))

        conn.commit()
        return redirect(request.referrer or url_for("tasks_view"))


    finally:
        conn.close()


@app.post("/patient/<int:patient_id>/delete_photo")
def delete_photo(patient_id):
    conn = get_connection()
    cur = conn.cursor()

    # Get the current filename
    cur.execute("SELECT photo_filename FROM patients WHERE id = ?;", (patient_id,))
    row = cur.fetchone()
    if row and row["photo_filename"]:
        filepath = UPLOAD_FOLDER / row["photo_filename"]
        if filepath.exists():
            try:
                filepath.unlink()   # delete the file
            except (Exception,):
                pass  # Avoid crash if file is locked or missing

    # Reset DB entry
    cur.execute(
        "UPDATE patients SET photo_filename = NULL WHERE id = ?;",
        (patient_id,)
    )
    conn.commit()
    conn.close()

    return redirect(url_for("patient_detail", patient_id=patient_id))

@app.route("/select_nurse", methods=["GET", "POST"])
def select_nurse():
    conn = get_connection()
    cur = conn.cursor()

    if request.method == "POST":
        nurse_id = request.form.get("nurse_id")
        if nurse_id:
            session["current_nurse_id"] = int(nurse_id)
        conn.close()
        return redirect(url_for("home"))

    # GET → show list of nurses
    cur.execute("SELECT id, name FROM nurses ORDER BY name;")
    nurses = cur.fetchall()
    current_nurse = get_current_nurse(conn)
    conn.close()

    return render_template("select_nurse.html",
                           nurses=nurses,
                           current_nurse=current_nurse)

@app.get("/logout")
def logout():
    # Remove the nurse from the session
    session.pop("current_nurse_id", None)

    # Redirect back to login page
    return redirect(url_for("select_nurse"))


@app.route("/voice-doc", methods=["GET", "POST"])
def voice_doc():
    if "current_nurse_id" not in session:
        return redirect(url_for("select_nurse"))

    conn = get_connection()
    cur = conn.cursor()
    current_nurse = get_current_nurse(conn)
    author = current_nurse["name"] if current_nurse else "Sprachdokumentation"

    if request.method == "POST":
        patient_identifier = request.form.get("patient_identifier")
        spoken_text = request.form.get("spoken_text", "")

        # find patient
        cur.execute(
            "SELECT id FROM patients WHERE patient_identifier = ?;",
            (patient_identifier,)
        )
        patient = cur.fetchone()

        if patient:
            task_desc = map_spoken_phrase_to_task(spoken_text)

            if task_desc:
                # insert as completed task
                cur.execute("""
                    INSERT INTO ai_tasks (patient_id, description, due_time, completed)
                    VALUES (?, ?, ?, 1);
                """, (
                    patient["id"],
                    task_desc,
                    datetime.now().isoformat(timespec="minutes"),
                ))

            observation = request.form.get("observation_text")
            if observation and observation.strip():
                cur.execute("""
                    INSERT INTO nurse_notes (patient_id, note, created_at, author)
                    VALUES (?, ?, ?, ?);
                """, (
                    patient["id"],
                    observation.strip(),
                    datetime.now().strftime("%Y-%m-%d %H:%M"),
                    author,
                ))

                conn.commit()

        conn.close()
        return redirect(url_for("tasks_view", patient_id=patient["id"]))

    conn.close()
    return render_template("voice_doc.html")




# ---------------------------------------------------------
# RUN SERVER
# ---------------------------------------------------------
import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)



