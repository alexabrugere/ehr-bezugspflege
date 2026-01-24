import sqlite3
from pathlib import Path
from datetime import datetime, timedelta

DB_PATH = Path("ehr_bezugspflege.db")

# Delete old DB if it exists (recreates everything fresh)
if DB_PATH.exists():
    DB_PATH.unlink()

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

# ----------------------------
# Helpers for dynamic dates
# ----------------------------
def now_dt():
    return datetime.now()

def now_local_iso_minutes():
    # if you already have now_local() in app.py, here we keep it simple:
    return datetime.now().isoformat(timespec="minutes")

def iso_minutes(dt: datetime) -> str:
    return dt.replace(second=0, microsecond=0).isoformat(timespec="minutes")

def date_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")

def dt_days_ago(days: int) -> datetime:
    return now_dt() - timedelta(days=days)

def dt_days_from_now(days: int) -> datetime:
    return now_dt() + timedelta(days=days)

def today_at(hour: int, minute: int = 0) -> datetime:
    n = now_dt()
    return n.replace(hour=hour, minute=minute, second=0, microsecond=0)

def next_time_today_or_tomorrow(hour: int, minute: int = 0) -> datetime:
    """If the time has already passed today, return tomorrow at that time."""
    t = today_at(hour, minute)
    if t <= now_dt():
        t = t + timedelta(days=1)
    return t

# ----------------------------
# Tables (same schema you use)
# ----------------------------

# ---- Nurses ----
cur.execute("""
CREATE TABLE nurses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
);
""")

# ---- Patients ----
cur.execute("""
CREATE TABLE patients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_identifier TEXT NOT NULL,
    name TEXT NOT NULL,
    gender TEXT,
    dob TEXT,
    room TEXT,
    diagnosis TEXT,
    bezugspflege_id INTEGER,
    allergies TEXT,
    code_status TEXT,
    admission_date TEXT,
    expected_discharge TEXT,
    primary_doctor TEXT,
    photo_filename TEXT,
    FOREIGN KEY (bezugspflege_id) REFERENCES nurses(id)
);
""")

# ---- Medications ----
cur.execute("""
CREATE TABLE medications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    dose TEXT,
    route TEXT,
    schedule TEXT,
    next_due TEXT,
    due_time TEXT,
    completed INTEGER DEFAULT 0,
    given INTEGER DEFAULT 0,
    not_given INTEGER DEFAULT 0,
    last_given_by TEXT,
    last_given_at TEXT,
    FOREIGN KEY (patient_id) REFERENCES patients(id)
);
""")

# ---- Med administrations (history) ----
cur.execute("""
CREATE TABLE med_administrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL,
    med_id INTEGER NOT NULL,
    nurse_id INTEGER,
    given_at TEXT,
    FOREIGN KEY (patient_id) REFERENCES patients(id),
    FOREIGN KEY (med_id) REFERENCES medications(id),
    FOREIGN KEY (nurse_id) REFERENCES nurses(id)
);
""")

# ---- Orders / tasks ----
cur.execute("""
CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL,
    description TEXT NOT NULL,
    due_date TEXT,
    due_time TEXT,
    status TEXT,
    ordered_by TEXT,
    type TEXT,
    completed INTEGER DEFAULT 0,
    FOREIGN KEY (patient_id) REFERENCES patients(id)
);
""")

# ---- Doctor notes ----
cur.execute("""
CREATE TABLE doctor_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT,
    author TEXT,
    FOREIGN KEY (patient_id) REFERENCES patients(id)
);
""")

# ---- Nurse notes ----
cur.execute("""
CREATE TABLE nurse_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT,
    author TEXT,
    FOREIGN KEY (patient_id) REFERENCES patients(id)
);
""")

# ---- Labs ----
cur.execute("""
CREATE TABLE labs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL,
    test_name TEXT NOT NULL,
    result_value TEXT,
    unit TEXT,
    reference_range TEXT,
    status TEXT NOT NULL,
    result_datetime TEXT,
    ordered_datetime TEXT,
    comment TEXT,
    FOREIGN KEY (patient_id) REFERENCES patients(id)
);
""")

# ---- Assessments (Flowsheet) ----
cur.execute("""
CREATE TABLE assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL,
    created_at TEXT,
    author TEXT,

    temperature REAL,
    heart_rate INTEGER,
    respiration_rate INTEGER,
    systolic_bp INTEGER,
    diastolic_bp INTEGER,
    oxygen_sat INTEGER,
    weight INTEGER,

    pain INTEGER,
    mobility INTEGER,
    edema INTEGER,
    confusion INTEGER,
    nutrition INTEGER,

    skin TEXT,
    cardiac TEXT,
    respiratory TEXT,
    endocrine TEXT,
    lymphatic TEXT,
    musculoskeletal TEXT,
    neuro TEXT,
    gastro TEXT,
    other_notes TEXT,

    FOREIGN KEY (patient_id) REFERENCES patients(id)
);
""")

