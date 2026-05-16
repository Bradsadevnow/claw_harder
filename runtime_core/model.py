from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import re
import time
from typing import Any
from urllib import error, request
try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(*_args: Any, **_kwargs: Any) -> bool:
        return False

load_dotenv()

from typing import Generator
from .memory import MemoryFrame
from .tools import ToolRegistry, openai_tool_spec

GenerationControls = dict[str, Any]


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    call_id: str | None = None


class ValidationError(Exception):
    pass


@dataclass
class NormalizationIssue:
    kind: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelResponse:
    text: str
    tool_calls: list[ToolCall]
    issues: list[NormalizationIssue] = field(default_factory=list)
    thought: str | None = None
    expression: dict[str, Any] | None = None


class BaseModel:
    def descriptor(self) -> dict[str, str]:
        return {"provider": "demo", "model": "demo", "base_url": ""}

    def is_available(self) -> bool:
        return False

    def generate(
        self,
        system_prompt: str,
        history: list[MemoryFrame],
        registry: ToolRegistry,
        generation_controls: GenerationControls | None = None,
    ) -> ModelResponse:
        raise NotImplementedError


class DemoModel(BaseModel):
    def descriptor(self) -> dict[str, str]:
        return {"provider": "demo", "model": "demo", "base_url": ""}

    def is_available(self) -> bool:
        return True

    def generate(
        self,
        system_prompt: str,
        history: list[MemoryFrame],
        registry: ToolRegistry,
        generation_controls: GenerationControls | None = None,
    ) -> ModelResponse:
        user_text = next((frame.content for frame in reversed(history) if frame.role == "user"), "")
        if user_text.startswith("/tool "):
            parts = user_text.split(maxsplit=2)
            tool_name = parts[1] if len(parts) > 1 else ""
            raw_args = parts[2] if len(parts) > 2 else "{}"
            arguments, arg_issues = normalize_tool_arguments(raw_args)
            return ModelResponse(
                text=f"Running tool `{tool_name}`.",
                tool_calls=[ToolCall(name=tool_name, arguments=arguments)],
                issues=arg_issues,
            )
        inline_calls, cleaned_text, inline_issues, _thought = extract_inline_tool_calls(user_text)
        if inline_calls:
            response_text = cleaned_text or f"Running tool `{inline_calls[0].name}`."
            return ModelResponse(
                text=response_text,
                tool_calls=dedupe_tool_calls(inline_calls),
                issues=inline_issues,
            )
        return ModelResponse(
            text=(
                "Demo mode is active. Set `OPENAI_BASE_URL`, `OPENAI_API_KEY`, and `OPENAI_MODEL` "
                "for a real model, or run `/tool list_tools {}` style commands to exercise the tool loop."
            ),
            tool_calls=[],
        )


class ReplayModel(BaseModel):
    """
    Deterministic replay model. 
    Returns pre-recorded responses from a log and prohibits live provider calls.
    """
    def __init__(self, responses: list[ModelResponse] | None = None) -> None:
        self.responses = responses or []
        self._cursor = 0

    def descriptor(self) -> dict[str, str]:
        return {"provider": "replay", "model": "deterministic-replay", "base_url": "N/A"}

    def is_available(self) -> bool:
        return True

    def generate(
        self,
        system_prompt: str,
        history: list[MemoryFrame],
        registry: ToolRegistry,
        generation_controls: GenerationControls | None = None,
    ) -> ModelResponse:
        if self._cursor >= len(self.responses):
            raise RuntimeError(f"ReplayModel exhaustion: no more logged responses (requested at history depth {len(history)})")
        
        response = self.responses[self._cursor]
        self._cursor += 1
        return response

    def complete(self, call_id: str | None = None) -> ModelResponse:
        """Alias for consumption by specific replay implementations."""
        # Note: If we use call_id, we'd look up in a dict instead of a list
        return self.generate("", [], None) # type: ignore


