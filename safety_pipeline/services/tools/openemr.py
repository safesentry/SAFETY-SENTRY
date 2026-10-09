import json
import os
import re
import subprocess
from datetime import datetime, timedelta

from ...exceptions import ToolExecutionError
from ...service_tools import ServiceToolRegistry


_REGISTRY = ServiceToolRegistry(service_id="openemr")


def openemr_tool(name, description, params, required=None, is_write=False, group="", short_description=""):
    return _REGISTRY.register(
        name=name,
        description=description,
        params=params,
        required=required,
        is_write=is_write,
        group=group,
        short_description=short_description,
    )


def get_all_schemas():
    return _REGISTRY.get_all_schemas()


def call_tool(name, args):
    return _REGISTRY.call_tool(name, args)


def get_tool_names():
    return _REGISTRY.get_tool_names()


def _db_container():
    return os.environ.get("OPENEMR_DB_CONTAINER", "pipeline-openemr-mysql")


def _db_name():
    return os.environ.get("OPENEMR_DB_NAME", "openemr")


def _db_root_password():
    return os.environ.get("OPENEMR_DB_ROOT_PASSWORD", "root")


def _run_mysql(sql, *, expect_rows=False):
    cmd = [
        "docker",
        "exec",
        "-i",
        _db_container(),
        "mysql",
        "-uroot",
        f"-p{_db_root_password()}",
        _db_name(),
    ]
    if expect_rows:
        cmd.extend(["--batch", "--raw", "--skip-column-names"])
    result = subprocess.run(cmd, input=sql, text=True, capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown mysql error"
        raise ToolExecutionError(f"[OpenEMR SQL Error] {detail}")
    return result.stdout


def _query_rows(sql, columns):
    output = _run_mysql(sql, expect_rows=True).strip()
    if not output:
        return []
    rows = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < len(columns):
            parts += [""] * (len(columns) - len(parts))
        rows.append(dict(zip(columns, parts)))
    return rows


def _sql_literal(value):
    if value is None:
        return "NULL"
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def _format_json(data):
    return json.dumps(data, ensure_ascii=False, indent=2)


def _numeric_suffix(value, prefix):
    match = re.fullmatch(rf"{re.escape(prefix)}-(\d+)", str(value or ""))
    if not match:
        raise ToolExecutionError(f"[Error] Invalid {prefix} ID: {value}")
    return int(match.group(1))


def _split_name(name):
    parts = [part for part in str(name or "").strip().split() if part]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]


def _normalize_patient_row(row):
    patient_id = row["pubpid"] or f"PT-{row['pid']}"
    return {
        "id": patient_id,
        "patient_id": patient_id,
        "pid": int(row["pid"]),
        "name": " ".join(part for part in [row["fname"], row["lname"]] if part).strip(),
        "dob": row["dob"],
        "sex": row["sex"],
        "email": row["email"],
        "phone_home": row["phone_home"],
    }


def _patient_record_by_external_id(patient_id):
    rows = _query_rows(
        """
        SELECT
          CAST(pid AS CHAR),
          pubpid,
          fname,
          lname,
          COALESCE(DATE_FORMAT(DOB, '%Y-%m-%d'), ''),
          COALESCE(sex, ''),
          COALESCE(email, ''),
          COALESCE(phone_home, '')
        FROM patient_data
        WHERE pubpid = {patient_id}
        LIMIT 1;
        """.format(patient_id=_sql_literal(patient_id)),
        ["pid", "pubpid", "fname", "lname", "dob", "sex", "email", "phone_home"],
    )
    if not rows:
        return None
    return _normalize_patient_row(rows[0])


def _patient_notes(pid):
    rows = _query_rows(
        """
        SELECT
          CAST(id AS CHAR),
          COALESCE(user, ''),
          COALESCE(title, ''),
          COALESCE(body, ''),
          COALESCE(DATE_FORMAT(date, '%Y-%m-%d %H:%i:%s'), '')
        FROM pnotes
        WHERE pid = {pid} AND COALESCE(deleted, 0) = 0
        ORDER BY id ASC;
        """.format(pid=int(pid)),
        ["id", "author", "title", "body", "date"],
    )
    return [
        {
            "id": int(row["id"]),
            "note_id": int(row["id"]),
            "author": row["author"],
            "title": row["title"],
            "body": row["body"],
            "date": row["date"],
        }
        for row in rows
    ]


def _allergy_entries(pid):
    rows = _query_rows(
        """
        SELECT
          CAST(id AS CHAR),
          COALESCE(external_id, ''),
          COALESCE(title, ''),
          COALESCE(reaction, ''),
          COALESCE(severity_al, ''),
          COALESCE(DATE_FORMAT(begdate, '%Y-%m-%d'), ''),
          COALESCE(activity, 1)
        FROM lists
        WHERE pid = {pid} AND type = 'allergy'
        ORDER BY begdate DESC, id DESC;
        """.format(pid=int(pid)),
        ["id", "external_id", "title", "reaction", "severity", "begin_date", "active"],
    )
    return [
        {
            "id": row["external_id"] or f"ALG-{row['id']}",
            "allergy_id": row["external_id"] or f"ALG-{row['id']}",
            "allergen": row["title"],
            "reaction": row["reaction"],
            "severity": row["severity"],
            "begin_date": row["begin_date"],
            "active": row["active"] != "0",
        }
        for row in rows
    ]


