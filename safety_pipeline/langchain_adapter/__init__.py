"""LangChain adapter — wraps the safety_pipeline runtime as LangChain tools
and middleware so the trained sentinel can sit in front of any LangChain
agent. Tested against ``langchain==1.2.x`` + ``langgraph==1.0.x``.
"""

from .tools import build_langchain_tools
from .sentinel_middleware import SentinelMiddleware, ToolBlocked, AskHumanInterrupt
from .tool_rag_middleware import ToolRagMiddleware

__all__ = [
    "build_langchain_tools",
    "SentinelMiddleware",
    "ToolRagMiddleware",
    "ToolBlocked",
    "AskHumanInterrupt",
]
