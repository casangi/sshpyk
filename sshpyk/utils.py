import json
import logging
import re
import time
from pathlib import Path
from shutil import which
from subprocess import PIPE, STDOUT, Popen, run
from typing import Dict, List, Optional, Tuple, Union

from jupyter_client.connect import find_connection_file

logger = logging.getLogger(__name__)


def inline_script(script: str):
    lines = (line.strip() for line in script.splitlines())
    return "; ".join(line for line in lines if line)


GET_ALL_SPECS_PY = inline_script(
    (Path(__file__).parent / "get_all_specs.py").read_text()
)


# ANSI color codes for terminal output
G = "\033[32m"  # Green
R = "\033[31m"  # Red
C = "\033[36m"  # Cyan
M = "\033[35m"  # Magenta
E = "\033[90m"  # Grey
W = "\033[33m"  # Orange
N = "\033[39m"  # Reset color only, not formatting

# Opening server ControlMaster connections the first time can take a long time.
# Even on my local network this easily took >15s for the first connection.
LAUNCH_TIMEOUT = 30
SHUTDOWN_TIME = 30
UNAME_PREFIX = "UNAME_INFO_RESULT"
RGX_UNAME_PREFIX = re.compile(rf"^{UNAME_PREFIX}=(.+)")
GET_SPECS_PREFIX = "GET_SPECS_RESULT"
RGX_GET_SPECS_PREFIX = re.compile(rf"^{GET_SPECS_PREFIX}=(.+)")

SSHPYK_PERSISTENT_FP_BASE = "sshpyk-kernel"


def verify_local_ssh(
    ssh: Optional[str],
    log: logging.Logger = logger,
    name: str = "ssh",
    lp: str = "",
    timeout: Optional[int] = LAUNCH_TIMEOUT,
) -> str:
    """Verify that the local SSH is working."""
    if not ssh:
        ssh = which(name)
        if not ssh:
            log.warning(f"{lp}Local {name} executable not found.")
        else:
            log.info(f"{lp}Auto-detected {name} executable: {ssh}")
    if not ssh:
        raise EnvironmentError(f"{lp}Local {name} executable not found.")
    cmd = [ssh, "-V"]
    log.debug(f"{lp}Verifying local {name} {cmd = }")
    ret = run(  # noqa: S603
        cmd,
        stdin=PIPE,
        stdout=PIPE,
        stderr=STDOUT,  # at least OpenSSH outputs version to stderr
        text=True,
        universal_newlines=True,
        check=False,
        timeout=timeout,
        bufsize=1,
    )  # type: ignore
    ok = ret.returncode == 0
    out = ret.stdout.strip()
    if not ok:
        msg = f"{lp}Local {name} verification failed: {out!r}"
        log.error(msg)
        raise EnvironmentError(msg)
    log.debug(f"{lp}Local {name} verification succeeded, {name} version: {out}")
    return ssh


def parse_ssh_config(config: str):
    lines = (line.strip() for line in config.strip().splitlines())
    tuples = (line.split(" ", 1) for line in lines if " " in line)
    # .lower() just in case, SSH already prints in lowercase
    configs = ((key.strip().lower(), value.strip()) for key, value in tuples)

    out: Dict[str, Union[str, List[str]]] = {}
    # ! there can be multiple entries for the same config key, e.g. IdentityFile
    for key, value in configs:
        if key in out:
            if isinstance(out[key], list):
                out[key].append(value)  # type: ignore
            else:
                out[key] = [out[key], value]  # type: ignore
        else:
            out[key] = value
    return out