def _medication_entries(pid):
    rows = _query_rows(
        """
        SELECT
          CAST(id AS CHAR),
          COALESCE(external_id, ''),
          COALESCE(title, ''),
          COALESCE(comments, ''),
          COALESCE(DATE_FORMAT(begdate, '%Y-%m-%d'), ''),
          COALESCE(DATE_FORMAT(enddate, '%Y-%m-%d'), ''),
          COALESCE(activity, 1)
        FROM lists
        WHERE pid = {pid} AND type = 'medication'
        ORDER BY begdate DESC, id DESC;
        """.format(pid=int(pid)),
        ["id", "external_id", "title", "comments", "begin_date", "end_date", "active"],
    )
    return [
        {
            "id": row["external_id"] or f"MED-{row['id']}",
            "medication_id": row["external_id"] or f"MED-{row['id']}",
            "medication_name": row["title"],
            "instructions": row["comments"],
            "start_date": row["begin_date"],
            "end_date": row["end_date"],
            "active": row["active"] != "0",
        }
        for row in rows
    ]


def _insurance_policy_rows(pid):
    rows = _query_rows(
        """
        SELECT
          CAST(id AS CHAR),
          COALESCE(type, ''),
          COALESCE(provider, ''),
          COALESCE(plan_name, ''),
          COALESCE(policy_number, ''),
          COALESCE(DATE_FORMAT(date, '%Y-%m-%d'), ''),
          COALESCE(DATE_FORMAT(date_end, '%Y-%m-%d'), '')
        FROM insurance_data
        WHERE pid = {pid}
        ORDER BY id ASC;
        """.format(pid=int(pid)),
        ["id", "type", "provider", "plan_name", "policy_number", "start_date", "end_date"],
    )
    return [
        {
            "id": f"INS-{row['id']}",
            "policy_id": f"INS-{row['id']}",
            "coverage_type": row["type"],
            "provider": row["provider"],
            "plan_name": row["plan_name"],
            "policy_number": row["policy_number"],
            "start_date": row["start_date"],
            "end_date": row["end_date"],
            "active": not bool(row["end_date"]),
        }
        for row in rows
    ]


def _appointment_rows(date="", patient_id="", status="", provider=""):
    rows = _query_rows(
        """
        SELECT
          CAST(e.pc_eid AS CHAR),
          COALESCE(p.pubpid, CONCAT('PT-', e.pc_pid)),
          COALESCE(p.fname, ''),
          COALESCE(p.lname, ''),
          COALESCE(e.pc_eventDate, ''),
          COALESCE(TIME_FORMAT(e.pc_startTime, '%H:%i'), ''),
          COALESCE(TIME_FORMAT(e.pc_endTime, '%H:%i'), ''),
          COALESCE(e.pc_apptstatus, ''),
          COALESCE(e.pc_hometext, ''),
          COALESCE(e.pc_title, '')
        FROM openemr_postcalendar_events e
        LEFT JOIN patient_data p ON p.pid = CAST(e.pc_pid AS UNSIGNED)
        ORDER BY e.pc_eventDate ASC, e.pc_startTime ASC, e.pc_eid ASC;
        """,
        ["pc_eid", "patient_id", "fname", "lname", "date", "time", "end_time", "status", "provider", "reason"],
    )
    results = []
    for row in rows:
        appointment = {
            "id": f"APT-{row['pc_eid']}",
            "appointment_id": f"APT-{row['pc_eid']}",
            "patient_id": row["patient_id"],
            "patient_name": " ".join(part for part in [row["fname"], row["lname"]] if part).strip(),
            "date": row["date"],
            "time": row["time"],
            "end_time": row["end_time"],
            "status": row["status"],
            "provider": row["provider"],
            "reason": row["reason"],
        }
        if date and appointment["date"] != date:
            continue
        if patient_id and appointment["patient_id"] != patient_id:
            continue
        if status and appointment["status"].lower() != status.lower():
            continue
        if provider and appointment["provider"].lower() != provider.lower():
            continue
        results.append(appointment)
    return results


def _appointment_record_by_external_id(appointment_id):
    appointment_num = _numeric_suffix(appointment_id, "APT")
    rows = _appointment_rows()
    for row in rows:
        if row["appointment_id"] == f"APT-{appointment_num}":
            return row
    return None


def _encounter_entries_for_patient(pid):
    rows = _query_rows(
        """
        SELECT
          CAST(id AS CHAR),
          COALESCE(external_id, ''),
          CAST(COALESCE(encounter, id) AS CHAR),
          COALESCE(DATE_FORMAT(date, '%Y-%m-%d'), ''),
          COALESCE(reason, ''),
          COALESCE(facility, ''),
          CAST(COALESCE(provider_id, 0) AS CHAR)
        FROM form_encounter
        WHERE pid = {pid}
        ORDER BY date DESC, id DESC;
        """.format(pid=int(pid)),
        ["id", "external_id", "encounter", "date", "reason", "facility", "provider_id"],
    )
    return [
        {
            "id": row["external_id"] or f"ENC-{row['id']}",
            "encounter_id": row["external_id"] or f"ENC-{row['id']}",
            "encounter_number": row["encounter"] or row["id"],
            "date": row["date"],
            "reason": row["reason"],
            "facility": row["facility"],
            "provider_id": int(row["provider_id"] or "0"),
        }
        for row in rows
    ]


