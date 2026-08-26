"""Web adapter and API bridge for orca_engine.

Provides serialization, Gaussian spectral convolution with wavelength shift,
XYZ coordinate generation, and a lightweight standalone HTTP server for the
interactive 3D & UV-Vis GUI.
"""

from __future__ import annotations

import http.server
import io
import json
import math
import socketserver
import threading
import urllib.parse
import webbrowser
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from orca_engine.experimental_spectrum import (
    ExperimentalSpectrum,
    ExperimentalSpectrumError,
    build_multi_spectrum_overlay,
    convolute_theoretical_spectrum,
    detect_peaks,
    inspect_experimental_file,
    parse_experimental_excel,
    parse_experimental_multi_series_excel,
    parse_experimental_multi_series_text,
    parse_experimental_text,
)
from orca_engine.io import load_directory_parallel
from orca_engine.models import JobData, MoleculeData
from orca_engine.parser import OrcaParser
from orca_engine.reporting import job_to_dict, reaction_result_to_dict
from orca_engine.thermochemistry import ThermochemistryEngine

WEB_DIR = Path(__file__).parent.parent / "web"


def generate_xyz_string(job: JobData, comment: str = "") -> str:
    """Generate a standard XYZ format string from a JobData geometry.

    Args:
        job: Parsed ORCA job.
        comment: Optional single-line comment for the XYZ header.

    Returns:
        Formatted XYZ string with atom count, header, and element coordinates.
    """

    if not job.elements or not job.coords:
        return ""

    lines = [str(len(job.elements)), comment or f"ORCA calculation - {len(job.elements)} atoms"]
    for element, (x, y, z) in zip(job.elements, job.coords, strict=False):
        lines.append(f"{element:<3} {x:18.12f} {y:18.12f} {z:18.12f}")
    return "\n".join(lines)


def convolute_uvvis_spectrum(
    tddft_cm: Sequence[float],
    tddft_fosc: Sequence[float],
    wavelength_shift_nm: float = 0.0,
    sigma_nm: float = 20.0,
    start_nm: float = 180.0,
    end_nm: float = 800.0,
    step_nm: float = 1.0,
) -> list[dict[str, float]]:
    """Convolute TD-DFT stick transitions with a Gaussian line shape and wavelength offset.

    The continuous absorption curve is generated according to Gaussian line broadening:
    epsilon(lambda) = sum_i (f_i / (sigma * sqrt(2*pi)))
                      * exp(-(lambda - lambda_eff)^2 / (2*sigma^2))
    where lambda_eff = (10^7 / nu_i) + delta_lambda.

    Args:
        tddft_cm: Transition energies in cm^-1.
        tddft_fosc: Oscillator strengths for each transition.
        wavelength_shift_nm: Wavelength axis offset (delta lambda) to align with experimental data.
        sigma_nm: Standard deviation for Gaussian broadening in nm.
        start_nm: Beginning wavelength of the spectrum grid.
        end_nm: Ending wavelength of the spectrum grid.
        step_nm: Wavelength grid step resolution in nm.

    Returns:
        List of dictionaries with 'wavelength_nm' and 'intensity'.
    """

    if not tddft_cm or not tddft_fosc or sigma_nm <= 0:
        return []

    # Filter and convert valid positive energies to effective wavelengths in nm
    transitions: list[tuple[float, float]] = []
    for cm, fosc in zip(tddft_cm, tddft_fosc, strict=False):
        if cm > 0 and fosc >= 0:
            lambda_nm = (1.0e7 / cm) + wavelength_shift_nm
            transitions.append((lambda_nm, fosc))

    if not transitions:
        return []

    # Generate the continuous grid
    two_sigma_sq = 2.0 * sigma_nm * sigma_nm
    norm_factor = 1.0 / (sigma_nm * math.sqrt(2.0 * math.pi))

    curve: list[dict[str, float]] = []
    current_nm = start_nm
    while current_nm <= end_nm:
        intensity = 0.0
        for lambda_nm, fosc in transitions:
            diff = current_nm - lambda_nm
            if abs(diff) <= 5.0 * sigma_nm:
                intensity += fosc * math.exp(-(diff * diff) / two_sigma_sq)

        curve.append({
            "wavelength_nm": round(current_nm, 2),
            "intensity": round(intensity * norm_factor * 1000.0, 6),
        })
        current_nm += step_nm

    return curve