class OpenAICompatibleModel(BaseModel):
    def __init__(
        self, 
        base_url: str, 
        api_key: str, 
        model: str, 
        provider: str = "openai-compatible",
        use_native_tools: bool = True
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.provider = provider
        self.use_native_tools = use_native_tools

    def descriptor(self) -> dict[str, str]:
        return {
            "provider": self.provider, 
            "model": self.model, 
            "base_url": self.base_url,
            "native_tools": str(self.use_native_tools)
        }

    def is_available(self) -> bool:
        req = request.Request(
            url=f"{self.base_url}/models",
            headers={"Authorization": f"Bearer {self.api_key}"},
            method="GET",
        )
        try:
            with request.urlopen(req, timeout=5):
                return True
        except Exception:
            return False

    def generate(
        self,
        system_prompt: str,
        history: list[MemoryFrame],
        registry: ToolRegistry,
        generation_controls: GenerationControls | None = None,
    ) -> ModelResponse:
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        for frame in history:
            messages.append({"role": frame.role, "content": frame.content})

        tool_specs = [openai_tool_spec(spec) for spec in registry.list_specs()]
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": 2048,
        }
        controls = generation_controls or {}
        raw_temperature = controls.get("temperature")
        if isinstance(raw_temperature, (int, float)) and not isinstance(raw_temperature, bool):
            payload["temperature"] = max(0.0, min(2.0, float(raw_temperature)))
        sanitized_logit_bias = sanitize_logit_bias(controls.get("logit_bias"))
        if sanitized_logit_bias:
            payload["logit_bias"] = sanitized_logit_bias
        if tool_specs and self.use_native_tools:
            payload["tools"] = tool_specs
            payload["tool_choice"] = "auto"
        req = request.Request(
            url=f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        body = None
        for attempt in range(4):
            try:
                with request.urlopen(req, timeout=120) as response:
                    body = json.loads(response.read().decode("utf-8"))
                break
            except error.HTTPError as exc:
                if exc.code == 429 and attempt < 3:
                    time.sleep(2 ** attempt * 5)  # 5s, 10s, 20s
                    continue
                err_body = exc.read().decode("utf-8")
                raise RuntimeError(f"Model request failed (HTTP {exc.code}): {err_body}") from exc
            except error.URLError as exc:
                raise RuntimeError(f"Model request failed: {exc}") from exc

        if "choices" not in body:
            raise RuntimeError(f"Model response missing 'choices'. Response: {json.dumps(body)}")

        message = body["choices"][0]["message"]
        text = normalize_message_text(message.get("content"))
        tool_calls: list[ToolCall] = []
        
        for call in message.get("tool_calls", []):
            name = normalize_tool_name(call["function"].get("name", ""))
            if not name:
                raise ValidationError("Model emitted a tool call without a usable tool name.")
            parsed_args, _ = normalize_tool_arguments(call["function"].get("arguments", "{}"))
            tool_calls.append(
                ToolCall(
                    name=name,
                    arguments=parsed_args,
                    call_id=call.get("id"),
                )
            )

        inline_calls, cleaned_text, _, thought = extract_inline_tool_calls(text)
        tool_calls.extend(inline_calls)
        text = cleaned_text

        return ModelResponse(text=text, tool_calls=dedupe_tool_calls(tool_calls), thought=thought)

    def stream_text(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        collected_tool_calls: list[ToolCall] | None = None,
        registry: ToolRegistry | None = None,
    ) -> Generator[str, None, None]:
        """
        Stream a model response. Yields text deltas as they arrive.

        If collected_tool_calls is provided and registry is provided, tool specs are
        included in the request and any tool call fragments are assembled and appended
        to collected_tool_calls when the stream closes. When collect_tool_calls is None,
        tool specs are omitted (prose-only pass).
        """
        full_messages = [{"role": "system", "content": system_prompt}, *messages]
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": full_messages,
            "max_tokens": 600,
            "stream": True,
        }
        if collected_tool_calls is not None and registry is not None and self.use_native_tools:
            tool_specs = [openai_tool_spec(spec) for spec in registry.list_specs()]
            if tool_specs:
                payload["tools"] = tool_specs
                payload["tool_choice"] = "auto"

        req = request.Request(
            url=f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        # Retry 429 before the stream opens — once streaming starts, no retries.
        response = None
        for attempt in range(4):
            try:
                response = request.urlopen(req, timeout=300)
                break
            except error.HTTPError as exc:
                if exc.code == 429 and attempt < 3:
                    time.sleep(2 ** attempt * 5)
                    continue
                err_body = exc.read().decode("utf-8")
                raise RuntimeError(f"Streaming request failed (HTTP {exc.code}): {err_body}") from exc
            except error.URLError as exc:
                raise RuntimeError(f"Streaming request failed: {exc}") from exc

        if response is None:
            return

        # Tool call fragment accumulator (index → partial data)
        tc_frags: dict[int, dict[str, str]] = {}

        with response:
            for raw_line in response:
                line = raw_line.decode("utf-8").strip()
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                choices = chunk.get("choices", [])
                if not choices:
                    continue
                delta = choices[0].get("delta", {})

                content = delta.get("content")
                if content:
                    yield content

                for tc_chunk in delta.get("tool_calls", []):
                    idx = tc_chunk.get("index", 0)
                    frag = tc_frags.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                    frag["id"] += tc_chunk.get("id") or ""
                    fn = tc_chunk.get("function", {})
                    frag["name"] += fn.get("name") or ""
                    frag["arguments"] += fn.get("arguments") or ""

        if collected_tool_calls is not None and tc_frags:
            for frag in sorted(tc_frags.values(), key=lambda f: f["id"]):
                name = normalize_tool_name(frag["name"])
                if not name:
                    continue
                args, _ = normalize_tool_arguments(frag["arguments"])
                collected_tool_calls.append(
                    ToolCall(name=name, arguments=args, call_id=frag["id"] or None)
                )


PROVIDER_PRESETS = {
    "xai": {
        "base_url": "https://api.x.ai/v1",
        "api_key_env": "XAI_API_KEY",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
    },
    "openai-compatible": {
        "base_url": "",
        "api_key_env": "OPENAI_API_KEY",
        "api_key_default": "sk-local",
    },
    # LM Studio runs a local OpenAI-compatible server; no real API key required.
    "lmstudio": {
        "base_url": "http://localhost:1234/v1",
        "api_key_env": "LMSTUDIO_API_KEY",
        "api_key_default": "lm-studio",
    },
}


def load_model(
    provider: str = "auto",
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    api_key_env: str | None = None,
    use_native_tools: bool | None = None,
) -> BaseModel:
    if provider == "demo":
        return DemoModel()

    if provider == "auto":
        env_base_url = os.environ.get("OPENAI_BASE_URL")
        env_api_key = os.environ.get("OPENAI_API_KEY")
        env_model = os.environ.get("OPENAI_MODEL")
        if env_base_url and env_api_key and env_model:
            return OpenAICompatibleModel(
                base_url=env_base_url,
                api_key=env_api_key,
                model=env_model,
                provider="openai-compatible",
                use_native_tools=use_native_tools if use_native_tools is not None else True,
            )
        xai_api_key = os.environ.get("XAI_API_KEY")
        xai_model = os.environ.get("XAI_MODEL") or os.environ.get("OPENAI_MODEL")
        if xai_api_key and xai_model:
            return OpenAICompatibleModel(
                base_url=PROVIDER_PRESETS["xai"]["base_url"],
                api_key=xai_api_key,
                model=xai_model,
                provider="xai",
            )
        return DemoModel()

    preset = PROVIDER_PRESETS.get(provider)
    if preset is None:
        raise ValueError(f"Unknown provider: {provider}")

    resolved_base_url = base_url or preset["base_url"] or os.environ.get("OPENAI_BASE_URL", "")
    resolved_api_key_env = api_key_env or preset.get("api_key_env", "")
    resolved_api_key = (
        api_key
        or (os.environ.get(resolved_api_key_env, "") if resolved_api_key_env else "")
        or preset.get("api_key_default", "")
    )
    resolved_model = model or os.environ.get("RUNTIME_MODEL") or os.environ.get("OPENAI_MODEL") or os.environ.get("XAI_MODEL")

    if not resolved_base_url:
        raise ValueError(f"Provider '{provider}' requires a base URL. Pass --base-url or set OPENAI_BASE_URL.")
    if not resolved_api_key:
        raise ValueError(
            f"Provider '{provider}' requires an API key. Set {resolved_api_key_env} or pass --api-key-env with a populated env var."
        )
    if not resolved_model:
        raise ValueError(
            f"Provider '{provider}' requires a model name. Pass --model or set RUNTIME_MODEL."
        )

    return OpenAICompatibleModel(
        base_url=resolved_base_url,
        api_key=resolved_api_key,
        model=resolved_model,
        provider=provider,
        use_native_tools=use_native_tools if use_native_tools is not None else (provider != "lmstudio")
    )


def load_model_from_env() -> BaseModel:
    return load_model(provider="auto")


class ModelManager(BaseModel):
    """Named model registry that behaves as a single BaseModel.

    The active model handles all generate() calls. Switch it at runtime
    without restarting the runtime or reloading state.
    """

    def __init__(self, models: dict[str, BaseModel], default: str | None = None) -> None:
        if not models:
            raise ValueError("ModelManager requires at least one model.")
        self._models: dict[str, BaseModel] = dict(models)
        first = next(iter(models))
        self._active: str = default if default in models else first

    @property
    def active_name(self) -> str:
        return self._active

    def names(self) -> list[str]:
        return list(self._models)

    def get(self, name: str) -> BaseModel:
        if name not in self._models:
            raise ValueError(f"Unknown model {name!r}. Available: {sorted(self._models)}")
        return self._models[name]

    def switch(self, name: str) -> None:
        if name not in self._models:
            raise ValueError(f"Unknown model {name!r}. Available: {sorted(self._models)}")
        self._active = name

    def descriptor(self) -> dict[str, str]:
        desc = self._models[self._active].descriptor()
        desc["manager_active"] = self._active
        return desc

    def is_available(self) -> bool:
        return self._models[self._active].is_available()

    def health_check(self) -> dict[str, bool]:
        return {name: model.is_available() for name, model in self._models.items()}

    def generate(
        self,
        system_prompt: str,
        history: list[MemoryFrame],
        registry: ToolRegistry,
        generation_controls: GenerationControls | None = None,
    ) -> ModelResponse:
        return self._models[self._active].generate(
            system_prompt,
            history,
            registry,
            generation_controls=generation_controls,
        )


def sanitize_logit_bias(raw_logit_bias: Any) -> dict[str, float]:
    if not isinstance(raw_logit_bias, dict):
        return {}
    sanitized: dict[str, float] = {}
    for key, value in raw_logit_bias.items():
        key_text = str(key).strip()
        if not key_text:
            continue
        try:
            token_id = int(key_text)
        except (TypeError, ValueError):
            continue
        if token_id < 0:
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        bias = max(-100.0, min(100.0, float(value)))
        sanitized[str(token_id)] = bias
    return sanitized

def load_manager_from_config(path: "str | Path") -> ModelManager:
    """Load a ModelManager from a JSON config file.

    Config format::

        {
          "default": "local",
          "models": {
            "local": {"provider": "lmstudio", "model": "meta-llama-3.1-8b-instruct"},
            "gpt":   {"provider": "openai",   "model": "gpt-4o-mini"}
          }
        }
    """
    from pathlib import Path as _Path
    config_path = _Path(path)
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"Model config not found: {config_path}")
    if not isinstance(payload, dict):
        raise ValueError("Model config must be a JSON object.")

    raw_models = payload.get("models", {})
    if not isinstance(raw_models, dict) or not raw_models:
        raise ValueError("Model config must have a non-empty 'models' object.")

    models: dict[str, BaseModel] = {}
    for name, spec in raw_models.items():
        if not isinstance(spec, dict):
            raise ValueError(f"Model spec for '{name}' must be an object.")
        models[name] = load_model(
            provider=spec.get("provider", "auto"),
            model=spec.get("model"),
            base_url=spec.get("base_url"),
            api_key=spec.get("api_key"),
            api_key_env=spec.get("api_key_env"),
        )

    default = payload.get("default")
    if not isinstance(default, str):
        default = None
    return ModelManager(models=models, default=default)


