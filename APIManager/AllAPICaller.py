"""
Module: call_llm_api.py

Description:
    This module defines the CallLLMApi class, which is used to interact with various Language Model (LLM) APIs such as OpenAI, Anthropic, and Google.
    It provides methods to call the LLM service API with a provided prompt and retrieve the generated text.

Classes:
    - CallLLMApi: Class to call Language Model (LLM) APIs.

Attributes:
    - llmService (str): The LLM service to be used (e.g., "OpenAI", "Anthropic").
    - api_temp_dict (dict): The API dictionary containing endpoint, headers, and payload.

Methods:
    - __init__(self, llmService="OpenAI"): Initializes an instance of CallLLMApi class.
    - __set_apidict__(self, llmService): Set up the API dictionary based on the specified LLM service.
    - CallService(self, prompt: str) -> str: Call the LLM service API with the provided prompt and return the generated text.
"""

import json
import logging
import subprocess
import time
import requests
import yaml
from APIManager.circuit_breaker import get_breaker
from Utilities.base_utils import get_config_val

logger = logging.getLogger(__name__)

# Errors caused by the request itself (one user's prompt), not the provider:
# they don't count towards opening its circuit breaker.
_REQUEST_ERROR_STATUS = {400, 413, 422}


class LLMRequestError(ValueError):
    """A provider answered with a non-200 status."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


def _record_error(breaker, exc: Exception) -> None:
    if getattr(exc, "status_code", None) in _REQUEST_ERROR_STATUS:
        breaker.abandon()
    else:
        breaker.record_failure()

_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.0          # seconds; doubles on each attempt
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_REQUEST_TIMEOUT = 60            # seconds before a hung request is aborted


class CallLLMApi:
    """
    Class to call Language Model (LLM) APIs.

    Attributes:
        llmService (str): The LLM service to be used (e.g., "OpenAI", "Anthropic").
        api_temp_dict (dict): The API dictionary containing endpoint, headers, and payload.
    """
    def __init__(self, llmService="OpenAI", model=None):
        """
        Initializes an instance of CallLLMApi class.

        Args:
            llmService (str, optional): The LLM service to be used. Defaults to "OpenAI".
            model (str, optional): Model name override. When provided, overrides the
                model_name from model_access_config.YAML for this call only.
        """
        self.llmService = llmService
        self.model_override = model
        self.api_temp_dict = None
        self.__set_apidict__(llmService)

    def __set_apidict__(self, llmService):
        """
        Set up the API dictionary based on the specified LLM service.

        Args:
            llmService (str): The LLM service.

        Returns:
            dict: The API dictionary.
        """

        model_config_path = get_config_val("model_config", ["model_config","path"])

        with open(model_config_path, "r") as model_config_FObj:
            full_config = yaml.load(model_config_FObj, yaml.FullLoader)
            model_config = full_config.get(str(llmService).upper(), {})

        # Resolve effective model: explicit override wins, else YAML default
        effective_model = self.model_override or model_config.get("model_name", "")

        # CLI-based providers — no HTTP template needed
        if llmService.lower() == "claude_code":
            self.api_temp_dict = {"model": effective_model or "claude-sonnet-4-5"}
            return

        if llmService.lower() == "codex_cli":
            self.api_temp_dict = {"model": effective_model or "codex-mini-latest"}
            return

        # Load API calling template for HTTP-based providers
        with open(model_config["api_template"], "r") as api_temp_fobj:
            api_temp_str = api_temp_fobj.read()
            api_temp_str = api_temp_str.replace("<<api_key>>", model_config["api_key"])
            api_temp_str = api_temp_str.replace("<<model>>", effective_model)

        # Convert API template string to dictionary
        self.api_temp_dict = json.loads(api_temp_str)


    def CallService(self, prompt: str) -> str:
        """
        Call the LLM service API with the provided prompt.

        Args:
            prompt (str): The prompt text.

        Returns:
            str: Generated text.

        Raises:
            ValueError: If the API call fails.
        """
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string.")

        # Circuit breaker: fail fast (CircuitOpenError) while the provider is
        # paused. Any error raised by the call itself — transport error,
        # timeout, HTTP 5xx/429, 401/403 and other 4xx (a bad key fails every call),
        # CLI missing/non-zero exit — counts as a failure, except 400/413/422,
        # which are about this request and give no verdict; a returned
        # response resets the count. Input validation above is not counted.
        breaker = get_breaker(self.llmService)
        breaker.before_call()
        try:
            result = self._call_service_impl(prompt)
        except Exception as exc:
            _record_error(breaker, exc)
            raise
        breaker.record_success()
        return result

    def _call_service_impl(self, prompt: str) -> str:
        # ------------------------------------------------------------------ #
        # claude_code — delegate to the local Claude Code CLI                 #
        # ------------------------------------------------------------------ #
        if self.llmService.lower() == "claude_code":
            import json as _json
            model = self.api_temp_dict.get("model", "claude-sonnet-4-5")
            try:
                # Pass the full task prompt as --system-prompt so Claude treats it
                # as operating instructions rather than user-pasted content.
                # A neutral -p trigger activates the response without framing the
                # prompt as something the "user" typed into the chat.
                result = subprocess.run(
                    [
                        "claude", "-p", "Execute the task as specified.",
                        "--system-prompt", prompt,
                        "--model", model,
                        "--output-format", "json",
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=120,
                )
            except FileNotFoundError:
                raise ValueError(
                    "Claude Code CLI not found. "
                    "Install it with: npm install -g @anthropic-ai/claude-code"
                )
            except subprocess.TimeoutExpired:
                raise ValueError("Claude Code CLI timed out after 120 seconds.")

            if result.returncode != 0:
                err = result.stderr.strip() or result.stdout.strip()
                raise ValueError(f"Claude Code CLI exited with error: {err[:300]}")

            # Parse JSON response to extract text and record token usage
            try:
                data = _json.loads(result.stdout)
                text_out = data.get("result", result.stdout).strip()
                usage = data.get("usage") or {}
                cost  = data.get("cost_usd") or 0.0
                try:
                    from backend.usage_tracker import record_claude_code
                    record_claude_code(
                        input_tokens=int(usage.get("input_tokens", 0)),
                        output_tokens=int(usage.get("output_tokens", 0)),
                        cost_usd=float(cost),
                    )
                except Exception:
                    pass  # tracking is best-effort
            except (_json.JSONDecodeError, AttributeError):
                text_out = result.stdout.strip()

            return text_out

        # ------------------------------------------------------------------ #
        # HTTP-based providers                                                 #
        # ------------------------------------------------------------------ #
        if self.llmService.lower() in ("open_ai", "groq", "codex", "anthropic"):
            self.api_temp_dict["payload"]["messages"][0]["content"] = prompt

        if self.llmService.lower() == "google":
            self.api_temp_dict["payload"]["contents"][0]["parts"][0]["text"] = prompt

        # Make the API call with exponential backoff retry for transient errors
        for attempt in range(1, _MAX_RETRIES + 1):
            response = requests.post(
                self.api_temp_dict["endpoint"],
                headers=self.api_temp_dict["headers"],
                json=self.api_temp_dict["payload"],
                timeout=_REQUEST_TIMEOUT
            )

            if response.status_code == 200:
                data = response.json()
                if self.llmService.lower() in ("open_ai", "groq", "codex"):
                    return data['choices'][0]['message']['content']
                if self.llmService.lower() == "anthropic":
                    return data['content'][0]['text']
                if self.llmService.lower() == "google":
                    return data['candidates'][0]['content']['parts'][0]['text']

            if response.status_code in _RETRYABLE_STATUS_CODES:
                if attempt < _MAX_RETRIES:
                    delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    logger.warning(
                        "LLM API returned %d (attempt %d/%d) — retrying in %.1fs",
                        response.status_code, attempt, _MAX_RETRIES, delay
                    )
                    time.sleep(delay)
                    continue

            # Non-retryable error — surface billing errors clearly
            detail = response.text[:300]
            try:
                err_msg = response.json().get("error", {})
                if isinstance(err_msg, dict):
                    err_msg = err_msg.get("message", "")
                if "credit" in str(err_msg).lower() or "billing" in str(err_msg).lower() or "balance" in str(err_msg).lower():
                    raise ValueError(
                        f"Provider '{self.llmService}' has no remaining credits. "
                        "Please top up your account or switch to a different provider."
                    )
            except ValueError:
                raise
            except Exception:
                pass
            raise LLMRequestError(
                f"LLM API call failed after {attempt} attempt(s). "
                f"Status code: {response.status_code} — {detail}",
                response.status_code,
            )

    def CallServiceStream(self, prompt: str):
        """
        Generator version of CallService — yields text token strings as they arrive
        from the LLM provider's streaming API.

        For providers that do not support streaming (claude_code, codex_cli) this
        falls back to a single-shot call and yields the full response as one chunk.

        Args:
            prompt (str): The prompt text.

        Yields:
            str: Individual token/chunk strings.
        """
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string.")

        # CLI providers — no native streaming; fall back to batch
        # (CallService applies the circuit breaker itself)
        if self.llmService.lower() in ("claude_code", "codex_cli"):
            yield self.CallService(prompt)
            return

        if self.llmService.lower() not in ("open_ai", "groq", "codex", "anthropic", "google"):
            yield self.CallService(prompt)   # unknown provider — batch, breaker inside
            return

        # Circuit breaker for the streaming HTTP path: a raise before or while
        # yielding is a failure, running to completion is a success, and a
        # consumer that stops early (GeneratorExit) gives no verdict.
        breaker = get_breaker(self.llmService)
        breaker.before_call()
        try:
            yield from self._stream_impl(prompt)
        except GeneratorExit:
            breaker.abandon()
            raise
        except Exception as exc:
            _record_error(breaker, exc)
            raise
        breaker.record_success()

    def _stream_impl(self, prompt: str):

        if self.llmService.lower() in ("open_ai", "groq", "codex", "anthropic"):
            self.api_temp_dict["payload"]["messages"][0]["content"] = prompt

        if self.llmService.lower() == "google":
            self.api_temp_dict["payload"]["contents"][0]["parts"][0]["text"] = prompt

        if self.llmService.lower() in ("open_ai", "groq", "codex"):
            payload = {**self.api_temp_dict["payload"], "stream": True}
            with requests.post(
                self.api_temp_dict["endpoint"],
                headers=self.api_temp_dict["headers"],
                json=payload,
                stream=True,
                timeout=_REQUEST_TIMEOUT,
            ) as response:
                if response.status_code != 200:
                    raise LLMRequestError(
                        f"LLM API call failed: {response.status_code} — {response.text[:200]}",
                        response.status_code,
                    )
                for raw_line in response.iter_lines():
                    line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                        token = (chunk.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
                        if token:
                            yield token
                    except Exception:
                        pass
            return

        if self.llmService.lower() == "anthropic":
            payload = {**self.api_temp_dict["payload"], "stream": True}
            with requests.post(
                self.api_temp_dict["endpoint"],
                headers=self.api_temp_dict["headers"],
                json=payload,
                stream=True,
                timeout=_REQUEST_TIMEOUT,
            ) as response:
                if response.status_code != 200:
                    raise LLMRequestError(
                        f"LLM API call failed: {response.status_code} — {response.text[:200]}",
                        response.status_code,
                    )
                for raw_line in response.iter_lines():
                    line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
                    if not line or not line.startswith("data: "):
                        continue
                    try:
                        chunk = json.loads(line[6:])
                        if chunk.get("type") == "content_block_delta":
                            token = chunk.get("delta", {}).get("text") or ""
                            if token:
                                yield token
                    except Exception:
                        pass
            return

        if self.llmService.lower() == "google":
            # Google streaming uses :streamGenerateContent?alt=sse endpoint
            endpoint = self.api_temp_dict["endpoint"].replace(
                ":generateContent", ":streamGenerateContent"
            )
            if "?" not in endpoint:
                endpoint += "?alt=sse"
            else:
                endpoint += "&alt=sse"
            with requests.post(
                endpoint,
                headers=self.api_temp_dict["headers"],
                json=self.api_temp_dict["payload"],
                stream=True,
                timeout=_REQUEST_TIMEOUT,
            ) as response:
                if response.status_code != 200:
                    raise LLMRequestError(
                        f"LLM API call failed: {response.status_code} — {response.text[:200]}",
                        response.status_code,
                    )
                for raw_line in response.iter_lines():
                    line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
                    if not line or not line.startswith("data: "):
                        continue
                    try:
                        chunk = json.loads(line[6:])
                        token = (
                            chunk.get("candidates", [{}])[0]
                            .get("content", {})
                            .get("parts", [{}])[0]
                            .get("text") or ""
                        )
                        if token:
                            yield token
                    except Exception:
                        pass
            return

        # Unknown provider — fall back to batch
        yield self.CallService(prompt)

