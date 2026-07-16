#!/usr/bin/env python3
# -*- coding: utf8 -*-


from __future__ import annotations

import logging
import sys
from importlib import resources
from itertools import pairwise
from pathlib import Path

import click
import hs
from dataclasses import fields
from asserttool import ic
from asserttool import icp
from click_auto_help import AHGroup
from clicktool import click_add_options
from clicktool import click_global_options
from clicktool import tvicgvd
from eprint import eprint
from globalverbose import gvd

from compile_kernel import KernelBuild
from compile_kernel import KernelFlags
from compile_kernel import build_status
from compile_kernel import check_kernel_config
from compile_kernel import check_kernel_config_perf
from compile_kernel import compile_and_install_kernel
from compile_kernel import configure_kernel
from compile_kernel import generate_module_config_dict
from compile_kernel import get_set_kernel_config_option
from compile_kernel import install_compiled_kernel
from compile_kernel import set_grub_font

click_option_code_debug = click.option("--code-debug", is_flag=True)

# Every command that configures a kernel takes the same debug-group flags.
# Defined once here and shared, so a new group is added in exactly one place.
_KERNEL_FLAG_OPTIONS = [
    click.option("--kasan", is_flag=True, help="Enable KASAN/KFENCE memory error detection"),
    click.option("--kmemleak", is_flag=True, help="Enable kmemleak memory leak detection"),
    click.option("--slub-debug", is_flag=True, help="Enable SLUB allocator debugging"),
    click.option("--lockdep", is_flag=True, help="Enable lockdep lock correctness checking"),
    click.option("--lock-stat", is_flag=True, help="Enable CONFIG_LOCK_STAT for lock contention profiling (~10-20% cost; lighter than --lockdep)"),
    click.option("--perf-profile", is_flag=True, help="Enable KALLSYMS_ALL+DEBUG_INFO_DWARF5+FRAME_POINTER for perf record --call-graph=dwarf"),
    click.option("--debug-objects", is_flag=True, help="Enable object lifecycle debugging"),
    click.option("--gcov", is_flag=True, help="Enable GCOV kernel coverage"),
    click.option("--zbtree-debug", is_flag=True, help="Enable KFENCE+SLUB_DEBUG+DEBUG_OBJECTS for out-of-tree module debugging"),
    click.option("--zfs-debug", is_flag=True, help="Enable CONFIG_FRAME_POINTER required by sys-fs/zfs USE=debug (auto-detected from the resolved USE flag)"),
    click.option("--ubsan", is_flag=True, help="Enable UBSAN undefined behaviour checks"),
    click.option("--kcsan", is_flag=True, help="Enable KCSAN data-race detector (sampling)"),
    click.option("--watchdog", is_flag=True, help="Enable softlockup/hardlockup/hung-task/WQ watchdogs"),
    click.option("--fault-inject", is_flag=True, help="Enable fault injection framework (slab/page/futex)"),
    click.option("--mem-init", is_flag=True, help="Enable memory init-on-alloc/free and page poisoning"),
    click.option("--dma-debug", is_flag=True, help="Enable DMA API correctness checking"),
    click.option("--data-struct-debug", is_flag=True, help="Enable list/SG/notifier/IRQ integrity checks"),
    click.option("--disable-netconsole", is_flag=True, help="Disable netconsole UDP kernel log (on by default)"),
    click.option("--harden", is_flag=True, help="Enable CPU mitigations and KASLR (off by default for perf)"),
    click.option("--ia32", is_flag=True, help="Enable COMPAT and IA32_EMULATION for 32-bit binaries (off by default)"),
    click.option("--bpftrace", is_flag=True, help="Enable BTF + FTRACE_SYSCALLS required by dev-debug/bpftrace"),
    click.option("--docker", is_flag=True, help="Enable container-runtime kernel options (Docker/Podman/containerd/kube)"),
    click.option("--zfs-compat-lockdep", is_flag=True, help="Disable the full lockdep selector chain (PROVE_LOCKING, LOCK_STAT, DEBUG_LOCK_ALLOC, DEBUG_SPINLOCK, DEBUG_MUTEXES, LOCKDEP) so ZFS builds when --lockdep is set"),
    click.option("--nvidia-compat", is_flag=True, help="Override LOCKDEP/SLUB_DEBUG_ON/DEBUG_MUTEXES=n so nvidia-drivers builds"),
]

_variant_option = click.option(
    "--variant",
    type=str,
    default=None,
    help="Name this build: appends -VARIANT to CONFIG_LOCALVERSION so it installs alongside (not over) the plain build of the same source — its own vmlinuz, initramfs, /lib/modules tree, and grub entry",
)

_FLAG_FIELDS = tuple(f.name for f in fields(KernelFlags) if f.name != "netconsole")


