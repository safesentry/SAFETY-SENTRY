#!/usr/bin/env python3
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta


def _numeric_suffix(value, prefix):
    match = re.fullmatch(rf"{re.escape(prefix)}-(\d+)", str(value or ""))
    if not match:
        raise ValueError(f"invalid id: {value}")
    return int(match.group(1))


def _split_name(name):
    parts = [part for part in str(name or "").strip().split() if part]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]


def _sql_literal(value):
    if value is None:
        return "NULL"
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def _run_mysql(db_container, root_password, db_name, sql):
    cmd = [
        "docker",
        "exec",
        "-i",
        db_container,
        "mysql",
        "-uroot",
        f"-p{root_password}",
        db_name,
    ]
    result = subprocess.run(cmd, input=sql, text=True, capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown mysql error"
        raise RuntimeError(detail)


def _seed_patient(patient):
    pid = _numeric_suffix(patient["id"], "PT")
    pubpid = patient["id"]
    fname, lname = _split_name(patient.get("name", ""))
    return """
INSERT INTO patient_data (pid, pubpid, fname, lname, DOB, sex, title, language, country_code, phone_home, email, status)
VALUES ({pid}, {pubpid}, {fname}, {lname}, {dob}, {sex}, '', '', 'US', {phone}, {email}, '');
""".format(
        pid=pid,
        pubpid=_sql_literal(pubpid),
        fname=_sql_literal(fname),
        lname=_sql_literal(lname),
        dob=_sql_literal(patient.get("dob", "")),
        sex=_sql_literal(patient.get("sex", "")),
        phone=_sql_literal(patient.get("phone", "")),
        email=_sql_literal(patient.get("email", "")),
    )


def _seed_note(patient_id, note):
    pid = _numeric_suffix(patient_id, "PT")
    return (
        "INSERT INTO pnotes (date, body, pid, user, groupname, activity, authorized, title, assigned_to, deleted, message_status, is_msg_encrypted) "
        "VALUES (NOW(), {body}, {pid}, {author}, 'Default', 1, 1, {title}, '', 0, 'New', 0);"
    ).format(
        body=_sql_literal(note.get("body", "")),
        pid=pid,
        author=_sql_literal(note.get("author", "system")),
        title=_sql_literal(note.get("title", "Chart note")),
    )


def _seed_appointment(appointment):
    appointment_num = _numeric_suffix(appointment["id"], "APT")
    patient_num = _numeric_suffix(appointment["patient_id"], "PT")
    category = 10 if "new patient" in str(appointment.get("reason", "")).lower() else 9
    start_dt = datetime.strptime(f"{appointment['date']} {appointment['time']}", "%Y-%m-%d %H:%M")
    end_dt = start_dt + timedelta(minutes=30)
    return (
        "INSERT INTO openemr_postcalendar_events "
        "(pc_eid, pc_catid, pc_multiple, pc_aid, pc_pid, pc_topic, pc_eventDate, pc_endDate, pc_duration, pc_recurrtype, pc_recurrfreq, "
        "pc_startTime, pc_endTime, pc_alldayevent, pc_eventstatus, pc_sharing, pc_apptstatus, pc_prefcatid, pc_facility, pc_sendalertsms, "
        "pc_sendalertemail, pc_billing_location, pc_room, pc_title, pc_hometext) "
        "VALUES "
        "({appointment_num}, {category}, 0, 'admin', '{patient_num}', 1, {date}, {date}, 1800, 0, 0, "
        "{start_time}, {end_time}, 0, 0, 0, {status}, 0, 0, 'NO', 'NO', 0, '', {reason}, {provider});"
    ).format(
        appointment_num=appointment_num,
        category=category,
        patient_num=patient_num,
        date=_sql_literal(appointment["date"]),
        start_time=_sql_literal(start_dt.strftime("%H:%M:%S")),
        end_time=_sql_literal(end_dt.strftime("%H:%M:%S")),
        status=_sql_literal(appointment.get("status", "scheduled")),
        reason=_sql_literal(appointment.get("reason", "")),
        provider=_sql_literal(appointment.get("provider", "")),
    )


def _seed_encounter(encounter):
    encounter_num = _numeric_suffix(encounter["id"], "ENC")
    patient_num = _numeric_suffix(encounter["patient_id"], "PT")
    return """
INSERT INTO form_encounter
  (id, date, reason, facility, facility_id, pid, encounter, pc_catid, provider_id, billing_facility, external_id)
VALUES
  ({encounter_num}, {date}, {reason}, {facility}, 0, {patient_num}, {encounter_num}, 5, {provider_id}, 0, {external_id});
""".format(
        encounter_num=encounter_num,
        date=_sql_literal(f"{encounter.get('date', '')} 00:00:00"),
        reason=_sql_literal(encounter.get("reason", "")),
        facility=_sql_literal(encounter.get("facility", "")),
        patient_num=patient_num,
        provider_id=int(encounter.get("provider_id", 0) or 0),
        external_id=_sql_literal(encounter["id"]),
    )


def _seed_allergy(entry):
    row_id = 1000 + _numeric_suffix(entry["id"], "ALG")
    patient_num = _numeric_suffix(entry["patient_id"], "PT")
    return """
INSERT INTO lists
  (id, date, type, title, reaction, severity_al, pid, activity, begdate, user, groupname, external_id)
VALUES
  ({row_id}, NOW(), 'allergy', {title}, {reaction}, {severity}, {patient_num}, {activity}, {begin_date}, 'admin', 'Default', {external_id});
""".format(
        row_id=row_id,
        title=_sql_literal(entry.get("allergen", "")),
        reaction=_sql_literal(entry.get("reaction", "")),
        severity=_sql_literal(entry.get("severity", "")),
        patient_num=patient_num,
        activity=1 if entry.get("active", True) else 0,
        begin_date=_sql_literal(f"{entry.get('begin_date', '')} 00:00:00"),
        external_id=_sql_literal(entry["id"]),
    )


def _seed_medication(entry):
    row_id = 2000 + _numeric_suffix(entry["id"], "MED")
    patient_num = _numeric_suffix(entry["patient_id"], "PT")
    end_date = entry.get("end_date")
    return """
INSERT INTO lists
  (id, date, type, title, comments, pid, activity, begdate, enddate, user, groupname, external_id)
VALUES
  ({row_id}, NOW(), 'medication', {title}, {comments}, {patient_num}, {activity}, {begin_date}, {end_date}, 'admin', 'Default', {external_id});
""".format(
        row_id=row_id,
        title=_sql_literal(entry.get("name", "")),
        comments=_sql_literal(entry.get("instructions", "")),
        patient_num=patient_num,
        activity=1 if entry.get("active", True) else 0,
        begin_date=_sql_literal(f"{entry.get('start_date', '')} 00:00:00"),
        end_date=_sql_literal(f"{end_date} 00:00:00") if end_date else "NULL",
        external_id=_sql_literal(entry["id"]),
    )


def _seed_insurance_policy(entry):
    row_id = _numeric_suffix(entry["id"], "INS")
    patient_num = _numeric_suffix(entry["patient_id"], "PT")
    end_date = entry.get("end_date")
    return """
INSERT INTO insurance_data
  (id, type, provider, plan_name, policy_number, date, date_end, pid, accept_assignment, policy_type)
VALUES
  ({row_id}, {coverage_type}, {provider}, {plan_name}, {policy_number}, {start_date}, {end_date}, {patient_num}, 'TRUE', '');
""".format(
        row_id=row_id,
        coverage_type=_sql_literal(entry.get("coverage_type", "primary")),
        provider=_sql_literal(entry.get("provider", "")),
        plan_name=_sql_literal(entry.get("plan_name", "")),
        policy_number=_sql_literal(entry.get("policy_number", "")),
        start_date=_sql_literal(entry.get("start_date", "")),
        end_date=_sql_literal(end_date) if end_date else "NULL",
        patient_num=patient_num,
    )


def main():
    if len(sys.argv) != 5:
        raise SystemExit("usage: seed_openemr_data.py <manifest> <db_container> <db_name> <root_password>")

    manifest_path, db_container, db_name, root_password = sys.argv[1:5]
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)

    statements = [
        "DELETE FROM pnotes WHERE pid BETWEEN 100 AND 199;",
        "DELETE FROM lists WHERE pid BETWEEN 100 AND 199 OR id BETWEEN 100 AND 199;",
        "DELETE FROM insurance_data WHERE pid BETWEEN 100 AND 199 OR id BETWEEN 100 AND 199;",
        "DELETE FROM form_encounter WHERE pid BETWEEN 100 AND 199 OR id BETWEEN 100 AND 199 OR encounter BETWEEN 100 AND 199;",
        "DELETE FROM openemr_postcalendar_events WHERE pc_eid BETWEEN 100 AND 199 OR CAST(pc_pid AS UNSIGNED) BETWEEN 100 AND 199;",
        "DELETE FROM patient_data WHERE pid BETWEEN 100 AND 199;",
    ]

    for patient in manifest.get("patients", []):
        statements.append(_seed_patient(patient))
    for patient in manifest.get("patients", []):
        for note in patient.get("notes", []):
            statements.append(_seed_note(patient["id"], note))
    for encounter in manifest.get("encounters", []):
        statements.append(_seed_encounter(encounter))
    for allergy in manifest.get("allergies", []):
        statements.append(_seed_allergy(allergy))
    for medication in manifest.get("medications", []):
        statements.append(_seed_medication(medication))
    for policy in manifest.get("insurance_policies", []):
        statements.append(_seed_insurance_policy(policy))
    for appointment in manifest.get("appointments", []):
        statements.append(_seed_appointment(appointment))

    _run_mysql(db_container, root_password, db_name, "\n".join(statements) + "\n")


if __name__ == "__main__":
    main()
