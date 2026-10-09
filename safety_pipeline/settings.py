import os


GENERATED_ENV_FILES = (
    ".env.gitea.generated",
    ".env.vaultwarden.generated",
    ".env.erpnext.generated",
    ".env.mailu.generated",
    ".env.nocodb.generated",
    ".env.openemr.generated",
    ".env.owncloud.generated",
    ".env.rocketchat.generated",
    ".env.zammad.generated",
)

REPO_ROOT = os.path.dirname(os.path.dirname(__file__))

def _load_env_file(path, skip_keys=None):
    skip_keys = set(skip_keys or [])
    if not os.path.isfile(path):
        return
    with open(path) as env_file:
        for line in env_file:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                key = key.strip()
                if key in skip_keys:
                    continue
                os.environ[key] = val.strip()


# Automatically load environment files from the repository root.
def reload_runtime_env():
    _load_env_file(os.path.join(REPO_ROOT, ".env"))
    for file_name in GENERATED_ENV_FILES:
        _load_env_file(os.path.join(REPO_ROOT, file_name), skip_keys={"PIPELINE_ENV"})


reload_runtime_env()

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "your_openai_api_key")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", None)
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.4")
# Optional cheaper model for the pass1 agent loop (and the canonical_step
# composer). Pass 2a / Pass 2b stay on OPENAI_MODEL for labeling quality.
# Falls back to OPENAI_MODEL when unset, so existing single-model setups
# keep working.
OPENAI_MODEL_PASS1 = os.environ.get("OPENAI_MODEL_PASS1") or OPENAI_MODEL
OPENAI_EMBEDDING_MODEL = os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
OPENAI_MAX_TOKENS = int(os.environ.get("OPENAI_MAX_TOKENS", "4096"))
DEFAULT_PIPELINE_ENV = os.environ.get("PIPELINE_ENV", "gitea")
MAX_DIALOGUE_SUMMARY_CHARS = 400
MAX_AGENT_TOOL_ROUNDS = 20

ARTIFACTS_DIR = os.path.join(REPO_ROOT, "artifacts")
TRACE_SESSION_PATH = os.path.join(ARTIFACTS_DIR, "trace_sessions.jsonl")
DECISION_TOKEN_SFT_PATH = os.path.join(ARTIFACTS_DIR, "decision_token_sft.json")
TOOL_RAG_CACHE_DIR = os.path.join(ARTIFACTS_DIR, "tool_rag_cache")
TOOL_RAG_TOP_K = int(os.environ.get("TOOL_RAG_TOP_K", "12"))
TOOL_SEARCH_TOP_K = int(os.environ.get("TOOL_SEARCH_TOP_K", "8"))


def get_pipeline_env():
    return os.environ.get("PIPELINE_ENV", DEFAULT_PIPELINE_ENV)


def set_pipeline_env(env_name):
    os.environ["PIPELINE_ENV"] = env_name
