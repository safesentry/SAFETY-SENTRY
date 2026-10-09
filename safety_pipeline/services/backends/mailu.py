"""Mailu mail-service backend."""

import os
import subprocess

from ...backend_abc import EnvironmentBackend
from ...settings import REPO_ROOT, reload_runtime_env


class MailuBackend(EnvironmentBackend):
    def __init__(self):
        self._mailu_tools = None

    def _get_mailu_tools(self):
        if self._mailu_tools is not None:
            if hasattr(self._mailu_tools, "refresh_runtime_config"):
                self._mailu_tools.refresh_runtime_config()
            return self._mailu_tools
        try:
            from ..tools import mailu as mailu_tools_module
        except ModuleNotFoundError as exc:
            raise RuntimeError("The current environment is missing the mailu_tools module.") from exc
        self._mailu_tools = mailu_tools_module
        if hasattr(self._mailu_tools, "refresh_runtime_config"):
            self._mailu_tools.refresh_runtime_config()
        return self._mailu_tools

    def get_tool_schemas(self):
        return self._get_mailu_tools().get_all_schemas()

    def get_tool_names(self):
        return self._get_mailu_tools().get_tool_names()

    def execute_tool(self, name, args):
        return self._get_mailu_tools().call_tool(name, args)

    def reset(self):
        script_path = os.path.join(REPO_ROOT, "scripts", "reset_mailu_env.sh")
        try:
            subprocess.run(["bash", script_path], cwd=REPO_ROOT, check=True)
            reload_runtime_env()
            print("[MailuBackend] reset_mailu_env.sh completed")
        except Exception as exc:
            print(f"[MailuBackend] reset_mailu_env.sh failed: {exc}")
