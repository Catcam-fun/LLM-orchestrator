---
name: frontend-ui
description: >
  Use this skill when an agent must design, implement, restyle, or polish a
  production frontend: landing pages, marketing surfaces, dashboards,
  internal tools, auth flows, or component libraries. It applies to React,
  Next.js, Vite, plain HTML/CSS/JS, TypeScript, dark-mode design systems,
  responsive layouts, motion/animation, typographic hierarchy, color
  systems, micro-interactions, hero sections, navigation patterns, and
  visual quality reviews. Trigger it when the task involves changing UI
  files, styling, layout, design-system tokens, introducing motion,
  restyling an existing surface, or reviewing visual quality. Relevant
  inputs: the existing styling stack, package.json, target audience for
  the surface, design references the user names, and the
  `state/capabilities.json` registry of approved libraries. Do not use it
  for backend APIs, CLI-only features, Obsidian syntax, data migrations,
  build tooling without a UI dimension, or pure copy-editing tasks that
  do not touch visual structure.
---

# frontend-ui

This skill provides the *toolkit* for frontend work (libraries, tokens,
motion patterns, anti-slop awareness). The *aesthetic direction* comes
from the user at task time: they may name references, paste images, or
describe the feel they want. Read what they actually asked for; this
skill helps you execute it well without falling into AI-default patterns.

## When to use

Trigger on tasks that involve:
- Restyling, redesigning, or building UI surfaces
- Setting up a design system foundation (tokens, typography, base components)
- Adding motion, animation, or micro-interactions
- Polishing existing UI for visual quality
- Choosing or installing frontend libraries

Do not use for:
- Tasks where the UI is incidental and not the user-visible deliverable
- Backend/API work even if it has a small admin surface
- Pure functional fixes to existing UI (an event-handler bug)
- Copy-edits that do not change visual structure

## Approved libraries

The vault maintains a pre-approved library registry at
`state/capabilities.json`. Pick from the list; never install outside it.
If a library you need is missing, surface the gap at refinement so the
user can decide whether to add it to the registry.

Frontend-relevant categories:

| Category | When to reach for it |
|---|---|
| `frontend.styling` | Tailwind CSS (preferred for fresh starts or broad restyles); paired deps: `postcss`, `autoprefixer`, `@tailwindcss/typography` |
| `frontend.components` | `shadcn` (copy-into-repo, not a regular npm install); Radix primitives via npm if shadcn is not a fit |
| `frontend.animation` | `framer-motion` for real motion (layout animation, page transitions, gesture support), not just hover transitions |
| `frontend.3d` | `three` + `@react-three/fiber` + `@react-three/drei` for hero/background 3D, particle effects, shaders |
| `frontend.icons` | `lucide-react` (tree-shakeable, 1000+ icons) |
| `frontend.theming` | `@radix-ui/colors` (paired color scales, not hand-picked hex); `next-themes` (theme toggle, works outside Next.js) |
| `frontend.forms` | `react-hook-form` when forms are heavy (multi-step auth, settings panels); skip for simple two-field forms |
| `frontend.utilities` | `clsx` + `tailwind-merge` (paired in a `cn()` helper); `class-variance-authority` for variant prop APIs |

At planning time, list the libraries the task needs under `## Packages Requested`. The execution agent runs the registry-specified `setup_steps` for each.

## Stack-assessment step (do this before designing anything)

1. Read `package.json`: what frameworks, what styling approach, what is already installed?
2. Read 1-2 representative page files: what is the current JSX shape? Inline styles? CSS modules? Tailwind classes? Plain CSS?
3. Read the global stylesheet: what tokens exist? What is hardcoded?
4. Read the routing: what pages exist? Which is the unauthenticated entry point?

Do not assume. Always check first.

## Workflow

### Phase 1: Design direction

1. Run the stack-assessment step.
2. Read the user's task carefully. If they named references, identify the *specific signature moves* they want applied (typography, color, motion, layout). Do not aim for a generic clone of any one site.
3. Frame the direction in the plan before implementing: name what you are doing and what you are explicitly avoiding from the anti-slop checklist below.

### Phase 2: Design tokens

- **Color palette**: prefer `@radix-ui/colors` paired scales over hand-picked hex. Use one base scale + one accent scale (12 steps each), mapped to semantic tokens (`--color-bg`, `--color-bg-elevated`, `--color-fg`, `--color-fg-muted`, `--color-border`, `--color-accent`, `--color-accent-bg`, `--color-accent-fg`).
- **Typography**: pick a real font, loaded via Google Fonts in `public/index.html` or `@import` in CSS. Do not rely on `system-ui` as a primary choice. Type scale: 12 / 14 / 16 / 18 / 20 / 24 / 30 / 36 / 48 / 60 / 72. Heading tracking: `-0.025em` for large headings, `-0.01em` for body. Line-height: `1.6` body, `1.1-1.2` headings.
- **Spacing**: 4px base unit; canonical scale 0/1/2/3/4/6/8/12/16/24/32/48/64/96/128.
- **Radius**: 6-8px max on most surfaces. Pills (`9999px`) only for badges and tags.
- **Motion**: default easing `cubic-bezier(0.2, 0, 0, 1)` for entry, `cubic-bezier(0.4, 0, 1, 1)` for exit. Hover: 150ms. Page transitions: 220ms. Use `transform` and `opacity` for performance.
- **Shadows**: minimize. Replace with `box-shadow: 0 0 0 1px <border-color>` for definition. If you need depth, use a single low-opacity drop shadow.

