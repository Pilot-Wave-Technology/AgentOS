# `static/` — browser assets

CSS, JavaScript and the favicon, served directly by the web service.

There is no build step and no bundler: files here are what the browser gets.
That is deliberate for a tool whose users are mostly running it for themselves —
it keeps `git clone` to a working UI with no toolchain in between.

Templates live in [`../app/templates/`](../app/templates/).