def get_local_ssh_configs(
    ssh: List[str],
    alias: str,
    log: logging.Logger = logger,
    lp: str = "",
    timeout: Optional[int] = LAUNCH_TIMEOUT,
) -> List[Dict[str, Union[str, List[str]]]]:
    aliases = [alias]
    hosts_configs = []
    while aliases:
        alias = aliases.pop(0)
        # ! for non-defined aliases, it will still dump a config based on defaults
        cmd = [*ssh, "-G", alias]  # dumps all configs for the alias
        log.debug(f"{lp}Reading local SSH config for {alias!r}")
        log.debug(f"{C}{lp}{N}cmd: {' '.join(cmd)}")
        ret = run(  # noqa: S603
            cmd,
            stdin=PIPE,
            capture_output=True,
            text=True,
            universal_newlines=True,
            check=False,
            timeout=timeout,
            bufsize=1,
        )  # type: ignore
        ok = ret.returncode == 0
        if not ok:
            stderr = ret.stderr.strip()
            msg = f"{lp}Reading local SSH config for {alias!r} failed: {stderr!r}"
            log.error(msg)
            raise EnvironmentError(msg)
        config = parse_ssh_config(ret.stdout)

        if "host" not in config:
            # Some SSH versions (e.g. OpenSSH_9.0p1, LibreSSL 3.3.6 shipped with macOS)
            # do not include the "host" key in the output of the "-G" option.
            config["host"] = alias

        log.debug(f"{lp}Local SSH config for host alias {alias!r}: {config}")
        proxy_jump = config.get("proxyjump", None)
        if proxy_jump:
            if isinstance(proxy_jump, list):
                log.warning(
                    f"{lp}SSH reports more than one ProxyJump for {alias!r}: "
                    f"{proxy_jump!r}. This is likely a misconfiguration!"
                )
                # ? is this SSH config possible/valid at all?
                aliases.append(proxy_jump[0])
            else:
                aliases.append(proxy_jump)
        hosts_configs.append(config)
    log.debug(f"{lp}Read {len(hosts_configs)} local SSH configs")
    return hosts_configs


def validate_ssh_config(config: Dict[str, Union[str, List[str]]]):
    out = {}
    keys = [
        "hostname",
        "user",
        "batchmode",
        "identityfile",
        "controlmaster",
        "controlpersist",
        "controlpath",
        "proxyjump",
        "proxycommand",
    ]
    for key in tuple(keys):
        if isinstance(config.get(key, None), list):
            # ! If "IdentityFile" is missing, SSH will use a list of default identity
            # ! files. Don't make it an "error", otherwise the (last-resort) automated
            # ! password-based authentications (e.g. using `sshpass` + macOS Keychain)
            # ! might not be possible.
            out[key] = (
                "warning" if key == "identityfile" else "error",
                f"Likely missing in your ssh config. Multiple values: {config[key]}.",
            )
            del keys[keys.index(key)]

    if "user" in keys:
        if "user" not in config:
            out["user"] = ("error", "Missing, must be set in the ssh config.")
        else:
            out["user"] = ("info", config["user"])

    if "hostname" in keys:
        host = config.get("host", None)
        if host:
            hostname = config.get("hostname", None)
            if host != hostname:
                out["hostname"] = ("info", hostname)
            else:
                out["hostname"] = (
                    "error",
                    "Likely missing in your ssh config. "
                    f"{host=!r} and {hostname=!r} must be different.",
                )
        else:
            out["hostname"] = ("error", "Missing, must be set in the ssh config.")

    # ! Don't make it an "error", otherwise the (last-resort) automated password-based
    # ! authentications might not be possible. E.g. using `sshpass` + macOS Keychain.
    if "batchmode" in keys:
        bm = config.get("batchmode", None)
        if bm:
            if bm in ("yes", "true"):
                out["batchmode"] = ("ok", bm)
            else:
                out["batchmode"] = ("warning", f"Recommended to be 'yes', not {bm!r}.")
        else:
            out["batchmode"] = ("warning", "Missing, recommended to be 'yes'.")

    if "identityfile" in keys:
        id_file = config.get("identityfile", None)
        if id_file:
            fp = Path(id_file).resolve()  # type: ignore
            if fp.exists():
                out["identityfile"] = ("ok", str(fp))
            else:
                out["identityfile"] = (
                    "error",
                    f"'{fp}' does not exist. Make sure the private key file exists!",
                )
        else:
            out["identityfile"] = (
                "warning",
                "Missing, it is recommended to use private key authentication.",
            )

    if "controlmaster" in keys:
        cm = config.get("controlmaster", None)
        if cm:
            if cm == "auto":
                out["controlmaster"] = ("ok", "auto")
            else:
                out["controlmaster"] = (
                    "error",
                    f"Must be 'auto', not {cm!r}.",
                )
        else:
            out["controlmaster"] = ("error", "Missing, must be 'auto'.")

    if "controlpersist" in keys:
        c_persist = config.get("controlpersist", None)
        if c_persist:
            if c_persist not in ("no", "false"):
                out["controlpersist"] = ("ok", c_persist)
            else:
                out["controlpersist"] = (
                    "error",
                    f"Must be, e.g., '10m' or 'yes', not {c_persist!r}.",
                )
        else:
            out["controlpersist"] = ("error", "Missing, recommended '10m' (or larger).")

    if "controlpath" in keys:
        c_path = config.get("controlpath", None)
        recommended = "~/.ssh/sshpyk_%r@%h_%p"
        if c_path:
            dp = Path(c_path).resolve().parent  # type: ignore
            if dp.exists():
                out["controlpath"] = ("ok", c_path)
            else:
                out["controlpath"] = (
                    "error",
                    f"Parent dir '{dp}' does not exist! Parent dir must exist!",
                )
        else:
            out["controlpath"] = ("error", f"Missing, use, e.g., {recommended!r}.")

    proxy_cmd = config.get("proxycommand", None)
    if "proxycommand" in keys and proxy_cmd:
        out["proxycommand"] = (
            "warning",
            f"ProxyCommand: {proxy_cmd!r}. "
            "Use ProxyJump instead if reaching this host requires a Bastion host!",
        )

    if "proxyjump" in keys:
        proxy_jump = config.get("proxyjump", None)
        if proxy_jump:
            out["proxyjump"] = ("info", proxy_jump)  # nothing we can check about this

    return out


