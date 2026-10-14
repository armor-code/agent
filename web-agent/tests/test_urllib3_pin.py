"""ENG-142186: web-agent must ship urllib3 >= 2.8.0.

urllib3 < 2.8.0 leaks target-server TLS settings (e.g. cert_reqs=CERT_NONE) into
the HTTPS proxy connection (GHSA-8988-9cw3-xx77, ArmorCode finding 3006834).

Static tests run anywhere with pytest. The image test needs Docker and is opt-in:
    RUN_DOCKER_TESTS=1 python3 -m pytest web-agent/tests -v
"""
import os
import py_compile
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

WEB_AGENT_DIR = Path(__file__).resolve().parent.parent
DOCKERFILE = WEB_AGENT_DIR / "Dockerfile"
REQUIREMENTS = WEB_AGENT_DIR / "requirements.txt"
WORKER = WEB_AGENT_DIR / "app" / "worker.py"

MIN_URLLIB3 = (2, 8, 0)
VENV_PYTHON = "/usr/src/venv/bin/python3"
# Matches: urllib3>=2.8.0  /  "urllib3>=2.8.0"  /  urllib3==2.8.0
URLLIB3_SPEC = re.compile(r"""urllib3\s*(>=|==|~=)\s*(\d+(?:\.\d+)*)""")


def _parse_version(text):
    return tuple(int(part) for part in text.split("."))


def _lower_bounds(path):
    return [
        _parse_version(match.group(2))
        for match in URLLIB3_SPEC.finditer(path.read_text())
    ]


def _at_least(version, minimum):
    padded = version + (0,) * (len(minimum) - len(version))
    return padded >= minimum


def test_dockerfile_pins_urllib3_at_or_above_fix():
    bounds = _lower_bounds(DOCKERFILE)

    assert bounds, "Dockerfile must constrain urllib3 explicitly"
    assert all(_at_least(bound, MIN_URLLIB3) for bound in bounds), (
        f"Dockerfile urllib3 bound(s) {bounds} are below the fixed version {MIN_URLLIB3}"
    )


def test_requirements_pin_urllib3_at_or_above_fix():
    bounds = _lower_bounds(REQUIREMENTS)

    assert bounds, "requirements.txt must constrain urllib3 explicitly"
    assert all(_at_least(bound, MIN_URLLIB3) for bound in bounds), (
        f"requirements.txt urllib3 bound(s) {bounds} are below the fixed version {MIN_URLLIB3}"
    )


def test_worker_compiles(tmp_path):
    """The Dockerfile copies app/* verbatim; a syntax error crashes the agent at startup."""
    py_compile.compile(str(WORKER), cfile=str(tmp_path / "worker.pyc"), doraise=True)


@pytest.fixture(scope="module")
def built_image():
    if os.environ.get("RUN_DOCKER_TESTS") != "1":
        pytest.skip("set RUN_DOCKER_TESTS=1 to build the image")
    if shutil.which("docker") is None:
        pytest.skip("docker not installed")

    tag = f"web-agent-urllib3-test:{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "build", "-t", tag, str(WEB_AGENT_DIR)],
        check=True, timeout=900,
    )
    yield tag
    subprocess.run(["docker", "rmi", "-f", tag], check=False)


def _run_in_image(tag, *args):
    return subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", VENV_PYTHON, tag, *args],
        capture_output=True, text=True, timeout=120, check=True,
    ).stdout.strip()


def test_image_ships_fixed_urllib3(built_image):
    """Checks the venv interpreter: system python3 has no urllib3 and would false-negative."""
    version = _run_in_image(built_image, "-c", "import urllib3; print(urllib3.__version__)")

    assert _at_least(_parse_version(version), MIN_URLLIB3), (
        f"image ships urllib3 {version}, need >= {'.'.join(map(str, MIN_URLLIB3))}"
    )


def test_image_has_single_urllib3_dist_info(built_image):
    """Guards against a stale 2.7.0 dist-info shadowing the upgraded package."""
    listing = _run_in_image(
        built_image, "-c",
        "import glob; print(' '.join(sorted(glob.glob("
        "'/usr/src/venv/lib/python*/site-packages/urllib3-*.dist-info'))))",
    )

    assert len(listing.split()) == 1, f"expected exactly one urllib3 dist-info, got: {listing}"


def test_image_core_imports_work(built_image):
    """urllib3 2.8.0 must stay compatible with requests/gevent used by worker.py."""
    output = _run_in_image(
        built_image, "-c",
        "import requests, gevent, urllib3, idna; print('ok')",
    )

    assert output == "ok"
