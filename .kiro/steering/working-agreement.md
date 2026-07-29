# Working agreement

How changes are made in this repository. Written after the font-chain fix; the reasoning
behind each rule is a defect this repository actually shipped.

## Gates — a change is not done until all of these pass

```bash
ruff check .                       # blocking; rule set pinned in pyproject.toml
python -m pytest                   # warnings are errors, --strict-markers, --strict-config
cd frontend && npm run lint && npm run test:run && npm run build
```

Three rules that follow from `SESSION_HANDOFF.md` §2 and are easy to violate accidentally:

- **A skip is not a pass.** CI fails if any test is skipped. A test that stops running
  because a binary went missing has silently removed its own coverage.
- **Warnings are errors.** A new dependency that emits a `DeprecationWarning` fails the
  suite until triaged. Do not relax `filterwarnings`; add a targeted `ignore` with a
  comment saying why it cannot be fixed.
- **CI needs full history** (`fetch-depth: 0`). Two parity guards diff against
  `origin/main`; a shallow checkout makes them skip.

## Test against the real program, never a mock of it

The `stem_inpainting` engine shipped, merged, and could not run on any machine while 598
tests passed. The `ffmpeg -filters` probe identified the flag column with
`not parts[0].isalnum()` — false for `TSC` (all flags set), so 124 of ffmpeg 7.0's 486
filters were dropped, including the `highpass`/`lowpass` the engine required. Every
capability test mocked the probe, and every fixture used dot-bearing flag groups only.

So: **anything that parses another program's output gets a test that runs the real
program**, cross-checked through an independent mechanism that shares no parsing code.
See `tests/test_capabilities_real_binary.py`.

## Assert the resolved value, not the requested one

The same class of defect, in the font chain: presets requested `Arial`, and the fallback
for an unavailable font was *also* `Arial`, so libass silently resolved to whatever
fontconfig offered (Noto Sans regular, with synthesised bold). Tests asserted the
requested font, and the golden files had `font_substituted:Arial` frozen in as correct.

When a value passes through a resolver — fonts, codecs, filters, model names — the test
must assert **what came out**, not what went in.

## Fonts specifically

- Bundled faces live in `assets/fonts/`, described by `assets/fonts.json`, with licences in
  `assets/font-licenses/`. `captions.subtitles_filter` passes the directory to libass as
  `fontsdir`, and the Dockerfile also installs the faces system-wide. Appearance must not
  depend on what the host happens to have installed.
- **`assets/fonts/` must contain nothing but font files.** libass offers every entry to
  FreeType and warns on anything else (`Read failed` for a subdirectory, `Error opening
  memory font` for a stray file). That is why the manifest and licences are siblings, not
  children. The burn tests in `test_kinetic_ass.py` fail on any libass warning, which is
  what caught it.
- **ASS can only express bold on/off**, which fontconfig reads as weight 200. It cannot
  ask for ExtraBold (205) or Black (210). A weight heavier than bold must be requested by
  *family name* — `fc-match "Poppins ExtraBold"` resolves to `Poppins-ExtraBold.ttf`,
  whereas `fc-match Montserrat:bold` caps out at the Bold instance. This is why the
  fallback ladder prefers faces whose heavy weight is its own family (`Anton`,
  `Archivo Black`, `Bebas Neue`) and why C3 needs a real `font_weight` field.
- **Verify a face before adding it to the manifest**, with both:

  ```bash
  fc-query -f '%{family}|%{style}\n' assets/fonts/<file>   # what the file declares
  fc-match  -f '%{family[0]}|%{style[0]}|%{file}\n' '<Name>'  # what a request resolves to
  ```

  The two answers differ more often than expected. Variable fonts are usable — fontconfig
  matches their weight axis, so `Montserrat` gives Regular and `Montserrat:bold` gives
  Bold — but `%{style[0]}` on a variable file reports its *first* named instance (Thin for
  Montserrat), which is not what a request resolves to. Do not read one as the other.
- **`Arial` is never installed on Linux.** With `fonts-liberation` present it metric-aliases
  to Liberation Sans Regular; without it, to Noto Sans. Either way the result is a
  regular-weight sans with synthesised bold, which is the bug C1 fixed. Never use a font
  name as a fallback without checking `fc-match` resolves it to a real file.

## Change shape

- One vertical slice per commit; the suite is green at every commit, not just at the end.
- Conventional commit prefixes (`fix:`, `feat:`, `docs:`, `test:`, `chore:`).
- `docs/IMPROVEMENT_PLAN.md` is the backlog; reference item IDs (`C1`, `A3`, `M7`) in
  commits and PR titles so the plan stays navigable.
- Do not mix a mechanical sweep (formatter, ruff `UP`/`B`) with a behavioural change.
- Do not start clip-selection quality work (§3) before the evaluation harness (**S1**)
  exists; without it the results cannot be judged.

## `.env.example` is a contract

`tests/test_config_documentation.py` fails if a `Settings` field is undocumented or a
documented key is not a real setting. Adding a setting means adding it there too.
