#!/usr/bin/env python3
# -*- coding: utf8 -*-


from __future__ import annotations

import gzip
import logging
import os
import sys
import time
from importlib import resources
from pathlib import Path

import hs
from asserttool import ic
from asserttool import icp
from asserttool import root_user
from eprint import eprint
from getdents import files_pathlib
from globalverbose import gvd
from pathtool import file_exists_nonzero
from with_chdir import chdir

# from rich import print as pprint
logging.basicConfig(level=logging.INFO)

USED_SYMBOL_SET = set()


def generate_module_config_dict(path: Path):
    _manual_mappings = {}

    # _manual_mappings["USB_XHCI_PCI"] = ["xhci_pci.o"]
    # _manual_mappings["I2C_I801"] = ["i2c_i801.o"]

    _makefiles = files_pathlib(path, names=["Makefile"])
    icp(_makefiles)
    config_dict = {}
    prefix = "obj-$(CONFIG_"
    _pprefix = [
        "mpic-msi-",
        "mpic-msgr-",
        "fsl-msi-",
        "mmcif-",
        "hyp-",
        "efi-",
        "compat-",
        "arm-",
        "riscv-",
        "zboot-",
        "pxa2xx-",
        "my-",
        "sfp-",
    ]
    _prefixes = [prefix]
    for _p in _pprefix:
        _prefixes.append(_p)

    for _makefile in _makefiles:
        with open(_makefile, "r", encoding="utf8") as f:
            for line in f:
                line = line.strip()  # some lines have leading whitespace
                if line.startswith("#"):
                    continue
                if "+=" not in line:  # bug, need to properly parse the Makefiles
                    continue
                if prefix in line:
                    # eprint(line)
                    assert line.startswith(tuple(_prefixes))
                    _config_name = line.split(prefix)[-1]
                    # icp(_config_name)
                    _config_name = _config_name.split(")")[0]
                    # icp(_config_name)
                    _modules = line.split("+=")[-1].strip()
                    _modules = _modules.split()
                    _omodules = []
                    for _m in _modules:
                        if _m.endswith(".o"):
                            _omodules.append(_m)
                            if "-" in _m:
                                _omodules.append(_m.replace("-", "_"))

                    # icp(_modules)
                    if _omodules:
                        config_dict[_config_name] = _omodules

    # pprint(config_dict)
    return config_dict | _manual_mappings


def read_content_of_kernel_config(path: Path):
    try:
        with gzip.open(path, mode="rt", encoding="utf8") as _fh:
            content = _fh.read()
    except gzip.BadGzipFile:
        with open(path, mode="rt", encoding="utf8") as _fh:
            content = _fh.read()
    return content


def get_set_kernel_config_option(
    *,
    path: Path,
    define: str,
    state: bool,
    module: bool,
    get: bool,
) -> None | str:
    icp(
        path,
        define,
        state,
        module,
        get,
    )
    if not get:
        assert define not in USED_SYMBOL_SET
        USED_SYMBOL_SET.add(define)
    if not state:
        assert not module
    script_path = Path("/usr/src/linux/scripts/config")
    config_command = hs.Command(script_path)
    config_command.bake("--file", path.as_posix())

    if get:
        config_command.bake("--state")
        config_command.bake(define)
        _result = config_command().strip()
        return _result

    if not state:
        config_command.bake("--disable")
    else:
        config_command.bake("--enable")

    config_command.bake(define)
    _result = config_command()
    icp(_result)

    del config_command
    if module:
        config_command = hs.Command(script_path)
        config_command.bake("--file", path.as_posix())
        config_command.bake("--module")
        config_command.bake(define)
        _result = config_command()
        icp(_result)

    # content = read_content_of_kernel_config(path)
    # return content
    return None