def convolute_ir_spectrum(
    frequencies_cm: Sequence[float],
    intensities_km_mol: Sequence[float] | None = None,
    scaling_factor: float = 1.0,
    fwhm_cm: float = 15.0,
    start_cm: float = 400.0,
    end_cm: float = 4000.0,
    step_cm: float = 2.0,
) -> list[dict[str, float]]:
    """Convolute vibrational IR transitions with Lorentzian line broadening.

    Args:
        frequencies_cm: Harmonic vibrational frequencies in cm^-1.
        intensities_km_mol: IR intensities (T^2) in km/mol.
        scaling_factor: Harmonic vibrational frequency scaling factor.
        fwhm_cm: Full Width at Half Maximum line width in cm^-1.
        start_cm: Beginning wavenumber of the IR spectrum grid.
        end_cm: Ending wavenumber of the IR spectrum grid.
        step_cm: Wavenumber grid step resolution in cm^-1.

    Returns:
        List of dictionaries with 'wavenumber_cm', 'absorbance', 'absorbance_norm', and 'transmittance_pct'.
    """
    if not frequencies_cm or fwhm_cm <= 0:
        return []

    if intensities_km_mol and len(intensities_km_mol) == len(frequencies_cm):
        intensities = list(intensities_km_mol)
    else:
        intensities = [10.0] * len(frequencies_cm)

    modes: list[tuple[float, float]] = []
    for freq, intensity in zip(frequencies_cm, intensities, strict=False):
        if freq > 0:
            modes.append((freq * scaling_factor, max(0.0, float(intensity))))

    if not modes:
        return []

    gamma_half = fwhm_cm / 2.0
    gamma_half_sq = gamma_half * gamma_half

    points: list[tuple[float, float]] = []
    max_abs = 0.0
    current_cm = start_cm
    while current_cm <= end_cm:
        abs_val = 0.0
        for pos_cm, int_val in modes:
            diff = current_cm - pos_cm
            if abs(diff) <= 15.0 * fwhm_cm:
                abs_val += (int_val / math.pi) * (gamma_half / (diff * diff + gamma_half_sq))
        if abs_val > max_abs:
            max_abs = abs_val
        points.append((current_cm, abs_val))
        current_cm += step_cm

    curve: list[dict[str, float]] = []
    for wn, a in points:
        a_norm = (a / max_abs) if max_abs > 0 else 0.0
        t_pct = 100.0 * (10.0 ** (-a_norm))
        curve.append({
            "wavenumber_cm": round(wn, 2),
            "absorbance": round(a, 4),
            "absorbance_norm": round(a_norm, 4),
            "transmittance_pct": round(t_pct, 2),
        })

    return curve


def job_to_web_json(
    job: JobData, molecule_name: str, job_index: int = 1
) -> dict[str, object]:
    """Convert a JobData record into a rich web-ready representation.

    Args:
        job: Parsed ORCA job.
        molecule_name: Name of the molecule.
        job_index: 1-based index of the job.

    Returns:
        JSON-serializable dictionary with comprehensive calculation data.
    """

    base_dict = job_to_dict(job_index, job)
    xyz_str = generate_xyz_string(job, comment=f"{molecule_name} - Job {job_index}")

    # Build detailed excited states list
    transitions: list[dict[str, float | int]] = []
    for idx, (cm, fosc) in enumerate(zip(job.tddft_cm, job.tddft_fosc, strict=False), 1):
        if cm > 0:
            nm = 1.0e7 / cm
            ev = cm / 8065.544
            transitions.append({
                "state": idx,
                "energy_cm": round(cm, 2),
                "energy_ev": round(ev, 4),
                "wavelength_nm": round(nm, 2),
                "oscillator_strength": round(fosc, 6),
            })

    spectrum = convolute_uvvis_spectrum(job.tddft_cm, job.tddft_fosc)

    # Build IR spectrum dataset
    ir_freqs = job.ir_frequencies_cm or job.vibrational_frequencies_cm
    ir_conv = convolute_ir_spectrum(ir_freqs, job.ir_intensities_km_mol)

    web_data: dict[str, object] = {
        **base_dict,
        "molecule": molecule_name,
        "xyz": xyz_str,
        "elements": job.elements,
        "coords": job.coords,
        "hirshfeld_charges": job.hirshfeld_charges,
        "mulliken_charges": job.mulliken_charges,
        "mayer_charges": job.mayer_charges,
        "mayer_valences": job.mayer_valences,
        "loewdin_charges": job.loewdin_charges,
        "atomic_charges": job.atomic_charges,
        "vibrational_frequencies": job.vibrational_frequencies_cm,
        "vibrational_frequencies_cm": job.vibrational_frequencies_cm,
        "ir_frequencies_cm": job.ir_frequencies_cm,
        "ir_intensities_km_mol": job.ir_intensities_km_mol,
        "ir_spectrum": job.ir_spectrum,
        "convoluted_ir_spectrum": ir_conv,
        "transitions": transitions,
        "uvvis_spectrum": spectrum,
        "nmr": job.nmr_result.to_dict() if getattr(job, "nmr_result", None) else None,
    }


    return web_data


