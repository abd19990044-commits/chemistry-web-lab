# ChemDraw features in ORCA Web Lab

Source repository: https://github.com/abd19990044-commits/chemdraw
Source commit: `e3c4c935d9d8f3bd263a4f3ffde5943afd0d7854` (MIT).

The source bot's pure Python drawing/chemistry modules live under
`services/chemdraw/` with relative imports and the original MIT license.
`drawing_service.py` and `drawing_routes.py` provide web adapters; no Telegram
process, notifications, credentials or per-user bot caches are included.

Transferred features:

- Publication renderer V3: 600 dpi PNG, vector SVG, smart formulas/condensed
  structural formulas for tiny organics, skeletal mode, three arrow styles,
  conditions above and below arrows, genuine transparent PNG/SVG, and on-demand
  lossless LZW TIFF with 600 dpi metadata.
- Exact rational stoichiometry solver. Already balanced equations preserve the
  entered coefficients; otherwise only supplied species are used and the result
  is independently checked for element and formal charge conservation. A bounded
  exact search is used for modest underdetermined systems; failure is reported.
- Redox balancing in acidic/basic media (only medium helpers/electrons added),
  conservative aqueous net ionic reduction, unique common-substructure mapping,
  atom-number previews and user-specified electron-pair/fishhook mechanism arrows.
- Existing editable CDXML and chemically validated RXN exports remain available.
  Immutable response snapshots ensure TIFF requests use the displayed chemistry
  and settings, even if the user later edits the form.

Web-specific repairs: bracketed positive charges use the source's charge-aware
parser, symmetry mappings must have one orientation (not merely one atom set),
isotope mismatches are rejected, a fishhook has a single open barb, self-arrows
and nonexistent atom indices are rejected, and unsupported/unbalanced ionic
requests explain their limitations. A nonblocking bounded rendering semaphore
per web worker prevents unbounded chemistry work; HTTP 429 includes Retry-After.
Input sizes, species/atom counts and raster allocations are bounded.

## Interactive editing

Ketcher 3.18.0 standalone adds atom/bond/ring tools, stereochemical bonds,
reaction arrows, text, selection/dragging/rotation, cleanup, undo/redo, and native
file dialogs. Ketcher/Indigo chemistry executes in-browser. The official archive
SHA-256 is pinned in `fetch_ketcher_editor.py`; only main application assets,
required chunks, images and notices are extracted. Files are precompressed at
image build time. Vendor binaries are not tracked in Git. No external CDN or
Indigo server is used. The iframe is loaded only when its tab is opened.

Explorer and generated reactions can be opened for editing. KET preserves the
full drawing including captions/positions; MOL/SDF store molecular chemistry,
RXN stores reaction chemistry, and images preserve artwork. Reaction transfer
back to the drawing tools transfers chemistry, not arbitrary canvas positions.
A browser-only draft is saved when storage is available. Replacing unsaved work
requires an explicit in-product choice, with a KET save option.

Molecule transfer to 3D uses the existing local Open Babel worker, adds valence
hydrogens and generates a new 3D starting geometry before opening Molecular
Studio. It rejects reaction arrows/query atoms and preserves typed bond orders.
The source WASM exposes the per-atom AddHydrogens overload, which is exercised
in the real native-engine test with drawn ethanol (C2H6O, 9 atoms, 8 bonds).

## Three-dimensional coordinate export

Molecular Studio now has distinct MOL, SDF and XYZ buttons. The former
"Export MOL" button incorrectly produced an SDF; the formats are now separate.
MOL/SDF preserve current 3D coordinates, atom order, bond orders and formal
charges. MOL V2000 rounds coordinates to four decimals and enforces its field
limits. SDF includes coordinate units and source/unverified topology metadata.
XYZ preserves numeric coordinate precision but cannot store bonds or charges.
Exports are browser-only and do not re-minimize, recenter or alter coordinates.

The application remains MIT; Ketcher and its dependencies retain their upstream
licenses, with Apache LICENSE/NOTICE available in the interface.
