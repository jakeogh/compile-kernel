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

import logging
import os
import sys
import time
from importlib import resources
from pathlib import Path

import click
import sh
from asserttool import ic
from asserttool import icp
from asserttool import pause
from asserttool import root_user
from click_auto_help import AHGroup
from clicktool import click_add_options
from clicktool import click_global_options
from clicktool import tv
from eprint import eprint
from globalverbose import gvd
from pathtool import file_exists_nonzero
from with_chdir import chdir

logging.basicConfig(level=logging.INFO)
sh.mv = None  # use sh.busybox('mv'), coreutils ignores stdin read errors


def read_content_of_kernel_config(path: Path):
    try:
        content = sh.zcat(path)
    except sh.ErrorReturnCode_1 as e:
        icp(dir(e))
        if hasattr(e, "stderr"):
            # icp(e.stderr)
            if f"{path.as_posix()}: not in gzip format" in e.stderr.decode("utf8"):
                content = sh.cat(path)
            else:
                raise e
    return content


def set_kernel_config_option(*, path: Path, define: str, state: bool, module: bool):
    if not state:
        assert not module
    script_path = Path("/usr/src/linux/scripts/config")
    config_command = sh.Command(script_path)
    config_command = config_command.bake("--file", path.as_posix())
    if not state:
        config_command = config_command.bake("--disable")
    else:
        config_command = config_command.bake("--enable")

    config_command = config_command.bake(define)
    _result = config_command()
    icp(_result)

    del config_command
    if module:
        config_command = sh.Command(script_path)
        config_command = config_command.bake("--file", path.as_posix())
        config_command = config_command.bake("--module")
        config_command = config_command.bake(define)
        _result = config_command()
        icp(_result)

    content = read_content_of_kernel_config(path)
    return content


def verify_kernel_config_setting(
    *,
    path: Path,
    content: str,
    define: str,
    required_state: bool,
    warn: bool,
    fix: bool,
    url: None | str = None,
    verbose: bool | int | float = False,
):
    ic(path, len(content), define, required_state, warn, url)

    if fix:
        content = set_kernel_config_option(
            path=path, define=define, state=required_state, module=False
        )

    state_table = {True: "enabled", False: "disabled"}
    assert isinstance(required_state, bool)
    assert not define.endswith(":")

    current_state = None

    msg = ""
    if url:
        msg += f" See: {url}"

    if (define + "" not in content) and (define + "=" not in content):
        current_state = False
    elif define + " is not set" not in content:
        # the define could be enabled
        if define + "=y" in content:
            # found_define = True
            current_state = True
        if define + "=m" in content:
            # found_define = True
            current_state = True
    else:
        # the define is disabled
        # found_define = False
        current_state = False

    if current_state == required_state:
        return  # all is well

    ic(current_state)

    # mypy: Invalid index type "None | bool" for "Dict[bool, str]"; expected type "bool"  [index] (E)
    if gvd:
        ic(define, current_state, state_table)

    assert current_state is not None
    msg = f"{define} is {state_table[current_state]}!" + msg
    if warn:
        msg = "WARNING: " + msg
        eprint(path.as_posix(), msg)
        pause("press any key to continue")
        return

    msg = "ERROR: " + msg
    raise ValueError(path.as_posix(), msg)


