import os
import json
from os import execvp
from functools import reduce as fold
from uuid import uuid4
from shutil import which
from os.path import isfile, join
from argparse import ArgumentParser, SUPPRESS
from subprocess import run, PIPE

# No tabs, no multiline, quote { and } !
KERNEL_SCRIPT = """
import os
fname = os.path.expanduser("{fname}")
from jupyter_client import write_connection_file
write_connection_file(fname=fname, ip="{ip}", key=b"{key}", transport="{transport}", signature_scheme="{signature_scheme}", kernel_name="{kernel_name}")
fd = open(fname, "r")
ci = fd.read()
fd.close()
print(ci)
"""

def main( host, python, connection_info, env ):
    ssh = which('ssh')
    remote_kernel_file = f'''/tmp/.sshpyk_{uuid4( )}.json'''
    substituted_script = KERNEL_SCRIPT.format(fname=remote_kernel_file, **connection_info)
    script = '; '.join(substituted_script.strip( ).split("\n"))

    ###
    ### Create remote kernel file with port information...
    ###
    result = run( [ ssh, host, f"""{python} -c '{script}'""" ], stdout=PIPE, stderr=PIPE )
    remote_state = json.loads(result.stdout.decode('utf-8'))
    f = open( "/tmp/out.txt", "w" )
    print( '--------------------------------------------------------------------------------', file=f )
    print( repr(remote_state), file=f )
    print( '--------------------------------------------------------------------------------', file=f )
    if type(remote_state) != dict or len(remote_state) <= 0:
        exit( 'Creation of remote ipykernel state failed.' )

    ###
    ### Launch kernel on remote system with SSH tunnels between the local ports
    ### and the remote ports... tunnel format is '-L {local}:IP:{remote}' where
    ### IP is one of 127.0.0.1 or localhost (not sure if this is referring to
    ### the local or remote host...
    ###
    ### This also removes the kernel configuration file after the ipykernel exits...
    ###
    ssh_env = env if env else [ ]
    ssh_tunnels = fold( lambda acc,k: [ '-L', f'''{connection_info[k]}:{connection_info['ip']}:{remote_state[k]}''' ] + acc,
                        filter( lambda k: k.endswith('_port'), remote_state.keys() ), [] )
    print( repr( ssh_tunnels ), file=f )
    print( '--------------------------------------------------------------------------------', file=f )
    f.close( )
    execvp( ssh, [ 'ssh', '-q', '-t', *ssh_tunnels, host, f'''{' '.join(ssh_env)} {python} -m ipykernel_launcher --HistoryManager.hist_file=:memory: -f {remote_kernel_file}; rm -f {remote_kernel_file}''' ] )

if __name__ == "__main__":
    parse = ArgumentParser( add_help=False )
    optional = parse.add_argument_group("optional arguments")

    ### prevents --help from appearing in it's own "options:" group
    optional.add_argument( "--help", "-h", action="help", default=SUPPRESS, help="show this help message and exit" )
    optional.add_argument( "--timeout", "-t", type=int, help="timeout for remote commands", default=5 )
    optional.add_argument( "--name", "-n", type=str, help="kernel name" )
    optional.add_argument( "--env", "-e", nargs="*",
                           help="environment variables for the remote kernel in the form: VAR1=value1 VAR2=value2" )
    optional.add_argument( "-s", action="store_true", help="sudo required to start kernel on the remote machine" )


    required = parse.add_argument_group("required arguments")
    required.add_argument( "--file", "-f", required=True, help="jupyter kernel connection file" )
    required.add_argument( "--host", "-H", required=True, help="remote host" )
    required.add_argument( "--python", "-p", required=True, help="remote python_path" )

    args = parse.parse_args( )

    if args.name is not None:
        if len(args.name.split( )) > 1:
            exit( f'''Provided kernel name ('{args.name}') cannot contain whitespace.''' )

    if not isfile(args.file):
        exit( f'''Specified file ('{args.file}') does not exist.''' )

    with open(args.file, "r") as fd:
        connection_info = json.loads(fd.read())

    ## seems like newer versions of the connection file do not include 'kernel_name'
    if args.name is not None:
        connection_info['kernel_name'] = args.name
    elif 'kernel_name' not in connection_info:
        connection_info['kernel_name'] = f'''ipykrn{os.getpid( )}'''

    main( args.host, join(args.python, 'bin', 'python'), connection_info, args.env )