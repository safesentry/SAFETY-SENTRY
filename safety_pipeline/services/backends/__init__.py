"""Service backend implementations."""

from .erpnext import ERPNextBackend
from .mailu import MailuBackend
from .openemr import OpenEMRBackend
from .vaultwarden import VaultwardenBackend
from .zammad import ZammadBackend

__all__ = [
    "ERPNextBackend",
    "MailuBackend",
    "OpenEMRBackend",
    "VaultwardenBackend",
    "ZammadBackend",
]