def check_kernel_config(
    *,
    path: Path,
    fix: bool,
    verbose: bool | int | float = False,
):
    path = path.resolve()
    content = read_content_of_kernel_config(path)

    # to see options like CONFIG_TRIM_UNUSED_KSYMS
    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_EXPERT",
        required_state=True,
        warn=False,
        fix=fix,
        url="",
    )

    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_FB_EFI",
        required_state=True,
        warn=False,
        fix=fix,
        url="",
    )

    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_TRIM_UNUSED_KSYMS",
        required_state=False,
        warn=False,
        fix=fix,
        url="",
    )

    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_INTEL_IOMMU_DEFAULT_ON",
        required_state=False,
        warn=True,
        fix=fix,
        url="http://forums.debian.net/viewtopic.php?t=126397",
    )

    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_IKCONFIG_PROC",
        required_state=True,
        warn=False,
        fix=fix,
        url=None,
    )

    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_IKCONFIG",
        required_state=True,
        warn=False,
        fix=fix,
        url=None,
    )

    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_NFS_FS",
        required_state=True,
        warn=False,
        fix=fix,
        url=None,
    )

    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_NFSD_V4",
        required_state=True,
        warn=False,
        fix=fix,
        url=None,
    )

    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_SUNRPC_DEBUG",
        required_state=True,
        warn=False,
        fix=fix,
        url=None,
    )

    # verify_kernel_config_setting(
    #    path=path,
    #    content=content,
    #    define="CONFIG_DEBUG_INFO",
    #    required_state=True,
    #    warn=False, fix=fix,
    #    url=None,
    # )

    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_COMPILE_TEST",
        required_state=False,
        warn=False,
        fix=fix,
        url=None,
    )

    # verify_kernel_config_setting(
    #    path=path,
    #    content=content,
    #    define="CONFIG_FRAME_POINTER",
    #    required_state=True,
    #    warn=False, fix=fix,
    #    url=None,
    # )

    ## not sure what this was for
    # verify_kernel_config_setting(
    #    path=path,
    #    content=content,
    #    define="CONFIG_CRYPTO_USER",
    #    required_state=True,
    #    warn=False, fix=fix,
    #    url=None,
    # )

    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_DRM",
        required_state=True,
        warn=False,
        fix=fix,
        url="https://wiki.gentoo.org/wiki/Nouveau",
    )

    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_DRM_FBDEV_EMULATION",
        required_state=True,
        warn=False,
        fix=fix,
        url="https://wiki.gentoo.org/wiki/Nouveau",
    )

    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_DRM_NOUVEAU",
        required_state=True,  # =m
        warn=False,
        fix=fix,
        url="https://wiki.gentoo.org/wiki/Nouveau",
    )

    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_DRM_RADEON",
        required_state=True,  # =m
        warn=False,
        fix=fix,
        url="https://wiki.gentoo.org/wiki/Nouveau",
    )

    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_BINFMT_MISC",
        required_state=True,  # =m
        warn=False,
        fix=fix,
        url="https://pypi.org/project/fchroot",
    )

    verify_kernel_config_setting(
        path=path,
        content=content,
        define="HID_WACOM",
        required_state=True,
        warn=False,
        fix=fix,
        url="https://github.com/gentoo/gentoo/blob/master/x11-drivers/xf86-input-wacom/xf86-input-wacom-0.40.0.ebuild",
    )

    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_TASK_DELAY_ACCT",
        required_state=True,  # =m
        warn=False,
        fix=fix,
        url="http://guichaz.free.fr/iotop/",
    )

    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_NET_CORE",
        required_state=True,  # =y
        warn=False,
        fix=fix,
        url="",
    )

    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_TUN",
        required_state=True,  # =m
        warn=False,
        fix=fix,
        url="https://www.kernel.org/doc/html/latest/networking/tuntap.html",
    )

    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_VIRTIO_NET",
        required_state=True,  # =m
        warn=False,
        fix=fix,
        url="",
    )

    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_APPLE_PROPERTIES",
        required_state=True,  # =m
        warn=False,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_KEYBOARD_APPLESPI",
        required_state=True,  # =m
        warn=False,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_MOUSE_APPLETOUCH",
        required_state=True,  # =m
        warn=False,
        fix=fix,
        url="https://www.kernel.org/doc/html/v6.1-rc4/input/devices/appletouch.html",
    )
    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_BACKLIGHT_APPLE",
        required_state=True,  # =m
        warn=False,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_HID_APPLE",
        required_state=True,  # =m
        warn=False,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_HID_APPLEIR",
        required_state=True,  # =m
        warn=False,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_USB_APPLEDISPLAY",
        required_state=True,  # =m
        warn=False,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_APPLE_MFI_FASTCHARGE",
        required_state=True,  # =m
        warn=False,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_APPLE_GMUX",
        required_state=True,  # =m
        warn=False,
        fix=fix,
        url="",
    )
    # for GPM
    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_INPUT_MOUSEDEV",
        required_state=True,
        warn=False,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_ZRAM",
        required_state=True,
        warn=False,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_BLK_DEV_FD",
        required_state=True,
        warn=False,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        content=content,
        define="CONFIG_EARLY_PRINTK",
        required_state=True,
        warn=False,
        fix=fix,
        url="",
    )