def normalize_message_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif "text" in item:
                    parts.append(str(item["text"]))
                else:
                    parts.append(json.dumps(item, ensure_ascii=True))
            else:
                parts.append(str(item))
        return "\n".join(part.strip() for part in parts if str(part).strip())
    return str(content).strip()


def _extract_balanced_json(text: str, start: int) -> str | None:
    """Extract a balanced JSON object from `text` beginning at `start`.

    Handles arbitrarily nested objects by counting brace depth rather than
    using a greedy/lazy regex, which fails on nested structures.
    """
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def normalize_tool_name(name: str) -> str:
    cleaned = name.strip().strip("`").replace("functions.", "")
    return cleaned


def _strip_empty_keys(args: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in args.items() if k.strip()}


def extract_balanced_json_objects(text: str) -> list[str]:
    objects: list[str] = []
    start: int | None = None
    depth = 0
    for index, char in enumerate(text):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start is not None:
                objects.append(text[start : index + 1])
                start = None
    return objects


def extract_inline_tool_calls(text: str) -> tuple[list[ToolCall], str, list[NormalizationIssue], str | None]:
    calls: list[ToolCall] = []
    issues: list[NormalizationIssue] = []
    
    # 1. Extract thought/commentary blocks using multiple patterns
    # Support standard tags and the model's specific 'channel' patterns
    thought_patterns = [
        r"<\|(?:thought|reasoning|commentary)\|>(.*?)(?:<\|/(?:thought|reasoning|commentary)\|>|$)",
        r"<(?:thought|reasoning|commentary)>(.*?)(?:</(?:thought|reasoning|commentary)>|$)",
        r"<\|channel\|>.*?\|thought>(.*?)(?:<\|/assistant\|>|$)",
        r"<\|channel\|>analysis\|commentary\|>(.*?)(?:<\||$)",
        r"commentary to=\S+\s+(.*?)(?=\n\{|$)",
        r"Thought:\s*(.*?)(?=\n\{|\n\n|$)"
    ]
    
    thought = None
    for pattern in thought_patterns:
        match = re.search(pattern, text, flags=re.DOTALL | re.IGNORECASE)
        if match:
            thought = match.group(1).strip()
            break
    
    # 2. Imperative CALL_TOOL recovery (legacy fallback)
    stripped = text.strip()
    imperative_match = re.fullmatch(r"CALL_TOOL\s+(\S+)(?:\s+(.*))?", stripped, flags=re.IGNORECASE)
    if imperative_match:
        tool_name = normalize_tool_name(imperative_match.group(1))
        raw_arguments = imperative_match.group(2) if imperative_match.group(2) is not None else "{}"
        arguments, arg_issues = normalize_tool_arguments(raw_arguments)
        if tool_name:
            calls.append(ToolCall(name=tool_name, arguments=arguments))
            issues.append(NormalizationIssue(kind="imperative_tool_intent", message="Recovered imperative CALL_TOOL syntax."))
            issues.extend(arg_issues)
            return dedupe_tool_calls(calls), "", issues, thought

    # 3a. Handle LM Studio constrained-output format inside thoughts:
    #     <|channel|>commentary to=TOOL_NAME ... <|constrain|>json<|message|>{"arg":"val"}
    if thought:
        # Check for specific 'to=TOOL_NAME' in the thought header
        to_match = re.search(r"commentary to=(\S+)", text)
        constrain_match = re.search(r"<\|constrain\|>json<\|message\|>(\{[^}]*\})", text)
        
        if constrain_match:
            payload = try_parse_json_object(constrain_match.group(1))
            if isinstance(payload, dict):
                tool_name = None
                if to_match:
                    tool_name = normalize_tool_name(to_match.group(1))
                elif "tool" in payload:
                    tool_name = normalize_tool_name(str(payload["tool"]))
                elif "key" in payload:
                    tool_name = "set_fact" if "value" in payload else "get_fact"
                
                if tool_name:
                    # If the payload contains 'arguments' key, use that; otherwise use the whole payload
                    arguments = payload.get("arguments", payload) if "tool" in payload else payload
                    arguments, arg_issues = normalize_tool_arguments(arguments)
                    
                    calls.append(ToolCall(name=tool_name, arguments=arguments))
                    issues.append(NormalizationIssue(kind="constrained_channel_intent", message=f"Recovered {tool_name} from model's constrained channel."))
                    issues.extend(arg_issues)

    # 3. Extract JSON tool calls BEFORE aggressive tag stripping
    # This ensures that structural tags around JSON (like <|constrain|>) don't wipe the payload
    temp_text = text
    for candidate in extract_balanced_json_objects(text):
        payload = try_parse_json_object(candidate)
        if isinstance(payload, dict) and "tool" in payload:
            tool_name = normalize_tool_name(str(payload.get("tool", "")))
            if tool_name:
                arguments, arg_issues = normalize_tool_arguments(payload.get("arguments", {}))
                calls.append(ToolCall(name=tool_name, arguments=arguments))
                issues.append(NormalizationIssue(kind="inline_tool_intent", message="Recovered tool call from JSON payload."))
                issues.extend(arg_issues)
                temp_text = temp_text.replace(candidate, " __TOOL_CALL__ ", 1)

    # 4. Clean up structural tags to isolate assistant prose
    cleaned = temp_text
    # Strip thought blocks we already extracted
    for pattern in thought_patterns:
        cleaned = re.sub(pattern, " ", cleaned, flags=re.DOTALL | re.IGNORECASE)
    
    # Strip known structural tags
    cleaned = re.sub(r"<\|.*?\|>", " ", cleaned)
    cleaned = re.sub(r"<\|/.*?\|>", " ", cleaned)
    cleaned = re.sub(r"<(?:thought|reasoning|commentary)>.*?</(?:thought|reasoning|commentary)>", " ", cleaned, flags=re.DOTALL | re.IGNORECASE)
    
    # Remove specific model boilerplate
    cleaned = re.sub(r"to=remember", " ", cleaned)
    cleaned = re.sub(r"json\s+", " ", cleaned)
    cleaned = cleaned.replace("__TOOL_CALL__", " ")
    
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return dedupe_tool_calls(calls), cleaned, issues, thought


