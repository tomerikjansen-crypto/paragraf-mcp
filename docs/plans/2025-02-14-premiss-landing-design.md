# premiss.app — Landing Page Design

## Overview

Personal studio portfolio site for Kasper Holter Johns — jurist & utvikler. Static single-screen split layout showcasing two products: Paragraf and Endringsmeldinger.

## Concept: "Splitten"

Full-viewport split layout representing the intersection of law and technology. Visually echoes the split-§ logo from Paragraf.

## Layout

```
┌──────────────────┬────────────────────────┐
│                  │                        │
│   IDENTITY       │   PRODUCTS             │
│   (~45%)         │   (~55%)               │
│   Dark           │   Light                │
│                  │                        │
│   Premiss        │   ┌──────────────┐     │
│   Kasper Holter  │   │ § Paragraf   │     │
│   Johns          │   │              │     │
│                  │   └──────────────┘     │
│   Jurist &       │                        │
│   utvikler       │   ┌──────────────┐     │
│                  │   │ Endringsm.   │     │
│   [GH] [LI]     │   │ NS 8407      │     │
│                  │   └──────────────┘     │
│                  │                        │
└──────────────────┴────────────────────────┘
```

- No scroll on desktop. Single viewport.
- Split is ~45/55 (identity narrower).
- Right half offset ~3-4px downward, echoing the §-logo split offset.

### Mobile

- Stacks vertically: identity top (~40vh, dark), products below (light, scrollable if needed).
- Icon links move to bottom.

## Left Half — Identity

Background: `#1B2A4A`. Content vertically centered.

1. **"PREMISS"** — small uppercase, `tracking-widest`, `white/60%` opacity
2. **"Kasper Holter Johns"** — large bold white text
3. **"Jurist & utvikler"** — subtitle, `white/70%` opacity
4. **One-liner** — "Jeg bygger verktoy i skjaeringspunktet mellom rett og teknologi." — `white/50%` opacity, max 2 lines
5. **Icon links** — GitHub + LinkedIn, subtle white icons at bottom

Subtle grid overlay in background (same as paragraf.dev).

## Right Half — Products

Background: `bg-pkt-bg-subtle` (light). Two product cards vertically centered.

### Paragraf Card

- Animated split-§ icon (terminal blink + red half slides in)
- **"Paragraf"** — bold
- "Norsk lov for KI — 770 lover, 3 600 forskrifter, 92 000 paragrafer" — subtle text
- Link to paragraf.dev

### Endringsmeldinger Card

- Simple icon (Radix `FileTextIcon` or similar)
- **"Endringsmeldinger"** — bold
- "Digital handtering av endringer etter NS 8407 for byggherrer og totalentreprenorer" — subtle text
- "Under utvikling" badge
- No link yet

### Card Style

White background, subtle border, light shadow. Same style as InfoCard on paragraf.dev. Hover: slight border-color change + translateY(-1px).

## Color Palette

| Element | Value |
|---------|-------|
| Left background | `#1B2A4A` |
| Right background | `bg-pkt-bg-subtle` |
| Accent | `#DC2626` (Norwegian red, used sparingly) |
| Text left | White with varied opacity (60%, 70%, 50%) |
| Text right | Standard `pkt-text-body` tokens |

## Animation

Load-only, no loops:

1. Left half fades in from left (0.3s ease-out)
2. Right half fades in from right (0.3s ease-out, 100ms delay)
3. §-animation in Paragraf card runs as on paragraf.dev

## Split Line

No visible border between halves. The dark/light contrast is the division. Right half has subtle vertical offset (~3-4px down) to echo the §-logo displacement.

## Tech

- React + Vite + Tailwind v4 (same stack as paragraf.dev)
- Static deploy to premiss.app (Cloudflare Pages or GitHub Pages)
- Repo TBD
