#!/usr/bin/env python3
# -*- coding: utf8 -*-

# pylint: disable=C0111  # docstrings are always outdated and wrong
# pylint: disable=W0511  # todo is encouraged
# pylint: disable=C0301  # line too long
# pylint: disable=R0902  # too many instance attributes
# pylint: disable=C0302  # too many lines in module
# pylint: disable=C0103  # single letter var names, func name too descriptive
# pylint: disable=R0911  # too many return statements
# pylint: disable=R0912  # too many branches
# pylint: disable=R0915  # too many statements
# pylint: disable=R0913  # too many arguments
# pylint: disable=R1702  # too many nested blocks
# pylint: disable=R0914  # too many local variables
# pylint: disable=R0903  # too few public methods
# pylint: disable=E1101  # no member for base
# pylint: disable=W0201  # attribute defined outside __init__
# pylint: disable=R0916  # Too many boolean expressions in if statement


import os
import sys
import time
from pathlib import Path
from shutil import get_terminal_size

import click
import sh
from kcl.commandops import run_command
from kcl.userops import am_root

#from sh import ErrorReturnCode_1
#from sh.contrib import git


def eprint(*args, **kwargs):
    if 'file' in kwargs.keys():
        kwargs.pop('file')
    print(*args, file=sys.stderr, **kwargs)


try:
    from icecream import ic  # https://github.com/gruns/icecream
except ImportError:
    ic = eprint


def symlink_config(*,
                   verbose: bool,
                   debug: bool,):

    dot_config = Path('/usr/src/linux/.config')
    if dot_config.exists():
        if not dot_config.is_symlink():
            timestamp = str(time.time())
            sh.mv(dot_config, '/home/cfg/sysskel/usr/src/linux_configs/.config.' + timestamp)

    if not dot_config.exists():
        sh.ln('-s', '/home/cfg/sysskel/usr/src/linux_configs/.config', dot_config)


def check_config_enviroment(*,
                            verbose: bool,
                            debug: bool,):

    # https://www.mail-archive.com/lede-dev@lists.infradead.org/msg07290.html
    if not (os.getenv('KCONFIG_OVERWRITECONFIG') == '1'):
        ic('KCONFIG_OVERWRITECONFIG=1 needs to be set to 1')
        ic('add it to /etc/env.d/99kconfig-symlink. Exiting.')
        sys.exit(1)


def gcc_check(*,
              verbose: bool,
              debug: bool,):

    test_path = Path("/usr/src/linux/init/.init_task.o.cmd")
    if test_path.exists():

        ic('found previously compiled kernel tree, checking is the current gcc version was used')
        gcc_version = sh.gcc_config('-l')
        gcc_version = gcc_version.splitlines()
        for line in gcc_version:
            if not line.endswith('*'):
                continue
        assert line
        gcc_version = line.split('-')[-1]
        gcc_version = gcc_version.split(' ')[0]
        ic('checking for gcc version:', gcc_version)

        try:
            sh.grep('gcc/x86_64-pc-linux-gnu/' + gcc_version, '/usr/src/linux/init/.init_task.o.cmd')
            ic(gcc_version, 'was used to compile kernel previously, not running \"make clean\"')
        except sh.ErrorReturnCode_1:
            ic('old gcc version detected, make clean required. Sleeping 5.')
            os.chdir('/usr/src/linux')
            time.sleep(5)
            sh.make('clean')


