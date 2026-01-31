# 1. Stan projektu — podsumowanie

## Struktura po 3 fazach refactoringu

```
localcode/                          ŁĄCZNIE: ~6300 linii kodu
├── localcode.py         1685 ln   Agent loop, API, entrypoint
├── config.py             162 ln   CLI parsing, config loading (Phase 3)
├── session.py            195 ln   Session persistence (Phase 3)
├── model_calls.py        336 ln   Self-call, subprocess, batch (Phase 3)
├── hooks.py               47 ln   Event registry
├── __init__.py            38 ln   Lazy proxy
├── middleware/           ~810 ln   (Phase 1)
│   ├── logging_hook.py    111
│   ├── metrics_hook.py    110
│   ├── feedback_hook.py   476
│   └── conversation_dump   87
├── tool_handlers/       ~2000 ln   (Phase 2, 11 plików)
│   ├── dispatch.py        287
│   ├── schema.py          200
│   ├── patch_handlers.py  358
│   ├── search_handlers.py 312
│   ├── read_handlers.py   156
│   ├── shell_handler.py   148
│   ├── write_handlers.py  130
│   ├── _sandbox.py        130
│   ├── _state.py          111
│   ├── _path.py            52
│   └── __init__.py        112
├── tools/                JSON tool definitions
├── agents/               Agent configs (gguf/, mlx/)
├── prompts/              System prompts
└── tests/
    ├── test_localcode.py  3323 ln  (402 testy, unittest)
    └── test_hooks.py       329 ln  (31 testów, pytest-style)
```

## Ocena ogólna: 7/10

**Mocne strony:**
- Dobrze zaprojektowany hook/event system (publish-subscribe)
- Deklaratywne tool definitions w JSON (feedback templates, model_call)
- Solidny sandbox (allowlist, path validation, chaining detection)
- Feedback system z rule-based matching
- 402 testy przechodzą, pokrycie tool handlerów dobre

**Słabe strony:**
- 20+ mutowalnych globali w localcode.py (brak DI/context object)
- Brak conversation compaction (context rośnie bez limitu)
- Brak task decomposition (prompt → single run)
- Brak structured output
- Test coverage nierówne (config.py, model_calls.py: 0%)

---

# 2. Code Review — znalezione bugi i problemy

## BUG 1: Dead import w model_calls.py (linia 17)

```python
from localcode.tool_handlers._state import SANDBOX_ROOT, MAX_FILE_SIZE
```

`SANDBOX_ROOT` jest importowane jako snapshot wartości (w momencie importu = `None`),
ale nigdy nie jest używane bezpośrednio. Kod na linii 161 poprawnie używa
`_tool_state.SANDBOX_ROOT` (dynamiczny odczyt). Import `SANDBOX_ROOT` to dead code.

**Fix:** Usunąć `SANDBOX_ROOT` z importu.

## BUG 2: Session nie działa dla agentów z `/` w nazwie

`create_new_session_path()` tworzy ścieżkę:
```python
f"{timestamp}_{agent_name}.json"  # np. "2026-01-29_gguf/gpt-oss-120b.json"
```

`/` w nazwie agenta (`gguf/gpt-oss-120b`) tworzy subdirectory.
`find_latest_session()` szuka globem `*_{agent_name}.json`, ale `glob.glob()`
bez `recursive=True` nie matchuje `/` w `*`.

**Efekt:** `--continue` mode nigdy nie znajduje poprzedniej sesji dla agentów
z namespace paths. Sesja jest zawsze nowa.

**Fix:** Zamienić `/` na `--` w nazwie sesji, lub użyć `Path(agent_name).name`.

## BUG 3: Brak error isolation w hooks.py

```python
def emit(event, data):
    for cb in _hooks.get(event, []):
        result = cb(data)  # ← brak try/except
```

Jeden wadliwy hook callback crashuje cały agent loop.

**Fix:** Wrap w try/except z logowaniem błędu.

## BUG 4: SANDBOX_ROOT triple-sync

SANDBOX_ROOT jest synchronizowany trzema mechanizmami:
1. `_SyncModule.__setattr__` w localcode.py (metaclass trick)
2. `__init__.py.__setattr__` proxy
3. Explicit `_tool_state.SANDBOX_ROOT = SANDBOX_ROOT` w `__main__`

To jest fragile — łatwo o desynchronizację. Powinno być jedno źródło prawdy.

## BUG 5: Duplikacja grep_fn / search_fn

`search_handlers.py`: `grep_fn` (linie 80-179) i `search_fn` (182-293) mają
~80% identycznego kodu. DRY violation.

## BUG 6: Bare `tuple` return type w session.py

```python
def load_session(...) -> tuple:  # powinno być Tuple[List[Dict], Optional[str]]
```

## BUG 7: `tool_before` nie modyfikuje faktycznie wywołania narzędzia [MAJOR]

**Miejsce:** `localcode.py:1324-1333`

