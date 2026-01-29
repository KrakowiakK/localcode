#!/usr/bin/env python3
"""
Standalone benchmark runner for localcode agent.
No aider dependency — uses only stdlib.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ── Prompt templates ─────────────────────────────────────────

INSTRUCTIONS_ADDENDUM = """
####

Use the above instructions to modify the supplied files: {file_list}
Don't change the names of existing functions or classes, as they may be referenced from other code like unit tests, etc.
Only use standard libraries, don't suggest installing any packages.
"""

TEST_FAILURES = """
####

See the testing errors above.
The tests are correct, don't try and change them.
Fix the code in {file_list} to resolve the errors.
"""

# ── Test commands per extension ──────────────────────────────

TEST_COMMANDS = {
    ".py": ["pytest"],
    ".rs": ["cargo", "test", "--", "--include-ignored"],
    ".go": ["go", "test", "./..."],
    ".js": ["/benchmark/npm-test.sh"],
    ".cpp": ["/benchmark/cpp-test.sh"],
    ".java": ["./gradlew", "test"],
}

TEST_TIMEOUT = 60 * 3


# ── Helpers ──────────────────────────────────────────────────

def read_config(testdir: Path):
    """Read .meta/config.json and return (solution_files, test_files)."""
    config_file = testdir / ".meta" / "config.json"
    if not config_file.exists():
        raise ValueError(f"No config file found: {config_file}")

    config = json.loads(config_file.read_text())
    files = config.get("files", {})
    solution_files = set(files.get("solution", []))
    test_files = list(files.get("test", []))
    example_files = list(files.get("example", []))

    # Files to ignore (not editable by LLM)
    ignore = {"CMakeLists.txt", "Cargo.toml"}
    ignore.update(str(p.relative_to(testdir)) for p in testdir.glob(".meta/**/*") if p.is_file())
    ignore.update(str(p.relative_to(testdir)) for p in testdir.glob(".docs/**/*") if p.is_file())
    ignore.update(test_files)
    ignore.update(example_files)
    solution_files -= ignore

    return sorted(solution_files), test_files


def restore_solution_files(testdir: Path, original_dir: Path, solution_files: list):
    """Copy original solution files into testdir."""
    # Find the relative language path parts
    # testdir = .../results/<run>/<lang>/exercises/practice/<task>
    # original_dir = .../polyglot-benchmark
    lang_part = None
    parts = testdir.parts
    for i, part in enumerate(parts):
        if part == "exercises" and i + 1 < len(parts) and parts[i + 1] == "practice":
            lang_part = parts[i - 1]
            break

    for file_path in solution_files:
        src = testdir / file_path
        if lang_part:
            original = original_dir / lang_part / "exercises" / "practice" / testdir.name / file_path
        else:
            original = None

        if original and original.exists():
            os.makedirs(src.parent, exist_ok=True)
            shutil.copy(original, src)
        elif not src.exists():
            print(f"  Warning: Solution file not found: {file_path}")


def build_prompt(testdir: Path, file_list: str) -> str:
    """Build prompt from .docs/ markdown files + addendum."""
    instructions = ""

    introduction = testdir / ".docs" / "introduction.md"
    if introduction.exists():
        instructions += introduction.read_text()

    instructions_md = testdir / ".docs" / "instructions.md"
    if instructions_md.exists():
        instructions += instructions_md.read_text()

    instructions_append = testdir / ".docs" / "instructions.append.md"
    if instructions_append.exists():
        instructions += instructions_append.read_text()

    instructions += INSTRUCTIONS_ADDENDUM.format(file_list=file_list)
    return instructions


def get_test_command(test_files: list):
    """Determine test command from test file extensions."""
    extensions = {Path(f).suffix for f in test_files}
    for ext in extensions:
        if ext in TEST_COMMANDS:
            return TEST_COMMANDS[ext]
    raise ValueError(f"No test command for extensions: {extensions}")


def run_tests(testdir: Path, original_dir: Path, test_files: list) -> str | None:
    """Run unit tests. Returns error output or None on success."""
    command = get_test_command(test_files)

    # Copy test files from original
    lang_part = None
    parts = testdir.parts
    for i, part in enumerate(parts):
        if part == "exercises" and i + 1 < len(parts) and parts[i + 1] == "practice":
            lang_part = parts[i - 1]
            break

    for file_path in test_files:
        if lang_part:
            src = original_dir / lang_part / "exercises" / "practice" / testdir.name / file_path
        else:
            src = None
        dst = testdir / file_path
        if src and src.exists():
            os.makedirs(dst.parent, exist_ok=True)
            shutil.copy(src, dst)

    # Remove @Disabled annotations from Java test files
    for file_path in test_files:
        if file_path.endswith(".java"):
            test_file = testdir / file_path
            if test_file.exists():
                content = test_file.read_text()
                content = re.sub(r"@Disabled\([^)]*\)\s*\n", "", content)
                test_file.write_text(content)

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=TEST_TIMEOUT,
        cwd=testdir,
        encoding="utf-8",
        errors="replace",
    )

    output = result.stdout
    # Clean timing info
    output = re.sub(r"\bin \d+\.\d+s\b", "", output)
    output = output.replace(str(testdir), testdir.name)

    if result.returncode != 0:
        return output
    return None


def get_docker_url(agent_config: str) -> str | None:
    """Read agent JSON and rewrite localhost → host.docker.internal for Docker."""
    if "AIDER_DOCKER" not in os.environ:
        return None
    # Find agent config file
    for prefix in ["/localcode/agents/", ""]:
        path = Path(prefix) / f"{agent_config}.json"
        if path.exists():
            try:
                data = json.loads(path.read_text())
                url = data.get("url", "")
                if "localhost" in url or "127.0.0.1" in url:
                    url = url.replace("localhost", "host.docker.internal")
                    url = url.replace("127.0.0.1", "host.docker.internal")
                    return url
            except (json.JSONDecodeError, OSError):
                pass
    return None


def call_localcode(prompt_file: str, testdir: Path, agent_config: str,
                   continue_session: bool = False):
    """Call localcode agent."""
    cmd = [
        "python3", "/localcode/localcode.py",
        "--agent", agent_config,
        "-f", prompt_file,
    ]
    if continue_session:
        cmd.append("--continue")

    # Rewrite localhost URL for Docker
    docker_url = get_docker_url(agent_config)
    if docker_url:
        cmd.extend(["--url", docker_url])

    env = os.environ.copy()
    print(f"  Running: {' '.join(cmd)}")

    result = subprocess.run(
        cmd,
        cwd=testdir,
        env=env,
        timeout=60 * 10,  # 10 min max per call
    )
    return result.returncode


def clean_build_artifacts(testdir: Path):
    """Remove build directories to save space."""
    for dirname in ["target/debug", "build", "node_modules"]:
        d = testdir / dirname
        if d.exists():
            try:
                shutil.rmtree(d)
            except (OSError, PermissionError):
                pass


def run_exercise(testdir: Path, original_dir: Path, agent_config: str,
                 tries: int) -> dict:
    """Run a single exercise. Returns results dict."""
    results_file = testdir / ".aider.results.json"

    # Skip if already done
    if results_file.exists():
        try:
            return json.loads(results_file.read_text())
        except json.JSONDecodeError:
            print(f"  Corrupt results, redoing: {results_file}")

    solution_files, test_files = read_config(testdir)
    if not solution_files:
        print(f"  No solution files found, skipping")
        return {}

    # Restore originals
    restore_solution_files(testdir, original_dir, solution_files)

    file_list = " ".join(Path(f).name for f in solution_files)
    prompt_text = build_prompt(testdir, file_list)

    start = time.time()
    test_outcomes = []
    timeouts = 0

    for attempt in range(tries):
        # Write prompt to /tmp (outside testdir so localcode doesn't see it)
        prompt_file = f"/tmp/benchmark_prompt_{os.getpid()}.md"
        with open(prompt_file, "w") as f:
            f.write(prompt_text)

        # Call localcode
        is_retry = attempt > 0
        call_localcode(prompt_file, testdir, agent_config, continue_session=is_retry)

        # Clean up prompt file
        try:
            os.unlink(prompt_file)
        except OSError:
            pass

        # Run tests
        print(f"  Running tests...")
        try:
            errors = run_tests(testdir, original_dir, test_files)
        except subprocess.TimeoutExpired:
            errors = "Tests timed out!"
            timeouts += 1

        if errors:
            test_outcomes.append(False)
            # Show last lines of test output
            error_lines = errors.strip().splitlines()
            preview = error_lines[-min(15, len(error_lines)):]
            print(f"  Try {attempt + 1}: FAIL")
            for line in preview:
                print(f"    {line}")
            if attempt < tries - 1:
                # Build retry prompt
                prompt_text = errors + TEST_FAILURES.format(file_list=file_list)
        else:
            test_outcomes.append(True)
            print(f"  Try {attempt + 1}: PASS")
            break

    duration = time.time() - start

    # Clean build artifacts
    clean_build_artifacts(testdir)

    results = {
        "testdir": str(testdir),
        "testcase": testdir.name,
        "tests_outcomes": test_outcomes,
        "duration": duration,
        "test_timeouts": timeouts,
    }

    results_file.write_text(json.dumps(results, indent=4))
    return results


# ── Exercise discovery ───────────────────────────────────────

def discover_exercises(exercises_dir: Path, languages: str | None,
                       keywords: str | None, num_tests: int) -> list:
    """Find exercise directories to run."""
    lang_dirs = sorted(d for d in exercises_dir.iterdir() if d.is_dir())

    if languages:
        requested = {l.strip().lower() for l in languages.split(",")}
        lang_dirs = [d for d in lang_dirs if d.name.lower() in requested]

    exercise_dirs = []
    for lang_dir in lang_dirs:
        practice_dir = lang_dir / "exercises" / "practice"
        if practice_dir.exists():
            exercise_dirs.extend(sorted(d for d in practice_dir.iterdir() if d.is_dir()))

    if keywords:
        kw_list = [k.strip() for k in keywords.split(",")]
        exercise_dirs = [d for d in exercise_dirs if any(k in d.name for k in kw_list)]

    if num_tests > 0:
        exercise_dirs = exercise_dirs[:num_tests]

    return exercise_dirs


def copy_exercises_to_results(exercises_dir: Path, results_run_dir: Path,
                              exercise_dirs: list):
    """Copy exercise dirs to results directory if not already there."""
    if not results_run_dir.exists():
        os.makedirs(results_run_dir, exist_ok=True)

    for ex_dir in exercise_dirs:
        # ex_dir = .../polyglot-benchmark/<lang>/exercises/practice/<task>
        rel = ex_dir.relative_to(exercises_dir)
        dest = results_run_dir / rel
        if not dest.exists():
            os.makedirs(dest.parent, exist_ok=True)
            shutil.copytree(ex_dir, dest)


# ── Main ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Standalone benchmark runner")
    parser.add_argument("--tries", type=int, default=2, help="Attempts per exercise")
    parser.add_argument("--languages", "-l", help="Comma-separated languages to test")
    parser.add_argument("-k", "--keywords", help="Comma-separated keyword filter")
    parser.add_argument("--new", dest="run_name", help="Run name (timestamp prefix added)")
    parser.add_argument("--num-tests", "-n", type=int, default=-1, help="Limit number of tests")
    parser.add_argument("--exercises-dir", default="/benchmarks/polyglot-benchmark",
                        help="Path to polyglot-benchmark exercises")
    parser.add_argument("--results-dir", default="/results",
                        help="Path to results directory")

    args = parser.parse_args()

    # Require AIDER_DOCKER env (safety check)
    if "AIDER_DOCKER" not in os.environ:
        print("Warning: benchmarking runs unvetted LLM code, run in a Docker container")
        print("Set AIDER_DOCKER=1 to proceed.")
        sys.exit(1)

    exercises_dir = Path(args.exercises_dir)
    if not exercises_dir.is_dir():
        print(f"Error: exercises directory not found: {exercises_dir}")
        sys.exit(1)

    results_dir = Path(args.results_dir)

    agent_config = os.environ.get("LOCALCODE_AGENT_CONFIG", "localcode")

    # Resolve run directory
    import datetime
    run_name = args.run_name or "benchmark"
    if not re.match(r"\d{4}-\d{2}-\d{2}-", run_name):
        now = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S--")
        run_name = now + run_name

    run_dir = results_dir / run_name

    # Discover exercises
    exercise_dirs = discover_exercises(
        exercises_dir, args.languages, args.keywords, args.num_tests
    )

    if not exercise_dirs:
        print("No exercises found matching criteria")
        sys.exit(1)

    print(f"Found {len(exercise_dirs)} exercises")
    print(f"Results: {run_dir}")
    print(f"Agent config: {agent_config}")
    print(f"Tries: {args.tries}")
    print()

    # Copy exercises to results dir
    copy_exercises_to_results(exercises_dir, run_dir, exercise_dirs)

    # Run each exercise
    total = len(exercise_dirs)
    passed = 0
    failed = 0

    for idx, ex_dir in enumerate(exercise_dirs, 1):
        rel = ex_dir.relative_to(exercises_dir)
        testdir = run_dir / rel
        task_name = ex_dir.name
        lang = rel.parts[0] if rel.parts else "unknown"

        print(f"[{idx}/{total}] {lang}/{task_name}")

        try:
            result = run_exercise(testdir, exercises_dir, agent_config, args.tries)
            outcomes = result.get("tests_outcomes", [])
            if any(outcomes):
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"  ERROR: {e}")
            failed += 1
            # Write error result
            error_result = {"testcase": task_name, "exception": str(e), "tests_outcomes": []}
            (testdir / ".aider.results.json").write_text(json.dumps(error_result, indent=4))

        print()

    # Summary
    print("=" * 60)
    print(f"Results: {passed}/{total} passed, {failed}/{total} failed")
    if total > 0:
        print(f"Pass rate: {passed / total * 100:.1f}%")
    print(f"Output: {run_dir}")
    print("=" * 60)


if __name__ == "__main__":
    main()
