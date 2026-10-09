import os
import subprocess

from ...backend_abc import EnvironmentBackend
from ...settings import REPO_ROOT, reload_runtime_env


class ERPNextBackend(EnvironmentBackend):
    def __init__(self):
        self._erpnext_tools = None

    def _get_erpnext_tools(self):
        if self._erpnext_tools is not None:
            return self._erpnext_tools
        try:
            from ..tools import erpnext as erpnext_tools_module
        except ModuleNotFoundError as exc:
            raise RuntimeError("The current environment is missing the erpnext_tools module.") from exc
        self._erpnext_tools = erpnext_tools_module
        return self._erpnext_tools

    def get_tool_schemas(self):
        return self._get_erpnext_tools().get_all_schemas()

    def get_tool_names(self):
        return self._get_erpnext_tools().get_tool_names()

    def execute_tool(self, name, args):
        return self._get_erpnext_tools().call_tool(name, args)

    def reset(self):
        script_path = os.path.join(REPO_ROOT, "scripts", "reset_erpnext_env.sh")
        try:
            subprocess.run(["bash", script_path], cwd=REPO_ROOT, check=True)
            reload_runtime_env()
            print("[ERPNextBackend] reset_erpnext_env.sh completed")
        except Exception as exc:
            print(f"[ERPNextBackend] reset_erpnext_env.sh failed: {exc}")
            raise RuntimeError("ERPNext reset failed; the environment was not restored to a known-good baseline.") from exc
