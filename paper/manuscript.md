# Silent failure modes in the automated parsing of ORCA output files, and a validated parser that guards against them

**Salam Hasan**

*Correspondence: salamhasan199904@gmail.com*

---

## Abstract

Extracting numbers from quantum chemistry output files is usually treated as a
clerical step rather than a methodological one, and is usually done with short,
untested scripts. We argue that this is a mistake, because the characteristic
failure mode of such scripts is not a crash but a plausible wrong number. We
document a set of such failures found in a working ORCA parsing script and
quantify them against a corpus of 69 ORCA 5 and ORCA 6 output files. Two
independent defects in frontier orbital extraction together corrupted the
HOMO–LUMO gap in 16 of 62 files (25.8%), by a median of 0.49 eV and a maximum of
7.50 eV, while producing output indistinguishable from a correct result on
inspection: one pairs the highest occupied orbital energy of the converged
geometry with the lowest unoccupied energy of the initial guess, and one, in
open-shell systems whose beta channel holds no electrons, reports the lowest
alpha virtual as the LUMO instead of the far lower beta level. Three further
defects are silent in the same way: error terminations masked by a subsequent
rerun into the same file, a case-sensitive file glob that made 4 of 69 files
invisible on Linux and macOS but not on Windows, and species keyed by filename
stem that merged calculations performed at different levels of theory. We
present `orca-engine`, a dependency-free Python library that corrects these
defects and treats each as a tested invariant rather than a documented caveat.
The library parses in a single streaming pass with memory independent of file
size (0.04–0.06 MB peak on a 2.58 MB file, two orders of magnitude below
reading the file into a list), and reports alongside every reaction energy
whether the reaction is atom balanced, charge balanced, free of error
terminations, and computed at a single level of theory. After the corrections
all 62 comparable files agree with an independent reference implementation. We
also report that our *first* validation attempt was itself wrong in two
different ways, which we take to be the most transferable result in the paper.
The library, the test suite, and the validation script are openly available.

**Keywords:** computational chemistry, ORCA, output parsing, research software,
reproducibility, silent errors, thermochemistry

---

## 1. Introduction

A modern density functional theory study can generate hundreds of output files.
The numbers that reach a manuscript — reaction energies, frontier orbital gaps,
bond dissociation energies, excitation wavelengths — are almost never
transcribed by hand. They are extracted by a script, and that script is often a
few dozen lines of regular expressions written by the graduate student who ran
the calculations.

This step receives very little methodological scrutiny. Electronic structure
methods are benchmarked exhaustively; the code that reads their output is
usually not tested at all. The implicit assumption is that parsing is easy
enough to be correct by inspection.

We think that assumption is wrong, and for a specific reason. When a parser
fails, it usually does not raise an exception. It returns a number of the right
magnitude, in the right units, with the right sign — just not the number that
was asked for. A HOMO–LUMO gap of 4.29 eV where the correct value is 4.48 eV
looks entirely reasonable. It survives plotting, statistics, peer review, and
publication. Nothing about it invites suspicion.

This paper reports several such failures. They were not constructed as examples;
they were found in a script that had been used for real work, by auditing it
against a corpus of 69 ORCA output files. Section 4 quantifies each one. We then
describe `orca-engine`, a library written to correct them, and — more
importantly — to make each correction a tested invariant that cannot silently
regress.

The contribution is therefore twofold. The software is useful in its own right.
But the more transferable result is the catalogue of failure modes in Section 2,
which is not specific to our code: every one of them arises from an idiom that is
common in quantum chemistry parsing scripts.

A third result emerged unplanned, and we report it prominently because it makes
the point better than the rest of the paper does. Our first validation attempt
was wrong twice over. Its reference implementation initially mishandled
open-shell systems, producing a false disagreement on 36 files; and after that
was corrected, the reference still shared a guard clause with the code it was
supposed to be checking, which concealed a 7.50 eV error until a genuinely
independent third implementation was written. Section 4.2 gives the details.
Verification of parsing code is not merely neglected — it is harder to do
correctly than it looks.

### 1.1 Related work

The established general-purpose parser is `cclib` [3], which supports many
electronic structure packages behind a uniform data model and is the right
choice when program independence is the priority. The Atomic Simulation
Environment [8] and similar frameworks include calculator interfaces that read
output files as part of a larger simulation workflow. Alongside these, a large amount of parsing is done by scripts that
are never released, which is precisely the population this paper is about.