`hooks.emit("tool_before", ...)` zwraca potencjalnie zmodyfikowane dane, ale wynik
(`before_data`) nie jest używany. `process_tool_call()` dostaje oryginalny `tc`,
więc modyfikacje zwrócone przez hook są ignorowane. Łamie API opisane
w `middleware/README.md`.

**Fix:** Po `hooks.emit` zastosować zmiany do `tc`/`tool_args` (np. jeśli
`before_data["tool_call"]` lub `before_data["tool_args"]` istnieje).
Dodać test integracyjny: hook zmienia argumenty, wynik potwierdza zmianę.

## BUG 8: `turn_end` emitowane tylko przy feedbacku [MAJOR]

**Miejsce:** `localcode.py:1429-1430`

`turn_end` jest emitowane wyłącznie w ścieżce z `feedback_text`. W normalnym
przebiegu (udane narzędzia lub finalny content) event nigdy nie występuje.
Middleware/logowanie oparte o `turn_end` nie działa.

**Fix:** Emitować `turn_end` zawsze po przetworzeniu tool calls (i/lub po
zakończeniu tury bez tool calls), niezależnie od feedbacku.

## BUG 9: Podwójne zliczanie `feedback_counts` i błędne numerowanie prób [MAJOR]

**Miejsce:** `middleware/metrics_hook.py:53-55` oraz `localcode.py:1405-1407`

`metrics_hook.on_tool_after` zwiększa `feedback_counts` gdy `feedback_reason`
istnieje, a następnie `run_agent` zwiększa ten sam licznik ponownie. Pierwsza
próba dostaje numer 2.

**Fix:** Zostawić inkrementację tylko w jednym miejscu. Usunąć inkrement
w `run_agent` i użyć zaktualizowanego licznika z `metrics_hook`.

## BUG 10: Off-by-one w liczniku nieudanych patchy [MEDIUM]

**Miejsce:** `localcode.py:1358` oraz `middleware/feedback_hook.py:186-188`

`patch_fail_count` przekazywany do hooka jest wartością sprzed aktualnego błędu.
`feedback_hook` pokazuje "SECOND FAILURE" dopiero przy trzeciej porażce.

**Fix:** Przekazywać zaktualizowaną wartość (`+1` przy `is_error`), albo
zarejestrować `metrics_hook` wcześniej tak, aby licznik był zaktualizowany
przed `feedback_hook`.

## BUG 11: `logging_hook` zapisuje pełne `result` z `tool_after` [MEDIUM]

**Miejsce:** `middleware/logging_hook.py:60-69`

`_on_event` filtruje `messages`, `request_data`, `response`, ale **nie** filtruje
`result`. Przy `tool_after` w logu ląduje pełna zawartość pliku (np. z `read`)
albo całe patche. Duże logi, ryzyko wycieku danych.

**Fix:** Dodać per-event filtr (usuwać `result`/`tool_args` lub je skracać),
albo logować tylko preview.

## BUG 12: `install()` w middleware nie jest idempotentne [MINOR]

**Miejsce:** `middleware/logging_hook.py:102-111` (analogicznie: `metrics_hook`,
`feedback_hook`, `conversation_dump`)

Każde wywołanie `install()` rejestruje nowe callbacki bez sprawdzenia, czy były
już zainstalowane. Przy wielokrotnym `install()` eventy/logi są powielane.

**Fix:** Wprowadzić flagę `_installed` albo `hooks.clear()`/kontrolę duplikatów.

## Inne problemy

- `ToolTuple` zdefiniowane w 3 miejscach (localcode.py, dispatch.py, schema.py)
- `think_default.txt` jest puste — _self_call dostaje pusty system prompt
- `conversation_dump.py` — silent exception swallowing (2x `except: pass`)
- Pipe detection gap: `ls|cat` (bez spacji) omija `shlex.split()` jako jeden token
- String-based error protocol (`"error: ..."`) — brak structured Result type

---

# 3. Test Coverage — gaps

## Test runner (docelowo)

- **Wybór:** pytest jako jedyny runner.
- pytest uruchamia również testy unittest (klasy `unittest.TestCase`) bez zmian,
  więc nie trzeba przepisywać `test_localcode.py` natychmiast.
- Docelowa komenda:
  - `python3 -m pytest -q localcode/tests`

## Moduły z 0% coverage

| Moduł | Publiczne funkcje | Status |
|-------|-------------------|--------|
| `config.py` | 7 funkcji | **0 testów** |
| `model_calls.py` (impl) | 5 funkcji | **0 testów** (testy patchują wrappery) |
| `session.py` (nowe fn) | `infer_run_name_from_path`, `sync_logging_context` | **0 testów** |
| `schema.py` | 6 funkcji | **1 test** |

## Brakujące testy

- `call_api()` — brak jakiegokolwiek testu
- `batch_read()` — user-facing tool, 0 testów
- `run_agent()` — brak integration test z prawdziwymi narzędziami
- `_coerce_cli_value()` — 7 ścieżek typów, 0 testów
- Entrypoint (`__main__`) — brak testu
- GLM `thinking` payload formatting — brak testu

## Brakujące testy integracyjne (hooks/middleware)