# ---- AI priorities ----
cur.execute("""
CREATE TABLE patient_priorities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL,
    priority_rank INTEGER NOT NULL,
    problem TEXT NOT NULL,
    FOREIGN KEY (patient_id) REFERENCES patients(id)
);
""")

# ---- AI tasks ----
cur.execute("""
CREATE TABLE ai_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL,
    description TEXT NOT NULL,
    due_time TEXT,
    completed INTEGER DEFAULT 0,
    FOREIGN KEY (patient_id) REFERENCES patients(id)
);
""")

# ---- AI alerts ----
cur.execute("""
CREATE TABLE IF NOT EXISTS ai_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL,
    alert TEXT NOT NULL,
    severity TEXT,
    created_at TEXT,
    FOREIGN KEY (patient_id) REFERENCES patients(id)
);
""")

# ---- Lab results ----
cur.execute("""
CREATE TABLE IF NOT EXISTS lab_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    result_value TEXT,
    result_flag TEXT,
    result_datetime TEXT,
    FOREIGN KEY (patient_id) REFERENCES patients(id)
);
""")

# ---- Lab orders ----
cur.execute("""
CREATE TABLE IF NOT EXISTS lab_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    priority TEXT,
    status TEXT,
    ordered_at TEXT,
    FOREIGN KEY (patient_id) REFERENCES patients(id)
);
""")

# ----------------------------
# Seed data
# ----------------------------

# Nurses (keep 3)
cur.executemany(
    "INSERT INTO nurses (name) VALUES (?);",
    [
        ("Mia Gross",),
        ("Karl Loch",),
        ("Lisa Hans",),
    ]
)

# Patients (10) — short full names (not abbreviated)
# Admissions are relative to now: 2–5 days ago
# Discharge is relative to now: 1–6 days from now
patients = [
    ("P-100001", "Maria Braun",   "F", "1980-03-14", "1A", "Herzinsuffizienz", 3,
     "Penicillin, Latex", "Keine Reha/Intub", dt_days_ago(3), dt_days_from_now(3), "Dr. Keller"),
    ("P-100002", "Sarah Schulz",     "F", "1972-08-09", "1B", "NSTEMI", 3,
     "Keine", "nicht festgelegt", dt_days_ago(2), dt_days_from_now(2), "Dr. Roth"),
    ("P-100003", "Rolf Schwarz",    "M", "1968-11-22", "2A", "Vorhofflimmern", 3,
     "Jod", "nicht festgelegt", dt_days_ago(5), dt_days_from_now(2), "Dr. Klein"),
    ("P-100004", "Jan Fischer",   "M", "1959-05-18", "2B", "Herzinsuffizienz", 3,
     "Keine", "Alles gewünscht", dt_days_ago(4), dt_days_from_now(4), "Dr. Keller"),
    ("P-100005", "Sofia Wagner",     "F", "1977-01-09", "3A", "KHK / Angina pectoris", 3,
     "ASS", "nicht festgelegt", dt_days_ago(3), dt_days_from_now(1), "Dr. Roth"),
    ("P-100006", "Nina Becker",    "F", "1983-09-30", "3B", "Hypertensive Krise", 3,
     "Keine", "nicht festgelegt", dt_days_ago(2), dt_days_from_now(2), "Dr. Klein"),
    ("P-100007", "Markus Vogt",   "M", "1990-02-12", "4A", "V.a. Myokarditis", 3,
     "Penicillin", "nicht festgelegt", dt_days_ago(2), dt_days_from_now(5), "Dr. Keller"),
    ("P-100008", "Paul Fuchs",     "M", "1948-06-03", "4B", "Pneumonie / Herzinsuffizienz", 3,
     "Keine", "Keine Reha/Intub", dt_days_ago(5), dt_days_from_now(3), "Dr. Roth"),
    ("P-100009", "Helena Wolf",    "F", "1961-12-27", "5A", "Vorhofflimmern", 3,
     "Heparin", "nicht festgelegt", dt_days_ago(3), dt_days_from_now(4), "Dr. Klein"),
    ("P-100010", "Renate Webb",    "F", "1970-07-15", "5B", "Bradykardie / Synkope", 3,
     "Keine", "nicht festgelegt", dt_days_ago(4), dt_days_from_now(2), "Dr. Keller"),
]

