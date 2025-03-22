import json
from argparse import SUPPRESS, ArgumentParser
from os.path import exists, join

from jupyter_client import kernelspec as ks

from .utils import kinfo_exe, rexists


def _read_json(kernel_path):
    with open(join(kernel_path, "kernel.json")) as f:
        return json.load(f)
    return None


def _is_valid_kernel(kinfo):
    ex = kinfo_exe(kinfo[1])
    return exists(ex[0]) and (ex[1] is None or rexists(ex[2], ex[1]))


def get_kernel_desc(all=False, valid_only=True):
    km = ks.KernelSpecManager()
    kdirs = km.find_kernel_specs()
    keys = sorted(
        kdirs.keys() if all else filter(lambda k: k.startswith("ssh_"), kdirs.keys())
    )
    result = {
        k: {"ssh": k.startswith("ssh_"), "path": kdirs[k], "spec": _read_json(kdirs[k])}
        for k in keys
    }
    if not valid_only:
        return result

    return dict(filter(_is_valid_kernel, result.items()))


def _red(s):
    return s if args.no_color else f"\033[31m{s}\033[0m"


def _green(s):
    return s if args.no_color else f"\033[32m{s}\033[0m"


def _kernel_paths(kinfo):
    colsize = 0
    for k in kinfo.keys():
        if len(k) > colsize:
            colsize = len(k)
    for k, info in kinfo.items():
        e = kinfo_exe(info)
        if info["ssh"]:
            # remote kernel spec
            # e[0] => local python path
            # e[1] => remote python path
            # e[2] => remote host name
            problems = []
            ok = True
            if args.verbose:
                if e[0] is None:
                    problems.append("no local Python path provided")
                    ok = False
                elif not exists(e[0]):
                    problems.append(f"local Python path does not exist: {e[0]}")
                    ok = False
                if e[1] is None:
                    problems.append("no remote Python path provided")
                    ok = False
                if e[2] is None:
                    problems.append("no remote host name provided")
                    ok = False
                if e[1] is not None and e[2] is not None and not rexists(e[2], e[1]):
                    problems.append(f"remote Python path does not exist: {e[1]}")
                    ok = False

            if (
                args.verbose
                and ok
                or not args.verbose
                and (
                    args.no_check
                    or e[0] is not None
                    and e[1] is not None
                    and e[2] is not None
                    and exists(e[0])
                    and rexists(e[2], e[1])
                )
            ):
                print(f"{k.ljust(colsize)} {_green(info['path'])}")
            else:
                print(f"{k.ljust(colsize)} {_red(info['path'])}")
                for problem in problems:
                    print(f"{k.ljust(colsize)} {_red('>>> ' + problem)}")
        else:
            # local kernel spec
            # e[0] => local python path
            problems = []
            ok = True
            if args.verbose:
                if e[0] is None:
                    problems.append("no local Python path provided")
                    ok = False
                elif not exists(e[0]):
                    problems.append(f"local Python path does not exist: {e[0]}")
                    ok = False

            if (
                args.verbose
                and ok
                or not args.verbose
                and (
                    args.no_check
                    or e[0] is not None
                    and (not e[0].startswith("/") or exists(e[0]))
                )
            ):
                print(f"{k.ljust(colsize)} {_green(info['path'])}")
            else:
                print(f"{k.ljust(colsize)} {_red(info['path'])}")
                for problem in problems:
                    print(f"{k.ljust(colsize)} {_red('>>> ' + problem)}")


def _local_paths(kinfo):
    colsize = 0
    for k in kinfo.keys():
        if len(k) > colsize:
            colsize = len(k)
    for k, info in kinfo.items():
        e = kinfo_exe(info)
        # e[0] => local python path
        problems = []
        ok = True
        if args.verbose:
            if e[0] is None:
                problems.append("no local Python path provided")
                ok = False
            elif not exists(e[0]):
                problems.append(f"local Python path does not exist: {e[0]}")
                ok = False

        if (
            args.verbose
            and ok
            or not args.verbose
            and (
                args.no_check
                or e[0] is not None
                and (not e[0].startswith("/") or exists(e[0]))
            )
        ):
            print(f"{k.ljust(colsize)} {_green(e[0])}")
        else:
            print(f"{k.ljust(colsize)} {_red(e[0])}")
            for problem in problems:
                print(f"{k.ljust(colsize)} {_red('>>> ' + problem)}")


def _remote_paths(kinfo):
    colsize = 0
    for k in kinfo.keys():
        if len(k) > colsize:
            colsize = len(k)
    for k, info in kinfo.items():
        e = kinfo_exe(info)
        if info["ssh"]:
            # remote kernel spec
            # e[0] => local python path
            # e[1] => remote python path
            # e[2] => remote host name
            problems = []
            ok = True
            if args.verbose:
                if e[1] is None:
                    problems.append("no remote Python path provided")
                    ok = False
                if e[2] is None:
                    problems.append("no remote host name provided")
                    ok = False
                if e[1] is not None and e[2] is not None and not rexists(e[2], e[1]):
                    problems.append(f"remote Python path does not exist: {e[1]}")
                    ok = False

            if (
                args.verbose
                and ok
                or not args.verbose
                and (
                    args.no_check
                    or e[1] is not None
                    and e[2] is not None
                    and rexists(e[2], e[1])
                )
            ):
                print(f"{k.ljust(colsize)} {_green(e[2])}:{e[1]}")
            else:
                print(f"{k.ljust(colsize)} {_red(e[2] + ':' + e[1])}")
                for problem in problems:
                    print(f"{k.ljust(colsize)} {_red('>>> ' + problem)}")


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
        "--all",
        "-a",
        action="store_true",
        help="list all kernels (not just ssh/sshpyk)",
    )
    optional.add_argument(
        "--local",
        "-l",
        action="store_true",
        help="list the information for the local python executable",
    )
    optional.add_argument(
        "--remote",
        "-r",
        action="store_true",
        help="list the information for the remote python executable",
    )
    optional.add_argument(
        "--no-check", action="store_true", help="do not check for Python executables"
    )
    optional.add_argument(
        "--no-color", action="store_true", help="do not check for Python executables"
    )
    optional.add_argument("--verbose", "-V", action="store_true", help="verbose output")

    args = parse.parse_args()

    kinfo = get_kernel_desc(all=args.all, valid_only=False)
    if args.local:
        _local_paths(kinfo)
    elif args.remote:
        _remote_paths(kinfo)
    else:
        _kernel_paths(kinfo)
