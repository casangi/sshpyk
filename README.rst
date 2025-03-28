Remote Jupyter Kernels via SSH tunnels
######################################

Why sshpyk?
**********

Data scientists and researchers often need to:

* Run computations on powerful remote servers while using local notebooks
* Access specialized hardware (GPUs, large memory) not available locally
* Work with data that can't leave secure environments due to size or privacy
* Maintain a consistent development environment across multiple machines

sshpyk solves these problems by:

* Creating secure SSH tunnels to remote Jupyter kernels with minimal configuration
* Supporting modern Jupyter Client (7.0+) with its kernel provisioning API
* Enabling passwordless, key-based authentication for seamless connections
* Working with complex network setups including bastion/jump hosts

The design of this package is based upon `SSH Kernel <https://github.com/bernhard-42/ssh_ipykernel>`_ which
in turn is based upon `remote_ikernel <https://bitbucket.org/tdaff/remote_ikernel>`_. This implementation was
created to adapt to recent changes to :code:`jupyter_client` (which broke :code:`ssh_ipykernel`)
and to support Python 3.10+.

Installation
***********

You can install sshpyk using pip::

    pip install sshpyk

For development installation::

    pip install -e ".[dev]"

Requirements:

* On the local system: :code:`sshpyk` and :code:`jupyter_client`
* On the remote system: :code:`jupyter_client` (which provides :code:`jupyter-kernel` command)

Managing Jupyter Kernels
************************

:code:`sshpyk` provides a command-line interface to manage remote Jupyter kernels via SSH tunnels.

Listing Available Kernels
=========================

You can list all available kernels using the :code:`list` command::

  $ sshpyk list
  Display Name                    | Name    | SSH Host | Path
  --------------------------------+---------+----------+-------------------------------------------------------------
  Python 3.10                     | f310    |          | /Users/victor/Library/Jupyter/kernels/f310
  Python 3.9 Pipelines            | f39p    |          | /Users/victor/Library/Jupyter/kernels/f39p
  Python 3 Tiago                  | f39t    |          | /Users/victor/Library/Jupyter/kernels/f39t
  R                               | ir      |          | /opt/homebrew/anaconda3/envs/g/share/jupyter/kernels/ir
  Python 3 (ipykernel)            | python3 |          | /opt/homebrew/anaconda3/envs/g/share/jupyter/kernels/python3
  Python 3.9 (Remote MacBook Air) | ssh_p39 | mba      | /Users/victor/Library/Jupyter/kernels/ssh_p39

Adding a Remote Kernel
======================

To add a new remote kernel, use the :code:`add` command. For a remote kernel to work:

* :code:`sshpyk` must be installed on the local system (which depends on :code:`jupyter_client` explicitly)
* :code:`jupyter_client` must be installed on the remote system (which provides :code:`jupyter-kernel` command)

Here's the help information for the :code:`add` command::

  $ sshpyk add --help
  usage: sshpyk add [-h] [--kernel-name KERNEL_NAME] [--display-name DISPLAY_NAME] [--language LANGUAGE] --ssh-host-alias SSH_HOST_ALIAS --remote-python-prefix REMOTE_PYTHON_PREFIX
                    --remote-kernel-name REMOTE_KERNEL_NAME [--remote-kernel-launch-timeout REMOTE_KERNEL_LAUNCH_TIMEOUT] [--replace]

  options:
    -h, --help            show this help message and exit
    --kernel-name KERNEL_NAME
                          Name for the kernel (default: ssh_<host>_<remote_kernel>)
    --display-name DISPLAY_NAME
                          Display name for the kernel
    --language LANGUAGE   Kernel language (default: python)
    --ssh-host-alias SSH_HOST_ALIAS
                          SSH host alias
    --remote-python-prefix REMOTE_PYTHON_PREFIX
                          Path to Python prefix on remote system
    --remote-kernel-name REMOTE_KERNEL_NAME
                          Kernel name on the remote system. Use `jupyter kernelspec list` on the remote system to find it.
    --remote-kernel-launch-timeout REMOTE_KERNEL_LAUNCH_TIMEOUT
                          Timeout for launching the remote kernel (default: 60)
    --replace             Replace existing kernel with the same name if it exists

Editing an Existing Kernel
==========================

