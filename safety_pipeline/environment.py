"""
Environment backend factory plus local backend implementations.

The synthesis pipeline only needs real-tool schema lookup, real-tool execution,
and environment reset between episodes.
"""

import os
import subprocess

from .backend_abc import EnvironmentBackend
from .services.backends import (
    ERPNextBackend,
    MailuBackend,
    OpenEMRBackend,
    VaultwardenBackend,
    ZammadBackend,
)
from .settings import REPO_ROOT, reload_runtime_env


def _run_reset_script(script_name, backend_label, *, after_reset=None):
    script_path = os.path.join(REPO_ROOT, "scripts", script_name)
    try:
        subprocess.run(["bash", script_path], cwd=REPO_ROOT, check=True)
        reload_runtime_env()
        if after_reset is not None:
            after_reset()
        print(f"[{backend_label}] {script_name} completed")
    except Exception as exc:
        print(f"[{backend_label}] {script_name} failed: {exc}")


class GiteaBackend(EnvironmentBackend):
    """Gitea API backend."""

    def __init__(self):
        self._gitea_tools = None

    def _get_gitea_tools(self):
        if self._gitea_tools is not None:
            if hasattr(self._gitea_tools, "refresh_runtime_config"):
                self._gitea_tools.refresh_runtime_config()
            return self._gitea_tools
        try:
            from .services.tools import gitea as gitea_tools_module
        except ModuleNotFoundError as exc:
            raise RuntimeError("The current environment is missing the gitea_tools module.") from exc
        self._gitea_tools = gitea_tools_module
        if hasattr(self._gitea_tools, "refresh_runtime_config"):
            self._gitea_tools.refresh_runtime_config()
        return self._gitea_tools

    def get_tool_schemas(self):
        return self._get_gitea_tools().get_all_schemas()

    def get_tool_names(self):
        return self._get_gitea_tools().get_tool_names()

    def execute_tool(self, name, args):
        return self._get_gitea_tools().call_tool(name, args)

    def reset(self):
        _run_reset_script("reset_env.sh", "GiteaBackend")


class NocoDBBackend(EnvironmentBackend):
    """NocoDB API backend."""

    def __init__(self):
        self._nocodb_tools = None

    def _get_nocodb_tools(self):
        if self._nocodb_tools is not None:
            if hasattr(self._nocodb_tools, "refresh_runtime_config"):
                self._nocodb_tools.refresh_runtime_config()
            return self._nocodb_tools
        try:
            from .services.tools import nocodb as nocodb_tools_module
        except ModuleNotFoundError as exc:
            raise RuntimeError("The current environment is missing the nocodb_tools module.") from exc
        self._nocodb_tools = nocodb_tools_module
        if hasattr(self._nocodb_tools, "refresh_runtime_config"):
            self._nocodb_tools.refresh_runtime_config()
        return self._nocodb_tools

    def get_tool_schemas(self):
        return self._get_nocodb_tools().get_all_schemas()

    def get_tool_names(self):
        return self._get_nocodb_tools().get_tool_names()

    def execute_tool(self, name, args):
        return self._get_nocodb_tools().call_tool(name, args)

    def reset(self):
        _run_reset_script("reset_nocodb_env.sh", "NocoDBBackend")


class OwnCloudBackend(EnvironmentBackend):
    """ownCloud (oCIS) WebDAV backend."""

    def __init__(self):
        self._owncloud_tools = None

    def _get_owncloud_tools(self):
        if self._owncloud_tools is not None:
            return self._owncloud_tools
        try:
            from .services.tools import owncloud as owncloud_tools_module
        except ModuleNotFoundError as exc:
            raise RuntimeError("The current environment is missing the owncloud_tools module.") from exc
        self._owncloud_tools = owncloud_tools_module
        return self._owncloud_tools

    def get_tool_schemas(self):
        return self._get_owncloud_tools().get_all_schemas()

    def get_tool_names(self):
        return self._get_owncloud_tools().get_tool_names()

    def execute_tool(self, name, args):
        return self._get_owncloud_tools().call_tool(name, args)

    def reset(self):
        _run_reset_script("reset_owncloud_env.sh", "OwnCloudBackend")


class RocketChatBackend(EnvironmentBackend):
    """Rocket.Chat REST API backend."""

    def __init__(self):
        self._rocketchat_tools = None

    def _get_rocketchat_tools(self):
        if self._rocketchat_tools is not None:
            return self._rocketchat_tools
        try:
            from .services.tools import rocketchat as rocketchat_tools_module
        except ModuleNotFoundError as exc:
            raise RuntimeError("The current environment is missing the rocketchat_tools module.") from exc
        self._rocketchat_tools = rocketchat_tools_module
        return self._rocketchat_tools

    def get_tool_schemas(self):
        return self._get_rocketchat_tools().get_all_schemas()

    def get_tool_names(self):
        return self._get_rocketchat_tools().get_tool_names()

    def execute_tool(self, name, args):
        return self._get_rocketchat_tools().call_tool(name, args)

    def reset(self):
        def _clear_auth_cache():
            rt = self._get_rocketchat_tools()
            rt._auth_cache["user_id"] = None
            rt._auth_cache["token"] = None

        _run_reset_script(
            "reset_rocketchat_env.sh",
            "RocketChatBackend",
            after_reset=_clear_auth_cache,
        )


_BACKEND_FACTORIES = {
    "erpnext": ERPNextBackend,
    "gitea": GiteaBackend,
    "mailu": MailuBackend,
    "nocodb": NocoDBBackend,
    "openemr": OpenEMRBackend,
    "owncloud": OwnCloudBackend,
    "rocketchat": RocketChatBackend,
    "vaultwarden": VaultwardenBackend,
    "zammad": ZammadBackend,
}

_BACKEND_INSTANCES = {}


def get_supported_backend_names():
    return list(_BACKEND_FACTORIES.keys())


def get_backend(env_name=None):
    """Return the singleton backend for the specified environment."""
    env_name = env_name or os.environ.get("PIPELINE_ENV", "gitea")
    factory = _BACKEND_FACTORIES.get(env_name)
    if factory is None:
        supported = ", ".join(get_supported_backend_names())
        raise ValueError(f"Unknown environment backend: {env_name}. Registered backends: {supported}")
    if env_name not in _BACKEND_INSTANCES:
        _BACKEND_INSTANCES[env_name] = factory()
    return _BACKEND_INSTANCES[env_name]


def reset_backend(env_name=None):
    """Reset backend singletons (for tests)."""
    global _BACKEND_INSTANCES
    if env_name:
        _BACKEND_INSTANCES.pop(env_name, None)
        return
    _BACKEND_INSTANCES = {}