def parse_nmr_output(
    text: str,
    reference_1h: float | None = None,
    reference_13c: float | None = None,
    group_close_peaks: bool = False,
    group_tolerance_ppm: float = 0.02,
) -> dict[str, Any]:
    """Parse raw ORCA output and return formatted NMR spectra dictionary."""
    from orca_engine.nmr import NMRNucleus, NMRReference, OrcaNMRParser, build_nmr_spectrum

    meta_dict: dict[str, Any] = {}
    coords = None
    elements = None
    try:
        jobs = OrcaParser(text).parse()
        if jobs:
            j = jobs[-1]
            meta_dict = {
                "method": j.metadata.method,
                "basis_set": j.metadata.basis_set,
                "solvent": j.metadata.solvent,
                "orca_version": j.metadata.orca_version,
            }
            coords = j.coords
            elements = j.elements
    except Exception:
        pass

    parser = OrcaNMRParser()
    res = parser.parse_text(text, coordinates=coords, elements=elements, metadata=meta_dict)

    # Apply custom or requested references if supplied
    ref_h1 = NMRReference(NMRNucleus.H1, reference_1h, "Custom", reference_source="User Configured") if reference_1h is not None else None
    ref_c13 = NMRReference(NMRNucleus.C13, reference_13c, "Custom", reference_source="User Configured") if reference_13c is not None else None

    if ref_h1:
        res.h1_spectrum = build_nmr_spectrum(
            res.atoms,
            nucleus=NMRNucleus.H1,
            reference=ref_h1,
            group_close_peaks=group_close_peaks,
            group_tolerance_ppm=group_tolerance_ppm,
            method=res.provenance.get("method", "Unknown"),
            basis_set=res.provenance.get("basis_set", "Unknown"),
            solvent=res.provenance.get("solvent", "None"),
            orca_version=res.provenance.get("orca_version", "Unknown"),
        )
        res.references["1H"] = ref_h1

    if ref_c13:
        res.c13_spectrum = build_nmr_spectrum(
            res.atoms,
            nucleus=NMRNucleus.C13,
            reference=ref_c13,
            group_close_peaks=group_close_peaks,
            group_tolerance_ppm=group_tolerance_ppm,
            method=res.provenance.get("method", "Unknown"),
            basis_set=res.provenance.get("basis_set", "Unknown"),
            solvent=res.provenance.get("solvent", "None"),
            orca_version=res.provenance.get("orca_version", "Unknown"),
        )
        res.references["13C"] = ref_c13

    return res.to_dict()



def molecule_to_web_json(molecule: MoleculeData) -> dict[str, object]:
    """Convert a MoleculeData container to a web JSON dictionary."""

    return {
        "name": molecule.name,
        "sources": molecule.sources,
        "had_error_termination": molecule.had_error_termination,
        "jobs": [
            job_to_web_json(job, molecule.name, index)
            for index, job in enumerate(molecule.jobs, 1)
        ],
    }


def create_web_bundle(molecules: Mapping[str, MoleculeData]) -> dict[str, object]:
    """Package a dictionary of molecules into a unified web payload."""

    return {
        "molecules": {
            name: molecule_to_web_json(molecule) for name, molecule in molecules.items()
        }
    }


