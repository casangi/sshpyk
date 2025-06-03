"""An application to launch a kernel by name in a local subprocess."""

import os
import re
import signal
import sys
import uuid
from signal import SIGHUP, SIGINT, SIGQUIT, SIGTERM, SIGUSR1, SIGUSR2
from typing import Any, Sequence, Union

from jupyter_client import __version__  # type: ignore
from jupyter_client.kernelspec import KernelSpecManager
from jupyter_client.manager import KernelManager
from jupyter_core.application import JupyterApp, base_flags
from tornado.ioloop import IOLoop, PeriodicCallback
from tornado.iostream import StreamClosedError
from traitlets import Bool, Integer, Unicode
from traitlets import Enum as EnumTrait
from traitlets.config.loader import Config

LEAVE_HELP = """
Launch remote kernel, leave it running, and exit this SSHKernelApp.
When set to True, it provides a way to skip having to interact with the
SSHKernelApp using keyboard input or signals.
"""

EXISTING = dict(
    config=True,
    help="Filename or absolute path to a provisioner info file previously saved "
    "using, e.g., `sshpyk-kernel --persistent ...` to connect to an existing "
    "sshpyk remote kernel.",
)
PERSISTENT_HELP = """
If True, the remote kernel will be left running on shutdown so that you
can reconnect to it later using, e.g., `sshpyk-kernel --existing ...`.
If `--persistent-file` is provided, this option is overridden to True.
"""
PERSISTENT = dict(
    config=True,
    help=PERSISTENT_HELP,
    default_value=False,
)
PERSISTENT_FILE = dict(
    config=True,
    help="Path to the file where to save the persistence info. "
    "If not provided, the file will be saved in Jupyter's `runtime` directory. "
    "If not provided, but `--persistent` flag is passed, the file will be preserved. "
    "If provided, the file will be preserved and `--persistent` is overridden to True.",
)
SSH_VERBOSE = dict(
    config=True,
    help="Increases verbosity of the SSH connection.",
    default_value=None,
    allow_none=True,
    values=("v", "vv", "vvv"),
)