def _flags_from_kwargs(kwargs: dict) -> KernelFlags:
    """Consume the shared kernel-flag options out of click's kwargs.

    netconsole is the one group that defaults ON, so it is exposed as
    --disable-netconsole and inverted here.
    """
    values = {name: kwargs.pop(name) for name in _FLAG_FIELDS}
    values["netconsole"] = not kwargs.pop("disable_netconsole")
    return KernelFlags(**values)


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
@_variant_option
@click_add_options(_KERNEL_FLAG_OPTIONS)
@click_option_code_debug
@click_add_options(click_global_options)
@click.pass_context
def configure(
    ctx,
    no_fix: bool,
    variant: str | None,
    code_debug: bool,
    verbose_inf: bool,
    dict_output: bool,
    verbose: bool = False,
    **kwargs,
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
        logging.disable(logging.INFO)
    else:
        ic.enable()
        logging.disable(logging.NOTSET)
    if verbose_inf:
        gvd.enable()

    fix = not no_fix
    warn_only = not fix
    if code_debug:
        ic.enable()

    configure_kernel(
        fix=fix,
        warn_only=warn_only,
        interactive=True,
        flags=_flags_from_kwargs(kwargs),
        variant=variant,
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
    _lsmod_lines = hs.Command("lsmod")().splitlines()[1:]
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
@click.option("--no-fix", is_flag=True)
@click.option("--no-check-boot", is_flag=True)
@click.option(
    "--pre-module-rebuild",
    is_flag=True,
    help="Run emerge zfs @module-rebuild before the kernel compile (only useful if pre-build modules need updating)",
)
@_variant_option
@click.option(
    "--pair",
    is_flag=True,
    help="Also build a plain kernel with no debug groups, and make it the boot default. The flags given on this command line apply to the second (instrumented) kernel, which installs under --variant (default: debug). Both appear in the grub menu.",
)
@click_add_options(_KERNEL_FLAG_OPTIONS)
@click_option_code_debug
@click_add_options(click_global_options)
@click.pass_context
def compile_and_install(
    ctx,
    configure: bool,
    no_fix: bool,
    no_check_boot: bool,
    pre_module_rebuild: bool,
    variant: str | None,
    pair: bool,
    code_debug: bool,
    verbose_inf: bool,
    dict_output: bool,
    verbose: bool = False,
    **kwargs,
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
        logging.disable(logging.INFO)
    else:
        ic.enable()
        logging.disable(logging.NOTSET)
    if verbose_inf:
        gvd.enable()

    fix = not no_fix
    warn_only = not fix
    if code_debug:
        ic.enable()

    flags = _flags_from_kwargs(kwargs)

    if pair:
        if flags == KernelFlags():
            raise click.UsageError(
                "--pair with no debug groups would build the same kernel twice; "
                "pass the groups you want in the instrumented build"
            )
        # builds[0] is the boot default: plain kernel, tool defaults only.
        builds = [
            KernelBuild(flags=KernelFlags(), variant=None),
            KernelBuild(flags=flags, variant=variant or "debug"),
        ]
    else:
        builds = [KernelBuild(flags=flags, variant=variant)]

    compile_and_install_kernel(
        builds=builds,
        configure=configure,
        fix=fix,
        warn_only=warn_only,
        no_check_boot=no_check_boot,
        pre_module_rebuild=pre_module_rebuild,
    )
    eprint("DONT FORGET TO UMOUNT /boot")


@cli.command("install-kernel")
@_variant_option
@click_add_options(_KERNEL_FLAG_OPTIONS)
@click_add_options(click_global_options)
@click.pass_context
def _install_kernel(
    ctx,
    variant: str | None,
    verbose_inf: bool,
    dict_output: bool,
    verbose: bool = False,
    **kwargs,
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

    install_compiled_kernel(flags=_flags_from_kwargs(kwargs), variant=variant)


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
    metavar="DOTCONFIG...",
)
@click.option("--fix", is_flag=True)
@click_add_options(_KERNEL_FLAG_OPTIONS)
@click_option_code_debug
@click_add_options(click_global_options)
@click.pass_context
def check_config(
    ctx,
    dotconfigs: tuple[Path, ...],
    fix: bool,
    code_debug: bool,
    verbose_inf: bool,
    dict_output: bool,
    verbose: bool = False,
    **kwargs,
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
        logging.disable(logging.INFO)
    else:
        ic.enable()
        logging.disable(logging.NOTSET)
    if verbose_inf:
        gvd.enable()

    warn_only = not fix
    if code_debug:
        ic.enable()

    if not dotconfigs:
        raise click.UsageError(
            "at least one DOTCONFIG path is required (e.g. /usr/src/linux/.config or /proc/config.gz)"
        )

    flags = _flags_from_kwargs(kwargs)
    active = flags.labels()

    for config in dotconfigs:
        eprint(f"check-config: {config.resolve()}")
        eprint(f"  mode: {'fix' if fix else 'warn-only'}")
        if active:
            eprint(f"  debug groups ON: {' '.join(active)}")
        else:
            eprint("  debug groups ON: (none)")
        check_kernel_config(
            path=config,
            fix=fix,
            warn_only=warn_only,
            flags=flags,
        )


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
            _diffconfig_command = hs.Command("python3")
            _diffconfig_command.bake(_diffconfig)
            _diffconfig_command.bake(config1, config2)
            _diffconfig_command(_out=sys.stdout, _err=sys.stderr)


@cli.command("grub-font")
@click.option(
    "--size",
    type=int,
    default=12,
    show_default=True,
    help="Font size in pixels (stock GRUB unicode.pf2 is 16px)",
)
@click_add_options(click_global_options)
@click.pass_context
def grub_font(
    ctx,
    size: int,
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

    set_grub_font(size=size)


@cli.command("status")
@click_add_options(click_global_options)
@click.pass_context
def status(
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

    build_status()


@cli.command("check-config-perf")
@click.argument(
    "dotconfigs",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    nargs=-1,
)
@click_add_options(click_global_options)
@click.pass_context
def check_config_perf(
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

    if not dotconfigs:
        raise click.UsageError(
            "at least one DOTCONFIG path is required (e.g. /usr/src/linux/.config or /proc/config.gz)"
        )

    for config in dotconfigs:
        check_kernel_config_perf(path=config)
