import json
import logging
from pathlib import Path
from shutil import which
from subprocess import run
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def inline_script(script: str):
    lines = (line.strip() for line in script.splitlines())
    return "; ".join(line for line in lines if line)


GET_ALL_SPECS_PY = inline_script(
    (Path(__file__).parent / "get_all_specs.py").read_text()
)


def verify_local_ssh(
    ssh: Optional[str], log: logging.Logger = logger, name: str = "ssh", lp: str = ""
) -> str:
    """Verify that the local SSH is working."""
    if not ssh:
        ssh = which(name)
        if not ssh:
            log.warning(f"{lp}Local {name!r} executable not found.")
        else:
            log.info(f"{lp}Auto-detected {name!r} executable: {ssh}")
    if not ssh:
        raise EnvironmentError(f"{lp}Local '{name!r}' executable not found.")
    cmd = [ssh, "-V"]
    log.debug(f"{lp}Verifying local {name!r} {cmd = }")
    ret = run(  # noqa: S603
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )  # type: ignore
    ok = ret.returncode == 0
    if not ok:
        msg = f"{lp}Local {name!r} verification failed"
        log.error(msg)
        raise EnvironmentError(msg)
    log.debug(f"{lp}Local {name!r} verification succeeded")
    return ssh


def verify_ssh_connection(
    ssh: str,
    host_alias: str,
    log: logging.Logger = logger,
    lp: str = "",
) -> Tuple[bool, str]:
    """Verify that the SSH connection to the remote host is working."""
    cmd = [ssh, "-q", host_alias, "echo OK"]
    log.debug(f"{lp}Verifying SSH connection to {host_alias!r}: {cmd = }")
    ret = run(  # noqa: S603
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )  # type: ignore
    ok = ret.returncode == 0 and ret.stdout.strip() == "OK"
    if not ok:
        msg = f"{lp}SSH connection to {host_alias!r} failed: {ret.stdout.strip()!r}"
        log.error(msg)
    else:
        msg = f"{lp}SSH connection to {host_alias!r} succeeded."
        log.debug(msg)
    return ok, msg


def verify_rem_executable(
    ssh: str,
    host_alias: str,
    fp: str,
    log: logging.Logger = logger,
    lp: str = "",
) -> Tuple[bool, str]:
    """Verify that the remote executable exists and is executable."""
    # NB the quotes around filename are mandatory and safer
    cmd = [ssh, host_alias, f'test -e "{fp}" && test -r "{fp}" && test -x "{fp}"']
    log.debug(f"{lp}Verifying remote executable {fp!r} on {host_alias!r}: {cmd = }")
    ret = run(  # noqa: S603
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )  # type: ignore
    ok = ret.returncode == 0
    ret_str = ret.stdout.strip()
    if not ok:
        msg = f"{lp}Remote {fp!r} not found/readable/executable ({ret_str!r})"
        log.error(msg)
    else:
        msg = f"{lp}Remote {fp!r} exists, is readable and executable."
        log.debug(msg)
    return ok, msg


def fetch_remote_kernel_specs(
    ssh: str,
    host_alias: str,
    python: str,
    log: logging.Logger = logger,
    lp: str = "",
) -> dict:
    """
    Fetch kernel specifications from the remote host using SSH.
    Returns a dictionary of kernel specs from the remote system.
    """
    cmd = [ssh, host_alias, f"{python} -c '{GET_ALL_SPECS_PY}'"]
    log.debug(f"{lp}Fetching remote kernel specs from {host_alias!r}: {cmd = }")
    ret = run(  # noqa: S603
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )  # type: ignore
    if ret.returncode != 0:
        msg = f"{lp}Failed to fetch remote kernel specs: {ret.stderr.strip()!r}"
        log.error(msg)
        raise RuntimeError(msg)

    try:
        specs = json.loads(ret.stdout.strip())
        log.info(
            f"{lp}Successfully fetched {len(specs)} kernel specs from {host_alias!r}"
        )
        return specs
    except json.JSONDecodeError as e:
        msg = f"{lp}Failed to parse remote kernel specs: {e}"
        log.error(msg)
        raise RuntimeError(msg) from e