def verify_ssh_connection_quiet_ssh(
    cmd: List[str],
    host_alias: str,
    log: logging.Logger = logger,
    lp: str = "",
    start_new_session: bool = False,
    timeout: Optional[int] = LAUNCH_TIMEOUT,
):
    uname = ""
    try:
        ret = run(  # noqa: S603
            cmd,
            stdout=PIPE,
            stderr=PIPE,
            stdin=PIPE,
            text=True,
            check=False,
            start_new_session=start_new_session,
            universal_newlines=True,
            timeout=timeout,
        )
    except Exception as e:
        log.error(f"{R}{lp}{N}SSH connection to {host_alias!r} failed: {e}")
        return -123, uname

    stdout = ret.stdout.strip()
    stderr = ret.stderr.strip()
    ok = ret.returncode == 0

    for line in stderr.splitlines():
        line = line.strip()
        if not line:
            continue
        if ok:
            log.debug(f"{E}{lp}{N}[{host_alias} stderr] {line}")
        else:
            log.error(f"{R}{lp}{N}[{host_alias} stderr] {line}")

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        log.debug(f"{E}{lp}{N}[{host_alias} stdout] {line}")
        match = RGX_UNAME_PREFIX.search(line)
        if match:
            uname = match.group(1)
            break

    return ret.returncode, uname


