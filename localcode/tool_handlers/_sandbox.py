"""
Shell security: dangerous command detection, sandbox allowlist.

Stdlib only — no imports from other tool_handlers modules.
"""

import os
import re
import shlex
from typing import Optional


# Dangerous command patterns (soft sandbox)
DANGEROUS_PATTERNS = [
    r"rm\s+(-[rf]+\s+)*(/|~|\$HOME|/\*)",
    r"rm\s+.*\s+(/etc|/usr|/bin|/lib|/boot|/var|/sys|/proc)",
    r"(mv|cp)\s+.*\s+(/etc|/usr|/bin|/lib|/boot)/",
    r"dd\s+.*of=/dev/",
    r"mkfs\.",
    r"^sudo\s+",
    r"^su\s+",
    r"chmod\s+(-R\s+)?(777|666)\s+/",
    r";\s*(rm|mv|dd|mkfs|sudo|su)\s+",
    r"\|\s*(rm|mv|dd|mkfs|sudo|su)\s+",
    r":\(\)\s*\{",
    r"(curl|wget).*\|\s*(ba)?sh",
    r"(?:\d\s*)?>{1,2}\s*/(?:etc|usr|bin|lib|boot|var|sys|proc)/",
    r"tee\b.*\s+/(?:etc|usr|bin|lib|boot)/",
]
_DANGEROUS_COMMAND_RES = [re.compile(p, re.IGNORECASE) for p in DANGEROUS_PATTERNS]

# Shell chaining operators blocked in sandbox mode
# NOTE: pipe (|) is checked token-level in _check_sandbox_allowlist to avoid
# false positives on "|" inside quoted args (e.g. rg "a|b").
_SHELL_CHAINING_RE = re.compile(r';|&&|\|\||`|\n|\r|\$\(|(^|\s)\.\./')
_SHELL_CD_RE = re.compile(r'^\s*cd\b')

# Allowlist of binaries permitted in sandbox mode.
# Only the basename of the first token (the command) is checked.
_SANDBOX_ALLOWED_CMDS = frozenset({
    # Language runtimes (without inline code flags — checked separately)
    "python", "python3", "python3.8", "python3.9", "python3.10", "python3.11",
    "python3.12", "python3.13", "python3.14",
    "node",
    # Core utilities
    "ls", "cat", "head", "tail", "wc", "sort", "uniq", "tr", "cut", "tee",
    "echo", "printf", "true", "false", "test", "expr",
    "cp", "mv", "mkdir", "touch", "chmod", "dirname", "basename", "realpath",
    "find", "xargs",
    # Search / diff
    "grep", "egrep", "fgrep", "rg", "ag", "sed", "awk", "diff", "patch",
    # Build / package (read-only or project-scoped)
    "git", "npm", "npx", "yarn", "pnpm", "pip", "pip3", "cargo", "make",
    "go", "rustc", "javac", "java", "gcc", "g++", "clang", "clang++",
    # Other common
    "env", "which", "file", "stat", "du", "df", "uname", "date", "whoami",
})

# Flags that allow arbitrary code execution in interpreters.
# Blocked in sandbox to prevent escapes like: python -c "import os; ..."
_SANDBOX_INLINE_CODE_RE = re.compile(
    r"(?:^|\s)(?:"
    r"python[0-9.]*\s+(?:-[a-zA-Z]*c|-c)"      # python -c, python3 -c, python -Sc etc.
    r"|node\s+(?:-e|--eval|-p|--print)"          # node -e / --eval / -p / --print
    r"|perl\s+-[a-zA-Z]*e"                       # perl -e, perl -ne, etc.
    r"|ruby\s+-[a-zA-Z]*e"                       # ruby -e
    r"|sh\s+-c|bash\s+-c|zsh\s+-c"              # sh -c, bash -c, zsh -c (defense-in-depth)
    r")",
    re.IGNORECASE,
)

_ENV_VAR_ASSIGN_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')

TEST_MENTION_RE = re.compile(
    r"\b(run tests?|tests?|npm test|jest|pytest|go test|cargo test|ctest|yarn test|pnpm test)\b",
    re.IGNORECASE,
)


def _check_dangerous_command(command: str) -> Optional[str]:
    for pattern_re in _DANGEROUS_COMMAND_RES:
        if pattern_re.search(command):
            return pattern_re.pattern
    return None


def _check_sandbox_allowlist(command: str) -> Optional[str]:
    """Return an error string if command's binary is not in the sandbox allowlist, else None."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        # Malformed quoting — shlex.split in shell() will catch this too
        tokens = command.split()
    if not tokens:
        return None
    # Skip leading env-var assignments (e.g. VAR=1 python script.py)
    cmd_idx = 0
    while cmd_idx < len(tokens) and _ENV_VAR_ASSIGN_RE.match(tokens[cmd_idx]):
        cmd_idx += 1
    if cmd_idx >= len(tokens):
        return "error: command contains only variable assignments, no actual command"
    cmd_token = tokens[cmd_idx]
    if "/" in cmd_token:
        return (
            f"error: command paths ('{cmd_token}') are not allowed in sandbox; "
            f"use the bare command name instead (e.g. 'ls' not '/bin/ls')."
        )
    binary = os.path.basename(cmd_token)
    if binary not in _SANDBOX_ALLOWED_CMDS:
        return (
            f"error: command '{binary}' is not in the sandbox allowlist; "
            f"allowed: python, python3, node, ls, cat, grep, rg, git, "
            f"npm, make, echo, etc. Use an allowed command or request sandbox changes."
        )
    # Block inline-code flags for interpreters (python -c, node -e, perl -e, sh -c, etc.)
    if _SANDBOX_INLINE_CODE_RE.search(command):
        return (
            f"error: inline code execution (e.g. -c / -e flags) is not allowed in sandbox; "
            f"write a script file and run it instead."
        )
    # Token-level pipe check: block "|" as a standalone token (shell pipe operator).
    # This catches "ls|cat" (shlex splits to ["ls|cat"] — no match) and "ls | cat"
    # (shlex splits to ["ls", "|", "cat"] — match) without false-positiving on
    # "|" inside quoted arguments like rg "a|b".
    if "|" in tokens:
        return (
            "error: pipe operator (|) is not allowed in sandbox; "
            "run commands separately instead."
        )
    return None