class SSHKernelApp(JupyterApp):
    """Launch a kernel by its local name."""

    version = __version__
    description = """Launch a kernel by its local name."""

    classes = [KernelManager, KernelSpecManager]

    aliases = {
        ("kernel", "k"): "SSHKernelApp.kernel_name",
        ("existing", "e"): "SSHKernelApp.existing",
        ("persistent_file", "f"): "SSHKernelApp.persistent_file",
        ("poll-interval", "i"): "SSHKernelApp.poll_interval",
        "ssh-verbose": "SSHKernelApp.ssh_verbose",
    }

    flags = {
        ("debug", "d"): base_flags["debug"],
        # To not have to pass `--leave=True`, but just `--leave`/`-l`
        ("leave", "l"): ({"SSHKernelApp": {"leave": True}}, LEAVE_HELP),
        ("persistent", "p"): ({"SSHKernelApp": {"persistent": True}}, PERSISTENT_HELP),
    }

    kernel_name = Unicode(
        config=True,
        help="The name of a kernel type to start",
        allow_none=False,
    )
    capture_stdin = Bool(
        True,
        config=True,
        help="Enable handling of Ctrl+D to interrupt/shutdown/restart/leave."
        "This has no effect in a non-interactive terminal.",
    )
    poll_interval = Integer(
        5000,
        config=True,
        help="Interval in milliseconds for polling to ensure SSH tunnels are running.",
    )
    leave = Bool(
        False,
        config=True,
        help=LEAVE_HELP,
    )
    existing = Unicode(**EXISTING)  # type: ignore
    persistent = Bool(**PERSISTENT)  # type: ignore
    persistent_file = Unicode(**PERSISTENT_FILE)  # type: ignore
    ssh_verbose = EnumTrait(**SSH_VERBOSE)  # type: ignore

    def initialize(self, argv: Union[str, Sequence[str], None] = None) -> None:
        """Initialize the application."""
        super().initialize(argv)

        cf_basename = f"kernel-{uuid.uuid4()}.json"
        fp = os.path.join(self.runtime_dir, cf_basename)
        self.config.setdefault("KernelManager", {}).setdefault("connection_file", fp)

        # Pass config options to provisioner
        p_name = "SSHKernelProvisioner"
        if p_name not in self.config:
            self.config[p_name] = Config()
        pc = self.config[p_name]
        for k in ("existing", "persistent", "persistent_file", "ssh_verbose"):
            if getattr(self, k):
                v = getattr(self, k)
                if k in pc:
                    msg = f"Overriding --{p_name}.{k}={pc[k]} with --{k}={v}"
                    self.log.warning(msg)
                pc[k] = v
        self.log.debug(f"{self.config = }")

        self.km = KernelManager(kernel_name=self.kernel_name, config=self.config)

        self.loop = IOLoop.current()

        # Setup periodic callback to ensure tunnels are running even if internet
        # connection is lost.
        # If the callback runs for longer than callback_time milliseconds,
        # subsequent invocations will be skipped to get back on schedule.
        self.periodic_poll = PeriodicCallback(self.poll, self.poll_interval)

        # Setup stdin handler to detect Ctrl+D (EOF) in an interactive terminal
        if self.capture_stdin and sys.stdin.isatty():
            self.log.debug("Setting up stdin listener for Ctrl+D")
            self.loop.add_handler(
                sys.stdin.fileno(), self._handle_stdin, self.loop.READ
            )

    def _handle_stdin(self, fd: int, events: int) -> None:
        """Handle stdin input, checking for EOF (Ctrl+D)"""
        try:
            data = os.read(fd, 1)
            if not data:  # EOF detected
                self.log.info("Received EOF (Ctrl+D)")
                magenta = "\033[35m"  # Magenta
                reset = "\033[39m"  # Reset color only, not formatting
                sys.stdout.write(
                    f"{magenta}Pick an action and press Enter: "
                    "Y = shutdown (default), i = interrupt, "
                    "l = leave w/out kernel shutdown, r = restart, n = nothing"
                    f"\nAction? [Y/i/l/r/n]:{reset} "
                )
                sys.stdout.flush()

                # Temporarily remove handler to avoid recursion
                self.loop.remove_handler(fd)

                response = sys.stdin.readline().strip().lower()
                if response == "" or response == "y":
                    self.shutdown(0)
                elif response == "l":
                    self.leave_app(-2)
                elif response == "r":
                    self.restart(0)
                elif response == "i":
                    self.interrupt(0)
                else:
                    self.log.info("No actions taken, resuming")

                # Restore handler
                self.loop.add_handler(fd, self._handle_stdin, self.loop.READ)
        except (OSError, StreamClosedError):
            # Stream closed, remove the handler
            self.loop.remove_handler(fd)

    def setup_signals(self) -> None:
        """Set up signal handlers.

        SIGINT (Ctrl-C) will interrupt the kernel rather than shutdown
        SIGTERM will shutdown the remote kernel and all local and remote processes
        """
        if os.name == "nt":
            return

        self.log.debug("Setting up signal handlers")

        def shutdown_handler(signo: int, frame: Any) -> None:
            if signo == SIGINT:
                self.loop.add_callback_from_signal(self.interrupt, signo)
            elif signo == SIGUSR1:
                self.loop.add_callback_from_signal(self.restart, signo)
            elif signo == SIGQUIT:
                self.loop.add_callback_from_signal(self.leave_app, signo)
            elif signo in (SIGTERM, SIGHUP, SIGUSR2):
                self.loop.add_callback_from_signal(self.shutdown, signo)
            else:
                self.log.debug(f"Unexpected signal {signo}")

        msg_shutdown = (
            "to shutdown, remote kernel will not shutdown if `--persistent` or "
            "`--persistent-file` have been passed"
        )
        c = self.__class__.__name__
        for sig, msg in (
            (SIGHUP, msg_shutdown),
            (SIGTERM, msg_shutdown),
            (SIGUSR2, "to shutdown the remote kernel, ignoring persistent flags"),
            (SIGINT, "or press Ctrl+C to interrupt the kernel"),
            (SIGUSR1, "to restart the kernel"),
            (
                SIGQUIT,
                f"or press Ctrl+\\ to quit this {c} without shutting down the kernel",
            ),
        ):
            signal.signal(sig, shutdown_handler)
            self.log.info(f"You can send {sig!r} {msg}")

        self.log.info(
            "You can press Ctrl+D and execute one of these actions: "
            "interrupt, shutdown, restart or leave (without shutdown)"
        )

    def interrupt(self, signo: int) -> None:
        """Interrupt the kernel."""
        msg = f"Interrupting kernel on signal {signo}"
        if signo == 0:
            msg = "Interrupting kernel on Ctrl+D"
        self.log.info(msg)
        self.km.interrupt_kernel()

    def shutdown(self, signo: int) -> None:
        """Shut down the application."""
        self.periodic_poll.stop()

        if signo == 0:
            msg = "Shutting down on Ctrl+D"
            # don't preserve kernel on Ctrl+D, otherwise the user has no simple way
            # to shutdown the remote kernel without sending an explicit `exit()` to it
            self.set_persistent(False)
        elif signo == -1:
            msg = "Kernel quit, shutting down."
        elif signo in (-2, -3):  # leave_app cases, already logged
            msg = None
        elif signo == SIGUSR2:
            msg = f"Shutting down remote kernel on signal {signo}"
            self.set_persistent(False)
        else:
            msg = f"Shutting down on signal {signo}"

        if msg:
            self.log.info(msg)

        self.km.shutdown_kernel()
        self.loop.stop()

    def set_persistent(self, persistent: bool) -> None:
        """Set the persistent flag."""
        p = getattr(self.km, "provisioner", None)
        if p and hasattr(p, "persistent"):
            # SSHKernelProvisioner checks this flag before deleting persistent_file
            if p.persistent != persistent:
                self.log.info(f"Setting {persistent = } (previous {p.persistent = })")
                p.persistent = persistent

    def leave_app(self, signo: int) -> None:
        """Leave the application without shutting down the kernel."""
        c = self.__class__.__name__
        # Enforce persistent, this is mainly to allow preserving the kernel even
        # if the user launched the SSHKernelApp without the `--persistent` flag.
        self.set_persistent(True)

        if signo == -2:
            msg = "Leaving on Ctrl+D. Remote kernel will not be shutdown!"
        elif signo == -3:
            msg = "Leaving after kernel launch. Remote kernel will not be shutdown!"
        else:
            msg = f"Leaving {c} on signal {signo}. Remote kernel will not be shutdown!"
        self.log.info(msg)

        is_alive = self.km.is_alive()  # calls provisioner.poll()
        if not is_alive:
            self.log.error("Kernel is not running anymore!")
            self.set_persistent(False)

        # allow provisioner to perform local cleanups (e.g. cancel ssh tunnels)
        self.shutdown(signo)
        self.loop.stop()

    def restart(self, signo: int) -> None:
        """Restart the kernel."""
        msg = f"Restarting kernel on signal {signo}"
        if signo == 0:
            msg = "Restarting kernel on Ctrl+D"
        self.log.info(msg)
        self.periodic_poll.stop()  # Otherwise we stack monitoring old process
        self.km.restart_kernel()
        self.periodic_poll.start()

    async def poll(self) -> None:
        """Ensure the ssh tunnels are running."""
        if not self.km.shutting_down:
            p = getattr(self.km, "provisioner", None)
            if p:
                # restart SSH tunnels (if not shutting down)
                ret = await p.poll()
                if ret is not None and not self.km.shutting_down:
                    self.shutdown(-1)

    def log_connection_info(self) -> None:
        """Log the connection info for the kernel."""
        cf = self.km.connection_file
        self.log.info(f"Connection file: {cf}")
        self.log.info(f"To connect a client: --existing {os.path.basename(cf)}")

    def start(self) -> None:
        """Start the application."""
        self.log.info(f"Starting kernel {self.kernel_name}")
        try:
            self.km.start_kernel()
            self.setup_signals()
            self.log_connection_info()
            # Start the tunnel checker
            self.periodic_poll.start()
            if self.leave:
                self.leave_app(-3)
            self.loop.start()
        finally:
            if hasattr(self, "periodic_poll"):
                self.periodic_poll.stop()
            self.km.cleanup_resources()


main = SSHKernelApp.launch_instance

# This line is to be patched by the provisioning.py before piping the contents of
# this kernelapp.py file to the remote machine to launch the remote kernel with the
# necessary arguments.
ARGS_PATCH = []

# This is mainly to allow running the SSHKernelApp as a script on the remote machine
if __name__ == "__main__":
    sys.argv[0] = re.sub(r"(-script\.pyw|\.exe)?$", "", sys.argv[0])
    sys.argv += ARGS_PATCH  # see ARGS_PATCH comment above
    sys.exit(main())