### Phase 3: Base components

1. Tailwind setup (per `capabilities.json` setup_steps for `tailwindcss`).
2. shadcn init (per registry setup_steps): creates `components.json` and `src/lib/utils.js` with the `cn()` helper.
3. `npx shadcn@latest add` the components you need: `button`, `card`, `dialog`, `input`, `dropdown-menu`, etc. Customize the generated code; you own it now.

Beyond shadcn defaults:
- **Navbar / Header**: sticky, blurred background (`backdrop-filter: blur(12px)`), 1px bottom border.
- **Hero**: real typographic hierarchy. Headline is the biggest element. Subhead in muted color. Single primary CTA.
- **Section variety**: do not repeat the same 3-column grid four times. Vary the layout per section.

### Phase 4: Motion (Framer Motion)

Pick 3-5 motion moments that genuinely add meaning:
- **Page entry**: stagger reveal of hero content. `initial={{opacity: 0, y: 20}}`, `animate={{opacity: 1, y: 0}}`, with `delay: 0`, `delay: 0.05`, `delay: 0.1` on title/subtitle/CTA.
- **Layout animation on content change**: wrap dynamic lists in `<motion.div layout>` so they smoothly reorder.
- **Hover**: `whileHover={{scale: 1.02}}` on cards is fine. Prefer ease curves over bouncy springs on hover (springs feel cheap).
- **AnimatePresence** for modals, drawers, toasts so exit animations are coherent.

Do not animate everything.

### Phase 5: Application

1. Replace inline styles, hardcoded hex, hardcoded font-family with tokens.
2. Replace raw `<button>` / `<input>` with the shadcn primitives.
3. Restructure layout to use the spacing scale consistently.
4. Add motion at the chosen moments.
5. Test at three breakpoints: mobile (<640px), tablet (640-1024px), desktop (>1024px).

## Anti-slop checklist (patterns that signal AI defaults)

These patterns are *signals*, not absolute prohibitions. If you find yourself reaching for several at once, you are probably producing the generic AI-React-app aesthetic. Pause and decide which are intentional.

| Pattern | Why it signals AI-default |
|---|---|
| Dark background + Inter + single subtle accent + rounded cards in a centered column | The exact combination every AI-generated SaaS landing page produces |
| Card radius > 12px on most surfaces | Reads as "AI template"; intentional design tends toward 6-8px |
| `system-ui` as a primary font | A real loaded font (Inter, Geist, your call) is almost always better |
| `box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1)` everywhere | 1px borders + an occasional accent ring almost always read sharper |
| Default Tailwind colors (`bg-blue-500` etc.) | Custom tokens via `@radix-ui/colors` or hand-tuned hex with intent communicates care |
| Logo grids of named brands as "social proof" | Actual numbers, real names, real screenshots build more trust |
| Purple-to-blue gradient hero | Either pick a specific signature gradient with intent, or use bold typography with one disciplined accent |
| Lorem ipsum, "Feature 1", "Coming soon" | If the copy is not real, the surface is not done |
| `transition: all 150ms` on everything | Target specific properties (`transform`, `opacity`, `color`) |
| Only a hover state on interactive elements | Design hover, focus, active, and disabled |
| Decorative chrome unrelated to the brand's actual voice | The brand lives in the copy and the typographic system, not in ornament |

## Common pitfalls

- **Over-restrained palette**: a single accent on a dark base reads as cliché. Add a second tinted scale at low opacity for variety in background and border zones.
- **Cargo-culting a reference**: copying a site's shadow + radius + font without understanding why produces a faded imitation. Pick a specific signature move and apply it; do not copy the whole aesthetic wholesale.
- **Animation as decoration**: hover transitions on every button, card, and link feels busy. Pick 3-5 moments that communicate state change or hierarchy.
- **Mobile as afterthought**: design mobile first, or design desktop and verify mobile holds at the same step. The mobile layout should not surprise you when you check.
- **Form state amnesia**: design loading, error, success, disabled. Auth forms especially. Turnstile-failed, validation-error, and rate-limit states matter as much as the happy path.

## Verification suggestions

When this skill loads, the plan should produce a verification block that checks both structural correctness and visual outcome. Example:

```yaml
verification:
  - id: tokens_file_exists
    type: assertion
    file: src/styles/tokens.css
    contains: "--color-bg"

  - id: real_font_loaded
    type: assertion
    file: public/index.html
    contains: "fonts.googleapis.com"

  - id: ui_primitives_present
    type: assertion
    file: src/components/ui/button.tsx
    contains: "variant"

  - id: build_passes
    type: test_suite
    run: npm run build
    expect_returncode: 0

  - id: visual_token_resolves
    type: command
    run: |
      npm test -- --watchAll=false --testNamePattern="visual background"
    expect_returncode: 0

verification_policy:
  max_iterations: 2
```

The `visual_token_resolves` check should be a test the agent writes that mounts the App and reads `getComputedStyle(document.documentElement)` for `--color-bg`, asserting it matches the token value. Cheapest behavioral check that the design system is wired up correctly.
