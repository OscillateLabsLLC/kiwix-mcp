# Kiwix → Neon/OVOS Solver + Skill: Design & Plan

Status: **plan, not yet implemented**. Target runtime: **Neon Hub** (pinned 0.x OVOS),
with forward-compatibility toward modern OVOS where it is cheap.

## 1. What the two reference repos actually are

### `neon-solver-plugin-wikipedia`
A ~100-line `AbstractSolver` implementation. All retrieval is delegated to the
`wikipedia_for_humans` library; the plugin itself is glue plus **query shaping**:

- `extract_keyword()` — strips conversational framing ("who is X", "tell me about X")
  down to a bare search term using `simplematch`.
- `get_secondary_search()` — "what is the *population* of *France*" → `(France, population)`.
- `get_data()` / `get_spoken_answer()` / `get_image()` / `get_expanded_answer()` — the
  exported solver methods. `get_expanded_answer()` sentence-splits the summary into
  steps, which is what powers "tell me more".

**The value to us is the shape, not the code.** This is the contract that lets a
knowledge source participate in OVOS *common query*, where solvers bid on a question.

### `skill-wikipedia`
An OVOS skill, and the weaker model to copy. It bypasses the solver entirely and calls
`wikipedia_for_humans` directly, holding mutable `self.idx` / `self.results` for
pagination. It subclasses `NeonSkill` (hard `neon_utils` dependency) and declares
`requires_internet=True` — **exactly backwards for Kiwix**.

### `kiwix-mcp` (this repo)
Already has the hard part: `KiwixClient.list_books()/search()/fetch_article()` and a
`parse.py` that handles kiwix-serve's malformed OPDS ampersands, search-HTML scraping,
and tag stripping. `kiwix_ovos/` is already stubbed and already listed in the hatch
wheel packages.

## 2. Measured facts (verified this session, not assumed)

### Version eras — the actual Neon Hub pins

**Confirmed by Mike from the live hub:** `ovos-plugin-manager` **0.9.0**,
`ovos-workshop` **0.1.7**. Verified by installing those exact pins and running
`inspect.signature`.

| | **Neon Hub (our target)** | Modern OVOS |
|---|---|---|
| `ovos-plugin-manager` | **0.9.0** | 2.x |
| `ovos-workshop` | **0.1.7** | `>=8.0.0` |
| Solver signature | **`(query, lang=None, units=None)`** | same |
| Entry point group | **`neon.plugin.solver`** | `opm.solver.question` |
| Skill → answer path | solver plugin | direct engine + `@common_query` |

This is a **hybrid**, and it is better news than an early draft of this plan assumed:

- **The solver signature is already the MODERN one.** opm 0.9.0 uses
  `get_spoken_answer(self, query, lang=None, units=None)` — no `context` dict.
  The `neon-solver-plugin-wikipedia` `(query, context)` signature is **obsolete for us**
  and must NOT be copied. Port its *keyword-extraction logic*, not its method contract.
- **The entry point group is still the OLD one.** Verified in
  `ovos_plugin_manager/utils/__init__.py:51`:
  ```python
  QUESTION_SOLVER = "neon.plugin.solver"  # TODO rename "opm.solver.question"
  ```
  The upstream `TODO` confirms the rename is planned but had not happened at 0.9.0.
  `find_plugins(PluginTypes.QUESTION_SOLVER)` is the loader path.
- **No abstract methods** (`__abstractmethods__` is `None`), so nothing is
  force-required at class definition time; override what we need.

Environment note: `ovos-plugin-manager` imports `pkg_resources`, which requires
**`setuptools<81`** on Python 3.12+. Confirmed by Mike; reproduced here.

Environment note: `ovos-plugin-manager` 0.0.25 imports `pkg_resources`, which requires
**`setuptools<81`** on Python 3.12+. Confirmed by Mike; reproduced here.

### Live server behaviour (graywind, 39 books; prepdisk, 15 books)

- **Unscoped search does NOT 400 on the graywind server** despite mixed `en`/`es`/`fr`
  books. The README's multi-language caveat did not reproduce. Unscoped returned
  4000 results for "who is Isaac Newton".
- **A bad book slug returns HTTP 400 with a clean, parseable body:**
  `No such book: nonexistent_book_2024`. This makes loud failure on a stale slug cheap
  and precise.
