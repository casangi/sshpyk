Remote Jupyter Kernels via SSH
##############################

Launch and connect securely to Jupyter kernels on remote machines via SSH with minimal
configuration, as if they were local.

Quick Start
***********

Get up and running with sshpyk in minutes:

1. Install sshpyk on your local machine::

    pip install sshpyk

See `Installation`_ for more details.

2. Ensure you have SSH access to your remote server and public key authentication is set up::

    # Test your connection - must connect without password prompt
    ssh remote_server_alias

See `Authentication Requirements`_ for setting up SSH keys.

3. Add a remote kernel (replace values with your configuration)::

    sshpyk add --ssh-host-alias remote_server_alias \
                --kernel-name ssh_remote_python3 \
                --display-name "Remote Python 3.10" \
                --remote-python-prefix /path/to/python/env \
                --remote-kernel-name python3 \

See `Adding a Remote Kernel`_ for all available options.

4. Start JupyterLab and select your new remote kernel for a notebook/console::

    jupyter lab

5. Your code now runs on the remote server and your local notebook interfaces with it!

Installation
************

You can install sshpyk using pip::

    pip install sshpyk

For development installation::

    pip install -e ".[dev]"

Requirements:

* On the local system: :code:`sshpyk` and :code:`jupyter_client`
* On the remote system: :code:`jupyter_client` (which provides :code:`jupyter-kernel` command)

Managing Jupyter Kernels
************************

:code:`sshpyk` provides a command-line interface to manage remote Jupyter kernels via SSH tunnels::

  $ sshpyk --help
  usage: sshpyk [-h] [--verbose] {list,add,edit,delete} ...

  Manage SSH Jupyter kernels (version 0.0)

  positional arguments:
    {list,add,edit,delete}
                          Command to execute
      list                List available kernels
      add                 Add a new SSH kernel
      edit                Edit an existing SSH kernel
      delete              Delete a kernel

  options:
    -h, --help            show this help message and exit
    --verbose, -v         Increase logs verbosity (-v for warning, -vv for info, -vvv for debug)

Listing Available Kernels
=========================

You can list all available kernels using the :code:`list` command::

  $ sshpyk list --help
  usage: sshpyk list [-h] [--remote] [--local] [--no-check]

  options:
    -h, --help      show this help message and exit
    --remote, -r    List only remote SSH kernels
    --local, -l     List only local kernels
    --no-check, -n  Skip remote kernel checks

  $ sshpyk list
  ----- Local Kernel ------
  Name:                     f310
  Display Name:             Python 3.10
  Resource Dir:             /Users/victor/Library/Jupyter/kernels/f310
  Command:                  /opt/homebrew/anaconda3/envs/f310/bin/python -m ipykernel_launcher -f {connection_file}
  Language:                 python
  Interrupt Mode:           signal

  ----- Local Kernel ------
  Name:                     ir
  Display Name:             R
  Resource Dir:             /opt/homebrew/anaconda3/envs/g/share/jupyter/kernels/ir
  Command:                  R --slave -e IRkernel::main() --args {connection_file}
  Language:                 R
  Interrupt Mode:           signal

  ------ SSH Kernel -------
  Name:                     ssh_mbp_ext
  Display Name:             Python 3.13 (mbp ext)
  Resource Dir:             /Users/victor/Library/Jupyter/kernels/ssh_mbp_ext
  Command (simplified):     ssh mbp_ext jupyter-kernel --KernelApp.kernel_name=python3 ...
  Language:                 python
  SSH Host Alias:           (v) mbp_ext
  SSH Path:                 (v) /opt/homebrew/bin/ssh
  Remote Python Prefix:     (v) /opt/homebrew/anaconda3/envs/g
  Remote Kernel Name:       (v) python3
  Start Timeout:            30
  Remote Command:           python -m ipykernel_launcher -f {connection_file}
  Remote Interrupt Mode: signal

Adding a Remote Kernel
======================

To add a new remote kernel, use the :code:`add` command. For a remote kernel to work:

* :code:`sshpyk` must be installed on the local system (which depends on :code:`jupyter_client` explicitly)
* :code:`jupyter_client` must be installed on the remote system (which provides :code:`jupyter-kernel` command)