class OrcaWebRequestHandler(http.server.SimpleHTTPRequestHandler):
    """Custom HTTP handler serving the web interface and JSON API endpoints."""

    def __init__(
        self,
        request: socketserver.BaseServer | tuple[bytes, socketserver.BaseServer],
        client_address: tuple[str, int],
        server: socketserver.BaseServer,
        data_dir: Path | None = None,
    ) -> None:
        """Initialize the web request handler."""

        self.data_dir = data_dir
        super().__init__(request, client_address, server, directory=str(WEB_DIR))  # type: ignore[arg-type]

    def do_GET(self) -> None:
        """Handle GET requests for static assets and API data."""

        parsed_url = urllib.parse.urlparse(self.path)
        if parsed_url.path == "/api/molecules":
            self._handle_api_molecules()
            return
        super().do_GET()

    def do_POST(self) -> None:
        """Handle POST requests for parsing, spectral convolution, and thermochemistry."""

        parsed_url = urllib.parse.urlparse(self.path)
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"

        try:
            payload: dict[str, Any] = json.loads(body)
        except Exception:
            payload = {}

        if parsed_url.path == "/api/parse":
            self._handle_api_parse(payload)
        elif parsed_url.path == "/api/convolute":
            self._handle_api_convolute(payload)
        elif parsed_url.path == "/api/thermochemistry":
            self._handle_api_thermochemistry(payload)
        else:
            self.send_error(404, "Endpoint not found")

    def _handle_api_molecules(self) -> None:
        """Scan data_dir and return parsed molecules."""

        if not self.data_dir or not self.data_dir.is_dir():
            self._send_json({"molecules": {}})
            return

        molecules = load_directory_parallel(self.data_dir, workers=1)
        bundle = create_web_bundle(molecules)
        self._send_json(bundle)

    def _handle_api_parse(self, payload: dict[str, Any]) -> None:
        """Parse raw ORCA output text and return web-ready JSON."""

        text = str(payload.get("content", ""))
        name = str(payload.get("name", "uploaded_molecule"))
        if not text:
            self._send_json({"error": "No content provided"}, status=400)
            return

        jobs = OrcaParser(io.StringIO(text), source_name=name).parse()
        molecule = MoleculeData(name=name, jobs=jobs, sources=[name])
        self._send_json({"molecule": molecule_to_web_json(molecule)})

    def _handle_api_convolute(self, payload: dict[str, Any]) -> None:
        """Compute Gaussian UV-Vis convolution with custom parameters and wavelength shift."""

        cm_raw = payload.get("tddft_cm", [])
        fosc_raw = payload.get("tddft_fosc", [])
        cm = [float(x) for x in cm_raw] if isinstance(cm_raw, list) else []
        fosc = [float(x) for x in fosc_raw] if isinstance(fosc_raw, list) else []

        shift_nm = float(payload.get("wavelength_shift_nm", 0.0))
        sigma_nm = float(payload.get("sigma_nm", 20.0))
        start_nm = float(payload.get("start_nm", 180.0))
        end_nm = float(payload.get("end_nm", 800.0))
        step_nm = float(payload.get("step_nm", 1.0))

        spectrum = convolute_uvvis_spectrum(
            tddft_cm=cm,
            tddft_fosc=fosc,
            wavelength_shift_nm=shift_nm,
            sigma_nm=sigma_nm,
            start_nm=start_nm,
            end_nm=end_nm,
            step_nm=step_nm,
        )
        self._send_json({"spectrum": spectrum})

    def _handle_api_thermochemistry(self, payload: dict[str, Any]) -> None:
        """Evaluate a reaction equation given molecule definitions."""

        equation = str(payload.get("equation", ""))
        molecules_raw = payload.get("molecules", {})
        if not equation:
            self._send_json({"error": "Missing equation"}, status=400)
            return

        molecules: dict[str, MoleculeData] = {}
        if isinstance(molecules_raw, dict):
            for name in molecules_raw:
                molecules[name] = MoleculeData(name=name)

        try:
            engine = ThermochemistryEngine(molecules)
            result = engine.evaluate(equation)
            self._send_json({"result": reaction_result_to_dict(result)})
        except Exception as err:
            self._send_json({"error": str(err)}, status=400)

    def _send_json(self, data: object, status: int = 200) -> None:
        """Send a JSON HTTP response."""

        encoded = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(encoded)


def run_gui_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    open_browser: bool = True,
    data_dir: Path | None = None,
) -> None:
    """Start the local ORCA Engine Web Interface server.

    Args:
        host: Interface to bind to.
        port: TCP port to listen on.
        open_browser: Whether to launch the system default browser.
        data_dir: Optional directory with ORCA output files to serve.
    """

    def handler_factory(
        request: socketserver.BaseServer | tuple[bytes, socketserver.BaseServer],
        client_address: tuple[str, int],
        server: socketserver.BaseServer,
    ) -> OrcaWebRequestHandler:
        return OrcaWebRequestHandler(
            request, client_address, server, data_dir=data_dir
        )

    with socketserver.TCPServer((host, port), handler_factory) as httpd:
        url = f"http://{host}:{port}/"
        print("==================================================")
        print("  ORCA Engine 3D & UV-Vis Interactive Web Studio  ")
        print(f"  Running at: {url}")
        print("==================================================")

        if open_browser:
            threading.Timer(0.8, lambda: webbrowser.open(url)).start()

        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server...")
