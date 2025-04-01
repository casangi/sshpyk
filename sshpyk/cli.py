"""Command line interface to manage sshpyk kernel specifications."""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Dict

from jupyter_client.kernelspec import KernelSpecManager

from .provisioning import RGX_KERNEL_NAME, RGX_SSH_HOST_ALIAS
from .utils import (
    fetch_remote_kernel_specs,
    verify_rem_executable,
    verify_ssh_connection,
)

try:
    from .__version__ import __version__  # type: ignore
except ImportError:
    __version__ = "0.0"

logger = logging.getLogger(__name__)

LOG_FORMAT = "%(process)6d %(asctime)s %(levelname)-8s %(name)s %(module)s:%(lineno)d %(funcName)s: %(message)s"  # noqa: E501


# ANSI color codes for terminal output
G = "\033[32m"  # Green
R = "\033[31m"  # Red
C = "\033[36m"  # Cyan
M = "\033[35m"  # Magenta
N = "\033[39m"  # Reset color only, not formatting

K_NAME = "Name:"
K_DISP = "Display Name:"
K_RES = "Resource Dir:"
K_CMD = "Command:"
K_CMDS = "Command (simplified):"
K_LANG = "Language:"
K_SSH = "SSH Host Alias:"
K_RPFX = "Remote Python Prefix:"
K_RKER = "Remote Kernel Name:"
K_RLANG = "Remote Language:"
K_TIME = "Start Timeout:"
K_RCMD = "Remote Command:"
K_INT = "Interrupt Mode:"
K_RINT = "Remote Interrupt Mode:"
K_RRES = "Remote Resource Dir:"