cur.executemany("""
INSERT INTO patients
(patient_identifier, name, gender, dob, room, diagnosis, bezugspflege_id,
 allergies, code_status, admission_date, expected_discharge, primary_doctor)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
""", [
    (pid, name, gender, dob, room, dx, nurse_id, allergies, code, date_str(adm), date_str(dis), doc)
    for (pid, name, gender, dob, room, dx, nurse_id, allergies, code, adm, dis, doc) in patients
])

# Medications:
# Use next_due relative to now so they show as upcoming (morning/afternoon/evening)
# NOTE: you can tune times if you want.
med_seed = []

def add_med(patient_id: int, name: str, dose: str, route: str, schedule: str, due_dt: datetime):
    med_seed.append((patient_id, name, dose, route, schedule, iso_minutes(due_dt), iso_minutes(due_dt)))

# Define some common due times
due_morning = next_time_today_or_tomorrow(8, 0)
due_noon    = next_time_today_or_tomorrow(12, 0)
due_afternoon = next_time_today_or_tomorrow(14, 0)
due_evening = next_time_today_or_tomorrow(20, 0)


# We'll just add patient 2 meds manually to avoid weirdness
med_seed = [m for m in med_seed if True]  # keep list

# Instead: add a helper for arbitrary offsets:
def add_med_in_hours(patient_id, name, dose, route, schedule, hours):
    d = now_dt() + timedelta(hours=hours)
    med_seed.append((patient_id, name, dose, route, schedule, iso_minutes(d), iso_minutes(d)))

# Patient 1
add_med(1, "Bisoprolol", "2.5 mg", "p.o.", "1x morgens", due_morning)
add_med(1, "Furosemid", "20 mg", "i.v.", "2x täglich", due_morning)
add_med(1, "Pantoprazol", "40 mg", "p.o.", "1x morgens", due_morning)

# Patient 2
add_med(2, "ASS", "100 mg", "p.o.", "1x morgens", due_morning)
add_med(2, "Heparin", "5000 IE", "s.c.", "alle 8h", iso_minutes(now_dt() + timedelta(hours=2)) and (now_dt()+timedelta(hours=2)))

# Patient 3
add_med(3, "Metoprolol", "25 mg", "p.o.", "2x täglich", due_morning)
add_med(3, "Apixaban", "5 mg", "p.o.", "2x täglich", due_evening)

# Patient 4
add_med(4, "Ramipril", "2.5 mg", "p.o.", "1x morgens", due_morning)
add_med(4, "Spironolacton", "25 mg", "p.o.", "1x morgens", due_morning)
add_med(4, "Furosemid", "40 mg", "i.v.", "2x täglich", due_afternoon)

# Patient 5
add_med(5, "ASS", "100 mg", "p.o.", "1x morgens", due_morning)
add_med(5, "Nitroglycerin", "0.4 mg", "s.l.", "bei Bedarf", now_dt() + timedelta(hours=1))

# Patient 6
add_med(6, "Urapidil", "10 mg", "i.v.", "bei Bedarf", now_dt() + timedelta(hours=1))
add_med(6, "Amlodipin", "5 mg", "p.o.", "1x morgens", due_morning)

# Patient 7
add_med(7, "Ibuprofen", "400 mg", "p.o.", "3x täglich", due_noon)
add_med(7, "Pantoprazol", "40 mg", "p.o.", "1x morgens", due_morning)

# Patient 8
add_med(8, "Piperacillin/Tazobactam", "4.5 g", "i.v.", "alle 8h", now_dt() + timedelta(hours=1))
add_med(8, "Furosemid", "20 mg", "i.v.", "2x täglich", due_morning)

# Patient 9
add_med(9, "Rivaroxaban", "20 mg", "p.o.", "1x abends", due_evening)
add_med(9, "Metoprolol", "25 mg", "p.o.", "2x täglich", due_morning)

# Patient 10
add_med(10, "Atropin", "0.5 mg", "i.v.", "bei Bedarf", now_dt() + timedelta(hours=1))
add_med(10, "Bisoprolol", "1.25 mg", "p.o.", "1x morgens", due_morning)

# Insert meds
cur.executemany("""
INSERT INTO medications (patient_id, name, dose, route, schedule, next_due, due_time)
VALUES (?, ?, ?, ?, ?, ?, ?);
""", [
    (pid, name, dose, route, sched, iso_minutes(nd), iso_minutes(nd))
    if isinstance(nd, datetime) else (pid, name, dose, route, sched, nd, nd)
    for (pid, name, dose, route, sched, nd, dtv) in med_seed
])