def verify_kernel_config_setting(
    *,
    path: Path,
    define: str,
    required_state: bool,
    module: bool,
    warn: bool,
    fix: bool,
    url: None | str = None,
):

    _current_state = get_set_kernel_config_option(
        path=path,
        define=define,
        state=required_state,
        module=module,
        get=True,
    )
    ic(
        path,
        define,
        required_state,
        module,
        _current_state,
        warn,
        fix,
        url,
    )

    if _current_state == "y" and required_state and not module:
        return
    if (_current_state == "m") and (required_state and module):
        ic(_current_state, required_state, module)
        return
    if _current_state == "n" and not required_state and not module:
        return

    if fix:
        get_set_kernel_config_option(
            path=path,
            define=define,
            state=required_state,
            module=module,
            get=False,
        )
        _current_state = get_set_kernel_config_option(
            path=path,
            define=define,
            state=required_state,
            module=module,
            get=True,
        )

    state_table = {True: "enabled", False: "disabled"}
    module_table = {True: "module", False: "non-module"}
    assert isinstance(required_state, bool)
    assert not define.endswith(":")

    enabled_state = False
    if _current_state in {"y", "m"}:
        enabled_state = True

    module_state = False
    if _current_state == "m":
        module_state = True

    msg = ""
    if url:
        msg += f" See: {url}"

    if _current_state == "y":
        if required_state and not module:
            return  # all is well
    if _current_state == "m":
        if required_state and module:
            return  # all is well
    if _current_state == "n":
        if not required_state and not module:
            return  # all is well

    # mypy: Invalid index type "None | bool" for "Dict[bool, str]"; expected type "bool"  [index] (E)
    if gvd:
        ic(
            define,
            _current_state,
            enabled_state,
            module_state,
        )

    msg = (
        f"{define} is {state_table[enabled_state]} and {module_table[module_state]}!"
        + msg
    )
    if warn:
        msg = "WARNING: " + msg
        eprint(path.as_posix(), msg)
        # pause("press any key to continue")
        return

    msg = "ERROR: " + msg
    raise ValueError(path.as_posix(), msg)


def check_kernel_config_nfs(
    *,
    path: Path,
    fix: bool,
    warn_only: bool,
):
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_NFS_FS",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url=None,
    )

    verify_kernel_config_setting(
        path=path,
        define="CONFIG_NFSD",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url=None,
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_NFSD_V4",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url=None,
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_NFS_V4",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url=None,
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_NFS_V4_1",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url=None,
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_NFS_V4_2",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url=None,
    )


