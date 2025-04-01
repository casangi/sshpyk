import json
import logging
from pathlib import Path
from shutil import which
from subprocess import run
from typing import Tuple

logger = logging.getLogger(__name__)


def inline_script(script: str):
    lines = (line.strip() for line in script.splitlines())
    return "; ".join(line for line in lines if line)


GET_ALL_SPECS_PY = inline_script(
    (Path(__file__).parent / "get_all_specs.py").read_text()
)


def verify_local_ssh(log: logging.Logger = logger) -> str:
    """Verify that the local SSH is working."""
    ssh = which("ssh")
    if not ssh:
        raise EnvironmentError("Local 'ssh' executable not found.")
    cmd = [ssh, "-V"]
    log.debug(f"Verifying local SSH {cmd = }")
    ret = run(  # noqa: S603
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )  # type: ignore
    ok = ret.returncode == 0
    if not ok:
        msg = f"Local SSH verification failed: {ret.stdout.strip()!r}"
        log.error(msg)
        raise EnvironmentError(msg)
    log.info(f"Local SSH verification succeeded: {ret.stdout.strip()!r}")
    return ssh


def verify_ssh_connection(
    host_alias: str,
    log: logging.Logger = logger,
) -> Tuple[str, bool, str]:
    """Verify that the SSH connection to the remote host is working."""
    ssh = verify_local_ssh(log)
    cmd = [ssh, host_alias, "echo OK"]
    log.debug(f"Verifying SSH connection to {host_alias!r}: {cmd = }")
    ret = run(  # noqa: S603
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )  # type: ignore
    ret_str = ret.stdout.strip()
    ok = ret_str == "OK"
    if not ok:
        msg = f"SSH connection to {host_alias!r} failed: {ret_str!r}"
        log.error(msg)
    else:
        msg = f"SSH connection to {host_alias!r} succeeded"
        log.info(msg)
    return ssh, ok, msg


def verify_rem_executable(
    ssh: str,
    host_alias: str,
    fp: str,
    log: logging.Logger = logger,
) -> Tuple[bool, str]:
    """Verify that the remote executable exists and is executable."""
    # NB the quotes around filename are mandatory and safer
    cmd = [ssh, host_alias, f'test -e "{fp}" && test -r "{fp}" && test -x "{fp}"']
    log.debug(f"Verifying remote executable {fp!r} on {host_alias!r}: {cmd = }")
    ret = run(  # noqa: S603
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )  # type: ignore
    ok = ret.returncode == 0
    ret_str = ret.stdout.strip()
    if not ok:
        msg = f"Remote {fp!r} not found/readable/executable ({ret_str!r})"
        log.error(msg)
    else:
        msg = f"Remote {fp!r} exists, is readable and executable."
        log.debug(msg)
    return ok, msg


def fetch_remote_kernel_specs(
    ssh: str,
    host_alias: str,
    python: str,
    log: logging.Logger = logger,
) -> dict:
    """
    Fetch kernel specifications from the remote host using SSH.
    Returns a dictionary of kernel specs from the remote system.
    """
    cmd = [ssh, host_alias, f"{python} -c '{GET_ALL_SPECS_PY}'"]
    log.debug(f"Fetching remote kernel specs from {host_alias!r}: {cmd = }")
    ret = run(  # noqa: S603
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )  # type: ignore
    if ret.returncode != 0:
        msg = f"Failed to fetch remote kernel specs: {ret.stderr.strip()!r}"
        log.error(msg)
        raise RuntimeError(msg)

    try:
        specs = json.loads(ret.stdout.strip())
        log.info(f"Successfully fetched {len(specs)} kernel specs from {host_alias!r}")
        return specs
    except json.JSONDecodeError as e:
        msg = f"Failed to parse remote kernel specs: {e}"
        log.error(msg)
        raise RuntimeError(msg) from e
