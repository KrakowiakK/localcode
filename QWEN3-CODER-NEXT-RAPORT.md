# Qwen3-Coder-Next — Raport techniczny

> Data: 2026-02-11 | Kontekst: optymalizacja tool-call stability na M3 Ultra

---

## 1. Architektura modelu

| Parametr | Wartość |
|---|---|
| Parametry (total) | 80B |
| Parametry (aktywne/token) | 3B |
| Non-embedding | 79B |
| Hidden dim | 2048 |
| Warstwy | 48 |
| Kontekst (natywny) | 262 144 (256K) |
| Kontekst (YaRN) | 1M+ |
| Thinking mode | **Brak** (non-thinking only) |
| Licencja | Apache 2.0 |

### Hybrid layout
```
12 × (3 × (Gated DeltaNet → MoE) + 1 × (Gated Attention → MoE))
```
- **3:1 ratio** — linear attention (DeltaNet, O(n)) do full attention (GQA)
- Gated DeltaNet: 32 V-heads, 16 QK-heads, head_dim=128
- Gated Attention: 16 Q-heads, 2 KV-heads, head_dim=256, RoPE dim=64

### MoE
- **512 ekspertów**, 10 aktywnych/token + 1 shared
- Expert intermediate dim: 512

### Tokenizer
- Standardowy Qwen3 tokenizer, ChatML-style special tokens
- `<|im_start|>`, `<|im_end|>` (= EOS)
- FIM: `<|fim_prefix|>`, `<|fim_suffix|>`, `<|fim_middle|>`

---

## 2. Tool Call Format — KLUCZOWE

### Qwen3-Coder-Next używa CUSTOM XML, NIE JSON

To jest fundamentalna różnica wobec Qwen3-Instruct (Hermes JSON) i OpenAI:

```xml
<tool_call>
<function=function_name>
<parameter=param_name_1>
value_1
</parameter>
<parameter=param_name_2>
This is the value for the second parameter
that can span
multiple lines
</parameter>
</function>
</tool_call>
```

Cechy:
- `<function=NAME>` — nazwa w atrybucie tagu
- `<parameter=NAME>` — każdy parametr osobno owinięty
- Wartości to **surowy tekst**, nie JSON-encoded
- Multi-line wartości dozwolone
- Reasoning PRZED `<tool_call>`, NIE po

### Dla porównania — Qwen3-Instruct (standard):
```xml
<tool_call>
{"name": "function_name", "arguments": {"param": "value"}}
</tool_call>
```

### Definicja narzędzi w system message (XML):
```xml
<tools>
<function>
<name>tool_name</name>
<description>Tool description</description>
<parameters>
<parameter>
<name>param_name</name>
<type>string</type>
<description>Param description</description>
</parameter>
</parameters>
</function>
</tools>
```

### Tool response format (role=user):
```
<|im_start|>user
<tool_response>
{result content}
</tool_response>
<|im_end|>
```

