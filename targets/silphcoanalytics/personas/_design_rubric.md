# SilphCo Design Quality Rubric

A scored rubric for automated design review of the SilphCo Analytics frontend.
Dimensions are evaluated by the vision critic in the `DESIGN_REVIEW` phase.
Each dimension is scored 0–100; scores feed `SpineConfig.design_score_threshold` (default 70).

---

## Dimensions

### 1. spacing (0–100)
Measures whitespace consistency and layout rhythm.

| Score | Meaning |
|-------|---------|
| 90–100 | Consistent 4/8px grid throughout; clear breathing room between sections; no crowding |
| 70–89  | Minor inconsistencies (1–2 off-grid elements); overall comfortable |
| 50–69  | Noticeable crowding or uneven padding; sections feel compressed or misaligned |
| 25–49  | Multiple spacing violations; layout looks rushed or unpolished |
| 0–24   | Broken layout; elements overlap or collide; unusable |

**Good heuristics:**
- Text never touches a container edge without at least 12px padding.
- Section gaps are multiples of 8px.
- Card grids have equal gutters between items.

**Bad heuristics:**
- Price label clipped at card edge (see `CardDetail` component, ref `/cards/base1-4`).
- Header nav items with 1–2px spacing difference between desktop vs mobile builds.

**Good exemplar:** `/` (home) default state — hero section uses consistent 32px vertical rhythm.
**Bad exemplar:** `/cards/base1-4` when `grade_badge` overflows the card bounds.

---

### 2. hierarchy (0–100)
Measures typographic contrast and information architecture.

| Score | Meaning |
|-------|---------|
| 90–100 | Clear H1→H2→body→caption scale; price and card name always dominant |
| 70–89  | Hierarchy mostly clear; minor ambiguity (e.g. H2 vs H3 weight) |
| 50–69  | Two or more competing visual weights; hard to identify primary CTA |
| 25–49  | Flat type scale; nothing stands out; user must hunt for key data |
| 0–24   | No hierarchy; all text same size/weight |

**Good heuristics:**
- Card name is always the largest text on the card component.
- Price (`tvwap_price_usd`) is visually heavier than provenance metadata.
- Section titles are ≥4px larger than body copy.

**Bad heuristics:**
- Badge label same font-size as card title.
- Grader name visually competing with the grade number on `GradeChip`.

---

### 3. contrast (0–100)
Measures WCAG AA color contrast compliance.

| Score | Meaning |
|-------|---------|
| 90–100 | All text ≥4.5:1 on background; interactive elements ≥3:1; passes AA |
| 70–89  | 1–2 minor violations (decorative text, disabled states); functionally accessible |
| 50–69  | Primary content fails contrast on some states (hover, dark mode) |
| 25–49  | Multiple body-text contrast failures; readable only in ideal conditions |
| 0–24   | Text invisible or unreadable against background |

**Good heuristics:**
- Momentum direction indicators (`↑ up`, `↓ down`) use distinct colors with ≥3:1 contrast.
- Chart axis labels are ≥4.5:1 on chart background.

**Bad heuristics:**
- Light grey price change % on white card background (common in light mode).
- `tvwap_staleness` indicator fading to near-white when staleness > 0.8.

---

### 4. chart_legibility (0–100)
Measures readability and correctness of data visualizations (price charts, population bars, etc.).

| Score | Meaning |
|-------|---------|
| 90–100 | Axes labeled, values readable, legend present; no overlapping labels |
| 70–89  | Minor label overlap or missing units; data still interpretable |
| 50–69  | Key labels cut off; axis values crowded; requires effort to read |
| 25–49  | Charts missing labels or units; data uninterpretable without tooltip |
| 0–24   | Chart broken / blank; renders no useful data |

**Good heuristics:**
- Y-axis always shows currency unit (`USD`) or grade range.
- Confidence bands (`tvwap_predicted_low_usd` / `_high_usd`) are visually distinct from the trend line.
- Mobile chart collapses gracefully: fewer X-axis ticks, no label overlap.

**Bad heuristics:**
- Momentum CI band same color as price line (indistinguishable).
- Set daily volume bars cut off at chart boundary when volume spikes.

---

### 5. mobile (0–100)
Measures responsive layout quality at the mobile breakpoint (390×844 viewport).

| Score | Meaning |
|-------|---------|
| 90–100 | All content accessible; no horizontal scroll; touch targets ≥44×44px |
| 70–89  | Minor horizontal overflow on 1 component; touch targets slightly small |
| 50–69  | Some content hidden or requires horizontal scroll |
| 25–49  | Multiple layout breakages; critical content clipped |
| 0–24   | Layout broken; primary content unusable on mobile |

**Good heuristics:**
- Navigation collapses into a hamburger or bottom tab bar at ≤480px.
- Card grids switch to single-column at mobile breakpoint.
- Price number is not truncated on any standard mobile width.

**Bad heuristics:**
- Desktop table view rendering at mobile with horizontal scroll rather than card/list view.
- `GradeChip` stack overflowing its container on the card detail page at 390px.

---

## Verdict mapping

| Verdict | When to use |
|---------|-------------|
| `pass` | All dimensions ≥ threshold (default 70); no `high`-severity issues |
| `needs_work` | One or more dimensions 50–69, OR 1–2 `medium`-severity issues |
| `block` | Any dimension < 50, OR any `high`-severity issue |

The policy gate (`DefaultPolicy.design_gate`) additionally checks the mean
score against `SpineConfig.design_score_threshold` and upgrades a `pass` verdict
to `needs_work` if the mean falls below the threshold.