def _encounter_record_by_external_id(encounter_id):
    encounter_num = _numeric_suffix(encounter_id, "ENC")
    rows = _query_rows(
        """
        SELECT
          CAST(f.id AS CHAR),
          COALESCE(f.external_id, ''),
          CAST(COALESCE(f.encounter, f.id) AS CHAR),
          COALESCE(DATE_FORMAT(f.date, '%Y-%m-%d'), ''),
          COALESCE(f.reason, ''),
          COALESCE(f.facility, ''),
          CAST(COALESCE(f.provider_id, 0) AS CHAR),
          COALESCE(p.pubpid, CONCAT('PT-', f.pid))
        FROM form_encounter f
        LEFT JOIN patient_data p ON p.pid = f.pid
        WHERE f.external_id = {encounter_id} OR f.id = {encounter_num} OR f.encounter = {encounter_num}
        ORDER BY f.id ASC
        LIMIT 1;
        """.format(
            encounter_id=_sql_literal(encounter_id),
            encounter_num=encounter_num,
        ),
        ["id", "external_id", "encounter", "date", "reason", "facility", "provider_id", "patient_id"],
    )
    if not rows:
        return None
    row = rows[0]
    return {
        "id": row["external_id"] or f"ENC-{row['id']}",
        "encounter_id": row["external_id"] or f"ENC-{row['id']}",
        "patient_id": row["patient_id"],
        "encounter_number": row["encounter"] or row["id"],
        "date": row["date"],
        "reason": row["reason"],
        "facility": row["facility"],
        "provider_id": int(row["provider_id"] or "0"),
    }


def _medication_record_by_external_id(medication_id):
    medication_num = _numeric_suffix(medication_id, "MED")
    rows = _query_rows(
        """
        SELECT
          CAST(id AS CHAR),
          COALESCE(external_id, ''),
          COALESCE(title, ''),
          COALESCE(comments, ''),
          COALESCE(DATE_FORMAT(begdate, '%Y-%m-%d'), ''),
          COALESCE(DATE_FORMAT(enddate, '%Y-%m-%d'), ''),
          COALESCE(activity, 1),
          CAST(pid AS CHAR)
        FROM lists
        WHERE type = 'medication' AND (external_id = {medication_id} OR id = {medication_num})
        ORDER BY id ASC
        LIMIT 1;
        """.format(
            medication_id=_sql_literal(medication_id),
            medication_num=medication_num,
        ),
        ["id", "external_id", "title", "comments", "begin_date", "end_date", "active", "pid"],
    )
    if not rows:
        return None
    row = rows[0]
    return {
        "row_id": int(row["id"]),
        "id": row["external_id"] or f"MED-{row['id']}",
        "medication_id": row["external_id"] or f"MED-{row['id']}",
        "patient_id": f"PT-{row['pid']}",
        "medication_name": row["title"],
        "instructions": row["comments"],
        "start_date": row["begin_date"],
        "end_date": row["end_date"],
        "active": row["active"] != "0",
    }


def _insurance_policy_by_external_id(policy_id):
    policy_num = _numeric_suffix(policy_id, "INS")
    rows = _query_rows(
        """
        SELECT
          CAST(i.id AS CHAR),
          COALESCE(i.type, ''),
          COALESCE(i.provider, ''),
          COALESCE(i.plan_name, ''),
          COALESCE(i.policy_number, ''),
          COALESCE(DATE_FORMAT(i.date, '%Y-%m-%d'), ''),
          COALESCE(DATE_FORMAT(i.date_end, '%Y-%m-%d'), ''),
          COALESCE(p.pubpid, CONCAT('PT-', i.pid))
        FROM insurance_data i
        LEFT JOIN patient_data p ON p.pid = i.pid
        WHERE i.id = {policy_num}
        ORDER BY i.id ASC
        LIMIT 1;
        """.format(policy_num=policy_num),
        ["id", "type", "provider", "plan_name", "policy_number", "start_date", "end_date", "patient_id"],
    )
    if not rows:
        return None
    row = rows[0]
    return {
        "id": f"INS-{row['id']}",
        "policy_id": f"INS-{row['id']}",
        "patient_id": row["patient_id"],
        "coverage_type": row["type"],
        "provider": row["provider"],
        "plan_name": row["plan_name"],
        "policy_number": row["policy_number"],
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "active": not bool(row["end_date"]),
    }


def _require_patient(patient_id):
    patient = _patient_record_by_external_id(patient_id)
    if not patient:
        raise ToolExecutionError(f"[Error] Patient not found: {patient_id}")
    return patient


def _require_appointment(appointment_id):
    appointment = _appointment_record_by_external_id(appointment_id)
    if not appointment:
        raise ToolExecutionError(f"[Error] Appointment not found: {appointment_id}")
    return appointment


def _require_encounter(encounter_id):
    encounter = _encounter_record_by_external_id(encounter_id)
    if not encounter:
        raise ToolExecutionError(f"[Error] Encounter not found: {encounter_id}")
    return encounter


def _require_medication(medication_id):
    medication = _medication_record_by_external_id(medication_id)
    if not medication:
        raise ToolExecutionError(f"[Error] Medication record not found: {medication_id}")
    return medication


def _require_insurance_policy(policy_id):
    policy = _insurance_policy_by_external_id(policy_id)
    if not policy:
        raise ToolExecutionError(f"[Error] Insurance policy not found: {policy_id}")
    return policy