`orca-engine` is narrower than `cclib` by design. It targets ORCA only, which
lets it record program version, basis set, solvation model, charge,
multiplicity, temperature and pressure for every job and use that metadata to
check whether a requested arithmetic operation is physically meaningful. It is
intended to complement general-purpose parsers, not to replace them.

---

## 2. Failure modes

ORCA [1,2] is the program throughout; the idioms below, however, are not
specific to it. Each defect is stated as the idiom that causes it, so that readers can check
their own scripts, rather than as a bug report about ours.

### 2.1 Frontier orbitals drawn from different geometries

**The idiom.** Scan the file for orbital table rows. Keep the energy of the last
row with non-zero occupation as the HOMO, and the first row with zero occupation
after it as the LUMO.

**Why it fails.** A geometry optimization prints more than one `ORBITAL
ENERGIES` section — typically two, one after the initial SCF and one after the
SCF at the converged structure, independent of how many optimization cycles were
run. (In our corpus, `napc11br.out` has 44 cycles and 2 orbital sections;
`napbrbr.out` has 40 and 2.) The natural implementation keeps overwriting the
HOMO, correctly ending at the last section, but assigns the LUMO only once,
because the guard that fills it is typically written as "if a HOMO has been seen
and no LUMO has been assigned yet". After the first section that condition is
never true again. The reported HOMO belongs to the converged geometry; the
reported LUMO belongs to the initial one.

The result is a HOMO–LUMO gap that mixes two different structures. It is not
noise: it is a systematic bias whose size depends on how far the geometry moved
during optimization.

**Diagnosis.** Whether the result is wrong depends on the file. A single point
calculation prints one orbital section and the answer is correct. An
optimization is wrong by an amount nobody can estimate without re-reading the
file. This is the worst situation for a silent error, because it is inconsistent
across a dataset: some rows of a results table are right and some are wrong,
with no visible marker distinguishing them.

**The fix.** Two independent mechanisms. Encountering a new `ORBITAL ENERGIES`
header clears the stored frontier levels, so nothing survives from a superseded
section. Independently, encountering a further occupied orbital invalidates any
LUMO already assigned, which also handles tables that are not strictly ordered.
Either alone is sufficient for well-formed output; both are kept because they
fail differently. The reset additionally ensures that a run which crashes
partway through a later section reports *no* frontier data rather than stale
data from an abandoned geometry — reporting nothing is correct there, and
reporting the earlier values is not.

### 2.2 A LUMO that requires a HOMO in the same spin channel

**The idiom.** Within each spin channel, record the first unoccupied orbital
encountered *after* an occupied one.

**Why it fails.** In an unrestricted calculation of a system with one electron —
a hydrogen atom, or any doublet radical treated with a small active space — the
beta channel contains no occupied orbital at all. The guard never fires, so no
beta LUMO is recorded. A fallback that takes the minimum LUMO across channels
then sees only the alpha value and returns the lowest alpha *virtual*, which lies
far above the lowest beta orbital. Physically, the lowest unoccupied orbital of
such a system *is* the lowest beta orbital.

**Diagnosis.** This is the largest error we found, and it is instructive that we
found it last. It affects 4 of 62 corpus files with gap errors of 4.79 to
7.50 eV — five to seven times the maximum error of the defect in Section 2.1,
which was the one we set out to fix. It survived our first two validation
attempts because the reference implementation contained the identical guard
(Section 4.2).

**The fix.** A zero-occupancy orbital records a LUMO whether or not a HOMO has
been seen in that channel. Encountering an occupied orbital still invalidates
any LUMO already assigned, so ordering is still handled.

### 2.3 Atomic units reported as Angstrom

**The idiom.** Enter a coordinate-reading state on a line matching
`CARTESIAN COORDINATES`, and read the last three numeric columns of each
subsequent row.

**Why it fails.** ORCA prints every geometry twice, first as
`CARTESIAN COORDINATES (ANGSTROEM)` and immediately after as
`CARTESIAN COORDINATES (A.U.)`. Both match the pattern. The second block
overwrites the first, and its values are in Bohr — a factor of 1.8897 larger.
The atomic-unit block also carries five leading columns rather than one, so
column-position heuristics break on it as well.

