"""Keep ordinary regression tests independent of workstation credentials.

Load this before collecting application modules: core.llm calls load_dotenv at
import time, which otherwise walks out of an isolated worktree into its parent.
Live integration tests opt in only through NEO4J_TEST_* / QDRANT_TEST_* settings;
unit tests inject any application configuration they need with monkeypatch.
"""

import os


os.environ["PYTHON_DOTENV_DISABLED"] = "1"
for _name in tuple(os.environ):
    if _name.startswith(("NEO4J_TEST_", "QDRANT_TEST_")):
        continue
    if _name.startswith(("LLM_", "OPENAI_", "DEEPSEEK_", "NEO4J_", "QDRANT_", "RAG_", "MEMORY_EMBEDDING_")):
        os.environ.pop(_name, None)
del _name
