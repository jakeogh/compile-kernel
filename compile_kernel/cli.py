#!/usr/bin/env python3
# -*- coding: utf8 -*-

# pylint: disable=useless-suppression             # [I0021]
# pylint: disable=missing-docstring               # [C0111] docstrings are always outdated and wrong
# pylint: disable=missing-param-doc               # [W9015]
# pylint: disable=missing-module-docstring        # [C0114]
# pylint: disable=fixme                           # [W0511] todo encouraged
# pylint: disable=line-too-long                   # [C0301]
# pylint: disable=too-many-instance-attributes    # [R0902]
# pylint: disable=too-many-lines                  # [C0302] too many lines in module
# pylint: disable=invalid-name                    # [C0103] single letter var names, name too descriptive
# pylint: disable=too-many-return-statements      # [R0911]
# pylint: disable=too-many-branches               # [R0912]
# pylint: disable=too-many-statements             # [R0915]
# pylint: disable=too-many-arguments              # [R0913]
# pylint: disable=too-many-nested-blocks          # [R1702]
# pylint: disable=too-many-locals                 # [R0914]
# pylint: disable=too-many-public-methods         # [R0904]
# pylint: disable=too-few-public-methods          # [R0903]
# pylint: disable=no-member                       # [E1101] no member for base
# pylint: disable=attribute-defined-outside-init  # [W0201]
# pylint: disable=too-many-boolean-expressions    # [R0916] in if statement

from __future__ import annotations

import sys
from importlib import resources
from itertools import pairwise
from pathlib import Path

import click
import sh
from asserttool import ic
from asserttool import icp
from click_auto_help import AHGroup
from clicktool import click_add_options
from clicktool import click_global_options
from clicktool import tvicgvd
from eprint import eprint
from globalverbose import gvd

from compile_kernel import check_kernel_config
from compile_kernel import compile_and_install_kernel
from compile_kernel import configure_kernel
from compile_kernel import generate_module_config_dict
from compile_kernel import get_set_kernel_config_option
from compile_kernel import install_compiled_kernel

# logging.basicConfig(level=logging.INFO)
sh.mv = None  # use sh.busybox('mv'), coreutils ignores stdin read errors


@click.group(no_args_is_help=True, cls=AHGroup)
@click_add_options(click_global_options)
@click.pass_context
def cli(
    ctx,
    verbose_inf: bool,
    dict_output: bool,
    verbose: bool = False,
) -> None:
    tty, verbose = tvicgvd(
        ctx=ctx,
        verbose=verbose,
        verbose_inf=verbose_inf,
        ic=ic,
        gvd=gvd,
    )


@cli.command()
@click.option("--no-fix", is_flag=True)
@click_add_options(click_global_options)
@click.pass_context
def configure(
    ctx,
    no_fix: bool,
    verbose_inf: bool,
    dict_output: bool,
    verbose: bool = False,
):
    tty, verbose = tvicgvd(
        ctx=ctx,
        verbose=verbose,
        verbose_inf=verbose_inf,
        ic=ic,
        gvd=gvd,
    )
    if not verbose:
        ic.disable()
    else:
        ic.enable()
    if verbose_inf:
        gvd.enable()

    fix = not no_fix
    warn_only = False
    if not fix:
        warn_only = True

    configure_kernel(
        fix=fix,
        warn_only=warn_only,
        interactive=True,
    )


@cli.command()
@click.argument(
    "kernel_dir",
    type=click.Path(
        exists=True,
        dir_okay=True,
        file_okay=False,
        allow_dash=False,
        path_type=Path,
    ),
    nargs=1,
    default=Path("/usr/src/linux"),
)
@click_add_options(click_global_options)
@click.pass_context
def generate_module_to_config_mapping(
    ctx,
    kernel_dir: Path,
    verbose_inf: bool,
    dict_output: bool,
    verbose: bool = False,
):
    tty, verbose = tvicgvd(
        ctx=ctx,
        verbose=verbose,
        verbose_inf=verbose_inf,
        ic=ic,
        gvd=gvd,
    )
    if not verbose:
        ic.disable()
    else:
        ic.enable()
    if verbose_inf:
        gvd.enable()

    _m_config_dict = generate_module_config_dict(path=kernel_dir)