**Diagnosis.** Whether this manifests depends on incidental details of the state
machine. In the script we audited, the header line of the A.U. block was
consumed by the transition that ended the Angstrom block, so the A.U. rows were
never read and the coordinates happened to be correct. That is not robustness;
it is luck, and it would have been destroyed by any refactor of the state exit
logic.

**The fix.** The two headers are matched by distinct patterns, the unit is
stored alongside the coordinates in an explicit `coords_unit` field, and an
Angstrom block already read for the current step is not replaced by its
atomic-unit copy. A line that terminates a section is re-dispatched rather than
consumed, so correctness no longer depends on which line happens to end a state.

### 2.4 Error terminations masked by a rerun

**The idiom.** Set a success flag when `ORCA TERMINATED NORMALLY` is seen, and
an error flag when an error banner is seen. The last one encountered wins.

**Why it fails.** Rerunning a calculation with output appended to the existing
file — or a compound job in which one module fails and later ones succeed —
leaves a normal-termination banner at the end of a file that also contains
failures. Last-banner-wins reports the run as clean.

In our corpus, 7 of 69 files contained at least one error termination, including
SCF failures, integral-generation failures and an input error. Two of those
seven end with a normal-termination banner, so a last-banner check classifies
them as successful; the other five happen to end with the error banner and are
classified correctly. The masked fraction is therefore 2 of 69 (2.9%), not the
full 7.

**Diagnosis.** This defect is worse than a wrong number, because it silently
readmits calculations that were excluded on purpose. A researcher who filters on
"terminated normally" believes they have applied a quality control step that
they have not. That only two corpus files are affected reflects how those
particular runs happened to end, not any property of the check; a rerun appended
to a failed file produces the masked pattern by construction.

**A subtler recurrence.** Our own first fix reintroduced the defect one level
up. Error banners were retained per job block, but a compound job or an appended
rerun is split into several blocks, so code inspecting only the last block — the
library's own validation script, as originally written — again saw a clean
calculation. The flag is now aggregated across every job of a species.

**A related trap.** The same script's error pattern included a catch-all
alternative matching any line of the form `Error: ...`. ORCA prints such lines
during successful runs, so this produced false positives in the opposite
direction. Broad patterns are unsafe in both directions.

**The fix.** Error banners are matched only on ORCA's genuine fatal-termination
text, and *all* of them are retained. A separate `had_error_termination` flag
stays true even when a later rerun succeeded, so a partially failed calculation
can never be mistaken for a clean one.

### 2.5 File discovery that depends on the operating system

**The idiom.** Collect input files with a glob such as `rglob("*.out")`.

**Why it fails.** ORCA writes `.out` or `.OUT` depending on how it is invoked,
and shell globbing is case-sensitive on Linux and macOS but not on Windows. The
same directory therefore yields different file sets on different machines.

In our corpus, 4 of 69 files (5.8%) carried an uppercase suffix. On Linux they
were silently absent; on Windows they were included. No error is raised in either
case: the analysis simply proceeds over a smaller dataset.

**Diagnosis.** This defeats reproducibility in a way that is invisible to both
the original author and to a reviewer re-running the analysis. Two people can
run identical code on identical data and obtain different results.

**The fix.** Suffix matching is case-insensitive and results are de-duplicated
by resolved path, so the file set is identical on every platform and no file is
counted twice on case-insensitive filesystems.

### 2.6 Species identity taken from the filename

**The idiom.** Key each species by its output file stem, so that reaction
equations can be written in terms of filenames.

**Why it fails.** File stems are not unique across directories, and they carry
no information about the level of theory. Our corpus contains `nap.out` in both
an ORCA 5 and an ORCA 6.1 directory. These were merged into a single species
whose reference energy came from whichever file was read last — despite differing
in program version (5.0.2 vs 6.1.0), basis set (def2-TZVPP vs def2-TZVP) and
solvation (gas phase vs SMD).

Two of the corpus's species identifiers collided in this way, each spanning two
distinct levels of theory.

**Diagnosis.** Nothing in the arithmetic prevents subtracting a def2-SVP energy
from a def2-TZVP one. The difference of two total energies computed with
different basis sets is not a reaction energy; it is dominated by the basis set
difference and can be wrong by tens of kcal/mol. The output is a plausible
number in the right units.