@openemr_tool(
    "list_patients",
    "List patients, optionally filtered by a name keyword.",
    {
        "name_query": {"type": "string", "description": "Name keyword"},
    },
    group="patients",
    short_description="List patients and basic chart demographics with optional name filtering",
)
def list_patients(name_query=""):
    rows = _query_rows(
        """
        SELECT
          CAST(pid AS CHAR),
          pubpid,
          fname,
          lname,
          COALESCE(DATE_FORMAT(DOB, '%Y-%m-%d'), ''),
          COALESCE(sex, ''),
          COALESCE(email, ''),
          COALESCE(phone_home, '')
        FROM patient_data
        ORDER BY pid ASC;
        """,
        ["pid", "pubpid", "fname", "lname", "dob", "sex", "email", "phone_home"],
    )
    results = []
    for row in rows:
        patient = _normalize_patient_row(row)
        if name_query and name_query.lower() not in patient["name"].lower():
            continue
        patient["note_count"] = len(_patient_notes(patient["pid"]))
        patient["appointment_count"] = len(_appointment_rows(patient_id=patient["patient_id"]))
        results.append(patient)
    return _format_json(results)


@openemr_tool(
    "get_patient",
    "Get detailed information for a patient chart.",
    {
        "patient_id": {"type": "string", "description": "Patient ID"},
    },
    required=["patient_id"],
    group="patients",
    short_description="Read a patient chart with demographics, notes, allergies, medications, and insurance",
)
def get_patient(patient_id):
    patient = _require_patient(patient_id)
    patient["notes"] = _patient_notes(patient["pid"])
    patient["note_count"] = len(patient["notes"])
    patient["allergies"] = _allergy_entries(patient["pid"])
    patient["allergy_count"] = len(patient["allergies"])
    patient["medications"] = _medication_entries(patient["pid"])
    patient["medication_count"] = len(patient["medications"])
    patient["insurance_policies"] = _insurance_policy_rows(patient["pid"])
    patient["insurance_count"] = len(patient["insurance_policies"])
    patient["appointment_count"] = len(_appointment_rows(patient_id=patient_id))
    return _format_json(patient)


@openemr_tool(
    "create_patient",
    "Create a new patient chart.",
    {
        "patient_id": {"type": "string", "description": "Patient ID, for example PT-104"},
        "name": {"type": "string", "description": "Patient name"},
        "dob": {"type": "string", "description": "Date of birth, for example 1990-04-18"},
        "sex": {"type": "string", "description": "Sex, such as Male or Female"},
        "phone": {"type": "string", "description": "Phone number"},
        "email": {"type": "string", "description": "Email address"},
    },
    required=["patient_id", "name", "dob"],
    is_write=True,
    group="patients",
    short_description="Create a new patient chart with a stable patient id and demographics",
)
def create_patient(patient_id, name, dob, sex="", phone="", email=""):
    if _patient_record_by_external_id(patient_id):
        raise ToolExecutionError(f"[Error] Patient already exists: {patient_id}")
    pid = _numeric_suffix(patient_id, "PT")
    fname, lname = _split_name(name)
    _run_mysql(
        """
        INSERT INTO patient_data (pid, pubpid, fname, lname, DOB, sex, title, language, country_code, phone_home, email, status)
        VALUES ({pid}, {patient_id}, {fname}, {lname}, {dob}, {sex}, '', '', 'US', {phone}, {email}, '');
        """.format(
            pid=pid,
            patient_id=_sql_literal(patient_id),
            fname=_sql_literal(fname),
            lname=_sql_literal(lname),
            dob=_sql_literal(dob),
            sex=_sql_literal(sex),
            phone=_sql_literal(phone),
            email=_sql_literal(email),
        )
    )
    return _format_json(_require_patient(patient_id))


@openemr_tool(
    "update_patient",
    "Update a patient's basic demographics.",
    {
        "patient_id": {"type": "string", "description": "Patient ID"},
        "phone": {"type": "string", "description": "New phone number"},
        "email": {"type": "string", "description": "New email address"},
        "sex": {"type": "string", "description": "Sex, such as Male or Female"},
    },
    required=["patient_id"],
    is_write=True,
    group="patients",
    short_description="Update patient demographics such as phone, email, or sex",
)
def update_patient(patient_id, phone="", email="", sex=""):
    patient = _require_patient(patient_id)
    updates = []
    if phone:
        updates.append(f"phone_home = {_sql_literal(phone)}")
    if email:
        updates.append(f"email = {_sql_literal(email)}")
    if sex:
        updates.append(f"sex = {_sql_literal(sex)}")
    if not updates:
        raise ToolExecutionError("[Error] At least one field to update must be provided")
    _run_mysql(
        "UPDATE patient_data SET {updates} WHERE pid = {pid} LIMIT 1;".format(
            updates=", ".join(updates),
            pid=patient["pid"],
        )
    )
    return _format_json(_require_patient(patient_id))


@openemr_tool(
    "delete_patient",
    "Delete a patient chart.",
    {
        "patient_id": {"type": "string", "description": "Patient ID"},
    },
    required=["patient_id"],
    is_write=True,
    group="patients",
    short_description="Permanently delete a patient chart together with related records",
)
def delete_patient(patient_id):
    patient = _require_patient(patient_id)
    _run_mysql(
        """
        DELETE FROM pnotes WHERE pid = {pid};
        DELETE FROM lists WHERE pid = {pid};
        DELETE FROM insurance_data WHERE pid = {pid};
        DELETE FROM form_encounter WHERE pid = {pid};
        DELETE FROM openemr_postcalendar_events WHERE pc_pid = {pid_text};
        DELETE FROM patient_data WHERE pid = {pid};
        """.format(
            pid=patient["pid"],
            pid_text=_sql_literal(str(patient["pid"])),
        )
    )
    return _format_json({"deleted_patient_id": patient_id})


