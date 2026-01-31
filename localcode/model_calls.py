"""Model call utilities — self-reflection, batch calls, subprocess agents.

Standalone model invocation utilities (HTTP calls, subprocess spawning,
batch threading). These take parameters and return results without mutating
agent state directly.
"""

import json
import os
import re
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Tuple

from localcode.tool_handlers._state import SANDBOX_ROOT, MAX_FILE_SIZE
from localcode.tool_handlers import (
    _is_ignored_path,
    _is_path_within_sandbox,
    _require_args_dict,
    _validate_path,
)


def _load_prompt_file(relative_path: str, base_dir: str) -> str:
    """Load a prompt file relative to base_dir."""
    full = os.path.join(base_dir, relative_path)
    with open(full, "r", encoding="utf-8") as f:
        return f.read().strip()


def _self_call(
    prompt: str,
    system_prompt: str,
    temperature: float = 0.3,
    max_tokens: int = 4000,
    timeout: int = 120,
    include_history: bool = True,
    user_prefix: str = "",
    *,
    api_url: str = "",
    model: str = "",
    current_messages: List[Dict[str, Any]] = None,
) -> str:
    """Make an API call to the same model (self-reflection / thinking)."""
    if current_messages is None:
        current_messages = []

    history_messages = []
    if include_history:
        for msg in current_messages:
            if msg.get("role") in ("user", "assistant", "tool"):
                history_messages.append(msg)

    user_content = f"{user_prefix}{prompt}" if user_prefix else prompt

    messages = [
        {"role": "system", "content": system_prompt},
        *history_messages,
        {"role": "user", "content": user_content},
    ]

    request_data = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    try:
        req = urllib.request.Request(
            api_url,
            data=json.dumps(request_data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=timeout)
        payload = json.loads(resp.read())

        if "choices" in payload and payload["choices"]:
            content = payload["choices"][0].get("message", {}).get("content", "")
            if content:
                return content.strip()

        return "error: no response from model"

    except Exception as e:
        return f"error: API call failed: {e}"


def _self_call_batch(
    questions: List[str],
    system_prompt: str,
    temperature: float = 0.3,
    max_tokens: int = 2000,
    timeout: int = 120,
    include_history: bool = True,
    max_concurrent: int = 4,
    *,
    api_url: str = "",
    model: str = "",
    current_messages: List[Dict[str, Any]] = None,
) -> str:
    """Send multiple questions concurrently via ThreadPoolExecutor.

    Fail-all: if any question fails, the entire batch returns an error.
    """
    def call_one(idx: int, question: str) -> Tuple[int, str]:
        result = _self_call(
            prompt=question,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            include_history=include_history,
            api_url=api_url,
            model=model,
            current_messages=current_messages,
        )
        return (idx, result)

    results: List[Tuple[int, str]] = []
    with ThreadPoolExecutor(max_workers=max_concurrent) as executor:
        futures = {
            executor.submit(call_one, i, q): i
            for i, q in enumerate(questions)
        }
        for future in as_completed(futures):
            idx, answer = future.result()
            if answer.startswith("error:"):
                return answer
            results.append((idx, answer))

    results.sort(key=lambda x: x[0])
    parts = []
    for idx, answer in results:
        parts.append(f"## Question {idx + 1}: {questions[idx]}\n\n{answer}")
    return "\n\n---\n\n".join(parts)


def _subprocess_call(
    prompt: str,
    agent: str,
    timeout_sec: int,
    files: List[str],
    config: Dict[str, Any],
    *,
    base_dir: str = "",
    api_url: str = "",
) -> str:
    """Run a sub-agent via subprocess and return its cleaned response."""
    from localcode.tool_handlers import _state as _tool_state

    # If files are specified, read them and append to prompt
    if config.get("read_files") and files and isinstance(files, list):
        file_contents = []
        for file_path in files:
            if not isinstance(file_path, str):
                continue
            try:
                if _tool_state.SANDBOX_ROOT:
                    full_path = _validate_path(file_path, check_exists=True)
                else:
                    full_path = os.path.abspath(file_path)
                if _is_ignored_path(full_path):
                    file_contents.append(f"=== {file_path} ===\n(ignored path)")
                    continue
                if os.path.exists(full_path) and os.path.isfile(full_path):
                    stat = os.stat(full_path)
                    if stat.st_size > MAX_FILE_SIZE:
                        file_contents.append(f"=== {file_path} ===\n(file too large: {stat.st_size} bytes)")
                        continue
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read(MAX_FILE_SIZE)
                    file_contents.append(f"=== {file_path} ===\n{content}")
                else:
                    file_contents.append(f"=== {file_path} ===\n(file not found)")
            except ValueError as e:
                file_contents.append(f"=== {file_path} ===\n(access denied: {e})")
            except Exception as e:
                file_contents.append(f"=== {file_path} ===\n(error reading: {e})")

        if file_contents:
            prompt = prompt + "\n\nFILES:\n" + "\n\n".join(file_contents)

    # Build the localcode command - pass URL from parent agent
    localcode_path = os.path.join(base_dir, "localcode.py")
    cmd = [
        sys.executable,
        localcode_path,
        "--agent", agent,
        "--url", api_url,
        prompt,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            cwd=os.getcwd(),
        )
        stdout = (result.stdout or "").strip()

        lines = stdout.split("\n")
        response_lines = []
        in_thinking = False

        for line in lines:
            # Remove ANSI escape codes
            clean = re.sub(r'\x1b\[[0-9;]*m', '', line) if config.get("strip_ansi") else line
            # Check for thinking section markers before stripping unicode
            if config.get("strip_thinking") and "----- THINKING -----" in clean:
                in_thinking = True
                continue
            if in_thinking and ("\u23fa" in line or clean.strip().startswith("**")):
                in_thinking = False
            if in_thinking:
                continue
            # Remove other special characters (Unicode symbols)
            clean = re.sub(r'[^\x00-\x7F]+', '', clean).strip()
            if not clean:
                continue
            # Skip status/header lines from localcode output
            if config.get("strip_status_lines"):
                if clean.startswith("localcode["):
                    continue
                if clean.startswith("TURN"):
                    continue
                if clean.startswith("TASK ") and ("TRY" in clean or "id:" in clean):
                    continue
            response_lines.append(clean)

        response = "\n".join(response_lines).strip()
        if not response:
            return f"error: agent returned no output (stdout_len={len(stdout)}, returncode={result.returncode})"

        return response

    except subprocess.TimeoutExpired:
        return f"error: agent timed out after {timeout_sec} seconds"
    except Exception as e:
        return f"error: failed to call agent: {e}"


def make_model_call_handler(
    tool_name: str,
    config: Dict[str, Any],
    *,
    get_api_url,
    get_model,
    get_current_messages,
    get_base_dir,
):
    """Factory that creates a tool handler from a model_call config block.

    The get_* callables are zero-arg functions that return the current value
    of the corresponding global (deferred lookup avoids circular imports).
    """
    mode = config.get("mode", "self")

    def handler(args: Any) -> str:
        args, err = _require_args_dict(args, tool_name)
        if err:
            return err

        api_url = get_api_url()
        model = get_model()
        current_messages = get_current_messages()
        base_dir = get_base_dir()

        if mode == "self_batch":
            questions = args.get("questions")
            if not questions or not isinstance(questions, list):
                return "error: questions is required and must be an array of strings"
            questions = [q for q in questions if isinstance(q, str) and q.strip()]
            if not questions:
                return "error: questions array must contain at least one non-empty string"
            max_questions = config.get("max_questions", 10)
            if len(questions) > max_questions:
                return f"error: maximum {max_questions} questions per batch"

            system_prompt = _load_prompt_file(config["system_prompt_file"], base_dir)
            return _self_call_batch(
                questions=questions,
                system_prompt=system_prompt,
                temperature=config.get("temperature", 0.3),
                max_tokens=config.get("max_tokens", 2000),
                timeout=config.get("timeout", 120),
                include_history=config.get("include_history", True),
                max_concurrent=config.get("max_concurrent", 4),
                api_url=api_url,
                model=model,
                current_messages=current_messages,
            )

        prompt = args.get("prompt") or args.get("content")
        if not prompt or not isinstance(prompt, str):
            return "error: prompt is required and must be a string"

        if mode == "subprocess":
            agent = args.get("agent", config.get("default_agent", "code-architect"))
            timeout_sec = args.get("timeout", config.get("default_timeout", 300))
            files = args.get("files", [])
            return _subprocess_call(
                prompt, agent, timeout_sec, files, config,
                base_dir=base_dir, api_url=api_url,
            )

        # mode == "self"
        system_prompt = _load_prompt_file(config["system_prompt_file"], base_dir)
        stage_param = config.get("stage_param")
        if stage_param:
            stage = args.get(stage_param, "").lower().strip()
            stage_files = config.get("stage_prompt_files", {})
            if stage and stage in stage_files:
                system_prompt = _load_prompt_file(stage_files[stage], base_dir)
            # Log for debugging (preserves original think behavior)
            print(f"\n[{tool_name.upper()}] stage={stage or 'none'} prompt={prompt}\n", file=sys.stderr)

        return _self_call(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=config.get("temperature", 0.3),
            max_tokens=config.get("max_tokens", 4000),
            timeout=config.get("timeout", 120),
            include_history=config.get("include_history", True),
            user_prefix=config.get("user_prefix", ""),
            api_url=api_url,
            model=model,
            current_messages=current_messages,
        )

    handler.__name__ = f"model_call_{tool_name}"
    return handler