**A second-order effect.** The original loader consumed parallel results with
`concurrent.futures.as_completed`, so when stems collided, which calculation won
depended on thread scheduling. In principle the same command could give
different answers on different runs.

**The fix.** Collisions are reported, and can be made fatal with
`--strict-duplicates`. Every job carries its level of theory as a
`(version, basis, solvation)` key, and any reaction spanning more than one key is
flagged. Parallel results are collected with `Executor.map`, which preserves
input order, so reports are byte-identical across runs.

### 2.7 A note on units in labels

A sixth issue is not a silent numerical error but is worth recording. ORCA
prints two lines, `Final entropy term` and `Total entropy correction`, whose
values are `+T·S` and `−T·S` in Hartree. Both are energies, not entropies, and
they differ in sign. A single field named `entropy_value` populated by whichever
line appeared last inherits a sign that depends on ORCA's print order.
`orca-engine` stores them as two fields, `entropy_term_eh` and
`entropy_correction_eh`, both documented as energies.

Relatedly, the original entropy pattern matched the bare word "entropy" followed
by any number. It matched the explanatory sentence "out the resulting rotational
entropy values for sn=1,12:" and captured `1.0`. Patterns in `orca-engine` are
anchored on ORCA's exact printed labels.

---

## 3. Implementation

### 3.1 Streaming architecture

The parser consumes an `Iterator[str]` and never materializes its input. This is
not only a memory optimization; it means file handles, ZIP members, decompressed
streams and network sources are all valid inputs without special cases, and the
test suite can drive the parser from a list of strings with no files on disk.

Parsing is a state machine over four states — searching, coordinates, orbitals,
excited states — with two dispatch tables. Scalar observables that may appear
anywhere are matched on every line; section transitions are matched only in the
searching state. Lines that terminate a section are re-dispatched rather than
consumed, which is what makes Section 2.3's incidental correctness unnecessary.

### 3.2 Consistency checking

Every reaction and bond dissociation energy result carries a report of four
checks:

- **Atom balance.** Element counts on both sides, weighted by stoichiometric
  coefficients, taken from the parsed geometries.
- **Charge balance.** Total charge on both sides, weighted likewise.
- **Level of theory.** All species must share `(ORCA version, basis set,
  solvation model)`.
- **Temperature.** Gibbs energies must come from a common temperature.

Two design decisions matter here. First, a check that cannot be evaluated — atom
balance when no geometry was printed, for instance — reports `None`, never
`True`. Silence and success are different states and are not conflated. Second,
a failed check annotates the result but does not suppress the number, because a
user debugging an equation needs to see both. The `--strict-consistency` flag
converts failures into a non-zero exit code for pipeline use.

### 3.3 Testing

The suite comprises 70 tests. Regression tests carry docstrings naming the
defect they pin, including its magnitude, so that a later reader understands why
the test exists before deciding to simplify it. `mypy --strict` and `ruff` pass
with no findings; continuous integration covers Python 3.10–3.13 on Linux,
macOS and Windows.

Because these defects are silent, we verified that the tests detect them rather
than merely passing. Eighteen mutations were applied to the fixed code, each
restoring one original defect or removing one guard; all eighteen are caught
(Table 2).

Two mutations initially survived, and both were informative. The first
frontier-orbital test did not catch the removal of the orbital-section reset,
because the second, redundant mechanism masked it; this led to an additional
fixture covering a run that crashes partway through a later orbital section.
The first spin-orbit test did not catch the removal of its guard either — for a
subtler reason given in Section 5.

---

## 4. Validation

### 4.1 Corpus

The validation corpus is 69 ORCA output files totalling 24.8 MB. Fifty-two carry
an ORCA 5.0.2 banner and 14 an ORCA 6.1.0 banner; two are output fragments with
no version banner, and one is a zero-byte file. Files range to 2.58 MB with a
median of 0.17 MB. The corpus comprises geometry optimizations, frequency jobs,
single points, TDDFT absorption spectra, restricted and unrestricted
calculations, gas-phase and SMD-solvated runs, and several runs that terminated
with errors. Calculations use the B3LYP and BP86 functionals with D3(BJ) or D4
dispersion corrections [5,6] and def2 basis sets [4], with SMD solvation [7]
where applicable.