def check_kernel_config(
    *,
    path: Path,
    fix: bool,
    warn_only: bool,
):
    icp(path, fix, warn_only)
    global USED_SYMBOL_SET
    USED_SYMBOL_SET = set()

    path = path.resolve()
    assert insure_config_exists()
    icp(path, warn_only)

    check_kernel_config_nfs(
        path=path,
        fix=fix,
        warn_only=warn_only,
    )

    # BPF, required for CONFIG_FUNCTION_TRACER
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_FTRACE",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # BPF
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_FUNCTION_TRACER",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )

    verify_kernel_config_setting(
        path=path,
        define="CONFIG_HAVE_FENTRY",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # to see options like CONFIG_TRIM_UNUSED_KSYMS
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_EXPERT",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # warnings as errors
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_WERROR",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # fs
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_EXT2_FS",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # fs
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_EXT3_FS",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # fs
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_EXFAT_FS",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # fs
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_NTFS_FS",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # sec
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_FORTIFY_SOURCE",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # sec
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_HARDENED_USERCOPY",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )

    # legacy old
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_UID16",
        required_state=False,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # not a paravirt kernel
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_PARAVIRT",
        required_state=False,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # kvm
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_KVM",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # kvm
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_KVM_AMD",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # kvm
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_VIRTIO_BALLOON",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # pcie
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_HOTPLUG_PCI_PCIE",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # intel low power support
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_X86_INTEL_LPSS",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # boot VESA
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_FB",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # boot VESA
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_FRAMEBUFFER_CONSOLE",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # boot VESA
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_FB_MODE_HELPERS",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # boot VESA
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_FB_RADEON",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # boot VESA
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_FB_NVIDIA",
        required_state=False,  # boot seems to hang here
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    ## boot VESA
    # seems to have been removed, oldconfig removes it
    # verify_kernel_config_setting(
    #    path=path,
    #    define="CONFIG_FB_INTEL",
    #    required_state=True,
    #    module=True,
    #    warn=warn_only,
    #    fix=fix,
    #    url="",
    # )
    # boot VESA
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_SYSFB_SIMPLEFB",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # boot VESA
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_BOOT_VESA_SUPPORT",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # boot VESA
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_DRM_LOAD_EDID_FIRMWARE",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # power managment debug
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_PM_DEBUG",
        required_state=False,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )

    # required for CONFIG_MEDIA_USB_SUPPORT below
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_MEDIA_SUPPORT",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # unknown if necessary
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_MEDIA_USB_SUPPORT",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )

    verify_kernel_config_setting(
        path=path,
        define="CONFIG_FB_EFI",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )

    verify_kernel_config_setting(
        path=path,
        define="CONFIG_TRIM_UNUSED_KSYMS",
        required_state=False,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )

    verify_kernel_config_setting(
        path=path,
        define="CONFIG_INTEL_IOMMU_DEFAULT_ON",
        required_state=False,
        module=False,
        warn=True,
        fix=fix,
        url="http://forums.debian.net/viewtopic.php?t=126397",
    )

    verify_kernel_config_setting(
        path=path,
        define="CONFIG_IKCONFIG_PROC",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url=None,
    )

    verify_kernel_config_setting(
        path=path,
        define="CONFIG_IKCONFIG",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url=None,
    )

    verify_kernel_config_setting(
        path=path,
        define="CONFIG_SUNRPC_DEBUG",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url=None,
    )

    # required by sys-fs/zfs-kmod-9999
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_DEBUG_INFO_DWARF5",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url=None,
    )

    # verify_kernel_config_setting(
    #    path=path,
    #    define="CONFIG_COMPILE_TEST",
    #    required_state=False,
    #    module=False,
    #    warn=warn_only,
    #    fix=fix,
    #    url=None,
    # )
    # required by sys-fs/zfs-kmod-9999
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_UNWINDER_ORC",
        required_state=False,  # so CONFIG_FRAME_POINTER can be set
        module=False,
        warn=warn_only,
        fix=fix,
        url=None,
    )
    # required by sys-fs/zfs-kmod-9999
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_UNWINDER_FRAME_POINTER",
        required_state=True,  # so CONFIG_FRAME_POINTER can be set
        module=False,
        warn=warn_only,
        fix=fix,
        url=None,
    )

    # required by sys-fs/zfs-kmod-9999
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_FRAME_POINTER",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url=None,
    )

    ## not sure what this was for
    # verify_kernel_config_setting(
    #    path=path,
    #    define="CONFIG_CRYPTO_USER",
    #    required_state=True,
    #    module=True,
    #    warn=warn_only, fix=fix,
    #    url=None,
    # )

    verify_kernel_config_setting(
        path=path,
        define="CONFIG_DRM",
        required_state=True,
        module=False,  # technically, it can be a module, but that breaks stuff
        warn=warn_only,
        fix=fix,
        url="https://wiki.gentoo.org/wiki/Nouveau",
    )

    verify_kernel_config_setting(
        path=path,
        define="CONFIG_DRM_FBDEV_EMULATION",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="https://wiki.gentoo.org/wiki/Nouveau",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_DRM_AMDGPU",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_DRM_UDL",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_FIRMWARE_EDID",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_FB_VESA",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_MTRR_SANITIZER",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # speculative execution
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_MITIGATION_SLS",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # ACPI
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_ACPI_FPDT",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # ACPI
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_ACPI_TAD",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # ACPI
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_ACPI_PCI_SLOT",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # ACPI
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_ACPI_SBS",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # ACPI
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_ACPI_HED",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # ACPI
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_ACPI_APEI",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # ACPI
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_ACPI_DPTF",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # ACPI
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_ACPI_CONFIGFS",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # ACPI
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_ACPI_APEI_GHES",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # ACPI
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_ACPI_APEI_PCIEAER",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # ACPI
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_ACPI_NFIT",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # ACPI
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_ACPI_PROCESSOR_AGGREGATOR",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # ACPI
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_HIBERNATION",
        required_state=False,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # cpu frequency
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_CPU_FREQ_STAT",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # module versioning
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_MODVERSIONS",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # block layer SG
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_BLK_DEV_BSGLIB",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # ECC
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_MEMORY_FAILURE",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # ECC
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_MTD_NAND_ECC_SW_BCH",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # ECC
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_RAS_CEC",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # mem
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_PAGE_REPORTING",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # mem
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_TRANSPARENT_HUGEPAGE",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # mem
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_PER_VMA_LOCK",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # chipset
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_LPC_ICH",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # chipset
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_LPC_SCH",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # pcie
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_PCIEAER",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # pcie
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_PCIE_DPC",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # pcie
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_PCI_IOV",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # old interface
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_UEVENT_HELPER",
        required_state=False,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # dmi
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_DMI_SYSFS",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # mtd
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_MTD",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # i386
    # I forget why... maybe virtualbox?
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_IA32_EMULATION",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # usb speakers
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_SND_USB_AUDIO",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # alsa required for the rest
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_SND",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # alsa required for the rest
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_SND_SOC",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # alsa
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_SND_SOC_AMD_ACP",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # alsa
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_SND_OSSEMUL",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # alsa
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_SND_MIXER_OSS",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # alsa
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_SND_PCM_OSS",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # alsa
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_SND_INTEL8X0",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # alsa
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_SND_INTEL8X0M",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # alsa
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_SND_HDA_GENERIC",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # alsa audio
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_SND_AC97_CODEC",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # alsa
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_USB_GADGET",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    ## alsa
    # verify_kernel_config_setting(
    #    path=path,
    #    define="CONFIG_SND_USB_AUDIO_USE_MEDIA_CONTROLLER",
    #    required_state=True,
    #    module=True,
    #    warn=warn_only,
    #    fix=fix,
    #    url="",
    # )
    # alsa
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_SND_SUPPORT_OLD_API",
        required_state=False,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # alsa
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_SOUNDWIRE",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # usb otg
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_USB_OTG",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )

    verify_kernel_config_setting(
        path=path,
        define="CONFIG_DRM_NOUVEAU",
        required_state=True,  # =m
        module=True,
        warn=warn_only,
        fix=fix,
        url="https://wiki.gentoo.org/wiki/Nouveau",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_VT_HW_CONSOLE_BINDING",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_VGA_SWITCHEROO",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )

    verify_kernel_config_setting(
        path=path,
        define="CONFIG_DRM_RADEON",
        required_state=True,  # =m
        module=True,
        warn=warn_only,
        fix=fix,
        url="https://wiki.gentoo.org/wiki/Nouveau",
    )

    verify_kernel_config_setting(
        path=path,
        define="CONFIG_BINFMT_MISC",
        required_state=True,  # =m
        module=True,
        warn=warn_only,
        fix=fix,
        url="https://pypi.org/project/fchroot",
    )

    verify_kernel_config_setting(
        path=path,
        define="HID_WACOM",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="https://github.com/gentoo/gentoo/blob/master/x11-drivers/xf86-input-wacom/xf86-input-wacom-0.40.0.ebuild",
    )

    ## performance
    ## required to enable CONFIG_TASK_DELAY_ACCT below, but disabled for now
    # verify_kernel_config_setting(
    #    path=path,
    #    define="CONFIG_TASKSTATS",
    #    required_state=False,
    #    module=False,
    #    warn=warn_only,
    #    fix=fix,
    #    url="",
    # )
    # verify_kernel_config_setting(
    #    path=path,
    #    define="CONFIG_TASK_DELAY_ACCT",
    #    required_state=False,
    #    module=False,
    #    warn=warn_only,
    #    fix=fix,
    #    url="http://guichaz.free.fr/iotop/",
    # )

    verify_kernel_config_setting(
        path=path,
        define="CONFIG_NET_CORE",
        required_state=True,  # =y
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )

    verify_kernel_config_setting(
        path=path,
        define="CONFIG_TUN",
        required_state=True,  # =m
        module=True,
        warn=warn_only,
        fix=fix,
        url="https://www.kernel.org/doc/html/latest/networking/tuntap.html",
    )

    verify_kernel_config_setting(
        path=path,
        define="CONFIG_VIRTIO_NET",
        required_state=True,  # =m
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )

    verify_kernel_config_setting(
        path=path,
        define="CONFIG_APPLE_PROPERTIES",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_SPI",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_KEYBOARD_APPLESPI",
        required_state=True,  # =m
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_MOUSE_APPLETOUCH",
        required_state=True,  # =m
        module=True,
        warn=warn_only,
        fix=fix,
        url="https://www.kernel.org/doc/html/v6.1-rc4/input/devices/appletouch.html",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_BACKLIGHT_APPLE",
        required_state=True,  # =m
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_HID_APPLE",
        required_state=True,  # =m
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_HID_APPLEIR",
        required_state=True,  # =m
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_USB_APPLEDISPLAY",
        required_state=True,  # =m
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_APPLE_MFI_FASTCHARGE",
        required_state=True,  # =m
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_APPLE_GMUX",
        required_state=True,  # =m
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # for GPM
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_INPUT_MOUSEDEV",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_ZRAM",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_ZRAM_MEMORY_TRACKING",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_BLK_DEV_FD",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_EARLY_PRINTK",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # sshuttle
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_NF_NAT",
        required_state=True,  # =m
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # sshuttle
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_NETFILTER_ADVANCED",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # sshuttle
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_IP_NF_MATCH_TTL",
        required_state=True,  # =m
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # sshuttle
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_IP_NF_TARGET_REDIRECT",
        required_state=True,  # =m
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # sshuttle
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_NETFILTER_XT_TARGET_HL",
        required_state=True,  # =m
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # old outdated option
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_NO_HZ",
        required_state=False,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )

    # speed
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_PREEMPT_NONE",
        required_state=False,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # speed
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_PREEMPT_VOLUNTARY",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # speed
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_PREEMPT",
        required_state=False,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # new process accounting
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_BSD_PROCESS_ACCT_V3",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # memory cgrroup
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_MEMCG",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # cgroup debugging
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_CGROUP_DEBUG",
        required_state=False,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # cgroup
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_CGROUP_FAVOR_DYNMODS",
        required_state=True,  # was false
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    #
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_CHECKPOINT_RESTORE",
        required_state=False,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )

    # required for CONFIG_X86_SGX below
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_X86_X2APIC",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    #
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_X86_SGX",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )

    # auto cgroups... might contradict PREEMPT_NONE
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_SCHED_AUTOGROUP",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )

    # zswap
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_ZSWAP",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    ## zswap
    ## depreciated
    # verify_kernel_config_setting(
    #    path=path,
    #    define="CONFIG_Z3FOLD",
    #    required_state=True,
    #    module=True,
    #    warn=warn_only,
    #    fix=fix,
    #    url="",
    # )
    # memory deduplication
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_KSM",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # nvme
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_BLK_DEV_NVME",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # nvme
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_NVME_VERBOSE_ERRORS",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # nvme
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_NVME_HWMON",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # nvme
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_NVME_MULTIPATH",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # nvme
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_NVME_TARGET",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    #
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_X86_CPU_RESCTRL",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    #
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_BCACHE",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    #
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_THERMAL_STATISTICS",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # audio
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_SND_SEQUENCER_OSS",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # audio
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_SND_HDA_CODEC_HDMI",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # audio pc-speaker
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_INPUT_PCSPKR",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # pcie pc-card reader
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_MISC_RTSX_PCI",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_BPF_SYSCALL",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_NET_CLS_BPF",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_NET_ACT_BPF",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_BPF_EVENTS",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # kvm
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_KVM_INTEL",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # kvm
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_VHOST_NET",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )

    # mmc
    # required for CONFIG_MMC_BLOCK below
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_MMC",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # mmc
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_MMC_BLOCK",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )

    # FUSE
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_FUSE_FS",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # vlan
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_VLAN_8021Q",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # NUMA
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_NUMA",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # udev
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_DEVTMPFS",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="https://wiki.gentoo.org/wiki/Udev",
    )
    # wireguard
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_WIREGUARD",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="https://wiki.gentoo.org/wiki/WireGuard",
    )
    ## serial console debugging
    # verify_kernel_config_setting(
    #    path=path,
    #    define="CONFIG_USB_SERIAL_CONSOLE",
    #    required_state=True,
    #    module=False,
    #    warn=warn_only,
    #    fix=fix,
    #    url="",
    # )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_USB_SERIAL",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_USB_SERIAL_PL2303",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_USB_SERIAL_CH341",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_USB_SERIAL_FTDI_SIO",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_USB_PEGASUS",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_USB_USBNET",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_USB_SERIAL_CYPRESS_M8",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_USB_ACM",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # verify_kernel_config_setting(
    #    path=path,
    #    define="CONFIG_NET_DROP_MONITOR",
    #    required_state=True,
    #    module=True,
    #    warn=warn_only,
    #    fix=fix,
    #    url="",
    # )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_BRIDGE",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_BLK_DEV_NBD",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_USB4",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    ## performance
    ## nope, zfs-kmode REQUIRES this
    # verify_kernel_config_setting(
    #    path=path,
    #    define="CONFIG_DEBUG_INFO_DWARF5",
    #    required_state=False,
    #    module=False,
    #    warn=warn_only,
    #    fix=fix,
    #    url="",
    # )
    # performance
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_DEBUG_STACK_USAGE",
        required_state=False,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # performance
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_DEBUG_WX",
        required_state=False,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # performance
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_DEBUG_KERNEL",
        required_state=False,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # performance
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_DEBUG_MISC",
        required_state=False,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )

    # performance
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_DEBUG_MEMORY_INIT",
        required_state=False,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    ## performance
    ## BPF requires this
    # verify_kernel_config_setting(
    #    path=path,
    #    define="CONFIG_FUNCTION_TRACER",
    #    required_state=False,
    #    module=False,
    #    warn=warn_only,
    #    fix=fix,
    #    url="",
    # )
    ## performance
    # verify_kernel_config_setting(
    #    path=path,
    #    define="CONFIG_FUNCTION_GRAPH_TRACER",
    #    required_state=False,
    #    module=False,
    #    warn=warn_only,
    #    fix=fix,
    #    url="",
    # )
    ## performance
    # verify_kernel_config_setting(
    #    path=path,
    #    define="CONFIG_DYNAMIC_FTRACE",
    #    required_state=False,
    #    module=False,
    #    warn=warn_only,
    #    fix=fix,
    #    url="",
    # )
    # performance
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_RCU_TRACE",
        required_state=False,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # performance
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_SCHEDSTATS",
        required_state=False,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    ## performance
    # verify_kernel_config_setting(
    #    path=path,
    #    define="CONFIG_TASK_XACCT",
    #    required_state=False,
    #    module=False,
    #    warn=warn_only,
    #    fix=fix,
    #    url="",
    # )
    ## performance
    # verify_kernel_config_setting(
    #    path=path,
    #    define="CONFIG_TASK_IO_ACCOUNTING",
    #    required_state=False,
    #    module=False,
    #    warn=warn_only,
    #    fix=fix,
    #    url="",
    # )
    # performance
    # enable THP only for applications that explicitly request it (via madvise), MADV_DONTNEED
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_TRANSPARENT_HUGEPAGE_MADVISE",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # performance
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_SLUB_DEBUG",
        required_state=False,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # performance
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_CPU_FREQ_DEFAULT_GOV_USERSPACE",
        required_state=False,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # performance
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_CPU_FREQ_DEFAULT_GOV_PERFORMANCE",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # performance
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_X86_INTEL_PSTATE",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # performance
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_X86_AMD_PSTATE",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # performance
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_SECURITY_SELINUX",
        required_state=False,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    ## performance
    # verify_kernel_config_setting(
    #    path=path,
    #    define="CONFIG_DEFAULT_SECURITY_SELINUX",
    #    required_state=False,
    #    module=False,
    #    warn=warn_only,
    #    fix=fix,
    #    url="",
    # )
    ## performance
    # verify_kernel_config_setting(
    #    path=path,
    #    define="",
    #    required_state=,
    #    module=,
    #    warn=warn_only,
    #    fix=fix,
    #    url="",
    # )
    ## performance
    # verify_kernel_config_setting(
    #    path=path,
    #    define="",
    #    required_state=,
    #    module=,
    #    warn=warn_only,
    #    fix=fix,
    #    url="",
    # )
    ## performance
    # verify_kernel_config_setting(
    #    path=path,
    #    define="",
    #    required_state=,
    #    module=,
    #    warn=warn_only,
    #    fix=fix,
    #    url="",
    # )
    ## performance
    # verify_kernel_config_setting(
    #    path=path,
    #    define="",
    #    required_state=,
    #    module=,
    #    warn=warn_only,
    #    fix=fix,
    #    url="",
    # )
    ## performance
    # verify_kernel_config_setting(
    #    path=path,
    #    define="",
    #    required_state=,
    #    module=,
    #    warn=warn_only,
    #    fix=fix,
    #    url="",
    # )
    ## performance
    # verify_kernel_config_setting(
    #    path=path,
    #    define="",
    #    required_state=,
    #    module=,
    #    warn=warn_only,
    #    fix=fix,
    #    url="",
    # )
    ## performance
    # verify_kernel_config_setting(
    #    path=path,
    #    define="",
    #    required_state=,
    #    module=,
    #    warn=warn_only,
    #    fix=fix,
    #    url="",
    # )
    ## performance
    # verify_kernel_config_setting(
    #    path=path,
    #    define="",
    #    required_state=,
    #    module=,
    #    warn=warn_only,
    #    fix=fix,
    #    url="",
    # )
    ## genkernel
    # verify_kernel_config_setting(
    #    path=path,
    #    define="CONFIG_MICROCODE_AMD",
    #    required_state=True,
    #    module=True,
    #    warn=warn_only,
    #    fix=fix,
    #    url="",
    # )
    ## genkernel
    # verify_kernel_config_setting(
    #    path=path,
    #    define="CONFIG_MICROCODE_INTEL",
    #    required_state=True,
    #    module=True,
    #    warn=warn_only,
    #    fix=fix,
    #    url="",
    # )

    # zfs LSI
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_SCSI_MPT3SAS",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # security, like pledge
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_SECURITY_LANDLOCK",
        required_state=True,
        module=False,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # 10G Ethernet
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_NET_VENDOR_AQUANTIA",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # 10G Ethernet
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_AQTION",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )
    # zbook g5 sd card reader
    verify_kernel_config_setting(
        path=path,
        define="CONFIG_MMC_REALTEK_PCI",
        required_state=True,
        module=True,
        warn=warn_only,
        fix=fix,
        url="",
    )


