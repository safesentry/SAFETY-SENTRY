import os
import subprocess

from ...backend_abc import EnvironmentBackend
from ...settings import REPO_ROOT, reload_runtime_env


class VaultwardenBackend(EnvironmentBackend):
    def __init__(self):
        self._vaultwarden_tools = None

    def _get_vaultwarden_tools(self):
        if self._vaultwarden_tools is not None:
            return self._vaultwarden_tools
        try:
            from ..tools import vaultwarden as vaultwarden_tools_module
        except ModuleNotFoundError as exc:
            raise RuntimeError("The current environment is missing the vaultwarden_tools module.") from exc
        self._vaultwarden_tools = vaultwarden_tools_module
        return self._vaultwarden_tools

    def get_tool_schemas(self):
        return self._get_vaultwarden_tools().get_all_schemas()

    def get_tool_names(self):
        return self._get_vaultwarden_tools().get_tool_names()

    def execute_tool(self, name, args):
        return self._get_vaultwarden_tools().call_tool(name, args)

    def reset(self):
        script_path = os.path.join(REPO_ROOT, "scripts", "reset_vaultwarden_env.sh")
        try:
            subprocess.run(["bash", script_path], cwd=REPO_ROOT, check=True)
            reload_runtime_env()
            print("[VaultwardenBackend] reset_vaultwarden_env.sh completed")
        except Exception as exc:
            print(f"[VaultwardenBackend] reset_vaultwarden_env.sh failed: {exc}")
