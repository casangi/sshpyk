"""SSH Kernel Provisioner implementation for Jupyter Client."""

import asyncio
import json
import os
import re
import selectors
import subprocess
import time
from pathlib import Path
from shutil import which
from subprocess import PIPE, Popen, run
from typing import Any, Dict, List, Optional, Tuple

from jupyter_client.connect import KernelConnectionInfo, LocalPortCache
from jupyter_client.provisioning.provisioner_base import KernelProvisionerBase
from traitlets import Integer, Unicode


def _nope(*args, **kwargs):
    pass


RGX_CONN_FP = re.compile(r"file: (.*\.json)")
PID_PREFIX = "KERNEL_APP_PID="
RGX_PID = re.compile(rf"{PID_PREFIX}(\d+)")
REM_SESSION_KEY_NAME = "SSHPYK_SESSION_KEY"
# extracted from jupyter_client/kernelspec.py
RGX_KERNEL_NAME = re.compile(r"^[a-z0-9._-]+$", re.IGNORECASE)
RGX_SSH_HOST_ALIAS = re.compile(r"^[a-z0-9_-]+$", re.IGNORECASE)

getpgid = getattr(os, "getpgid", _nope)


class SshHost(Unicode):
    def validate(self, obj, value):
        value = super().validate(obj, value)
        try:
            if not RGX_SSH_HOST_ALIAS.match(value):
                raise ValueError(
                    f"Invalid SSH host alias {value!r}"
                    + f"Must match this pattern {RGX_SSH_HOST_ALIAS.pattern}. "
                    + "Verify that it is defined in your local SSH config file."
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


LOG_PREFIX = "[SSHKernel] "


class SSHKernelProvisioner(KernelProvisionerBase):
    """
    Kernel provisioner that launches Jupyter kernels on remote systems via SSH.

    This provisioner connects to remote systems using SSH, sets up port local forwarding
    for kernel communication, and manages the lifecycle of the remote kernel.
    """

    ssh_host_alias = SshHost(
        config=True,
        help="Remote host alias to connect to. "
        + "It must be defined in your local SSH config file.",
        allow_none=False,
    )
    remote_python_prefix = UnicodePath(
        config=True,
        help="Path to Python prefix on remote system. "
        + "Run `python -c 'import sys; print(sys.prefix)'` on the remote system "
        + "to find the path. If the remote kernel is part of a virtual environment, "
        + "first activate your virtual environment and then query the `sys.prefix`. "
        + "It must have jupyter_client package installed.",
        allow_none=False,
    )
    remote_kernel_name = KernelName(
        config=True,
        help="Kernel name on the remote system "
        + "(i.e. first column of `jupyter kernelspec list` on the remote system).",
        allow_none=False,
    )
    remote_kernel_launch_timeout = Integer(
        default=60,
        config=True,
        help="Timeout for launching the remote kernel through the ssh command.",
        allow_none=False,
    )

    process = None
    process_tunnels = None

    ports_cached = False
    ssh = None

    rem_python = None
    rem_jupyter = None
    rem_conn_fp = None
    rem_conn_info = None

    pid = None
    pgid = None
    pid_tunnels = None
    pgid_tunnels = None
    rem_pid = None

    def li(self, msg: str, *args, **kwargs):
        self.log.info(f"{LOG_PREFIX}{msg}", *args, **kwargs)

    def ld(self, msg: str, *args, **kwargs):
        self.log.debug(f"{LOG_PREFIX}{msg}", *args, **kwargs)

    def lw(self, msg: str, *args, **kwargs):
        self.log.warning(f"{LOG_PREFIX}{msg}", *args, **kwargs)

    def le(self, msg: str, *args, **kwargs):
        self.log.error(f"{LOG_PREFIX}{msg}", *args, **kwargs)

    # Use asyncio to read output with timeout
    async def extract_connection_file_from_process_pipes(
        self, timeout: int
    ) -> Tuple[Optional[int], Optional[str]]:
        """
        When executing the `jupyter kernel --kernel=...` it prints (to stderr):
        ```
        [KernelApp] Starting kernel 'python3'
        [KernelApp] Connection file: /some/path/to/the/connection_file.json
        [KernelApp] To connect a client: --existing connection_file.json
        """
        # Create a selector and register both stdout and stderr pipes to listen events
        selector = selectors.DefaultSelector()
        selector.register(self.process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(self.process.stderr, selectors.EVENT_READ, "stderr")

        remote_pid = conn_file_path = None

        t = time.time()
        while time.time() - t < timeout:
            await asyncio.sleep(0.1)  # yield control back to event loop

            # Check for available data on either pipe
            events = selector.select(timeout=0)
            for key, _ in events:
                pipe_name, pipe = key.data, key.fileobj

                line = pipe.readline()
                if not line:  # EOF on this pipe
                    self.le(f"Process {pipe_name} closed unexpectedly")
                    selector.unregister(pipe)
                    continue

                line_str = line.decode().strip()
                if line_str:
                    # Log both stdout and stderr
                    self.li(f"Remote jupyter says ({pipe_name}): {line_str}")
                    # Then information was output into the stderr, but check both
                    match = RGX_PID.search(line_str)
                    if match:
                        remote_pid = int(match.group(1))
                    match = RGX_CONN_FP.search(line_str)
                    if match:
                        conn_file_path = match.group(1)
                    if remote_pid and conn_file_path:
                        return remote_pid, conn_file_path

            # If no pipes left to monitor or process ended, exit loop
            if not selector.get_map() or self.process.poll() is not None:
                return remote_pid, conn_file_path

        raise TimeoutError(timeout)

    async def extract_connection_file(self, rem_cmd: str, timeout: int):
        self.ld(
            f"Waiting for remote connection file path from {rem_cmd!r} ({timeout = })"
        )
        try:
            rpid, rcf = await self.extract_connection_file_from_process_pipes(
                timeout=timeout
            )
        except TimeoutError as e:
            msg = f"Timed out ({timeout}s) waiting for connection file information."
            self.le(msg)
            raise RuntimeError(msg) from e

        if not rpid:
            msg = f"Could not extract PID of remote process during {rem_cmd!r}"
            self.le(msg)
            raise RuntimeError(msg)

        if not rcf:
            msg = f"Could not extract connection file path on remote during {rem_cmd!r}"
            self.le(msg)
            raise RuntimeError(msg)

        try:
            Path(rcf)  # should raise if not valid
        except Exception as e:
            msg = f"Unexpected remote connection file path {rcf}."
            self.le(msg)
            raise RuntimeError(msg) from e

        return rpid, rcf

    def fetch_remote_connection_info(self) -> Dict[str, Any]:
        try:
            # For details on the way the processes are spawned see the Popen call in
            # pre_launch().
            cmd = [self.ssh, "-q", self.ssh_host_alias, f"cat {self.rem_conn_fp!r}"]
            self.li(f"Fetching remote connection file {cmd = }")
            result = run(  # noqa: S603
                cmd,
                stdout=PIPE,
                stderr=PIPE,
                stdin=PIPE,
                check=True,
                start_new_session=True,
            )  # type: ignore
            rci = json.loads(result.stdout)
            rci["key"] = rci["key"].encode()  # the rest of the code uses bytes
            return rci
        except json.JSONDecodeError as e:
            msg = f"Failed to parse remote connection file: {e}"
            self.le(msg)
            raise RuntimeError(msg) from e
        except subprocess.CalledProcessError as e:
            msg = f"Failed to fetch remote connection file: {e}"
            self.le(msg)
            raise RuntimeError(msg) from e

    async def pre_launch(self, **kwargs: Any) -> Dict[str, Any]:
        """
        Prepare for kernel launch.

        NB do to the connection file being overwritten on the remote machine by the
        jupyter-kernel command, this function ACTUALLY launches the remote kernel
        and later in launch_kernel() it sets up the SSH tunnels.
        """
        if (
            not self.ssh_host_alias
            or not self.remote_python_prefix
            or not self.remote_kernel_name
        ):
            raise ValueError("Bad configuration")

        p = Path(self.remote_python_prefix)
        self.rem_python = str(p / "bin" / "python")
        self.rem_jupyter = str(p / "bin" / "jupyter-kernel")

        self.ssh = which("ssh")
        if not self.ssh:
            raise EnvironmentError("'ssh' executable not found")

        km = self.parent  # KernelManager
        if km is None:
            raise RuntimeError("Parent kernel manager not set")

        rem_args = [
            self.rem_jupyter,
            "kernel",
            f"--kernel={self.remote_kernel_name}",
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
            # Simply specifying the connection file does not work due to the way the
            # remote KernelApp works.
            # This does the trick of forcing the remote kernel to use the
            # provided key (which was generated when we started the remote kernel
            # for the first time).
            # NOTE: if we input the session key directly here in plain text, then on
            # the remote machine you can run e.g. `ps aux | grep jupyter-kernel`
            # and see the key in plain text in the command. Instead of this we
            # communicate it securely using the stdin pipe of the ssh process below.
            rem_args.append(
                f"--ConnectionFileMixin.Session.key=${REM_SESSION_KEY_NAME}"
            )

        rem_cmd = " ".join(rem_args)
        # Use nohup to ensure the remote kernel is not killed is fully detached from the
        # local machine to avoid it being killed unintentionally (e.g. network issues).
        # NOTE: I am not sure if running a `nohup` inside a Popen(...) is a canonical
        # thing to do. But it seems to work well without the need to redirect outputs
        # to some file on the remote systems and then run yet another ssh call to fetch
        # that file, potentially several times till the remote kernel has started.
        rem_cmd = f"exec nohup {rem_cmd} & echo {PID_PREFIX}$!"
        if self.rem_conn_fp is not None:
            rem_cmd = f"read {REM_SESSION_KEY_NAME} && " + rem_cmd
        self.li(f"Remote command: {rem_cmd!r}")
        cmd = [
            self.ssh,
            "-q",  # mute ssh output
            # "-t", # NB We don't need a pseudo-tty to be allocated.
            self.ssh_host_alias,
            rem_cmd,
        ]
        self.li(f"Local command: {cmd!r}")

        # The way the processes are spawned is very important.
        # See launch_kernel() source code in jupyter_client for details.
        self.process = Popen(  # noqa: S603
            cmd,
            stdout=PIPE,
            stderr=PIPE,
            # Essential in order to not mess up the stdin of the local jupyter process
            # that is managing our local "fake" kernel.
            stdin=PIPE,
            # Ensures that when the jupyter server is requested to shutdown, with e.g.
            # a Ctrl-C in the terminal, our child processes are not terminated abruptly
            # causing jupyter to try to launch them again, etc..
            start_new_session=True,
        )
        if self.rem_conn_fp is not None:
            # Communicate the session key to the `read {REM_SESSION_KEY_NAME}` command
            # on the remote machine securely using the stdin pipe of the ssh
            # process.
            self.process.stdin.write(self.rem_conn_info["key"] + b"\n")
            self.process.stdin.flush()
        # Close the input pipe, see launch_kernel in jupyter_client
        self.process.stdin.close()

        self.pid = self.process.pid
        self.pgid = None
        try:
            self.pgid = getpgid(self.pid)
        except OSError:
            pass

        self.rem_pid, rcfp = await self.extract_connection_file(
            rem_cmd, timeout=self.remote_kernel_launch_timeout
        )
        self.li(f"Remote kernel launched, PID of remote KernelApp: {self.rem_pid}")

        self.li(f"Connection file on remote machine: {rcfp}")
        # We are done with the starting the remote kernel. We know its PID to kill it
        # later.
        self.process.terminate()
        await self._wait("process")

        if self.rem_conn_fp is None:
            self.rem_conn_fp = rcfp
        elif self.rem_conn_fp != rcfp:
            raise RuntimeError(f"Unexpected remote connection file path {rcfp}.")

        # Always fetch the remote connection info to forward to the correct ports on
        # the remote system
        self.rem_conn_info = self.fetch_remote_connection_info()
        self.ld(f"Connection info remote: {self.rem_conn_info}")

        # This part is inspired from LocalProvisioner.pre_launch where it seems to be
        # a temporary thing because the division of labor is not clear.
        if km.cache_ports and not self.ports_cached:
            # Find available ports on local machine for all channels.
            # These are the ports that the local kernel client will connect to.
            # These ports are SSH-forwarded to the remote kernel.
            lpc = LocalPortCache.instance()
            for channel in ("shell", "iopub", "stdin", "hb", "control"):
                setattr(km, channel + "_port", lpc.find_available_port(km.ip))
            self.ports_cached = True

        # Fill in the rest of the connection info based on the remote connection info
        rci = self.rem_conn_info
        # NB don't override the local kernel name. Important for extra clarity on how
        # and to which kernel we are connected.
        # km.session.kernel_name = rci.get("kernel_name", "")
        km.transport = rci["transport"]
        km.session.signature_scheme = rci["signature_scheme"]
        key_prev = self.connection_info.get("key", None)
        key_new = rci["key"]
        if key_prev and key_prev != key_new:
            # Just in case on some combination of systems it does not work
            self.le(f"Session key was not preserved ({key_prev=} vs {key_new=}")
        km.session.key = key_new

        _ = kwargs.pop("extra_arguments", [])  # bc LocalProvisioner does it

        # This if-else is here bc LocalProvisioner does it
        if "env" in kwargs:
            jupyter_session = kwargs["env"].get("JPY_SESSION_NAME", "")
            km.write_connection_file(jupyter_session=jupyter_session)
        else:
            km.write_connection_file()
        self.li(
            f"Connection file on local machine: {Path(km.connection_file).absolute()}."
        )

        ci = self.connection_info = km.get_connection_info()
        self.ld(f"Connection info local: {self.connection_info}")

        # SSH tunnels between local and remote ports
        ssh_tunnels = []
        for key in rci:
            if key.endswith("_port"):
                ssh_tunnels += ["-L", f"{ci[key]}:localhost:{rci[key]}"]

        cmd = [
            self.ssh,
            "-q",  # mute ssh output
            "-N",  # do nothing, i.e. maintain the tunnels alive
            *ssh_tunnels,  # ssh tunnels within the same command
            self.ssh_host_alias,
        ]

        # NOTE: in case of future bugs check if calling this is relevant for running our
        # local commands
        # cmd = km.format_kernel_cmd(extra_arguments=extra_arguments)

        # NB `cmd` arg is passed in bc it is expected inside the KernelManager
        return await super().pre_launch(cmd=cmd, **kwargs)

    async def launch_kernel(
        self, cmd: List[str], **kwargs: Any
    ) -> KernelConnectionInfo:
        """Launch a kernel on the remote system via SSH."""
        cmd_str = " ".join(cmd)
        self.li(f"SSH-forwarding local ports to remote kernel ports: {cmd_str!r}")

        # For details on the way the processes are spawned see the Popen call in
        # pre_launch().
        self.process_tunnels = Popen(  # noqa: S603
            cmd,
            stdout=PIPE,
            stderr=PIPE,
            stdin=PIPE,
            start_new_session=True,
        )
        # Close the input pipe, see launch_kernel in jupyter_client
        self.process_tunnels.stdin.close()

        self.pid_tunnels = self.process_tunnels.pid
        self.pgid_tunnels = None
        try:
            self.pgid_tunnels = getpgid(self.pid_tunnels)
        except OSError:
            pass

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
                "pid": self.pid,
                "pgid": self.pgid,
                "rem_pid": self.rem_pid,
                "pid_tunnels": self.pid_tunnels,
                "pgid_tunnels": self.pgid_tunnels,
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
        self.pid = provisioner_info["pid"]
        self.pgid = provisioner_info["pgid"]
        self.rem_pid = provisioner_info["rem_pid"]
        self.pid_tunnels = provisioner_info["pid_tunnels"]
        self.pgid_tunnels = provisioner_info["pgid_tunnels"]
        self.rem_conn_fp = provisioner_info["rem_conn_fp"]
        self.rem_conn_info = provisioner_info["rem_conn_info"]

    async def cleanup(self, restart: bool = False) -> None:
        """Clean up resources used by the provisioner."""
        if self.ports_cached and not restart:
            lpc = LocalPortCache.instance()
            for k, port in self.connection_info.items():
                if k.endswith("_port"):
                    lpc.return_port(port)
            self.ports_cached = False

    @property
    def has_process(self) -> bool:
        """Returns true if this provisioner is currently managing a process."""
        return self.process_tunnels is not None

    async def poll(self) -> Optional[int]:
        """Checks if kernel process is still running."""
        if self.process_tunnels:
            return self.process_tunnels.poll()
        return 0

    async def _wait(self, process_attr_name: str):
        process = getattr(self, process_attr_name)
        if not process:
            return 0
        # Wait for process to terminate
        while process.poll() is None:
            await asyncio.sleep(0.1)

        # Process is no longer alive, wait and clear
        ret = process.wait()
        # Close file descriptors
        for attr in ["stdout", "stderr", "stdin"]:
            fid = getattr(process, attr)
            if fid:
                try:
                    fid.close()
                except BrokenPipeError:
                    self.ld(
                        f"BrokenPipeError when closing {attr} for {process_attr_name}"
                    )
        setattr(self, process_attr_name, None)
        return ret

    async def wait(self) -> Optional[int]:
        """Waits for processes to terminate."""
        self.ld("Waiting for process(es) to terminate...")
        ret0, ret1 = await asyncio.gather(
            self._wait("process"),
            self._wait("process_tunnels"),
        )
        self.ld("Process(es) terminated.")
        return ret0 or ret1

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
        """Terminates the kernel process."""
        if self.rem_pid:
            try:
                # 15 is equivalent to SIGTERM
                cmd = [self.ssh, "-q", self.ssh_host_alias, f"kill -15 {self.rem_pid}"]
                run(  # noqa: S603
                    cmd,
                    stdout=PIPE,
                    stderr=PIPE,
                    stdin=PIPE,
                    check=True,
                )  # type: ignore
                self.ld(f"Sent SIGTERM to remote process {cmd = }")
                self.rem_pid = None
            except Exception as e:
                self.log.exception(e)
                self.ld(f"Failed to terminate remote process PID={self.rem_pid}: {e}")

        for p in (self.process, self.process_tunnels):
            if p:
                p.terminate()