def kcompile(*,
             configure: bool,
             force: bool,
             no_check_boot: bool,
             verbose: bool,
             debug: bool,):
    ic()
    am_root()
    #columns = get_terminal_size().columns
    columns = 80

    if no_check_boot:
        ic('skipped checking if /boot was mounted')
    else:
        if not Path('/boot/grub/grub.cfg').exists():
            ic('/boot/grub/grub.cfg not found. Exiting.')
            sys.exit(1)

        if not Path('/boot/kernel').exists():
            ic('mount /boot first. Exiting.')
            sys.exit(1)

    for line in sh.emerge('genkernel', '-u', _err_to_out=True, _iter=True, _out_bufsize=columns):
        eprint(line)

    # handle a downgrade from -9999 before genkernel calls @module-rebuild
    for line in sh.emerge('sys-fs/zfs', _err_to_out=True, _iter=True, _out_bufsize=columns):
        eprint(line)

    for line in sh.emerge('sys-fs/zfs-kmod', _err_to_out=True, _iter=True, _out_bufsize=columns):
        eprint(line)

    for line in sh.emerge('@module-rebuild', _err_to_out=True, _iter=True, _out_bufsize=columns):
        eprint(line)

    # might fail if gcc was upgraded and the kernel hasnt been recompiled yet
    #for line in sh.emerge('sci-libs/linux-gpib', '-u', _err_to_out=True, _iter=True, _out_bufsize=100):
    #   eprint(line)

    genkernel_command = ['genkernel']
    genkernel_command.append('all')
    if configure:
        genkernel_command.append('--nconfig')
    genkernel_command.append('--no-clean')
    genkernel_command.append('--symlink')
    genkernel_command.append('--module-rebuild')
    genkernel_command.append('--all-ramdisk-modules')
    genkernel_command.append('--makeopts="-j12"')
    #--callback="/usr/bin/emerge zfs zfs-kmod sci-libs/linux-gpib-modules @module-rebuild"
    #--callback="/usr/bin/emerge zfs zfs-kmod sci-libs/linux-gpib sci-libs/linux-gpib-modules @module-rebuild"
    #--zfs
    gcc_check(verbose=verbose, debug=debug,)
    check_config_enviroment(verbose=verbose, debug=debug,)
    symlink_config(verbose=verbose, debug=debug,)

    os.chdir('/usr/src/linux')

    test_path = Path("/usr/src/linux/init/.init_task.o.cmd")

    if not configure:
        if Path("/boot/initramfs").exists():
            if Path("/boot/initramfs").stat().st_size > 0:
                if Path("/usr/src/linux/include/linux/kconfig.h").exists():
                    ic('/boot/initramfs and /usr/src/linux/include/linux/kconfig.h exist, skiping compile')
                    return
            ic('/boot/initramfs exists, checking if /usr/src/linux is configured')
            if test_path.exists():
                if not force:
                    ic(test_path, 'exists, skipping kernel compile')
                    return
                else:
                    ic('found configured /usr/src/linux, but --force was specified so not skipping recompile')

    run_command(genkernel_command, verbose=True, system=True)

    sh.rc_update('add', 'zfs-import', 'boot')
    sh.rc_update('add', 'zfs-share', 'default')
    sh.rc_update('add', 'zfs-zed', 'default')

    sh.grub_mkconfig('-o', '/boot/grub/grub.cfg')

    for line in sh.emerge('sys-kernel/linux-firmware', _err_to_out=True, _iter=True, _out_bufsize=columns):
        eprint(line)

    os.makedirs('/boot_backup', exist_ok=True)
    os.chdir('/boot_backup')
    if not Path('/boot_backup/.git').is_dir():
        sh.git.init()

    timestamp = str(time.time())
    os.makedirs(timestamp)
    sh.cp('-ar', '/boot', timestamp + '/')
    sh.git.add(timestamp, '--force')
    sh.git.commit('-m', timestamp)
    ic('kernel compile and install completed OK')


@click.command()
@click.option('--configure', is_flag=True)
@click.option('--verbose', is_flag=True)
@click.option('--debug', is_flag=True)
@click.option('--force', is_flag=True)
@click.option('--no-check-boot', is_flag=True)
@click.option('--ipython', is_flag=True)
@click.option("--printn", is_flag=True)
@click.pass_context
def cli(ctx,
        configure,
        verbose,
        debug,
        force,
        no_check_boot,
        ipython,
        printn,):

    null = not printn
    end = '\n'
    if null:
        end = '\x00'
    if sys.stdout.isatty():
        end = '\n'
        assert not ipython

    ctx.ensure_object(dict)
    ctx.obj['verbose'] = verbose
    ctx.obj['debug'] = debug
    ctx.obj['end'] = end
    ctx.obj['null'] = null
    ctx.obj['force'] = force

    kcompile(configure=configure,
             force=force,
             no_check_boot=no_check_boot,
             verbose=verbose,
             debug=debug,)

