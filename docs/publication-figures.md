# Publication figures

The Calculation Analyzer exports 3D geometry, orbital eigenvalues, UV-Vis, IR and calculated NMR figures on the user's device. Open **Publication figure export** above the results to select the final print width/height, resolution and file format. Single-column and double-column presets are starting points; the target journal's artwork instructions take precedence. A journal's quartile does not define a graphics specification or certify the underlying science.

## Formats and size

- **PNG:** lossless RGB/RGBA raster, with a physical-resolution `pHYs` chunk and UTF-8 scientific processing metadata.
- **TIFF:** uncompressed, baseline, 8-bit RGB raster with explicit X/Y resolution in dots per inch and an ASCII-safe JSON image description. Transparency is composited on white. Monochrome styling is still an RGB image, not one-bit bitmap line art.
- **SVG:** editable vector paths and text at the selected millimetre dimensions, for plots and orbital diagrams. It is not an embedded screenshot. Convert to the submission format requested by the journal if it does not accept SVG.

The 600 dpi default supports detailed color figures. Select 1200 dpi or a vector export for demanding line-art requirements. Raster exports are limited to 32 megapixels and 10,000 pixels per side to bound browser memory usage; a rejected size is never silently downsampled. SVG is independent of raster resolution. Plots use Arial/Helvetica-compatible type at 8-9 points, white backgrounds, distinct line patterns and a high-contrast palette. Legends wrap without truncating file/layer names. Increase the height for many layers. Optional panel labels and plot titles are available; manuscript captions normally remain outside the figure.

The 3D exporter renders at the requested resolution, preserves the current camera and coordinates, and temporarily uses a white background with dark atom labels. It restores the viewer even if capture fails. PNG and TIFF are available for 3D geometry.

## Scientific behavior

- Visible UV-Vis layers, displayed ranges, normalization, broadening and theoretical shifts are used. Theoretical and experimental curves retain separate labeled Y axes when their scales differ. Experimental samples are neither smoothed nor shifted. The current wavelength-domain Gaussian is an oscillator-strength-weighted intensity on an arbitrary scale; it is labeled as intensity rather than absolute molar absorptivity. Export formatting does not introduce an uncalibrated conversion to epsilon units.
- IR frequency scaling, shifts and broadening apply to calculated modes only. Wavenumbers decrease from left to right. Calculated pseudo-transmittance is identified explicitly. Integrated IR intensity export uses the original calculated band intensities as sticks, rather than assigning integrated-intensity units to broadened peak heights. Compare experimental and calculated spectra in absorbance or transmittance mode.
- Calculated NMR uses the selected nucleus and reference. Referenced chemical shifts run from high to low ppm; unreferenced shielding is labeled as shielding. Hover/selection decorations are excluded. Calculated sticks do not imply measured line shapes or spin-spin splitting.
- Orbital diagrams use finite reported HOMO/LUMO pairs, distinguish available alpha/beta channels, and scale to the actual eigenvalues. No method, basis, solvent or missing orbital value is fabricated. The signed eigenvalue difference is not presented as an optical excitation energy. CDFT data remain available in the analyzer instead of being squeezed into the orbital figure.
- Raster metadata and SVG metadata retain source names and relevant processing settings for review. The raw calculation and experimental data remain unchanged.

## Publisher references

- [ACS artwork guidance](https://researcher-resources.acs.org/publish/author_guidelines?coden=ascecg): final production size, readable lettering, RGB color and different resolution requirements for line, grayscale and color artwork.
- [Elsevier artwork types](https://www.elsevier.com/about/policies-and-standards/author/artwork-and-media-instructions/artwork-types) and [file formats](https://www.elsevier.com/about/policies-and-standards/author/artwork-and-media-instructions/artwork-overview): vector and raster formats and resolution guidance.

Visual formatting does not validate the chemical model, convergence, assignments, reference choice or interpretation. Review these against the original calculation and the journal's requirements before submission.
