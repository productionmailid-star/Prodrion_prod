# Building the dashboard CSS

The HTML currently loads Tailwind from `cdn.tailwindcss.com`, which is a
**development-only** JIT compiler and must not be used in production. Build a
static stylesheet instead:

```bash
cd web
npm install
npm run build:css        # writes web/dist/app.css
```

Then in each HTML file replace:

```html
<!-- dev only -->
<script src="https://cdn.tailwindcss.com?plugins=forms,container-queries"></script>
<script>tailwind.config = { ... }</script>
```

with:

```html
<link rel="stylesheet" href="/dist/app.css">
```

(`tailwind.config.js` already mirrors the inline `tailwind.config` blocks, and
`src/input.css` is the entry point.)

`npm run watch:css` rebuilds on change during development.

## Deploy note

`web/` is deployed as static files behind the same nginx that fronts the API
(so `/dist/app.css` and the Supabase SDK are same-origin-cacheable). The
Supabase JS SDK is pinned with an SRI hash; to remove the CDN dependency
entirely, vendor `supabase.min.js` into `web/vendor/` and load it locally.