# bpf
# CONFIG_BPF_SYSCALL:         is not set when it should be.
# CONFIG_NET_CLS_BPF:         is not set when it should be.
# CONFIG_NET_ACT_BPF:         is not set when it should be.
# CONFIG_BPF_EVENTS:  is not set when it should be.


def _symlink_config():
    dot_config = Path("/usr/src/linux/.config")
    if dot_config.exists():
        if not dot_config.is_symlink():
            timestamp = str(time.time())
            hs.Command("busybox")(
                "mv",
                dot_config,
                f"{dot_config}.{timestamp}",
            )

    if not dot_config.exists():
        with resources.path("compile_kernel", ".config") as _kernel_config:
            icp(_kernel_config)
            hs.Command("ln")("-s", _kernel_config, dot_config)


def extract_kernel_config():
    input_path = "/proc/config.gz"
    output_path = "/usr/src/linux/.config"

    if os.path.exists(output_path):
        raise FileExistsError(f"File {output_path} already exists")
    with gzip.open(input_path, "rb") as f_in:
        config_data = f_in.read()
    with open(output_path, "wb") as f_out:
        f_out.write(config_data)


def insure_config_exists():
    dot_config = Path("/usr/src/linux/.config")
    if not dot_config.exists():
        # if _symlink_config():
        #    return True
        extract_kernel_config()
        assert dot_config.exists()
    return True