# Orders/tasks (dynamic)
orders = []
def add_order(patient_id, desc, days_from_now, hour, minute, status, ordered_by, typ):
    due_dt = next_time_today_or_tomorrow(hour, minute) + timedelta(days=days_from_now)
    orders.append((
        patient_id,
        desc,
        date_str(due_dt),
        iso_minutes(due_dt),
        status,
        ordered_by,
        typ
    ))

for pid in range(1, 11):
    add_order(pid, "Vitalzeichenkontrolle nach Standard", 0, 8, 0, "offen", "Station", "Pflegeaufgabe")
    add_order(pid, "Bilanzierung 24h", 0, 20, 0, "offen", "Station", "Pflegeaufgabe")
    add_order(pid, "Gewichtskontrolle täglich", 0, 8, 0, "offen", "Station", "Anordnung")

# Add a couple special orders
add_order(1, "Blutbild - Routine", 0, 10, 0, "geplant", "Dr. Roth", "Diagnostik")
add_order(2, "Blutbild - Routine", 0, 10, 0, "geplant", "Dr. Roth", "Diagnostik")
add_order(3, "Blutbild - Routine", 0, 10, 0, "geplant", "Dr. Roth", "Diagnostik")
add_order(4, "Blutbild - Routine", 0, 10, 0, "geplant", "Dr. Roth", "Diagnostik")
add_order(5, "Blutbild - Routine", 0, 10, 0, "geplant", "Dr. Roth", "Diagnostik")
add_order(6, "Blutbild - Routine", 0, 10, 0, "geplant", "Dr. Roth", "Diagnostik")
add_order(7, "Blutbild - Routine", 0, 10, 0, "geplant", "Dr. Roth", "Diagnostik")
add_order(8, "Blutbild - Routine", 0, 10, 0, "geplant", "Dr. Roth", "Diagnostik")
add_order(9, "Blutbild - Routined", 0, 10, 0, "geplant", "Dr. Roth", "Diagnostik")
add_order(10, "Blutbild - Routine", 0, 10, 0, "geplant", "Dr. Roth", "Diagnostik")

cur.executemany("""
INSERT INTO orders (patient_id, description, due_date, due_time, status, ordered_by, type)
VALUES (?, ?, ?, ?, ?, ?, ?);
""", orders)

# Notes (dynamic timestamps)
doc_notes = [
    (1, "Klinisch kompensiert, Diuretika angepasst.", iso_minutes(now_dt() - timedelta(hours=6)), "Dr. Keller"),
    (2, "Post-NSTEMI, Verlauf stabil, Echo geplant.", iso_minutes(now_dt() - timedelta(hours=7)), "Dr. Roth"),
    (3, "Vorhofflimmern, Frequenzkontrolle begonnen.", iso_minutes(now_dt() - timedelta(hours=10)), "Dr. Klein"),
    (8, "Pneumonie, Antibiotika laufen, O₂ Bedarf.", iso_minutes(now_dt() - timedelta(hours=5)), "Dr. Roth"),
]
cur.executemany("""
INSERT INTO doctor_notes (patient_id, note, created_at, author)
VALUES (?, ?, ?, ?);
""", doc_notes)

nurse_notes = [
    (1, "Patient kurzatmig bei Belastung, O₂ 2l.", iso_minutes(now_dt() - timedelta(hours=5)), "Anna Müller"),
    (1, "Bilanz begonnen, Angehörige aufgeklärt.", iso_minutes(now_dt() - timedelta(hours=2)), "Anna Müller"),
    (2, "Kurzatmigkeit bei Bewegung.", iso_minutes(now_dt() - timedelta(hours=4)), "Jonas Weber"),
    (4, "Ödeme an Unterschenkeln, Haut gespannt.", iso_minutes(now_dt() - timedelta(hours=3)), "Lisa Schmidt"),
    (8, "Husten produktiv, Sättigung schwankt.", iso_minutes(now_dt() - timedelta(hours=1)), "Jonas Weber"),
]
cur.executemany("""
INSERT INTO nurse_notes (patient_id, note, created_at, author)
VALUES (?, ?, ?, ?);
""", nurse_notes)

cur.execute("SELECT id FROM patients;")
patient_ids = [r[0] for r in cur.fetchall()]

due = (datetime.now() + timedelta(hours=0)).isoformat(timespec="minutes")

standard_tasks = [
    "Vitalzeichen nach Standard",
    "Schmerzen täglich nachfragen",
    "Gewicht täglich messen",
]

for pid in patient_ids:
    for desc in standard_tasks:
        cur.execute("""
            INSERT INTO ai_tasks (patient_id, description, due_time, completed)
            VALUES (?, ?, ?, 0);
        """, (pid, desc, due))

conn.commit()
conn.close()

print("✅ Neue Datenbank mit 10 Patienten + dynamischen Datumswerten wurde erstellt.")



