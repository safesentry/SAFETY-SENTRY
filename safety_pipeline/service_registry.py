from dataclasses import asdict, dataclass
from typing import Optional


@dataclass(frozen=True)
class ServiceSpec:
    service_id: str
    display_name: str
    domain: str
    status: str
    default_backend: Optional[str] = None
    notes: str = ""

    def to_dict(self):
        return asdict(self)

SERVICE_SPECS = {
    "gitea": ServiceSpec(
        service_id="gitea",
        display_name="Gitea",
        domain="software_development",
        status="implemented",
        default_backend="gitea",
        notes="Primary code-hosting deployment target and current implemented backend.",
    ),
    "rocketchat": ServiceSpec(
        service_id="rocketchat",
        display_name="Rocket.Chat",
        domain="team_communication",
        status="implemented",
        default_backend="rocketchat",
        notes="Team chat service with channels, messages, and user management via REST API.",
    ),

    "owncloud": ServiceSpec(
        service_id="owncloud",
        display_name="ownCloud",
        domain="file_management",
        status="implemented",
        default_backend="owncloud",
        notes="WebDAV file management service with oCIS backend.",
    ),
    "nocodb": ServiceSpec(
        service_id="nocodb",
        display_name="NocoDB",
        domain="database_table",
        status="implemented",
        default_backend="nocodb",
        notes="Structured table CRUD service with PostgreSQL backend.",
    ),
    "zammad": ServiceSpec(
        service_id="zammad",
        display_name="Zammad",
        domain="customer_support",
        status="implemented",
        default_backend="zammad",
        notes="Customer-support ticketing service backed by real Zammad Docker containers and REST API.",
    ),
    "erpnext": ServiceSpec(
        service_id="erpnext",
        display_name="ERPNext",
        domain="finance_accounting",
        status="implemented",
        default_backend="erpnext",
        notes="Finance and invoice workflow service backed by real ERPNext Docker deployment and Frappe site operations.",
    ),
    "openemr": ServiceSpec(
        service_id="openemr",
        display_name="OpenEMR",
        domain="healthcare",
        status="implemented",
        default_backend="openemr",
        notes="Healthcare records and scheduling service backed by real OpenEMR Docker deployment and MariaDB state.",
    ),
    "vaultwarden": ServiceSpec(
        service_id="vaultwarden",
        display_name="Vaultwarden",
        domain="credential_management",
        status="implemented",
        default_backend="vaultwarden",
        notes="Self-hosted password / credential manager. Bitwarden-compatible API.",
    ),
    "mailu": ServiceSpec(
        service_id="mailu",
        display_name="Mailu",
        domain="email_communication",
        status="implemented",
        default_backend="mailu",
        notes="Email service with Admin REST API, SMTP/IMAP integration. Includes truly irreversible operations (sent emails).",
    ),
}


def get_service_spec(service_id):
    return SERVICE_SPECS.get(service_id)


def list_all_service_specs():
    return list(SERVICE_SPECS.values())


def build_service_summary():
    return [spec.to_dict() for spec in list_all_service_specs()]
