"""
Shared abstraction for service tool registration.

Each service tools module should expose a uniform interface:
- get_all_schemas()
- get_tool_names()
- call_tool(name, args)

This keeps runtime and backend code independent from service-specific
registration details.
"""

from dataclasses import dataclass

from .exceptions import ToolExecutionError


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    schema: dict
    handler: object


class ServiceToolRegistry:
    def __init__(self, service_id):
        self.service_id = service_id
        self._tools = {}

    def register(
        self,
        name,
        description,
        params,
        required=None,
        is_write=False,
        group="",
        short_description="",
    ):
        def decorator(func):
            if required is None:
                import inspect

                sig = inspect.signature(func)
                req = [
                    p for p, v in sig.parameters.items()
                    if v.default is inspect.Parameter.empty
                ]
            else:
                req = list(required)

            schema = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": params,
                        "required": req,
                    },
                },
            }
            self._tools[name] = RegisteredTool(
                name=name,
                schema=schema,
                handler=func,
            )
            return func

        return decorator

    def get_all_schemas(self):
        return [tool.schema for tool in self._tools.values()]

    def get_tool_names(self):
        return list(self._tools.keys())

    def call_tool(self, name, args):
        tool = self._tools.get(name)
        if not tool:
            raise ToolExecutionError(f"[Error] Unknown tool: {name}")
        try:
            return tool.handler(**args)
        except ToolExecutionError:
            raise
        except Exception as exc:
            raise ToolExecutionError(f"[Execution Error] {type(exc).__name__}: {exc}") from exc