def _symlink_config(
    *,
    verbose: bool | int | float = False,
):
    dot_config = Path("/usr/src/linux/.config")
    if dot_config.exists():
        if not dot_config.is_symlink():
            timestamp = str(time.time())
            sh.busybox.mv(
                dot_config,
                f"{dot_config}.{timestamp}",
            )

    if not dot_config.exists():
        with resources.path("compile_kernel", ".config") as _kernel_config:
            icp(_kernel_config)

            sh.ln("-s", _kernel_config, dot_config)


def check_config_enviroment(
    *,
    verbose: bool | int | float = False,
):
    # https://www.mail-archive.com/lede-dev@lists.infradead.org/msg07290.html
    if not (os.getenv("KCONFIG_OVERWRITECONFIG") == "1"):
        icp("KCONFIG_OVERWRITECONFIG=1 needs to be set to 1")
        icp("add it to /etc/env.d/99kconfig-symlink. Exiting.")
        sys.exit(1)


def get_kernel_version_from_symlink():
    linux = Path("/usr/src/linux")
    assert linux.is_symlink()
    path = linux.resolve()
    version = path.parts[-1]
    version = version.split("linux-")[-1]
    return version


def boot_is_correct(
    *,
    linux_version: str,
    verbose: bool | int | float = False,
):
    assets = ["System.map", "initramfs", "vmlinux"]
    for asset in assets:
        path = Path(asset) / Path("-") / Path(linux_version)
        if not file_exists_nonzero(path):
            return False
    return True


def gcc_check(
    *,
    verbose: bool | int | float = False,
):
    test_path = Path("/usr/src/linux/init/.init_task.o.cmd")
    if test_path.exists():
        icp(
            "found previously compiled kernel tree, checking is the current gcc version was used"
        )
        gcc_version = sh.gcc_config("-l")
        icp(gcc_version)
        gcc_version = gcc_version.splitlines()
        line = None
        for line in gcc_version:
            if not line.endswith("*"):
                continue
        assert line
        gcc_version = line.split("-")[-1]
        gcc_version = gcc_version.split(" ")[0]
        icp("checking for gcc version:", gcc_version)

        try:
            grep_target = ("gcc/x86_64-pc-linux-gnu/" + gcc_version,)
            icp(grep_target)
            sh.grep(
                grep_target,
                "/usr/src/linux/init/.init_task.o.cmd",
            )
            icp(
                gcc_version,
                "was used to compile kernel previously, not running `make clean`",
            )
        except sh.ErrorReturnCode_1 as e:
            icp(e)
            icp("old gcc version detected, make clean required. Sleeping 5.")
            os.chdir("/usr/src/linux")
            time.sleep(5)
            sh.make("clean")


def kernel_is_already_compiled(
    verbose: bool | int | float = False,
):
    kernel_version = get_kernel_version_from_symlink()
    icp(kernel_version)
    test_path = Path("/usr/src/linux/init/.init_task.o.cmd")

    if Path(
        "/boot/initramfs"
    ).exists():  # should be looking for the current kernel version
        if Path("/boot/initramfs").stat().st_size > 0:
            # if Path("/usr/src/linux/include/linux/kconfig.h").exists():
            if test_path.exists():
                eprint(
                    f"/boot/initramfs and {test_path.as_posix()} exist, skipping compile"
                )
                return True
        icp("/boot/initramfs exists, checking if /usr/src/linux is configured")
        if test_path.exists():
            icp(test_path, "exists, skipping kernel compile")
            return True


