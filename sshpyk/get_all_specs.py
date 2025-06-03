import json

from jupyter_client.kernelspec import KernelSpecManager

ksm = KernelSpecManager()
print(json.dumps(ksm.get_all_specs()))