- Brak testów sprawdzających współpracę `run_agent` z hookami
- `tool_before` modyfikuje args → brak weryfikacji że zmiana jest zastosowana
- `turn_end` emitowane w każdym scenariuszu → brak testu
- Poprawne `attempt_num` po feedbacku → brak testu regresji
- Kolejność hooków (patch fail count vs. feedback) → brak testu

## Fragile tests

- 20+ testów manipuluje `_inner.SANDBOX_ROOT` bezpośrednio
- 5+ testów patchuje `localcode.localcode.call_api` — refactoring API breaks them
- Brak `conftest.py`, brak shared fixtures
- Global state leaks między testami (FILE_VERSIONS, _NOOP_COUNTS)

---

# 4. Kierunki rozwoju

## 4.1 Task Decomposition (PRIORYTET WYSOKI)

### Jak to robi Claude Code

Claude Code używa narzędzia **TodoWrite** (odpowiednik TaskCreate/TaskUpdate):
- Tworzy **structured JSON task lists** z: id, content, status (pending/in_progress/completed)
- Po każdym tool call **re-injectuje aktualny stan tasków** jako system message
- Task list działa jako **working memory** — zapobiega "goal drift"

### Propozycja dla localcode

**Nowe narzędzie: `plan_tasks`**

```json
{
  "name": "plan_tasks",
  "parameters": {
    "action": "string (create|update|list)",
    "tasks": "array? [{id, description, status, priority}]",
    "task_id": "string?",
    "status": "string? (pending|in_progress|completed)"
  }
}
```

**Architektura:**
1. Nowy moduł `localcode/task_manager.py` (~200 linii)
2. Task state trzymany w pamięci + zapisywany do sesji
3. Po KAŻDYM tool call: wstrzykuj `[TASKS]` do **system promptu** (nie dopisuj do `messages`)
4. Prompt classifier (patrz 4.2) decyduje czy potrzebny task decomposition

**Flow:**
```
User prompt → Complexity classifier →
  IF complex: plan_tasks(create) → subtasks → execute each
  IF simple: execute directly
```

## 4.2 Prompt Complexity Classification

### Użycie structured output

Structured output może ocenić złożoność promptu PRZED uruchomieniem agenta.

**Podejście 1: Self-call z JSON schema (dla lokalnych modeli)**

```python
complexity_schema = {
    "type": "object",
    "properties": {
        "complexity": {"type": "string", "enum": ["simple", "moderate", "complex"]},
        "subtasks": {
            "type": "array",
            "items": {"type": "string"}
        },
        "estimated_tools": {"type": "integer"},
        "requires_planning": {"type": "boolean"}
    }
}
```

Użyć `_self_call()` z dedykowanym system promptem + wymusić JSON output.

**Podejście 2: Fake tool trick (lepsze dla modeli bez structured output)**

Zdefiniować tool `classify_prompt` z odpowiednim schema — model zwraca
structured data jako tool call arguments.

**Rekomendacja:** Podejście 2 (fake tool) — działa z każdym modelem,
nie wymaga constrained decoding.

## 4.3 Conversation Compaction (PRIORYTET WYSOKI)

### Problem

Aktualnie `trim_messages()` tylko ucina najstarsze wiadomości gdy context > 400k chars.
Nie ma summarization — tracimy informacje.

### Jak to robi Claude Code

- Trigger przy ~85-90% context utilization
- Używa **tańszego modelu** (Haiku) do generowania summary
- Summary opakowuje w `<summary></summary>` tagi
- Czyści historię, zostawia tylko summary + ostatnie N wiadomości
- `/compact` command do ręcznego triggerowania

### Propozycja

**Nowy moduł: `localcode/compaction.py` (~150 linii)**

```python
def should_compact(messages, max_chars, threshold=0.85):
    """Return True when context usage > threshold."""
    current = sum(len(m.get("content","")) for m in messages)
    return current > max_chars * threshold

def compact_messages(messages, api_url, model, keep_last_n=10):
    """Summarize old messages, keep recent ones."""
    old = messages[:-keep_last_n]
    recent = messages[-keep_last_n:]

    summary = _self_call(
        prompt="Summarize this conversation preserving: file paths, decisions, errors, task progress.",
        system_prompt=COMPACTION_PROMPT,
        include_history=False,
        # ... pass old messages as context
    )

    return [{"role": "user", "content": f"<summary>{summary}</summary>"}] + recent
```

**Integracja z agent loop:**
```python
# W run_agent(), przed call_api():
if should_compact(messages, MAX_CONTEXT_CHARS):
    messages = compact_messages(messages, API_URL, MODEL)
    logging_hook.log_event("compaction", {"before": len_before, "after": len(messages)})
```

## 4.4 Task-Scoped Conversation History

### Problem

User chce żeby subtaski miały własną historię konwersacji, ale mogły korzystać
z głównej.

### Propozycja