You can modify an existing kernel using the :code:`edit` command::

  $ sshpyk edit --help
  usage: sshpyk edit [-h] --kernel-name KERNEL_NAME [--display-name DISPLAY_NAME] [--language LANGUAGE] [--ssh-host-alias SSH_HOST_ALIAS] [--remote-python-prefix REMOTE_PYTHON_PREFIX]
                     [--remote-kernel-name REMOTE_KERNEL_NAME] [--remote-kernel-launch-timeout REMOTE_KERNEL_LAUNCH_TIMEOUT]

  options:
    -h, --help            show this help message and exit
    --kernel-name KERNEL_NAME
                          Name of the kernel to edit
    --display-name DISPLAY_NAME
                          Display name for the kernel
    --language LANGUAGE   Kernel language
    --ssh-host-alias SSH_HOST_ALIAS
                          SSH host alias
    --remote-python-prefix REMOTE_PYTHON_PREFIX
                          Path to Python prefix on remote system
    --remote-kernel-name REMOTE_KERNEL_NAME
                          Kernel name on the remote system. Use `jupyter kernelspec list` on the remote system to find it.
    --remote-kernel-launch-timeout REMOTE_KERNEL_LAUNCH_TIMEOUT
                          Timeout for launching the remote kernel

Deleting a Kernel
=================

To remove a kernel, use the :code:`delete` command::

  $ sshpyk delete --help
  usage: sshpyk delete [-h] kernel_name

  positional arguments:
    kernel_name  Name of the kernel to delete

  options:
    -h, --help   show this help message and exit

SSH Configuration Notes
**********************

Understanding SSH Host Aliases
=============================

The :code:`--ssh-host-alias` parameter refers to host aliases defined in your SSH configuration, not IP addresses.
These aliases provide a convenient way to manage connections to remote systems.

.. note::
   Currently, Windows is not supported as either a local or remote machine.

Basic SSH Config Setup
=====================

Your SSH configuration is typically stored in :code:`$HOME/.ssh/config`. A basic entry looks like::

  Host myserver
    HostName 192.168.1.100 # IP address of the remote system
    User myusername # your unix username on the remote system
    Port 22 # this is the default
    IdentityFile ~/.ssh/id_rsa # required for passwordless login
    StrictHostKeyChecking no # optional, but recommended for automation

With this configuration, you can use :code:`myserver` as your :code:`--ssh-host-alias` in sshpyk commands.

Authentication Requirements
==========================

**Important**: sshpyk only supports passwordless SSH authentication. You must set up key-based authentication
for all remote hosts you intend to use.

To set up passwordless SSH authentication:

1. Generate an SSH key pair on your local machine (if you don't already have one)::

     ssh-keygen -t ed25519 -C "your_email@example.com"

2. Copy your public key to the remote server::

     ssh-copy-id username@remote-host

   Or manually add the contents of :code:`~/.ssh/id_ed25519.pub` to :code:`~/.ssh/authorized_keys` on the remote machine.

3. Test your connection::

     ssh remote-host

   You should connect without being prompted for a password.

Advanced: Using Bastion Hosts
============================

One powerful feature is the ability to connect to hosts behind a bastion (jump) server. For example in your SSH config::

  Host bastion
    HostName bastion.example.com
    User bastion-username
    IdentityFile ~/.ssh/id_rsa_bastion # required for passwordless login
    StrictHostKeyChecking no # optional, but recommended for automation

  Host internal_server
    HostName internal-server.example.com
    User remote-username
    IdentityFile ~/.ssh/id_rsa_internal # required for passwordless login
    ForwardX11Trusted yes
    StrictHostKeyChecking no # optional, but recommended for automation
    ProxyJump bastion # this is the key line that enables the "jump" through the bastion

This configuration allows you to:

1. Connect first to :code:`bastion.example.com` as :code:`bastion-username`
2. Then tunnel through to :code:`internal-server` as :code:`remote-username`

When using sshpyk, you would simply specify :code:`--ssh-host-alias internal-server` and the SSH tunneling
will be handled automatically according to your configuration.

.. note::
   Remember that passwordless authentication must be set up for both the bastion host and the internal server.

Development
##########

In a Python 3.8+ environment:

1. `pip install -e ".[dev]"` # installs the python package in editable mode
2. Reload your shell, e.g. open the terminal again.
3. `pre-commit install`
4. Make your changes to the files and test them.
5. `git commit -m "your message"`, this will run the pre-commit hooks defined in `.pre-commit-config.yaml`. If your code has problems it won't let you commit.

Run git hooks manually
**********************

To auto-format code, apply other small fixes (e.g. trailing whitespace) and to lint all the code:

`pre-commit run --all-files`

Implementation Details
*********************

sshpyk integrates with Jupyter Client through the kernel provisioning API introduced in jupyter_client 7.0+.
It implements a custom :code:`KernelProvisionerBase` subclass called :code:`SSHKernelProvisioner` that:

1. Establishes SSH connections to remote hosts
2. Sets up port forwarding for kernel communication channels
3. Launches kernels on remote systems
4. Manages the lifecycle of remote kernels

The provisioner is registered as an entry point in :code:`pyproject.toml`, making it available to any
Jupyter application that uses `jupyter_client`.