We note plainly that this is one research group's working set — naphthalene
derivatives and related species — and not a designed benchmark. Its value is
that it is representative of real practice, including its untidiness: duplicated
filenames, mixed program versions, mixed basis sets, a zero-byte file, and
reruns appended to existing files. It is not a random sample of ORCA usage, and
the rates in Table 1 should be read as evidence that these failures occur in
practice, not as population estimates.

### 4.2 Reference implementation, and two failed attempts at it

Frontier orbital energies were extracted a second time by a separate
implementation. Getting that implementation right took three attempts, and the
sequence is the most useful thing we can report.

**Attempt one** did not separate spin channels. It concatenated the alpha and
beta orbital tables of open-shell species, and consequently disagreed with the
library on 36 files. Inspection showed the *reference* was at fault, not the
library. Had we trusted it, we would have "fixed" correct code.

**Attempt two** separated the channels and agreed with the library on all 62
comparable files. We initially reported that as validation. It was not. The
reference had been written by adapting the library's own logic: it shared the
float pattern, the table regexes, and — decisively — the guard clause "record a
LUMO only once a HOMO has been seen". Both implementations therefore contained
the defect of Section 2.2, and agreed with each other while both were wrong by
up to 7.50 eV. An agreement between two implementations that share a premise
tests the premise not at all.

**Attempt three**, which is what the repository ships, uses a different
algorithm rather than a re-implementation of the same one. It collects every
`(occupation, energy)` pair in the final orbital section and takes set extrema:
the HOMO is the maximum energy among occupied orbitals, the LUMO the minimum
among unoccupied ones. It depends on no ordering assumption, no assignment
guard, and no pattern defined in the library. It is slow and holds the file in
memory, which is why it is a validation instrument and not the production path.
It is what exposed the defect in Section 2.2.

### 4.3 Results

Measured against the third reference implementation, the original parser
returned an incorrect HOMO–LUMO gap for **16 of 62** comparable files (25.8%),
with a median absolute error of 0.49 eV and a maximum of 7.50 eV. The
distribution is bimodal: 12 files carry errors of 0.06–1.01 eV from the
mixed-geometry defect of Section 2.1, and 4 carry errors of 4.79–7.50 eV from
the empty-spin-channel defect of Section 2.2.

**Table 1. Defects quantified against the corpus.**

| Failure mode | Affected | Magnitude |
| --- | --- | --- |
| Frontier orbitals from mixed geometries (§2.1) | 12 / 62 (19.4%) | 0.06–1.01 eV gap error |
| LUMO missing from an empty spin channel (§2.2) | 4 / 62 (6.5%) | 4.79–7.50 eV gap error |
| Error termination masked by a later normal banner (§2.4) | 2 / 69 (2.9%) | run misclassified as clean |
| Files invisible on Linux/macOS (§2.5) | 4 / 69 (5.8%) | files silently absent |
| Species merged across levels of theory (§2.6) | 2 species | def2-TZVPP gas vs def2-TZVP SMD |
| Entropy sign fixed by print order (§2.7) | all thermochemistry jobs | `±T·S` in Hartree |

Files are counted out of 62 for the frontier orbital comparison because 7 of the
69 print no orbital table.

After the corrections, all 62 comparable files agree with the reference to
within 10⁻⁴ eV. Three repeated runs over the corpus with 8 workers produced
byte-identical JSON reports.

**Table 2. Mutation testing. Each mutation restores one pre-1.0 defect or
removes one guard; all are detected.**

| Mutation | Tests failed |
| --- | --- |
| Remove orbital-section reset | 1 |
| Restore permissive LUMO guard (cross-section) | 1 |
| Require a HOMO before recording a LUMO (§2.2) | 1 |
| Restore case-sensitive file discovery | 1 |
| Discard error-message history | 4 |
| Report only the last job's error flag | 1 |
| Remove the atom-balance check | 6 |
| Remove the termination check | 1 |
| Remove the temperature check | 1 |
| Inspect one job per species for level of theory | 1 |
| Prefer the atomic-unit coordinate block | 2 |
| Hard-code the coordinate unit to Angstrom | 1 |
| Swallow the line that terminates a section | 1 |
| Restore the loose entropy pattern | 3 |
| Restore the catch-all `Error:` pattern | 1 |
| Match the SOC spectrum header as a substring | 2 |
| Leave ghost centres unmatched | 2 |
| Collect parallel results with `as_completed` | 1 |