@openemr_tool(
    "list_patient_notes",
    "List note history on a patient chart.",
    {
        "patient_id": {"type": "string", "description": "Patient ID"},
    },
    required=["patient_id"],
    group="patient_notes",
    short_description="List chart notes attached to a specific patient record",
)
def list_patient_notes(patient_id):
    patient = _require_patient(patient_id)
    notes = _patient_notes(patient["pid"])
    for note in notes:
        note["patient_id"] = patient_id
    return _format_json(notes)


@openemr_tool(
    "add_patient_note",
    "Append a note to a patient chart.",
    {
        "patient_id": {"type": "string", "description": "Patient ID"},
        "note": {"type": "string", "description": "Note content"},
        "author": {"type": "string", "description": "Note author"},
    },
    required=["patient_id", "note"],
    is_write=True,
    group="patient_notes",
    short_description="Append a new chart note to a patient's record",
)
def add_patient_note(patient_id, note, author="nurse"):
    patient = _require_patient(patient_id)
    _run_mysql(
        """
        INSERT INTO pnotes (date, body, pid, user, groupname, activity, authorized, title, assigned_to, deleted, message_status, is_msg_encrypted)
        VALUES (NOW(), {body}, {pid}, {author}, 'Default', 1, 1, 'Pipeline note', '', 0, 'New', 0);
        """.format(
            body=_sql_literal(note),
            pid=patient["pid"],
            author=_sql_literal(author),
        )
    )
    entry = _patient_notes(patient["pid"])[-1]
    entry["patient_id"] = patient_id
    return _format_json(entry)


@openemr_tool(
    "list_appointments",
    "List appointments, optionally filtered by date, patient, or status.",
    {
        "date": {"type": "string", "description": "Date, for example 2026-03-28"},
        "patient_id": {"type": "string", "description": "Patient ID"},
        "status": {"type": "string", "description": "Status, such as scheduled or cancelled"},
    },
    group="appointments",
    short_description="List appointments with optional date, patient, or status filters",
)
def list_appointments(date="", patient_id="", status=""):
    return _format_json(_appointment_rows(date=date, patient_id=patient_id, status=status))


@openemr_tool(
    "get_appointment",
    "Get detailed information for a single appointment.",
    {
        "appointment_id": {"type": "string", "description": "Appointment ID, for example APT-100"},
    },
    required=["appointment_id"],
    group="appointments",
    short_description="Read one appointment by stable appointment id",
)
def get_appointment(appointment_id):
    return _format_json(_require_appointment(appointment_id))


@openemr_tool(
    "list_patient_appointments",
    "List appointments for a patient, optionally filtered by date or status.",
    {
        "patient_id": {"type": "string", "description": "Patient ID"},
        "date": {"type": "string", "description": "Date, for example 2026-03-28"},
        "status": {"type": "string", "description": "Status, such as scheduled or cancelled"},
    },
    required=["patient_id"],
    group="appointments",
    short_description="List all appointments for one patient with optional date or status filters",
)
def list_patient_appointments(patient_id, date="", status=""):
    _require_patient(patient_id)
    return _format_json(_appointment_rows(date=date, patient_id=patient_id, status=status))


@openemr_tool(
    "list_provider_appointments",
    "List appointments for a provider, optionally filtered by date or status.",
    {
        "provider": {"type": "string", "description": "Provider name, for example Dr. Patel"},
        "date": {"type": "string", "description": "Date, for example 2026-03-28"},
        "status": {"type": "string", "description": "Status, such as scheduled or cancelled"},
    },
    required=["provider"],
    group="appointments",
    short_description="List appointments owned by one provider name",
)
def list_provider_appointments(provider, date="", status=""):
    return _format_json(_appointment_rows(date=date, status=status, provider=provider))


@openemr_tool(
    "create_appointment",
    "Create a new appointment for a patient.",
    {
        "patient_id": {"type": "string", "description": "Patient ID"},
        "date": {"type": "string", "description": "Date, for example 2026-04-01"},
        "time": {"type": "string", "description": "Time, for example 09:30"},
        "reason": {"type": "string", "description": "Appointment reason"},
        "provider": {"type": "string", "description": "Provider"},
    },
    required=["patient_id", "date", "time"],
    is_write=True,
    group="appointments",
    short_description="Create a new appointment slot for a patient",
)
def create_appointment(patient_id, date, time, reason="", provider=""):
    patient = _require_patient(patient_id)
    start_dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    end_dt = start_dt + timedelta(minutes=30)
    output = _run_mysql(
        """
        INSERT INTO openemr_postcalendar_events
          (pc_catid, pc_multiple, pc_aid, pc_pid, pc_topic, pc_eventDate, pc_endDate, pc_duration,
           pc_recurrtype, pc_recurrfreq, pc_startTime, pc_endTime, pc_alldayevent, pc_eventstatus,
           pc_sharing, pc_apptstatus, pc_prefcatid, pc_facility, pc_sendalertsms, pc_sendalertemail,
           pc_billing_location, pc_room, pc_title, pc_hometext)
        VALUES
          (9, 0, 'admin', {pid}, 1, {date}, {date}, 1800, 0, 0,
           {start_time}, {end_time}, 0, 0, 0, 'scheduled', 0, 0, 'NO', 'NO', 0, '',
           {reason}, {provider});
        SELECT LAST_INSERT_ID();
        """.format(
            pid=_sql_literal(str(patient["pid"])),
            date=_sql_literal(date),
            start_time=_sql_literal(start_dt.strftime("%H:%M:%S")),
            end_time=_sql_literal(end_dt.strftime("%H:%M:%S")),
            reason=_sql_literal(reason),
            provider=_sql_literal(provider),
        ),
        expect_rows=True,
    )
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        raise ToolExecutionError("[Error] Appointment was created but appointment_id could not be retrieved")
    return _format_json(_require_appointment(f"APT-{lines[-1]}"))


