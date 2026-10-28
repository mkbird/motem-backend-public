# Motem Widget Template — AI Development Guide

## What you're building

A standalone H5 widget rendered inside an `WKWebView` on iOS, embedded in
the Motem dashboard. The widget lives in its own folder and consists
of `index.html` + `app.js` + `meta.json` + assets.

Multiple widgets can be on the same page. Your widget is **just a
rectangle of HTML** — there is no chrome, no header, no scrollbar.

## File layout (cwd = widget folder, use relative paths only)

- `index.html` — entry point. Loads `./assets/base.css` and `./app.js`. **MODIFY THIS** to set up the DOM skeleton.
- `app.js` — runtime logic. **MODIFY THIS**.
- `meta.json` — widget metadata. **MODIFY THIS** (title, description, supported_sizes).
- `assets/base.css` — DS design tokens (colors, font). **DO NOT MODIFY**.
- `assets/fonts/VT323-Regular.ttf` — pixel font, registered by base.css. **DO NOT MODIFY**.

You may add more files under `assets/` (extra CSS, JS, images) — but **never reference URLs outside this folder** for fonts/CSS/JS. External `fetch()` to HTTPS APIs is OK and encouraged.

**CRITICAL**: All file ops (Read/Write/Edit) MUST use relative paths. NEVER use absolute paths or `..`.

## Style rules (visual consistency)

Your widget MUST use the existing design tokens. They are CSS custom properties in `:root`:

```css
color: var(--ds-fg);              /* foreground (text) */
background: var(--ds-bg);         /* never use; host paints bg */
font-family: var(--ds-pixel-font);/* VT323 pixel font */
```

- Background MUST be transparent (the host SwiftUI provides the page bg).
- Text color MUST be `var(--ds-fg)` — sometimes with `opacity` to dim.
- Font MUST be `var(--ds-pixel-font)`. NEVER bring in Google Fonts. NEVER hardcode `#fff` or `#000` or `Arial`.
- Use UPPERCASE + wide letter-spacing for labels (matches the existing widgets).
- Round corners with `border-radius: var(--ds-radius)`.
- Avoid heavy box-shadows (the host already overlays a CRT pixel grid).

## Sizes

Your widget will be loaded inside one of these sizes (decided by the
user from `meta.json["supported_sizes"]`). The `<body>` element fills
the assigned rect; you should adapt with `@media` or by reading
`window.motem.size`:

| Size       | Logical px (approx) | Use case                 |
|------------|----------------------|--------------------------|
| small      |  ~80×80              | one number / one icon    |
| medium     |  ~170×80             | one row, label + value   |
| large      |  ~170×170            | label + value + chart    |
| xlarge     | full screen           | richest layouts          |

Set `meta.json["supported_sizes"]` to whichever sizes you actually lay
out for. Sizes you don't support are hidden in the picker.

## Host runtime API

The host injects `window.motem` at document-start:

```js
window.motem = {
  locale: "en" | "zh-Hans",
  size:   "small" | "medium" | "large" | "xlarge",
  params: { /* user-set widget params */ },
  apiBase: "http://...",        // backend base URL
  authBearer: "<jwt>",          // user's access token
  fetchData: async fn(channel), // PREFERRED data source — see below
  onTimeTick: fn(cb),           // cb(Date) every 1s while widget is mounted
}
```

DO NOT use `setInterval` for clock updates — use `onTimeTick`. Setting your own interval prevents the host from suspending widgets when offscreen.

## Host-injected CSS hooks (XL fullscreen support)

When the widget renders at `xlarge` on the dashboard, the host's iOS WKWebView
fills the **entire screen edge-to-edge** (no host frame). The widget's `<body>`
is the viewport. To prevent content being hidden behind iPhone Dynamic Island
or Home Indicator, the host injects two things at `documentStart`:

### 1. `:root { --ds-safe-* }` CSS variables

Real screen safe-area insets in pixels (iPhone 16 Pro Max landscape:
`left/right ≈ 62`, `bottom ≈ 20`, `top ≈ 0`). The default `base.css`
already uses these on `body { padding }`, so **simple widgets that just put
content in body get safe-area-aware padding for free**.

Use them in your own CSS:

```css
.my-stage {
  padding: var(--ds-safe-top, 12px)
           var(--ds-safe-right, 12px)
           var(--ds-safe-bottom, 16px)
           var(--ds-safe-left, 12px);
}
```

The fallback (`12px` / `16px`) covers non-XL sizes (S/M/L on dashboard) where
host doesn't inject vars — your widget still has reasonable edge spacing.

### 2. `documentElement.dataset.dsSize` attribute (`<html data-ds-size="xlarge">`)

The size mode (`small` / `medium` / `large` / `xlarge`) is also exposed as
an HTML attribute. Use attribute selectors to apply mode-specific layout:

```css
/* Make digits bigger only at xlarge */
[data-ds-size="xlarge"] .digit { font-size: 200px; }

/* Different number of items by mode */
[data-ds-size="small"]  .extras { display: none; }
[data-ds-size="xlarge"] .extras { display: flex; }
```

