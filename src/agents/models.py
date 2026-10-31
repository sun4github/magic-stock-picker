"""Turning a configured model name into something `LlmAgent(model=...)` accepts.

Its own module, with no local imports, because both `definitions.py` (which builds
the analyst) and `runtime.py` (which imports `definitions`) need it. Putting it in
runtime.py would make definitions import runtime and close a cycle.
"""


def resolve_model(name: str, **litellm_kwargs):
    """Turn a configured model name into something `LlmAgent(model=...)` accepts.

    ADK takes a bare string as a GEMINI model and resolves it against Google's API.
    So `model="glm-5.2:cloud"` would not error at construction — it would fail later,
    at the first call, against the wrong provider. Anything with a provider prefix is
    therefore routed through LiteLLM instead, which is what ADK's `LiteLlm` wrapper
    is for:

        gemini-3.6-flash              -> Gemini, handled by ADK directly
        ollama_chat/glm-5.2:cloud     -> LiteLLM -> Ollama
        anthropic/claude-sonnet-5-5   -> LiteLLM -> Anthropic
        openai/gpt-oss:120b           -> LiteLLM -> any OpenAI-compatible endpoint

    The `/` is the discriminator because that is LiteLLM's own convention for
    `provider/model`, and no Gemini model id contains one.

    `litellm_kwargs` (for example `reasoning_effort` or `max_tokens`) are passed to
    LiteLLM on every call. They are IGNORED for a bare Gemini name: a Gemini agent's
    thinking is configured through ADK, not here, and silently applying a LiteLLM
    knob to it would be a lie about what the agent is doing.

    Credentials and endpoints are LiteLLM's business, read from the environment
    (`ANTHROPIC_API_KEY`, `OLLAMA_API_BASE`, `OLLAMA_API_KEY`, ...) rather than from
    config.yaml — config is committed and keys are not.

    LiteLLM is imported lazily. It is a heavy import, and every process that touches
    `agents` would otherwise pay for it whether or not anything is pointed at it.
    """
    if "/" not in name:
        return name
    from google.adk.models.lite_llm import LiteLlm
    return LiteLlm(model=name, **litellm_kwargs)
