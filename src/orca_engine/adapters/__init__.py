"""Integration adapters for web frameworks and GUI front-ends."""

from orca_engine.adapters.web_adapter import (
    convolute_uvvis_spectrum,
    generate_xyz_string,
    job_to_web_json,
    molecule_to_web_json,
    run_gui_server,
)

__all__ = [
    "convolute_uvvis_spectrum",
    "generate_xyz_string",
    "job_to_web_json",
    "molecule_to_web_json",
    "run_gui_server",
]
