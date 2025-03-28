"""Command line interface to manage sshpyk kernel specifications."""

import argparse
import json
import sys
from pathlib import Path

from jupyter_client.kernelspec import KernelSpecManager

from .provisioning import RGX_KERNEL_NAME, RGX_SSH_HOST_ALIAS


def list_kernels(args: argparse.Namespace) -> None:
    """List all available kernels."""
    specs = KernelSpecManager().get_all_specs()

    if not specs:
        print("No kernels available")
        return

    # Collect data first to determine column widths
    rows = []
    max_host_len = len("SSH Host")  # Minimum width for header
    max_path_len = len("Path")  # Minimum width for header

    for name, spec_info in sorted(specs.items()):
        spec = spec_info.get("spec", {})
        display_name = spec.get("display_name", "")
        resource_dir = spec_info.get("resource_dir", "")

        # Check if it's an SSH kernel
        metadata = spec.get("metadata", {})
        provisioner = metadata.get("kernel_provisioner", {})
        is_ssh = provisioner.get("provisioner_name") == "sshpyk-provisioner"

        host = ""
        if is_ssh:
            host = provisioner.get("config", {}).get("ssh_host_alias", "")
            max_host_len = max(max_host_len, len(host))

        max_path_len = max(max_path_len, len(resource_dir))
        rows.append((name, display_name, host, resource_dir))

    # Format output with dynamic column widths
    name_len = max(len(name) for name, _, _, _ in rows)
    name_len = max(name_len, len("Name"))

    display_len = max(len(display) for _, display, _, _ in rows)
    display_len = max(display_len, len("Display Name"))

    print(
        f"{'Display Name'.ljust(display_len)} | {'Name'.ljust(name_len)} | "
        f"{'SSH Host'.ljust(max_host_len)} | {'Path'}"
    )
    print(
        f"{'-' * display_len}-+-{'-' * name_len}-+-{'-' * max_host_len}-+-{'-' * max_path_len}"  # noqa: E501
    )

    for name, display_name, host, resource_dir in rows:
        print(
            f"{display_name.ljust(display_len)} | {name.ljust(name_len)} | "
            f"{host.ljust(max_host_len)} | {resource_dir}"
        )


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

    # Get the kernel spec path
    spec_info = specs[args.kernel_name]
    kernel_dir = spec_info["resource_dir"]
    kernel_json_path = Path(kernel_dir) / "kernel.json"

    if not kernel_json_path.exists():
        print(f"Error: kernel.json not found at {kernel_json_path}")
        sys.exit(1)

    # Read existing kernel spec
    with open(kernel_json_path, "r", encoding="utf-8") as f:
        kernel_spec = json.load(f)

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


def main() -> None:
    """Main entry point for the CLI."""
    parser = argparse.ArgumentParser(
        description="Manage SSH Jupyter kernels via SSH tunnels"
    )

    # Create subparsers for commands
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # List command
    list_parser = subparsers.add_parser("list", help="List available kernels")
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

    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)

    # Execute the appropriate function
    args.func(args)


if __name__ == "__main__":
    main()
