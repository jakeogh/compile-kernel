#!/usr/bin/env python3
# -*- coding: utf8 -*-

# flake8: noqa           # flake8 has no per file settings :(
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
# pylint: disable=C0305  # Trailing newlines editor should fix automatically, pointless warning


import os
import sys
import time
from pathlib import Path
from typing import Optional

import click
import sh
from asserttool import pause
from asserttool import root_user
from pathtool import file_exists_nonzero
from run_command import run_command
from with_chdir import chdir

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


def verify_kernel_config_setting(*,
                                 location: Path,
                                 content: str,
                                 define: str,
                                 required_state: bool,
                                 warn: bool,
                                 verbose: bool,
                                 debug: bool,
                                 url: Optional[str] = None,
                                 ):
    if verbose or debug:
        ic(location, len(content), define, required_state, warn, url)

    state_table = {True: 'enabled', False: 'disabled'}
    assert isinstance(required_state, bool)

    current_state = None
    #found_define = None

    msg = ''
    if url:
        msg += " See: {url}".format(url=url)

    if define + ' is not set' not in content:
        # the define could be enabled
        if define + '=y' in content:
            found_define = True
            current_state = True
        if define + 'm' in content:
            found_define = True
            current_state = True
    else:
        # the define is disabled
        found_define = False
        current_state = False

    if current_state == required_state:
        return   # all is well

    # mypy: Invalid index type "Optional[bool]" for "Dict[bool, str]"; expected type "bool"  [index] (E)
    msg = "{define} is {status}!".format(define=define, status=state_table[current_state],) + msg
    if warn:
        msg = "WARNING: " + msg
        eprint(location.as_posix(), msg)
        pause('press any key to continue')
        return

    msg = "ERROR: " + msg
    raise ValueError(location.as_posix(), msg)


def check_kernel_config(*,
                        verbose: bool,
                        debug: bool,
                        ):
    #locations = [Path('/proc/config.gz'), Path('/usr/src/linux/.config')]
    locations = [Path('/usr/src/linux/.config')]
    assert locations[0].exists()
    for location in locations:
        if not location.exists():
            ic('skipping:', location)
            continue
        try:
            content = sh.zcat(location)
        except sh.ErrorReturnCode_1 as e:
            if hasattr(e, 'stderr'):
                if b'/usr/src/linux/.config: not in gzip format' in e.stderr:
                    content = sh.cat(location)
                else:
                    raise e

        verify_kernel_config_setting(location=location,
                                     content=content,
                                     define='CONFIG_INTEL_IOMMU_DEFAULT_ON',
                                     required_state=False,
                                     warn=True,
                                     url='http://forums.debian.net/viewtopic.php?t=126397',
                                     verbose=verbose,
                                     debug=debug,
                                     )

        verify_kernel_config_setting(location=location,
                                     content=content,
                                     define='CONFIG_IKCONFIG_PROC',
                                     required_state=True,
                                     warn=False,
                                     url=None,
                                     verbose=verbose,
                                     debug=debug,
                                     )

        verify_kernel_config_setting(location=location,
                                     content=content,
                                     define='CONFIG_IKCONFIG',
                                     required_state=True,
                                     warn=False,
                                     url=None,
                                     verbose=verbose,
                                     debug=debug,
                                     )

        verify_kernel_config_setting(location=location,
                                     content=content,
                                     define='CONFIG_SUNRPC_DEBUG',
                                     required_state=True,
                                     warn=False,
                                     url=None,
                                     verbose=verbose,
                                     debug=debug,
                                     )

        verify_kernel_config_setting(location=location,
                                     content=content,
                                     define='CONFIG_DEBUG_INFO',
                                     required_state=True,
                                     warn=False,
                                     url=None,
                                     verbose=verbose,
                                     debug=debug,
                                     )

        verify_kernel_config_setting(location=location,
                                     content=content,
                                     define='CONFIG_COMPILE_TEST',
                                     required_state=False,
                                     warn=False,
                                     url=None,
                                     verbose=verbose,
                                     debug=debug,
                                     )

        verify_kernel_config_setting(location=location,
                                     content=content,
                                     define='CONFIG_FRAME_POINTER',
                                     required_state=True,
                                     warn=False,
                                     url=None,
                                     verbose=verbose,
                                     debug=debug,
                                     )

        verify_kernel_config_setting(location=location,
                                     content=content,
                                     define='CONFIG_CRYPTO_USER',
                                     required_state=True,
                                     warn=False,
                                     url=None,
                                     verbose=verbose,
                                     debug=debug,
                                     )

        verify_kernel_config_setting(location=location,
                                     content=content,
                                     define='CONFIG_DRM',
                                     required_state=True,
                                     warn=False,
                                     url='https://wiki.gentoo.org/wiki/Nouveau',
                                     verbose=verbose,
                                     debug=debug,
                                     )

        verify_kernel_config_setting(location=location,
                                     content=content,
                                     define='CONFIG_DRM_FBDEV_EMULATION',
                                     required_state=True,
                                     warn=False,
                                     url='https://wiki.gentoo.org/wiki/Nouveau',
                                     verbose=verbose,
                                     debug=debug,
                                     )

        verify_kernel_config_setting(location=location,
                                     content=content,
                                     define='CONFIG_DRM_NOUVEAU:',
                                     required_state=True,   # =m
                                     warn=False,
                                     url='https://wiki.gentoo.org/wiki/Nouveau',
                                     verbose=verbose,
                                     debug=debug,
                                     )


