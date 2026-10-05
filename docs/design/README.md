# Design reference

`Sanctuary Prototype.dc.html` is the Claude Design prototype the React frontend
(`frontend/`) is built from; `support.js` is the runtime it needs. Open the HTML
file in a browser to see it (it loads React and fonts from public CDNs, so it
needs network access — the real app ships everything self-hosted).

Design tokens live in `frontend/src/styles/index.css`; screens are ported view by
view under `frontend/src/features/`.
