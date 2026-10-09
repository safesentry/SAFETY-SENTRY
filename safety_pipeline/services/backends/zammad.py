import os
import subprocess

from ...backend_abc import EnvironmentBackend
from ...settings import REPO_ROOT, reload_runtime_env


class ZammadBackend(EnvironmentBackend):
    def __init__(self):
        self._zammad_tools = None

    def _get_zammad_tools(self):
        if self._zammad_tools is not None:
            return self._zammad_tools
        try:
            from ..tools import zammad as zammad_tools_module
        except ModuleNotFoundError as exc:
            raise RuntimeError("The current environment is missing the zammad_tools module.") from exc
        self._zammad_tools = zammad_tools_module
        return self._zammad_tools

    def get_tool_schemas(self):
        return self._get_zammad_tools().get_all_schemas()

    def get_tool_names(self):
        return self._get_zammad_tools().get_tool_names()

    def execute_tool(self, name, args):
        return self._get_zammad_tools().call_tool(name, args)

    def reset(self):
        script_path = os.path.join(REPO_ROOT, "scripts", "reset_zammad_env.sh")
        try:
            subprocess.run(["bash", script_path], cwd=REPO_ROOT, check=True)
            reload_runtime_env()
            print("[ZammadBackend] reset_zammad_env.sh completed")
        except Exception as exc:
            print(f"[ZammadBackend] reset_zammad_env.sh failed: {exc}")
