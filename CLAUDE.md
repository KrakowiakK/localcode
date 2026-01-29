# BENCHMARK - Claude Notes

## Struktura projektu

```
BENCHMARK/
├── bin/
│   ├── llama-server              # Binary (z build-llama.sh)
│   ├── build-llama.sh            # Buduje llama.cpp
│   ├── start-server.sh           # Startuje serwer
│   ├── stop-server.sh            # Zatrzymuje serwer
│   ├── setup-benchmark.sh        # Klonuje polyglot + buduje Docker
│   ├── run-benchmark.sh          # Runner + statystyki
│   └── run-localcode-benchmark.sh # Docker runner
├── benchmark/                    # Standalone benchmark code
│   ├── benchmark.py              # Główny skrypt benchmarku
│   ├── Dockerfile                # Docker image (runtimes only)
│   ├── npm-test.sh               # JS test runner
│   ├── cpp-test.sh               # C++ test runner
│   ├── tmp.benchmarks/           # Dane benchmarku
│   │   └── polyglot-benchmark/   # Ćwiczenia (klonowane przez setup)
│   └── tmp.benchmark/            # Wyniki
├── localcode/                    # Benchmark agent (Python, native tool calls)
│   └── agents/
│       ├── mlx/                  # Agenci dla serwera MLX
│       └── gguf/                 # Agenci dla serwera GGUF
└── llama.cpp/                    # Źródło llama.cpp (z build-llama.sh)
```

**Szczegółowa dokumentacja:**
- `server_mlx/CLAUDE.md` - MLX server

## Quick Start

### Pierwszy raz — setup

```bash
# Klonuje polyglot-benchmark, buduje Docker
./bin/setup-benchmark.sh
```

### Benchmark (GGUF / llama.cpp)

```bash
# Zbuduj llama.cpp (pierwszy raz)
./bin/build-llama.sh

# Start serwer + benchmark
./bin/start-server.sh gpt-oss-120b-mxfp4 --background
./bin/run-benchmark.sh gpt-oss-120b-mxfp4 -k space-age
./bin/stop-server.sh

# Więcej testów
./bin/run-benchmark.sh glm-4.7-flash -k space-age,leap

# Wszystkie testy JS
./bin/run-benchmark.sh gpt-oss-120b-mxfp4 --all
```

### MLX (Apple Silicon)

```bash
# Uruchom serwer MLX ręcznie
cd server_mlx && source mlx_env/bin/activate && python main.py

# Benchmark (w innym terminalu)
./bin/run-benchmark.sh glm-4.7-flash -k space-age
```

## Serwery

### llama-server (GGUF)

```bash
# Start z agent config
./bin/start-server.sh gpt-oss-120b-mxfp4 --background

# Sprawdź status
curl http://localhost:1235/health

# Zatrzymaj
./bin/stop-server.sh
```

### MLX Server (port 1234)

```bash
# Uruchom serwer
cd server_mlx && source mlx_env/bin/activate && python main.py

# Sprawdź status
curl http://localhost:1234/health
```

## Architektura

```
              ┌────────────────────────────────────────┐
              │        bin/ Scripts                     │
              │  setup-benchmark.sh (setup)             │
              │  run-benchmark.sh (runner + stats)      │
              │  run-localcode-benchmark.sh (Docker)    │
              │  start-server.sh / stop-server.sh       │
              │  build-llama.sh                         │
              └───────────────┬────────────────────────┘
                              │
          ┌───────────────────┼───────────────────┐
          │                   │                   │
          ▼                   ▼                   ▼
┌─────────────────┐   ┌─────────────┐   ┌─────────────┐
│ llama-server    │   │   Docker    │   │  Results    │
│ (bin/)          │   │  benchmark  │   │ benchmark/  │
├─────────────────┤   └──────┬──────┘   │ tmp.bench   │
│ MLX Server      │          │          │ mark/       │
│ :1234           │          ▼          └─────────────┘
└─────────────────┘  ┌─────────────┐
                     │ localcode   │
                     │ agent       │
                     │ (mounted)   │
                     └─────────────┘
```

## Modele

### MLX

| Model | Plik konfiguracji |
|-------|-------------------|
| GPT-OSS 120B | `localcode/agents/mlx/gpt-oss-120b.json` |
| GLM 4.7 Flash | `localcode/agents/mlx/glm-4.7-flash.json` |

### GGUF

| Model | Plik konfiguracji | Ścieżka GGUF |
|-------|-------------------|--------------|
| GPT-OSS 120B | `localcode/agents/gguf/gpt-oss-120b.json` | `~/.lmstudio/models/unsloth/gpt-oss-safeguard-120b-GGUF/` |
| GLM 4.7 Flash | `localcode/agents/gguf/glm-4.7-flash.json` | `~/.lmstudio/models/unsloth/GLM-4.7-Flash-GGUF/` |

## Ważne ścieżki

| Ścieżka | Opis |
|---------|------|
| `bin/` | Wszystkie skrypty |
| `localcode/agents/mlx/*.json` | Agenci MLX |
| `localcode/agents/gguf/*.json` | Agenci GGUF |
| `localcode/prompts/*.txt` | Prompty systemowe |
| `benchmark/tmp.benchmark/` | Wyniki |
| `/tmp/benchmark-llama-server.log` | Logi serwera llama |
| `/tmp/benchmark-server.pid` | PID serwera |

## Debugging

```bash
# Logi serwera llama
tail -f /tmp/benchmark-llama-server.log

# Logi agenta z benchmarku
cat benchmark/tmp.benchmark/.../space-age/localcode_stderr.log

# Health check
curl http://localhost:1235/health | python3 -m json.tool
```

## Docker

```bash
# Setup (klonuje polyglot + buduje Docker)
./bin/setup-benchmark.sh

# Rebuild Docker image
./bin/setup-benchmark.sh --rebuild

# Update repos + rebuild
./bin/setup-benchmark.sh --update
```

## Harmony Format (GPT-OSS)

GPT-OSS używa specjalnego formatu Harmony dla tool calling:
```
<|start|>assistant<|channel|>commentary to=functions.read_file<|message|>{"path":"/foo"}<|call|>
```

**MLX:** Harmony parser w `server_mlx/app/harmony/`
**GGUF:** Harmony parser (port) w `server_gguf/harmony/`

GLM 4.7 Flash używa standardowego JSON - nie wymaga Harmony parsera.

## GPT-OSS-120B: Ważne zmiany w llama.cpp

**UWAGA:** Przy aktualizacji `llama.cpp/` pamiętaj o poprawce template Jinja!

W pliku `llama.cpp/models/templates/openai-gpt-oss-120b.jinja` (linia 12):

```jinja
// Zmień z:
{%- if param_spec['items'] -%}

// Na:
{%- set array_items = param_spec.get('items') -%}
{%- if array_items -%}
```

**Przyczyna:** `param_spec['items']` konfliktuje z metodą dict `.items()` w Jinja2 llama.cpp.

**Alternatywa:** Użyj `--chat-template-file` w agent JSON `extra_args`:
```json
"extra_args": ["--chat-template-file", "llama.cpp/models/templates/openai-gpt-oss-120b.jinja"]
```

Szczegóły: `localcode/README.md` → sekcja "GPT-OSS-120B Specific Fixes"
