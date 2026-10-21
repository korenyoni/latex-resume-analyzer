"""Deep agent construction.

The agent runs against an OpenAI-compatible endpoint (OpenRouter by default) via
langchain-openai's ChatOpenAI, and loads skills from .agents/skills/ through
a FilesystemBackend rooted at the project directory.
"""
from __future__ import annotations

from deepagents import FilesystemPermission, create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.middleware import SkillsMiddleware
from langchain_openai import ChatOpenAI

from app.config import Settings
from app.tools import make_extract_pdf_tool

# Uploaded documents are untrusted: never let the model touch env files.
# Matches /.env, /.env.local, /uploads/.env.x, /config/.env.local, etc.
_DENY_ENV_RULES = [
    FilesystemPermission(
        operations=["read", "write", "edit"],
        paths=["/.env*", "/**/.env*"],
        mode="deny",
    ),
]


def build_model(settings: Settings) -> ChatOpenAI:
    """Chat model pointing at the configured OpenAI-compatible endpoint."""
    return ChatOpenAI(
        model=settings.llm.model,
        base_url=settings.llm.base_url,
        # The OpenAI client rejects an empty key at construction time, so use a
        # placeholder until the real token is configured; it is never sent.
        api_key=settings.llm.api_key or "missing-api-key",
        temperature=settings.llm.temperature,
    )


def build_backend(settings: Settings) -> FilesystemBackend:
    """Filesystem sandbox rooted at the project directory.

    Virtual path "/" maps to the project root, so skills live at
    "/.agents/skills/" and uploaded PDFs at "/uploads/".
    """
    return FilesystemBackend(root_dir=str(settings.project_root), virtual_mode=True)


def build_agent(settings: Settings):
    """Create the deep agent with skills loaded from the skills directory.

    The SkillsMiddleware is constructed explicitly (rather than via the
    `skills=` shortcut) so skill tools such as `extract_pdf` can be gated
    behind their skill's `metadata.include_tools`.
    """
    backend = build_backend(settings)
    skills_middleware = SkillsMiddleware(
        backend=backend,
        sources=[settings.agent.skills_path],
        tools=[make_extract_pdf_tool(settings.project_root)],
    )
    return create_deep_agent(
        model=build_model(settings),
        system_prompt=settings.agent.system_prompt,
        backend=backend,
        middleware=[skills_middleware],
        permissions=_DENY_ENV_RULES,
    )


def run_agent(agent, prompt: str) -> str:
    """Invoke the agent with a user prompt and return the final text."""
    result = agent.invoke({"messages": [{"role": "user", "content": prompt}]})
    content = result["messages"][-1].content
    if isinstance(content, list):  # content blocks -> plain text
        content = "".join(
            block.get("text", "") for block in content if isinstance(block, dict)
        )
    return str(content)