```python
class TaskContext:
    def __init__(self, task_id, parent_messages=None):
        self.task_id = task_id
        self.messages = []           # Task-specific messages
        self.parent_summary = None   # Compact summary of parent conversation

    def get_full_context(self):
        """Return messages for API call: parent summary + task messages."""
        ctx = []
        if self.parent_summary:
            ctx.append({"role": "user", "content": f"<context>{self.parent_summary}</context>"})
        ctx.extend(self.messages)
        return ctx
```

**Flow:**
```
Main conversation → plan_tasks creates subtasks →
  For each subtask:
    1. Compact main conversation to summary
    2. Create TaskContext(task_id, parent_summary=summary)
    3. Run sub-agent with task context
    4. Merge to main conversation: **summary only**
       (status + lista plików + krótki opis zmian; bez pełnej historii taska)
    5. Mark task completed
```

### Acceptance Criteria — Task Branch Execution

- **Plan-first:** dla promptów klasyfikowanych jako `complex` pierwszy krok to `plan_tasks(create)`; brak write tools przed planem.
- **Izolacja kontekstu:** każdy subtask działa tylko na `TaskContext` (parent summary + task messages), bez pełnej historii głównej.
- **Merge = summary only:** do głównej konwersacji trafia **jedna** wiadomość per task (status + pliki + krótki opis zmian + ewentualny błąd), bez tool calls i bez pełnej historii taska.
- **Brak zapychania historii:** lista tasków jest wstrzykiwana do `system_prompt`, nie dopisywana do `messages`.
- **Ślad audytowy:** logi zawierają `task_start`/`task_end` z `task_id`, `status`, `files_changed`, `summary_len`.

## 4.5 Batching Tool Calls

### Obecny stan

`max_batch_tool_calls` jest przekazywany do API, ale obsługa po stronie agenta
jest prosta — przetwarza all tool calls sekwencyjnie.

### Propozycja ulepszeń

1. **Parallel tool execution** — niezależne tool calls (np. 3x read) mogą być
   wykonane równolegle (ThreadPoolExecutor)
2. **Semantic batching** — `TOOL_CATEGORIES` (read/write) umożliwia grupowanie:
   read tools mogą iść równolegle, write tools sekwencyjnie
3. **Batch read optimization** — `batch_read` tool jest już zaimplementowany,
   ale nie jest testowany ani szeroko używany

**Uwaga o bezpieczeństwie wątków:** obecne globalne stany (`FILE_VERSIONS`,
`_NOOP_COUNTS`, logowanie, metryki) nie są thread-safe. Parallelism powinien
być **tylko dla read-only tools** i wymagać minimalnej synchronizacji albo
dedykowanej kolejki wyników.

## 4.6 Planning Mode

### Propozycja

Wzorowane na Claude Code's plan mode:

1. **Faza exploration** — agent ma dostęp TYLKO do read tools (read, grep, glob, ls)
2. **Faza planning** — agent tworzy plan via `plan_tasks`
3. **Faza execution** — agent ma dostęp do wszystkich tools

**Implementacja:**
```python
# W build_tools() lub run_agent():
if planning_mode:
    tools_dict = {k: v for k, v in tools_dict.items()
                  if TOOL_CATEGORIES.get(k) == "read"}
```

Agent config:
```json
{
  "planning_mode": "auto",
  "planning_trigger": "complex"
}
```

---

# 5. Techniki z Claude Code do zaadoptowania

## 5.1 Task List Injection (MUST HAVE)

Nie dopisuj tasków jako nowych `messages` — to szybko zapycha kontekst.
Zamiast tego **wstrzykuj aktualny stan zadań do system promptu** przed
każdym `call_api()` (out-of-band context, bez rozrostu historii).

```python
# W run_agent(), tuż przed call_api():
if task_manager.has_tasks():
    task_state = task_manager.format_tasks()  # markdown checklist
    system_prompt = f"{base_system_prompt}\n[TASKS]\n{task_state}"
```

## 5.2 Two-Phase Workflow

Explore → Plan → Execute. Agent najpierw czyta i analizuje,
potem planuje, potem implementuje. Daje lepsze wyniki niż "dive in".

## 5.3 Checkpoint/Snapshot System

Przed każdym write/edit/patch — zapisz aktualny stan pliku.
Umożliwia rollback przy błędzie.

```python
FILE_SNAPSHOTS = {}  # path -> previous_content

def snapshot_file(path):
    if os.path.exists(path):
        FILE_SNAPSHOTS[path] = open(path).read()
```

## 5.4 CLAUDE.md jako Project Memory

Już mamy `CLAUDE.md` w projekcie. Rozszerzyć o:
- Sekcja `## Compact Instructions` — co zachować przy compaction
- Sekcja `## Task Patterns` — typowe wzorce tasków
- Auto-update po zakończeniu sesji

## 5.5 Sub-Agent Depth Limiting

`_subprocess_call` uruchamia sub-agenta. Dodać:
- Max depth = 1 (sub-agent nie może uruchomić sub-agenta)
- Env var `LOCALCODE_AGENT_DEPTH=1` przekazywane do subprocess
- Sprawdzenie na starcie: if depth >= max_depth → refuse

---

# 6. Structured Output — zastosowania

## Gdzie może się przydać