def check_config_enviroment():
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
):
    assets = ["System.map", "initramfs", "vmlinux"]
    for asset in assets:
        path = Path(asset) / Path("-") / Path(linux_version)
        if not file_exists_nonzero(path):
            return False
    return True


def gcc_check():
    #'gcc --version | head -n1 | grep -oP "\d+\.\d+(\.\d+)?" | head -n1 | cut -d. -f1'
    _gcc_version_string = hs.Command("gcc", "--version").splitlines()[0]
    icp(_gcc_version_string)
    _current_gcc_major_version = _gcc_version_string.split(" ")[-2][:2]
    icp(_current_gcc_major_version)
    # assert _current_gcc_major_version == "14"
    _config_gcc_version = (
        hs.Command("grep", ["CONFIG_GCC_VERSION", "/usr/src/linux/.config"])
        .strip()
        .split("=")[-1][:2]
    )
    icp(_config_gcc_version)
    if _config_gcc_version == _current_gcc_major_version:
        icp(
            _config_gcc_version,
            "was used to compile kernel previously, not running `make clean`",
        )
        return
    else:
        icp("old gcc version detected, calling 'make clean'")
        os.chdir("/usr/src/linux")
        hs.Command("make", "clean")


def gcc_check_old():
    test_path = Path("/usr/src/linux/init/.init_task.o.cmd")
    if test_path.exists():
        icp(
            "found previously compiled kernel tree, checking is the current gcc version was used"
        )
        gcc_version = hs.Command("gcc-config", "-l")
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
            hs.Command("grep")(grep_target, "/usr/src/linux/init/.init_task.o.cmd")
            icp(
                gcc_version,
                "was used to compile kernel previously, not running `make clean`",
            )
        except hs.ErrorReturnCode_1 as e:
            icp(e)
            icp("old gcc version detected, make clean required. Sleeping 5.")
            os.chdir("/usr/src/linux")
            time.sleep(5)
            hs.Command("make", "clean")


