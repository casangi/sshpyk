from jupyter_client.manager import KernelManager
from traitlets.config.loader import Config

# Check out the source code of this function for inspiration on async code
# from jupyter_client.manager import start_new_async_kernel

# import logging
# logging.basicConfig(level=logging.INFO)  # optional

# use `sshpyk add ...` to create a sshpyk kernel
# local name of a sshpyk kernel
kernel_name = "demo_remote"  # EDIT ME <------------------------------------------------

# ######################################################################################
print("Launching remote kernel for the first time...")
# ######################################################################################

config = Config()
# keep remote kernel alive on local shutdown
config.SSHKernelProvisioner.persistent = True
# config["SSHKernelProvisioner"]["persistent"] = True # also works

km = KernelManager(kernel_name=kernel_name, config=config)
km.start_kernel()

persistent_file = km.provisioner.persistent_file
print(f"{persistent_file = }")

kc = km.client()
kc.start_channels()
kc.wait_for_ready(timeout=5)
kc.execute_interactive(
    # define a variable in the remote kernel
    code="""import socket; test_var=socket.gethostname()""",
    output_hook=lambda msg: print(msg["msg_type"], msg["content"]),
)
kc.stop_channels()
km.shutdown_kernel()  # perform local clean (e.g. close ssh tunnels)
del kc, km

# ######################################################################################
print("Provision and connect to the running remote kernel again...")
# ######################################################################################

config = Config()
config.SSHKernelProvisioner.existing = persistent_file

km = KernelManager(kernel_name=kernel_name, config=config)
km.start_kernel()
kc = km.client()
kc.start_channels()
kc.wait_for_ready(timeout=5)
kc.execute_interactive(
    # print the variable that should still be in the memory of the remote kernel
    code="""print(test_var)""",
    output_hook=lambda msg: print(msg["msg_type"], msg["content"]),
)
kc.stop_channels()
# this time the remote kernel will be shutdown, persistent=False by default
km.shutdown_kernel()  # shutdown remote kernel and clean
del kc, km
exit()