Here's the help information for the :code:`add` command::

  $ sshpyk add --help

Editing an Existing Kernel
==========================

You can modify an existing kernel using the :code:`edit` command::

  $ sshpyk edit --help

Deleting a Kernel
=================

To remove a kernel, use the :code:`delete` command::

  $ sshpyk delete --help

SSH Configuration Notes
***********************

Understanding SSH Host Aliases
==============================

The :code:`--ssh-host-alias` parameter refers to host aliases defined in your SSH configuration, not IP addresses.
These aliases provide a convenient way to manage connections to remote systems.

.. note::
  Currently, Windows is not supported as either a local or remote machine.

Basic SSH Config Setup
======================

Your SSH configuration is typically stored in :code:`$HOME/.ssh/config`. A basic entry looks like::

  Host myserver
    HostName 192.168.1.100 # IP address of the remote system
    User myusername # your unix username on the remote system
    Port 22 # this is the default
    IdentityFile ~/.ssh/id_rsa # required for automated login
    StrictHostKeyChecking no # optional, but recommended for automation

With this configuration, you can use :code:`myserver` as your :code:`--ssh-host-alias` in sshpyk commands.

Authentication Requirements
===========================

**Important**: sshpyk only supports key-based SSH authentication. You must set up SSH key authentication
for all remote hosts you intend to use.

To set up SSH key-based authentication:

1. Generate an SSH key pair on your local machine (if you don't already have one)::

    ssh-keygen -t ed25519 -f ~/.ssh/name_of_your_key -C "some comment for your own reference"

2. Copy your public key to the remote server::

    ssh-copy-id remote_username@some.remote.server.com

Or manually add the contents of :code:`~/.ssh/name_of_your_key.pub` from your local machine to :code:`~/.ssh/authorized_keys` on the remote machine.

3. Add the key to your SSH config (edit to match your own setup)::

    Host remote_server_alias
      HostName some.remote.server.com
      User remote_username
      IdentityFile ~/.ssh/name_of_your_key
      StrictHostKeyChecking no # optional, but recommended for automation

4. Test your connection::

    # You should connect without being prompted for a password.
    ssh remote_server_alias

Advanced: Using Bastion Hosts
=============================

One powerful feature is the ability to connect to hosts behind a bastion (jump) server. For example in your SSH config::

  Host bastion
    HostName bastion.example.com
    User bastion-username
    IdentityFile ~/.ssh/id_rsa_bastion # required for automated login
    StrictHostKeyChecking no # optional, but recommended for automation

  Host internal_server
    HostName internal-server.example.com
    User remote-username
    IdentityFile ~/.ssh/id_rsa_internal # required for automated login
    ForwardX11Trusted yes
    StrictHostKeyChecking no # optional, but recommended for automation
    ProxyJump bastion # this is the key line that enables the "jump" through the bastion

This configuration allows you to:

1. Connect first to :code:`bastion.example.com` as :code:`bastion-username`
2. Then tunnel through to :code:`internal-server` as :code:`remote-username`

When using sshpyk, you would simply specify :code:`--ssh-host-alias internal-server` and the SSH tunneling
will be handled automatically according to your configuration.

.. note::
  Remember that SSH key-based authentication must be set up for both the bastion host and the internal server.

Development
###########

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
**********************

sshpyk integrates with Jupyter Client through the kernel provisioning API introduced in jupyter_client 7.0+.
It implements a custom :code:`KernelProvisionerBase` subclass called :code:`SSHKernelProvisioner` that:

1. Establishes SSH connections to remote hosts
2. Sets up port forwarding for kernel communication channels
3. Launches kernels on remote systems
4. Manages the lifecycle of remote kernels

The provisioner is registered as an entry point in :code:`pyproject.toml`, making it available to any
Jupyter application that uses `jupyter_client`.

Historical Note
***************

The design of this package was initially inspired upon `SSH Kernel <https://github.com/bernhard-42/ssh_ipykernel>`_ which
in turn is based upon `remote_ikernel <https://bitbucket.org/tdaff/remote_ikernel>`_. This implementation was
created to adapt to recent changes to :code:`jupyter_client` (which broke :code:`ssh_ipykernel`)
and to support Python 3.10+. Later it was reimplemented to integrate with jupyter_client's provisioning system.