- **Search latency scales with book size** (measured, `pattern=water+purification`):

  | book | time |
  |---|---|
  | `explainxkcd_en_all_maxi_2021-03` | 0.18s |
  | `openstreetmap_en_all_maxi_2020-05` | 0.99s |
  | `skeptics.stackexchange.com_en_all_2025-08` | 2.14s |
  | `gutenberg_en_all_2020-10` | **4.69s** |
  | `wikipedia_en_all_maxi_2024-01` (6.86M articles) | **timed out via MCP** |

  Catalog on the same host answers in 0.16s, so the host is healthy — large-book search
  is simply slow. **This is a voice-UX constraint**: `common_query` has a bounded time
  budget, and a 5s answer arrives after the framework has moved on.

### Retrieval quality — the load-bearing finding

**Rank-1 is not reliably the best spoken answer.**

- "who is Isaac Newton" (unscoped) → rank-1 is *The Life of Sir Isaac Newton*, a
  **101,851-word** Gutenberg book whose first paragraph is 1820s publisher boilerplate
  about "an American Family Library". Speaking that aloud is gibberish. Rank-4 is an
  xkcd comic. Noise like *Vital Records of the Town of Auburn* matched on the
  stopword "who".
- Scoped + keyword-extracted "Isaac Newton" → 9 clean results instead of 4000 noisy ones.
- "how to purify water" in Appropedia → rank-1 is a niche gadget ("The Life Bowl");
  the genuinely useful general article ("Water filter") is rank-13. Word counts here
  are sane (300–2000), so this book suits voice well.

Consequences:
1. **Keyword extraction is load-bearing, not cosmetic.** Porting the Neon
   `extract_keyword()` logic is required, not optional.
2. **The confidence gate must consider word count, not just title match.** A 100k-word
   book is a bad answer source regardless of title similarity — *in an encyclopedia
   corpus*. In a book corpus it is the only thing on offer, which is why the ceiling is
   configurable (`AnswerTuning.max_words`, `for_long_form()`) rather than fixed.

## 3. Design decisions (agreed)

- **Location:** in this repo, under `kiwix_ovos/`.
- **Answer strategy:** top hit + **title-match confidence gate**.
- **Book scope:** **explicit book slug in config** (no auto-detection).

### Noted risk on explicit slugs
Slugs embed dates (`wikipedia_en_all_maxi_2024-01`, `devdocs_en_jq_2025-10`), so a pinned
slug **breaks on every ZIM update**. Decision respected, with two mitigations that keep
it explicit:
- Accept the stable OPDS `name` (e.g. `devdocs_en_jq`) *as well as* the dated `slug`.
- Fail **loudly at init** (log an error listing available slugs) on `No such book:`,
  rather than degrading to a silent no-answer.

## 4. Structure (implemented)

```
kiwix_ovos/
  engine.py   # AnswerTuning + KiwixRetrievalEngine — plain Python, zero OVOS imports
  solver.py   # KiwixSolver — thin adapter on the modern signature
  skill/      # Neon-targeted skill (locale, intents)  — NOT YET BUILT
```

### `engine.py` — framework-free, independently testable
Depends only on the existing `KiwixClient`. Holds:
- keyword extraction (ported from the Neon plugin's `simplematch` regexes as plain
  `re`, dropping the extra dependency),
- search → gate → fetch → `strip_html` → lead-paragraph truncation,
- scoring: title/keyword overlap **and** a word-count sanity band.

Zero OVOS coupling means it is testable under the existing pytest setup and survives
any future framework churn. This is where the portable value lives.

#### `AnswerTuning` — every threshold is configurable
An earlier draft of this design hardcoded the gate thresholds as module constants. That
was wrong in a way that mattered: those values are judgement calls about a *particular
corpus*, and the hardcoded ceiling made **Project Gutenberg unanswerable** — every
article in a book library exceeds 20k words, so the engine refused the whole corpus with
no config escape.

They are now fields on a frozen `AnswerTuning` dataclass, validated in `__post_init__`
so illegal states (overlap > 1.0, `max_words < min_words`, non-positive timeout) fail at
construction rather than misbehaving silently.

