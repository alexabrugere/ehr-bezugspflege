import sqlite3
from pathlib import Path

DB_PATH = Path("ehr_bezugspflege.db")

# Delete old DB if it exists (recreates everything fresh)
if DB_PATH.exists():
    DB_PATH.unlink()

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

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
# NOTE: due_time, completed, given, last_given_by, last_given_at
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



# ---- Med administrations (history of who gave which med when) ----
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
# NOTE: due_time + completed are already here
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

# ---- Labs (you can keep this even if lab_orders/lab_results also exist) ----
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

    -- Vital signs
    temperature REAL,
    heart_rate INTEGER,
    respiration_rate INTEGER,
    systolic_bp INTEGER,
    diastolic_bp INTEGER,
    oxygen_sat INTEGER,
    weight INTEGER,

    -- Symptom / risk scales
    pain INTEGER,
    mobility INTEGER,
    edema INTEGER,
    confusion INTEGER,
    nutrition INTEGER,

    -- Body systems notes
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

# ---- AI priorities (top 3 problems per patient) ----
cur.execute("""
CREATE TABLE patient_priorities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL,
    priority_rank INTEGER NOT NULL,
    problem TEXT NOT NULL,
    FOREIGN KEY (patient_id) REFERENCES patients(id)
);
""")

# ---- AI-generated nursing tasks ----
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

# ---- Lab results (completed labs) ----
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

# ---- Lab orders (pending / ordered labs) ----
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

# ---- Mock data ----

# Nurses
cur.executemany(
    "INSERT INTO nurses (name) VALUES (?);",
    [
        ("Anna Müller",),
        ("Jonas Weber",),
        ("Lisa Schmidt",)
    ]
)

# Patients
cur.executemany("""
INSERT INTO patients 
(patient_identifier, name, gender, dob, room, diagnosis, bezugspflege_id,
 allergies, code_status, admission_date, expected_discharge, primary_doctor)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
""", [
    ("P-100001", "Maria Koch", "F", "1980-03-14", "3B",
     "Herzinsuffizienz NYHA III", 1,
     "Penicillin, Latex", "DNAR", "2025-11-30", "2025-12-07", "Dr. Keller"),

    ("P-100002", "Rolf Braun", "M", "1972-08-09", "3C",
     "NSTEMI", 2,
     "Keine bekannten Allergien", "Full Code", "2025-12-01", "2025-12-05", "Dr. Roth"),
])

# Meds for patient 1
cur.executemany("""
INSERT INTO medications (patient_id, name, dose, route, schedule, next_due, due_time)
VALUES (?, ?, ?, ?, ?, ?, ?);
""", [
    (1, "Bisoprolol", "2.5 mg", "p.o.", "1x morgens", "2025-12-09 08:00", "2025-12-09 08:00"),
    (1, "Furosemid", "20 mg", "i.v.", "2x täglich", "2025-12-09 08:00", "2025-12-09 14:00"),
])

# Meds for patient 2
cur.executemany("""
INSERT INTO medications (patient_id, name, dose, route, schedule, next_due, due_time)
VALUES (?, ?, ?, ?, ?, ?, ?);
""", [
    (2, "ASS", "100 mg", "p.o.", "1x morgens", "2025-12-9 08:00", "2025-12-09 08:00"),
])

# Orders/tasks
cur.executemany("""
INSERT INTO orders (patient_id, description, due_date, due_time, status, ordered_by, type)
VALUES (?, ?, ?, ?, ?, ?, ?);
""", [
    (1, "Gewichts-kontrolle täglich", "2025-12-04", "2025-12-04 08:00", "offen", "Dr. Keller", "Anordnung"),
    (1, "Bilanz ausführen", "2025-12-04", "2025-12-04 20:00", "offen", "Dr. Keller", "Pflegeaufgabe"),
    (2, "Belastungs-EKG", "2025-12-05", "2025-12-05 10:00", "geplant", "Dr. Roth", "Diagnostik"),
])

# Notes
cur.executemany("""
INSERT INTO doctor_notes (patient_id, note, created_at, author)
VALUES (?, ?, ?, ?);
""", [
    (1, "Klinisch kompensiert, Diuretika angepasst.", "2025-12-03 10:15", "Dr. Keller"),
    (2, "Post-NSTEMI, Verlauf stabil, Echo morgen.", "2025-12-03 09:40", "Dr. Roth"),
])

cur.executemany("""
INSERT INTO nurse_notes (patient_id, note, created_at, author)
VALUES (?, ?, ?, ?);
""", [
    (1, "PAT kurzatmig bei Belastung, O2 2l.", "2025-12-03 11:00", "Anna Müller"),
    (1, "Bilanz begonnen, Angehörige aufgeklärt.", "2025-12-03 15:30", "Anna Müller"),
    (2, "Schmerzskala 2/10 in Ruhe, 4/10 bei Bewegung.", "2025-12-03 12:00", "Jonas Weber"),
])

conn.commit()
conn.close()

print("Neue Datenbank mit due_time usw. wurde erstellt.")