def symlink_config(*,
                   verbose: bool,
                   debug: bool,
                   ):

    dot_config = Path('/usr/src/linux/.config')
    if dot_config.exists():
        if not dot_config.is_symlink():
            timestamp = str(time.time())
            sh.mv(dot_config, '/home/cfg/sysskel/usr/src/linux_configs/.config.' + timestamp)

    if not dot_config.exists():
        sh.ln('-s', '/home/cfg/sysskel/usr/src/linux_configs/.config', dot_config)


def check_config_enviroment(*,
                            verbose: bool,
                            debug: bool,
                            ):

    # https://www.mail-archive.com/lede-dev@lists.infradead.org/msg07290.html
    if not (os.getenv('KCONFIG_OVERWRITECONFIG') == '1'):
        ic('KCONFIG_OVERWRITECONFIG=1 needs to be set to 1')
        ic('add it to /etc/env.d/99kconfig-symlink. Exiting.')
        sys.exit(1)


def get_kernel_version_from_symlink():
    linux = Path('/usr/src/linux')
    assert linux.is_symlink()
    path = linux.resolve()
    version = path.parts[-1]
    version = version.split('linux-')[-1]
    return version


def boot_is_correct(*,
                    linux_version: str,
                    verbose: bool,
                    debug: bool,
                    ):
    assets = ['System.map', 'initramfs', 'vmlinux']
    for asset in assets:
        path = Path(asset) / Path('-') / Path(linux_version)
        if not file_exists_nonzero(path):
            return False
    return True


def gcc_check(*,
              verbose: bool,
              debug: bool,
              ):

    test_path = Path("/usr/src/linux/init/.init_task.o.cmd")
    if test_path.exists():

        ic('found previously compiled kernel tree, checking is the current gcc version was used')
        gcc_version = sh.gcc_config('-l')
        gcc_version = gcc_version.splitlines()
        line = None
        for line in gcc_version:
            if not line.endswith('*'):
                continue
        assert line
        gcc_version = line.split('-')[-1]
        gcc_version = gcc_version.split(' ')[0]
        ic('checking for gcc version:', gcc_version)

        try:
            sh.grep('gcc/x86_64-pc-linux-gnu/' + gcc_version, '/usr/src/linux/init/.init_task.o.cmd')
            ic(gcc_version, 'was used to compile kernel previously, not running `make clean`')
        except sh.ErrorReturnCode_1:
            ic('old gcc version detected, make clean required. Sleeping 5.')
            os.chdir('/usr/src/linux')
            time.sleep(5)
            sh.make('clean')


def kernel_is_already_compiled(verbose: bool,
                               debug: bool,
                               ):
    kernel_version = get_kernel_version_from_symlink()
    ic(kernel_version)
    test_path = Path("/usr/src/linux/init/.init_task.o.cmd")

    if Path("/boot/initramfs").exists():    # should be looking for the current kernel version
        if Path("/boot/initramfs").stat().st_size > 0:
            #if Path("/usr/src/linux/include/linux/kconfig.h").exists():
            if test_path.exists():
                eprint('/boot/initramfs and {} exist, skipping compile'.format(test_path.as_posix()))
                return True
        ic('/boot/initramfs exists, checking if /usr/src/linux is configured')
        if test_path.exists():
            ic(test_path, 'exists, skipping kernel compile')
            return True