| Zastosowanie | Opis | Priorytet |
|-------------|------|-----------|
| Prompt classification | Ocena złożoności, rozbicie na subtaski | Wysoki |
| Tool argument repair | Wymuś poprawny format argumentów | Średni |
| Plan generation | Structured plan z krokami, zależnościami | Wysoki |
| Summary generation | Structured summary przy compaction | Średni |
| Error classification | Kategoryzacja błędów tool calls | Niski |

## Implementacja dla lokalnych modeli

Lokalne modele (GPT-OSS, GLM) nie wspierają Anthropic's constrained decoding.
Alternatywy:

1. **Fake tool trick** — zdefiniuj tool z pożądanym schema, wymuś tool call
2. **JSON mode** — llama.cpp wspiera `response_format: {"type": "json_object"}`
3. **Grammar-based** — llama.cpp wspiera GBNF grammar constraints
4. **Post-processing** — parse odpowiedzi regexem, retry na failure

**Rekomendacja:** Fake tool trick + JSON mode jako fallback.

---

# 7. Proponowany plan implementacji (fazy)

## Phase 4a: Correctness & hook contracts (must-fix)

1. Fix BUG 7: `tool_before` honoruje zmiany (apply do `tc`/`tool_args` **+ re-walidacja**) [MAJOR]
2. Fix BUG 8: `turn_end` emitować zawsze (tool calls success + feedback + brak tool calls) [MAJOR]
3. Fix BUG 9: podwójne zliczanie `feedback_counts` — jedna źródłowa inkrementacja [MAJOR]
4. Fix BUG 10: off-by-one w `patch_fail_count` — aktualizować licznik **przed** `tool_after` [MEDIUM]
5. Fix BUG 11: logging_hook — skrócić/odfiltrować `result` w `tool_after` [MEDIUM]
6. Fix BUG 2: session path z `/` — sanitizacja **+ kompatybilność wsteczna** [MAJOR]
7. Fix BUG 3: error isolation w hooks.py (log&continue + opcjonalny strict mode)
8. Fix BUG 12: idempotentne `install()` w middleware [MINOR]
9. Fix BUG 1: dead import w model_calls.py

## Phase 4b: Test runner + coverage gaps

1. Ujednolicić runner: **pytest jako jedyny runner**
2. Dodać testy integracyjne hook contracts:
   - `tool_before` modyfikuje args i wpływa na realne wykonanie
   - `turn_end` emitowany w każdym scenariuszu
   - poprawne `attempt_num` po feedbacku
3. Dodać testy: `config.py`, `model_calls.py`, `batch_read`, `schema.py`
4. Dodać `conftest.py` / shared fixtures + reset global state

## Phase 4c: Refaktoryzacje (non-blocking)

1. Deduplikacja `grep_fn` / `search_fn`
2. Type annotations w `session.py`
3. Ujednolicenie definicji `ToolTuple` (opcjonalnie)

## Phase 5: Conversation Compaction

1. Dodać pole `context_window` do agent JSON schema (potrzebne do % threshold)
2. `localcode/compaction.py` — summarization engine z narrative prompt (9.4)
3. Integracja z `run_agent()` — auto-trigger przy 90% context window
4. `/compact` command w interactive mode
5. Testy

## Phase 6: Task Decomposition + Planning

1. `localcode/task_manager.py` — task state management
2. Tool definition: `tools/plan_tasks.json`
3. Prompt classifier (complexity assessment)
4. Task list injection **do system promptu** przed `call_api()`
5. Task-scoped conversation history (TaskContext)
6. Planning mode (read-only phase)
7. Testy

## Phase 7: Structured Output + Batching

1. JSON mode / fake tool trick dla prompt classification
2. Parallel tool execution dla read tools
3. Sub-agent depth limiting
4. Checkpoint/snapshot system

---

# 8. Weryfikacja

Każda faza powinna przejść:
1. **Testy (pytest):**  
   - `python3 -m pytest -q localcode/tests`
2. Benchmark smoke test: `./bin/run-benchmark.sh <model> -k space-age`
3. Manual test interactive mode: `python3 localcode/localcode.py -a <agent>`
4. Sprawdzenie line counts — localcode.py nie powinien rosnąć

---

# 9. Inspiracje z konkurencyjnych projektów

Zestawienie funkcji z OpenCode, Factory Droid, Aider i Goose, z oceną
przydatności dla localcode i mapowaniem na istniejące sekcje tego dokumentu.

## 9.1 Git Snapshot przed każdym krokiem (OpenCode) — PRIORYTET WYSOKI

OpenCode robi `git write-tree` do shadow repo przed KAŻDYM tool call.
Pozwala na rollback po nieudanym edycie. Lepsze niż in-memory `FILE_SNAPSHOTS`
(sekcja 5.3), bo przeżywa crash procesu.

**Jak działa:**
- Shadow git repo w `.localcode-shadow/` (oddzielny od user repo)
- Przed write/edit/patch → `git add -A && git write-tree` → zapisz tree hash
- Rollback = `git read-tree <hash> && git checkout-index -a -f`
- Zero overhead dla usera — shadow repo jest niewidoczny