def verify_ssh_connection_verbose_ssh(
    cmd: List[str],
    host_alias: str,
    log: logging.Logger = logger,
    lp: str = "",
    start_new_session: bool = False,
):
    """
    ! SSH does not close its stderr pipe if the ControlMaster does NOT exist yet
    ! AND a verbose -v/vv/vvv option has been passed to it.
    ! Irony of a bug while trying to debug...
    ! If the ControlMaster DOES exist already, then we don't have this issue.

    ! I don't fully understand why this happens, but from some simple python
    ! scripts that invokes ssh it seems that ssh never closes its stderr pipe
    ! when it was started with -v/vv/vvv and a control master child process
    ! was forked to create the control master socket.

    ! OpenSSH seems to have several bugs related to this over the years.
    ! https://bugzilla.mindrot.org/show_bug.cgi?id=1988 (seem resolved)
    ! https://bugzilla.mindrot.org/show_bug.cgi?id=3046 (no answers, fits our case)
    ! As of 'OpenSSH_9.9p2, OpenSSL 3.4.1 11 Feb 2025' the issue is still there, at
    ! least on macOS using `openssh` from Homebrew.

    ! In any case, since people are likely to use oldish versions of OpenSSH,
    ! we need to handle this to the best of our ability.
    """
    uname = ""
    try:
        proc = Popen(  # noqa: S603
            cmd,
            stdout=PIPE,
            # Merge the pipes so that we have a criteria to break from the loop despite
            # the pipe never being closed by the ssh process.
            stderr=STDOUT,
            stdin=PIPE,
            text=True,
            universal_newlines=True,
            start_new_session=start_new_session,
            bufsize=1,
        )
        # We don't have anything to send to the process
        proc.stdin.close()  # type: ignore
    except Exception as e:
        log.error(f"{R}{lp}{N}SSH connection to {host_alias!r} failed: {e}")
        return -123, uname

    # ! REMINDER: when verbose=True, and a control socket was created as part of the ssh
    # ! command, then ssh won't close its stderr pipe (here piped into stdout).
    # ! We wait for our target line and then break and close pipes ourselves.
    # ! Note that `for line in proc.stdout` blocks if the stderr/stdout pipe is never
    # ! closed. This is not ideal but acceptable since this functions is intended to
    # ! run only when performing some kind of debugging with verbose ssh, so the user
    # ! should be able to detect if it hangs and kill the process(es) themselves.
    for line in proc.stdout:  # type: ignore
        line = line.strip()
        if not line:
            continue
        log.debug(f"{E}{lp}{N}[{host_alias} stdout/stderr] {line}")
        match = RGX_UNAME_PREFIX.search(line)
        if match:
            uname = match.group(1)
            break

    proc.stdout.close()  # type: ignore

    # Simple cleanup retries
    for _ in range(20):
        proc.terminate()
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    if proc.poll() is None:
        proc.kill()  # if still alive force-kill
    proc.wait()
    return proc.returncode, uname


def verify_ssh_connection(
    ssh: List[str],
    host_alias: str,
    log: logging.Logger = logger,
    lp: str = "",
    start_new_session: bool = False,
    timeout: Optional[int] = LAUNCH_TIMEOUT,
):
    """Verify that the SSH connection to the remote host is working."""
    verbose = any(v in ssh for v in ("-v", "-vv", "-vvv"))
    cmd = [*ssh, host_alias, f'echo "{UNAME_PREFIX}=$(uname -a)"']
    log.debug(f"{E}{lp}{N}Verifying SSH connection to {host_alias!r}")
    if verbose:
        log.info(f"{C}{lp}{N}cmd: {' '.join(cmd)}")
    else:
        log.debug(f"{C}{lp}{N}cmd: {' '.join(cmd)}")

    if verbose:
        returncode, uname = verify_ssh_connection_verbose_ssh(
            cmd, host_alias, log, lp, start_new_session
        )
    else:
        returncode, uname = verify_ssh_connection_quiet_ssh(
            cmd, host_alias, log, lp, start_new_session, timeout=timeout
        )
    ok = returncode == 0 and bool(uname)
    if not ok:
        msg = f"{R}{lp}{N}SSH connection to {host_alias!r} failed "
        msg += f"(exit code={returncode})."
        log.error(msg)
    else:
        msg = f"{E}{lp}{N}SSH connection to {host_alias!r} succeeded: {uname = !r}."
        log.debug(msg)
    return ok, uname


