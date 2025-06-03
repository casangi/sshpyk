# Automated SSH Authentication via `sshpass` (Last Resort)

**⚠️ EXTREME CAUTION ADVISED ⚠️**

This directory contains an example `ssh-sshpass-wrapper` script and a corresponding `config` file. These are intended as a **last resort** for automating SSH connections to remote hosts that **ONLY** support password-based authentication and where private/public key authentication is unequivocally not an option.

## Motivation

Ideally, SSH authentication should always be performed using private/public key pairs for security and manageability. However, in some highly restrictive environments, this might not be possible. This wrapper provides a mechanism to automate password entry using `sshpass`, by, for example, retrieving a password from a secure keychain.

## Security Considerations

**❗️This method is inherently less secure than key-based authentication and carries significant risks if not handled properly. You are responsible for ensuring the security of your password storage and retrieval mechanisms❗️**

- The primary risk is exposing your password. Ensure the script that retrieves the password and `ssh-sshpass-wrapper` itself have very restrictive permissions (e.g., `chmod 700`).
- `sshpass` sends the password when it gets triggered by a specific target string in the ssh's output, by default the `assword` string to match things like `password:`. While generally functional, be aware of potential security implications on multi-user systems where other users might inspect processes or environment variables (though `sshpass` attempts some minor mitigations).
- The wrapper script is designed to use `sshpass` _only_ when needed (missing or lost shh control socket) and _only_ to execute a dummy remote command, minimizing the risk of the password being accidentally transmitted to some process on the remote machine. For your own security, don't break this design!
- The security of this entire setup hinges on the security of your chosen password storage (keychain, password manager).

## Instructions

1.  **Understand the Risks**: Before proceeding, ensure you fully understand the security implications of scripting password authentication.
2.  **Install `sshpass`**: This wrapper relies on `sshpass`. Install it on your local system (e.g., `brew install sshpass` on macOS).
3.  **Adapt the Wrapper**:
    - Open `ssh-sshpass-wrapper`.
    - Modify the `jump_hostname`, `jump_alias`, `ssh_config_file`, and `SSH_EXECUTABLE` variables to match your environment.
    - **Crucially**, modify the `export SSHPASS=$(...)` line to securely retrieve your password. **DO NOT hardcode your password directly in the script.** Use a secure method like macOS Keychain (as in the example), GNOME Keyring, KeePassXC, or another password manager's CLI.
4.  **Adapt the SSH `config`**:
    - Open `config`.
    - Modify the `Host sshpyk_jump` and `Host sshpyk_host` entries.
      - Ensure `Hostname` and `User` are correct for your jump host (if any) and target host.
      - The example `config` uses `PreferredAuthentications keyboard-interactive,password` for the jump host. This is for clarity, your host is enforcing it anyway if you are reading this.
      - The `ProxyJump` directive is used if you need to connect through a bastion/jump host. You can remove this line otherwise.
    - The `ControlMaster`, `ControlPath`, and `ControlPersist` settings under `Host *` are always required for `sshpyk` to function correctly and efficiently.
5.  **Make the Wrapper Executable**:
    ```bash
    chmod +x ssh-sshpass-wrapper
    ```
6.  **Add a Kernel Specification with `sshpyk`**:
    When adding your remote kernel using `sshpyk add`, you need to tell `sshpyk` to use this wrapper script instead of the default `ssh` command. You also need to point to the specific SSH config file.

    Replace the placeholders with your actual values:

    ```bash
    sshpyk add \
        --kernel-name my_password_kernel \
        --display-name "Python (Password Auth)" \
        --ssh-host-alias sshpyk_host \
        --remote-python /path/to/remote/python_env/bin/python \
        --remote-kernel-name python3 \
        --language python \
        --ssh /path/to/ssh-sshpass-wrapper \
        --ssh-config /path/to/config
    ```

    - `--ssh-host-alias sshpyk_host`: This should match the target host alias in your `dangerous/config` file.
    - `--ssh /path/to/ssh-sshpass-wrapper`: **Absolute path** to the wrapper script.
    - `--ssh-config /path/to/config`: **Absolute path** to the SSH config file.

7.  **Test it**:

    Attempt to launch the kernel from a shell. Check the outputs for any errors.

    ```bash
    sshpyk-kernel --kernel my_password_kernel --debug
    ```

**This solution should only be used when all other authentication methods have been exhausted and you have taken all possible precautions to secure your credentials.**