def kcompile(
    *,
    configure: bool,
    configure_only: bool,
    force: bool,
    fix: bool,
    no_check_boot: bool,
    symlink_config: bool,
    verbose: bool | int | float = False,
):
    icp()
    if configure_only:
        configure = True
    if not root_user():
        raise ValueError("you must be root")

    unconfigured_kernel = None

    if no_check_boot:
        icp("skipped checking if /boot was mounted")
    else:
        if not Path("/boot/grub/grub.cfg").exists():
            icp("/boot/grub/grub.cfg not found. Exiting.")
            raise ValueError("/boot/grub/grub.cfg not found")

        if not Path("/boot/kernel").exists():
            icp("mount /boot first. Exiting.")
            raise ValueError("mount /boot first")

    if symlink_config:
        check_config_enviroment()
        _symlink_config()
        assert Path("/usr/src/linux/.config").is_symlink()

    if not configure_only:
        sh.emerge("genkernel", "-u", _out=sys.stdout, _err=sys.stderr)

    if not configure_only:
        # handle a downgrade from -9999 before genkernel calls @module-rebuild
        icp("attempting to upgrade zfs and zfs-kmod")
        try:
            sh.emerge(
                "sys-fs/zfs",
                "sys-fs/zfs-kmod",
                "-u",
                # _out=sys.stdout,
                # _err=sys.stderr,
                _tee=True,
                _tty_out=False,
            )
        except sh.ErrorReturnCode_1 as e:
            icp(e)
            icp(dir(e))
            unconfigured_kernel = False
            # ic(dir(e))  # this lists e.stdout
            # ic(e.stdout)
            # ic(e.stderr)
            # assert False
            if hasattr(e, "stdout"):
                icp(type(e.stdout))
                # ic('e.stdout', e.stdout)
                # ic('e.stderr', e.stdout)
                # ic(type(e.stdout))  # <class 'bytes'>  #hmph. the next line should cause a TypeError (before making the str bytes) ... but didnt
                if b"Could not find a usable .config" in e.stdout:
                    unconfigured_kernel = True
                if b"tree at that location has not been built." in e.stdout:
                    unconfigured_kernel = True
                if b"Kernel sources need compiling first" in e.stdout:
                    unconfigured_kernel = True
                if (
                    b"Could not find a Makefile in the kernel source directory"
                    in e.stdout
                ):
                    unconfigured_kernel = True
                if b"These sources have not yet been prepared" in e.stdout:
                    unconfigured_kernel = True

            # assert e.stdout
            # if hasattr(e, 'stdout'):
            #    ic(e.stdout)
            if not unconfigured_kernel:
                # ic(unconfigured_kernel)
                icp("unconfigured_kernel:", unconfigured_kernel)
                raise e
            icp(
                "NOTE: kernel is unconfigured, skipping `emerge sys-fs/zfs sys-fs/zfs-kmod` before kernel compile"
            )

        if not unconfigured_kernel:
            icp("attempting emerge @module-rebuild")
            try:
                sh.emerge("@module-rebuild", _out=sys.stdout, _err=sys.stderr)
            except sh.ErrorReturnCode_1 as e:
                unconfigured_kernel = True  # todo, get conditions from above
                if not unconfigured_kernel:
                    raise e
                icp(
                    "NOTE: kernel is unconfigured, skipping `emerge @module-rebuild` before kernel compile"
                )

    # might fail if gcc was upgraded and the kernel hasnt been recompiled yet
    # for line in sh.emerge('sci-libs/linux-gpib', '-u', _err_to_out=True, _iter=True, _out_bufsize=100):
    #   eprint(line, end='')

    if configure:
        with chdir(
            "/usr/src/linux",
            verbose=verbose,
        ):
            os.system("make nconfig")
        check_kernel_config(
            path=Path("/usr/src/linux/.config"),
            fix=fix,
        )  # must be done after nconfig

    if configure_only:
        return

    gcc_check(
        verbose=verbose,
    )

    os.chdir("/usr/src/linux")

    linux_version = get_kernel_version_from_symlink()
    icp(
        boot_is_correct(
            linux_version=linux_version,
            verbose=verbose,
        )
    )

    if not configure_only:
        if not force:
            if kernel_is_already_compiled(
                verbose=verbose,
            ):
                icp("kernel is already compiled, skipping")
                return

    check_kernel_config(
        path=Path("/usr/src/linux/.config"),
        fix=fix,
    )  # must be done after nconfig
    genkernel_command = sh.Command("genkernel")
    # genkernel_command = ["genkernel"]
    genkernel_command = genkernel_command.bake("all")
    # if configure:
    #    genkernel_command.append('--nconfig')
    genkernel_command = genkernel_command.bake("--no-clean")
    genkernel_command = genkernel_command.bake("--symlink")
    genkernel_command = genkernel_command.bake("--luks")
    genkernel_command = genkernel_command.bake("--module-rebuild")
    genkernel_command = genkernel_command.bake("--all-ramdisk-modules")
    genkernel_command = genkernel_command.bake("--firmware")
    genkernel_command = genkernel_command.bake("--microcode=all")
    genkernel_command = genkernel_command.bake("--microcode-initramfs")
    genkernel_command = genkernel_command.bake('--makeopts="-j12"')
    genkernel_command = genkernel_command.bake(
        "--callback=/usr/bin/emerge zfs zfs-kmod @module-rebuild"
    )
    # --callback="/usr/bin/emerge zfs zfs-kmod sci-libs/linux-gpib sci-libs/linux-gpib-modules @module-rebuild"
    # --zfs
    icp(genkernel_command)
    genkernel_command(_fg=True)

    sh.rc_update("add", "zfs-import", "boot")
    sh.rc_update("add", "zfs-share", "default")
    sh.rc_update("add", "zfs-zed", "default")

    if Path("/boot/grub").is_dir():
        sh.grub_mkconfig("-o", "/boot/grub/grub.cfg")

    sh.emerge("sys-kernel/linux-firmware", _out=sys.stdout, _err=sys.stderr)

    if Path("/boot/grub").is_dir():
        os.makedirs("/boot_backup", exist_ok=True)
        with chdir(
            "/boot_backup",
            verbose=verbose,
        ):
            if not Path("/boot_backup/.git").is_dir():
                sh.git.init()

            sh.git.config("user.email", "user@example.com")
            sh.git.config("user.name", "user")

            timestamp = str(time.time())
            os.makedirs(timestamp)
            sh.cp("-ar", "/boot", timestamp + "/")
            sh.git.add(timestamp, "--force")
            sh.git.commit("-m", timestamp)

    icp("kernel compile and install completed OK")