| field | default | purpose |
|---|---|---|
| `min_words` | 40 | reject stubs — too little prose to speak |
| `max_words` | 20_000 | reject book-length texts; **0 disables** |
| `min_title_overlap` | 0.5 | fraction of keyword tokens required in the title |
| `exact_title_bonus` | 0.5 | score bonus for a (near-)exact title match |
| `concise_bonus` / `concise_words` | 0.1 / 5_000 | nudge toward article-length prose |
| `min_sentence_chars` | 25 | treat shorter fragments as headers when finding the lead |
| `summary_chars` | 400 | spoken-summary budget |
| `timeout` | 5.0 | abandon slow searches before the caller's budget expires |
| `boilerplate_prefixes` | see source | line prefixes skipped when hunting the lead paragraph |

Two constructors matter:
- **`AnswerTuning.for_long_form()`** — for book-length corpora (Gutenberg). Sets
  `max_words=0` and raises `min_title_overlap` to 0.75 to compensate for the lost length
  signal.
- **`AnswerTuning.from_config(dict)`** — filters unrelated keys, so a solver can pass its
  entire config block through without knowing which keys are tuning knobs.

The only remaining module-level constant is `_EXACT_MATCH_THRESHOLD = 0.99`, which is a
float-comparison epsilon ("all tokens matched"), not a tunable preference.

### `solver.py` — thin adapter, modern signature
Because opm 0.9.0 already uses the modern contract, **no compatibility shim is needed**:

```python
def get_spoken_answer(self, query: str, lang: Optional[str] = None,
                      units: Optional[str] = None) -> Optional[str]:
```

This is source-identical to modern OVOS, so the solver is forward-compatible **for free**.
An earlier draft proposed a dual `context=`/`lang=` signature; that is now dead weight and
should not be written.

Entry points — declare **both**:
```toml
[project.entry-points."neon.plugin.solver"]
kiwix = "kiwix_ovos.solver:KiwixSolver"

[project.entry-points."opm.solver.question"]
kiwix = "kiwix_ovos.solver:KiwixSolver"
```
The first is what the hub loads today; the second is inert now and becomes live when
upstream executes its documented `TODO` rename. Extra entry points cost nothing on a
runtime that does not scan that group.

**Verified, not assumed:** with the package installed into a venv pinned to opm 0.9.0,
`find_plugins(PluginTypes.QUESTION_SOLVER)` returns `{'kiwix': KiwixSolver}`. A clean
import would not have proven discoverability.

#### Solver config
```yaml
base_url:   http://localhost:8080     # kiwix-serve URL
book:       appropedia_en_all_maxi_2025-03   # required — search is scoped per-book
long_form:  false                     # true for Gutenberg-style book corpora
# any AnswerTuning field may also be set here:
summary_chars: 400
min_title_overlap: 0.5
timeout: 5.0
```
Unrecognised keys are ignored, so the whole settings block can be passed straight
through. A missing `book` raises at construction — a solver that silently never answers
hides its own misconfiguration.

The solver never raises into the `common_query` path: a stale slug or transport failure
is logged and converted to a decline, so one misconfigured plugin cannot take down the
question pipeline.

### Skill layer — target Neon only
**Recommendation: do not make the skill dual-target.** The skill layer diverged much
harder than the solver layer (`NeonSkill` + `neon_utils` vs `OVOSSkill` 8.x +
`@common_query`). Keep the skill thin and let the engine carry portability. See the
pin caveat in §6.

## 4b. Deploying it

Install on the hub with the skill extra (which pulls the solver too):

```bash
pip install 'kiwix-mcp[skill]'
```

Skill settings (`~/.config/mycroft/skills/kiwix-mcp.oscillatelabsllc/settings.json`):

```json
{
  "base_url": "http://prepdisk.graywind.org:3000/kiwix",
  "books": [
    {"book": "wikipedia_en_all_maxi_2024-01"},
    {"book": "wikihow_en_maxi_2023-03", "preset": "how_to"},
    {"book": "mdwiki_en_all_2024-06"},
    {"book": "gutenberg_en_all_2023-08", "preset": "long_form"}
  ]
}
```