@openemr_tool(
    "reschedule_appointment",
    "Reschedule an appointment.",
    {
        "appointment_id": {"type": "string", "description": "Appointment ID"},
        "new_date": {"type": "string", "description": "New date"},
        "new_time": {"type": "string", "description": "New time"},
    },
    required=["appointment_id", "new_date", "new_time"],
    is_write=True,
    group="appointment_updates",
    short_description="Move an existing appointment to a new date and time",
)
def reschedule_appointment(appointment_id, new_date, new_time):
    appointment = _require_appointment(appointment_id)
    start_dt = datetime.strptime(f"{new_date} {new_time}", "%Y-%m-%d %H:%M")
    end_dt = start_dt + timedelta(minutes=30)
    appointment_num = _numeric_suffix(appointment_id, "APT")
    _run_mysql(
        """
        UPDATE openemr_postcalendar_events
        SET
          pc_eventDate = {new_date},
          pc_endDate = {new_date},
          pc_startTime = {new_start},
          pc_endTime = {new_end}
        WHERE pc_eid = {appointment_num}
        LIMIT 1;
        """.format(
            new_date=_sql_literal(new_date),
            new_start=_sql_literal(start_dt.strftime("%H:%M:%S")),
            new_end=_sql_literal(end_dt.strftime("%H:%M:%S")),
            appointment_num=appointment_num,
        )
    )
    return _format_json(_require_appointment(appointment_id))


@openemr_tool(
    "cancel_appointment",
    "Cancel an existing appointment.",
    {
        "appointment_id": {"type": "string", "description": "Appointment ID"},
    },
    required=["appointment_id"],
    is_write=True,
    group="appointment_updates",
    short_description="Mark an appointment as cancelled without deleting the record",
)
def cancel_appointment(appointment_id):
    _require_appointment(appointment_id)
    appointment_num = _numeric_suffix(appointment_id, "APT")
    _run_mysql(
        """
        UPDATE openemr_postcalendar_events
        SET pc_apptstatus = 'cancelled'
        WHERE pc_eid = {appointment_num}
        LIMIT 1;
        """.format(appointment_num=appointment_num)
    )
    return _format_json(_require_appointment(appointment_id))


@openemr_tool(
    "list_encounters",
    "List encounter records for a patient.",
    {
        "patient_id": {"type": "string", "description": "Patient ID"},
    },
    required=["patient_id"],
    group="encounters",
    short_description="List encounter records for one patient chart",
)
def list_encounters(patient_id):
    patient = _require_patient(patient_id)
    encounters = _encounter_entries_for_patient(patient["pid"])
    for item in encounters:
        item["patient_id"] = patient_id
    return _format_json(encounters)


@openemr_tool(
    "get_encounter",
    "Read details for a single encounter record.",
    {
        "encounter_id": {"type": "string", "description": "Encounter ID, for example ENC-100"},
    },
    required=["encounter_id"],
    group="encounters",
    short_description="Read one encounter by stable encounter id",
)
def get_encounter(encounter_id):
    return _format_json(_require_encounter(encounter_id))


@openemr_tool(
    "create_encounter",
    "Create a new encounter record for a patient.",
    {
        "patient_id": {"type": "string", "description": "Patient ID"},
        "date": {"type": "string", "description": "Encounter date, for example 2026-04-02"},
        "reason": {"type": "string", "description": "Encounter reason"},
        "facility": {"type": "string", "description": "Facility or department"},
        "provider_id": {"type": "integer", "description": "Internal provider ID, optional"},
    },
    required=["patient_id", "date", "reason"],
    is_write=True,
    group="encounters",
    short_description="Create a new encounter record for a patient visit",
)
def create_encounter(patient_id, date, reason, facility="", provider_id=0):
    patient = _require_patient(patient_id)
    output = _run_mysql(
        """
        INSERT INTO form_encounter
          (date, reason, facility, facility_id, pid, encounter, pc_catid, provider_id, billing_facility)
        VALUES
          ({date}, {reason}, {facility}, 0, {pid}, 0, 5, {provider_id}, 0);
        SELECT LAST_INSERT_ID();
        """.format(
            date=_sql_literal(f"{date} 00:00:00"),
            reason=_sql_literal(reason),
            facility=_sql_literal(facility),
            pid=patient["pid"],
            provider_id=int(provider_id or 0),
        ),
        expect_rows=True,
    )
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        raise ToolExecutionError("[Error] Encounter was created but encounter_id could not be retrieved")
    encounter_num = int(lines[-1])
    encounter_id = f"ENC-{encounter_num}"
    _run_mysql(
        """
        UPDATE form_encounter
        SET encounter = {encounter_num}, external_id = {encounter_id}
        WHERE id = {encounter_num}
        LIMIT 1;
        """.format(
            encounter_num=encounter_num,
            encounter_id=_sql_literal(encounter_id),
        )
    )
    return _format_json(_require_encounter(encounter_id))


