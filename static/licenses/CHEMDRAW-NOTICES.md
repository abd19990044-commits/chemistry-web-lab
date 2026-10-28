## ChemDraw feature transfer and standalone 2D editing

ChemDraw drawing, redox, net-ionic, mapping, mechanism, and exact balancing code
is adapted from `abd19990044-commits/chemdraw` at commit
`e3c4c935d9d8f3bd263a4f3ffde5943afd0d7854` (MIT, copyright 2026
abdulsalam salih). Its full license is retained in `services/chemdraw/LICENSE`.
Telegram integration, tokens, notifications and bot state are not included.

Ketcher **3.18.0**, by EPAM Systems, Inc., is a separately licensed **Apache-2.0**
standalone editor, backed by its bundled Indigo WASM. It is downloaded only
at image build time from the official release; `fetch_ketcher_editor.py` verifies
the pinned release SHA-256 before extraction. It is served from this application's
origin and loaded only when opening the 2D editor. All editing chemistry runs on
the user's device. Full upstream LICENSE and NOTICE are distributed in
`static/licenses/ketcher/` and copied into the installed editor. Dependency
license notices in the release's JavaScript bundles are preserved. The MIT
application license does not replace these upstream licenses.

Upstream: <https://github.com/epam/ketcher/releases/tag/v3.18.0>.