### 4.4 Performance

On the largest file in the corpus (2.58 MB), streaming parsing peaked at
0.04–0.06 MB of traced allocation across repeated runs, against 4.98 MB for the
same parse driven from a `readlines()` list: a factor of 90 to 130, and
independent of file size rather than proportional to it. Parsing the entire
69-file corpus serially took 9.1 s (2.7 MB/s) with a peak of 0.27 MB. Throughput
is dominated by regular expression evaluation, not I/O; the constant memory
profile is what makes the design worthwhile on the multi-gigabyte outputs that
large TDDFT and coupled-cluster jobs produce.

---

## 5. Discussion

The defects in Section 2 share a structure. Each arises from an idiom that is
correct for the simple case and wrong for a case that is common but not the
first one a developer thinks of: one orbital section versus several, one
coordinate block versus two, one termination banner versus several, one
filesystem convention versus two, one level of theory versus several, a spin
channel with electrons versus one without.

That structure has a practical consequence. Testing a parser on a file whose
answer you already know does not detect these defects, because the file you pick
to test with is almost always the simple case. Detecting them requires either an
independent second extraction or fixtures constructed specifically around the
awkward case. We would suggest that either is a reasonable minimum for any
script whose output reaches a manuscript.

But Section 4.2 shows that "independent second extraction" is easier to say than
to do. Our second attempt agreed with the library on every file while both were
wrong by up to 7.50 eV, because the reference had been written by adapting the
code it was meant to check. Agreement between two implementations that share a
premise is not evidence about that premise. A useful reference implementation
should differ *algorithmically*, not merely in code: ours takes set extrema
where the library walks a state machine, so no assignment guard is common to
both.

A second methodological point comes from mutation testing. Two of our eighteen
mutations initially survived, and in both cases the reason was the same:
correctness was over-determined. The orbital reset was masked by a second,
redundant guard, and the spin-orbit spectrum guard was masked by unrelated
column-header lines that happened to eject the parser from the excited-state
section before the wrong data could be read. In each case the code was right,
the test passed, and the test was measuring something other than what it
claimed. This is the same phenomenon as the coordinate-unit handling in
Section 2.3, which was correct only because a section header happened to be
consumed by a state transition. Incidental correctness is not a defect today; it
is a defect the next time somebody refactors.

We also want to be careful about what this paper does not show. We have not
audited anyone else's code, and we make no claim about how common these specific
defects are in the field. What we can say is that all of them were present
simultaneously in one working script, that none produced an error message, and
that most would have propagated into published numbers without leaving any trace
in the output.

The wider issue is that parsing sits outside the boundary of what computational
chemistry treats as methodology. Basis sets, functionals and convergence
criteria are reported in the methods section; the code that reads the resulting
files is not reported at all. Given that this code determines every number that
reaches the reader, that boundary seems misplaced.

---

## 6. Limitations

- **Coverage.** `orca-engine` extracts the quantities listed in Section 3, not
  the full contents of an ORCA output. Population analyses, NMR and EPR
  parameters, coupled-cluster component energies, and vibrational frequencies
  are not parsed. Contributions are welcome.
- **Job types not modelled.** Relaxed surface scans, NEB and IRC calculations
  are parsed as ordinary jobs, so the retained energy and geometry are those of
  the last printed point rather than of a chemically meaningful structure. The
  library does not detect an optimization that exhausted its iteration limit, so
  such a run is reported as terminated normally with a non-stationary geometry.
  Users of these job types should not rely on the single-value fields.
- **Counterpoise geometries.** Ghost centres are recognised and excluded from
  the stoichiometry, but counterpoise-corrected interaction energies are not
  computed; the corrected and uncorrected energies are not distinguished.
- **Program scope.** ORCA only. This is deliberate — the consistency checking
  depends on ORCA-specific metadata — but `cclib` [3] remains the right choice
  when program independence matters.
- **Corpus.** As stated in Section 4.1, the validation corpus is one group's
  working set, weighted toward naphthalene derivatives, and includes one
  zero-byte file and two fragments without version banners. The parser has not
  been exercised against relativistic, multireference, or periodic calculations,
  and the corpus contains no spin-orbit-corrected spectra, so the guard in
  Section 3 for those is covered by a constructed fixture rather than by real
  output.