def verify_rem_executable(
    ssh: List[str],
    host_alias: str,
    fp: str,
    log: logging.Logger = logger,
    lp: str = "",
    timeout: Optional[int] = LAUNCH_TIMEOUT,
):
    """Verify that the remote executable exists and is executable."""
    # NB the quotes around filename are mandatory and safer
    cmd = [*ssh, host_alias, f'test -e "{fp}" && test -r "{fp}" && test -x "{fp}"']
    log.debug(f"{lp}Verifying remote executable {fp!r} on {host_alias!r}: {cmd = }")
    ret = run(  # noqa: S603
        cmd,
        stdin=PIPE,
        capture_output=True,
        text=True,
        universal_newlines=True,
        check=False,
        timeout=timeout,
        bufsize=1,
    )  # type: ignore
    ok = ret.returncode == 0
    if not ok:
        msg = f"{lp}Remote {fp!r} not found/readable/executable "
        msg += f"(exit code {ret.returncode})."
        log.error(msg)
    else:
        msg = f"{lp}Remote {fp!r} exists, is readable and executable."
        log.debug(msg)
    return ok


def verify_rem_dir_exists(
    ssh: List[str],
    host_alias: str,
    dir_path: str,
    log: logging.Logger = logger,
    lp: str = "",
    timeout: Optional[int] = LAUNCH_TIMEOUT,
) -> Tuple[bool, str]:
    """Verify that the remote directory exists."""
    # NB the quotes around dir_path are mandatory and safer
    cmd = [*ssh, host_alias, f'echo "{dir_path}" && test -d "{dir_path}"']
    log.debug(
        f"{lp}Verifying remote directory {dir_path!r} on {host_alias!r}: {cmd = }"
    )
    ret = run(  # noqa: S603
        cmd,
        stdin=PIPE,
        capture_output=True,
        text=True,
        universal_newlines=True,
        check=False,
        timeout=timeout,
        bufsize=1,
    )  # type: ignore
    ok = ret.returncode == 0
    if not ok:
        msg = f"{lp}Remote directory {dir_path!r} does not exist or is not a directory."
        # Don't log as error, it might be an expected non-existent default path
        log.debug(msg)
    else:
        msg = f"{lp}Remote directory {dir_path!r} exists."
        log.debug(msg)
    return ok, ret.stdout.strip()


def fetch_remote_kernel_specs(
    ssh: List[str],
    host_alias: str,
    python: str,
    log: logging.Logger = logger,
    lp: str = "",
    timeout: Optional[int] = LAUNCH_TIMEOUT,
) -> dict:
    """
    Fetch kernel specifications from the remote host using SSH.
    Returns a dictionary of kernel specs from the remote system.
    """
    cmd = [
        *ssh,
        host_alias,
        # ! `echo -n` is not supported on all systems, use `printf` instead.
        f"printf '{GET_SPECS_PREFIX}=' && {python} -c '{GET_ALL_SPECS_PY}'",
    ]
    log.debug(f"{lp}Fetching remote kernel specs from {host_alias!r}: {cmd = }")
    ret = run(  # noqa: S603
        cmd,
        stdin=PIPE,
        capture_output=True,
        text=True,
        universal_newlines=True,
        check=False,
        timeout=timeout,
        bufsize=1,
    )  # type: ignore
    if ret.returncode != 0:
        msg = f"{lp}Failed to fetch remote kernel specs: {ret.stderr.strip()!r}"
        log.error(msg)
        raise RuntimeError(msg)
    stdout = ret.stdout.strip()
    lines = stdout.splitlines()
    try:
        for line in lines:
            if not line:
                continue
            match = RGX_GET_SPECS_PREFIX.search(line)
            if match:
                specs = json.loads(match.group(1))
                break
    except json.JSONDecodeError as e:
        msg = f"{lp}Failed to parse remote kernel specs: {e}"
        log.error(msg)
        raise RuntimeError(msg) from e

    log.info(f"{lp}Successfully fetched {len(specs)} kernel specs from {host_alias!r}")
    return specs


def find_persistent_file(
    filename: str = f"{SSHPYK_PERSISTENT_FP_BASE}-*.json",
    path: Union[str, List[str], None] = None,
    profile: Optional[str] = None,
) -> str:
    """
    Find a persistent file by name or glob pattern.
    The persistent file is a JSON file that contains the provisioner info.

    This function is a wrapper around `jupyter_client.connect.find_connection_file`.
    See the docstring of that function for details.
    """
    return find_connection_file(filename, path, profile)