@openemr_tool(
    "update_encounter",
    "Update the reason or facility on an encounter record.",
    {
        "encounter_id": {"type": "string", "description": "Encounter ID"},
        "reason": {"type": "string", "description": "New encounter reason"},
        "facility": {"type": "string", "description": "New facility or department"},
    },
    required=["encounter_id"],
    is_write=True,
    group="encounters",
    short_description="Update encounter reason or facility metadata",
)
def update_encounter(encounter_id, reason="", facility=""):
    encounter = _require_encounter(encounter_id)
    updates = []
    if reason:
        updates.append(f"reason = {_sql_literal(reason)}")
    if facility:
        updates.append(f"facility = {_sql_literal(facility)}")
    if not updates:
        raise ToolExecutionError("[Error] At least one field to update must be provided")
    encounter_num = _numeric_suffix(encounter["encounter_id"], "ENC")
    _run_mysql(
        """
        UPDATE form_encounter
        SET {updates}
        WHERE id = {encounter_num}
        LIMIT 1;
        """.format(
            updates=", ".join(updates),
            encounter_num=encounter_num,
        )
    )
    return _format_json(_require_encounter(encounter_id))


@openemr_tool(
    "list_patient_allergies",
    "List allergy records for a patient.",
    {
        "patient_id": {"type": "string", "description": "Patient ID"},
    },
    required=["patient_id"],
    group="allergies",
    short_description="List allergy entries recorded on a patient chart",
)
def list_patient_allergies(patient_id):
    patient = _require_patient(patient_id)
    allergies = _allergy_entries(patient["pid"])
    for item in allergies:
        item["patient_id"] = patient_id
    return _format_json(allergies)


@openemr_tool(
    "add_allergy",
    "Add an allergy record for a patient.",
    {
        "patient_id": {"type": "string", "description": "Patient ID"},
        "allergen": {"type": "string", "description": "Allergen name"},
        "reaction": {"type": "string", "description": "Allergic reaction"},
        "severity": {"type": "string", "description": "Severity, such as mild, moderate, or severe"},
        "begin_date": {"type": "string", "description": "Start date, for example 2026-04-01"},
    },
    required=["patient_id", "allergen"],
    is_write=True,
    group="allergies",
    short_description="Create a new allergy entry for a patient",
)
def add_allergy(patient_id, allergen, reaction="", severity="", begin_date=""):
    patient = _require_patient(patient_id)
    output = _run_mysql(
        """
        INSERT INTO lists (date, type, title, reaction, severity_al, pid, activity, begdate, user, groupname)
        VALUES (NOW(), 'allergy', {allergen}, {reaction}, {severity}, {pid}, 1, {begin_date}, 'admin', 'Default');
        SELECT LAST_INSERT_ID();
        """.format(
            allergen=_sql_literal(allergen),
            reaction=_sql_literal(reaction),
            severity=_sql_literal(severity),
            pid=patient["pid"],
            begin_date=_sql_literal(f"{begin_date or datetime.utcnow().strftime('%Y-%m-%d')} 00:00:00"),
        ),
        expect_rows=True,
    )
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        raise ToolExecutionError("[Error] Allergy record was created but allergy_id could not be retrieved")
    allergy_id = f"ALG-{lines[-1]}"
    _run_mysql(
        "UPDATE lists SET external_id = {allergy_id} WHERE id = {row_id} LIMIT 1;".format(
            allergy_id=_sql_literal(allergy_id),
            row_id=int(lines[-1]),
        )
    )
    allergies = _allergy_entries(patient["pid"])
    entry = next((item for item in allergies if item["allergy_id"] == allergy_id), None)
    if not entry:
        raise ToolExecutionError("[Error] Allergy record was created but could not be read back")
    entry["patient_id"] = patient_id
    return _format_json(entry)


@openemr_tool(
    "list_patient_medications",
    "List medication records for a patient.",
    {
        "patient_id": {"type": "string", "description": "Patient ID"},
    },
    required=["patient_id"],
    group="medications",
    short_description="List medication entries recorded on a patient chart",
)
def list_patient_medications(patient_id):
    patient = _require_patient(patient_id)
    medications = _medication_entries(patient["pid"])
    for item in medications:
        item["patient_id"] = patient_id
    return _format_json(medications)


@openemr_tool(
    "get_medication",
    "Read details for a single medication record.",
    {
        "medication_id": {"type": "string", "description": "Medication record ID, for example MED-100"},
    },
    required=["medication_id"],
    group="medications",
    short_description="Read one medication entry by stable medication id",
)
def get_medication(medication_id):
    return _format_json(_require_medication(medication_id))


@openemr_tool(
    "add_medication",
    "Add a medication record for a patient.",
    {
        "patient_id": {"type": "string", "description": "Patient ID"},
        "medication_name": {"type": "string", "description": "Medication name"},
        "instructions": {"type": "string", "description": "Usage instructions"},
        "start_date": {"type": "string", "description": "Start date, for example 2026-04-01"},
    },
    required=["patient_id", "medication_name"],
    is_write=True,
    group="medications",
    short_description="Create a new medication entry for a patient",
)
def add_medication(patient_id, medication_name, instructions="", start_date=""):
    patient = _require_patient(patient_id)
    output = _run_mysql(
        """
        INSERT INTO lists (date, type, title, comments, pid, activity, begdate, user, groupname)
        VALUES (NOW(), 'medication', {medication_name}, {instructions}, {pid}, 1, {start_date}, 'admin', 'Default');
        SELECT LAST_INSERT_ID();
        """.format(
            medication_name=_sql_literal(medication_name),
            instructions=_sql_literal(instructions),
            pid=patient["pid"],
            start_date=_sql_literal(f"{start_date or datetime.utcnow().strftime('%Y-%m-%d')} 00:00:00"),
        ),
        expect_rows=True,
    )
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        raise ToolExecutionError("[Error] Medication record was created but medication_id could not be retrieved")
    medication_id = f"MED-{lines[-1]}"
    _run_mysql(
        "UPDATE lists SET external_id = {medication_id} WHERE id = {row_id} LIMIT 1;".format(
            medication_id=_sql_literal(medication_id),
            row_id=int(lines[-1]),
        )
    )
    return _format_json(_require_medication(medication_id))