def normalize_tool_arguments(raw_arguments: Any) -> tuple[dict[str, Any], list[NormalizationIssue]]:
    issues: list[NormalizationIssue] = []
    if isinstance(raw_arguments, dict):
        return raw_arguments, issues
    if raw_arguments is None:
        issues.append(NormalizationIssue(
            kind="missing_arguments",
            message="Tool call omitted arguments; defaulting to empty object.",
        ))
        return {}, issues
    if not isinstance(raw_arguments, str):
        issues.append(NormalizationIssue(
            kind="non_object_arguments",
            message="Tool arguments were not a JSON object; defaulting to empty object.",
            details={"type": type(raw_arguments).__name__},
        ))
        return {}, issues

    raw_text = raw_arguments.strip()
    if not raw_text:
        return {}, issues

    # Salvage JSON from markdown code blocks
    code_block_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
    if code_block_match:
        raw_text = code_block_match.group(1).strip()

    parsed = try_parse_json_object(raw_text)
    if isinstance(parsed, dict):
        return _strip_empty_keys(parsed), issues
    if parsed is not None:
        issues.append(NormalizationIssue(
            kind="non_object_arguments",
            message="Tool arguments parsed, but not as a JSON object; defaulting to empty object.",
            details={"value_type": type(parsed).__name__},
        ))
        return {}, issues

    for candidate in extract_balanced_json_objects(raw_text):
        candidate_parsed = try_parse_json_object(candidate)
        if isinstance(candidate_parsed, dict):
            issues.append(NormalizationIssue(
                kind="salvaged_arguments",
                message="Recovered tool arguments from mixed prose/JSON output.",
            ))
            return _strip_empty_keys(candidate_parsed), issues

    issues.append(NormalizationIssue(
        kind="invalid_json_arguments",
        message="Tool arguments were malformed JSON; defaulting to empty object.",
        details={"raw": raw_text[:240]},
    ))
    return {}, issues


def try_parse_json_object(raw_text: str) -> Any:
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        return None


def dedupe_tool_calls(tool_calls: list[ToolCall]) -> list[ToolCall]:
    seen: set[tuple[str, str]] = set()
    deduped: list[ToolCall] = []
    for call in tool_calls:
        key = (call.name, json.dumps(call.arguments, sort_keys=True, ensure_ascii=True))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(call)
    return deduped
