"""SSH Kernel Provisioner implementation for Jupyter Client."""

import asyncio
import json
import re
import subprocess
from functools import reduce
from pathlib import Path
from subprocess import PIPE, Popen, run
from typing import Any, Callable, Dict, List, Optional

from jupyter_client.connect import KernelConnectionInfo, LocalPortCache
from jupyter_client.provisioning.provisioner_base import KernelProvisionerBase
from traitlets import Integer, Unicode

from .utils import (
    verify_local_ssh,
    verify_rem_executable,
    verify_ssh_connection,
)

RGX_CONN_FP = re.compile(r"\[KernelApp\].*file: (.*\.json)")
RGX_CONN_CLIENT = re.compile(r"\[KernelApp\].*client: (.*\.json)")
PID_PREFIX_KERNEL = "KERNEL_APP_PID="
RGX_PID_KERNEL = re.compile(rf"{PID_PREFIX_KERNEL}(\d+)")
REM_SESSION_KEY_NAME = "SSHPYK_SESSION_KEY"
# extracted from jupyter_client/kernelspec.py
RGX_KERNEL_NAME = re.compile(r"^[a-z0-9._-]+$", re.IGNORECASE)
RGX_SSH_HOST_ALIAS = re.compile(r"^[a-z0-9_-]+$", re.IGNORECASE)


class SshHost(Unicode):
    def validate(self, obj, value):
        value = super().validate(obj, value)
        try:
            if not RGX_SSH_HOST_ALIAS.match(value):
                raise ValueError(
                    f"Invalid SSH host alias {value!r}. "
                    f"Must match this pattern {RGX_SSH_HOST_ALIAS.pattern}. "
                    "Verify that it is defined in your local SSH config file."
                )
            return value
        except UnicodeEncodeError:
            self.error(obj, value)


class UnicodePath(Unicode):
    def validate(self, obj, value):
        value = super().validate(obj, value)
        try:
            Path(value)  # should raise if not a valid path
            return value
        except:
            self.error(obj, value)
            raise


class KernelName(Unicode):
    def validate(self, obj, value):
        # value = super().validate(obj, value) # not needed since we use regex
        try:
            if not RGX_KERNEL_NAME.match(value):
                raise ValueError(f"Invalid kernel name {value!r}")
            return value
        except:
            self.error(obj, value)
            raise