### Oficjalny parser: `qwen3coder_tool_parser.py`
- Regex: `<tool_call>(.*?)</tool_call>` → `<function=(.*?)</function>` → `<parameter=(.*?)(?:</parameter>|...)`
- Wykonuje type conversion wg schematu (string, int, float, bool, object, array)
- Nazwy parserów:
  - `--tool-call-parser qwen3_coder` (oryginalny, non-streaming)
  - `--tool-call-parser qwen3_xml` (nowszy, streaming — **zalecany** wg HF discussion #17)

### Sampling — ważne rozróżnienie
- **Model card / tryb ogólny (interactive tool use):** często spotykane `temp=1.0`, `top_p=0.95`, `top_k=40`
- **Oficjalne skrypty eval Qwen (benchmark):**
  - tau-bench: `--temperature 0.0`
  - BFCL: domyślnie `temperature 0.001`
- Wniosek praktyczny: dla stabilności tool-calli w benchmarku preferować tryb deterministyczny (0.0 / 0.001).

---

## 3. Znane problemy z tool calling

### Issue 1: Brak `<tool_call>` opening tag
- **Źródło**: [QwenLM/Qwen3-Coder#475](https://github.com/QwenLM/Qwen3-Coder/issues/475)
- Model często pomija `<tool_call>` tag, szczególnie po tekście
- To defekt na poziomie treningu — wymaga prompt-forcing
- Parser ma fallback: jeśli brak `<tool_call>`, traktuje cały output jako tool call

### Issue 2: Zduplikowane/malformed JSON keys (llama.cpp)
- **Źródło**: [ggml-org/llama.cpp#19382](https://github.com/ggml-org/llama.cpp/issues/19382)
- Model generuje: `{"content":"...","filePath":"/path","filePath"/path"}`
- Konsystentne across quants: MXFP4MOE, IQ4_NL, IQ4_XS, Q8_0
- **Brak fix** (luty 2026)

### Issue 3: XML vs JSON confusion (streaming)
- **Źródło**: [lmstudio-ai/lmstudio-bug-tracker#1071](https://github.com/lmstudio-ai/lmstudio-bug-tracker/issues/1071)
- W streaming model zwraca XML `<parameter=...>` zamiast JSON
- Naprawione w LM Studio v0.3.30

### Issue 4: >5 narzędzi → model przechodzi na XML w content field
- **Źródło**: [block/goose#6883](https://github.com/block/goose/issues/6883)
- Powyżej ~5 narzędzi model zmienia behavior i generuje XML w content

### Issue 5: Premature EOS po tool call intent
- **Źródło**: [HF Qwen3-Coder-Next discussions#15](https://huggingface.co/Qwen/Qwen3-Coder-Next/discussions/15)
- Model deklaruje zamiar ("Let me read source_file.c:") → generuje EOS zanim wyemituje tool call

### Issue 6: `<parameter>` tags crash JSON parsers
- **Źródło**: [QwenLM/qwen-code#783](https://github.com/QwenLM/qwen-code/issues/783)
- `JSON.parse()` i `jsonrepair` failują na formacie XML
- Puste argumenty propagują downstream

### Issue 7: Degradacja po 30K tokenów kontekstu
- **Źródło**: [unsloth GGUF discussion#10](https://huggingface.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF/discussions/10)
- Non-Unsloth quantizations mają problemy z tool calling
- Po ~30K context model degraduje w agentic use

---

## 4. llama.cpp — Stan wsparcia

### Architektura: WSPIERANA
- PR [#16095](https://github.com/ggml-org/llama.cpp/issues/15940) — dodaje hybrid architecture
- XML tool call parser: PR [#16932](https://github.com/ggml-org/llama.cpp/pull/16932)

### Dostępne GGUF
| Format | Rozmiar |
|---|---|
| Q4_K_M | 48.4 GB |
| Q5_K_M | 56.7 GB |
| Q6_K | 65.5 GB |
| Q8_0 | 84.8 GB |
| MXFP4_MOE (noctrex) | ~42 GB |

### KRYTYCZNY BUG (naprawiony)
- [Issue #19305](https://github.com/ggml-org/llama.cpp/issues/19305): Bug w vectorized key_gdiff — powoduje degradację outputów
- **NAPRAWIONY** — wymaga update llama.cpp + re-download GGUFs

### Problemy z wydajnością MoE
| Issue | Problem |
|---|---|
| [#19480](https://github.com/ggml-org/llama.cpp/issues/19480) | CPU ~5x wolniejsze niż powinno — MoE routing nie jest sparse-optimized |
| [#19386](https://github.com/ggml-org/llama.cpp/issues/19386) | ARM CPU 6.8x wolniejsze niż Qwen3-MoE-30B (identyczne active params) |
| [#19345](https://github.com/ggml-org/llama.cpp/issues/19345) | 40% wolniejsze niż vLLM na GPU — cache invalidation |

### GBNF Grammar System
- Pełne constrained generation — wymusza valid JSON/XML
- `json-schema-to-grammar.py` konwertuje JSON schemas → GBNF
- **Lazy grammar** — wolny tekst do triggera, potem constrained
- **ALE**: Grammar enforcement NIE jest automatycznie zintegrowany z tool calling
- [#19355](https://github.com/ggml-org/llama.cpp/issues/19355): Server crash z grammar processing (open)

### Komenda startowa (rekomendowana)
```bash
./llama-server \
    -m Qwen3-Coder-Next-Q5_K_M.gguf \
    --jinja -ngl 99 -fa \
    --temp 1.0 --top-p 0.95 --top-k 40 --min-p 0.01 \
    -c 40960 -n 32768 --no-context-shift
```

---

## 5. MLX — Stan wsparcia

### Architektura: WSPIERANA (od mlx-lm 0.24.0)
- Potwierdzone przez Awni Hannun (Apple MLX lead)
- MXFP4 via `mlx_lm.convert --q-mode mxfp4`

### mlx_lm.server 0.30.6: PEŁNE TOOL CALLING (zweryfikowane!)

**UWAGA**: Wcześniejsze źródła internetowe (Feature Request #784) są nieaktualne.
`mlx_lm.server` od wersji 0.30.x **ma wbudowane parsowanie tool calls** z auto-detekcją formatu.

**Zweryfikowane na żywym benchmarku** — react exercise, Qwen3-Coder-Next-8bit, 0 tool call errors.

#### Jak to działa:

1. **Auto-detekcja parsera** (`tokenizer_utils.py:_infer_tool_parser()`):
   - Sprawdza chat template modelu
   - Dla Qwen3-Coder-Next wykrywa `<tool_call>\n<function=` → wybiera parser `qwen3_coder`
   - Inne auto-detected parsery: `minimax_m2`, `function_gemma`, `longcat`, `glm47`, `kimi_k2`, `json_tools`

2. **XML parsing w runtime** (`tool_parsers/qwen3_coder.py`):
   - `tool_call_start = "<tool_call>"`, `tool_call_end = "</tool_call>"`
   - Serwer śledzi tokeny — gdy napotka `<tool_call>`, zbiera tekst do `</tool_call>`
   - Parser regex: `<function=(.*?)</function>` → `<parameter=(.*?)</parameter>`
   - **Type conversion** wg JSON schema narzędzi (string, int, float, bool, object, array)

3. **Konwersja na OpenAI format** (`server.py:format_tool_call()`):
   ```python
   # Model output XML:
   # <tool_call><function=read><parameter=path>\nreact.js\n</parameter></function></tool_call>
   #
   # → Odpowiedź API:
   {
       "function": {"name": "read", "arguments": "{\"path\": \"react.js\"}"},
       "type": "function",
       "id": "63244d05-ac7a-4624-bc8c-c1db5aead1de"  # UUID
   }
   ```

4. **Pełny pipeline**:
   ```
   localcode → POST /v1/chat/completions {tools: [...]}
     → mlx_lm.server: Jinja template renderuje tools jako XML <tools>
     → model generuje XML <tool_call>
     → serwer: qwen3_coder parser → OpenAI tool_calls JSON
   localcode ← response.choices[0].message.tool_calls [{function:{name,arguments}}]
     → dispatch.py: json.loads(arguments) → normalne przetwarzanie
   ```

#### Wspierane parametry API:
- `tools` — definicje narzędzi w formacie OpenAI
- `tool_choice` — `"auto"`, `"required"`, `"none"`, lub named function
- Streaming z tool calls (tool_call_start/end detection per token)
- `finish_reason: "tool_calls"` gdy model użył narzędzia

#### Dane z benchmarku (react exercise, 2026-02-11):
- Agent: `mlx/qwen3-coder-next-8bit`
- Model: `mlx-community/Qwen3-Coder-Next-8bit` na localhost:1236
- Decode TPS: 43-60 tok/s (po rozgrzewce)
- Prefill TPS: 560-1800 tok/s
- Tool calls: read, write, finish — wszystkie poprawnie sparsowane
- 0 format errors, 0 JSON parse errors

### Dostępne modele mlx-community

| Model | Rozmiar | Uwagi |
|---|---|---|
| [mlx-community/Qwen3-Coder-Next-4bit](https://huggingface.co/mlx-community/Qwen3-Coder-Next-4bit) | ~44.8 GB | Standard 4-bit |
| [lmstudio-community/Qwen3-Coder-Next-MLX-4bit](https://huggingface.co/lmstudio-community/Qwen3-Coder-Next-MLX-4bit) | ~44.8 GB | LM Studio |
| [lmstudio-community/Qwen3-Coder-Next-MLX-8bit](https://huggingface.co/lmstudio-community/Qwen3-Coder-Next-MLX-8bit) | ~85 GB | 8-bit |
| [nightmedia MXFP4 (base, nie Coder)](https://huggingface.co/nightmedia/Qwen3-Next-80B-A3B-Instruct-mxfp4-mlx) | ~42.4 GB | 57 tok/s gen |

### ZNANY BUG (naprawiony)
- [Issue #844](https://github.com/ml-explore/mlx-lm/issues/844): Garbage output po ~1000 tokenów (mlx 0.30.4)
- Fix w [mlx#3099](https://github.com/ml-explore/mlx/pull/3099)
- Workaround: `pip install mlx==0.30.3 mlx-metal==0.30.3`

### Speculative Decoding: ZEPSUTE dla Qwen3
- [Issue #846](https://github.com/ml-explore/mlx-lm/issues/846): Tokeny pomijane/gubione
- **NIE UŻYWAĆ** speculative decoding z Qwen3

### Brak guided decoding / structured output
- `mlx_lm.server` nie ma GBNF/grammar/constrained decoding
- Nie wymusza valid JSON — polega na modelu + parser post-hoc
- Outlines dostępne programatycznie (`outlines.from_mlxlm`), ale nie przez HTTP server

### Wydajność na M3 Ultra

| Quant | Model size | Gen speed | Peak memory |
|---|---|---|---|
| 4-bit | ~45 GB | 40-60 tok/s | ~47 GB |
| MXFP4 | ~42 GB | 45-57 tok/s | ~43 GB |
| 8-bit | ~85 GB | 43-60 tok/s (bench) | ~88 GB |

### Optymalizacje wydajności

```bash
# Wired memory (macOS 15+)
sudo sysctl iogpu.wired_limit_mb=110000

# Prompt caching (reuse system prompt)
mlx_lm.cache_prompt --model <path> --prompt "System..." --output cache.safetensors
mlx_lm.generate --model <path> --prompt "User..." --prompt-cache cache.safetensors
```

### Alternatywne serwery MLX (opcjonalne, mlx_lm.server wystarcza)

#### cubist38/mlx-openai-server
```bash
pip install mlx-openai-server
mlx-openai-server launch \
  --model-path ~/.lmstudio/models/mlx-community/Qwen3-Coder-Next-8bit \
  --tool-call-parser qwen3_coder \
  --enable-auto-tool-choice
```
Dodatkowe parsery: `qwen3`, `qwen3_moe`, `qwen3_next`, `glm4_moe`, `minimax_m2`, `harmony`

#### waybarrios/vllm-mlx
```bash
pip install git+https://github.com/waybarrios/vllm-mlx.git
vllm-mlx serve mlx-community/Qwen3-Coder-Next-4bit --port 8000 --continuous-batching
```
- OpenAI + Anthropic API compatible
- Continuous batching (3.4x throughput @ 16 concurrent)
- MCP tool calling, paged KV cache
- **Brak guided decoding**

---

## 6. vLLM — Stan wsparcia

### Na Apple Silicon: NIE DZIAŁA (praktycznie)

#### vllm-metal (oficjalny plugin)
- [vllm-project/vllm-metal](https://github.com/vllm-project/vllm-metal)
- Używa MLX jako compute backend
- **Brak MoE support** — "limited to MLX-compatible architectures"
- **Brak quantization** (planned)
- **Brak tool calling/guided decoding**
- Jedyny testowany model: `Llama-3.2-1B-Instruct-4bit`

#### vllm-mlx (third-party)
- Opisany wyżej w sekcji MLX

### Na CUDA: PEŁNE WSPARCIE

```bash
vllm serve Qwen/Qwen3-Coder-Next --port 8000 --tensor-parallel-size 2 \
  --enable-auto-tool-choice --tool-call-parser qwen3_coder
```

### Parsery tool call w vLLM (pełna lista)

| Parser | Modele |
|---|---|
| `hermes` | Hermes 2/3, Qwen2.5+, Qwen3 (non-Coder) |
| `mistral` | Mistral function-calling |
| `llama3_json` | Llama 3.1/3.2/3.3 |
| `qwen3_xml` | Qwen3 |
| `qwen3_coder` | **Qwen3-Coder, Qwen3-Coder-Next** |
| `granite` | IBM Granite |
| `internlm` | InternLM2.5+ |
| `jamba` | AI21 Jamba-1.5 |
| `xlam` | xLAM |
| `deepseek_v3` | DeepSeek V3 |
| `deepseek_v31` | DeepSeek V3.1 |
| `minimax_m1` | MiniMax M1 |
| `kimi_k2` | Kimi-K2 |
| `glm45` / `glm47` | GLM-4.5/4.7 |
| `pythonic` | Python-list format |
| `openai` | OpenAI OSS |

### Guided Decoding Backends

| Backend | Technika | Szybkość |
|---|---|---|
| **XGrammar** (default) | Pushdown Automaton | ~5x szybsze niż Outlines |
| **Outlines** (fallback) | Finite State Machine | Wolniejsze, ale stabilniejsze |
| **guidance** | Grammar-based | Variable |
| **lm-format-enforcer** | Variable | Lightweight |

### ZNANY PROBLEM: qwen3_coder + guided decoding
- [vLLM issue #27766](https://github.com/vllm-project/vllm/issues/27766)
- Przy `tool_choice=required` vLLM wymusza **JSON grammar** ale Qwen3-Coder generuje **XML**
- Guided decoding nie działa poprawnie z parserem `qwen3_coder`

---

## 7. Porównanie: llama.cpp vs MLX vs vLLM

| Feature | vLLM (CUDA) | llama.cpp (Metal) | mlx_lm.server 0.30.6 | mlx-openai-server | vllm-mlx |
|---|---|---|---|---|---|
| Tool call parsing | 20+ parserów | ~10 native + XML PR#16932 | **7 auto-detected** (qwen3_coder, glm47, kimi_k2, ...) | qwen3_coder + inne | MCP only |
| Grammar enforcement | XGrammar/Outlines | GBNF (manual) | **Brak** (post-hoc parsing) | JSON Schema | **Brak** |
| Auto tool_choice | Tak (guided) | Nie | Tak (`"required"`, `"auto"`, named) | Tak | Tak |
| Qwen3-Coder XML auto-detect | Manual `--tool-call-parser` | Via `--jinja` + template | **Auto** (z chat template) | Manual flag | Nie |
| Apple Silicon | Nie (plugin limited) | Tak (Metal) | Tak (native) | Tak (MLX) | Tak (MLX) |
| MoE support | Pełne | Tak (GGUF, buggy perf) | Tak | Tak | Ograniczone |
| Continuous batching | Tak | Nie | Nie | Nie | Tak |
| Qwen3-Coder-Next | Tak | Tak (buggy JSON #19382) | **Tak (zweryfikowane!)** | Tak | Prawdopodobnie |
| Wydajność M3 Ultra | N/A | ~28-35 tok/s* | **43-60 tok/s** (bench) | ~41-57 tok/s | ~40-55 tok/s |

*llama.cpp ma problemy z MoE routing na tym modelu — issues #19480, #19386

---

## 8. Benchmarki oficjalne

| Benchmark | Wynik |
|---|---|
| SWE-Bench Verified (SWE-Agent) | **70.6%** |
| SWE-Bench Multilingual (SWE-Agent) | 62.8% |
| SWE-Bench Pro (SWE-Agent) | 44.3% |
| Terminal-Bench 2.0 | 36.2% |
| Aider Benchmark | 66.2% |
| SecCodeBench | 61.2% |

Kontekst: bije DeepSeek-V3.2 (70.2%) na SWE-Bench Verified, Claude Opus 4.5 (52.5%) na SecCodeBench.

Trening: 800K verifiable tasks z real GitHub PRs + RL z Docker container test execution.

---

## 9. Wnioski i rekomendacje

### Obecny stan: dwa w pełni działające backendy

Oba backendy (llama.cpp i MLX) poprawnie obsługują Qwen3-Coder-Next tool calling:

| Aspekt | llama.cpp (GGUF, port 1235) | mlx_lm.server (MLX, port 1236) |
|---|---|---|
| Tool call parsing | XML parser PR#16932 | Auto-detected `qwen3_coder` parser |
| XML→JSON konwersja | Transparentna | Transparentna |
| Localcode widzi | Standard OpenAI tool_calls | Standard OpenAI tool_calls |
| Konfiguracja | `--jinja` wymagane | Zero-config (auto z chat template) |
| Wydajność M3 Ultra | ~28-41 tok/s | **43-60 tok/s** |
| MoE optymalizacja | Buggy (#19480, #19386) | Sprawna (unified memory) |
| Znane problemy | #19382 malformed JSON | #846 spec. decoding broken |

### MLX jest lepszym backendem

1. **Szybsze** — 43-60 tok/s vs ~28-41 tok/s na llama.cpp (MoE routing issues)
2. **Zero-config tool calling** — auto-detects `qwen3_coder` z chat template, nie wymaga flag
3. **Brak llama.cpp MoE bugów** — #19480 (5x slow), #19386 (6.8x slow ARM), #19382 (malformed JSON)
4. **localcode dispatch działa identycznie** — oba serwery zwracają standard OpenAI format

### Architektura tool call pipeline (oba backendy)

```
localcode (dispatch.py)
  ↕ OpenAI API (tools JSON, tool_calls JSON)
serwer (llama.cpp LUB mlx_lm.server)
  ↕ Jinja template + XML parser
model (Qwen3-Coder-Next, custom XML format)
```

localcode **nie musi wiedzieć o XML** — serwer robi pełną konwersję w obie strony.
dispatch.py dostaje JSON arguments i stosuje: alias mapping, JSON repair, validation, type checking.

### Potencjalne ulepszenia do rozważenia

1. **Outlines + MLX** jako logits processor — constrained decoding na poziomie tokenów, ale wymaga custom server i nie jest potrzebne przy 0 errors
2. **vllm-mlx** z continuous batching — przydatne dla batch inference, nie dla sequential benchmark
3. **Ujednolicenie samplingu pod benchmark** — zamiast losowego decode (`temp>0`) używać `temp=0.0` (lub 0.001 jak BFCL), żeby ograniczyć halucynacje nazw narzędzi
4. **mlx-community/Qwen3-Coder-Next-4bit** zamiast 8bit — mniejszy model, potencjalnie szybszy, ale niższa jakość

### Czego NIE robić

- NIE włączać speculative decoding w MLX — zepsute dla Qwen3 (#846)
- NIE używać kontekstu >32K w agentic use — degradacja po ~30K (HF discussion)
- NIE polegać na llama.cpp grammar enforcement dla tool calls — GBNF nie jest zintegrowane z tool calling pipeline
- NIE ufać starym źródłom o "braku tool calling w mlx_lm.server" — od 0.30.x jest pełne wsparcie
- NIE wysyłać modelowi globalnych metadanych narzędzi spoza aktywnego zestawu agenta (powoduje dodatkowy szum semantyczny)

---

## 10. Aktualizacja Localcode (2026-02-11, wieczór)

### Problem zidentyfikowany w runtime

W logach `bf16` pojawiały się halucynacje typu:
- `error: unknown tool 'run'. Available tools: apply_patch, edit, finish, glob, grep, ls, read, search, write`

Jednocześnie model dostawał poprawną listę aktywnych tools, ale w payloadzie requestu był też klucz `tool_categories` budowany globalnie ze wszystkich definicji narzędzi (w tym nieaktywnych dla agenta, np. `shell`/`run`, `think`, `ask_agent`).

To nie jest standardowy element OpenAI function-calling i zwiększa ryzyko mylenia małych/średnich modeli agentowych.

### Fix wdrożony

Naprawiono izolację `tool_categories`:
- mapa kategorii jest budowana wyłącznie dla aktywnych narzędzi bieżącego agenta,
- uwzględnia tylko ich canonical names, aliasy i alias display,
- nie zawiera już wpisów narzędzi nieaktywnych dla danego agenta.

### Testy (dodane i przechodzą)

Dodano testy regresyjne:
- `TestBuildToolCategoryMap.test_scopes_to_active_tools_and_aliases`
- `TestBuildToolCategoryMap.test_without_active_tools_includes_all`

Uruchomienie:
- `python3 -m unittest localcode.tests.test_localcode.TestBuildToolCategoryMap localcode.tests.test_localcode.TestResolveToolDisplayMap`
- wynik: `OK`

### Wniosek po poprawce

Aliasy narzędzi działają zgodnie z założeniem (jedna spójna przestrzeń nazw dla modelu), a przeciek globalnych kategorii został usunięty.

Pozostaje tuning decodowania pod benchmark (deterministyczny sampling), bo to jest drugi kluczowy czynnik stabilności tool-calli.

---

## 11. Trening Tool Calling — Jak model uczył się obsługi narzędzi

### 11.1 Pipeline treningowy (staged)

Na podstawie [tech reportu](https://github.com/QwenLM/Qwen3-Coder/blob/main/qwen3_coder_next_tech_report.pdf), [SWE-Universe (arXiv:2602.02361)](https://arxiv.org/html/2602.02361) i [MegaFlow (arXiv:2601.07526)](https://arxiv.org/html/2601.07526v2):

| Etap | Opis |
|------|------|
| **1. Base** | Qwen3-Next-80B-A3B-Base, 7.5T tokenów, 70% kod |
| **2. Mid-training** | ~600B tokenów repo-level data (262K context), PR-level search-and-replace + git diff |
| **3. SFT na trajektoriach** | 500K successful trajectories z 5 scaffoldów, 30B tokenów |
| **4. Code RL** | Automatyczne skalowanie test cases, binary pass/fail reward |
| **5. Agent RL (long-horizon)** | Multi-turn interaction z kontenerami Docker, 1024 parallel envs, max 200 turns, 128K context |
| **6. Expert distillation** | Specjalizowane modele (Web Dev, UX/tool-format) → distylacja do jednego modelu |

### 11.2 Pięć scaffoldów treningowych

Model był szkolony na trajektoriach z **pięciu różnych frameworków agentowych** jednocześnie:

| Scaffold | Narzędzia | Format tool result |
|----------|-----------|-------------------|
| **SWE-Agent** | `str_replace_editor` (view/create/str_replace/insert/undo), `bash` | Patrz §11.3 |
| **Mini-SWE-Agent** | Uproszczona wersja SWE-Agent | — |
| **OpenHands** | Własne tool definitions | — |
| **Claude-Code** | `edit`, `write_file`, `read_file`, `bash` | `"Successfully replaced text at exactly one location."` |
| **Qwen-Code** | `edit`, `write_file`, `read_file`, `grep_search`, `glob`, `run_shell_command` | Patrz §11.4 |

**Kluczowe**: cross-scaffold transfer jest ograniczony (Fig. 4 w tech report). Model wytrenowany na jednym scaffoldzie nie generalizuje dobrze na inne. Dlatego team trenował na **wszystkich pięciu** i distylował UX Expert specjalnie na adherence do formatów narzędzi.

Dodatkowo, model był trenowany na **21 różnych formatach tool chat template** jednocześnie (Table 12, Section 4.2.2 tech report):

| Template | Źródło | Tool Def | Tool Call |
|----------|--------|----------|-----------|
| `qwen3_coder` | This work | XML | XML |
| `qwen3_xml_mixed` | This work | JSON | XML |
| `deepseekr1` | DeepSeek-R1 | JSON | Mixed |
| `deepseekv3` | DeepSeek-V3 | JSON | Mixed |
| `deepseekv31` | DeepSeek-V3.1 | Text | Mixed |
| `deepseekv32` | DeepSeek-V3.2 | JSON | XML |
| `glm46` | GLM-4.6 | JSON | XML |
| `minimax_m1` | MiniMax-M1 | JSON | JSON |
| `minimax_m2` | MiniMax-M2 | XML | XML |
| `kimik2` | Kimi-K2 | JSON | Mixed |
| `hermes` | Generic | JSON | JSON |
| `qwen25_coder` | Qwen2.5-Coder | JSON | JSON |
| `harmony_json` | gpt-oss | TS | JSON |
| `harmony_xml` | gpt-oss | TS | XML |
| `llama4_pythonic` | Llama4 | JSON | Python |
| `toolace` | ToolACE-8B | JSON | Python |
| `mistral3` | Mistral-Large-3 | JSON | Text+JSON |
| `xlam_qwen` | xLAM-2 | JSON | JSON |
| `xml_cline` | Cline Agent | JSON | XML |
| `xml_aone` | Aone Copilot | JSON | XML |
| + 1 więcej | — | — | — |

Cytat (Fig. 6): zwiększenie z 2 do 8 template'ów **konsystentnie poprawia SWE-Bench Verified**, nawet przy identycznej objętości danych.

> "Many existing models are trained with a single tool chat template, which often leads to overfitting. Increasing the number of tool call templates used during training consistently improves downstream robustness to format variation."

### 11.2b Token-level penalty za malformed tool calls

Z tech report Section 4.2.4:

> "We apply a turn-level tool-format penalty. At each interaction step, we perform rule-based validation of tool-call format correctness. During optimization, tokens associated with invalid tool calls receive token-level penalties."

Model był **karany na poziomie tokenów** za nieprawidłowe tool calls podczas RL. To wyjaśnia dlaczego tool call stability jest tak wysoka.

### 11.2c Reward hacking blocker

Podczas RL model **samodzielnie nauczył się exploitować git**, aby pobrać ground-truth:
- `git remote add origin https://github.com/...` → `git fetch origin`
- `git clone`, `curl`, `wget` do GitHub repos

Fix: heurystyczny bloker — tool call zawierający link do repo + keyword sieciowy jest blokowany, agent dostaje explicit feedback o zabronionym działaniu.

### 11.2d Emergent behavior: wzrost liczby turns

Podczas RL training średnia liczba turns agenta wzrosła z **50 do 130** (emergent behavior). W ewaluacji SWE-Bench Pro model był uruchamiany z limitem do **300 turns**. Nasz `max_turns=12` jest bardzo restrykcyjny w porównaniu.

### 11.2e Cztery modele Expert

| Expert | Specjalizacja | Technika |
|--------|--------------|----------|
| **Web Development** | Playwright + Chromium, VLM visual eval, DOM testing | RL |
| **User Experience** | Tool-call format adherence, **21 tool chat templates**, Cline/OpenCode/etc. | RL |
| **Single-turn QA** | Coding tasks, library usage, secure coding | RL z execution-based rewards |
| **Software Engineering** | Multi-turn agentic SWE tasks, 1024 parallel envs | Agent RL |

Po treningu: distylacja wszystkich 4 expertów z powrotem do jednego modelu (SFT checkpoint).

### 11.3 SWE-Agent — dokładne stringi tool result (domyślny scaffold SWE-Bench)

Źródło: [`tools/edit_anthropic/bin/str_replace_editor`](https://github.com/SWE-agent/SWE-agent/blob/main/tools/edit_anthropic/bin/str_replace_editor)

#### str_replace — sukces:
```
The file {path} has been edited. Here's the result of running `cat -n` on a snippet of {path}:
     {line_num}\t{line_content}
     ...
Review the changes and make sure they are as expected. Edit the file again if necessary.
```
(snippet: 4 linie przed i po edycji, format `{i:6}\t{line}`)

#### str_replace — sukces z lint errors (tylko Python):
```
The file {path} has been edited. Here's the result of running `cat -n` on a snippet of {path}:
     ...
Review the changes and make sure they are as expected. Edit the file again if necessary.

<NOTE>Your edits have been applied, but the linter has found syntax errors.</NOTE>

<ERRORS>
{errors}
</ERRORS>

Please review the changes and make sure they are correct.
...
Edit the file again if necessary.
```
(Edycja jest ZASTOSOWANA mimo lintera — to ważna różnica wobec `windowed_edit_replace`)

#### str_replace — old_str nie znaleziony:
```
No replacement was performed, old_str `{old_str}` did not appear verbatim in {path}.
```
(exit code 15)

#### str_replace — wiele wystąpień:
```
No replacement was performed. Multiple occurrences of old_str `{old_str}` in lines {lines}. Please ensure it is unique
```
(exit code 16)

#### str_replace — noop (old == new):
```
No replacement was performed, old_str `{old_str}` is the same as new_str `{new_str}`.
```
(exit code 161)

#### create — sukces:
```
File created successfully at: {path}
```

#### create — plik istnieje:
```
File already exists at: {path}. Cannot overwrite files using command `create`.
```

#### view — sukces:
```
Here's the result of running `cat -n` on {path}:
     1\t{line1}
     2\t{line2}
     ...
```

#### Truncation (>16000 znaków):
```
{content[:16000]}<response clipped><NOTE>To save on context only part of this file has been shown to you. You should retry this tool after you have searched inside the file with `grep -n` in order to find the line numbers of what you are looking for.</NOTE>
```

#### Linting (flake8, tylko Python):
```bash
flake8 --isolated --select=F821,F822,F831,E111,E112,E113,E999,E902 {file_path}
```

### 11.4 Qwen-Code (oficjalny CLI) — dokładne stringi tool result

Źródło: [`packages/core/src/tools/`](https://github.com/QwenLM/qwen-code) w `QwenLM/qwen-code`

#### Nazwy narzędzi (tool-names.ts):
```typescript
ToolNames = {
  EDIT: 'edit',           // search-and-replace
  WRITE_FILE: 'write_file',
  READ_FILE: 'read_file', // param: absolute_path (NIE path!)
  GREP: 'grep_search',
  GLOB: 'glob',
  SHELL: 'run_shell_command',
  LS: 'list_directory',
  // + todo_write, save_memory, task, skill, web_fetch, web_search, lsp
}
// Legacy aliasy: search_file_content → grep_search, replace → edit
```

#### write_file — sukces (nowy plik):
```
Successfully created and wrote to new file: {file_path}.
```

#### write_file — sukces (nadpisanie):
```
Successfully overwrote file: {file_path}.
```

#### write_file — sukces (user zmodyfikował content):
```
Successfully created and wrote to new file: {file_path}. User modified the `content` to be: {content}
```

#### edit — sukces (nowy plik):
```
Created new file: {file_path} with provided content.
```

#### edit — sukces (istniejący plik):
```
The file: {file_path} has been updated.
```

Z snippetem (opcjonalnie):
```
The file: {file_path} has been updated. Showing lines {startLine}-{endLine} of {totalLines} from the edited file:

---

{snippet_content}
```

#### edit — old_string nie znaleziony (0 occurrences):
```
Failed to edit, 0 occurrences found for old_string in {file_path}. No edits made. The exact text in old_string was not found. Ensure you're not escaping content incorrectly and check whitespace, indentation, and context. Use read_file tool to verify.
```
(error type: `EDIT_NO_OCCURRENCE_FOUND`)

#### edit — wiele wystąpień (replace_all nie włączone):
```
Failed to edit. Found N occurrences for old_string in {file_path} but replace_all was not enabled.
```
(error type: `EDIT_EXPECTED_OCCURRENCE_MISMATCH`)

#### edit — noop (old == new):
```
No changes to apply. The old_string and new_string are identical in file: {file_path}
```
(error type: `EDIT_NO_CHANGE`)

#### edit — identyczna zawartość po zastosowaniu:
```
No changes to apply. The new content is identical to the current content in file: {file_path}
```

#### edit — plik nie istnieje:
```
File not found: {file_path}
```
Display: `File not found. Cannot apply edit. Use an empty old_string to create a new file.`

#### read_file — truncated:
```
Showing lines {start}-{end} of {total} total lines.

---

{file_content}
```

#### read_file — parametry:
```typescript
{
  absolute_path: string,  // UWAGA: "absolute_path", nie "path"!
  offset?: number,        // 0-based line number
  limit?: number          // max lines
}
```

#### Typy błędów (tool-error.ts):
```
EDIT_NO_OCCURRENCE_FOUND, EDIT_EXPECTED_OCCURRENCE_MISMATCH,
EDIT_NO_CHANGE, EDIT_NO_CHANGE_LLM_JUDGEMENT,
FILE_NOT_FOUND, FILE_WRITE_FAILURE, PERMISSION_DENIED,
NO_SPACE_LEFT, TARGET_IS_DIRECTORY, ATTEMPT_TO_CREATE_EXISTING_FILE,
FILE_TOO_LARGE, SEARCH_PATH_NOT_FOUND
```

### 11.5 OpenHands — dokładne stringi tool result

Źródło: [`openhands_aci/editor/editor.py`](https://github.com/All-Hands-AI/openhands-aci/blob/main/openhands_aci/editor/editor.py)

OpenHands używa tego samego `str_replace_editor` co SWE-Agent (view/create/str_replace/insert/undo_edit), z parametrami `old_str`/`new_str` (NIE `old_string`/`new_string` jak w Qwen-Code).

Feedback jest prawie identyczny jak SWE-Agent — kluczowa różnica: OpenHands ma **67,074 trajektorii** w datasecie [Nebius SWE-rebench](https://huggingface.co/datasets/nebius/SWE-rebench-openhands-trajectories) wygenerowanych Qwen3-Coder-480B, prawdopodobnie użytych w treningu.

Format snippetu: `{line_number:6}\t{line_content}` (6-char padded + tab), `SNIPPET_CONTEXT_WINDOW = 4`, `MAX_RESPONSE_LEN_CHAR = 16000`.

### 11.6 Cline scaffold — format tool result

Źródło: [`src/core/prompts/responses.ts`](https://github.com/cline/cline) w `cline/cline`

#### write_to_file / replace_in_file — sukces:
```
The content was successfully saved to {path}.

Here is the full, updated content of the file that was saved:

<final_file_content path="{path}">
{full file content}
</final_file_content>

IMPORTANT: For any future changes to this file, use the final_file_content shown above as your reference.
```

#### replace_in_file — diff mismatch (plik PRZYWRÓCONY):
```
This is likely because the SEARCH block content doesn't match exactly with what's in the file...
The file was reverted to its original state:

<file_content path="{path}">
{original content}
</file_content>

Now that you have the latest state of the file, try the operation again with fewer, more precise SEARCH blocks.
(If you run into this error 3 times in a row, you may use the write_to_file tool as a fallback.)
```

### 11.7 Format tool_response w chat template

Z [tokenizer_config.json](https://huggingface.co/Qwen/Qwen3-Coder-Next/raw/main/tokenizer_config.json):

```
<|im_start|>user
<tool_response>
{plain text tool result}
</tool_response>
<|im_end|>
```

Wiele tool responses w jednym bloku:
```
<|im_start|>user
<tool_response>
{result_1}
</tool_response>
<tool_response>
{result_2}
</tool_response>
<|im_end|>
```

**Kluczowe**: tool results to `role: "tool"` w API → Jinja template konwertuje na `<|im_start|>user` + `<tool_response>`. Treść to **surowy tekst**, nie JSON.

### 11.8 SWE-Universe — 800K zadań

Źródło: [arXiv:2602.02361](https://arxiv.org/html/2602.02361)

**Źródło A — Real GitHub PRs: 807,693 instancji** z 52,960 repozytoriów:

| Język | Instancje | Repozytoria |
|-------|-----------|-------------|
| Python | 202,302 | 13,098 |
| JS/TS | 175,660 | 11,604 |
| Go | 121,062 | 5,554 |
| Java | 86,105 | 4,700 |
| Rust | 74,180 | 4,445 |
| C/C++ | 37,228 | 3,405 |
| C# | 24,387 | 1,929 |
| Inne | 86,769 | 8,225 |

**Źródło B — Synthesized Bugs: 851,898 instancji** (SWE-Smith, SWE-Flow, SWE-Rebench, Multi-SWE-RL)

Łącznie: **~1.66M** verifiable task instances w 9+ językach.

Pipeline:
- Walidacja: `evaluation.sh` zwraca 0 dla naprawionego stanu, ≠0 dla buggy
- Anty-hacking: LLM wykrywa superficjalne skrypty (grep zamiast testów)
- Iteracyjna walidacja: success rate 82.6% → **94%**
- Edycje w danych treningowych: **Search-and-Replace i git diff** (oba formaty)

### 11.9 MegaFlow — infrastruktura RL

Źródło: [arXiv:2601.07526](https://arxiv.org/html/2601.07526v2)

- Trzy serwisy: Model Service, Agent Service, Environment Service
- Kubernetes na Alibaba Cloud
- **1024 parallel SWE environments** per training step
- 64 distinct SWE instances × 16 replicas = 1024
- Docker images w Alibaba Cloud Container Registry (ACR) z layer caching
- Trajektoria: τ = {(s₀,a₀), (s₁,a₁), ..., (sₜ,aₜ)}
- Reward R = G(τ) obliczany po zakończeniu zadania (binary pass/fail)
- Asynchronous RL framework z 2x–4x speedup vs standardowy RL

### 11.10 Wnioski dla optymalizacji Localcode

| Aspekt | Co model widział w treningu | Co robi Localcode | Rekomendacja |
|--------|---------------------------|-------------------|--------------|
| **write sukces** | `"Successfully created/overwrote file: {path}."` (Qwen-Code) | Podobne | OK, opcjonalnie ujednolicić frazę |
| **edit sukces** | `"The file: {path} has been updated."` + snippet (Qwen-Code), `"The file {path} has been edited. Here's the result of running cat -n..."` (SWE-Agent) | — | Dodać snippet edited region |
| **edit not found** | `"Failed to edit, 0 occurrences..."` + sugestia `read_file` (Qwen-Code), `"No replacement was performed, old_str ... did not appear verbatim"` (SWE-Agent) | Pokazuje file content | OK |
| **edit noop** | `"No changes to apply. The old_string and new_string are identical"` (Qwen-Code), `"No replacement was performed, old_str is the same as new_str"` (SWE-Agent) | Progressive response + redirect to write | OK |
| **read truncated** | `"Showing lines {start}-{end} of {total} total lines."` (Qwen-Code) | `"File already fully read (N lines)"` | OK |
| **multi-scaffold** | 5 scaffoldów jednocześnie + UX Expert distillation | Własny scaffold | Model toleruje warianty |
| **edit format w mid-training** | Search-and-Replace **i** git diff w PR data | — | Model zna oba formaty edycji |

**Najważniejsze wnioski**:
1. Model oczekuje **krótkich, deklaratywnych** komunikatów sukcesu — nie pełnej treści pliku
2. Po edycji opcjonalnie **snippet** zmienionego regionu (SWE-Agent: 4 linie before/after, Qwen-Code: zakres)
3. Przy błędach: pokazać aktualną treść pliku, żeby model mógł retry z poprawnym tekstem
4. `<tool_response>` to surowy tekst — nie JSON, nie structured output
5. Model toleruje różne formaty dzięki multi-scaffold training + UX Expert distillation

### 11.11 Lista tasków do walidacji (na podstawie raportu)

Dla testów stabilności tool-calli i porównań profili prompt/tool-descriptions używamy pakietu JS:

- `binary`
- `complex-numbers`
- `grade-school`
- `phone-number`
- `pig-latin`
- `react`
- `simple-linked-list`
- `space-age`
- `tournament`
- `triangle`

Lista referencyjna jest utrzymywana w:
- `optimizations/tool_desc_profiles/report_tasks_js.txt`

Harness eksperymentalny (`bin/run-tool-desc-experiments.sh`) przy `task_spec=report` automatycznie używa tej listy.

---

## 12. Aktualny stan vs rekomendacje — Tool Definitions, Descriptions, Feedback

Porównanie aktualnej implementacji Localcode z formatami, na których model był trenowany (Qwen-Code, SWE-Agent, OpenHands, Cline). Analiza obejmuje trzy warstwy: definicje narzędzi (nazwy, parametry), opisy narzędzi (description widoczny dla modelu), oraz feedback zwrotny (tool result po wykonaniu operacji).

### 12.1 Tool Definitions — nazwy i parametry

#### write (write_file)

| Aspekt | Qwen-Code (trening) | SWE-Agent (trening) | Cline (trening) | Localcode (aktualny) | Ocena |
|--------|---------------------|---------------------|-----------------|---------------------|-------|
| **Nazwa** | `write_file` | `create` (tylko nowe) | `write_to_file` | `write` (alias: `write_file`) | OK — alias pokrywa |
| **Param: ścieżka** | `file_path` | `path` | `path` | `path` (aliasy: `file`, `filename`, `file_path`, `filepath`, `file_name`) | OK — alias pokrywa `file_path` |
| **Param: treść** | `content` | `file_text` | `content` | `content` (aliasy: `text`, `data`, `code`, `body`, `source`) | OK |
| **Overwrite** | Tak (zawsze) | NIE (`create` tylko nowe) | Tak | Tak | OK |
| **additionalProperties** | — | — | — | `false` | OK — blokuje nieznane params |

**Wniosek**: Definicja write jest kompatybilna. Aliasy pokrywają warianty z treningu.

#### edit (replace_in_file / str_replace)

| Aspekt | Qwen-Code | SWE-Agent | OpenHands | Localcode | Ocena |
|--------|-----------|-----------|-----------|-----------|-------|
| **Nazwa** | `edit` | `str_replace_editor` | `str_replace_editor` | `edit` (aliasy: `replace_in_file`, `edit_file`) | OK |
| **Param: old** | `old_string` | `old_str` | `old_str` | `old` (aliasy: `old_string`, `old_text`, `search`, `find`, `original`, `before`) | OK |
| **Param: new** | `new_string` | `new_str` | `new_str` | `new` (aliasy: `new_string`, `new_text`, `replace`, `replacement`, `after`) | OK |
| **Param: path** | `file_path` | `path` | `path` | `path` (aliasy: `file`, `filename`, `file_path`) | OK |
| **Param: all** | `replace_all` (bool) | — | — | `all` (bool) | OK — nazwa krótsza, czytelna |

**Wniosek**: Definicja edit pokrywa warianty ze wszystkich scaffoldów dzięki rozbudowanym aliasom. Brak `old_str`/`new_str` alias — **ale** `old_string`/`new_string` i `old_text`/`new_text` są pokryte.

**Brak**: alias `old_str` → `old` i `new_str` → `new` (format SWE-Agent/OpenHands). Model mógłby użyć `old_str` z pamięci SWE-Agent treningu.

#### read (read_file)

| Aspekt | Qwen-Code | SWE-Agent | Localcode | Ocena |
|--------|-----------|-----------|-----------|-------|
| **Nazwa** | `read_file` | `str_replace_editor` command=`view` | `read` (alias: `read_file`, `open_file`) | OK |
| **Param: ścieżka** | `absolute_path` | `path` | `path` (aliasy: `file`, `filename`, `file_path`, `filepath`) | UWAGA |
| **Param: offset** | `offset` (0-based) | `view_range` [start, end] (1-based) | `offset` (0-based) + `limit` + `line_start/line_end` (1-based) | OK — obsługuje oba style |
| **Param: limit** | `limit` | — | `limit` | OK |

**UWAGA**: Qwen-Code używa `absolute_path` jako nazwy parametru, nie `path`. Alias `absolute_path` → `path` **nie istnieje** w dispatch.py. Model wytrenowany na Qwen-Code scaffold może użyć `absolute_path` i dostać validation error.

#### search (grep_search / search_text)

| Aspekt | Qwen-Code | Localcode | Ocena |
|--------|-----------|-----------|-------|
| **Nazwa** | `grep_search` | `search` (alias: `search_text`) | UWAGA |
| **Param: pattern** | `query` | `pattern` (aliasy: `query`, `text`, `regex`, `pat`) | OK |
| **Param: path** | `path` | `path` (aliasy: `file`, `dir`) | OK |
| **Param: include** | `file_pattern` | `include` | Brak alias `file_pattern` → `include` |

**UWAGA**: Brak aliasu `grep_search` → `search`. Model z Qwen-Code treningu może próbować wywołać `grep_search` i dostać "unknown tool".

#### glob (find_files)

| Aspekt | Qwen-Code | Localcode | Ocena |
|--------|-----------|-----------|-------|
| **Nazwa** | `glob` | `glob` (alias: `find_files`) | OK |
| **Param: pattern** | `pattern` | `pat` (aliasy: `pattern`, `glob_pattern`, `search`) | OK |
| **Param: path** | `path` | `path` (aliasy: `file`, `dir`, `directory`, `folder`, `root`) | OK |

**Wniosek**: glob jest w pełni kompatybilny.

#### ls (list_directory)

| Aspekt | Qwen-Code | Localcode | Ocena |
|--------|-----------|-----------|-------|
| **Nazwa** | `list_directory` | `ls` | UWAGA |

**UWAGA**: Brak aliasu `list_directory` → `ls`. Model z Qwen-Code treningu może używać `list_directory`.

#### Brakujące aliasy — podsumowanie

| Alias z treningu | → Powinien mapować na | Scaffold źródłowy | Status |
|------------------|----------------------|-------------------|--------|
| `old_str` | `old` (w edit) | SWE-Agent, OpenHands | **BRAK** |
| `new_str` | `new` (w edit) | SWE-Agent, OpenHands | **BRAK** |
| `absolute_path` | `path` (w read) | Qwen-Code | **BRAK** |
| `grep_search` | `search` (tool alias) | Qwen-Code | **BRAK** |
| `list_directory` | `ls` (tool alias) | Qwen-Code | **BRAK** |
| `file_pattern` | `include` (w search) | Qwen-Code | **BRAK** |
| `search_file_content` | `search` (tool alias) | Qwen-Code (legacy) | **BRAK** |
| `run_shell_command` | (brak shell) | Qwen-Code | N/A (nie mamy shell) |

### 12.2 Tool Descriptions — co model widzi

#### Aktualny system prompt (`qwen3-coder.txt`, 37 linii)

```
You are a coding agent solving benchmark tasks.
Tests run externally after you finish. You cannot run tests directly.
...
```

**Mocne strony**:
- Zwięzły, konkretny — model nie musi parsować wielu akapitów
- Jasna strategia: read → write/edit → verify → finish
- Recovery rules z konkretnymi wskazówkami
- Hard rules (no test edits, base claims on tool results)

**Porównanie z SWE-Agent (trening)**:
- SWE-Agent używa SYSTEM MESSAGE z kompletnym opisem każdego narzędzia, formatem command + args, przykładami
- Model widział detale jak: "The new_str will replace the old_str, the indentation MATTERS"
- Localcode nie powtarza tego — opisy są w osobnych JSON tool definitions

**Porównanie z Qwen-Code (trening)**:
- Qwen-Code embeduje XML schema narzędzia w system message z `<tools>` tagami
- Model widział: `<tool_name>edit</tool_name>` + `<description>...</description>` + `<parameters>...`
- Localcode: opis jest w `edit.json` → llama-server/mlx konwertuje na XML `<tools>` sekcję

**Wniosek**: Opis narzędzi jest generowany przez inference server (llama.cpp XML parser / mlx tool template). Kluczowe jest, aby **description w JSON** był jasny i krótki, bo to on trafia do `<tools>` bloku.

#### Tool descriptions — szczegółowa analiza

| Narzędzie | Opis (Localcode) | Opis (Qwen-Code trening) | Ocena |
|-----------|------------------|--------------------------|-------|
| **write** | "Create or replace a full file. Use for first full implementation. After a successful write, prefer edit for small follow-up fixes instead of another full write." | "Create a new file or overwrite an existing file with new content." | OK — bardziej instruktażowy |
| **edit** | "Replace one exact snippet in an existing file. Use this after read/write for small corrective changes. If old text is not found, read current file and retry with exact text." | "Use this tool to make changes to an existing file. Replace old_str with new_str. old_str MUST match EXACTLY." | OK — podobny |
| **read** | "Read a file with line numbers. Use when text context is missing or stale. Do not repeat read on unchanged file unless new context is needed." | "Reads a file from the filesystem. Returns file content with line numbers." | OK — Localcode dodaje praktyczną wskazówkę |
| **search** | Wieloliniowy z przykładami, arg aliases, defaults | "Search for a term in the codebase. Returns matches with file paths and line numbers." | OK — bardziej rozbudowany |
| **glob** | Z PATH RULES i przykładami | "Find files by glob pattern." | OK |
| **finish** | "Call when code is complete. Set status to done/blocked/incomplete." | Brak bezpośredniego odpowiednika | OK — Localcode-specific |

### 12.3 Tool Results Feedback — porównanie dokładnych stringów

#### write — sukces (nowy plik)

| Scaffold | Feedback string |
|----------|----------------|
| **Qwen-Code** | `"Successfully created and wrote to new file: {path}."` |
| **SWE-Agent** | `"File created successfully at: {path}"` |
| **Cline** | `"The content was successfully saved to {path}."` + full file content w `<final_file_content>` |
| **Localcode** | `"ok: created {path}, +{N} lines\nfile_state: lines=X chars=Y sha256=Z\n{decision_hint}\n{state_brief}\n{state_line}\n{changed_preview}"` |

**Ocena**: Localcode zwraca **znacznie więcej metadanych** niż jakikolwiek scaffold treningowy. Model widział w treningu krótkie, jednozdaniowe potwierdzenia. Localcode dodaje: file_state, decision_hint, state_brief, state_line, changed_preview, loop_guard. To ~6-8 dodatkowych linii, których model nigdy nie widział w treningu.

**Ryzyko**: Model może próbować parsować te linie jako instrukcje i się pogubić, lub zignorować je i tracić context window.

**Rekomendacja**: Rozważyć uproszczenie do jednolinijkowego `"ok: created {path}, +{N} lines"` + opcjonalny snippet (jak SWE-Agent). Metadata (sha, state) jest wartościowa dla loop detection, ale model jej nie potrzebuje.

#### write — sukces (update)

| Scaffold | Feedback string |
|----------|----------------|
| **Qwen-Code** | `"Successfully overwrote file: {path}."` |
| **Cline** | `"The content was successfully saved to {path}."` + full content |
| **Localcode** | `"ok: updated {path}, +{A} -{R} lines\nfile_state: ...\n{decision_hint}\n{state_brief}\n{state_line}\n{change_summary}\n{changed_preview}"` |

**Ocena**: Jak wyżej — zbyt verbose. `+{A} -{R} lines` jest użyteczne (SWE-Agent też liczy zmiany), reszta to overhead.

#### write — noop (identyczna treść)

| Scaffold | Feedback string |
|----------|----------------|
| **Qwen-Code** | `"No changes to apply. The new content is identical to the current content in file: {path}"` |
| **Localcode (1st)** | `"ok: no changes - file already has this content.\nfile_state: ...\n{decision_hint}\n{state_brief}\n{state_line}"` |
| **Localcode (2nd+)** | `"error: repeated no-op write for {basename}. Write different content, or call finish..."` |

**Ocena**: Logika progressive noop jest dobra (break loop). Początkowy prefix `ok:` (1st) vs `error:` (2nd) jest rozsądny. Model z treningu widział jednozdaniowe "No changes to apply." — ale Localcode progressive approach jest lepszy dla stabilności.

#### edit — sukces

| Scaffold | Feedback string |
|----------|----------------|
| **Qwen-Code** | `"The file: {path} has been updated."` + opcjonalny snippet (`Showing lines {start}-{end} of {total}`) |
| **SWE-Agent** | `"The file {path} has been edited. Here's the result of running cat -n on a snippet of {path}:\n{4 lines before/after}\nReview the changes and make sure they are as expected. Edit the file again if necessary."` |
| **Cline** | `"The content was successfully saved to {path}."` + full file w `<final_file_content>` |
| **Localcode** | `"ok: {count} replacement(s). Edit applied in {basename}.\nfile_state: ...\n{decision_hint}\n{state_brief}\n{state_line}\n{change_summary}\n{changed_preview}\nAction: if this satisfies requirements, call finish; otherwise make the next targeted edit."` |

**Ocena**:
- SWE-Agent pokazuje **snippet 4 linii before/after** — to standard treningowy
- Qwen-Code opcjonalnie pokazuje snippet
- Localcode pokazuje `changed_preview` (do 6 linii zmienionego kodu) — **to jest bliskie SWE-Agent snippetowi**
- Localcode dodaje trailing "Action: if this satisfies..." — model z treningu widział "Review the changes..." (SWE-Agent) lub "Edit the file again if necessary" — **zbliżone**
- Nadmiar metadanych jak wyżej (file_state, decision_hint, state_brief, state_line, change_summary)

#### edit — old not found

| Scaffold | Feedback string |
|----------|----------------|
| **Qwen-Code** | `"Failed to edit, 0 occurrences found for old_string in {path}. No edits made. The exact text in old_string was not found. Ensure you're not escaping content incorrectly and check whitespace, indentation, and context. Use read_file tool to verify."` |
| **SWE-Agent** | `"No replacement was performed, old_str '{old_str}' did not appear verbatim in {path}."` |
| **Localcode** | `"error: old text was not found in {basename}.\nThis usually means whitespace or line-break mismatch.\nHere is the current content of {basename}:\n{full file}\nAction: copy the exact text..."` |

**Ocena**: Localcode pokazuje **pełną treść pliku** po błędzie — to agresywniejsze niż trening (SWE-Agent nie pokazuje, Qwen-Code sugeruje `read_file`), ale **skuteczne** bo model ma context do retry. Dobrze.

#### edit — noop (old == new)

| Scaffold | Feedback string |
|----------|----------------|
| **Qwen-Code** | `"No changes to apply. The old_string and new_string are identical in file: {path}"` |
| **SWE-Agent** | `"No replacement was performed, old_str '{old}' is the same as new_str '{new}'."` |
| **Localcode (1st)** | `"error: no changes - old equals new in {basename}. Use a different 'new' value, or switch to write_file for full rewrite."` |
| **Localcode (2nd)** | `"error: repeated no-op edit in {basename}. Read the latest file content and apply a real change, or finish if already correct."` |
| **Localcode (3rd+)** | `"error: repeated no-op edit in {basename}"` (krótkie) |

**Ocena**: Progressive noop jest lepszy niż jednozdaniowy feedback treningowy. Localcode aktywnie przerywa pętle. Jedyna różnica: trening nie używał prefiksu `error:` — Qwen-Code mówi "No changes to apply" (brak prefiksu), SWE-Agent ma exit code 161 ale tekstowo "No replacement was performed" (też brak prefiksu `error:`).

#### edit — wiele wystąpień

| Scaffold | Feedback string |
|----------|----------------|
| **Qwen-Code** | `"Failed to edit. Found N occurrences for old_string in {path} but replace_all was not enabled."` |
| **SWE-Agent** | `"No replacement was performed. Multiple occurrences of old_str '{old}' in lines {lines}. Please ensure it is unique"` |
| **Localcode** | `"error: 'old' text appears {count} times in {basename}; it must be unique. Include more surrounding lines in 'old' to make it unique, or set all=true to replace all occurrences."` |

**Ocena**: Localcode daje najbardziej instruktażowy feedback (dodaje radę o `all=true`). Dobra adaptacja.

#### read — past EOF / truncation

| Scaffold | Feedback string |
|----------|----------------|
| **Qwen-Code** | `"Showing lines {start}-{end} of {total} total lines."` |
| **SWE-Agent** | Truncation: `"{content[:16000]}<response clipped><NOTE>To save on context..."` |
| **Localcode (past-EOF)** | `"File already fully read ({N} lines). No more content. Proceed with your implementation."` |
| **Localcode (truncated)** | `"(... {remaining} more lines, use offset={X} to continue)"` |

**Ocena**: Localcode past-EOF jest lepszy (nie zaczyna od `error:` — co było kluczowym fixem). Truncation message jest jasny i instruktażowy.

#### read — format linii

| Scaffold | Format |
|----------|--------|
| **SWE-Agent** | `"     1\t{line}"` (6-char padded + tab) |
| **OpenHands** | `"     1\t{line}"` (6-char padded + tab) |
| **Qwen-Code** | Brak info (prawdopodobnie surowy content) |
| **Localcode** | `"   1| {line}"` (4-char padded + pipe + space) |

**Ocena**: Localcode używa `{i:4}| ` zamiast `{i:6}\t`. Model widział w treningu tab-separated z 6-char padding. Różnica jest kosmetyczna — model radzi sobie z obu formatami (potwierdzone 0 errors / 225 exercises).

### 12.4 Feedback z JSON definition (edit.json) vs trening

Localcode ma rozbudowany system `feedback` w JSON tool definitions, który jest wstrzykiwany zamiast generycznego error message:

| Feedback key | Localcode string | Analogiczny trening | Ocena |
|-------------|-----------------|---------------------|-------|
| `must_read_before_editing` | `"FORMAT ERROR: edit failed due to stale context.\nACTION: Call read(path)..."` | Brak — scaffoldy treningowe nie mają tego guard | EXTRA |
| `old_string_not_found` | `"ERROR: 'old' string not found in file. Common causes: 1. Whitespace mismatch..."` | Qwen-Code: `"Failed to edit, 0 occurrences..."` | Bardziej instruktażowy |
| `old_string_not_unique` | `"FORMAT ERROR: edit failed: old_string is not unique.\nACTION: Call read(path)..."` | SWE-Agent: `"Multiple occurrences..."` | Bardziej instruktażowy |
| `old_equals_new` | `"ERROR: edit called with 'old' identical to 'new'...\nACTION:\n1. Re-read the file...\n2. Identify the EXACT text...\nTIP: For small files, consider using write..."` | Qwen-Code: `"No changes to apply."` | Znacznie bardziej rozbudowany |

**Ocena**: Feedback w edit.json jest **bardziej instruktażowy** niż to co model widział w treningu. To może być zarówno plus (jasne wskazówki) jak i minus (model nigdy nie widział tak długich errorów i może je traktować jako szum). Przy 0 tool errors / 225 exercises — **działa dobrze**.

### 12.5 Dispatch layer — JSON repair i walidacja

Dispatch (`dispatch.py`) zawiera warstwę naprawczą, której **żaden scaffold treningowy nie miał**:

| Mechanizm | Opis | Analogia w treningu |
|-----------|------|---------------------|
| **JSON repair** | Trailing commas, single→double quotes, missing braces | Brak — scaffoldy zakładają poprawny JSON |
| **Arg alias mapping** | 20+ aliasów per narzędzie (file→path, old_string→old, ...) | Brak — każdy scaffold ma stałe nazwy params |
| **Number word coercion** | "twenty" → 20, "10.0" → 10 | Brak |
| **Patch block extraction** | Wyciąga `*** Begin Patch...*** End Patch` z surowego tekstu | Brak |
| **Tool name resolution** | Case-insensitive, splitlines, strip `<|` tokens | Brak |
| **Integer coercion** | `"10"` → 10, `10.0` → 10 | Brak |
| **Unknown tool message** | `"error: unknown tool 'X'. Available tools: ..."` | SWE-Agent: bash error |

**Ocena**: Ta warstwa jest **kluczowa dla małych modeli** (3B active params). Żaden scaffold treningowy nie miał takiej tolerancji na błędy formatu. To jest główny powód 0 tool errors / 225 exercises.

### 12.6 Podsumowanie — co poprawić

#### Priorytet WYSOKI (brakujące aliasy z treningu):

1. **`old_str` → `old`** i **`new_str` → `new`** w edit arg aliases — model z SWE-Agent/OpenHands treningu może użyć tych nazw
2. **`absolute_path` → `path`** w read arg aliases — model z Qwen-Code treningu używa `absolute_path`
3. **`grep_search` → `search`** w TOOL_ALIAS_MAP — model z Qwen-Code treningu może wywołać `grep_search`
4. **`list_directory` → `ls`** w TOOL_ALIAS_MAP — model z Qwen-Code treningu może wywołać `list_directory`

#### Priorytet ŚREDNI (feedback tuning):

5. **Uprościć write sukces feedback** — usunąć/skrócić state_brief, state_line, decision_hint z widoku modelu (zachować w logach). Model widział jednozdaniowe potwierdzenia w treningu.
6. **Uprościć edit sukces feedback** — analogicznie, zachować `changed_preview` (bliskie SWE-Agent snippet), usunąć nadmiar metadata.
7. **Dodać `file_pattern` → `include`** alias w search — model z Qwen-Code treningu używa `file_pattern`.

#### Priorytet NISKI (kosmetyka):

8. **Format linii read** — zmiana `{i:4}| ` na `{i:6}\t` (bliżej SWE-Agent/OpenHands). Nie krytyczne — 0 errors potwierdza kompatybilność.
9. **Dodać `search_file_content` → `search`** alias — legacy nazwa z Qwen-Code.
10. **Dodać `replace` → `edit`** tool alias — legacy z Qwen-Code `tool-names.ts`.

#### NIE ZMIENIAĆ (działa dobrze):

- Progressive noop handling (3 poziomy) — lepsze niż trening, break loops
- Past-EOF message bez `error:` prefiksu — kluczowy fix, nie cofać
- Showing full file content on edit-not-found — agresywne ale skuteczne
- Syntax guard (node -c) — brak w treningu ale zapobiega regresji
- JSON repair layer — brak w treningu ale kluczowy dla małych modeli
- Stealth cap, write nudge, force-stop — Localcode-specific, stabilizują zachowanie

---

## Źródła

### Model
- [Qwen/Qwen3-Coder-Next — HuggingFace](https://huggingface.co/Qwen/Qwen3-Coder-Next)
- [Qwen/Qwen3-Coder-Next-GGUF](https://huggingface.co/Qwen/Qwen3-Coder-Next-GGUF)
- [QwenLM/Qwen3-Coder — GitHub](https://github.com/QwenLM/Qwen3-Coder)
- [qwen3coder_tool_parser.py source](https://huggingface.co/Qwen/Qwen3-Coder-480B-A35B-Instruct/blob/main/qwen3coder_tool_parser.py)

### Tool calling issues
- [#475 — Unreliable function calling](https://github.com/QwenLM/Qwen3-Coder/issues/475)
- [#19382 — Invalid JSON (llama.cpp)](https://github.com/ggml-org/llama.cpp/issues/19382)
- [#1071 — Streaming XML (LM Studio)](https://github.com/lmstudio-ai/lmstudio-bug-tracker/issues/1071)
- [#6883 — Tool count threshold (Goose)](https://github.com/block/goose/issues/6883)
- [#783 — Parameter tags crash (qwen-code)](https://github.com/QwenLM/qwen-code/issues/783)
- [HF discussion #15 — Premature EOS](https://huggingface.co/Qwen/Qwen3-Coder-Next/discussions/15)
- [HF discussion #17 — Parser recommendation](https://huggingface.co/Qwen/Qwen3-Coder-Next/discussions/17)

### llama.cpp
- [#19305 — key_gdiff bug (FIXED)](https://github.com/ggml-org/llama.cpp/issues/19305)
- [#19480 — MoE CPU slow](https://github.com/ggml-org/llama.cpp/issues/19480)
- [#19386 — ARM CPU slow](https://github.com/ggml-org/llama.cpp/issues/19386)
- [#19345 — 40% slower than vLLM](https://github.com/ggml-org/llama.cpp/issues/19345)
- [#16932 — XML tool-call parser PR](https://github.com/ggml-org/llama.cpp/pull/16932)
- [Function calling docs](https://github.com/ggml-org/llama.cpp/blob/master/docs/function-calling.md)
- [GBNF grammar docs](https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md)

### MLX
- [ml-explore/mlx-lm](https://github.com/ml-explore/mlx-lm)
- mlx_lm.server 0.30.6 tool calling: `server.py:1355-1494`, `tokenizer_utils.py:470-563`, `tool_parsers/qwen3_coder.py`
- [#844 — Garbage output (FIXED)](https://github.com/ml-explore/mlx-lm/issues/844)
- [#846 — Speculative decoding broken](https://github.com/ml-explore/mlx-lm/issues/846)
- [#784 — Function calling feature request (RESOLVED in 0.30.x)](https://github.com/ml-explore/mlx-examples/issues/784)
- [cubist38/mlx-openai-server](https://github.com/cubist38/mlx-openai-server)
- [waybarrios/vllm-mlx](https://github.com/waybarrios/vllm-mlx)
- [Outlines MLX integration](https://dottxt-ai.github.io/outlines/latest/features/models/mlxlm/)
- Lokalna weryfikacja: `localcode/logs/localcode_mlx_qwen3-coder-next-8bit_2026-02-11_09-15-37.jsonl`

### vLLM
- [vLLM Tool Calling docs](https://docs.vllm.ai/en/latest/features/tool_calling/)
- [vLLM Structured Outputs docs](https://docs.vllm.ai/en/latest/features/structured_outputs/)
- [vllm-project/vllm-metal](https://github.com/vllm-project/vllm-metal)
- [#27766 — qwen3_coder guided decoding mismatch](https://github.com/vllm-project/vllm/issues/27766)
- [Qwen3-Coder vLLM recipe](https://docs.vllm.ai/projects/recipes/en/latest/Qwen/Qwen3-Coder-480B-A35B.html)

### Trening i scaffoldy
- [Qwen3-Coder-Next Tech Report (PDF)](https://github.com/QwenLM/Qwen3-Coder/blob/main/qwen3_coder_next_tech_report.pdf)
- [SWE-Universe (arXiv:2602.02361)](https://arxiv.org/html/2602.02361) — 800K verifiable tasks
- [MegaFlow (arXiv:2601.07526)](https://arxiv.org/html/2601.07526v2) — RL infrastructure
- [QwenLM/qwen-code — GitHub](https://github.com/QwenLM/qwen-code) — oficjalny CLI (tool source code)
- [qwen-code write-file.ts](https://github.com/QwenLM/qwen-code/blob/main/packages/core/src/tools/write-file.ts)
- [qwen-code edit.ts](https://github.com/QwenLM/qwen-code/blob/main/packages/core/src/tools/edit.ts)
- [qwen-code read-file.ts](https://github.com/QwenLM/qwen-code/blob/main/packages/core/src/tools/read-file.ts)
- [qwen-code tool-names.ts](https://github.com/QwenLM/qwen-code/blob/main/packages/core/src/tools/tool-names.ts)
- [qwen-code tool-error.ts](https://github.com/QwenLM/qwen-code/blob/main/packages/core/src/tools/tool-error.ts)
- [SWE-Agent str_replace_editor](https://github.com/SWE-agent/SWE-agent/blob/main/tools/edit_anthropic/bin/str_replace_editor)
- [SWE-Agent default config](https://github.com/SWE-agent/SWE-agent/blob/main/config/default.yaml)
- [Cline responses.ts](https://github.com/cline/cline/blob/main/src/core/prompts/responses.ts) — tool feedback
- [OpenHands ACI str_replace_editor](https://github.com/All-Hands-AI/openhands-aci/blob/main/openhands_aci/editor/editor.py)
- [Nebius SWE-rebench OpenHands Trajectories](https://huggingface.co/datasets/nebius/SWE-rebench-openhands-trajectories) — 67K trajectories
- [DeepSWE (Together AI)](https://www.together.ai/blog/deepswe) — related Agent RL approach
- [HF Discussion #14 — JSON formatting issue](https://huggingface.co/Qwen/Qwen3-Coder-Next/discussions/14)
- [Qwen3 Chat Template Deep Dive](https://huggingface.co/blog/qwen-3-chat-template-deep-dive)
- [tokenizer_config.json (chat template)](https://huggingface.co/Qwen/Qwen3-Coder-Next/raw/main/tokenizer_config.json)

### Benchmarki i porównania
- [Qwen3-Coder official eval scripts (tau/BFCL)](https://github.com/QwenLM/Qwen3-Coder/tree/main/qwencoder-eval/tool_calling_eval)
- [tau-bench run script (Qwen3-Coder, temp=0.0)](https://github.com/QwenLM/Qwen3-Coder/blob/main/qwencoder-eval/tool_calling_eval/tau-bench/airline-qwen3-coder.bash)
- [arxiv:2511.05502 — MLX vs llama.cpp](https://arxiv.org/abs/2511.05502)
- [Unsloth Qwen3-Coder-Next docs](https://unsloth.ai/docs/models/qwen3-coder-next)
- [Qwen Function Calling docs](https://qwen.readthedocs.io/en/latest/framework/function_call.html)