**Fallback gdy brak git:**
- Sandbox directory (benchmark exercises) to klonowany repo — `.git` istnieje.
- W ogólnym użyciu katalog może nie mieć `.git`. Wtedy:
  `git init .localcode-shadow/ && git -C .localcode-shadow/ ...`
  (shadow repo inicjalizowany automatycznie, niezależnie od user repo).
- Ostateczny fallback: in-memory snapshots (sekcja 5.3).

**Relacja do localcode:** To jest NOWA struktura, nie zamiennik `FILE_VERSIONS`.
- `FILE_VERSIONS` (w `_state.py`) = LRU cache zawartości **odczytanych** plików.
  Służy do porównań (np. czy write jest noop). Max 200 entries.
- `FILE_SNAPSHOTS` (sekcja 5.3) = kopia pliku **przed write/edit/patch**.
  Służy do rollbacku. Nie istnieje jeszcze w kodzie.
- Git shadow snapshot = persistent wersja FILE_SNAPSHOTS, przeżywa crash.
Te trzy struktury mają różne cele i współistnieją.

## 9.2 LSP Diagnostics Feedback Loop (OpenCode) — PRIORYTET WYSOKI

Po każdym write/edit — OpenCode wysyła `textDocument/didChange` do LSP servera,
czeka na diagnostyki, i feeduje je z powrotem do modelu. Agent widzi błędy
kompilacji/typów w real-time. Dramatycznie poprawia jakość kodu.

**Jak działa:**
- LSP client jako subprocess (pylsp, tsserver, clangd)
- Po tool call `write_file` / `edit_file` → notify LSP → wait ~2s → collect diagnostics
- Inject diagnostics jako tool result: `"File saved. LSP diagnostics: error on line 42: ..."`
- Model automatycznie naprawia błędy w następnym kroku

**Implementacja w localcode:**
- Nowy moduł `localcode/lsp_client.py`
- **V1 (pragmatyczne ~150 linii):** wrapper na shell — `pylsp`/`tsserver` uruchamiany
  jako subprocess per-check, parse stdout. Proste, ale wolniejsze.
- **V2 (pełne ~400-600 linii):** persistent LSP subprocess z JSON-RPC protocol,
  `initialize`/`didOpen`/`didChange`/`publishDiagnostics`. Szybsze, ale złożone.
- Hook `tool_after` w hooks.py → jeśli tool był write/edit/patch → trigger LSP check
- Config w agent JSON: `"lsp": {"python": "pylsp", "javascript": "tsserver"}`
- **Rekomendacja:** Zacząć od V1 (shell wrapper), upgrade do V2 jeśli latencja przeszkadza.

**Relacja do localcode:** Nowa funkcjonalność, komplementarna z lint-after-edit (9.6).

## 9.3 Multi-Trajectory Validation (Factory Droid) — PRIORYTET ŚREDNI

Generowanie 2-3 kandydackich rozwiązań, walidacja testami (istniejącymi +
auto-wygenerowanymi), wybór najlepszego. pass@3 daje ~30% lepsze wyniki
niż pass@1.

**Jak działa:**
- Dla danego subtaska → uruchom N niezależnych agent runs (różne temperature)
- Każdy kandydat walidowany: testy, linter, LSP diagnostics
- Score = (tests_passed, lint_warnings, loc_changed)
- Wybierz kandydata z najlepszym score

**Implementacja w localcode:**
- **UWAGA:** To NIE jest rozszerzenie `_self_call()` (która robi single-turn text).
  Multi-trajectory wymaga N pełnych **agent runs** (`run_agent()` lub
  `_subprocess_call()`), każdy z wieloma tool calls.
- N × `_subprocess_call()` z różnymi temperature — każdy zwraca pełne rozwiązanie
- **Ograniczenie sprzętowe:** Na single GPU (typowy setup localcode) — N runs musi
  być sekwencyjnych, bo model obsługuje 1 request naraz. Parallel execution
  wymaga multi-GPU lub batched inference w llama.cpp (`--parallel N`).
- Validator pipeline: run tests → lint → score
- Config: `"multi_trajectory": {"enabled": true, "n": 3, "temperature_range": [0.3, 0.8]}`

**Relacja do localcode:** Nowa funkcjonalność dla Phase 7. Wymaga structured output
(sekcja 6) do score comparison. Praktyczne głównie z szybkimi modelami (GLM Flash)
lub przy multi-GPU setup.

## 9.4 Auto-Compact z narrative continuity (OpenCode) — PRIORYTET WYSOKI

Trigger przy 90% context window. Summary prompt zachowuje:
"what we did, which files we're working on, what we're going to do next."
To jest dokładnie to co planujemy w sekcji 4.3, ale z lepszym promptem.

**Kluczowa różnica vs sekcja 4.3:**

Sekcja 4.3 proponuje generyczny prompt:
```
"Summarize this conversation preserving: file paths, decisions, errors, task progress."
```