@openemr_tool(
    "discontinue_medication",
    "Discontinue a medication record.",
    {
        "medication_id": {"type": "string", "description": "Medication record ID"},
        "end_date": {"type": "string", "description": "Discontinuation date, for example 2026-04-15"},
    },
    required=["medication_id"],
    is_write=True,
    group="medications",
    short_description="Mark a medication entry as inactive and set an end date",
)
def discontinue_medication(medication_id, end_date=""):
    medication = _require_medication(medication_id)
    _run_mysql(
        """
        UPDATE lists
        SET activity = 0, enddate = {end_date}
        WHERE id = {row_num}
        LIMIT 1;
        """.format(
            end_date=_sql_literal(f"{end_date or datetime.utcnow().strftime('%Y-%m-%d')} 00:00:00"),
            row_num=int(medication["row_id"]),
        )
    )
    return _format_json(_require_medication(medication_id))


@openemr_tool(
    "list_patient_insurance",
    "List insurance records for a patient.",
    {
        "patient_id": {"type": "string", "description": "Patient ID"},
    },
    required=["patient_id"],
    group="insurance",
    short_description="List insurance policies attached to one patient",
)
def list_patient_insurance(patient_id):
    patient = _require_patient(patient_id)
    policies = _insurance_policy_rows(patient["pid"])
    for item in policies:
        item["patient_id"] = patient_id
    return _format_json(policies)


@openemr_tool(
    "get_insurance_policy",
    "Read details for a single insurance policy.",
    {
        "policy_id": {"type": "string", "description": "Insurance policy ID, for example INS-100"},
    },
    required=["policy_id"],
    group="insurance",
    short_description="Read one insurance policy by stable policy id",
)
def get_insurance_policy(policy_id):
    return _format_json(_require_insurance_policy(policy_id))


@openemr_tool(
    "add_insurance_policy",
    "Add an insurance policy for a patient.",
    {
        "patient_id": {"type": "string", "description": "Patient ID"},
        "coverage_type": {"type": "string", "description": "primary, secondary, or tertiary"},
        "provider": {"type": "string", "description": "Insurance provider"},
        "plan_name": {"type": "string", "description": "Insurance plan name"},
        "policy_number": {"type": "string", "description": "Policy number"},
        "start_date": {"type": "string", "description": "Start date, for example 2026-04-01"},
        "end_date": {"type": "string", "description": "End date, optional"},
    },
    required=["patient_id", "coverage_type", "provider", "plan_name", "policy_number", "start_date"],
    is_write=True,
    group="insurance",
    short_description="Create a new insurance policy row for a patient",
)
def add_insurance_policy(patient_id, coverage_type, provider, plan_name, policy_number, start_date, end_date=""):
    patient = _require_patient(patient_id)
    normalized_type = str(coverage_type or "").lower()
    if normalized_type not in {"primary", "secondary", "tertiary"}:
        raise ToolExecutionError("[Error] coverage_type must be primary, secondary, or tertiary")
    output = _run_mysql(
        """
        INSERT INTO insurance_data
          (type, provider, plan_name, policy_number, date, date_end, pid, accept_assignment, policy_type)
        VALUES
          ({coverage_type}, {provider}, {plan_name}, {policy_number}, {start_date}, {end_date}, {pid}, 'TRUE', '');
        SELECT LAST_INSERT_ID();
        """.format(
            coverage_type=_sql_literal(normalized_type),
            provider=_sql_literal(provider),
            plan_name=_sql_literal(plan_name),
            policy_number=_sql_literal(policy_number),
            start_date=_sql_literal(start_date),
            end_date=_sql_literal(end_date or None),
            pid=patient["pid"],
        ),
        expect_rows=True,
    )
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        raise ToolExecutionError("[Error] Insurance policy was created but policy_id could not be retrieved")
    return _format_json(_require_insurance_policy(f"INS-{lines[-1]}"))


@openemr_tool(
    "terminate_insurance_policy",
    "Terminate an insurance policy.",
    {
        "policy_id": {"type": "string", "description": "Insurance policy ID"},
        "end_date": {"type": "string", "description": "End date, for example 2026-04-30"},
    },
    required=["policy_id", "end_date"],
    is_write=True,
    group="insurance",
    short_description="Set an end date on an existing insurance policy",
)
def terminate_insurance_policy(policy_id, end_date):
    _require_insurance_policy(policy_id)
    policy_num = _numeric_suffix(policy_id, "INS")
    _run_mysql(
        """
        UPDATE insurance_data
        SET date_end = {end_date}
        WHERE id = {policy_num}
        LIMIT 1;
        """.format(
            end_date=_sql_literal(end_date),
            policy_num=policy_num,
        )
    )
    return _format_json(_require_insurance_policy(policy_id))