Presets: `encyclopedia` (default), `how_to` (descriptive titles like "4 Ways to Purify
Water"), `long_form` (book corpora — disables the length ceiling). Any `AnswerTuning`
field set alongside `preset` overrides it. `base_url` may also be set per book, for a
deployment spanning several Kiwix servers.

**Verify each slug against the live server before deploying** — slugs embed dates and
change on ZIM update. A stale slug is logged as an error and that book declines; the
others still answer.

## 5. Open item — latency on very large ZIMs

`wikipedia_en_all_maxi` (6.86M articles) timed out on search while the catalog on the
same host answered in 0.16s. Options:
1. Scope to small/medium books (Appropedia, wikiHow, MDWiki — all sub-second) and treat
   the big Wikipedia ZIM as fetch-only, not common-query-able.
2. Short client-side timeout, returning no-answer rather than blocking the framework.
3. Warm/persist an `httpx` connection and measure whether large-book search improves.

**(2) is implemented** as `AnswerTuning.timeout` (default 5.0s), covered by
`test_search_timeout_yields_no_answer`. **(1) remains the recommended default
configuration.** (3) is unmeasured — worth a spike before pointing the solver at the
large Wikipedia ZIM.

## 6. Status

**Done:**
1. ~~Confirm the exact Neon Hub pins~~ — opm 0.9.0 / workshop 0.1.7, interface verified
   by `inspect.signature` against those exact versions.
2. ~~Build `engine.py` + unit tests~~ — `AnswerTuning` + `KiwixRetrievalEngine`, 95%
   covered via respx fixtures.
3. ~~Spike the solver against the real hub~~ — installed into an opm-0.9.0 venv;
   `find_plugins(PluginTypes.QUESTION_SOLVER)` lists `kiwix`, and the full
   `get_spoken_answer` / `get_data` / `get_expanded_answer` path answers end-to-end.
4. ~~Add `[project.optional-dependencies] ovos`~~ — solver tests `importorskip`, so the
   MCP-only install stays lean and the suite still passes without OVOS.

73 tests pass under the real Neon pins.

**Test-quality note.** The first version of the suite passed 18/18 while being unable to
detect the removal of its most important guard: disabling the word-count ceiling failed
nothing, because a two-result fixture let scoring pick the right answer for the wrong
reason. Each gate now has a test isolating it, re-verified by mutation after the
`AnswerTuning` refactor. **Re-run that mutation check after touching the gates** — a
green suite is not by itself evidence the guards work.

5. ~~Content extraction~~ — `extract_article_text()` returns only article prose;
   infoboxes, nav chrome and IPA no longer reach the speaker.
6. ~~Multi-book support~~ — `KiwixLibrary` fans out over explicitly configured books
   with per-book tuning, early-exiting once a book clears `confident_enough`.
7. ~~The skill layer~~ — see the resolved contract below.

**Remaining:**
- Choose the `books` list per deployment and verify each slug against the live server,
  since slugs carry dates and break on ZIM update. A stale slug is logged, not silent.
- Push the branch / open a PR.

### Skill-layer contract (resolved)
`ovos-workshop` **0.1.7** is the 0.x line, and the surface was confirmed against the
installed source rather than docs:

- **`@common_query` does not exist** before ovos-workshop 8. The modern
  `ovos-skill-wikipedia` structure cannot be copied.
- 0.1.x uses **`CommonQuerySkill.CQS_match_query_phrase(phrase)`** — the sole abstract
  method, taking a bare phrase with **no `lang` argument**.
- It returns the **4-tuple `(match, level, answer, callback)`**. The abstract method's
  own docstring describes a *3-tuple in a different order*; the framework's unpacking
  (`answer = result[2]`, `common_query_skill.py:143`) is authoritative. A test pins
  this, and reversing it fails two tests.
- The framework emits `handles_speech: True` and speaks the answer itself.
- `CQSMatchLevel` is `EXACT` / `CATEGORY` / `GENERAL`; engine confidence maps onto it
  at 0.9 and 0.6.

Implemented in `kiwix_ovos/skill/`, installed via the `skill` extra, registered as
`ovos.plugin.skill` → `kiwix-mcp.oscillatelabsllc`. From a clean venv the extra
resolves to exactly the hub's pins (workshop 0.1.7, opm 0.9.0, bus-client 0.1.6)
without pinning them explicitly.

Live behaviour: "who is Isaac Newton" bids EXACT in 0.41s, "how to purify water" bids
CATEGORY in 0.19s, nonsense declines.

**`runtime_requirements` must be inverted** from both Wikipedia repos:
`requires_internet=False`, `requires_network=True`. Kiwix is LAN-local; copying the
Wikipedia block verbatim means the skill will not load offline, defeating the point.