- **Version coverage.** Validated on ORCA 5.0.2 and 6.1.0. Other 5.x and 6.x
  releases are expected to work but have not been tested; ORCA 4 and earlier are
  not supported.
- **Consistency checks are necessary, not sufficient.** Passing every check does
  not make a reaction energy correct. It rules out specific classes of error.
  Functional choice, basis set superposition error, conformational sampling and
  the adequacy of the harmonic approximation are all outside the library's
  knowledge.
- **Atom balance requires geometries.** A calculation that prints no coordinate
  block cannot be balance-checked, and the check reports `None` rather than
  passing.

---

## 7. Availability

- **Source code:** https://github.com/salamhasan/orca-engine, MIT licensed.
- **Installation:** `pip install orca-engine`. No runtime dependencies beyond
  the Python standard library; requires Python 3.10 or later.
- **Validation corpus:** to be archived on Zenodo before submission; the DOI is
  recorded in `data/README.md` in the repository.
- **Reproducing Section 4:** `python scripts/validate_corpus.py <corpus>`
  regenerates the post-fix frontier-orbital comparison, the error-termination
  counts including which are masked, the species-identifier collisions, and the
  file-discovery count. The pre-fix defect counts and magnitudes in Table 1
  require re-running the same script against the tagged `v0.1.0` parser, and
  Table 2 is produced by the mutation script in the same directory.

---

## Acknowledgements

*To be completed before submission: funding sources, computational resources,
and colleagues who commented on the manuscript.*

## Conflicts of interest

The author declares no conflict of interest.

## Data availability

The software and analysis scripts are openly available as described in
Section 7. The validation corpus will be deposited on Zenodo and the DOI added
here before submission.

---

## References

1. Neese, F. Software update: The ORCA program system — Version 5.0. *WIREs
   Comput. Mol. Sci.* **2022**, *12*, e1606. DOI: 10.1002/wcms.1606

2. Neese, F. Software update: The ORCA program system — Version 6.0. *WIREs
   Comput. Mol. Sci.* **2025**, *15*, e70019. DOI: 10.1002/wcms.70019

3. O'Boyle, N. M.; Tenderholt, A. L.; Langner, K. M. cclib: A library for
   package-independent computational chemistry algorithms. *J. Comput. Chem.*
   **2008**, *29*, 839–845. DOI: 10.1002/jcc.20823

4. Weigend, F.; Ahlrichs, R. Balanced basis sets of split valence, triple zeta
   valence and quadruple zeta valence quality for H to Rn: Design and assessment
   of accuracy. *Phys. Chem. Chem. Phys.* **2005**, *7*, 3297–3305.
   DOI: 10.1039/B508541A

5. Grimme, S.; Antony, J.; Ehrlich, S.; Krieg, H. A consistent and accurate
   ab initio parametrization of density functional dispersion correction
   (DFT-D) for the 94 elements H-Pu. *J. Chem. Phys.* **2010**, *132*, 154104.
   DOI: 10.1063/1.3382344

6. Grimme, S.; Ehrlich, S.; Goerigk, L. Effect of the damping function in
   dispersion corrected density functional theory. *J. Comput. Chem.* **2011**,
   *32*, 1456–1465. DOI: 10.1002/jcc.21759

7. Marenich, A. V.; Cramer, C. J.; Truhlar, D. G. Universal solvation model
   based on solute electron density and on a continuum model of the solvent
   defined by the bulk dielectric constant and atomic surface tensions. *J.
   Phys. Chem. B* **2009**, *113*, 6378–6396. DOI: 10.1021/jp810292n

8. Larsen, A. H.; et al. The atomic simulation environment — a Python library
   for working with atoms. *J. Phys.: Condens. Matter* **2017**, *29*, 273002.
   DOI: 10.1088/1361-648X/aa680e

9. Wilkinson, M. D.; et al. The FAIR Guiding Principles for scientific data
   management and stewardship. *Sci. Data* **2016**, *3*, 160018.
   DOI: 10.1038/sdata.2016.18 — cited in Section 5 for the principle that data
   and the code that reads it should be equally available.

*Before submission: confirm every DOI, page range and author list against the
publisher record, and check the target journal's reference style.*