@cli.command()
@click.argument(
    "kernel_dir",
    type=click.Path(
        exists=True,
        dir_okay=True,
        file_okay=False,
        allow_dash=False,
        path_type=Path,
    ),
    nargs=1,
    default=Path("/usr/src/linux"),
)
@click.argument(
    "dotconfig",
    type=click.Path(
        exists=True,
        dir_okay=False,
        file_okay=True,
        allow_dash=False,
        path_type=Path,
    ),
    nargs=1,
)
@click_add_options(click_global_options)
@click.pass_context
def compare_loaded_modules_to_config(
    ctx,
    kernel_dir: Path,
    dotconfig: Path,
    verbose_inf: bool,
    dict_output: bool,
    verbose: bool = False,
):
    tty, verbose = tvicgvd(
        ctx=ctx,
        verbose=verbose,
        verbose_inf=verbose_inf,
        ic=ic,
        gvd=gvd,
    )
    if not verbose:
        ic.disable()
    else:
        ic.enable()
    if verbose_inf:
        gvd.enable()

    _m_config_dict = generate_module_config_dict(path=kernel_dir)
    _lsmod_lines = sh.lsmod().splitlines()[1:]
    _loaded_modules = []
    for _l in _lsmod_lines:
        _m = _l.split()[0]
        _loaded_modules.append(_m)
    icp(_loaded_modules)
    for _m in _loaded_modules:
        ic(_m)
        for _k, _os in _m_config_dict.items():
            for _o in _os:
                if _o == _m + ".o":
                    # print(_k, _o, _m)
                    _result = get_set_kernel_config_option(
                        path=dotconfig,
                        get=True,
                        define=_k,
                        state=False,
                        module=False,
                    )
                    ic(_result)
                    if _result not in ["y", "m"]:
                        print(f"{_k} is not enabled!")
                        # input("press enter to continue")


@cli.command()
@click.option("--configure", "--config", is_flag=True)
@click.option("--force", is_flag=True)
@click.option("--no-fix", is_flag=True)
@click.option("--symlink-config", is_flag=True)
@click.option("--no-check-boot", is_flag=True)
@click_add_options(click_global_options)
@click.pass_context
def compile_and_install(
    ctx,
    configure: bool,
    no_fix: bool,
    symlink_config: bool,
    verbose_inf: bool,
    dict_output: bool,
    force: bool,
    no_check_boot: bool,
    verbose: bool = False,
):
    tty, verbose = tvicgvd(
        ctx=ctx,
        verbose=verbose,
        verbose_inf=verbose_inf,
        ic=ic,
        gvd=gvd,
    )
    if not verbose:
        ic.disable()
    else:
        ic.enable()
    if verbose_inf:
        gvd.enable()

    fix = not no_fix
    warn_only = False
    if not fix:
        warn_only = True

    assert fix

    compile_and_install_kernel(
        configure=configure,
        force=force,
        fix=fix,
        warn_only=warn_only,
        no_check_boot=no_check_boot,
        symlink_config=symlink_config,
    )
    eprint("DONT FORGET TO UMOUNT /boot")


@cli.command("install-kernel")
@click_add_options(click_global_options)
@click.pass_context
def _install_kernel(
    ctx,
    verbose_inf: bool,
    dict_output: bool,
    verbose: bool = False,
):
    tty, verbose = tvicgvd(
        ctx=ctx,
        verbose=verbose,
        verbose_inf=verbose_inf,
        ic=ic,
        gvd=gvd,
    )
    if not verbose:
        ic.disable()
    else:
        ic.enable()
    if verbose_inf:
        gvd.enable()

    install_compiled_kernel()


@cli.command()
@click.argument(
    "dotconfigs",
    type=click.Path(
        exists=True,
        dir_okay=False,
        file_okay=True,
        allow_dash=False,
        path_type=Path,
    ),
    nargs=-1,
)
@click.option("--fix", is_flag=True)
@click_add_options(click_global_options)
@click.pass_context
def check_config(
    ctx,
    dotconfigs: tuple[Path, ...],
    fix: bool,
    verbose_inf: bool,
    dict_output: bool,
    verbose: bool = False,
):
    tty, verbose = tvicgvd(
        ctx=ctx,
        verbose=verbose,
        verbose_inf=verbose_inf,
        ic=ic,
        gvd=gvd,
    )
    if not verbose:
        ic.disable()
    else:
        ic.enable()
    if verbose_inf:
        gvd.enable()

    warn_only = False
    if not fix:
        warn_only = True

    for config in dotconfigs:
        check_kernel_config(
            path=config,
            fix=fix,
            warn_only=warn_only,
        )  # must be done after nconfig
        return


@cli.command()
@click.argument(
    "dotconfigs",
    type=click.Path(
        exists=True,
        dir_okay=False,
        file_okay=True,
        allow_dash=False,
        path_type=Path,
    ),
    nargs=-1,
)
@click_add_options(click_global_options)
@click.pass_context
def diff_config(
    ctx,
    dotconfigs: tuple[Path, ...],
    verbose_inf: bool,
    dict_output: bool,
    verbose: bool = False,
):
    tty, verbose = tvicgvd(
        ctx=ctx,
        verbose=verbose,
        verbose_inf=verbose_inf,
        ic=ic,
        gvd=gvd,
    )
    if not verbose:
        ic.disable()
    else:
        ic.enable()
    if verbose_inf:
        gvd.enable()

    with resources.path("compile_kernel", "diffconfig.py") as _diffconfig:
        icp(_diffconfig)
        for config1, config2 in pairwise(dotconfigs):
            _diffconfig_command = sh.Command("python3")
            _diffconfig_command = _diffconfig_command.bake(_diffconfig)
            _diffconfig_command = _diffconfig_command.bake(config1, config2)
            _diffconfig_command(_out=sys.stdout, _err=sys.stderr)
