# Live analyzer verification: 30 October 2026

The deployed Hugging Face interface was exercised directly with the repository's
ORCA 6.1 TD-DFT fixture and the methylene-blue SDF fixture. This completes the
browser portion that was unavailable during the previous review.

## Verified exports and molecule import

- The TD-DFT output parsed and opened the publication preview successfully.
- A downloaded SVG had physical dimensions 180 by 110 mm, ten editable text
  elements, one vector path, and no embedded raster images.
- Downloaded PNG and TIFF outputs measured 8504 by 5197 pixels at 1200 dpi.
  PNG stores the nearest integer pixels-per-metre resolution (1199.9976 dpi).
- Pillow verified that line-art TIFF uses actual one-bit pixels, one sample per
  pixel, and 1200 dpi resolution tags; it is not an RGB image labelled as bilevel.
- The Word-copy action completed and reported the full PNG dimensions. A paste
  into Microsoft Word itself was not observed, and the browser automation's
  clipboard reader did not expose the copied image payload.
- The methylene-blue structure opened in the browser with 40 source bonds and
  preserved bond orders. Its multiple bonds were visible in the molecular
  preview and in the downloaded 1200 dpi PNG. The geometry-only state did not
  display computed energy values.

## Scientific display defects found

The TD-DFT output genuinely contains a normal termination banner, but the UI
looked only for a legacy `termination_status` field instead of the parsed
`terminated_normally` boolean. It therefore displayed an unknown status.

The same output includes an input comment mentioning geometry optimization.
A broad frontend text search treated that comment as evidence of an optimized
geometry and displayed an optimization callout without a convergence certificate.

The fix carries an explicit nullable `geometry_optimization_converged` field
through the parser and reporting adapter. Only an output convergence marker
sets it true. A new optimization cycle clears an earlier certificate, and a new
calculation block starts with no inherited certificate. Input comments and
echoed input lines cannot set the flag. The UI reads these scientific status
fields directly and no longer infers optimized geometry from arbitrary text.
Normal termination remains distinct from optimization convergence and from
frequency-based stationary-point classification.

Nine regression cases cover real TD-DFT output, comments, echoed input,
convergence markers, appended optimization cycles, separate calculation blocks,
and frontend status precedence. A combined parser, thermochemistry, schema,
coordinate-import, and frontend run passed 223 tests before publication.

No live Kaggle calculation or licensed ORCA calculation was launched by this
browser verification. The background execution and encrypted credential tests
from the earlier review remain separate from these UI/export checks.
