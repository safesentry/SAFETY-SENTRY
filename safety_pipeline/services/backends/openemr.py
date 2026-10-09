import os
import subprocess

from ...backend_abc import EnvironmentBackend
from ...settings import REPO_ROOT, reload_runtime_env


class OpenEMRBackend(EnvironmentBackend):
    def __init__(self):
        self._openemr_tools = None

    def _get_openemr_tools(self):
        if self._openemr_tools is not None:
            return self._openemr_tools
        try:
            from ..tools import openemr as openemr_tools_module
        except ModuleNotFoundError as exc:
            raise RuntimeError("The current environment is missing the openemr_tools module.") from exc
        self._openemr_tools = openemr_tools_module
        return self._openemr_tools

    def get_tool_schemas(self):
        return self._get_openemr_tools().get_all_schemas()

    def get_tool_names(self):
        return self._get_openemr_tools().get_tool_names()

    def execute_tool(self, name, args):
        return self._get_openemr_tools().call_tool(name, args)

    def reset(self):
        script_path = os.path.join(REPO_ROOT, "scripts", "reset_openemr_env.sh")
        try:
            subprocess.run(["bash", script_path], cwd=REPO_ROOT, check=True)
            reload_runtime_env()
            print("[OpenEMRBackend] reset_openemr_env.sh completed")
        except Exception as exc:
            print(f"[OpenEMRBackend] reset_openemr_env.sh failed: {exc}")
