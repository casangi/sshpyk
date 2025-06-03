"""SSH Kernel Provisioner implementation for Jupyter Client."""

import asyncio
import hashlib
import json
import re
import subprocess
import uuid
from enum import Enum, unique
from itertools import dropwhile
from pathlib import Path
from signal import SIGINT, SIGKILL, SIGTERM
from typing import Any, Callable, Dict, List, Optional, Tuple

from jupyter_client.connect import KernelConnectionInfo, LocalPortCache
from jupyter_client.provisioning.provisioner_base import KernelProvisionerBase
from jupyter_client.session import new_id_bytes
from jupyter_core.paths import jupyter_runtime_dir, secure_write
from traitlets import Bool, Integer, TraitError, Unicode
from traitlets import Enum as EnumTrait

from .kernelapp import EXISTING, PERSISTENT, PERSISTENT_FILE, SSH_VERBOSE
from .utils import (
    LAUNCH_TIMEOUT,
    RGX_UNAME_PREFIX,
    SHUTDOWN_TIME,
    SSHPYK_PERSISTENT_FP_BASE,
    UNAME_PREFIX,
    C,
    E,
    G,
    N,
    R,
    W,
    find_persistent_file,
    get_local_ssh_configs,
    validate_ssh_config,
    verify_local_ssh,
    verify_ssh_connection,
)

REMOTE_INFO_LINE = "; ".join(
    f"echo SSHPYK_{k}={v}"
    for k, v in (
        ("SHELL", "$SHELL"),
        ("HOST", "$(hostname)"),
        ("USER", "$USER"),
        ("HOME", "$HOME"),
        ("HOME_OK", '$(if test -d $HOME; then echo "yes"; else echo "no"; fi)'),
    )
)
PY_CHECK = "SSHPYK_PYTHON_CHECK"
RGX_PY_CHECK = re.compile(rf"^{PY_CHECK}=(\d+)")
PS_PREFIX = "SSHPYK_PS_OUTPUT_START"
RGX_PS_PREFIX = re.compile(rf"^{PS_PREFIX}=(.+)")
CONN_INFO = "SSHPYK_CONNECTION_INFO_JSON"
RGX_CONN_INFO = re.compile(rf"^{CONN_INFO}=(.+)")
RGX_CONN_FP = re.compile(r"\[SSHKernelApp\].*file: (.*\.json)")
RGX_CONN_CLIENT = re.compile(r"\[SSHKernelApp\].*client: (.*\.json)")
PID_KA = "SSHPYK_KERNELAPP_PID"
RGX_PID_KA = re.compile(rf"^{PID_KA}=(\d+)")
PID_KERNEL = "SSHPYK_KERNEL_PID"
RGX_PID_KERNEL = re.compile(rf"^{PID_KERNEL}=(\d+)")
# extracted from jupyter_client/kernelspec.py
RGX_KERNEL_NAME = re.compile(r"^[a-z0-9._-]+$", re.IGNORECASE)
RGX_SSH_HOST_ALIAS = re.compile(r"^[a-z0-9_-]+$", re.IGNORECASE)

RGX_SSH_LOGS = re.compile(r"^debug\d+: ")

KERNELAPP_PY = (Path(__file__).parent / "kernelapp.py").read_bytes()
KA_VERSION = hashlib.sha256(KERNELAPP_PY).hexdigest()[:8]  # 4 bytes = 8 hex chars
KERNELAPP_PY = KERNELAPP_PY.decode()

PNAMES = ("shell_port", "iopub_port", "stdin_port", "hb_port", "control_port")

T_PROC_INFO = Tuple[bool, Dict[int, Dict[str, str]]]


@unique
class RProcResult(Enum):
    OK = 0
    FETCH_FAILED = 1
    PROCESS_NOT_FOUND = 2
    SIGNAL_FAILED = 3


def is_zombie(state: str) -> bool:
    # `Z` in `ps` output means the process dead/zombie, expected in some cases
    return "z" in state.lower()


class SshHost(Unicode):
    def validate(self, obj, value):
        value = super().validate(obj, value)
        try:
            if not RGX_SSH_HOST_ALIAS.match(value):
                raise TraitError(
                    f"Invalid SSH host alias {value!r}. "
                    f"Must match this pattern {RGX_SSH_HOST_ALIAS.pattern}. "
                    "Verify that it is defined in your local SSH config file."
                )
            return value
        except Exception as e:
            self.error(obj, value, e)


class UnicodePath(Unicode):
    def validate(self, obj, value):
        value = super().validate(obj, value)
        try:
            Path(value)  # should raise if not a valid path
            return value
        except Exception:
            self.error(obj, value, ValueError(value, "a valid path"))


class MustExistUnicodePath(Unicode):
    def validate(self, obj, value):
        value = super().validate(obj, value)
        # should raise if not a valid path
        p = Path(value).expanduser().resolve().absolute()
        if not p.exists() or not p.is_file():
            msg = "a path to an existing ssh config file"
            self.error(obj, value, ValueError(value, msg))
        return str(p)


class UnicodeAbsolutePath(Unicode):
    def validate(self, obj, value):
        value = super().validate(obj, value)
        try:
            p = Path(value)  # should raise if not a valid path
            if not p.is_absolute():
                raise ValueError()
            return value
        except Exception:
            self.error(obj, value, ValueError(value, "an absolute path"))


class KernelName(Unicode):
    def validate(self, obj, value):
        # value = super().validate(obj, value) # not needed since we use regex
        try:
            if not RGX_KERNEL_NAME.match(value):
                raise ValueError()
            return value
        except Exception:
            self.error(obj, value, ValueError(value, "a valid kernel name"))


LOG_NAME = "SSHPYK"