LOG_PREFIX = "[SSHPYK] "


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
    remote_python_prefix = UnicodePath(
        config=True,
        help="Path to Python prefix on remote system. "
        "Run `python -c 'import sys; print(sys.prefix)'` on the remote system "
        "to find the path. If the remote kernel is part of a virtual environment, "
        "first activate your virtual environment and then query the `sys.prefix`. "
        "It must have jupyter_client package installed.",
        allow_none=False,
    )
    remote_kernel_name = KernelName(
        config=True,
        help="Kernel name on the remote system "
        "(i.e. first column of `jupyter kernelspec list` on the remote system).",
        allow_none=False,
    )
    remote_kernel_launch_timeout = Integer(
        default_value=60,
        config=True,
        help="Timeout for launching the remote kernel through the ssh command.",
        allow_none=False,
    )
    ssh = Unicode(
        config=True,
        help="Path to SSH executable. "
        "If None, will be auto-detected using 'which ssh'.",
        allow_none=True,
        default_value=None,
    )

    processes = None
    pid_tunnels = None

    cf_loaded = False

    ports_cached = False

    rem_python = None
    rem_jupyter = None

    rem_conn_fp = None
    rem_ready = False
    rem_conn_info = None

    rem_pid = None  # to be able to kill the remote KernelApp process

    def li(self, msg: str, *args, **kwargs):
        self.log.info(f"{LOG_PREFIX}{msg}", *args, **kwargs)

    def ld(self, msg: str, *args, **kwargs):
        self.log.debug(f"{LOG_PREFIX}{msg}", *args, **kwargs)

    def lw(self, msg: str, *args, **kwargs):
        self.log.warning(f"{LOG_PREFIX}{msg}", *args, **kwargs)

    def le(self, msg: str, *args, **kwargs):
        self.log.error(f"{LOG_PREFIX}{msg}", *args, **kwargs)

    def extract_from_process_pipes(
        self, process: Popen, line_handlers: List[Callable[[str], bool]], timeout: int
    ):
        handlers_done, len_handlers = set(), len(line_handlers)
        # NOTE: perhaps we should refactor this code to use asyncio subprocesses OR
        # Thread + Queue. So far for our usage we always need to wait for the output
        # of the processes in order to extract the information we need to fully
        # launch the remote kernel. However this might block the event loop of Jupyter.
        # https://docs.python.org/3/library/asyncio-subprocess.html#asyncio-subprocess
        # https://lucadrf.dev/blog/python-subprocess-buffers/#another-but-better-solution
        # https://stackoverflow.com/a/4896288
        while process.poll() is None:
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                self.ld(f"[Process {process.pid}]: {line}")
                for i, line_handler in enumerate(line_handlers):
                    if i in handlers_done:
                        continue
                    if line_handler(line):
                        handlers_done.add(i)
                        self.ld(f"[Process {process.pid}] line handler {i} done.")
                if len(handlers_done) == len_handlers:
                    self.ld(f"[Process {process.pid}] all handlers done.")
                    return
        raise RuntimeError(f"Process {process.pid} exited before all handlers done.")

    def extract_rem_info_handler(self, line: str):
        if not self.rem_pid:
            match = RGX_PID_KERNEL.search(line)
            if match:
                self.rem_pid = int(match.group(1))
                return False

        if not self.rem_conn_fp:
            match = RGX_CONN_FP.search(line)
            if match:
                rem_conn_fp_new = match.group(1)
                if self.rem_conn_fp is None:
                    self.rem_conn_fp = rem_conn_fp_new
                    return False
                elif rem_conn_fp_new != self.rem_conn_fp:
                    raise RuntimeError(
                        f"Unexpected remote connection file path "
                        f"{rem_conn_fp_new = } != {self.rem_conn_fp = }."
                    )

        if not self.rem_ready:
            if RGX_CONN_CLIENT.search(line):
                self.rem_ready = True

        if self.rem_pid and self.rem_conn_fp and self.rem_ready:
            return True
        return False

    def extract_rem_pid_and_connection_fp(
        self, process: Popen, cmd: List[str], timeout: int
    ):
        """
        Extract the remote process PID and connection file path.

        When executing the `jupyter-kernel --KernelApp.kernel_name=...` it prints
        to stderr:
        ```
        [KernelApp] Starting kernel 'python3'
        [KernelApp] Connection file: /some/path/to/the/connection_file.json
        [KernelApp] To connect a client: --existing connection_file.json
        ```

        TODO: Relying on the logs on the remote process is potentially fragile. For
        robustness, we should execute a script on remote and print the information in
        a JSON that can be parsed.
        """
        self.rem_ready = False  # reset
        self.ld(f"Waiting for remote connection file path from {cmd = } ({timeout = })")
        try:
            self.extract_from_process_pipes(
                process=process,
                line_handlers=[self.extract_rem_info_handler],
                timeout=timeout,
            )
        except TimeoutError as e:
            msg = f"Timed out ({timeout}s) waiting for connection file information."
            self.le(msg)
            raise RuntimeError(msg) from e

        if not self.rem_pid:
            msg = f"Could not extract PID of remote process during {cmd = }"
            self.le(msg)
            raise RuntimeError(msg)

        if not self.rem_conn_fp:
            msg = f"Could not extract connection file path on remote during {cmd = }"
            self.le(msg)
            raise RuntimeError(msg)

        try:
            Path(self.rem_conn_fp)  # should raise if not valid
        except Exception as e:
            msg = f"Unexpected remote connection file path {self.rem_conn_fp}."
            self.le(msg)
            raise RuntimeError(msg) from e

    def fetch_remote_connection_info(self) -> Dict[str, Any]:
        try:
            # For details on the way the processes are spawned see the Popen call in
            # pre_launch().
            cmd = [self.ssh, "-q", self.ssh_host_alias, f"cat {self.rem_conn_fp!r}"]
            self.ld(f"Fetching remote connection file {cmd = }")
            result = run(  # noqa: S603
                cmd,
                stdout=PIPE,
                stderr=subprocess.DEVNULL,
                stdin=PIPE,
                check=True,
                start_new_session=True,
            )  # type: ignore
            rci = json.loads(result.stdout)
            rci["key"] = rci["key"].encode()  # the rest of the code uses bytes
            self.rem_conn_info = rci
            self.ld(f"Connection info remote: {self.rem_conn_info}")
            return self.rem_conn_info
        except json.JSONDecodeError as e:
            msg = f"Failed to parse remote connection file: {e}"
            self.le(msg)
            raise RuntimeError(msg) from e
        except subprocess.CalledProcessError as e:
            msg = f"Failed to fetch remote connection file: {e}"
            self.le(msg)
            raise RuntimeError(msg) from e

    def make_ssh_tunnels(self) -> List[List[str]]:
        if not self.rem_conn_info:
            raise RuntimeError(f"Unexpected {self.rem_conn_info = }.")
        km = self.parent  # KernelManager
        if not km.cache_ports:
            self.le(
                f"Unexpected {km.cache_ports = }! Your system is likely not supported."
            )
        self.pick_kernel_ports()
        return self.make_kernel_tunnels()

    def pick_kernel_ports(self):
        """Find available ports on local machine for all kernel channels."""
        p_names = ("shell_port", "iopub_port", "stdin_port", "hb_port", "control_port")
        km = self.parent  # KernelManager
        # This part is inspired from LocalProvisioner.pre_launch where it seems to be
        # a temporary thing because the division of labor is not clear.
        # NOTE: there is a race condition on ports (from other processes on the local
        # machine), known issue: https://github.com/jupyter/jupyter_client/issues/487
        if self.cf_loaded:
            # If we have loaded the connection file, the KernelManager has the ports.
            ports = {p_name: getattr(km, p_name) for p_name in p_names}
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
            for port_name in p_names:
                p = lpc.find_available_port(km.ip)
                setattr(km, port_name, p)
            self.ports_cached = True

    def make_kernel_tunnels(self) -> List[List[str]]:
        """Create SSH tunnel arguments for kernel communication."""
        if not self.rem_conn_info:
            raise RuntimeError(f"Unexpected {self.rem_conn_info = }.")
        tunnels, km = [], self.parent
        p_names = ("shell_port", "iopub_port", "stdin_port", "hb_port", "control_port")
        for port_name in p_names:
            local_port = getattr(km, port_name)
            remote_port = self.rem_conn_info[port_name]
            tunnels += ["-L", f"{local_port}:localhost:{remote_port}"]
        return tunnels

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
        cf = Path(cf).absolute()
        if cf.is_file():
            # loads transport, ip, ports, key and signature_scheme
            # in KernelManager/Session
            km.load_connection_file(cf)
            self.cf_loaded = True

    async def pre_launch(self, **kwargs: Any) -> Dict[str, Any]:
        """
        Prepare for kernel launch.

        NB do to the connection file being overwritten on the remote machine by the
        jupyter-kernel command, this function ACTUALLY launches the remote kernel
        and later in launch_kernel() it sets up the SSH tunnels.
        """
        k_conf = self.ssh_host_alias, self.remote_python_prefix, self.remote_kernel_name
        if not all(k_conf):
            raise ValueError("Bad kernel configuration.")

        if self.processes is None:
            self.processes: Dict[int, Popen] = {}  # Dict[pid, Popen]

        # Auto-detect SSH executable if not specified, verify by calling it
        self.ssh = verify_local_ssh(self.ssh, self.log, "ssh", LOG_PREFIX)

        ssh_conn_ok, msg = verify_ssh_connection(
            self.ssh, self.ssh_host_alias, self.log, LOG_PREFIX
        )
        if not ssh_conn_ok:
            raise RuntimeError(f"{msg} See jupyter logs for details.")

        p = Path(self.remote_python_prefix)
        self.rem_jupyter = str(p / "bin" / "jupyter-kernel")
        ok, msg = verify_rem_executable(
            self.ssh, self.ssh_host_alias, self.rem_jupyter, self.log, LOG_PREFIX
        )
        if not ok:
            raise RuntimeError(f"{msg} See jupyter logs for details.")
        # self.rem_python = str(p / "bin" / "python")

        km = self.parent  # KernelManager
        if km is None:
            raise RuntimeError("Parent KernelManager not set")

        rem_args = [
            self.rem_jupyter,
            f"--KernelApp.kernel_name={self.remote_kernel_name}",
            # Generating the configuration file on the remote upfront did not work
            # because the jupyter command on the remote seemed to
            # override the connection ports and the key in the connection file.
            # Better to not interfere with the launch of the remote kernel. Let it take
            # care of everything as configured on the remote system.
            # f"--KernelManager.connection_file='{self.rem_conn_fp}'",
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
        if self.rem_conn_fp is not None:
            rem_args.append(f"--KernelManager.connection_file='{self.rem_conn_fp}'")

        # Simply specifying the connection file does not work because the
        # remote KernelApp overrides the contents of the connection file.
        # This does the trick of forcing the remote kernel to use the
        # provided key (which was generated when we started the remote kernel
        # for the first time).
        # NOTE: if we input the session key directly here in plain text, then on
        # the remote machine you can run e.g. `ps aux | grep jupyter-kernel`
        # and see the key in plain text in the command. We therefore
        # communicate it securely using the stdin pipe of the ssh process below.
        rem_args.append("--ConnectionFileMixin.Session.keyfile=/dev/stdin")

        # loads ip/ports/key/etc into KernelManager/Session
        self.load_connection_file()

        cmd = " ".join(rem_args)
        # Use nohup to ensure the remote kernel is not killed is fully detached from the
        # local machine to avoid it being killed unintentionally (e.g. network issues).
        # NOTE: I am not sure if running a `nohup` inside a Popen(...) is a canonical
        # thing to do. But it seems to work well without the need to redirect outputs
        # to some file on the remote systems and then run yet another ssh call to fetch
        # that file, potentially several times till the remote kernel has started.
        cmd = f"exec nohup {cmd} & echo {PID_PREFIX_KERNEL}$!"
        self.ld(f"Remote command {cmd = }")
        cmd = [
            self.ssh,
            "-q",  # mute ssh output
            # "-t", # NB We don't need a pseudo-tty to be allocated.
            self.ssh_host_alias,
            cmd,
        ]
        self.ld(f"Local command {cmd = }")

        # The way the processes are spawned is very important.
        # See launch_kernel() source code in jupyter_client for details.
        process = Popen(  # noqa: S603
            cmd,
            stdout=PIPE,
            stderr=subprocess.STDOUT,
            # Essential in order to not mess up the stdin of the local jupyter process
            # that is managing our local "fake" kernel.
            stdin=PIPE,
            # Ensures that when the jupyter server is requested to shutdown, with e.g.
            # a Ctrl-C in the terminal, our child processes are not terminated abruptly
            # causing jupyter to try to launch them again, etc..
            start_new_session=True,
            bufsize=1,  # return one line at a time
            universal_newlines=True,
        )
        self.processes[process.pid] = process

        # Preserving the key is essential for kernel restarts and for
        # externally-provided connection files.
        # Communicate the session key to the remote process securely using the stdin
        # pipe of the ssh process.
        process.stdin.write(km.session.key.decode() + "\n")
        process.stdin.flush()
        # Close the input pipe, see launch_kernel in jupyter_client
        process.stdin.close()

        # TODO: in case of exceptions, if possible, kill the remote process.
        self.extract_rem_pid_and_connection_fp(
            process, cmd, timeout=self.remote_kernel_launch_timeout
        )
        self.li(f"Remote KernelApp launched, RPID={self.rem_pid}")
        self.li(f"Connection file on remote machine: {self.rem_conn_fp}")
        # We are done with the starting the remote kernel. We know its PID to kill it
        # later. Terminate the local process.
        process.terminate()
        await self._wait(process)

        # Always fetch the remote connection info to forward to the correct ports
        self.fetch_remote_connection_info()

        # ##############################################################################
        # Run the ssh command ASAP to minimize the chance of race condition on ports
        # ##############################################################################
        ssh_tunnels = self.make_ssh_tunnels()
        cmd = [
            self.ssh,
            # "-q",  # don't mute ssh, required for reading the reverse tunnel port
            "-N",  # do nothing, i.e. maintain the tunnels alive
            *ssh_tunnels,  # ssh tunnels within the same command
            self.ssh_host_alias,
        ]
        self.ld(f"Setting up SSH tunnels {cmd = }")
        # For details on the way the processes are spawned see the Popen call above
        process_tunnels = Popen(  # noqa: S603
            cmd,
            stdout=PIPE,
            stderr=subprocess.STDOUT,
            stdin=PIPE,
            start_new_session=True,
            bufsize=1,
            universal_newlines=True,
        )
        self.pid_tunnels = process_tunnels.pid
        self.processes[process_tunnels.pid] = process_tunnels
        # Close the input pipe, see launch_kernel in jupyter_client
        process_tunnels.stdin.close()
        self.li(f"SSH tunnels launched, PID={process_tunnels.pid}")
        # ##############################################################################

        _ = kwargs.pop("extra_arguments", [])  # bc LocalProvisioner does it

        # NOTE: in case of future bugs check if calling this is relevant for running our
        # local commands
        # cmd = km.format_kernel_cmd(extra_arguments=extra_arguments)
        # NB `cmd` arg is passed in bc it is expected inside the KernelManager
        return await super().pre_launch(cmd=[], **kwargs)

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

        # TODO: set these when starting the remote KernelApp instead
        km.transport = rci["transport"]
        km.session.signature_scheme = rci["signature_scheme"]

        key_prev, key_new = km.session.key, rci["key"]
        if key_prev and key_prev != key_new:
            # Just in case on some combination of systems it does not work
            raise RuntimeError(
                f"Session key was not preserved ({key_prev=} vs {key_new=}"
            )

        # This if-else is here bc LocalProvisioner does it
        if "env" in kwargs:
            jupyter_session = kwargs["env"].get("JPY_SESSION_NAME", "")
            km.write_connection_file(jupyter_session=jupyter_session)
        else:
            km.write_connection_file()
        self.li(
            f"Connection file on local machine: {Path(km.connection_file).absolute()}"
        )

        self.connection_info = km.get_connection_info()
        self.ld(f"Connection info local: {self.connection_info}")

        return self.connection_info

    async def get_provisioner_info(self) -> Dict:
        """
        Get information about this provisioner instance.
        NB this method was never called during the development of this provisioner.
        """
        provisioner_info = await super().get_provisioner_info()
        provisioner_info.update(
            {
                "ssh_host_alias": self.ssh_host_alias,
                "remote_python_prefix": self.remote_python_prefix,
                "remote_kernel_name": self.remote_kernel_name,
                "rem_conn_fp": self.rem_conn_fp,
                "rem_conn_info": self.rem_conn_info,
            }
        )
        return provisioner_info

    async def load_provisioner_info(self, provisioner_info: Dict) -> None:
        """
        Load information about this provisioner instance.
        NB this method was never called during the development of this provisioner.
        """
        await super().load_provisioner_info(provisioner_info)
        self.ssh_host_alias = provisioner_info["ssh_host_alias"]
        self.remote_python_prefix = provisioner_info["remote_python_prefix"]
        self.remote_kernel_name = provisioner_info["remote_kernel_name"]
        self.rem_conn_fp = provisioner_info["rem_conn_fp"]
        self.rem_conn_info = provisioner_info["rem_conn_info"]

    async def cleanup(self, restart: bool = False) -> None:
        """Clean up resources used by the provisioner."""
        # TODO: change to debug
        self.ld("Cleaning up...")
        if self.ports_cached and not restart:
            lpc = LocalPortCache.instance()
            for k, port in self.connection_info.items():
                if k.endswith("_port"):
                    lpc.return_port(port)
            self.ports_cached = False
        self.ld("Cleaning up done.")

    @property
    def has_process(self) -> bool:
        """Returns true if this provisioner is currently managing a process."""
        return bool(self.processes)

    async def poll(self) -> Optional[int]:
        """Checks if kernel process is still running."""
        # TODO: check the remote process as well

        if not self.pid_tunnels:
            return 0
        return self.processes[self.pid_tunnels].poll()

    async def _wait(self, process: Popen) -> int:
        if not process:
            return 0

        while process.poll() is None:
            await asyncio.sleep(0.1)  # Wait for process to terminate

        # Process is no longer alive, wait and clear
        ret = process.wait()
        for attr in ["stdout", "stderr", "stdin"]:
            fid = getattr(process, attr)
            if not fid:
                continue
            try:
                fid.close()  # Close file descriptor
            except BrokenPipeError:
                self.ld(f"[Process {process.pid}] BrokenPipeError when closing {attr}")
        if process.pid in self.processes:
            del self.processes[process.pid]
            if process.pid == self.pid_tunnels:
                self.pid_tunnels = None
        return ret

    async def wait(self) -> Optional[int]:
        """Waits for processes to terminate."""
        self.ld("Waiting for process(es) to terminate...")
        ret = await asyncio.gather(
            *[self._wait(p) for p in self.processes.values()],
        )
        self.ld("Process(es) terminated.")
        return reduce(lambda r1, r2: r1 or r2, ret, 0)

    async def send_signal(self, signum: int) -> None:
        """
        Sends signal identified by signum to the kernel process.

        NB this method was never called during the development of this provisioner.
        This is expected since we are using `"interrupt_mode": "message"` in our spec.
        """
        self.ld(f"Unexpected `send_signal` call ({signum = })")

    async def kill(self, restart: bool = False) -> None:
        """
        Kill the kernel process.

        NB this method was never called during the development of this provisioner.
        This is expected since we are using `"interrupt_mode": "message"` in our spec.

        Update: this gets called if the remote process dies unexpectedly.
        """
        self.ld(f"Unexpected `kill` call ({restart = })")

    async def terminate(self, restart: bool = False) -> None:
        """Terminates the remote process(es)."""
        for p in self.processes.values():
            p.terminate()

        rem_pids = []
        if self.rem_pid:
            rem_pids.append(self.rem_pid)

        if not rem_pids:
            return

        for pid in rem_pids:
            try:
                # 15 is equivalent to SIGTERM
                cmd = [self.ssh, "-q", self.ssh_host_alias, f"kill -15 {pid}"]
                run(  # noqa: S603
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=PIPE,
                    check=True,
                )  # type: ignore
                self.ld(f"Sent SIGTERM to remote process, RPID={pid}")
                if pid == self.rem_pid:
                    self.rem_pid = None
            except Exception as e:
                self.log.exception(e)
                self.ld(f"Failed to terminate remote process, RPID={self.rem_pid}: {e}")