def create_header(text: str, width: int, color: str = "") -> str:
    dash_count = width - len(text)
    left_dashes = "-" * (dash_count // 2)
    right_dashes = "-" * (dash_count - (dash_count // 2))
    return f"{color}{left_dashes}{text}{right_dashes}{N if color else ''}"


def list_kernels(args: argparse.Namespace) -> None:
    """List all available kernels with sanity checks for SSH kernels."""
    # reset color upfront to avoid default color change after our color resets
    print(f"{N}", end="")
    specs = KernelSpecManager().get_all_specs()

    if not specs:
        print("No kernels available")
        return

    # Separate kernels into local and SSH
    local_kernels = []
    ssh_kernels = []

    for name, spec_info in sorted(specs.items()):
        spec = spec_info.get("spec", {})
        display_name = spec.get("display_name", "")
        resource_dir = spec_info.get("resource_dir", "")
        argv = " ".join(spec.get("argv", []))
        language = spec.get("language", "")

        # Check if it's an SSH kernel
        metadata = spec.get("metadata", {})
        provisioner = metadata.get("kernel_provisioner", {})
        is_ssh = provisioner.get("provisioner_name") == "sshpyk-provisioner"

        if is_ssh:
            config = provisioner.get("config", {})
            host = config.get("ssh_host_alias", "")
            remote_python_prefix = config.get("remote_python_prefix", "")
            remote_kernel_name = config.get("remote_kernel_name", "")
            timeout = config.get("remote_kernel_launch_timeout", 60)

            ssh_kernels.append(
                {
                    "name": name,
                    "display_name": display_name,
                    "resource_dir": resource_dir,
                    "host": host,
                    "remote_python_prefix": remote_python_prefix,
                    "remote_kernel_name": remote_kernel_name,
                    "timeout": timeout,
                    "language": language,
                }
            )
        else:
            local_kernels.append(
                {
                    "name": name,
                    "display_name": display_name,
                    "resource_dir": resource_dir,
                    "argv": argv,
                    "language": language,
                }
            )
    all_keys = [
        K_NAME,
        K_DISP,
        K_RES,
        K_CMD,
        K_LANG,
        K_SSH,
        K_RPFX,
        K_RKER,
        K_RLANG,
        K_TIME,
        K_RCMD,
        K_INT,
        K_RINT,
        K_RRES,
    ]
    max_key_len = max(len(key) for key in all_keys)

    # Print local kernels
    if local_kernels and (args.local or not (args.remote and not args.local)):
        for kernel in local_kernels:
            # Print header for local kernels
            print(create_header(" Local Kernel ", max_key_len, M))

            kernel_lines = []
            kernel_lines.append(f"{M}{K_NAME:<{max_key_len}}{N} {kernel['name']}")
            kernel_lines.append(
                f"{M}{K_DISP:<{max_key_len}}{N} {kernel['display_name']}"
            )
            kernel_lines.append(
                f"{M}{K_RES:<{max_key_len}}{N} {kernel['resource_dir']}"
            )
            kernel_lines.append(f"{M}{K_CMD:<{max_key_len}}{N} {kernel['argv']}")
            kernel_lines.append(
                f"{M}{K_LANG:<{max_key_len}}{N} {kernel.get('language', '')}"
            )
            # Add interrupt_mode if present
            interrupt_mode = (
                specs[kernel["name"]].get("spec", {}).get("interrupt_mode", "")
            )
            if interrupt_mode:
                kernel_lines.append(f"{M}{K_INT:<{max_key_len}}{N} {interrupt_mode}")

            print("\n".join(kernel_lines), end="\n\n")

    # Cache for remote kernel specs to avoid multiple calls to the same host
    remote_specs_cache: Dict[str, Dict] = {}

    # Process SSH kernels with sanity checks
    if ssh_kernels and (args.remote or not (args.local and not args.remote)):
        for kernel in ssh_kernels:
            # Print header for SSH kernels
            kernel_lines = [create_header(" SSH Kernel ", max_key_len, C)]

            # Perform checks if not skipped
            ssh_check = "(x)"
            exe_check = "(x)"
            k_check = "(x)"
            remote_cmd = ""
            ssh_ok = exec_ok = kernel_ok = False

            if not args.no_check:
                try:
                    # Check SSH connection
                    ssh_bin, ssh_ok, _ = verify_ssh_connection(kernel["host"])
                    if ssh_ok:
                        ssh_check = "(v)"

                        # Check remote executable
                        exec_ok, _ = verify_rem_executable(
                            ssh_bin,
                            kernel["host"],
                            str(
                                Path(kernel["remote_python_prefix"])
                                / "bin"
                                / "jupyter-kernel"
                            ),
                        )
                        if exec_ok:
                            exe_check = "(v)"

                        # Check remote kernel exists
                        if kernel["host"] not in remote_specs_cache:
                            try:
                                remote_specs_cache[kernel["host"]] = (
                                    fetch_remote_kernel_specs(
                                        ssh_bin,
                                        kernel["host"],
                                        str(
                                            Path(kernel["remote_python_prefix"])
                                            / "bin"
                                            / "python"
                                        ),
                                    )
                                )
                            except Exception:
                                remote_specs_cache[kernel["host"]] = {}

                        remote_specs = remote_specs_cache.get(kernel["host"], {})
                        kernel_ok = kernel["remote_kernel_name"] in remote_specs
                        if kernel_ok:
                            k_check = "(v)"
                            # Get the remote kernel's argv
                            rkn = kernel["remote_kernel_name"]
                            remote_kernel_spec = remote_specs.get(rkn, {}).get(
                                "spec", {}
                            )
                            remote_argv = remote_kernel_spec.get("argv", [])
                            if remote_argv:
                                remote_cmd = " ".join(remote_argv)
                except Exception as e:
                    logger.error(f"Error checking kernel {kernel['name']}: {e}")
            else:
                # Skip checks if --no-check is provided
                ssh_check = "(?)"
                exe_check = "(?)"
                k_check = "(?)"

            # Format SSH host with check mark/cross
            c = R if ssh_check == "(x)" else (G if ssh_check == "(v)" else C)
            formatted_ssh_host = (
                f"{c}{ssh_check[0]}{ssh_check[1:]}{N if c else ''} {kernel['host']}"
            )
            # Format remote prefix with check mark/cross
            c = R if exe_check == "(x)" else (G if exe_check == "(v)" else C)
            formatted_remote_prefix = (
                f"{c}{exe_check[0]}{exe_check[1:]}{N if c else ''} "
                + kernel["remote_python_prefix"]
            )
            # Format remote kernel with check mark/cross
            c = R if k_check == "(x)" else (G if k_check == "(v)" else C)
            formatted_remote_kernel = (
                f"{c}{k_check[0]}{k_check[1:]}{N if c else ''} "
                + kernel["remote_kernel_name"]
            )

            kernel_lines.append(f"{C}{K_NAME:<{max_key_len}}{N} {kernel['name']}")
            kernel_lines.append(
                f"{C}{K_DISP:<{max_key_len}}{N} {kernel['display_name']}"
            )
            kernel_lines.append(
                f"{C}{K_RES:<{max_key_len}}{N} {kernel['resource_dir']}"
            )
            ssh_command = (
                f"ssh {kernel['host']} jupyter-kernel "
                + f"--KernelApp.kernel_name={kernel['remote_kernel_name']}"
            )
            kernel_lines.append(f"{C}{K_CMDS:<{max_key_len}}{N} {ssh_command}")
            language = spec.get("language", "")
            if language:
                kernel_lines.append(f"{C}{K_LANG:<{max_key_len}}{N} {language}")
            interrupt_mode = (
                specs[kernel["name"]].get("spec", {}).get("interrupt_mode", "")
            )
            if interrupt_mode:
                kernel_lines.append(f"{C}{K_INT:<{max_key_len}}{N} {interrupt_mode}")
            kernel_lines.append(f"{C}{K_SSH:<{max_key_len}}{N} {formatted_ssh_host}")
            kernel_lines.append(
                f"{C}{K_RPFX:<{max_key_len}}{N} {formatted_remote_prefix}"
            )
            kernel_lines.append(
                f"{C}{K_RKER:<{max_key_len}}{N} {formatted_remote_kernel}"
            )
            if kernel_ok:
                remote_language = remote_kernel_spec.get("language", "")
                if remote_language:
                    kernel_lines.append(
                        f"{C}{K_RLANG:<{max_key_len}}{N} {remote_language}"
                    )

                # Add remote resource dir if available
                remote_resource_dir = remote_specs.get(rkn, {}).get("resource_dir", "")
                if remote_resource_dir:
                    kernel_lines.append(
                        f"{C}{K_RRES:<{max_key_len}}{N} {remote_resource_dir}"
                    )

                # Add remote interrupt_mode if it exists
                r_interrupt_mode = remote_kernel_spec.get("interrupt_mode", "")
                if r_interrupt_mode:
                    kernel_lines.append(
                        f"{C}{K_RINT:<{max_key_len}}{N} {r_interrupt_mode}"
                    )

            kernel_lines.append(f"{C}{K_TIME:<{max_key_len}}{N} {kernel['timeout']}")
            if remote_cmd:
                kernel_lines.append(f"{C}{K_RCMD:<{max_key_len}}{N} {remote_cmd}")

            print("\n".join(kernel_lines), end="\n\n")


def add_kernel(args: argparse.Namespace) -> None:
    """Add a new SSH kernel specification."""
    if (
        not args.display_name
        or not args.ssh_host_alias
        or not args.remote_python_prefix
        or not args.remote_kernel_name
    ):
        print(
            "Error: --display-name, --language, --ssh-host-alias, "
            + "--remote-python-prefix, and --remote-kernel-name are required"
        )
        sys.exit(1)

    # Validate SSH host alias
    if not RGX_SSH_HOST_ALIAS.match(args.ssh_host_alias):
        print(f"Error: Invalid SSH host alias '{args.ssh_host_alias}'")
        print(f"Must match pattern: {RGX_SSH_HOST_ALIAS.pattern!r}")
        sys.exit(1)

    # Validate remote kernel name
    if not RGX_KERNEL_NAME.match(args.remote_kernel_name):
        print(f"Error: Invalid remote kernel name '{args.remote_kernel_name}'")
        print(f"Must match pattern: {RGX_KERNEL_NAME.pattern!r}")
        sys.exit(1)

    # Generate kernel name if not provided
    kernel_name = (
        args.kernel_name or f"ssh_{args.ssh_host_alias}_{args.remote_kernel_name}"
    )

    # Check if kernel already exists
    ksm = KernelSpecManager()
    existing_specs = ksm.get_all_specs()

    if kernel_name in existing_specs and not args.replace:
        print(
            f"Error: Kernel '{kernel_name}' already exists. Use --replace to override "
            + "or specify a different --kernel-name."
        )
        sys.exit(1)

    # Create kernel spec
    kernel_spec = {
        "argv": [],
        "display_name": args.display_name,
        "language": args.language or "",
        "interrupt_mode": "message",
        "metadata": {
            "kernel_provisioner": {
                "provisioner_name": "sshpyk-provisioner",
                "config": {
                    "ssh_host_alias": args.ssh_host_alias,
                    "remote_python_prefix": args.remote_python_prefix,
                    "remote_kernel_name": args.remote_kernel_name,
                    "remote_kernel_launch_timeout": args.remote_kernel_launch_timeout,
                },
            }
        },
    }

    kernel_dir = Path(ksm.user_kernel_dir) / kernel_name
    kernel_dir.mkdir(parents=True, exist_ok=True)
    with open(kernel_dir / "kernel.json", "w", encoding="utf-8") as f:
        json.dump(kernel_spec, f, indent=2)

    print(f"Kernel specification '{kernel_name}' installed in {kernel_dir}")


def edit_kernel(args: argparse.Namespace) -> None:
    """Edit an existing SSH kernel specification."""
    if not args.kernel_name:
        print("Error: --kernel-name is required for editing")
        sys.exit(1)

    ksm = KernelSpecManager()
    specs = ksm.get_all_specs()

    if args.kernel_name not in specs:
        print(f"Error: Kernel '{args.kernel_name}' not found")
        sys.exit(1)

    # Get the kernel spec path and data
    spec_info = specs[args.kernel_name]
    kernel_dir = spec_info["resource_dir"]
    kernel_json_path = Path(kernel_dir) / "kernel.json"
    kernel_spec = spec_info.get("spec", {})

    if not kernel_spec:
        print(f"Error: empty kernel spec at {kernel_json_path}")
        sys.exit(1)

    # Check if it's an SSH kernel
    metadata = kernel_spec.get("metadata", {})
    provisioner = metadata.get("kernel_provisioner", {})
    if provisioner.get("provisioner_name") != "sshpyk-provisioner":
        print(f"Error: Kernel '{args.kernel_name}' is not an SSH kernel")
        sys.exit(1)

    # Update kernel spec
    if args.display_name:
        kernel_spec["display_name"] = args.display_name

    if args.language:
        kernel_spec["language"] = args.language

    config = provisioner.get("config", {})

    if args.ssh_host_alias:
        if not RGX_SSH_HOST_ALIAS.match(args.ssh_host_alias):
            print(f"Error: Invalid SSH host alias '{args.ssh_host_alias}'")
            print(f"Must match pattern: {RGX_SSH_HOST_ALIAS.pattern}")
            sys.exit(1)
        config["ssh_host_alias"] = args.ssh_host_alias

    if args.remote_python_prefix:
        config["remote_python_prefix"] = args.remote_python_prefix

    if args.remote_kernel_name:
        if not RGX_KERNEL_NAME.match(args.remote_kernel_name):
            print(f"Error: Invalid remote kernel name '{args.remote_kernel_name}'")
            print(f"Must match pattern: {RGX_KERNEL_NAME.pattern}")
            sys.exit(1)
        config["remote_kernel_name"] = args.remote_kernel_name

    if args.remote_kernel_launch_timeout:
        config["remote_kernel_launch_timeout"] = args.remote_kernel_launch_timeout

    # Write updated kernel.json
    with open(kernel_json_path, "w", encoding="utf-8") as f:
        json.dump(kernel_spec, f, indent=2)

    print(f"Kernel specification '{args.kernel_name}' updated in {kernel_dir}")


def delete_kernel(args: argparse.Namespace) -> None:
    """Delete a kernel specification."""
    if not args.kernel_name:
        print("Error: kernel name is required for deletion")
        sys.exit(1)

    ksm = KernelSpecManager()

    try:
        path = ksm.remove_kernel_spec(args.kernel_name)
        print(f"Removed kernel specification '{args.kernel_name}' from {path}")
    except Exception as e:
        print(f"Error removing kernel specification: {e}")
        sys.exit(1)


def setup_logging(level: int) -> None:
    """Set up logging with the specified level."""
    logging.basicConfig(level=level, format=LOG_FORMAT)
    logger.debug("Logging initialized at level %s", logging.getLevelName(level))


def main() -> None:
    """Main entry point for the CLI."""
    parser = argparse.ArgumentParser(
        description=f"Manage SSH Jupyter kernels (version {__version__})"
    )

    # Add global logging argument
    parser.add_argument(
        "--verbose",
        "-v",
        action="count",
        default=0,
        help="Increase logs verbosity (-v for warning, -vv for info, -vvv for debug)",
    )

    # Create subparsers for commands
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # List command
    list_parser = subparsers.add_parser("list", help="List available kernels")
    list_parser.add_argument(
        "--remote", "-r", action="store_true", help="List only remote SSH kernels"
    )
    list_parser.add_argument(
        "--local", "-l", action="store_true", help="List only local kernels"
    )
    list_parser.add_argument(
        "--no-check", "-n", action="store_true", help="Skip remote kernel checks"
    )
    list_parser.set_defaults(func=list_kernels)

    # Add command
    add_parser = subparsers.add_parser("add", help="Add a new SSH kernel")
    add_parser.add_argument(
        "--kernel-name",
        help="Name for the kernel (default: ssh_<host>_<remote_kernel>)",
    )
    add_parser.add_argument("--display-name", help="Display name for the kernel")
    add_parser.add_argument(
        "--language", default="python", help="Kernel language (default: python)"
    )
    add_parser.add_argument("--ssh-host-alias", required=True, help="SSH host alias")
    add_parser.add_argument(
        "--remote-python-prefix",
        required=True,
        help="Path to Python prefix on remote system",
    )
    add_parser.add_argument(
        "--remote-kernel-name",
        required=True,
        help="Kernel name on the remote system. "
        + "Use `jupyter kernelspec list` on the remote system to find it.",
    )
    add_parser.add_argument(
        "--remote-kernel-launch-timeout",
        type=int,
        default=60,
        help="Timeout for launching the remote kernel (default: 60)",
    )
    add_parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace existing kernel with the same name if it exists",
    )
    add_parser.set_defaults(func=add_kernel)

    # Edit command
    edit_parser = subparsers.add_parser("edit", help="Edit an existing SSH kernel")
    edit_parser.add_argument(
        "--kernel-name", required=True, help="Name of the kernel to edit"
    )
    edit_parser.add_argument("--display-name", help="Display name for the kernel")
    edit_parser.add_argument("--language", help="Kernel language")
    edit_parser.add_argument("--ssh-host-alias", help="SSH host alias")
    edit_parser.add_argument(
        "--remote-python-prefix", help="Path to Python prefix on remote system"
    )
    edit_parser.add_argument(
        "--remote-kernel-name",
        help="Kernel name on the remote system. "
        + "Use `jupyter kernelspec list` on the remote system to find it.",
    )
    edit_parser.add_argument(
        "--remote-kernel-launch-timeout",
        type=int,
        help="Timeout for launching the remote kernel",
    )
    edit_parser.set_defaults(func=edit_kernel)

    # Delete command
    delete_parser = subparsers.add_parser("delete", help="Delete a kernel")
    delete_parser.add_argument("kernel_name", help="Name of the kernel to delete")
    delete_parser.set_defaults(func=delete_kernel)

    # Parse arguments
    args = parser.parse_args()

    # Set up logging based on verbosity level
    log_level = logging.ERROR  # Default to ERROR level
    if args.verbose == 1:
        log_level = logging.WARNING
    elif args.verbose == 2:
        log_level = logging.INFO
    elif args.verbose >= 3:
        log_level = logging.DEBUG

    setup_logging(log_level)

    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)

    # Execute the appropriate function
    args.func(args)


if __name__ == "__main__":
    main()