class SSHKernelProvisioner(KernelProvisionerBase):
    """
    Kernel provisioner that launches Jupyter kernels on remote systems via SSH.

    This provisioner connects to remote systems using SSH, sets up port local forwarding
    for kernel communication, and manages the lifecycle of the remote kernel.
    """

    ssh_host_alias = SshHost(
        config=True,
        help="Remote host alias to connect to. "
        "It must be defined in your local SSH config file.",
        allow_none=False,
    )
    ssh_config = MustExistUnicodePath(
        config=True,
        help="Path to the SSH config file to use. "
        "If not provided, the SSH executable's default config file will be used.",
        allow_none=True,
        default_value=None,
    )
    remote_python = UnicodeAbsolutePath(
        config=True,
        help="Path to the Python executable on the remote system. "
        "Run `which python` on the remote system to find its path. "
        "If the remote kernel is part of a virtual environment (e.g. conda env), "
        "first activate your virtual environment and then run `which python`. "
        "Note that `jupyter_client` package must be installed on the remote. "
        "You can confirm it with `python -m pip show jupyter_client'.",
        allow_none=False,
    )
    remote_kernel_name = KernelName(
        config=True,
        help="Kernel name on the remote system "
        "(i.e. first column of `jupyter kernelspec list` on the remote system).",
        allow_none=False,
    )
    launch_timeout = Integer(
        default_value=LAUNCH_TIMEOUT,
        config=True,
        help="Timeout for launching the remote kernel through the ssh command(s).",
        allow_none=False,
    )
    shutdown_time = Integer(
        default_value=SHUTDOWN_TIME,
        config=True,
        help="Timeout for shutting down the remote kernel through the ssh command(s). "
        "If the kernel does not shutdown within this time, "
        "it will be killed forcefully, "
        "after which an equal amount of time will be waited for the kernel to exit.",
        allow_none=False,
    )
    ssh = Unicode(
        config=True,
        help="Path to SSH executable. "
        "If None, will be auto-detected using 'which ssh'.",
        allow_none=True,
        default_value=None,
    )
    ssh_verbose = EnumTrait(**SSH_VERBOSE)  # type: ignore
    independent_local_processes = Bool(
        config=True,
        help="If True, all the local subprocesses are spawned with "
        "`start_new_session=True` that makes these subprocesses have their own "
        "process group, instead of inheriting the process group of the parent process."
        " Some UIs like JupyterLab send SIGINT/SIGTERM/SIGKILL to the kernel's process"
        "group. This option allows to avoid it. You might want to change it to `False`"
        "if you are running `sshpyk-kernel` directly and know what you are doing.",
        default_value=True,
    )
    existing = Unicode(**EXISTING)  # type: ignore
    persistent = Bool(**PERSISTENT)  # type: ignore
    persistent_file = Unicode(**PERSISTENT_FILE)  # type: ignore

    restart_requested = False
    log_prefix = ""

    ssh_base = []
    all_hosts = []
    _fetch_remote_processes_lock = None
    _poll_lock = None
    _cleanup_lock = None
    _launch_lock = None
    _ensure_tunnels_lock = None

    popen_procs = None
    kernel_tunnels_args = None

    cf_loaded = False

    ports_cached = False

    rem_python_ok = None
    rem_sys_name = None

    rem_conn_fp = None
    rem_ready = False
    rem_conn_info = None

    rem_pid_ka = None  # to be able to kill the remote SSHKernelApp process
    rem_pid_k = None  # to be able to monitor and kill the remote kernel process

    rem_proc_cmds = None

    def li(self, msg: str, *args, **kwargs):
        self.log.info(f"{G}{self.log_prefix}{N}{msg}", *args, **kwargs)

    def ld(self, msg: str, *args, **kwargs):
        self.log.debug(f"{E}{self.log_prefix}{N}{msg}", *args, **kwargs)

    def lw(self, msg: str, *args, **kwargs):
        self.log.warning(f"{W}{self.log_prefix}{N}{msg}", *args, **kwargs)

    def le(self, msg: str, *args, **kwargs):
        self.log.error(f"{R}{self.log_prefix}{N}{msg}", *args, **kwargs)

    def ls(self, msg: str = "", cmd: Optional[List[str]] = None, *args, **kwargs):
        """A shortcut to log at the info level if ssh_verbose is enabled."""
        cmd_str = f"cmd: {' '.join(cmd)}" if cmd else ""
        if self.ssh_verbose:
            self.log.info(f"{C}{self.log_prefix}{N}{cmd_str}{N}{msg}", *args, **kwargs)
        else:
            self.log.debug(f"{C}{self.log_prefix}{N}{cmd_str}{N}{msg}", *args, **kwargs)

    async def extract_from_process_pipes(
        self, process: subprocess.Popen, line_handlers: List[Callable[[str], bool]]
    ):
        handlers_done, len_handlers = set(), len(line_handlers)
        # TODO: perhaps refactor this code to use asyncio subprocesses OR
        # Thread + Queue. So far, for our usage we always need to wait for the output
        # of the processes in order to extract the information we need to fully
        # launch the remote kernel. However this blocks the event loop of Jupyter.
        # https://docs.python.org/3/library/asyncio-subprocess.html#asyncio-subprocess
        # https://lucadrf.dev/blog/python-subprocess-buffers/#another-but-better-solution
        # https://stackoverflow.com/a/4896288
        while process.poll() is None:
            # ! this is a blocking call when there are not lines to read, might become
            # ! an infinite loop waiting for output to read.
            for line in process.stdout:  # type: ignore
                line = line.strip()
                if not line:
                    continue
                self.ld(f"[Process {process.pid}] stdout/stderr: {line}")
                # When enabling --ssh-verbose=vvv it can mess up the extraction
                # because sometimes it prints the commands it is executing on the remote
                if RGX_SSH_LOGS.match(line):
                    continue
                for i, line_handler in enumerate(line_handlers):
                    if i in handlers_done:
                        continue
                    if line_handler(line):
                        handlers_done.add(i)
                        self.ld(f"[Process {process.pid}] line handler {i} done.")
                if len(handlers_done) == len_handlers:
                    self.ld(f"[Process {process.pid}] all handlers done.")
                    return
            await asyncio.sleep(0.01)
        self.le(
            f"Process {process.pid} exited with code {process.returncode} before all "
            f"handlers done, {handlers_done = }"
        )

    def extract_rem_sys_info_handler(self, line: str):
        match = RGX_UNAME_PREFIX.search(line)
        if match:
            uname = match.group(1)
            self.li(f"Remote uname: {uname}")
            self.rem_sys_name = uname.split(None, 1)[0]
            return True
        return False

    def extract_rem_python_ok_handler(self, line: str):
        match = RGX_PY_CHECK.search(line)
        if match:
            self.rem_python_ok = match.group(1) == "0"
            self.ld(f"Remote python executable status: {self.rem_python_ok}")
            return True
        return False

    def extract_rem_pid_ka_handler(self, line: str):
        match = RGX_PID_KA.search(line)
        if match:
            self.rem_pid_ka = int(match.group(1))
            self.li(f"Remote SSHKernelApp expected to run with RPID={self.rem_pid_ka}")
            return True
        return False

    def extract_rem_conn_fp_handler(self, line: str):
        match = RGX_CONN_FP.search(line)
        if match:
            rem_conn_fp_new = match.group(1)
            if self.rem_conn_fp and rem_conn_fp_new != self.rem_conn_fp:
                # Don't raise, if the remote kernel dies, KernelManager restarts it,
                # code might be in a strange state.
                self.lw(
                    f"Unexpected remote connection file path "
                    f"{rem_conn_fp_new = } != {self.rem_conn_fp = }."
                )
            self.rem_conn_fp = rem_conn_fp_new
            self.li(f"Connection file on remote machine: {self.rem_conn_fp}")
            return True
        return False

    def extract_rem_ready_handler(self, line: str):
        if RGX_CONN_CLIENT.search(line):
            self.rem_ready = True
            self.ld("Remote kernel ready.")
            return True
        return False

    async def extract_from_kernel_launch(self, process: subprocess.Popen):
        """
        Extract the remote process PID, connection file path, etc..

        When executing the remote `sshpyk-kernel --kernel ...` it prints to stderr:
        ```
        [SSHKernelApp] [SSHPYK123] Connection file: /path/to/the/connection_file.json
        [SSHKernelApp] [SSHPYK123] To connect a client: --existing connection_file.json
        ```
        """
        self.rem_ready = False  # reset
        self.rem_ka_size = None  # reset
        self.rem_python_ok = None  # reset
        self.rem_ka_ok = None  # reset
        self.ld("Waiting for remote connection file path")
        try:
            future = self.extract_from_process_pipes(
                process=process,
                line_handlers=[
                    self.extract_rem_sys_info_handler,
                    self.extract_rem_python_ok_handler,
                    self.extract_rem_pid_ka_handler,
                    self.extract_rem_conn_fp_handler,
                    self.extract_rem_ready_handler,
                ],
            )
            await asyncio.wait_for(future, timeout=self.launch_timeout)
            rc = process.returncode
            if rc is not None and rc != 0:
                cmd = [*self.ssh_base_help, self.ssh_host_alias]  # type: ignore
                msg = (
                    f"Failed to launch the remote kernel, process exit code {rc}. "
                    f"Make sure you can connect manually (w/out any passwords): "
                    f"'{' '.join(cmd)}'"
                )
                self.le(msg)
                raise RuntimeError(msg)
        except TimeoutError as e:
            msg = f"Timed out waiting {self.launch_timeout}s for remote kernel launch."
            self.le(msg)
            raise RuntimeError(msg) from e

        if not self.rem_sys_name:
            cmd = [*self.ssh_base_help, self.ssh_host_alias]  # type: ignore
            msg = (
                "Could not extract remote system name. "
                f"Make sure you can connect manually (w/out any passwords): "
                f"'{' '.join(cmd)}'"
            )
            self.le(msg)
            raise RuntimeError(msg)

        if not self.rem_pid_ka:
            msg = "Could not extract PID of remote process"
            self.le(msg)
            raise RuntimeError(msg)

        if not self.rem_conn_fp:
            msg = "Could not extract connection file path on remote"
            self.le(msg)
            raise RuntimeError(msg)

        try:
            Path(self.rem_conn_fp)  # should raise if not valid
        except Exception as e:
            msg = f"Unexpected remote connection file path {self.rem_conn_fp}."
            self.le(msg)
            raise RuntimeError(msg) from e

    async def fetch_remote_connection_info(self):
        try:
            await self.check_control_sockets()
            # For details on the way the processes are spawned see the Popen call in
            # pre_launch().
            cmd = [
                *self.ssh_base,
                self.ssh_host_alias,
                f"echo {PID_KERNEL}=$(pgrep -P {self.rem_pid_ka}); "
                # print the connection file on a single line prefixed with a string
                # so that we can parse it later
                # ! `echo -n` is not supported on all systems, use `printf` instead.
                + f"printf '{CONN_INFO}='; "
                # `r` string is raw string so that python does not interpret the `\`
                + rf'cat "{self.rem_conn_fp}" | tr -d "\n" && echo ""',
            ]
            self.ld(f"Fetching remote connection file/kernel PID {cmd = }")
            self.ls(cmd=cmd)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.PIPE,
                start_new_session=self.independent_local_processes,
            )
            std_out, _std_err = await proc.communicate()
            output = std_out.decode().strip()
            # ! The remote machine might print some garbage welcome messages (e.g.
            # ! by using the `ForceCommand` directive in `/etc/ssh/sshd_config`).
            lines_raw = output.splitlines()
            lines = (line.strip() for line in lines_raw)
            for line in lines:
                if not line:
                    continue
                match = RGX_CONN_INFO.search(line)
                if match:
                    self.rem_conn_info = json.loads(match.group(1))
                    self.ld(f"Connection info remote: {self.rem_conn_info}")
                match = RGX_PID_KERNEL.search(line)
                if match:
                    self.rem_pid_k = int(match.group(1))
                    self.li(f"Remote kernel launched, RPID={self.rem_pid_k}")

            if not self.rem_pid_k:
                msg = f"Could not extract remote kernel PID from {lines_raw}"
                self.le(msg)
                raise RuntimeError(msg)
            if not self.rem_conn_info:
                msg = f"Could not extract connection info from {lines_raw}"
                self.le(msg)
                raise RuntimeError(msg)
        except json.JSONDecodeError as e:
            msg = f"Failed to parse remote connection file '{output}': {e}"
            self.le(msg)
            raise RuntimeError(msg) from e
        except ValueError as e:  # must come after JSONDecodeError
            msg = f"Failed to parse remote kernel PID '{output}': {e}"
            self.le(msg)
            raise RuntimeError(msg) from e
        except Exception as e:
            try:
                ec = f"{e.__class__.__name__}: "
            except Exception:
                ec = ""
            msg = f"Failed to fetch remote connection file: {ec}'{e}'"
            self.le(msg)
            raise RuntimeError(msg) from e

    def pick_kernel_local_ports(self):
        """Find available ports on local machine for all kernel channels."""
        km = self.parent  # KernelManager
        if not km.cache_ports:
            self.le(
                f"Unexpected {km.cache_ports = }! Your system is likely not supported."
            )
        # This part is inspired from LocalProvisioner.pre_launch where it seems to be
        # a temporary thing because the division of labor is not clear.
        # NOTE: there is a race condition on ports (from other processes on the local
        # machine), known issue: https://github.com/jupyter/jupyter_client/issues/487
        if self.cf_loaded:
            # If we have loaded the connection file, the KernelManager has the ports.
            ports = {p_name: getattr(km, p_name) for p_name in PNAMES}
            if not all(ports.values()):
                raise RuntimeError(
                    f"Unexpected {self.cf_loaded = } but {ports = } not all set."
                )
            return
        if not self.ports_cached:
            # Find available ports on local machine for all channels.
            # These are the ports that the local kernel client will connect to.
            # These ports are SSH-forwarded to the remote kernel.
            lpc = LocalPortCache.instance()
            for port_name in PNAMES:
                p = lpc.find_available_port(km.ip)
                setattr(km, port_name, p)
            self.ports_cached = True

    async def make_kernel_tunnels_args(self):
        """Create SSH tunnel arguments for kernel communication."""
        if not self.rem_conn_info:
            raise RuntimeError(f"Unexpected {self.rem_conn_info = }.")
        tunnels, km = [], self.parent
        for port_name in PNAMES:
            local_port = getattr(km, port_name)
            remote_port = self.rem_conn_info[port_name]
            tunnels += ["-L", f"{local_port}:localhost:{remote_port}"]

        old_tunnels = self.kernel_tunnels_args
        # When restarting the kernel, the ports on remote will often change
        if old_tunnels and tuple(old_tunnels) != tuple(tunnels):
            await self.close_tunnels(old_tunnels)
        self.kernel_tunnels_args = tunnels

    def load_connection_file(self):
        """
        Load connection file on local machine if it exists.
        This is to support launching this kernel "externally" from other apps e.g.
        VS Code. These apps often rely on creating a connection file themselves and then
        passing it in by formatting the argv specified in the kernel.json.
        """
        km = self.parent  # KernelManager
        self.cf_loaded = False
        cf = getattr(km, "connection_file", None)
        if not cf:
            return
        cf = Path(cf).resolve()
        if cf.is_file():
            # loads transport, ip, ports, key and signature_scheme
            # in KernelManager/Session
            km.load_connection_file(cf)  # type: ignore
            self.cf_loaded = True

    def make_base_ssh_command(self):
        if not self.ssh:
            raise RuntimeError(f"Unexpected {self.ssh = }")

        self.ssh_base = [self.ssh]

        # BatchMode is expected to be set to 'yes' in the config, must be overridden
        # otherwise it is hard to figure out if some interactive input is required.
        # That can happen when, e.g., a password prompt is required.
        self.ssh_base_help = [self.ssh, "-vvv", "-o", "BatchMode=no"]

        if self.ssh_verbose:
            arg = f"-{self.ssh_verbose}"
            self.ld(f"SSH verbose level: {arg}")
            self.ssh_base.append(arg)

        if self.ssh_config:
            fp = Path(self.ssh_config).resolve()
            if not fp.is_file():
                raise RuntimeError(
                    f"SSH config file {fp} does not exit! "
                    "Create the file or edit your sshpyk kernel."
                )
            self.ssh_base += ["-F", self.ssh_config]
            # Quote the path to avoid issues with spaces in the path if the user runs
            # the command from a shell.
            self.ssh_base_help += ["-F", f"'{self.ssh_config}'"]

        self.li(f"Base local ssh command: {' '.join(self.ssh_base)}")

    def pre_launch_init(self):
        """Initialize the provisioner on the first launch."""
        # ! REMINDER: this method is NOT called on kernel restarts.

        # Initialize locks, these are used to avoid race conditions
        # between the different async methods that jupyter_client calls.
        # This was introduced mainly because JupyterLab would often try to restart the
        # kernel while it was already shutting down or restarting.
        self._fetch_remote_processes_lock = asyncio.Lock()
        self._poll_lock = asyncio.Lock()
        self._cleanup_lock = asyncio.Lock()
        self._launch_lock = asyncio.Lock()
        self._ensure_tunnels_lock = asyncio.Lock()
        self.log_prefix = f"[{LOG_NAME}{str(id(self))[-3:]}] "
        k_conf = self.ssh_host_alias, self.remote_python, self.remote_kernel_name
        if not all(k_conf):
            raise ValueError("Bad kernel configuration.")

        if self.popen_procs is None:
            self.popen_procs: Dict[int, subprocess.Popen] = {}  # Dict[pid, Popen]
        if self.rem_proc_cmds is None:
            self.rem_proc_cmds: Dict[int, str] = {}  # Dict[pid, cmd]

        if self.existing and self.persistent_file:
            msg = f"Specify only {self.existing = } or {self.persistent_file = }"
            raise RuntimeError(msg)

        self.persistent = bool(self.persistent_file) or self.persistent

        if self.existing:
            fp = find_persistent_file(self.existing)
            if not fp:
                raise RuntimeError(f"Persistent info file {fp} not found")
            self.li(f"Using existing persistent info file {fp}")
            self.persistent_file = fp
            with open(fp) as f:
                info = json.load(f)
            self.ld(f"Persistent info {info = }")
            self.load_persistent_info(info)

        if not self.persistent_file:
            # Store the persistent file in the Jupyter runtime directory by default,
            # it is already managed by jupyter in a secure way.
            fp = Path(jupyter_runtime_dir())
            fp = fp / f"{SSHPYK_PERSISTENT_FP_BASE}-{uuid.uuid4()}.json"
            if fp.exists():
                self.ld("Persistent file exists. It will be overwritten.")
            self.persistent_file = str(fp)
            self.ld(f"{self.persistent_file = }")
        self.ld(f"{self.existing = }, {self.persistent = }, {self.persistent_file = }")

        self.ssh = verify_local_ssh(
            self.ssh, log=self.log, name="ssh", lp=self.log_prefix
        )

        self.make_base_ssh_command()

        configs = get_local_ssh_configs(
            self.ssh_base, alias=self.ssh_host_alias, log=self.log, lp=self.log_prefix
        )
        for config in configs:
            valid = validate_ssh_config(config)
            host = config["host"]
            for k, (v, msg) in valid.items():
                if v == "error":
                    self.le(f"[Local ssh config for {host}] {k}: {msg}")
                    raise RuntimeError(
                        f"Invalid config for host alias {host!r}: {k}: {msg} "
                        "Verify your local ssh config (e.g., ~/.ssh/config)."
                    )
                elif v == "warning":
                    self.lw(f"[Local ssh config for {host}] {k}: {msg}")
                else:
                    # `({v})` will be "ok" or "info"
                    self.li(f"[Local ssh config for {host}] ({v}) {k}: {msg}")

        # Reverse them so that we check the control sockets in the same orders as the
        # connections are established.
        self.all_hosts = [config["host"] for config in reversed(configs)]

        if self.parent is None:
            raise RuntimeError("Parent KernelManager not set")

        # ! REMINDER: this method is NOT called on kernel restarts.

    async def has_control_socket(self, alias: str, log_error: bool = False):
        cmd = [*self.ssh_base, "-O", "check", alias]
        self.ld(f"Checking local control socket for '{alias}': {cmd = }")
        self.ls(cmd=cmd)
        process = subprocess.Popen(  # noqa: S603
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            start_new_session=self.independent_local_processes,
            bufsize=1,
            universal_newlines=True,
        )
        self.popen_procs[process.pid] = process
        process.stdin.close()
        for k in ("stdout", "stderr"):
            for line in getattr(process, k):
                line = line.strip()
                if not line:
                    continue
                self.ld(f"[Process {process.pid}] {k}: {line}")
        await self.terminate_popen(process)
        self.ld(f"[Process {process.pid}] exit code: {process.returncode}")
        socket_ok = process.returncode == 0
        if socket_ok:
            self.ld(f"Host '{alias}' has a control socket.")
        else:
            msg = f"Host '{alias}' does not have a control socket."
            if log_error:
                self.le(msg)
            else:
                self.ld(msg)
        return socket_ok

    async def check_control_sockets(self, raise_on_failed_open: bool = False):
        for host in self.all_hosts:
            if not isinstance(host, str):
                raise RuntimeError(f"Invalid host alias: {host!r}")
            socket_ok = await self.has_control_socket(host)

            if socket_ok:
                continue

            # Try to open the control socket (a few times) by verifying the connection.
            # Since we require ControlMaster=auto, ssh will automatically create the
            # control socket if it is not present.
            ok = False
            for _ in range(3):
                ok, _ = verify_ssh_connection(
                    ssh=self.ssh_base,
                    host_alias=host,
                    log=self.log,
                    lp=self.log_prefix,
                    start_new_session=self.independent_local_processes,
                    timeout=self.launch_timeout,
                )
                if ok:
                    # double check that the control socket is opened
                    await self.has_control_socket(host)
                    break
                await asyncio.sleep(0.2)

            if ok:
                continue  # continue to the next host

            cmd = [*self.ssh_base_help, host]
            msg = (
                f"Failed to open control socket for {host!r}. "
                "Make sure you can connect manually (w/out any passwords) "
                f"to {host!r}: '{' '.join(cmd)}'"
            )

            if not raise_on_failed_open:
                self.lw(msg)
                continue

            self.le(msg)
            raise RuntimeError(msg)

    async def pre_launch(self, **kwargs: Any) -> Dict[str, Any]:
        """
        Prepare for kernel launch.

        NB do to the connection file being overwritten on the remote machine by the
        sshpyk-kernel command, this function ACTUALLY launches the remote kernel
        and later in launch_kernel() it sets up the SSH tunnels.
        """
        if not self.restart_requested:
            self.pre_launch_init()
        await self._launch_lock.acquire()  # type: ignore

        if not self.restart_requested:
            # loads local ip/ports/key/etc into KernelManager/Session
            self.load_connection_file()

        # Tries to open the control socket (a few times).
        # If any control socket fails it will raise an error.
        # We only raise during a kernel launch (or connection) attempt and not on every
        # ssh command because the problem might be a temporary network connection issue
        # and not a problem with the ssh configuration.
        await self.check_control_sockets(raise_on_failed_open=True)

        if not self.existing:
            await self.launch_remote_kernel()

        # Keep it outside `launch_remote_kernel()` because these PIDs can be loaded
        # from a persistent file.
        if not self.rem_pid_ka or not self.rem_pid_k:
            msg = f"Unexpected RPIDs: {self.rem_pid_ka = }, {self.rem_pid_k = }"
            self.le(msg)
            raise RuntimeError(msg)

        for _ in range(5):  # Try a few times for robustness
            pids = [self.rem_pid_ka, self.rem_pid_k]
            success, proc_info = await self.fetch_remote_processes_info(pids)
            if not success:
                await asyncio.sleep(0.2)
                continue
            if not self.rem_pid_k or not self.rem_pid_ka:
                self.le(
                    "Remote processes not found on remote system unexpectedly. "
                    f"RPIDs: {self.rem_pid_ka = }, {self.rem_pid_k = }"
                )
            break
        else:
            raise RuntimeError(
                "Failed to fetch remote processes info. "
                "Some processes might be still running on the remote system. "
                f"RPIDs: {self.rem_pid_ka}, {self.rem_pid_k}"
            )

        # It should not happen but if only the SSHKernelApp is dead while the kernel
        # process is still running, we proceed.
        if self.rem_pid_k not in proc_info:
            raise RuntimeError(
                "Kernel process not found on remote system. "
                "A related process might be still running on the remote system "
                f"RPID={self.rem_pid_ka}, you might need to kill it manually."
            )
        elif self.rem_pid_ka not in proc_info:
            msg = "SSHKernelApp process not found on remote system. Ignoring."
            self.lw(msg)
            if self.rem_pid_ka:
                self.rem_pid_ka = None  # reset

        if not self.existing:
            self.rem_proc_cmds = {pid: info["cmd"] for pid, info in proc_info.items()}
        else:
            for pid in [self.rem_pid_ka, self.rem_pid_k]:
                cmd_current = proc_info[pid]["cmd"]
                cmd_persistent = self.rem_proc_cmds[pid]
                if cmd_current != cmd_persistent:
                    msg = (
                        f"Expected remote PID={pid} to be running {cmd_persistent} "
                        f"but got {cmd_current}. Aborting."
                    )
                    self.le(msg)
                    raise RuntimeError(msg)

        await self.ensure_tunnels()

        _ = kwargs.pop("extra_arguments", [])  # because LocalProvisioner does it

        # * In case of future bugs check if calling this is relevant for running our
        # * local commands
        # cmd = km.format_kernel_cmd(extra_arguments=extra_arguments)
        # NB `cmd` arg is passed in bc it is expected inside the KernelManager
        return await super().pre_launch(cmd=[], **kwargs)

    async def launch_remote_kernel(self):
        sig_scheme = self.parent.session.signature_scheme  # type: ignore

        km = self.parent  # KernelManager
        if not km.session.key:  # type: ignore
            self.lw("Session key not set. Generating a new one.")
            # ! ensure there is always a key
            km.session.key = new_id_bytes()  # type: ignore
        s_key = km.session.key.decode()  # type: ignore

        args_patch = [
            f"--SSHKernelApp.kernel_name={self.remote_kernel_name}",
            f"--KernelManager.transport={km.transport}",  # type: ignore
            f"--ConnectionFileMixin.Session.signature_scheme={sig_scheme}",
            # Simply specifying the connection file does not work because the
            # remote jupyter_client code overrides the contents of the connection file.
            # We preserve on restarts to avoid jupyter errors.
            # ! If we input the session key directly in plain text, then on
            # ! the remote machine you can run e.g. `ps aux | grep sshpyk-kernel`
            # ! and see the key in plain text in the command. We therefore
            # ! communicate it securely inside the patched SSHKernelApp sent via stdin.
            f"--ConnectionFileMixin.Session.key={s_key}",
            # Allow users to see the logs of the remote SSHKernelApp when enabling
            # the local `--debug`.
            "--debug",
        ]
        # When we restart the kernel use the same connection file and key, otherwise it
        # will be different and there is an unhandled exception in the local jupyter
        # server.
        # The problem comes from the jupyter_client (on remote system) that does this:
        # def write_connection_file(self) -> None:
        #     """Write connection info to JSON dict in self.connection_file."""
        #     if self._connection_file_written and os.path.exists(self.connection_file):
        #         return
        #     self.connection_file, cfg = write_connection_file(...)
        # While self._connection_file_written has initial value set to `False`.
        if self.restart_requested:
            # ! Generating the configuration file on the remote upfront did not work
            # ! because the jupyter command on the remote seemed to
            # ! override the connection ports and the key in the connection file.
            # ! Better to not interfere with the launch of the remote kernel. Let it
            # ! take care of everything as configured on the remote system.
            # Only reuse the same remote connection file on restarts.
            args_patch.append(f'--KernelManager.connection_file="{self.rem_conn_fp}"')

        # Just a line that can will echoed by the remote script and will show up in the
        # local logs if the remote script starts running.
        msg = 'print("[SSHPYK]")'
        # Use `nohup` to ensure the remote kernel is not killed when we detach from the
        # remote machine.
        # `exec` ensures the `cmd` will have the same PID output by `echo $$`.
        # For robustness print a variable name and we extract it with regex later
        cmd_parts = [
            REMOTE_INFO_LINE,
            # Print `uname` of remote system
            f'echo "{UNAME_PREFIX}=$(uname -a)"',
            f'FP_PY="{self.remote_python}"',
            'test -e "$FP_PY" && test -r "$FP_PY" && test -x "$FP_PY"',
            f'echo "{PY_CHECK}=$?"',
            # Print the PID of the remote SSHKernelApp process
            f'echo "{PID_KA}=$$"',
            # Launch the SSHKernelApp
            f"exec nohup $FP_PY -c 'import sys; {msg}; exec(sys.stdin.read())'",
        ]
        # We already checked in `pre_launch()`
        # // await self.check_control_sockets()
        cmd = "; ".join(cmd_parts)
        self.ld(f"Remote command {cmd = }")
        cmd = [
            *self.ssh_base,
            # "-t",  # ! We don't need a pseudo-tty to be allocated
            self.ssh_host_alias,
            cmd,
        ]
        self.ld(f"Local command {cmd = }")
        self.ls(cmd=cmd)
        # The way the processes are spawned is very important.
        # See launch_kernel() source code in jupyter_client for details.
        process = subprocess.Popen(  # noqa: S603
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            # Essential in order to not mess up the stdin of the local jupyter process
            # that is managing our local "fake" kernel.
            stdin=subprocess.PIPE,
            # https://docs.python.org/3.9/library/subprocess.html#subprocess.Popen
            # "If start_new_session is true the setsid() system call will be made in the
            # child process prior to the execution of the subprocess. (POSIX only)"
            # Ensures that when the jupyter server is requested to shutdown, with e.g.
            # a Ctrl-C in the terminal, our child processes are not terminated abruptly
            # causing jupyter to try to launch them again, etc..
            # As a side effect, some processes might linger around if the process of the
            # kernel manager is forcefully killed with a SIGKILL (e.g. `kill -9`).
            start_new_session=self.independent_local_processes,
            bufsize=1,  # return one line at a time
            universal_newlines=True,
        )
        self.popen_procs[process.pid] = process
        # Preserving the key is essential for kernel restarts and for
        # externally-provided connection files.
        # Communicate the session key to the remote process securely using the stdin
        # pipe of the ssh process.
        # `!r` should suffice to make it valid python code
        patch_line = f"ARGS_PATCH = {args_patch!r}"
        self.ld(f"Remote SSHKernelApp args: {' '.join(args_patch)}")
        process.stdin.write(KERNELAPP_PY.replace("ARGS_PATCH = []", patch_line))
        process.stdin.flush()
        # Close the input pipe, see launch_kernel in jupyter_client
        # When we close the input pipe, the remote process will receive EOF and the
        # SSHKernelApp will start the kernel.
        process.stdin.close()

        await self.extract_from_kernel_launch(process=process)
        # We are done with the starting the remote kernel. We know its PID to kill it
        # later. Terminate the local process.
        await self.terminate_popen(process)

        # Always fetch the remote connection info to forward to the correct ports
        await self.fetch_remote_connection_info()

    async def terminate_popen(self, process: subprocess.Popen) -> None:
        process.terminate()
        try:
            await asyncio.wait_for(
                self.wait_local(process), timeout=self.launch_timeout
            )
        except asyncio.TimeoutError:
            msg = (
                f"Local process PID={process.pid} taking too long to terminate, "
                "killing it"
            )
            self.lw(msg)
            process.kill()

        try:
            await asyncio.wait_for(
                self.wait_local(process), timeout=self.launch_timeout
            )
        except asyncio.TimeoutError:
            msg = f"Failed to kill local process PID={process.pid}, ignoring"
            self.lw(msg)

    async def open_kernel_tunnels(self):
        """Open SSH tunnels for kernel communication."""
        # ##############################################################################
        # # After picking the ports, open tunnels ASAP to minimize the chance of a race
        # # condition on local ports (from other processes on the local machine)
        alias = self.ssh_host_alias
        if not self.restart_requested:
            self.pick_kernel_local_ports()
        await self.make_kernel_tunnels_args()  # closes old tunnels if needed
        if not self.kernel_tunnels_args:
            raise RuntimeError(f"Unexpected {self.kernel_tunnels_args = }")
        await self.check_control_sockets()
        cmd = [
            *self.ssh_base,
            "-O",  # mute ssh output
            "forward",  # do nothing, i.e. maintain the tunnels alive
            *self.kernel_tunnels_args,  # ssh tunnels within the same command
            alias,
        ]
        self.ld(f"Periodic kernel tunnels check {cmd = }")
        self.ls(cmd=cmd)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            start_new_session=self.independent_local_processes,
        )
        # ##############################################################################
        std_out, _std_err = await proc.communicate()
        output = std_out.decode().strip()
        lines = []
        if output:
            lines = [line.strip() for line in output.splitlines()]

        msg = (
            f"Try again after making sure you have `ControlMaster=auto` in "
            f"under your dedicated `Host {alias}` in your ssh config "
            f"file (usually `$HOME/.ssh/config`)."
        )
        # The `ssh -O forward` command is expected to exit cleanly (code 0)
        # 255 is returned if the control socket is lost, can happen sometimes
        if proc.returncode not in (0, 255):
            msg = (
                f"Tunnels process PID={proc.pid} exited with unexpected "
                f"{proc.returncode = }. " + msg
            )
            log_func = self.le
        else:
            msg = f"Tunnels to host {alias!r} for kernel ports seem opened"
            log_func = self.ld

        cmd = [*self.ssh_base_help[:-1], "-M", "-f", "-N", alias]
        msg_warn = (
            "You might have lost the control socket (e.g. WiFi disconnected). "
            f"Make sure {alias!r} is reachable. If you had run '{' '.join(cmd)}' "
            f"to input the login password for {alias!r} "
            "before starting the kernel, you have to manually run it again."
        )
        for line in lines:
            log_func(f"[Process {proc.pid}] {line}")
            if proc.returncode != 0:
                self.lw(f"'ssh -O forward ...' said '{line}'. " + msg_warn)

        log_func(msg)

    async def close_tunnels(self, tunnels_args: List[str]):
        await self.check_control_sockets()
        cmd = [
            *self.ssh_base,
            "-O",  # mute ssh output
            "cancel",  # do nothing, i.e. maintain the tunnels alive
            *tunnels_args,  # ssh tunnels within the same command
            self.ssh_host_alias,
        ]
        self.ld(f"Closing tunnels {cmd = }")
        self.ls(cmd=cmd)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            start_new_session=self.independent_local_processes,
        )
        std_out, _std_err = await proc.communicate()
        output = std_out.decode().strip()
        if output:
            for line in output.splitlines():
                self.ld(f"[Process {proc.pid}] {line.strip()}")
        if proc.returncode != 0:
            msg = (
                f"Cancel tunnels process PID={proc.pid} exited with unexpected "
                f"{proc.returncode = }."
            )
            self.le(msg)
        else:
            self.ld(f"Tunnels {tunnels_args} to {self.ssh_host_alias} closed")

    async def launch_kernel(
        self, cmd: List[str], **kwargs: Any
    ) -> KernelConnectionInfo:
        # Fill in the rest of the connection info based on the remote connection info
        km, rci = self.parent, self.rem_conn_info  # KernelManager
        if not rci:
            raise RuntimeError(f"Unexpected {rci = }")
        # NB don't override the local kernel name. Important for extra clarity on how
        # and to which kernel we are connected. Besides that the connection file might
        # be generated by some other piece of software with a strange kernel name.
        # km.session.kernel_name = rci.get("kernel_name", "")

        # Just in case the remote SSHKernelApp did not honor the transport and
        # signature scheme specified in the command line.
        km.transport = rci["transport"]
        km.session.signature_scheme = rci["signature_scheme"]
        key_l, key_r = km.session.key, rci["key"].encode()
        if key_l and key_l != key_r:
            self.ld(f"Overriding local Session key with remote ({key_l=} vs {key_r=}")
            km.session.key = key_r

        # This if-else is here bc LocalProvisioner does it
        if "env" in kwargs:
            jupyter_session = kwargs["env"].get("JPY_SESSION_NAME", "")
            km.write_connection_file(jupyter_session=jupyter_session)
        else:
            km.write_connection_file()
        self.li(
            f"Connection file on local machine: {Path(km.connection_file).resolve()}"
        )

        self.connection_info = km.get_connection_info()
        self.ld(f"Connection info local: {self.connection_info}")

        self.patch_session_send()
        self.write_persistent_info()

        self.li("Done launching kernel")  # just to signal everything should be ready
        return self.connection_info

    async def shutdown_requested(self, restart: bool = False):
        """
        The KernelManager calls this method after the kernel was requested to shutdown.

        If all goes well the kernel process on the remote machine shuts down gracefully
        because of the shutdown message sent by the KernelManager on the kernel's
        control port.

        ! Mind that at this point the sshpyk-kernel process (SSHKernelApp) on the remote
        ! machine is still alive.

        After this, the KernelManager polls the provisioner's `self.poll()` method to
        check if the kernel process is still alive. It waits up to
        self.get_shutdown_wait_time()/2. If the kernel process is still alive
        after that time, the KernelManager calls the provisioner's `self.terminate()`
        method and polls&wait again for self.get_shutdown_wait_time()/2. If the kernel
        process is still alive after that time, the KernelManager calls the
        provisioner's `self.kill()` method as last resort.
        """
        # Cache the `restart` arg so that kill/terminate/wait are aware of it
        # See https://github.com/jupyter/jupyter_client/issues/1061 for details.
        self.restart_requested = restart
        self.li(f"shutdown_requested({restart = })")

        if self.session_send_orig:
            # Restore the original session.send to avoid infinite recursion on restarts
            self.parent.session.send = self.session_send_orig

    def get_persistent_info(self):
        """sync version of get_provisioner_info"""
        return {
            "kernel_id": self.kernel_id,
            "rem_sys_name": self.rem_sys_name,
            "rem_conn_info": self.rem_conn_info,  # we kept `key` as string
            "rem_pid_k": self.rem_pid_k,
            "rem_pid_ka": self.rem_pid_ka,
            "rem_conn_fp": self.rem_conn_fp,
            "rem_proc_cmds": self.rem_proc_cmds,
        }

    async def get_provisioner_info(self) -> Dict:
        """Get information about this provisioner instance."""
        # * This method was never called during the development of this provisioner.
        return self.get_persistent_info()

    def load_persistent_info(self, persistent_info: Dict) -> None:
        """sync version of load_provisioner_info"""
        for k, v in persistent_info.items():
            if k == "rem_proc_cmds":
                # Fix json serializing of int keys to strings
                self.rem_proc_cmds = {int(k): v for k, v in v.items()}
            else:
                setattr(self, k, v)
        self.li("Persistent info loaded")

    async def load_provisioner_info(self, provisioner_info: Dict) -> None:
        """Load information about this provisioner instance."""
        # * This method was never called during the development of this provisioner.
        return self.load_persistent_info(provisioner_info)

    def write_persistent_info(self):
        """Write the provisioner info to a file."""
        persistent_info = self.get_persistent_info()
        self.ld(f"Writing persistent info {persistent_info = }")
        with secure_write(self.persistent_file, binary=False) as f:  # type: ignore
            json.dump(persistent_info, f, indent=2)
        fp = Path(self.persistent_file)  # type: ignore
        self.li(f"Persistent file: {fp.resolve()}")
        kernel_name = self.parent.kernel_name  # type: ignore
        self.li(
            f"To provision this remote kernel again: "
            f"`sshpyk-kernel --kernel {kernel_name} --existing {fp.name}`"
        )

    def get_shutdown_wait_time(self, recommended: Optional[float] = None):
        # x2 because the KernelManager waits 1/2 this time after the shutdown request,
        # and then forces the shutdown and wait another 1/2 this time.
        return self.shutdown_time * 2  # allow the kernel spec to override

    def get_stable_start_time(self, recommended: Optional[float] = None):
        return self.launch_timeout  # allow the kernel spec to override

    async def post_launch(self, **kwargs: Any) -> None:
        """
        Called after `launch_kernel`.

        # kwargs usually contain ['cwd', 'env'], env is a dict
        """
        self._launch_lock.release()  # type: ignore
        return await super().post_launch(**kwargs)

    async def _cleanup(self, restart: bool = False) -> None:
        """Clean up resources used by the provisioner."""

        # ! REMINDER: this function might be called more than once for the same shutdown

        self.ld(f"cleanup({restart = })")
        if self.ports_cached and not restart:
            lpc = LocalPortCache.instance()
            for k, port in self.connection_info.items():
                if k.endswith("_port"):
                    lpc.return_port(int(port))  # `int` to ensure type is correct
            self.ports_cached = False

        if not self.persistent and self.persistent_file and not restart:
            fp = Path(self.persistent_file)
            try:
                fp.unlink()
                self.ld(f"Cleaned up persistent info file {fp}")
            except FileNotFoundError:
                self.lw(f"Persistent file not found: {fp}")
            self.persistent_file = ""  # reset

        if self.kernel_tunnels_args:
            try:
                await self.close_tunnels(self.kernel_tunnels_args)
            except Exception as e:
                self.le(f"Failed to close tunnels {self.kernel_tunnels_args = }: {e}")
            self.kernel_tunnels_args = None  # reset

        if self.popen_procs:
            self.ld(f"Terminating local process(es) ({restart = })")
            for _pid, p in list(self.popen_procs.items()):
                await self.terminate_popen(p)
            self.ld(f"Local process(es) terminated ({restart = })")

        # Killing the remote kernel process should have happened already either by:
        # KernelManager sending a shutdown request to the kernel's ports,
        # `terminate()` or `kill()`
        if self.rem_pid_k:
            self.lw(f"Remote kernel process RPID={self.rem_pid_k} was not killed")

        timeout = self.get_shutdown_wait_time() / 4
        if self.rem_pid_ka:
            await self.send_sigterm_to_remote(self.rem_pid_ka, SIGTERM)
            try:
                await asyncio.wait_for(self.wait_remote([self.rem_pid_ka]), timeout)
            except asyncio.TimeoutError:
                self.lw(
                    f"Timeout for remote SSHKernelApp to terminate, "
                    f"RPID={self.rem_pid_ka}. Sending SIGKILL."
                )
                await self.send_sigterm_to_remote(self.rem_pid_ka, SIGKILL)

        if self.rem_pid_ka:  # check again, it might have been cleared
            try:
                await asyncio.wait_for(self.wait_remote([self.rem_pid_ka]), timeout)
            except asyncio.TimeoutError:
                self.lw(
                    f"Timeout for remote SSHKernelApp to terminate after SIGKILL, "
                    f"RPID={self.rem_pid_ka}. Ignoring."
                )
        self.li(f"Cleanup done ({restart = })")

        if self.rem_pid_ka:
            self.lw(f"Remote SSHKernelApp RPID={self.rem_pid_ka} was likely not killed")
            self.rem_pid_ka = None

        if self.rem_pid_k:
            self.lw(f"Remote kernel RPID={self.rem_pid_k} was likely not killed")
            self.rem_pid_k = None

        # ! REMINDER: this function might be called more than once for the same shutdown

    async def cleanup(self, restart: bool = False) -> None:
        async with self._cleanup_lock:  # type: ignore
            await self._cleanup(restart)

    @property
    def has_process(self) -> bool:
        """Returns true if this provisioner is currently managing a kernel."""
        # KernelManager.has_kernel property delegates to this property
        # Among other things, it controls if `provisioner.wait()` should be called, when
        # shutting down or killing the kernel.
        return bool(self.rem_pid_k)

    def clear_remote_pids(self, pids: List[int], processes: Dict[int, Dict[str, str]]):
        """Reset RPIDs if the process are not alive anymore"""
        if self.rem_proc_cmds is None:
            self.rem_proc_cmds = {}
        for attr in ("rem_pid_k", "rem_pid_ka"):
            pid = getattr(self, attr)
            if not pid:
                continue
            if pid not in pids:
                continue

            # Process is not running anymore, reset
            if pid not in processes:
                setattr(self, attr, None)  # reset
                if pid in self.rem_proc_cmds:
                    del self.rem_proc_cmds[pid]
                self.ld(f"RPID={pid} cleared ({attr})")
                continue

            cmd = processes[pid]["cmd"]
            expected_cmd = self.rem_proc_cmds.get(pid, None)
            # Can happen both when process goes into zombie state or if by a very low
            # probability a new process was restarted with a different command and now
            # has the same PID. Either way, reset so that we don't try to kill it again.
            if expected_cmd and cmd != expected_cmd:
                if not is_zombie(processes[pid]["state"]):
                    self.lw(
                        f"Command mismatch RPID={pid} ({attr}). "
                        f"Expected '{expected_cmd}', got '{cmd}'"
                    )
                self.ld(f"RPID={pid} cleared ({attr}) {processes[pid] = }")
                setattr(self, attr, None)  # reset
                del self.rem_proc_cmds[pid]

    async def _fetch_remote_processes_info(self, pids: List[int]) -> T_PROC_INFO:
        """Fetch the state of remote processes."""
        if not all(map(int, pids)):
            raise ValueError(f"All process IDs must be integers {pids = }")
        if not pids:
            self.le("No remote process IDs to fetch")
            return True, {}
        pids_str = ",".join(map(str, pids))
        if self.rem_sys_name == "Darwin":
            comm = "command"  # ! On macOS comm/args does not display the full command
        else:  # assume unix
            comm = "args"  # ? does this displays the full command on unix?
        await self.check_control_sockets()
        cmd = [
            *self.ssh_base,
            self.ssh_host_alias,
            # print the output of ps prefixed with a string so that we can ignore all
            # the output before that
            f'echo "{PS_PREFIX}" && ps -p {pids_str} -o pid,state,{comm}',
            # ? should we make it more robust and ensure that we got the full output?
            # E.g.
            # echo '{PS_PREFIX}'; ps -p 1234; PS_RET_CODE=$?; \
            # return_code() {return $PS_RET_CODE}; echo '{PS_PREFIX}'; return_code
            # Probably this is too much of an edge case, but if not and the remote
            # process actually still exists, we might trigger a kernel restart.
        ]
        self.ld(f"Checking remote processes state {cmd = }")
        self.ls(cmd=cmd)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.PIPE,
                start_new_session=self.independent_local_processes,
            )
            std_out, _std_err = await proc.communicate()
            output = std_out.decode().strip()
            # 255 happens for example when ssh gets a `Network is unreachable`
            if proc.returncode not in (0, 1, 255):
                self.lw(
                    f"Unexpected return code {proc.returncode} from '{' '.join(cmd)}'. "
                    f"Output: '{output}'"
                )

            if proc.returncode not in (0, 1):
                return False, {}

            lines = (line.strip() for line in output.splitlines())
            lines = dropwhile(lambda line: line != PS_PREFIX, lines)
            processes = (
                dict(zip(["pid", "state", "cmd"], line.split(None, 2)))
                for line in lines
                if line and line[0].isdigit()
            )
            processes = {int(p.pop("pid")): p for p in processes}

            # Clear remote PIDs that are not running anymore
            self.clear_remote_pids(pids, processes)

            return True, processes
        except Exception as e:
            try:
                ec = f"{e.__class__.__name__}: "
            except Exception:
                ec = ""
            self.le(f"Failed to fetch remote processes state '{cmd}': {ec}{e}")
            return False, {}

    async def fetch_remote_processes_info(self, pids: List[int]) -> T_PROC_INFO:
        async with self._fetch_remote_processes_lock:  # type: ignore
            res = await self._fetch_remote_processes_info(pids)
        return res

    async def ensure_tunnels(self):
        """Ensure the tunnels are being forwarded."""
        async with self._ensure_tunnels_lock:  # type: ignore
            # When using ControlMaster multiplexing, we have no way of checking which
            # tunnels have been already forwarded. So we always re(open) the tunnels.
            # This is not a problem, it is cheap to do. And in case the master
            # connection has dies, it will be restarted (assuming not password required)
            await self.open_kernel_tunnels()

    async def _poll(self) -> Optional[int]:
        """
        Checks if kernel process is still running.
        The KernelManager calls this method regularly to check if the kernel process is
        alive. Furthermore, the KernelManager calls this method to check if the kernel
        process has terminated after a shutdown request.
        """
        # * If the launch is in progress, assume all good, avoid restart during launch.
        if self._launch_lock.locked():  # type: ignore
            return None

        if not self.rem_pid_k:
            # Tell the caller all is good so that no action against the kernel is taken.
            # This is just in case the KernelManager calls poll() while the kernel is
            # still launching/shutting down.
            self.ld(f"{self.rem_pid_k = }, let caller assume kernel is running")
            return None  # assume all good

        if not self.parent.shutting_down:  # type: ignore
            await self.ensure_tunnels()

        # ! !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
        # ! Safeguard against shutting down state change during await. This problem was
        # ! observed at least in JupyterLab. Its AsyncIOLoopKernelRestarter monitors
        # ! if the kernel is alive and if not, it will attempt to restart it
        # ! automatically. This goes wrong when: the user requested a shutdown/restart
        # ! during a .poll() call that already detects the kernel is dead.
        # ! AsyncIOLoopKernelRestarter is not aware of the more recent shutdown/restart
        # ! request and will assume the kernel is dead and requires to be restarted.
        # ! Not the desired behavior in this case.
        shutting_down = self.parent.shutting_down  # type: ignore
        success, processes = await self.fetch_remote_processes_info([self.rem_pid_k])
        shutting_down_new = self.parent.shutting_down  # type: ignore
        if not shutting_down and shutting_down_new:
            self.ld(f"Shutting down changed {shutting_down} -> {shutting_down_new}")
            # avoid restart attempts during a shutdown, tell the old caller all was good
            return None
        # ! !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!

        if not success:
            return None  # for now assume all good, let it poll again

        zombie = is_zombie(processes.get(self.rem_pid_k, {}).get("state", ""))
        # * self.rem_pid_k is reset by clear_remote_pids() if the process is not found
        is_alive = self.rem_pid_k in processes and not zombie
        # KernelManager._async_is_alive() expects None if running
        # `1` is just something different from None
        return None if is_alive else 1

    async def poll(self) -> Optional[int]:
        async with self._poll_lock:  # type: ignore
            res = await self._poll()
        return res

    async def wait_local(self, process: subprocess.Popen) -> int:
        """Wait for a local process to terminate."""
        while process.poll() is None:
            await asyncio.sleep(0.1)  # Wait for process to terminate

        # Process is no longer alive, wait and clear pipes
        ret = process.wait()
        for attr in ["stdout", "stderr", "stdin"]:
            fid = getattr(process, attr)
            if not fid:
                continue
            try:
                fid.close()  # Close file descriptor
            except BrokenPipeError:
                self.ld(f"[Process {process.pid}] BrokenPipeError when closing {attr}")
        if process.pid in self.popen_procs:
            del self.popen_procs[process.pid]
        return ret

    async def wait_remote(
        self, pids: List[int], pids_extra: Optional[List[int]] = None
    ):
        """Wait for the remote process(es) to terminate."""
        pids = [pid for pid in pids if pid is not None]
        pids_extra = [pid for pid in (pids_extra or []) if pid is not None]
        if not pids:
            self.ld("No RPIDs to wait for")
            return
        while True:
            pids_fetch = pids + pids_extra
            success, processes = await self.fetch_remote_processes_info(pids_fetch)
            if not success:
                continue  # ignore, let it try again, handle better later if needed
            pids_alive = [
                pid
                for pid in pids
                if pid in processes and not is_zombie(processes[pid]["state"])
            ]
            if not pids_alive:
                return
            await asyncio.sleep(0.1)

    async def wait(self):
        """
        Called by the KernelManager with the intent to wait for the kernel process to
        terminate after it is not alive anymore.
        """
        self.ld("Waiting for remote kernel to shutdown")
        # ! Don't wait here for any of the ssh processes, these might still be relevant
        # ! for graceful shutdown.
        if not self.rem_pid_k:
            return
        # No need for timeout, KernelManager already has it

        # Since we are calling the remote machine anyway, fetch the status of the remote
        # SSHKernelApp process as well, if it is not running we won't have to kill it.
        pids_extra = [self.rem_pid_ka] if self.rem_pid_ka else None
        await self.wait_remote([self.rem_pid_k], pids_extra=pids_extra)
        self.ld("Remote kernel shutdown complete")

    async def send_signal(self, signum: int) -> None:
        """
        Sends signal identified by signum to the kernel process.

        NB this method was never called during the development of this provisioner.
        This is expected since we are using `"interrupt_mode": "message"` in our spec.
        """
        self.lw(f"Unexpected `send_signal` call ({signum = })")

    async def verify_remote_process(self, pid: int) -> RProcResult:
        """Verify the process exists and matches expected command."""
        success, processes = await self.fetch_remote_processes_info([pid])
        if not success:
            return RProcResult.FETCH_FAILED
        if pid not in processes:
            self.ld(f"Process {pid} not found on remote system")
            return RProcResult.PROCESS_NOT_FOUND
        return RProcResult.OK

    async def kill(self, restart: bool = False) -> None:
        """
        Intended to kill the kernel process. This is called by the KernelManager
        when a graceful shutdown of the kernel fails/times out, or when the
        KernelManager requests an immediate shutdown.
        """
        restart = self.restart_requested or restart
        self.lw(f"kill({restart = })")
        if self.rem_pid_k:
            await self.send_sigterm_to_remote(self.rem_pid_k, SIGKILL)

    async def send_signal_to_remote_process(self, pid: int, signum: int) -> RProcResult:
        if pid is None:  # to protect development mistakes
            raise ValueError("No remote process ID to send signal to")

        if signum not in (SIGINT, SIGTERM, SIGKILL):
            raise ValueError(f"Invalid signal number {signum}")
        try:
            await self.check_control_sockets()
            cmd = [*self.ssh_base, self.ssh_host_alias, f"kill -{signum} {pid}"]
            self.ld(f"Sending signal {signum} to remote process, RPID={pid}, {cmd = }")
            self.ls(cmd=cmd)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.PIPE,
                start_new_session=self.independent_local_processes,
            )
            std_out, _std_err = await proc.communicate()
            output = std_out.decode().strip()
            self.ld(f"Sent signal {signum} to remote process, RPID={pid}")
            if proc.returncode == 1:
                self.ld(
                    f"Signal {signum} sent to remote process, RPID={pid}, "
                    f"but process not found ('{output}')"
                )
                self.clear_remote_pids([pid], {})
                return RProcResult.PROCESS_NOT_FOUND
            elif proc.returncode != 0:
                self.le(
                    f"Failed to send signal {signum} to remote process, RPID={pid}, "
                    f"{proc.returncode = }: '{output}'"
                )
                return RProcResult.SIGNAL_FAILED
            return RProcResult.OK
        except Exception as e:
            self.log.exception(e)
            self.lw(
                f"Failed to send signal {signum} to remote process, RPID={pid}: {e}"
            )
            return RProcResult.SIGNAL_FAILED

    async def send_sigterm_to_remote(self, pid: int, signum: int, attempts: int = 5):
        """Terminate the remote process with the given signal."""
        # Can't verify the command of the process, simply send signal and continue
        if not self.rem_proc_cmds or pid not in self.rem_proc_cmds:
            for _ in range(attempts):
                res = await self.send_signal_to_remote_process(pid, signum)
                if res in (RProcResult.OK, RProcResult.PROCESS_NOT_FOUND):
                    break
                if res == RProcResult.SIGNAL_FAILED:
                    continue
            return

        # Do a careful verification of the remote process before sending sign
        for _ in range(attempts):
            res = await self.verify_remote_process(pid)
            if res == RProcResult.FETCH_FAILED:
                continue
            break

        if res == RProcResult.FETCH_FAILED:
            self.lw(
                f"Failed to verify remote process RPID={pid}, "
                f"continuing with sending signal {signum}"
            )
        elif res == RProcResult.PROCESS_NOT_FOUND:
            self.ld(
                f"Process RPID={pid} not found on remote system, "
                f"sending signal {signum} skipped"
            )
            return

        for _ in range(attempts):
            res = await self.send_signal_to_remote_process(pid, signum)
            if res in (RProcResult.OK, RProcResult.PROCESS_NOT_FOUND):
                break
            if res == RProcResult.SIGNAL_FAILED:
                continue

        # # After terminate is called, the KernelManager will call self.wait()
        # # which will wait for the remote process to terminate.

    async def terminate(self, restart: bool = False) -> None:
        """Terminates the remote kernel and SSHKernelApp."""
        # # This method is called by the KernelManager after a graceful shutdown of the
        # # kernel did not complete within the timeout.
        # ! Due to what seems to be a bug in jupyter_client <= 8.6.3, `restart` is
        # ! always passed as `False`.
        # ! https://github.com/jupyter/jupyter_client/issues/1061
        self.ld(f"terminate({restart = })")
        restart = self.restart_requested or restart
        if self.rem_pid_k:
            await self.send_sigterm_to_remote(self.rem_pid_k, SIGTERM)

    def session_send(self, stream, msg_or_type, *args, **kwargs):
        if isinstance(msg_or_type, dict):
            if msg_or_type.get("msg_type", None) == "shutdown_request":
                restart = msg_or_type.get("content", {}).get("restart", False)
                if self.persistent and not restart:
                    self.li(
                        "Intercepted shutdown_request from KernelManager. "
                        "Remote kernel will persist."
                    )
                    # Make the SSHProvisioner forget about the remote kernel and
                    # proceed with the local clean up
                    self.rem_pid_k = None
                    self.rem_pid_ka = None
                    return
        # For all other cases run as usual
        return self.session_send_orig(stream, msg_or_type, *args, **kwargs)

    def patch_session_send(self):
        """
        This patch is required because the KernelManager (self.parent) sends a shutdown
        message itself to the control port of the remote kernel BEFORE the provisioner
        can act on it. The provisioner only gets informed, i.e. provisioner's
        shutdown_requested is called, AFTER the message has been sent to the kernel.

        This patch intercepts that shutdown message and does not relay it to the remote
        kernel when shuting down (restart=False) and self.persistent = True.
        """
        if self.parent.session.send is not self.session_send:
            self.session_send_orig = self.parent.session.send
            self.parent.session.send = self.session_send
            self.ld(
                "Parent session.send patched to prevent remote kernel shutdown when "
                "persistent=True"
            )
