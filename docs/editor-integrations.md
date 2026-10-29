# Integrated 2D drawing workbench

The standalone editor remains Ketcher 3.18.0. `editor-integration.js` provides
local PubChem 2D import, typed group definitions, image preparation and Word
clipboard representations. `structure-editor.js` coordinates the user-facing
commands and the native editor APIs.

## Passing drawings

- Molecule Explorer and Reaction Drawing keep their existing Edit in 2D buttons.
  The editor also has buttons for the most recently generated molecule/reaction.
- PubChem imports a 2D SDF directly into the browser by name or CID. Requests have
  a 20-second timeout, bounded cache, single-flight deduplication and a 5 MB cap.
  Multiple records require a specific CID; they are not silently combined.
- Replace mode protects unsaved work; Add mode uses native `addFragment` and
  retains the current drawing. Files and pasted MOL/SDF/RXN/KET/SMILES share the
  same route. Complete reaction chemistry uses RXN with editable KET captions.
- Edited chemistry can be sent to Explorer, Reaction Drawing or Molecular Studio.
  Source geometry in Molecular Studio can be copied to a separate 2D layout.
  Source bond orders are required for this reverse transfer.
- Programmatic same-page interface:

  ```javascript
  await window.OrcaDrawingEditor.open({
    structure: 'CC(=O)O', title: 'Acetic acid', mode: 'replace', layout: false
  });
  window.dispatchEvent(new CustomEvent('orca:edit-2d', {
    detail: {structure: 'CCBr.[OH-]>>CCO.[Br-]', mode: 'add'}
  }));
  ```

## Free typing and chemistry

Native keyboard lookup works when focused over empty canvas space. A COOH user
template is registered before editor startup: its underlying graph is C(=O)O,
not the pre-existing PhCOOH (benzoic acid) template. Registration preserves other
user templates. It uses the pinned native `ketcher-tmpls` extension format with
serialized KET data; the upstream loader reads `header.moleculeName` as its name.

The Write labels & groups form accepts any 1-80-character group name, including
Unicode. Common unambiguous organic groups have explicit underlying SMILES.
The user may give any custom label an explicit SMILES definition; its first atom
is the attachment point. Concrete group definitions are converted and laid out
by the local Indigo service, then stored as a collapsed SUP group with all atoms,
bond orders, charges and stereochemistry retained. Definitions must be a single
connected graph with at most 100 atoms.

Unknown keyboard lookup entries can be confirmed with Enter. They and custom
names without a definition become explicitly undefined SUP groups with a query
atom and the `ORCA_UNDEFINED` class. They are editable and exportable as artwork,
but chemical transfers require replacing them or defining their real structure.
No chemistry is guessed for arbitrary names. The native text tool is available
for unrestricted captions, conditions and notes; these are not chemical atoms.

## PNG and Word

The figure is generated from an immutable KET snapshot of the edited canvas,
including text, reaction arrows and collapsed labels. SVG is rasterized locally
at a selected 2048/4096/up-to-6000-pixel long edge, with a 24 MP allocation cap,
white/transparent background and genuine 600 dpi PNG metadata. One revision's
result is cached and reused by preview, download and Word copy.

Word copy initiates Clipboard.write in the click handler using promise-backed
PNG and HTML representations, preserving browser user activation while rendering.
The HTML embeds the same PNG and escapes user titles. A failed image copy is
reported as such; users can save PNG and insert it in Word. No false success or
text-only image fallback is used. All image/group operations run on the client;
there are no new server-side rendering routes or remote Indigo service calls.

Native extension contracts checked against upstream v3.18.0:
- `packages/ketcher-core/src/application/ketcher.ts`
- `packages/ketcher-core/src/domain/serializers/ket/fromKet/moleculeToStruct.ts`
- `packages/ketcher-core/src/domain/serializers/ket/ketSerializer.ts`
- `packages/ketcher-react/src/script/ui/state/templates/init-lib.ts`
- `packages/ketcher-react/src/script/ui/state/hotkeys.ts`

## Radicals, ions and deletion

The `Radicals / ions` panel changes the selected atom's formal charge, MDL radical
state and implicit hydrogen count through the native atom tool. This preserves
native atom identifiers, isotopes, coordinates and undo operations. Expand a
collapsed functional group before editing its individual atoms.

The default deletion mode is radical cleavage: surviving endpoints retain their
previous implicit hydrogen counts. One removed bond order creates one radical
electron; two open valences use the triplet diradical state, which the user can
change to singlet. Higher open valences retain their hydrogen count and need an
explicit electron-state decision. `Keep existing H; set electrons manually` and
the conventional `Complete valence with H` modes are also available. Loading a
new structure, undo, redo and changing a bond order do not constitute cleavage.
The correction and deletion share one undo step.

Native Open Babel `gen3D` preserves the drawn protonation. The older wrapper's
automatic pH correction is not used. MDL radical states survive worker parsing,
3D geometry generation, MOL/SDF coordinate exports and transfer back to 2D.
ORCA setup receives the net formal charge and an initial multiplicity from the
drawn atom states. Users must review coupling between multiple radical centers;
an atom drawing alone cannot determine the molecular ground-state multiplicity.