OpenCode używa **narrative prompt**:
```
"Create a summary that captures:
1. What we've accomplished so far (completed actions, files modified)
2. What files we're currently working on and their state
3. What we need to do next (pending tasks, next steps)
4. Any important decisions or constraints discovered"
```

**Rekomendacja:** Zastąpić generyczny prompt z sekcji 4.3 promptem narrative-style.
Dodać do `localcode/prompts/compact_narrative.txt`.

**Ważna różnica vs Claude Code / OpenCode:**
- Claude Code używa tańszego modelu (Haiku) do summarization.
- localcode działa z jednym lokalnym modelem — `_self_call()` używa tych samych
  globali `API_URL`/`MODEL`. Nie ma drugiego tańszego modelu dostępnego.
- **Konsekwencja:** Compaction zużywa inference time tego samego modelu.
  Przy dużym modelu (120B) to jest kosztowne. Rozwiązania:
  1. Krótki summary prompt (minimalizuj output tokens)
  2. Model routing (9.10) w przyszłości — GLM Flash do summarization
  3. Compaction rzadziej (95% threshold zamiast 85%)

**Prerequisite:** Dodać pole `context_window` do agent JSON schema.
Aktualnie `trim_messages()` używa hardcoded `max_chars=400_000`, a agent configs
mają tylko `max_tokens` (output limit). Bez `context_window` nie da się obliczyć
procentowego threshold (85%/90%). Przykład:
```json
{
  "context_window": 131072,
  "max_tokens": 16000
}
```

## 9.5 Repository Map (Aider) — PRIORYTET ŚREDNI

Aider buduje mapę repo: function signatures + file structure.
Daje modelowi kontekst o CAŁYM codebase bez ładowania wszystkiego do context.

**Jak działa:**
- `ctags` lub `tree-sitter` parsuje cały repo
- Output: lista plików + function/class signatures (bez body)
- Inject jako system message: `"Repository structure:\n{repo_map}"`
- Refresh po każdym write/edit
- Aider używa ranking: pliki bliższe do edytowanych mają wyższy priorytet

**Implementacja w localcode:**
- Nowy moduł `localcode/repo_map.py` (~150 linii)
- `generate_map(root_dir) → str` — compact representation
- Backend: `ctags --output-format=json` lub `tree-sitter` bindings
- Cache z invalidation na file change
- Config: `"repo_map": {"enabled": true, "max_tokens": 2000}`

**Relacja do localcode:** Nowa funkcjonalność. Komplementarna z planning mode (4.6) —
repo map daje contekst w fazie exploration.

## 9.6 Lint-after-edit (Aider) — PRIORYTET WYSOKI

Aider automatycznie lintuje i testuje po KAŻDEJ zmianie.
Połączenie z LSP diagnostics (punkt 9.2) daje pełny feedback loop.

**Jak działa:**
- Po write/edit → run configured linter (flake8, eslint, etc.)
- Parse output → inject jako tool result
- Jeśli są errors → model automatycznie naprawia
- Opcjonalnie: run tests po edycji (configurable)

**Implementacja w localcode:**
- Hook `tool_after` → check if tool was write/edit → run linter
- Config w agent JSON:
  ```json
  "lint_after_edit": {
    "python": "flake8 --max-line-length=120",
    "javascript": "eslint --format=compact"
  }
  ```
- Inject lint output do conversation jako assistant observation

**Relacja do localcode:** Komplementarna z LSP (9.2). LSP = type errors,
lint = style + bugs. Razem dają pełny feedback loop.

## 9.7 /context auto-file-selection (Aider) — PRIORYTET ŚREDNI

Komenda `/context` automatycznie identyfikuje które pliki trzeba edytować
dla danego requesta. Oszczędza manual "add file to chat".

**Jak działa:**
- User wpisuje request → Aider analizuje repo map + request
- Ranking plików po relevance (embedding similarity + dependency graph)
- Top-N plików automatycznie dodanych do context
- User może zatwierdzić lub zmodyfikować listę

**Implementacja w localcode:**
- Rozszerzenie planning mode (4.6) — w fazie exploration agent sam wybiera pliki
- Alternatywnie: `_self_call()` z promptem "Which files need editing for: {request}?"
- Użyć repo map (9.5) jako input do file selection

**Relacja do localcode:** Rozszerzenie planning mode (sekcja 4.6).

## 9.8 MCP jako extension system (Goose) — PRIORYTET NISKI

Goose używa MCP (Model Context Protocol) jako uniwersalnego systemu pluginów.
Każdy MCP server = bridge do zewnętrznego serwisu (GitHub, Slack, DB).

**Ocena dla localcode:** Potencjalnie ciekawe, ale wymaga dużo pracy.
localcode ma już hook system i tool definitions w JSON — MCP byłby kolejną
warstwą abstrakcji. Rozważyć dopiero po Phase 7.

## 9.9 DroidShield — static analysis safety (Factory Droid) — PRIORYTET ŚREDNI

Real-time static analysis przed commitem: security vulnerabilities, bugs,
IP breaches.