def kcompile(*,
             configure: bool,
             force: bool,
             no_check_boot: bool,
             verbose: bool,
             debug: bool,
             ):
    ic()
    if not root_user():
        raise ValueError('you must be root')

    #columns = get_terminal_size().columns
    #columns = 80
    unconfigured_kernel = None

    if no_check_boot:
        ic('skipped checking if /boot was mounted')
    else:
        if not Path('/boot/grub/grub.cfg').exists():
            ic('/boot/grub/grub.cfg not found. Exiting.')
            sys.exit(1)

        if not Path('/boot/kernel').exists():
            ic('mount /boot first. Exiting.')
            sys.exit(1)

    check_config_enviroment(verbose=verbose, debug=debug,)
    symlink_config(verbose=verbose, debug=debug,)
    assert Path('/usr/src/linux/.config').is_symlink()

    for line in sh.emerge('genkernel', '-u', _err_to_out=True, _iter=True,):
        eprint(line, end='')

    # handle a downgrade from -9999 before genkernel calls @module-rebuild
    ic('attempting to upgrade zfs and zfs-kmod')
    try:
        for line in sh.emerge('sys-fs/zfs', 'sys-fs/zfs-kmod', '-u', _err_to_out=True, _iter=True,):
            eprint(line, end='')
    except sh.ErrorReturnCode_1 as e:
        #ic(e)
        unconfigured_kernel = False
        #ic(dir(e))  # this lists e.stdout
        #ic(e.stdout)
        #ic(e.stderr)
        #assert False
        if hasattr(e, 'stdout'):
            #ic(e.stdout)
            #ic(type(e.stdout))  # <class 'bytes'>  #hmph. the next line should cause a TypeError (before making the str bytes) ... but didnt
            if b'Could not find a usable .config' in e.stdout:
                unconfigured_kernel = True
            if b'Kernel sources need compiling first' in e.stdout:
                unconfigured_kernel = True
            if b'Could not find a Makefile in the kernel source directory.' in e.stdout:
                unconfigured_kernel = True

        #assert e.stdout
        #if hasattr(e, 'stdout'):
        #    ic(e.stdout)
        if not unconfigured_kernel:
            #ic(unconfigured_kernel)
            raise e
        ic('NOTE: kernel is unconfigured, skipping `emerge sys-fs/zfs sys-fs/zfs-kmod` before kernel compile')

    ic('attempting emerge @module-rebuild')
    try:
        for line in sh.emerge('@module-rebuild', _err_to_out=True, _iter=True,):
            eprint(line, end='')
    except sh.ErrorReturnCode_1 as e:
        unconfigured_kernel = True # todo, get conditions from above
        if not unconfigured_kernel:
            raise e
        ic('NOTE: kernel is unconfigured, skipping `emerge @module-rebuild` before kernel compile')


    # might fail if gcc was upgraded and the kernel hasnt been recompiled yet
    #for line in sh.emerge('sci-libs/linux-gpib', '-u', _err_to_out=True, _iter=True, _out_bufsize=100):
    #   eprint(line, end='')

    if configure:
        with chdir('/usr/src/linux'):
            os.system('make nconfig')
        check_kernel_config(verbose=verbose, debug=debug,)  # must be done after nconfig

    gcc_check(verbose=verbose, debug=debug,)

    os.chdir('/usr/src/linux')

    linux_version = get_kernel_version_from_symlink()
    ic(boot_is_correct(linux_version=linux_version,
                       verbose=verbose,
                       debug=debug,))

    if not force:
        if kernel_is_already_compiled(verbose=verbose,
                                      debug=debug,):
            ic('kernel is already compiled, skipping')
            return


    genkernel_command = ['genkernel']
    genkernel_command.append('all')
    #if configure:
    #    genkernel_command.append('--nconfig')
    genkernel_command.append('--no-clean')
    genkernel_command.append('--symlink')
    genkernel_command.append('--module-rebuild')
    genkernel_command.append('--all-ramdisk-modules')
    genkernel_command.append('--firmware')
    genkernel_command.append('--microcode=all')
    genkernel_command.append('--microcode-initramfs')
    genkernel_command.append('--makeopts="-j12"')
    genkernel_command.append('--callback="/usr/bin/emerge zfs zfs-kmod @module-rebuild"')
    #--callback="/usr/bin/emerge zfs zfs-kmod sci-libs/linux-gpib sci-libs/linux-gpib-modules @module-rebuild"
    #--zfs
    run_command(genkernel_command, verbose=True, system=True)

    sh.rc_update('add', 'zfs-import', 'boot')
    sh.rc_update('add', 'zfs-share', 'default')
    sh.rc_update('add', 'zfs-zed', 'default')

    if Path('/mnt/boot/grub').is_dir():
        sh.grub_mkconfig('-o', '/boot/grub/grub.cfg')

    for line in sh.emerge('sys-kernel/linux-firmware', _err_to_out=True, _iter=True,):
        eprint(line, end='')

    os.makedirs('/boot_backup', exist_ok=True)
    with chdir('/boot_backup'):
        if not Path('/boot_backup/.git').is_dir():
            sh.git.init()

        sh.git.config('user.email', "user@example.com")
        sh.git.config('user.name', "user")

        timestamp = str(time.time())
        os.makedirs(timestamp)
        sh.cp('-ar', '/boot', timestamp + '/')
        sh.git.add(timestamp, '--force')
        sh.git.commit('-m', timestamp)

    ic('kernel compile and install completed OK')


@click.command()
@click.option('--configure', '--config', is_flag=True)
@click.option('--verbose', is_flag=True)
@click.option('--debug', is_flag=True)
@click.option('--force', is_flag=True)
@click.option('--no-check-boot', is_flag=True)
@click.pass_context
def cli(ctx,
        configure: bool,
        verbose: bool,
        debug: bool,
        force: bool,
        no_check_boot: bool,
        ):

    kcompile(configure=configure,
             force=force,
             no_check_boot=no_check_boot,
             verbose=verbose,
             debug=debug,)
