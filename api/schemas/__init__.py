# -*- coding: utf-8 -*-
"""Pydantic request/response schemas for the /api/v1 surface."""
from pydantic import BaseModel, Field


class OrcaGenerateRequest(BaseModel):
    """ORCA input generation. maxdisk is a CONFIGURABLE caller input (MB):
    an explicit valid value is preserved verbatim in the generated input;
    when omitted the execution runner applies its configured budget
    (default 20000 MB). Never hard-limited to one backend's quota."""

    coords: str = Field(min_length=1, max_length=2 * 1024 * 1024)
    name: str = Field(default="", max_length=200)
    calc_type: str = "sp"
    family: str | None = None
    theory: str = ""
    basis: str = ""
    disp: str = "none"
    ri_type: str = "none"
    scf_conv: str = "none"
    solv_model: str = "none"
    solvent: str = "Water"
    custom_line: str | None = Field(default=None, max_length=4096)
    x2c: bool = False
    charge: int = Field(default=0, ge=-10, le=10)
    mult: int = Field(default=1, ge=1, le=20)
    nroots: int | None = Field(default=None, ge=1, le=999)
    cores: int = Field(default=4, ge=1, le=128)
    ram: int = Field(default=6000, ge=100, le=64000)
    maxdisk: int | None = Field(default=None, ge=1, description="MaxDisk budget in MB (configurable per backend)")
    largeprint: bool = False
    temp: float = Field(default=298.15, gt=0)
    pressure: float = Field(default=1.0, gt=0)


class OrcaGenerateResponse(BaseModel):
    ok: bool = True
    input_text: str
    filename: str
    file_base64: str


class KaggleCredentials(BaseModel):
    """The application's authentication contract: per-request Kaggle
    credentials (optionally resolved from the encrypted vault)."""

    kaggle_username: str = Field(min_length=1, max_length=256)
    kaggle_key: str = Field(min_length=1, max_length=8192)


class JobSubmitRequest(KaggleCredentials):
    # The API key may be omitted when the server has an owner-scoped encrypted
    # vault entry.  Username remains required because this v1 transport has no
    # browser-session dependency of its own.
    kaggle_key: str = Field(default="", max_length=8192)
    input_filename: str = Field(default="molecule.inp", max_length=255)
    input_content: str = Field(min_length=1, max_length=2 * 1024 * 1024)
    job_name: str = Field(default="", max_length=200)
    dataset_sources: str = Field(default="", max_length=4096, description="comma/space separated Kaggle dataset ids holding the licensed ORCA package")
    orca_link: str = Field(default="", max_length=4096, description="direct ORCA package download link (alternative to dataset_sources)")
    maxdisk_mb: int | None = Field(default=None, ge=1, description="optional per-job MaxDisk budget override (MB); default 20000")
    kaggle_passcode: str = Field(default="", max_length=1024, description="Optional site Kaggle execution authorization passcode")


class JobStatusRequest(KaggleCredentials):
    job_id: str = Field(min_length=1)


class ExtractOptCoordsRequest(KaggleCredentials):
    job_id: str = Field(min_length=1)
