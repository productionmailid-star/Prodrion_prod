# Building the dashboard CSS

`index.html` and `dashboard.html` load `/dist/app.css` (built by Tailwind),
not the `cdn.tailwindcss.com` Play CDN — that JIT compiler is dev-only and
must never be used in production. Build the stylesheet:

```bash
cd web
npm install
npm run build:css        # writes web/dist/app.css
```

`web/dist/app.css` is generated, not committed — run the build (or
`deploy/deploy.sh`, which runs it for you) before serving these pages, or
they'll render unstyled.

(`tailwind.config.js` mirrors what used to be the inline `tailwind.config`
blocks, and `src/input.css` is the entry point.)

`npm run watch:css` rebuilds on change during development.

## Deploy note

`web/` is deployed as static files behind the same nginx that fronts the API
(so `/dist/app.css` and the Supabase SDK are same-origin-cacheable). The
Supabase JS SDK is pinned with an SRI hash; to remove the CDN dependency
entirely, vendor `supabase.min.js` into `web/vendor/` and load it locally.
