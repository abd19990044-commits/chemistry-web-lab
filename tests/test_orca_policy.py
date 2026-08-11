from orca_policy import OrcaPolicyError, validate_payload


def payload(**overrides):
    value = {
        "custom_line": None,
        "calc_type": "sp",
        "family": "f_dft",
        "theory": "B3LYP",
        "basis": "def2-TZVP",
        "disp": "none",
        "ri_type": "rijcosx",
        "x2c": False,
    }
    value.update(overrides)
    return value


def test_wb97m_v_cannot_be_combined_with_tddft():
    try:
        validate_payload(payload(theory="wB97M-V", calc_type="tddft"))
    except OrcaPolicyError as exc:
        assert "TD-DFT" in str(exc)
    else:
        raise AssertionError("wB97M-V + TD-DFT must be rejected")


def test_wb97m_v_cannot_take_extra_d4():
    try:
        validate_payload(payload(theory="wB97M-V", disp="D4"))
    except OrcaPolicyError as exc:
        assert "wB97M-V" in str(exc)
    else:
        raise AssertionError("wB97M-V + D4 must be rejected")


def test_wb97x_d4_cannot_take_a_second_d4():
    try:
        validate_payload(payload(theory="wB97X-D4", disp="D4"))
    except OrcaPolicyError as exc:
        assert "D4" in str(exc)
    else:
        raise AssertionError("wB97X-D4 + D4 must be rejected")


def test_hybrid_dft_cannot_use_bare_ri():
    try:
        validate_payload(payload(theory="PBE0", ri_type="ri"))
    except OrcaPolicyError as exc:
        assert "RIJCOSX" in str(exc)
    else:
        raise AssertionError("hybrid DFT + bare RI must be rejected")


def test_ri_mp2_requires_an_ri_selection():
    try:
        validate_payload(payload(family="f_mp2", theory="RI-MP2", ri_type="none"))
    except OrcaPolicyError as exc:
        assert "RI-MP2" in str(exc)
    else:
        raise AssertionError("RI-MP2 without RI must be rejected")


def test_dlpno_may_skip_the_separate_ri_screen():
    # The wizard intentionally skips the RI screen for DLPNO methods because
    # the method itself obligatorily uses RI; the renderer adds AutoAux.
    validate_payload(payload(family="f_ccsd", theory="DLPNO-CCSD(T)", ri_type="none"))


def test_composite_methods_cannot_take_x2c_or_extra_dispersion():
    for overrides in (
        {"family": "f_comp", "theory": "r2SCAN-3C", "x2c": True},
        {"family": "f_comp", "theory": "B97-3C", "disp": "D4"},
    ):
        try:
            validate_payload(payload(**overrides))
        except OrcaPolicyError:
            pass
        else:
            raise AssertionError("unsupported composite-method combination was accepted")


def test_x2c_rejects_non_supported_guided_basis():
    try:
        validate_payload(payload(x2c=True, basis="cc-pVTZ"))
    except OrcaPolicyError as exc:
        assert "x2c" in str(exc).lower()
    else:
        raise AssertionError("unsupported X2C basis was accepted")