**Implementacja w localcode:**
- Hook `tool_after` (gdy tool = write/edit/patch) → run static analyzers
- Narzędzia: `bandit` (Python), `eslint` (JS), `semgrep` (multi-lang)
- Config: `"static_analysis": {"tools": ["bandit", "semgrep"], "block_on": "high"}`
- Inject wyniki do conversation — model widzi i naprawia

**Relacja do localcode:** Komplementarna z sandbox (tool_handlers/_sandbox.py).
Sandbox blokuje niebezpieczne komendy, DroidShield analizuje wygenerowany kod.

## 9.10 Model routing per subtask (Factory Droid) — PRIORYTET NISKI

Używanie różnych modeli do różnych subtasków: szybki model do klasyfikacji,
mocny do generowania kodu, tani do summarization.

**Relacja do localcode:** localcode ma `_self_call` — mógłby routować do
innego modelu. Wymaga multi-model config w agent JSON:
```json
"models": {
  "default": "gpt-oss-120b",
  "classify": "glm-4.7-flash",
  "summarize": "glm-4.7-flash"
}
```

Rozważyć po Phase 7.

## 9.11 Plan/Build dual agents (OpenCode) — PRIORYTET WYSOKI

OpenCode ma 2 wbudowane agenty: `plan` (read-only) i `build` (full access).
Switch z Tab.

**Relacja do localcode:** Dokładnie to co planujemy w sekcji 4.6, ale z osobnymi
agent configs zamiast runtime tool filtering. Rekomendacja: osobne agent JSON
(`agents/plan-*.json` z `"tools": ["read_file", "grep", "glob", "list_dir"]`)
zamiast dynamicznego filtrowania w `build_tools()`.

**Niska złożoność implementacji:** Agent configs już mają pole `"tools"` (lista
dozwolonych narzędzi). Wystarczy stworzyć nowe pliki JSON z ograniczoną listą —
`build_tools()` i `schema.py` automatycznie zbudują odpowiedni toolset. Żaden
kod w Pythonie nie wymaga zmian. Jedyne co trzeba dodać: mechanizm przełączania
plan→build w runtime (np. komenda `/build` lub automatyczny trigger po plan_tasks).

## 9.12 Non-interactive / scripting mode (OpenCode) — PRIORYTET ŚREDNI

`opencode -p "fix bug" -f json` — single-shot mode z JSON output.
Umożliwia CI/CD integration i scripting.

**Implementacja w localcode:**
- Nowe flagi CLI: `-p/--prompt` (single-shot), `-f/--format` (json/text)
- W `config.py`: parse nowych flag (obok istniejących `--file`/`-f`, `--agent`, etc.)
- W `run_agent()`: jeśli `--prompt` → single run → output → exit
- JSON output: `{"status": "ok", "files_modified": [...], "output": "..."}`

**Relacja do localcode:** Rozszerzenie config.py (Phase 3). Niska złożoność.
**Uwaga:** Częściowo już istnieje — `--file`/`-f` czyta prompt z pliku,
`run_agent()` zwraca po final content (single-run). Brakuje: (a) `--prompt` dla
inline tekstu, (b) `--format json` dla structured output, (c) suppressja
interactive prints (kolorowe statusy, progress bars).

---

## 9.13 Podsumowanie: mapowanie na fazy implementacji

### Phase 4a/4b (correctness + tests) — MUST HAVE
**WAŻNE:** BUG 7 (`tool_before` ignorowany) i BUG 8 (`turn_end` nie emitowany)
są **hard prerequisites** dla Phase 5.5 — feedback loop opiera się na poprawnym
działaniu hooków `tool_after` i `turn_end`. Bez Phase 4a/4b → Phase 5.5 nie działa.

### Phase 5 (compaction) — rozszerzyć:
- **Auto-compact z narrative prompt** (9.4) — użyć OpenCode-style promptu
  zamiast generycznego z sekcji 4.3
- Dodać pole `context_window` do agent JSON schema (prerequisite dla % threshold)

### Phase 5.5 (NOWA FAZA — feedback loop):
1. **Git shadow snapshot** (9.1) — `git write-tree` do `.localcode-shadow/`
2. **LSP diagnostics** (9.2) — po write/edit, query LSP via `tool_after` hook, feed do modelu
3. **Lint-after-edit** (9.6) — hook `tool_after` → run linter → inject wynik

### Phase 6 (task decomp) — rozszerzyć:
- **Plan/Build dual agents** (9.11) — osobne agent configs
  (`agents/plan-*.json` vs `agents/build-*.json`)

### Phase 7 (structured output) — rozszerzyć:
- **Multi-trajectory** (9.3) — generate N, validate, pick best
- **Repo map** (9.5) — ctags/tree-sitter compact index

### BACKLOG (post Phase 7):
- MCP extensions (9.8) — wymaga dużo pracy, niski ROI teraz
- DroidShield static analysis (9.9) — nice-to-have, niska złożoność
- Model routing (9.10) — wymaga multi-model infra
- Non-interactive mode (9.12) — niska złożoność, ale niszowy use case
- /context auto-file-selection (9.7) — zależny od repo map (9.5)