This is more reliable than `@container (min-width: ...)` for size-mode
detection because XL on the **dashboard** (~932px wide) and XL in the
**creator preview** (~540px wide) are both `[data-ds-size="xlarge"]` but
have very different viewport widths. Use `[data-ds-size]` for "what mode
am I in" and `@container (min-width: ...)` for "how wide is my actual
viewport".

## Full-bleed XL pattern (when host frame conflicts with widget bg color)

If your widget has a non-transparent background color that DOESN'T match the
dashboard's dark gray, the default `base.css` body-padding approach leaves
a thin border of dashboard color visible around the widget at XL. To get a
true edge-to-edge bg, use **`position: fixed; inset: 0`** on your stage layer:

```css
.my-stage {
  position: fixed;
  inset: 0;
  background: <your widget bg>;
  /* Manually apply safe-area padding here since position:fixed bypasses
     body padding */
  padding: var(--ds-safe-top, 12px)
           var(--ds-safe-right, 12px)
           var(--ds-safe-bottom, 16px)
           var(--ds-safe-left, 12px);
}
```

Caveat: at XL on landscape iPhone, Dynamic Island visually covers ~120px on
the leading edge — wider than the safe-area-inset of ~62px reports. If
content hugging the leading edge gets occluded, hardcode larger padding
**only for actual fullscreen XL** (not preview):

```css
@container widget (min-width: 800px) {
  [data-ds-size="xlarge"] .my-stage {
    padding-left: 90px !important;
    padding-right: 90px !important;
  }
}
```

The `min-width: 800px` filter ensures preview XL (~540px) keeps tighter
padding so its content doesn't get crushed.

## Wide-aspect overflow at XL

Layouts with fixed-aspect children (e.g., `.tube { aspect-ratio: 0.5 }`)
that lay out in a flex row often **overflow horizontally** at XL because
landscape iPhone is wider than the natural fit allows. Solution: cap
the children's `max-height` based on viewport width.

```css
[data-ds-size="xlarge"] .tubes {
  /* For N children of average aspect r:
     total width = N * r * h + (N-1) * gap
     h ≤ (container_w - 2*padding - (N-1)*gap) / (N * r) */
  max-height: min(
    calc((100cqw - 100px) / 3.64),  /* example: 8 tubes, mixed aspect */
    180px                            /* hard cap so dashboard XL still big */
  );
}
```

Use `100cqw` (container query units) to react to actual viewport width —
`body` has `container-type: size; container-name: widget` set by your CSS
when you opt in (e.g. `body { container-type: size; container-name: widget; }`),
or omit and use `100vw` as a coarser fallback.

## Backend channels — PREFERRED data source

For weather / todos / pomodoro / calendar etc, ALWAYS pull from the backend
via `fetchData(channel)`. The host caches the latest snapshot per user, so
even if the iOS app is in the background widgets get fresh data.

Available channels:
- `weather`  → `{temp, feels_like, humidity, description, icon, city}`
- `todos`    → `{count, pending, items: [{title, done}]}`
- `pomodoro` → `{state, remaining_s}`

```js
async function loadWeather() {
  const w = await window.motem.fetchData('weather');
  if (!w) {
    document.getElementById('temp').textContent = '--';  // friendly placeholder
    return;
  }
  document.getElementById('temp').textContent = Math.round(w.temp) + '°';
}
loadWeather();
setInterval(loadWeather, 300000);  // refresh every 5 min
```

`fetchData` adds `Authorization` + `Accept-Language` headers, 8s timeout, try/catch. Returns the payload directly (unwraps `{channel, payload, updated_at}`), or `null` on any error.

## External network — custom data not in backend channels

For things the backend doesn't track (NBA scores, news, exchange rates), `fetch()` HTTPS directly. Rules:

- HTTPS only. ATS blocks plain `http://`.
- Wrap every fetch in `try/catch`; render a placeholder on failure, NEVER blank.
- Cache results on `window` to survive widget reloads (no `localStorage`).
- Add `signal: AbortSignal.timeout(8000)` to every fetch.

## i18n

Player-facing strings MUST be a `{en, "zh-Hans"}` dict — branch on `window.motem.locale`. Internal logs / variables / comments stay English.

`meta.json` `title` and `description` can be plain strings; `title_i18n` / `description_i18n` MUST contain both `en` and `zh-Hans`.

## What you MUST NOT do

- Do NOT call `window.open(...)` — the host blocks it
- Do NOT navigate away from `index.html` (no `<a target=_top>`)
- Do NOT use `eval` / `new Function`
- Do NOT use `localStorage` / `sessionStorage` / `indexedDB`
- Do NOT use `<iframe>` to load remote pages
- Do NOT request camera, microphone, geolocation, notifications
- Do NOT include third-party fonts via `@import` / Google Fonts

## Final response format (separated by `---`)

After making file edits, output:

```
---
description_i18n: {"en": "one English sentence describing the widget", "zh-Hans": "一句简体中文描述当前小组件"}
supported_sizes: ["medium", "large"]
```

The host parses this to update `meta.json` and the size picker.
