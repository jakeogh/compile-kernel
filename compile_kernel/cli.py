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
from asserttool import ic
from asserttool import icp
from click_auto_help import AHGroup
from clicktool import click_add_options
from clicktool import click_global_options
from clicktool import tvicgvd
from eprint import eprint
from globalverbose import gvd

from compile_kernel import check_kernel_config
from compile_kernel import check_kernel_config_perf
from compile_kernel import compile_and_install_kernel
from compile_kernel import configure_kernel
from compile_kernel import generate_module_config_dict
from compile_kernel import get_set_kernel_config_option
from compile_kernel import install_compiled_kernel
from compile_kernel import set_grub_font

click_option_code_debug = click.option("--code-debug", is_flag=True)


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
@click.option(
    "--kasan", is_flag=True, help="Enable KASAN/KFENCE memory error detection"
)
@click.option("--kmemleak", is_flag=True, help="Enable kmemleak memory leak detection")
@click.option("--slub-debug", is_flag=True, help="Enable SLUB allocator debugging")
@click.option(
    "--lockdep", is_flag=True, help="Enable lockdep lock correctness checking"
)
@click.option("--debug-objects", is_flag=True, help="Enable object lifecycle debugging")
@click.option("--gcov", is_flag=True, help="Enable GCOV kernel coverage")
@click.option(
    "--zbtree-debug",
    is_flag=True,
    help="Enable KFENCE+SLUB_DEBUG+DEBUG_OBJECTS for out-of-tree module debugging",
)
@click.option(
    "--zfs-debug",
    is_flag=True,
    help="Enable CONFIG_FRAME_POINTER required by sys-fs/zfs USE=debug",
)
@click.option(
    "--lock-stat",
    is_flag=True,
    help="Enable CONFIG_LOCK_STAT for lock contention profiling (~10-20% cost; lighter than --lockdep)",
)
@click.option(
    "--perf-profile",
    is_flag=True,
    help="Enable KALLSYMS_ALL+DEBUG_INFO_DWARF5+FRAME_POINTER for perf record --call-graph=dwarf",
)
@click.option(
    "--harden",
    is_flag=True,
    help="Enable CPU mitigations and KASLR (off by default for perf)",
)
@click.option(
    "--ia32",
    is_flag=True,
    help="Enable COMPAT and IA32_EMULATION for 32-bit binaries (off by default)",
)
@click.option(
    "--bpftrace",
    is_flag=True,
    help="Enable BTF + FTRACE_SYSCALLS required by dev-debug/bpftrace",
)
@click.option(
    "--zfs-compat-lockdep",
    is_flag=True,
    help="Disable the full lockdep selector chain (PROVE_LOCKING, LOCK_STAT, DEBUG_LOCK_ALLOC, DEBUG_SPINLOCK, DEBUG_MUTEXES, LOCKDEP) so ZFS builds when --lockdep is set",
)
@click.option(
    "--nvidia-compat",
    is_flag=True,
    help="Override LOCKDEP/SLUB_DEBUG_ON/DEBUG_MUTEXES=n so nvidia-drivers builds",
)
@click_option_code_debug
@click_add_options(click_global_options)
@click.pass_context
def configure(
    ctx,
    no_fix: bool,
    kasan: bool,
    kmemleak: bool,
    slub_debug: bool,
    lockdep: bool,
    debug_objects: bool,
    gcov: bool,
    zbtree_debug: bool,
    zfs_debug: bool,
    lock_stat: bool,
    perf_profile: bool,
    harden: bool,
    ia32: bool,
    bpftrace: bool,
    zfs_compat_lockdep: bool,
    nvidia_compat: bool,
    code_debug: bool,
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
        logging.disable(logging.INFO)
    else:
        ic.enable()
        logging.disable(logging.NOTSET)
    if verbose_inf:
        gvd.enable()

    fix = not no_fix
    warn_only = False
    if not fix:
        warn_only = True
    if code_debug:
        ic.enable()

    configure_kernel(
        fix=fix,
        warn_only=warn_only,
        interactive=True,
        kasan=kasan,
        kmemleak=kmemleak,
        slub_debug=slub_debug,
        lockdep=lockdep,
        debug_objects=debug_objects,
        gcov=gcov,
        zbtree_debug=zbtree_debug,
        zfs_debug=zfs_debug,
        lock_stat=lock_stat,
        perf_profile=perf_profile,
        harden=harden,
        ia32=ia32,
        bpftrace=bpftrace,
        zfs_compat_lockdep=zfs_compat_lockdep,
        nvidia_compat=nvidia_compat,
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
@click.option("--force", is_flag=True)
@click.option("--no-fix", is_flag=True)
@click.option("--symlink-config", is_flag=True)
@click.option("--no-check-boot", is_flag=True)
@click.option("--pre-module-rebuild", is_flag=True, help="Run emerge @module-rebuild before kernel compile (genkernel's post-build callback already handles this; only useful if pre-build modules need updating)")
@click.option(
    "--kasan", is_flag=True, help="Enable KASAN/KFENCE memory error detection"
)
@click.option("--kmemleak", is_flag=True, help="Enable kmemleak memory leak detection")
@click.option("--slub-debug", is_flag=True, help="Enable SLUB allocator debugging")
@click.option(
    "--lockdep", is_flag=True, help="Enable lockdep lock correctness checking"
)
@click.option("--debug-objects", is_flag=True, help="Enable object lifecycle debugging")
@click.option("--gcov", is_flag=True, help="Enable GCOV kernel coverage")
@click.option(
    "--zbtree-debug",
    is_flag=True,
    help="Enable KFENCE+SLUB_DEBUG+DEBUG_OBJECTS for out-of-tree module debugging",
)
@click.option(
    "--zfs-debug",
    is_flag=True,
    help="Enable CONFIG_FRAME_POINTER required by sys-fs/zfs USE=debug",
)
@click.option("--ubsan", is_flag=True, help="Enable UBSAN undefined behaviour checks")
@click.option("--kcsan", is_flag=True, help="Enable KCSAN data-race detector (sampling)")
@click.option("--watchdog", is_flag=True, help="Enable softlockup/hardlockup/hung-task/WQ watchdogs")
@click.option("--fault-inject", is_flag=True, help="Enable fault injection framework (slab/page/futex)")
@click.option("--mem-init", is_flag=True, help="Enable memory init-on-alloc/free and page poisoning")
@click.option("--dma-debug", is_flag=True, help="Enable DMA API correctness checking")
@click.option("--data-struct-debug", is_flag=True, help="Enable list/SG/notifier/IRQ integrity checks")
@click.option("--disable-netconsole", is_flag=True, help="Disable netconsole UDP kernel log (on by default)")
@click.option(
    "--lock-stat",
    is_flag=True,
    help="Enable CONFIG_LOCK_STAT for lock contention profiling (~10-20% cost; lighter than --lockdep)",
)
@click.option(
    "--perf-profile",
    is_flag=True,
    help="Enable KALLSYMS_ALL+DEBUG_INFO_DWARF5+FRAME_POINTER for perf record --call-graph=dwarf",
)
@click.option(
    "--harden",
    is_flag=True,
    help="Enable CPU mitigations and KASLR (off by default for perf)",
)
@click.option(
    "--ia32",
    is_flag=True,
    help="Enable COMPAT and IA32_EMULATION for 32-bit binaries (off by default)",
)
@click.option(
    "--bpftrace",
    is_flag=True,
    help="Enable BTF + FTRACE_SYSCALLS required by dev-debug/bpftrace",
)
@click.option(
    "--zfs-compat-lockdep",
    is_flag=True,
    help="Disable the full lockdep selector chain (PROVE_LOCKING, LOCK_STAT, DEBUG_LOCK_ALLOC, DEBUG_SPINLOCK, DEBUG_MUTEXES, LOCKDEP) so ZFS builds when --lockdep is set",
)
@click.option(
    "--nvidia-compat",
    is_flag=True,
    help="Override LOCKDEP/SLUB_DEBUG_ON/DEBUG_MUTEXES=n so nvidia-drivers builds",
)
@click_option_code_debug
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
    pre_module_rebuild: bool,
    kasan: bool,
    kmemleak: bool,
    slub_debug: bool,
    lockdep: bool,
    debug_objects: bool,
    gcov: bool,
    zbtree_debug: bool,
    zfs_debug: bool,
    ubsan: bool,
    kcsan: bool,
    watchdog: bool,
    fault_inject: bool,
    mem_init: bool,
    dma_debug: bool,
    data_struct_debug: bool,
    disable_netconsole: bool,
    lock_stat: bool,
    perf_profile: bool,
    harden: bool,
    ia32: bool,
    bpftrace: bool,
    zfs_compat_lockdep: bool,
    nvidia_compat: bool,
    code_debug: bool,
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
        logging.disable(logging.INFO)
    else:
        ic.enable()
        logging.disable(logging.NOTSET)
    if verbose_inf:
        gvd.enable()

    fix = not no_fix
    warn_only = False
    if not fix:
        warn_only = True
    if code_debug:
        ic.enable()

    compile_and_install_kernel(
        configure=configure,
        force=force,
        fix=fix,
        warn_only=warn_only,
        no_check_boot=no_check_boot,
        symlink_config=symlink_config,
        pre_module_rebuild=pre_module_rebuild,
        kasan=kasan,
        kmemleak=kmemleak,
        slub_debug=slub_debug,
        lockdep=lockdep,
        debug_objects=debug_objects,
        gcov=gcov,
        zbtree_debug=zbtree_debug,
        zfs_debug=zfs_debug,
        ubsan=ubsan,
        kcsan=kcsan,
        watchdog=watchdog,
        fault_inject=fault_inject,
        mem_init=mem_init,
        dma_debug=dma_debug,
        data_struct_debug=data_struct_debug,
        netconsole=not disable_netconsole,
        lock_stat=lock_stat,
        perf_profile=perf_profile,
        harden=harden,
        ia32=ia32,
        bpftrace=bpftrace,
        zfs_compat_lockdep=zfs_compat_lockdep,
        nvidia_compat=nvidia_compat,
    )
    eprint("DONT FORGET TO UMOUNT /boot")


@cli.command("install-kernel")
@click.option(
    "--kasan", is_flag=True, help="Enable KASAN/KFENCE memory error detection"
)
@click.option("--kmemleak", is_flag=True, help="Enable kmemleak memory leak detection")
@click.option("--slub-debug", is_flag=True, help="Enable SLUB allocator debugging")
@click.option(
    "--lockdep", is_flag=True, help="Enable lockdep lock correctness checking"
)
@click.option("--debug-objects", is_flag=True, help="Enable object lifecycle debugging")
@click.option("--gcov", is_flag=True, help="Enable GCOV kernel coverage")
@click.option(
    "--zbtree-debug",
    is_flag=True,
    help="Enable KFENCE+SLUB_DEBUG+DEBUG_OBJECTS for out-of-tree module debugging",
)
@click.option(
    "--zfs-debug",
    is_flag=True,
    help="Enable CONFIG_FRAME_POINTER required by sys-fs/zfs USE=debug",
)
@click.option(
    "--lock-stat",
    is_flag=True,
    help="Enable CONFIG_LOCK_STAT for lock contention profiling (~10-20% cost; lighter than --lockdep)",
)
@click.option(
    "--perf-profile",
    is_flag=True,
    help="Enable KALLSYMS_ALL+DEBUG_INFO_DWARF5+FRAME_POINTER for perf record --call-graph=dwarf",
)
@click.option(
    "--harden",
    is_flag=True,
    help="Enable CPU mitigations and KASLR (off by default for perf)",
)
@click.option(
    "--ia32",
    is_flag=True,
    help="Enable COMPAT and IA32_EMULATION for 32-bit binaries (off by default)",
)
@click.option(
    "--bpftrace",
    is_flag=True,
    help="Enable BTF + FTRACE_SYSCALLS required by dev-debug/bpftrace",
)
@click.option(
    "--zfs-compat-lockdep",
    is_flag=True,
    help="Disable the full lockdep selector chain (PROVE_LOCKING, LOCK_STAT, DEBUG_LOCK_ALLOC, DEBUG_SPINLOCK, DEBUG_MUTEXES, LOCKDEP) so ZFS builds when --lockdep is set",
)
@click.option(
    "--nvidia-compat",
    is_flag=True,
    help="Override LOCKDEP/SLUB_DEBUG_ON/DEBUG_MUTEXES=n so nvidia-drivers builds",
)
@click_add_options(click_global_options)
@click.pass_context
def _install_kernel(
    ctx,
    kasan: bool,
    kmemleak: bool,
    slub_debug: bool,
    lockdep: bool,
    debug_objects: bool,
    gcov: bool,
    zbtree_debug: bool,
    zfs_debug: bool,
    lock_stat: bool,
    perf_profile: bool,
    harden: bool,
    ia32: bool,
    bpftrace: bool,
    zfs_compat_lockdep: bool,
    nvidia_compat: bool,
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

    install_compiled_kernel(
        kasan=kasan,
        kmemleak=kmemleak,
        slub_debug=slub_debug,
        lockdep=lockdep,
        debug_objects=debug_objects,
        gcov=gcov,
        zbtree_debug=zbtree_debug,
        zfs_debug=zfs_debug,
        lock_stat=lock_stat,
        perf_profile=perf_profile,
        harden=harden,
        ia32=ia32,
        bpftrace=bpftrace,
        zfs_compat_lockdep=zfs_compat_lockdep,
        nvidia_compat=nvidia_compat,
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
    metavar="DOTCONFIG...",
)
@click.option("--fix", is_flag=True)
@click.option(
    "--kasan", is_flag=True, help="Enable KASAN/KFENCE memory error detection"
)
@click.option("--kmemleak", is_flag=True, help="Enable kmemleak memory leak detection")
@click.option("--slub-debug", is_flag=True, help="Enable SLUB allocator debugging")
@click.option(
    "--lockdep", is_flag=True, help="Enable lockdep lock correctness checking"
)
@click.option("--debug-objects", is_flag=True, help="Enable object lifecycle debugging")
@click.option("--gcov", is_flag=True, help="Enable GCOV kernel coverage")
@click.option(
    "--zbtree-debug",
    is_flag=True,
    help="Enable KFENCE+SLUB_DEBUG+DEBUG_OBJECTS for out-of-tree module debugging",
)
@click.option(
    "--zfs-debug",
    is_flag=True,
    help="Enable CONFIG_FRAME_POINTER required by sys-fs/zfs USE=debug",
)
@click.option(
    "--lock-stat",
    is_flag=True,
    help="Enable CONFIG_LOCK_STAT for lock contention profiling (~10-20% cost; lighter than --lockdep)",
)
@click.option(
    "--perf-profile",
    is_flag=True,
    help="Enable KALLSYMS_ALL+DEBUG_INFO_DWARF5+FRAME_POINTER for perf record --call-graph=dwarf",
)
@click.option(
    "--harden",
    is_flag=True,
    help="Enable CPU mitigations and KASLR (off by default for perf)",
)
@click.option(
    "--ia32",
    is_flag=True,
    help="Enable COMPAT and IA32_EMULATION for 32-bit binaries (off by default)",
)
@click.option(
    "--bpftrace",
    is_flag=True,
    help="Enable BTF + FTRACE_SYSCALLS required by dev-debug/bpftrace",
)
@click.option(
    "--zfs-compat-lockdep",
    is_flag=True,
    help="Disable the full lockdep selector chain (PROVE_LOCKING, LOCK_STAT, DEBUG_LOCK_ALLOC, DEBUG_SPINLOCK, DEBUG_MUTEXES, LOCKDEP) so ZFS builds when --lockdep is set",
)
@click.option(
    "--nvidia-compat",
    is_flag=True,
    help="Override LOCKDEP/SLUB_DEBUG_ON/DEBUG_MUTEXES=n so nvidia-drivers builds",
)
@click_option_code_debug
@click_add_options(click_global_options)
@click.pass_context
def check_config(
    ctx,
    dotconfigs: tuple[Path, ...],
    fix: bool,
    kasan: bool,
    kmemleak: bool,
    slub_debug: bool,
    lockdep: bool,
    debug_objects: bool,
    gcov: bool,
    zbtree_debug: bool,
    zfs_debug: bool,
    lock_stat: bool,
    perf_profile: bool,
    harden: bool,
    ia32: bool,
    bpftrace: bool,
    zfs_compat_lockdep: bool,
    nvidia_compat: bool,
    code_debug: bool,
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
        logging.disable(logging.INFO)
    else:
        ic.enable()
        logging.disable(logging.NOTSET)
    if verbose_inf:
        gvd.enable()

    warn_only = False
    if not fix:
        warn_only = True
    if code_debug:
        ic.enable()

    if not dotconfigs:
        raise click.UsageError(
            "at least one DOTCONFIG path is required (e.g. /usr/src/linux/.config or /proc/config.gz)"
        )

    debug_flags: dict[str, bool] = {
        "kasan": kasan,
        "kmemleak": kmemleak,
        "slub-debug": slub_debug,
        "lockdep": lockdep,
        "debug-objects": debug_objects,
        "gcov": gcov,
        "zbtree-debug": zbtree_debug,
        "zfs-debug": zfs_debug,
        "lock-stat": lock_stat,
        "perf-profile": perf_profile,
        "harden": harden,
        "ia32": ia32,
        "bpftrace": bpftrace,
        "zfs-compat": zfs_compat_lockdep,
        "nvidia-compat": nvidia_compat,
    }

    for config in dotconfigs:
        eprint(f"check-config: {config.resolve()}")
        eprint(f"  mode: {'fix' if fix else 'warn-only'}")
        active = [k for k, v in debug_flags.items() if v]
        inactive = [k for k, v in debug_flags.items() if not v]
        if active:
            eprint(f"  debug groups ON:  {' '.join(active)}")
        if inactive:
            eprint(f"  debug groups OFF: {' '.join(inactive)}")
        check_kernel_config(
            path=config,
            fix=fix,
            warn_only=warn_only,
            kasan=kasan,
            kmemleak=kmemleak,
            slub_debug=slub_debug,
            lockdep=lockdep,
            debug_objects=debug_objects,
            gcov=gcov,
            zbtree_debug=zbtree_debug,
            zfs_debug=zfs_debug,
            lock_stat=lock_stat,
            perf_profile=perf_profile,
            harden=harden,
            ia32=ia32,
            bpftrace=bpftrace,
            zfs_compat_lockdep=zfs_compat_lockdep,
            nvidia_compat=nvidia_compat,
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
