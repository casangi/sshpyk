import json
import os
import re
import sys
import tempfile
from getpass import getuser
from json import dump
from os.path import exists, join
from shutil import which
from subprocess import PIPE, run

from jupyter_client import kernelspec as ks

from .kernel.utils import kinfo_exe, rexists

try:
    from .__version__ import __version__
except ImportError:
    __version__ = "0.0"


def version():
    return __version__


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
    host : str
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


def get_kernel_desc(all=False, valid_only=True):
    def _json(kernel_path):
        with open(join(kernel_path, "kernel.json")) as f:
            return json.load(f)
        return None

    km = ks.KernelSpecManager()
    kdirs = km.find_kernel_specs()
    keys = sorted(
        kdirs.keys() if all else filter(lambda k: k.startswith("ssh_"), kdirs.keys())
    )
    result = {
        k: {"ssh": k.startswith("ssh_"), "path": kdirs[k], "spec": _json(kdirs[k])}
        for k in keys
    }
    if valid_only is False:
        return result
    else:

        def is_valid(kinfo):
            ex = kinfo_exe(kinfo[1])
            return exists(ex[0]) and (ex[1] is None or rexists(ex[2], ex[1]))

        return dict(filter(is_valid, result.items()))