@click.group(no_args_is_help=True, cls=AHGroup)
@click_add_options(click_global_options)
@click.pass_context
def cli(
    ctx,
    verbose_inf: bool,
    dict_output: bool,
    verbose: bool | int | float = False,
) -> None:
    tty, verbose = tv(
        ctx=ctx,
        verbose=verbose,
        verbose_inf=verbose_inf,
    )
    if not verbose:
        ic.disable()
    else:
        ic.enable()

    if verbose_inf:
        gvd.enable()


@cli.command()
@click.option("--configure", "--config", is_flag=True)
@click.option("--configure-only", "--config-only", is_flag=True)
@click.option("--force", is_flag=True)
@click.option("--fix", is_flag=True)
@click.option("--symlink-config", is_flag=True)
@click.option("--no-check-boot", is_flag=True)
@click_add_options(click_global_options)
@click.pass_context
def compile(
    ctx,
    configure: bool,
    configure_only: bool,
    fix: bool,
    symlink_config: bool,
    verbose_inf: bool,
    dict_output: bool,
    force: bool,
    no_check_boot: bool,
    verbose: bool | int | float = False,
):
    tty, verbose = tv(
        ctx=ctx,
        verbose=verbose,
        verbose_inf=verbose_inf,
    )
    if not verbose:
        ic.disable()
    if verbose_inf:
        gvd.enable()

    kcompile(
        configure=configure,
        configure_only=configure_only,
        force=force,
        fix=fix,
        no_check_boot=no_check_boot,
        symlink_config=symlink_config,
        verbose=verbose,
    )
    eprint("DONT FORGET TO UMOUNT /boot")


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
    verbose: bool | int | float = False,
):
    tty, verbose = tv(
        ctx=ctx,
        verbose=verbose,
        verbose_inf=verbose_inf,
    )
    if not verbose:
        ic.disable()
    if verbose_inf:
        gvd.enable()

    for config in dotconfigs:
        check_kernel_config(
            path=config,
            fix=fix,
        )  # must be done after nconfig
        return
