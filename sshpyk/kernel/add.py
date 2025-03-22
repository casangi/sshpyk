import os
import re
import sys
import tempfile
from argparse import SUPPRESS, ArgumentParser
from getpass import getuser
from json import dump
from shutil import which
from subprocess import PIPE, run

from jupyter_client import kernelspec as ks


def _assignment(s):
    if not isinstance(s, str) or len(s) == 0:
        raise ValueError(s)
    eqindex = s.find("=")
    if eqindex < 0 or not s[0].isalpha() or not s[:eqindex].isalnum():
        raise ValueError(s)
    return s


def add_kernel(
    host,
    display_name,
    remote_python_path,
    local_python_path=sys.executable,
    env=None,
    sudo=False,
    system=False,
    timeout=5,
    session=False,
    echo=False,
):
    """
    Add a new kernel specification for a remote kernel

    Parameters
    ----------
    host: str
        name of the host (as used from SSH)
    display_name: str
        label displayed so the user will recognize this kernel
    remote_python_path: str
        path to the remote python installation with ipykernel installed
        (the python executable would be <PATH>/bin/python3)
    local_python_path: str
        path the the local python installation with sshpyk (the python executable would
        be <PATH>/bin/python3)
    env: [ str ]
        list of environment variables to set (list of strings with the form
        "<VARIABLE>=<VALUE>")
    sudo: bool
        indicates if the remote ipykernel should be started with sudo
    system: bool
        should the new kernel spec be created in the system area (True)
        or user area (False)
    timeout: int
        SSH connection timeout
    """
    if env is None:
        env = []

    def simplify(name):
        return re.sub(r"[^a-zA-Z0-9\-\_]", "", name)[:60]

    ssh = which("ssh")

    if ssh is None:
        raise RuntimeError("could not find SSH executable ('ssh')")

    # TODO: sanitize inputs if possible
    rproc = run(  # noqa: S603
        [ssh, host, f"file {remote_python_path}/bin/python"],
        stdout=PIPE,
        stderr=PIPE,
    )
    # TODO: should this be utf-8? (i.e. no argument)
    output = rproc.stdout.decode("ASCII")

    if len(output) == 0:
        raise RuntimeError(f"could not reach '{host}' with ssh")

    if "(No such file or directory)" in output:
        raise RuntimeError(f"not found on {host}: {output}")

    kernel_json = {
        "argv": [
            local_python_path,
            "-m",
            "sshpyk",
            "--host",
            host,
            "--python",
            remote_python_path,
            "--timeout",
            str(timeout),
            "-f",
            "{connection_file}",
        ],
        "display_name": display_name,
        "language": "python",
        # interrupt_mode="signal": process receives SIGINT, but in our case this will
        # kill the local process and not the remote process, leaving a zombie process on
        # the remote machine.
        # interrupt_mode="message": remote process receives a message instructing it
        # to interrupt the kernel.
        # Tested with a remote ipython kernel in JupyterLab. Interrupt button works.
        "interrupt_mode": "message",
    }
    if session:
        kernel_json["argv"].insert(-2, "--session")
    if echo:
        kernel_json["argv"].insert(-2, "--echo")
    if env:
        kernel_json["argv"].insert(-2, "--env")
        kernel_json["argv"].insert(-2, " ".join(env))

    if sudo:
        kernel_json["argv"].insert(-2, "-s")

    kernel_name = f"ssh_{host}_{simplify(display_name)}"

    with tempfile.TemporaryDirectory() as temp_dir:
        # TODO: check if permissions could be less permissive
        os.chmod(temp_dir, 0o755)  # noqa: S103

        with open(os.path.join(temp_dir, "kernel.json"), "w") as fd:
            dump(kernel_json, fd, sort_keys=True, indent=2)

        ks.install_kernel_spec(
            temp_dir, kernel_name, user=False if system else getuser(), replace=True
        )

    return kernel_name


if __name__ == "__main__":
    parse = ArgumentParser(add_help=False)
    optional = parse.add_argument_group("optional arguments")

    optional.add_argument(
        "--help",
        "-h",
        action="help",
        default=SUPPRESS,
        help="show this help message and exit",
    )
    optional.add_argument(
        "--timeout", "-t", type=int, default=5, help="specify timeout to use"
    )
    optional.add_argument(
        "--env",
        "-e",
        type=_assignment,
        nargs="*",
        default=[],
        help='add environment variable to set in the form: "NAME=VALUE"',
    )
    optional.add_argument(
        "--display-name",
        "-d",
        type=str,
        default=None,
        help="string which will be used to describe this kernel",
    )
    optional.add_argument(
        "--session",
        action="store_true",
        help="signal that session information should be stored for this kernel",
    )
    optional.add_argument(
        "--echo", action="store_true", help="echo SSH connection output to stdout"
    )
    optional.add_argument(
        "--sudo",
        "-s",
        action="store_true",
        help="sudo required to start kernel on remote machine",
    )

    required = parse.add_argument_group("required arguments")
    required.add_argument(
        "--host",
        "-H",
        required=True,
        help="name of remote host (as used to connect with ssh)",
    )
    required.add_argument(
        "--python",
        "-p",
        required=True,
        help="path to remote python installation "
        + '("PATH/bin/python" would be the python executable)',
    )

    args = parse.parse_args()
    if args.display_name is None:
        args.display_name = f"{args.host}: {args.python}"

    add_kernel(
        args.host,
        args.display_name,
        args.python,
        env=args.env,
        sudo=args.sudo,
        system=args.sudo,
        timeout=args.timeout,
        session=args.session,
        echo=args.echo,
    )