def kernel_is_already_compiled():
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


def install_compiled_kernel():
    with chdir("/usr/src/linux"):
        os.system("make install")

    genkernel_command = hs.Command("genkernel")
    genkernel_command.bake("initramfs")
    genkernel_command.bake("--no-clean")
    genkernel_command.bake("--no-mrproper")
    # genkernel_command.bake("--no-busybox")
    # genkernel_command.bake("--no-keymap")
    # icp(genkernel_command)
    genkernel_command(_fg=True)

    assert Path("/boot/grub").is_dir()
    hs.Command("grub-mkconfig")("-o", "/boot/grub/grub.cfg")


def configure_kernel(
    fix: bool,
    warn_only: bool,
    interactive: bool,
):
    if interactive:
        with chdir(
            "/usr/src/linux",
        ):
            os.system("make nconfig")
    check_kernel_config(
        path=Path("/usr/src/linux/.config"),
        fix=fix,
        warn_only=warn_only,
    )  # must be done after nconfig


def compile_and_install_kernel(
    *,
    configure: bool,
    force: bool,
    fix: bool,
    warn_only: bool,
    no_check_boot: bool,
    symlink_config: bool,
):
    icp()
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

    if configure:
        configure_kernel(
            fix=fix,
            warn_only=warn_only,
            interactive=True,
        )

    hs.Command("emerge")(
        "genkernel",
        "-u",
        _out=sys.stdout,
        _err=sys.stderr,
    )

    # do this before the long @module-rebuild to catch problems now
    configure_kernel(
        fix=fix,
        warn_only=warn_only,
        interactive=False,
    )
    # handle a downgrade from -9999 before genkernel calls @module-rebuild
    icp("attempting to upgrade zfs and zfs-kmod")
    try:
        hs.Command("emerge")(
            "sys-fs/zfs",
            "sys-fs/zfs-kmod",
            "-u",
            # _out=sys.stdout,
            # _err=sys.stderr,
            _tee=True,
            _tty_out=False,
        )
    except hs.ErrorReturnCode_1 as e:
        icp(e)
        icp(dir(e))
        unconfigured_kernel = False
        if hasattr(e, "stdout"):
            icp(type(e.stdout))
            if b"Could not find a usable .config" in e.stdout:
                unconfigured_kernel = True
            if b"tree at that location has not been built." in e.stdout:
                unconfigured_kernel = True
            if b"Kernel sources need compiling first" in e.stdout:
                unconfigured_kernel = True
            if b"Could not find a Makefile in the kernel source directory" in e.stdout:
                unconfigured_kernel = True
            if b"These sources have not yet been prepared" in e.stdout:
                unconfigured_kernel = True

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
            hs.Command("emerge")("@module-rebuild", _out=sys.stdout, _err=sys.stderr)
        except hs.ErrorReturnCode_1 as e:
            unconfigured_kernel = True  # todo, get conditions from above
            if not unconfigured_kernel:
                raise e
            icp(
                "NOTE: kernel is unconfigured, skipping `emerge @module-rebuild` before kernel compile"
            )

    # might fail if gcc was upgraded and the kernel hasnt been recompiled yet
    # for line in hs.Command("emerge")('sci-libs/linux-gpib', '-u', _err_to_out=True, _iter=True, _out_bufsize=100):
    #   eprint(line, end='')

    gcc_check()

    os.chdir("/usr/src/linux")

    linux_version = get_kernel_version_from_symlink()
    icp(
        boot_is_correct(
            linux_version=linux_version,
        )
    )

    # if not force:
    #    if kernel_is_already_compiled():
    #        icp("kernel is already compiled, skipping")
    #        return

    if not Path("/usr/src/linux/.config").exists():
        hs.Command("make")("defconfig")
        check_kernel_config(
            path=Path("/usr/src/linux/.config"),
            fix=True,
            warn_only=warn_only,
        )

    check_kernel_config(
        path=Path("/usr/src/linux/.config"),
        fix=fix,
        warn_only=warn_only,
    )  # must be done after nconfig
    genkernel_command = hs.Command("genkernel")
    genkernel_command.bake("all")
    # if configure:
    #    genkernel_command.bake('--nconfig')
    genkernel_command.bake("--no-clean")
    genkernel_command.bake("--no-mrproper")
    genkernel_command.bake("--symlink")
    # genkernel_command.bake("--luks")
    genkernel_command.bake("--module-rebuild")
    genkernel_command.bake("--all-ramdisk-modules")
    genkernel_command.bake("--firmware")
    genkernel_command.bake("--microcode=all")
    genkernel_command.bake("--microcode-initramfs")
    genkernel_command.bake('--makeopts="-j12"')
    # genkernel_command.bake("--no-busybox")
    # genkernel_command.bake("--no-keymap")
    genkernel_command.bake("--callback=/usr/bin/emerge zfs zfs-kmod @module-rebuild")
    # --callback="/usr/bin/emerge zfs zfs-kmod sci-libs/linux-gpib sci-libs/linux-gpib-modules @module-rebuild"
    # --zfs
    icp(genkernel_command)
    genkernel_command(_fg=True)

    hs.Command("rc-update")("add", "zfs-import", "boot")
    hs.Command("rc-update")("add", "zfs-share", "default")
    hs.Command("rc-update")("add", "zfs-zed", "default")

    if Path("/boot/grub").is_dir():
        hs.Command("grub-mkconfig")("-o", "/boot/grub/grub.cfg")

    hs.Command("emerge")("sys-kernel/linux-firmware", _out=sys.stdout, _err=sys.stderr)

    if Path("/boot/grub").is_dir():
        os.makedirs("/boot_backup", exist_ok=True)
        with chdir(
            "/boot_backup",
        ):
            if not Path("/boot_backup/.git").is_dir():
                hs.Command("git").init()

            hs.Command("git")("config", "user.email", "user@example.com")
            hs.Command("git")("config", "user.name", "user")

            timestamp = str(time.time())
            os.makedirs(timestamp)
            hs.Command("cp")("-ar", "/boot", timestamp + "/")
            hs.Command("git")("add", timestamp, "--force")
            hs.Command("git")("commit", "-m", timestamp)

    icp("kernel compile and install completed OK")
