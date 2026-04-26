#!/usr/bin/env python3


from __future__ import annotations

import gzip
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import hs
from asserttool import ic
from asserttool import icp
from asserttool import root_user
from eprint import eprint
from globalverbose import gvd
from pathtool import file_exists_nonzero
from with_chdir import chdir

# from rich import print as pprint
logging.basicConfig(level=logging.WARNING)

USED_SYMBOL_SET = set()


@dataclass
class ConfigOption:
    required_state: bool
    module: bool
    warn: bool
    url: str | None = None


# Ordered dict: later layers override earlier ones.
# Key = CONFIG_ define string, value = ConfigOption.
ConfigSpec = dict[str, ConfigOption]


def _spec_add(
    spec: ConfigSpec,
    define: str,
    required_state: bool,
    module: bool,
    warn: bool,
    url: str | None = None,
) -> None:
    if not define.startswith("CONFIG_"):
        raise ValueError(
            f"_spec_add: {define!r} must start with 'CONFIG_' "
            f"(verifier looks up keys by their full CONFIG_X name)"
        )
    spec[define] = ConfigOption(
        required_state=required_state,
        module=module,
        warn=warn,
        url=url,
    )


def _spec_apply(
    spec: ConfigSpec,
    path: Path,
    fix: bool,
) -> None:
    """Apply a fully-merged ConfigSpec, writing each symbol exactly once."""
    for define, opt in spec.items():
        verify_kernel_config_setting(
            path=path,
            define=define,
            required_state=opt.required_state,
            module=opt.module,
            warn=opt.warn,
            fix=fix,
            url=opt.url,
        )


# Integer-valued config options (e.g. CONFIG_STACK_DEPOT_MAX_ENTRIES).
# Stored as dict[symbol, int]; last-writer-wins same as ConfigSpec.
IntConfigSpec = dict[str, int]


def _int_spec_add(
    spec: IntConfigSpec,
    define: str,
    value: int,
) -> None:
    spec[define] = value


def _int_spec_apply(
    ispec: IntConfigSpec,
    path: Path,
    fix: bool,
) -> None:
    """Apply integer config values via scripts/config --set-val."""
    if not ispec:
        return
    script_path = Path("/usr/src/linux/scripts/config")
    for define, value in ispec.items():
        current = hs.Command(script_path)(
            "--file", path.as_posix(), "--state", define
        ).strip()
        if current == str(value):
            continue
        if fix:
            hs.Command(script_path)(
                "--file",
                path.as_posix(),
                "--set-val",
                define,
                str(value),
            )
        else:
            eprint(
                path.as_posix(),
                f"WARNING: {define} is {current!r} but should be {value}",
            )


_KCONFIG_INDEX_CACHE: dict[str, dict[str, dict]] = {}


def _kconfig_index(src: Path) -> dict[str, dict]:
    """Walk every Kconfig file under `src` once and return a dict
    {SYMBOL: {type, depends_on, file, line}}.

    type ∈ {'bool', 'tristate', 'string', 'int', 'hex', None}.
    depends_on is the conjunction of every direct `depends on` line under the
    symbol AND every enclosing `if EXPR` block, joined by ' && '. This matches
    Kconfig's actual visibility logic (an `if EXPR` wrapper has identical
    semantics to a `depends on EXPR` on every symbol it contains).
    Cached per-src so subsequent calls are free.
    """
    key = src.resolve().as_posix()
    if key in _KCONFIG_INDEX_CACHE:
        return _KCONFIG_INDEX_CACHE[key]

    cfg_re = re.compile(r"^(?:menu)?config\s+([A-Za-z0-9_]+)\s*$")
    type_re = re.compile(r"^(bool|tristate|string|int|hex)\b")
    dep_re = re.compile(r"^depends on\s+(.*?)\s*$")
    if_re = re.compile(r"^if\s+(.+?)\s*$")
    endif_re = re.compile(r"^endif\b")

    index: dict[str, dict] = {}
    for kfile in src.rglob("Kconfig*"):
        if not kfile.is_file():
            continue
        try:
            lines = kfile.read_text(encoding="utf8", errors="replace").splitlines()
        except OSError:
            continue
        if_stack: list[str] = []
        i = 0
        n = len(lines)
        while i < n:
            stripped = lines[i].lstrip().rstrip()
            ifm = if_re.match(stripped)
            if ifm:
                if_stack.append(ifm.group(1))
                i += 1
                continue
            if endif_re.match(stripped):
                if if_stack:
                    if_stack.pop()
                i += 1
                continue
            m = cfg_re.match(stripped)
            if not m:
                i += 1
                continue
            name = m.group(1)
            type_: str | None = None
            depends: list[str] = list(if_stack)  # inherit enclosing if-block deps
            j = i + 1
            while j < n:
                inner = lines[j].lstrip().rstrip()
                if cfg_re.match(inner):
                    break
                # nested if/endif inside the symbol body — uncommon but possible
                ifm2 = if_re.match(inner)
                if ifm2:
                    if_stack.append(ifm2.group(1))
                    j += 1
                    continue
                if endif_re.match(inner):
                    if if_stack:
                        if_stack.pop()
                    j += 1
                    continue
                tm = type_re.match(inner)
                if tm and type_ is None:
                    type_ = tm.group(1)
                dm = dep_re.match(inner)
                if dm:
                    depends.append(dm.group(1))
                j += 1
            # Last-writer-wins for duplicate declarations across architectures
            index[name] = {
                "type": type_,
                "depends_on": " && ".join(f"({d})" for d in depends) if depends else None,
                "file": kfile.as_posix(),
                "line": i + 1,
            }
            i = j

    _KCONFIG_INDEX_CACHE[key] = index
    return index


def _kmeta(define: str, index: dict[str, dict]) -> dict | None:
    name = define[len("CONFIG_"):] if define.startswith("CONFIG_") else define
    return index.get(name)


def _filter_spec_for_kernel(
    spec: ConfigSpec,
    src: Path,
) -> ConfigSpec:
    """Pre-process spec against the live kernel's Kconfig:
      • drop entries whose symbol no longer exists (removed in this version)
      • coerce module=True → module=False when Kconfig says the symbol is bool
    Returns a new dict; the input is not modified.
    """
    if not (src / "Kconfig").exists():
        return spec  # nothing to filter against

    index = _kconfig_index(src)
    out: ConfigSpec = {}
    for define, opt in spec.items():
        meta = _kmeta(define, index)
        if meta is None:
            eprint(f"spec filter: {define} not in this kernel — skipping (likely removed)")
            continue
        if opt.module and meta["type"] == "bool":
            eprint(f"spec filter: {define} is bool, not tristate — coercing module→y")
            out[define] = ConfigOption(
                required_state=opt.required_state,
                module=False,
                warn=opt.warn,
                url=opt.url,
            )
            continue
        out[define] = opt
    return out


def _resolve_and_verify_config(
    *,
    spec: ConfigSpec,
    ispec: IntConfigSpec,
    path: Path,
) -> None:
    """Run `make olddefconfig` to propagate Kconfig selects/choice resolution,
    then verify that every required-True symbol from the spec is actually set.

    scripts/config is text-only — it cannot propagate `select X` from a
    parent symbol to a child, and it cannot resolve choice blocks. Without
    this step, settings like CONFIG_UNWINDER_FRAME_POINTER=y exist in .config
    but the symbols they select (FRAME_POINTER, ARCH_WANT_FRAME_POINTERS) do
    not appear until make olddefconfig runs.
    """
    src = Path("/usr/src/linux")
    if not (src / "Makefile").exists():
        eprint("WARNING: /usr/src/linux/Makefile missing — cannot run olddefconfig")
        return

    icp("running make olddefconfig to propagate selects/choice")
    result = subprocess.run(
        ["make", "-C", src.as_posix(), "olddefconfig"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        eprint(
            "WARNING: make olddefconfig failed:",
            result.stderr.decode("utf8", errors="replace"),
        )
        return

    # Re-parse .config and verify required-True symbols
    content = path.read_text(encoding="utf8", errors="replace")
    state: dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("# CONFIG_") and line.endswith(" is not set"):
            sym = line[2:].split(" ", 1)[0]
            state[sym] = "n"
        elif line.startswith("CONFIG_") and "=" in line:
            sym, val = line.split("=", 1)
            state[sym] = val.strip().strip('"')

    missing: list[tuple[str, str, str]] = []
    for define, opt in spec.items():
        if not opt.required_state:
            continue
        want = "m" if opt.module else "y"
        got = state.get(define, "absent")
        if got != want and not (want == "y" and got == "m"):
            missing.append((define, want, got))

    if missing:
        index = _kconfig_index(src)
        eprint("WARNING: the following required symbols did not survive olddefconfig:")
        for sym, want, got in missing:
            meta = _kmeta(sym, index)
            if meta is None:
                eprint(f"  {sym}: not in Kconfig (removed?) — prune from spec")
            elif got == "absent" and meta.get("depends_on"):
                eprint(
                    f"  {sym}: hidden by unmet dependency — depends on: "
                    f"{meta['depends_on']}  (defined at {meta['file']}:{meta['line']})"
                )
                eprint(
                    f"      → enable each parent symbol in the spec, then re-run"
                )
            elif got == "y" and want == "m" and meta.get("type") == "bool":
                eprint(
                    f"  {sym}: spec wants =m but Kconfig declares it as bool "
                    f"— pre-filter should have coerced this; possible bug"
                )
            else:
                eprint(
                    f"  {sym}: want={want} got={got} "
                    f"(type={meta.get('type')}, depends_on={meta.get('depends_on')})"
                )
        eprint(
            "fix each entry above (prune, enable parents, or correct the spec), "
            "then re-run."
        )
        raise RuntimeError(
            f"{len(missing)} required config symbol(s) missing after olddefconfig"
        )
    else:
        icp("olddefconfig validated — all required symbols are set")


def generate_module_config_dict(path: Path):
    _manual_mappings = {}

    # _manual_mappings["USB_XHCI_PCI"] = ["xhci_pci.o"]
    # _manual_mappings["I2C_I801"] = ["i2c_i801.o"]

    _makefiles = list(Path(path).rglob("Makefile"))
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
        with open(_makefile, encoding="utf8") as f:
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
        with gzip.open(
            path,
            mode="rt",
            encoding="utf8",
        ) as _fh:
            content = _fh.read()
    except gzip.BadGzipFile:
        with open(path, encoding="utf8") as _fh:
            content = _fh.read()
    return content


def _detect_cpu_march_symbol() -> str:
    """Inspect /proc/cpuinfo and return the most specific CONFIG_M* march
    symbol the running CPU should be compiled for, or "CONFIG_GENERIC_CPU"
    if no specific match is found.

    Intel: family/model lookup against the canonical decoder ring.
    AMD: family/model + ISA hints (avx512_vnni, avx512vbmi2 etc.) to
        distinguish Zen2/Zen3/Zen4/Zen5.
    Anything we don't recognise falls back to GENERIC_CPU.
    """
    try:
        info = Path("/proc/cpuinfo").read_text(encoding="utf8", errors="replace")
    except OSError:
        return "CONFIG_GENERIC_CPU"

    vendor = ""
    family = -1
    model = -1
    flags: set[str] = set()
    for line in info.splitlines():
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if key == "vendor_id" and not vendor:
            vendor = val
        elif key == "cpu family" and family < 0:
            family = int(val) if val.isdigit() else -1
        elif key == "model" and model < 0:
            model = int(val) if val.isdigit() else -1
        elif key == "flags" and not flags:
            flags = set(val.split())
        if vendor and family >= 0 and model >= 0 and flags:
            break

    if vendor == "GenuineIntel" and family == 6:
        # Intel family-6 model decode table (low byte of CPUID DisplayModel)
        # Source: arch/x86/include/asm/intel-family.h plus historical data.
        intel_table = {
            # Core 2
            0x0F: "CONFIG_MCORE2", 0x16: "CONFIG_MCORE2", 0x17: "CONFIG_MCORE2",
            0x1D: "CONFIG_MCORE2",
            # Nehalem / Westmere
            0x1A: "CONFIG_MNEHALEM", 0x1E: "CONFIG_MNEHALEM",
            0x1F: "CONFIG_MNEHALEM", 0x2E: "CONFIG_MNEHALEM",
            0x25: "CONFIG_MWESTMERE", 0x2C: "CONFIG_MWESTMERE",
            0x2F: "CONFIG_MWESTMERE",
            # Sandy / Ivy Bridge
            0x2A: "CONFIG_MSANDYBRIDGE", 0x2D: "CONFIG_MSANDYBRIDGE",
            0x3A: "CONFIG_MIVYBRIDGE", 0x3E: "CONFIG_MIVYBRIDGE",
            # Haswell
            0x3C: "CONFIG_MHASWELL", 0x3F: "CONFIG_MHASWELL",
            0x45: "CONFIG_MHASWELL", 0x46: "CONFIG_MHASWELL",
            # Broadwell
            0x3D: "CONFIG_MBROADWELL", 0x47: "CONFIG_MBROADWELL",
            0x4F: "CONFIG_MBROADWELL", 0x56: "CONFIG_MBROADWELL",
            # Skylake / Kaby Lake / Coffee Lake / Comet Lake (client)
            0x4E: "CONFIG_MSKYLAKE", 0x5E: "CONFIG_MSKYLAKE",
            0x8E: "CONFIG_MSKYLAKE", 0x9E: "CONFIG_MSKYLAKE",
            0xA5: "CONFIG_MSKYLAKE", 0xA6: "CONFIG_MSKYLAKE",
            # Skylake-X / Cascade Lake / Cooper Lake (server, AVX-512)
            0x55: "CONFIG_MSKYLAKEX",
            # Cannon Lake
            0x66: "CONFIG_MCANNONLAKE",
            # Ice Lake
            0x6A: "CONFIG_MICELAKESERVER", 0x6C: "CONFIG_MICELAKESERVER",
            0x7E: "CONFIG_MICELAKE", 0x7D: "CONFIG_MICELAKE",
            # Tiger Lake
            0x8C: "CONFIG_MTIGERLAKE", 0x8D: "CONFIG_MTIGERLAKE",
            # Rocket Lake
            0xA7: "CONFIG_MROCKETLAKE",
            # Alder Lake / Raptor Lake / Meteor Lake (hybrid)
            0x97: "CONFIG_MALDERLAKE", 0x9A: "CONFIG_MALDERLAKE",
            0xB7: "CONFIG_MRAPTORLAKE", 0xBA: "CONFIG_MRAPTORLAKE",
            0xBF: "CONFIG_MRAPTORLAKE",
            0xAA: "CONFIG_MMETEORLAKE", 0xAC: "CONFIG_MMETEORLAKE",
            # Sapphire Rapids / Emerald Rapids (server)
            0x8F: "CONFIG_MSAPPHIRERAPIDS", 0xCF: "CONFIG_MEMERALDRAPIDS",
            # Atom / Goldmont / Tremont
            0x37: "CONFIG_MATOM", 0x4A: "CONFIG_MATOM", 0x4D: "CONFIG_MATOM",
            0x5A: "CONFIG_MATOM", 0x5D: "CONFIG_MATOM",
            0x5C: "CONFIG_MGOLDMONT", 0x5F: "CONFIG_MGOLDMONT",
            0x7A: "CONFIG_MGOLDMONTPLUS",
            0x86: "CONFIG_MTREMONT", 0x96: "CONFIG_MTREMONT",
            0x9C: "CONFIG_MTREMONT",
        }
        if model in intel_table:
            return intel_table[model]
        return "CONFIG_GENERIC_CPU"

    if vendor == "AuthenticAMD":
        # AMD: family + ISA hints — model numbers reuse across families so
        # ISA flags are the most reliable disambiguator for Zen generations.
        if family == 0x19:  # Zen 3 / Zen 4
            # Zen 4 introduced AVX-512 (avx512f, avx512vbmi2, avx512_vnni etc).
            if "avx512f" in flags:
                return "CONFIG_MZEN4"
            return "CONFIG_MZEN3"
        if family == 0x1A:  # Zen 5
            return "CONFIG_MZEN5"
        if family == 0x17:  # Zen / Zen+ / Zen 2
            # Zen 2 introduced CLWB and rdpru; bare Zen has neither.
            if "clwb" in flags:
                return "CONFIG_MZEN2"
            return "CONFIG_MZEN"
        if family == 0x15:  # Bulldozer family
            # 15h models 0x60-0x6F = Excavator, 0x30-0x3F = Steamroller,
            # 0x10-0x1F = Piledriver, 0x00-0x0F = Bulldozer
            if 0x60 <= model <= 0x7F:
                return "CONFIG_MEXCAVATOR"
            if 0x30 <= model <= 0x3F:
                return "CONFIG_MSTEAMROLLER"
            if 0x10 <= model <= 0x1F:
                return "CONFIG_MPILEDRIVER"
            return "CONFIG_MBULLDOZER"
        if family == 0x16:  # Jaguar / Puma
            return "CONFIG_MJAGUAR"
        if family == 0x14:  # Bobcat
            return "CONFIG_MBOBCAT"
        if family == 0x10:  # K10 (Barcelona/Phenom/Magny-Cours)
            return "CONFIG_MK10"
        if family == 0xF:   # K8 (Athlon64/Opteron/Turion)
            return "CONFIG_MK8"
        return "CONFIG_GENERIC_CPU"

    return "CONFIG_GENERIC_CPU"


# All x86 march choice members — used to mass-disable the ones we're not picking.
_X86_MARCH_CHOICE_SYMBOLS = (
    "CONFIG_M486SSE2", "CONFIG_M586", "CONFIG_M586TSC", "CONFIG_M586MMX",
    "CONFIG_M686", "CONFIG_MPENTIUMII", "CONFIG_MPENTIUMIII",
    "CONFIG_MPENTIUMM", "CONFIG_MPENTIUM4", "CONFIG_MK6", "CONFIG_MK7",
    "CONFIG_MK8", "CONFIG_MK8SSE3", "CONFIG_MK10", "CONFIG_MBARCELONA",
    "CONFIG_MBOBCAT", "CONFIG_MJAGUAR", "CONFIG_MBULLDOZER",
    "CONFIG_MPILEDRIVER", "CONFIG_MSTEAMROLLER", "CONFIG_MEXCAVATOR",
    "CONFIG_MZEN", "CONFIG_MZEN2", "CONFIG_MZEN3", "CONFIG_MZEN4",
    "CONFIG_MZEN5",
    "CONFIG_MCRUSOE", "CONFIG_MEFFICEON", "CONFIG_MWINCHIPC6",
    "CONFIG_MWINCHIP3D", "CONFIG_MCYRIXIII", "CONFIG_MVIAC3_2",
    "CONFIG_MVIAC7", "CONFIG_MGEODEGX1", "CONFIG_MGEODE_LX",
    "CONFIG_MELAN", "CONFIG_MATOM", "CONFIG_MGOLDMONT",
    "CONFIG_MGOLDMONTPLUS", "CONFIG_MTREMONT",
    "CONFIG_MCORE2", "CONFIG_MNEHALEM", "CONFIG_MWESTMERE",
    "CONFIG_MSILVERMONT", "CONFIG_MSANDYBRIDGE", "CONFIG_MIVYBRIDGE",
    "CONFIG_MHASWELL", "CONFIG_MBROADWELL", "CONFIG_MSKYLAKE",
    "CONFIG_MSKYLAKEX", "CONFIG_MCANNONLAKE", "CONFIG_MICELAKE",
    "CONFIG_MICELAKESERVER", "CONFIG_MTIGERLAKE", "CONFIG_MROCKETLAKE",
    "CONFIG_MALDERLAKE", "CONFIG_MRAPTORLAKE", "CONFIG_MMETEORLAKE",
    "CONFIG_MSAPPHIRERAPIDS", "CONFIG_MEMERALDRAPIDS",
    "CONFIG_MNATIVE_INTEL", "CONFIG_MNATIVE_AMD",
    "CONFIG_GENERIC_CPU",
)


def _zfs_debug_use_enabled() -> bool:
    """Return True if 'debug' is in sys-fs/zfs's currently configured USE flags.

    Uses the portage Python API with setcpv so package.use overrides apply,
    matching exactly what `emerge sys-fs/zfs` would see right now.
    Returns False if zfs is not in the portage tree.

    No exception handling: a broken portage installation should surface
    its own error rather than be silently ignored.
    """
    import portage  # imported lazily — only paid for at zfs_debug auto-detect

    db = portage.db[portage.root]
    portdb = db["porttree"].dbapi
    matches = portdb.match("sys-fs/zfs")
    if not matches:
        return False
    cpv = matches[-1]
    settings = portage.config(clone=portdb.settings)
    settings.setcpv(cpv, mydb=portdb)
    return "debug" in settings.get("USE", "").split()


def _decompress_config_if_needed(
    path: Path,
) -> tuple[Path, tempfile.NamedTemporaryFile | None]:
    """Return (plain_path, tmp) where tmp is a NamedTemporaryFile to keep alive,
    or None if the original path is already plain text.

    Handles:
      • plain text .config              → returned unchanged
      • gzipped config (e.g. /proc/config.gz) → decompressed to a temp file
      • bzImage/vmlinuz with CONFIG_IKCONFIG=y → extracted via
        scripts/extract-ikconfig (also handles ELF vmlinux)

    Raises:
      ValueError if the input is unrecognised (neither config nor kernel image).
      RuntimeError if a kernel image has no embedded IKCONFIG.
    """
    # 1. Try gzip first (covers /proc/config.gz)
    try:
        with gzip.open(path, "rb") as f:
            f.read(2)  # probe — raises BadGzipFile if not gzip
        tmp = tempfile.NamedTemporaryFile(
            mode="wb",
            suffix=".config",
            delete=False,
        )
        with gzip.open(path, "rb") as f_in:
            tmp.write(f_in.read())
        tmp.flush()
        tmp.close()
        return Path(tmp.name), tmp
    except gzip.BadGzipFile:
        pass

    # 2. Read enough to sniff
    try:
        head = path.read_bytes()[:8192]
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc

    # 3. Plain text kernel config — must contain CONFIG_ tokens within the head
    if b"CONFIG_" in head:
        return path, None

    # 4. Kernel image detection
    is_bzimage = len(head) > 0x206 and head[0x202:0x206] == b"HdrS"
    is_elf = head.startswith(b"\x7fELF")
    is_kernel_image = is_bzimage or is_elf

    if not is_kernel_image:
        raise ValueError(
            f"{path} is neither a kernel config nor a recognised kernel image "
            f"(no IKCONFIG signature, no bzImage HdrS magic, no ELF header)"
        )

    # 5. Try scripts/extract-ikconfig
    extract_script = Path("/usr/src/linux/scripts/extract-ikconfig")
    if not extract_script.exists():
        raise FileNotFoundError(
            f"{path} is a kernel image, but {extract_script} is missing — "
            f"cannot extract embedded config"
        )

    result = subprocess.run(
        [str(extract_script), str(path)],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or b"CONFIG_" not in result.stdout[:4096]:
        stderr_tail = result.stderr.decode("utf8", errors="replace").strip()
        raise RuntimeError(
            f"{path} is a kernel image but contains no embedded config "
            f"(kernel must be built with CONFIG_IKCONFIG=y).\n"
            f"extract-ikconfig stderr: {stderr_tail}"
        )

    tmp = tempfile.NamedTemporaryFile(
        mode="wb",
        suffix=".config",
        delete=False,
    )
    tmp.write(result.stdout)
    tmp.flush()
    tmp.close()
    return Path(tmp.name), tmp


def check_kernel_config_perf(*, path: Path) -> None:
    """Report kernel config options that may impact performance.

    Read-only analysis: walks a list of perf-relevant symbols, compares each
    to a recommended state, and prints findings grouped by category. Does not
    modify the config. Recommendations skew toward maximum throughput on a
    desktop/workstation; tradeoffs (security, latency) are noted in each entry.
    """
    path = path.resolve()
    path, _tmp_config = _decompress_config_if_needed(path)
    try:
        content = path.read_text(encoding="utf8", errors="replace")
    finally:
        if _tmp_config is not None:
            # keep temp file alive until after we've read it
            pass

    # Build {symbol: state} where state is "y", "m", "n", or a value string
    state: dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("# CONFIG_") and line.endswith(" is not set"):
            sym = line[2:].split(" ", 1)[0]
            state[sym] = "n"
        elif line.startswith("CONFIG_") and "=" in line:
            sym, val = line.split("=", 1)
            state[sym] = val.strip().strip('"')

    def get(sym: str) -> str:
        return state.get(sym, "?")

    # Each finding: (symbol, want, severity, explanation)
    # severity: HIGH (big perf swing), MED, LOW (minor or situational), INFO (just FYI)
    categories: list[tuple[str, list[tuple[str, str, str, str]]]] = [
        ("Debug overhead (significant cost when enabled)", [
            ("CONFIG_KASAN", "n", "HIGH", "memory sanitizer ~2-3x slowdown on every load/store"),
            ("CONFIG_KFENCE", "n", "LOW", "low-rate sampling sanitizer; cheap if KASAN is off"),
            ("CONFIG_DEBUG_KMEMLEAK", "n", "MED", "scans every alloc/free for unreferenced objects"),
            ("CONFIG_PROVE_LOCKING", "n", "HIGH", "lockdep instrumentation on every lock op"),
            ("CONFIG_LOCKDEP", "n", "HIGH", "lock dependency tracking core"),
            ("CONFIG_DEBUG_LOCK_ALLOC", "n", "HIGH", "lock allocation tracking"),
            ("CONFIG_DEBUG_SPINLOCK", "n", "HIGH", "spinlock debug overhead in hot paths"),
            ("CONFIG_DEBUG_MUTEXES", "n", "MED", "mutex debug overhead"),
            ("CONFIG_DEBUG_RWSEMS", "n", "MED", "rwsem fast-path accounting; threaded read-heavy workloads pay it"),
            ("CONFIG_DEBUG_ATOMIC_SLEEP", "n", "MED", "scheduler hot-path checks"),
            ("CONFIG_PROVE_RCU", "n", "MED", "RCU usage validation"),
            ("CONFIG_DEBUG_OBJECTS", "n", "MED", "object lifecycle tracking"),
            ("CONFIG_SLUB_DEBUG_ON", "n", "MED", "SLUB debug at runtime (vs SLUB_DEBUG which is off-by-default)"),
            ("CONFIG_DEBUG_PAGEALLOC", "n", "HIGH", "unmaps every freed page; ~100x slowdown on alloc/free"),
            ("CONFIG_PAGE_POISONING", "n", "MED", "writes poison pattern on every free"),
            ("CONFIG_INIT_ON_ALLOC_DEFAULT_ON", "n", "MED", "zeros memory on every alloc"),
            ("CONFIG_INIT_ON_FREE_DEFAULT_ON", "n", "MED", "zeros memory on every free"),
            ("CONFIG_KCSAN", "n", "MED", "data race sampling instrumentation"),
            ("CONFIG_UBSAN", "n", "LOW", "undefined behaviour sanitizer (~5% overhead)"),
            ("CONFIG_DMA_API_DEBUG", "n", "MED", "DMA API correctness checks on every map/unmap"),
            ("CONFIG_FUNCTION_TRACER", "n", "MED", "ftrace nop overhead on every function entry"),
            ("CONFIG_DEBUG_LIST", "n", "LOW", "list_head integrity checks"),
            ("CONFIG_DEBUG_SG", "n", "LOW", "scatter-gather list checks on every DMA"),
            ("CONFIG_DEBUG_PREEMPT", "n", "LOW", "preempt count debug"),
            ("CONFIG_TRACE_IRQFLAGS", "n", "LOW", "IRQ flags state tracking"),
            ("CONFIG_FAULT_INJECTION", "n", "LOW", "no cost unless triggered, but adds branches"),
        ]),
        ("Preemption / latency", [
            ("CONFIG_PREEMPT_DYNAMIC", "y", "MED", "runtime preempt selection via preempt= cmdline"),
            ("CONFIG_PREEMPT_VOLUNTARY", "y", "INFO", "voluntary preempt — best interactive default"),
            ("CONFIG_HZ_1000", "y", "INFO", "1000Hz tick — best interactive responsiveness"),
            ("CONFIG_NO_HZ_IDLE", "y", "MED", "tickless when idle — reduces wakeups, saves power"),
            ("CONFIG_HIGH_RES_TIMERS", "y", "MED", "required for accurate timing"),
            ("CONFIG_RCU_NOCB_CPU", "y", "MED", "offload RCU callbacks to dedicated kthreads"),
        ]),
        ("CPU mitigations (perf vs security tradeoff)", [
            ("CONFIG_PAGE_TABLE_ISOLATION", "n", "HIGH", "KPTI; ~5-30% syscall cost on Intel — disable only if not vulnerable or accepting risk"),
            ("CONFIG_MITIGATION_RETPOLINE", "n", "MED", "Spectre v2 mitigation; affects all indirect calls"),
            ("CONFIG_MITIGATION_RETHUNK", "n", "MED", "AMD/Intel return mitigations"),
            ("CONFIG_RANDOMIZE_BASE", "n", "LOW", "KASLR; minor TLB cost"),
            ("CONFIG_RANDOMIZE_MEMORY", "n", "LOW", "memory layout randomization"),
            ("CONFIG_STACKPROTECTOR_STRONG", "y", "INFO", "modest cost, large security benefit; keep on unless benchmarking"),
        ]),
        ("Scheduler", [
            ("CONFIG_SCHED_AUTOGROUP", "y", "MED", "desktop responsiveness under load"),
            ("CONFIG_SCHED_MC", "y", "MED", "multi-core load balancing"),
            ("CONFIG_SCHED_SMT", "y", "MED", "SMT-aware scheduling on hyperthreaded CPUs"),
            ("CONFIG_SCHED_CLUSTER", "y", "LOW", "cluster-aware scheduling (Intel hybrid, ARM big.LITTLE)"),
            ("CONFIG_FAIR_GROUP_SCHED", "y", "INFO", "needed for cgroup CPU control"),
        ]),
        ("CPU frequency / idle", [
            ("CONFIG_X86_INTEL_PSTATE", "y", "MED", "modern Intel P-state driver (HWP-aware)"),
            ("CONFIG_X86_AMD_PSTATE", "y", "MED", "modern AMD P-state driver"),
            ("CONFIG_CPU_FREQ_DEFAULT_GOV_SCHEDUTIL", "y", "MED", "best-balance default governor; sees scheduler load"),
            ("CONFIG_CPU_IDLE_GOV_TEO", "y", "LOW", "Timer Events Oriented idle governor — better than menu"),
        ]),
        ("Memory management", [
            ("CONFIG_TRANSPARENT_HUGEPAGE", "y", "MED", "THP support; large perf win for many workloads"),
            ("CONFIG_TRANSPARENT_HUGEPAGE_MADVISE", "y", "INFO", "default-madvise is safer than always; less RSS bloat"),
            ("CONFIG_COMPACTION", "y", "MED", "memory defrag for hugepage allocations"),
            ("CONFIG_NUMA_BALANCING", "y", "MED", "auto-migrate pages to local NUMA node (multi-socket only)"),
            ("CONFIG_ZSWAP", "y", "MED", "compressed swap cache; faster than disk swap under pressure"),
            ("CONFIG_ZSWAP_DEFAULT_ON", "y", "LOW", "enable zswap by default (else needs cmdline)"),
        ]),
        ("I/O", [
            ("CONFIG_BLK_WBT_MQ", "y", "MED", "writeback throttling — keeps reads responsive under heavy writes"),
            ("CONFIG_IOSCHED_BFQ", "y", "INFO", "BFQ I/O scheduler available (good for desktop interactive)"),
            ("CONFIG_MQ_IOSCHED_KYBER", "y", "INFO", "Kyber I/O scheduler (good for fast SSDs)"),
            ("CONFIG_IO_URING", "y", "MED", "modern async I/O API; major perf win for I/O-bound apps"),
        ]),
        ("Networking", [
            ("CONFIG_NET_RX_BUSY_POLL", "y", "MED", "low-latency packet polling for sockets"),
            ("CONFIG_TCP_CONG_BBR", "y", "MED", "BBR congestion control; far better than cubic on lossy/long-RTT"),
            ("CONFIG_BPF_JIT", "y", "MED", "JIT eBPF programs (XDP, tc, seccomp)"),
            ("CONFIG_BPF_JIT_ALWAYS_ON", "y", "LOW", "force-enable JIT (security: prevents interpreter)"),
            ("CONFIG_XDP_SOCKETS", "y", "INFO", "AF_XDP for kernel-bypass networking"),
        ]),
        ("x86 features", [
            ("CONFIG_X86_X2APIC", "y", "LOW", "x2APIC — required for >255 CPUs, faster on modern hw"),
            ("CONFIG_X86_FRED", "y", "INFO", "Flexible Return and Event Delivery (Intel, kernel 6.9+)"),
            ("CONFIG_COMPAT", "n", "LOW", "32-bit userspace support; off by default — flip with --ia32"),
            ("CONFIG_IA32_EMULATION", "n", "LOW", "32-bit syscall emulation; off by default — flip with --ia32"),
        ]),
    ]

    # Programmatic CPU march check: detect what we should be using and check
    # that exactly that symbol is enabled (and GENERIC_CPU is not).
    expected_march = _detect_cpu_march_symbol()
    march_state = state.get(expected_march, "absent")
    if march_state != "y":
        print("=== CPU march ===")
        print(f"  [HIGH] {expected_march:<48} = {march_state:<6}  want y  — autodetected for this CPU")
        print()
        issue_count += 1
    if expected_march != "CONFIG_GENERIC_CPU" and state.get("CONFIG_GENERIC_CPU") == "y":
        print("=== CPU march ===")
        print(f"  [HIGH] CONFIG_GENERIC_CPU                              = y       want n  — masks specific march, costs 5-15%")
        print()
        issue_count += 1

    print(f"perf-relevant config analysis: {path}")
    print()

    issue_count = 0
    for cat_name, checks in categories:
        cat_findings: list[str] = []
        for sym, want, sev, why in checks:
            cur = get(sym)
            ok = (cur == want) or (want == "y" and cur == "m")
            if ok and sev == "INFO":
                continue
            if ok:
                continue
            cat_findings.append(f"  [{sev:4}] {sym:<48} = {cur:<6}  want {want}  — {why}")
            issue_count += 1
        if cat_findings:
            print(f"=== {cat_name} ===")
            for line in cat_findings:
                print(line)
            print()

    if issue_count == 0:
        print("no perf-relevant deviations found.")
    else:
        print(f"{issue_count} perf-relevant deviation(s); review tradeoffs before changing.")

    if _tmp_config is not None:
        Path(_tmp_config.name).unlink(missing_ok=True)


def get_set_kernel_config_option(
    *,
    path: Path,
    define: str,
    state: bool,
    module: bool,
    get: bool,
) -> None | str:
    ic(
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
        ic(
            _current_state,
            required_state,
            module,
        )
        return
    if not required_state and not module and _current_state not in ("y", "m"):
        return  # undef/n/absent all mean "not enabled" — satisfied

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
    if not required_state and not module and _current_state not in ("y", "m"):
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
    spec: ConfigSpec,
    warn_only: bool,
):
    _spec_add(
        spec,
        "CONFIG_NFS_FS",
        required_state=True,
        module=True,
        warn=warn_only,
        url=None,
    )
    _spec_add(
        spec,
        "CONFIG_NFSD",
        required_state=True,
        module=True,
        warn=warn_only,
        url=None,
    )
    _spec_add(
        spec,
        "CONFIG_NFSD_V4",
        required_state=True,
        module=False,
        warn=warn_only,
        url=None,
    )
    _spec_add(
        spec,
        "CONFIG_NFS_V4",
        required_state=True,
        module=True,
        warn=warn_only,
        url=None,
    )
    _spec_add(
        spec,
        "CONFIG_NFS_V4_1",
        required_state=True,
        module=False,
        warn=warn_only,
        url=None,
    )
    _spec_add(
        spec,
        "CONFIG_NFS_V4_2",
        required_state=True,
        module=False,
        warn=warn_only,
        url=None,
    )


def check_kernel_config_kasan(
    *,
    spec: ConfigSpec,
    enable: bool,
) -> None:
    _spec_add(
        spec,
        "CONFIG_KASAN",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_KASAN_INLINE",
        required_state=enable,
        module=False,
        warn=True,
    )  # 2-3x faster than outline
    _spec_add(
        spec,
        "CONFIG_KASAN_VMALLOC",
        required_state=enable,
        module=False,
        warn=True,
    )  # kvmalloc(16KB) can fall back to vmalloc
    _spec_add(
        spec,
        "CONFIG_PANIC_ON_OOPS",
        required_state=enable,
        module=False,
        warn=True,
    )  # force kdump instead of continuing in corrupted state
    _spec_add(
        spec,
        "CONFIG_KASAN_STACK",
        required_state=enable,
        module=False,
        warn=True,
    )
    # disable the slower outline variant when inline is requested
    _spec_add(
        spec,
        "CONFIG_KASAN_OUTLINE",
        required_state=False,
        module=False,
        warn=True,
    )


def check_kernel_config_kmemleak(
    *,
    spec: ConfigSpec,
    enable: bool,
) -> None:
    _spec_add(
        spec,
        "CONFIG_DEBUG_KMEMLEAK",
        required_state=enable,
        module=False,
        warn=True,
    )


def check_kernel_config_slub_debug(
    *,
    spec: ConfigSpec,
    enable: bool,
) -> None:
    _spec_add(
        spec,
        "CONFIG_SLUB_DEBUG",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_SLUB_DEBUG_ON",
        required_state=enable,
        module=False,
        warn=True,
    )


def check_kernel_config_lockdep(
    *,
    spec: ConfigSpec,
    enable: bool,
) -> None:
    _spec_add(
        spec,
        "CONFIG_PROVE_LOCKING",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_DEBUG_SPINLOCK",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_DEBUG_MUTEXES",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_DEBUG_LOCK_ALLOC",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_PROVE_RCU",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_DEBUG_ATOMIC_SLEEP",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_LOCKDEP",
        required_state=enable,
        module=False,
        warn=True,
    )  # tracked explicitly even though selected by PROVE_LOCKING/DEBUG_LOCK_ALLOC
    _spec_add(
        spec,
        "CONFIG_DEBUG_RWSEMS",
        required_state=enable,
        module=False,
        warn=True,
    )  # rwsem fast-path accounting; same cost class as DEBUG_SPINLOCK/MUTEXES


def check_kernel_config_debug_objects(
    *,
    spec: ConfigSpec,
    enable: bool,
) -> None:
    _spec_add(
        spec,
        "CONFIG_DEBUG_OBJECTS",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_DEBUG_OBJECTS_FREE",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_DEBUG_OBJECTS_TIMERS",
        required_state=enable,
        module=False,
        warn=True,
    )


def check_kernel_config_gcov(
    *,
    spec: ConfigSpec,
    enable: bool,
) -> None:
    _spec_add(
        spec,
        "CONFIG_DEBUG_FS",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_GCOV_KERNEL",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_GCOV_FORMAT_AUTODETECT",
        required_state=enable,
        module=False,
        warn=True,
    )


def check_kernel_config_zbtree_debug(
    *,
    spec: ConfigSpec,
    enable: bool,
) -> None:
    """Minimal debug set for out-of-tree module development (zbtree et al).
    KFENCE: low-overhead sampling UAF/OOB, compatible with out-of-tree modules.
    SLUB_DEBUG: poisons freed slab objects (0x6b), catches UAF on next access.
    DEBUG_OBJECTS: tracks registered kernel object lifecycle.

    Overlapping entries (e.g. SLUB_DEBUG also in slub_debug group) are fine —
    the spec dict is last-writer-wins so the final value is simply the most
    recently applied layer. No USED_SYMBOL_SET conflict possible.
    """
    _spec_add(
        spec,
        "CONFIG_KFENCE",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_SLUB_DEBUG",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_DEBUG_OBJECTS",
        required_state=enable,
        module=False,
        warn=True,
    )


def check_kernel_config_ubsan(
    *,
    spec: ConfigSpec,
    enable: bool,
) -> None:
    """Undefined Behaviour Sanitizer.
    Catches C UB (OOB, shift, bad enum/bool) at runtime with ~5-15% overhead.
    UBSAN_TRAP is intentionally omitted — it turns every UB hit into a kernel
    panic, which is too aggressive for normal use. UBSAN_INTEGER_WRAP is also
    omitted; it fires on intentional signed wrap in hot paths and is very noisy.
    """
    _spec_add(
        spec,
        "CONFIG_UBSAN",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_UBSAN_BOUNDS",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_UBSAN_SHIFT",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_UBSAN_BOOL",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_UBSAN_ENUM",
        required_state=enable,
        module=False,
        warn=True,
    )


def check_kernel_config_kcsan(
    *,
    spec: ConfigSpec,
    enable: bool,
) -> None:
    """Kernel Concurrency Sanitizer — sampling data-race detector (6.0+, x86_64).
    KCSAN_ASSUME_PLAIN_WRITES_ATOMIC suppresses races where only the plain write
    side is uninstrumented, reducing false positives significantly.
    Note: mutually exclusive with KASAN in some kernel versions; compat layer
    should disable KASAN if both are requested.
    """
    _spec_add(
        spec,
        "CONFIG_KCSAN",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_KCSAN_ASSUME_PLAIN_WRITES_ATOMIC",
        required_state=enable,
        module=False,
        warn=True,
    )


def check_kernel_config_watchdog(
    *,
    spec: ConfigSpec,
    enable: bool,
) -> None:
    """Lockup and hung-task detectors.
    LOCKUP_DETECTOR: parent Kconfig gate — required for soft/hardlockup.
    SOFTLOCKUP: CPU stuck in kernel >10s (NMI-safe).
    HARDLOCKUP: CPU not taking IRQs (NMI watchdog, requires perf PMU).
    HARDLOCKUP_DETECTOR_PERF: x86 perf-PMU implementation of hardlockup.
    DETECT_HUNG_TASK: task in D-state >120s.
    WQ_WATCHDOG: workqueue stall detection.
    """
    _spec_add(
        spec,
        "CONFIG_LOCKUP_DETECTOR",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_SOFTLOCKUP_DETECTOR",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_HARDLOCKUP_DETECTOR",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_HARDLOCKUP_DETECTOR_PERF",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_DETECT_HUNG_TASK",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_WQ_WATCHDOG",
        required_state=enable,
        module=False,
        warn=True,
    )


def check_kernel_config_fault_inject(
    *,
    spec: ConfigSpec,
    enable: bool,
) -> None:
    """Fault injection framework.
    Allows injecting allocation failures into kmalloc and page allocator via
    debugfs knobs — useful for testing error-path coverage in drivers.
    DEBUG_FS is required to control injection at runtime.
    """
    _spec_add(
        spec,
        "CONFIG_FAULT_INJECTION",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_FAILSLAB",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_FAIL_PAGE_ALLOC",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_FAULT_INJECTION_DEBUG_FS",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_DEBUG_FS",
        required_state=enable,
        module=False,
        warn=True,
    )


def check_kernel_config_mem_init(
    *,
    spec: ConfigSpec,
    enable: bool,
) -> None:
    """Memory initialisation and page poisoning.
    INIT_ON_ALLOC: zero kmalloc/page alloc — catches use of uninit reads.
    INIT_ON_FREE: zero on free — catches use-after-free reads cheaply.
    PAGE_POISONING: poison freed pages with 0xAA pattern (catches UAF on access).
    DEBUG_PAGEALLOC is intentionally omitted: it unmaps every freed page and
    incurs a TLB flush per free — O(100x) slowdown, impractical outside bisects.
    """
    _spec_add(
        spec,
        "CONFIG_INIT_ON_ALLOC_DEFAULT_ON",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_INIT_ON_FREE_DEFAULT_ON",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_PAGE_POISONING",
        required_state=enable,
        module=False,
        warn=True,
    )


def check_kernel_config_dma_debug(
    *,
    spec: ConfigSpec,
    enable: bool,
) -> None:
    """DMA API correctness checking.
    Catches mapping leaks, double-free, and direction mismatches in DMA users.
    DMA_API_DEBUG_SG adds extra scatter-gather list validation.
    """
    _spec_add(
        spec,
        "CONFIG_DMA_API_DEBUG",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_DMA_API_DEBUG_SG",
        required_state=enable,
        module=False,
        warn=True,
    )


def check_kernel_config_data_struct_debug(
    *,
    spec: ConfigSpec,
    enable: bool,
) -> None:
    """Data structure integrity checks.
    DEBUG_LIST/PLIST: detect list_head corruption (prev/next pointer stomps).
    DEBUG_SG: validate scatterlist structure on every DMA call.
    DEBUG_NOTIFIERS: validate notifier chain call order and types.
    DEBUG_IRQFLAGS: track IRQ enable/disable state for inconsistency detection.
    """
    _spec_add(
        spec,
        "CONFIG_DEBUG_LIST",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_DEBUG_PLIST",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_DEBUG_SG",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_DEBUG_NOTIFIERS",
        required_state=enable,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_DEBUG_IRQFLAGS",
        required_state=enable,
        module=False,
        warn=True,
    )


def check_kernel_config_netconsole(
    *,
    spec: ConfigSpec,
    enable: bool,
) -> None:
    """Netconsole — send kernel log messages over UDP to a remote syslog host.
    NETCONSOLE_DYNAMIC enables runtime reconfiguration via configfs (target
    IP/port/interface changeable without reboot). Requires NETCONSOLE as the
    parent module.
    """
    _spec_add(
        spec,
        "CONFIG_NETCONSOLE",
        required_state=enable,
        module=enable,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_NETCONSOLE_DYNAMIC",
        required_state=enable,
        module=False,
        warn=True,
    )


def check_kernel_config_harden(
    *,
    spec: ConfigSpec,
    enable: bool,
) -> None:
    """Hardening: re-enable mitigations and KASLR that the production base
    leaves off for performance. NO-OP when enable is False (production base
    has them off already).

    Mitigations covered: KPTI (Meltdown), retpoline + rethunk (Spectre v2),
    SLS (Straight-Line-Speculation), SRSO (AMD speculative RAS),
    GDS_FORCE (Intel Gather Data Sampling), KASLR base + memory.
    """
    if not enable:
        return
    for sym in (
        "CONFIG_PAGE_TABLE_ISOLATION",
        "CONFIG_MITIGATION_RETPOLINE",
        "CONFIG_MITIGATION_RETHUNK",
        "CONFIG_MITIGATION_SLS",
        "CONFIG_MITIGATION_SRSO",
        "CONFIG_MITIGATION_GDS_FORCE",
        "CONFIG_RANDOMIZE_BASE",
        "CONFIG_RANDOMIZE_MEMORY",
        "CONFIG_CPU_MITIGATIONS",
    ):
        _spec_add(spec, sym, required_state=True, module=False, warn=True)


def check_kernel_config_ia32(
    *,
    spec: ConfigSpec,
    enable: bool,
) -> None:
    """32-bit emulation: enable COMPAT and IA32_EMULATION so 32-bit binaries
    (Wine, old Steam games, proprietary blobs) can run. Off by default for
    a small syscall-path perf win on pure 64-bit systems.
    """
    if not enable:
        return
    _spec_add(spec, "CONFIG_COMPAT", required_state=True, module=False, warn=True)
    _spec_add(spec, "CONFIG_IA32_EMULATION", required_state=True, module=False, warn=True)


def check_kernel_config_bpftrace(
    *,
    spec: ConfigSpec,
    enable: bool,
) -> None:
    """dev-debug/bpftrace requirements:
        CONFIG_DEBUG_INFO_BTF=y          — BTF type info embedded in vmlinux
        CONFIG_DEBUG_INFO_BTF_MODULES=y  — BTF for modules (kernel 5.11+)
        CONFIG_FTRACE_SYSCALLS=y         — syscall tracepoints used by uprobes/kfunc

    Costs are mostly disk/build-time (BTF generation needs pahole, adds ~5-10MB
    to vmlinux) — runtime cost is negligible. NO-OP when enable is False
    (production base keeps these off so bpftrace builds fail loudly without
    --bpftrace, indicating they need to be enabled).
    """
    if not enable:
        return
    _spec_add(spec, "CONFIG_DEBUG_INFO_BTF", required_state=True, module=False, warn=True)
    _spec_add(spec, "CONFIG_DEBUG_INFO_BTF_MODULES", required_state=True, module=False, warn=True)
    _spec_add(spec, "CONFIG_FTRACE_SYSCALLS", required_state=True, module=False, warn=True)


def check_kernel_config_zfs_compat_lockdep(
    *,
    spec: ConfigSpec,
) -> None:
    """ZFS build compatibility overrides.
    LOCKDEP and DEBUG_LOCK_ALLOC are 'select'-only symbols — they cannot be
    directly disabled; Kconfig will re-enable them via make oldconfig if any
    of their selectors remain set. Must disable the full selector chain:

      PROVE_LOCKING  → selects LOCKDEP, DEBUG_LOCK_ALLOC
      LOCK_STAT      → selects LOCKDEP
      DEBUG_LOCK_ALLOC → selects LOCKDEP

    Also disable DEBUG_SPINLOCK and DEBUG_MUTEXES from the lockdep group so
    there are no lingering symbols that could trigger re-selection on future
    oldconfig runs.
    """
    # Disable in dependency order: user-visible selectors first, then selected symbols.
    # PROVE_LOCKING and LOCK_STAT are the root user-visible nodes.
    # PROVE_LOCKING also selects DEBUG_WW_MUTEX_SLOWPATH which itself selects DEBUG_LOCK_ALLOC.
    # Must disable the full chain or make oldconfig re-enables it.
    _spec_add(
        spec,
        "CONFIG_PROVE_LOCKING",
        required_state=False,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_LOCK_STAT",
        required_state=False,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_DEBUG_WW_MUTEX_SLOWPATH",
        required_state=False,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_DEBUG_LOCK_ALLOC",
        required_state=False,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_DEBUG_SPINLOCK",
        required_state=False,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_DEBUG_MUTEXES",
        required_state=False,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_DEBUG_RWSEMS",
        required_state=False,
        module=False,
        warn=True,
    )
    # LOCKDEP is select-only — disable last after all selectors are cleared
    _spec_add(
        spec,
        "CONFIG_LOCKDEP",
        required_state=False,
        module=False,
        warn=True,
    )


def check_kernel_config_nvidia_compat(
    *,
    spec: ConfigSpec,
) -> None:
    """nvidia-drivers build compatibility overrides.
    nvidia-drivers-590 refuses to build if any of these are set.
    """
    _spec_add(
        spec,
        "CONFIG_LOCKDEP",
        required_state=False,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_SLUB_DEBUG_ON",
        required_state=False,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_DEBUG_MUTEXES",
        required_state=False,
        module=False,
        warn=True,
    )


def check_kernel_config_zfs_debug(
    *,
    spec: ConfigSpec,
    enable: bool,
) -> None:
    """sys-fs/zfs[debug] requires CONFIG_FRAME_POINTER.

    On x86, FRAME_POINTER is selected only by UNWINDER_FRAME_POINTER (the two
    user-visible unwinders are mutually exclusive: UNWINDER_ORC and
    UNWINDER_FRAME_POINTER). Production base sets ORC=y; this function
    overrides to UNWINDER_FRAME_POINTER=y and ORC=n when enable is True.

    When enable is False this function is a NO-OP — the production base's
    ORC=y / UNWINDER_FRAME_POINTER=n remain in the spec dict. Writing
    required_state=False here would still be correct but would also clobber
    if the production base ever changed; doing nothing is safer and matches
    the layer-system contract (sub-functions only override when active).
    """
    if not enable:
        return
    _spec_add(
        spec,
        "CONFIG_UNWINDER_FRAME_POINTER",
        required_state=True,
        module=False,
        warn=True,
    )
    _spec_add(
        spec,
        "CONFIG_UNWINDER_ORC",
        required_state=False,
        module=False,
        warn=True,
    )


def check_kernel_config(
    *,
    path: Path,
    fix: bool,
    warn_only: bool,
    kasan: bool = False,
    kmemleak: bool = False,
    slub_debug: bool = False,
    lockdep: bool = False,
    debug_objects: bool = False,
    gcov: bool = False,
    zbtree_debug: bool = False,
    zfs_debug: bool = False,
    ubsan: bool = False,
    kcsan: bool = False,
    watchdog: bool = False,
    fault_inject: bool = False,
    mem_init: bool = False,
    dma_debug: bool = False,
    data_struct_debug: bool = False,
    netconsole: bool = True,
    harden: bool = False,
    ia32: bool = False,
    bpftrace: bool = False,
    zfs_compat_lockdep: bool = False,
    nvidia_compat: bool = False,
):
    icp(
        path,
        fix,
        warn_only,
    )
    global USED_SYMBOL_SET
    USED_SYMBOL_SET = set()

    path = path.resolve()
    path, _tmp_config = _decompress_config_if_needed(path)
    assert insure_config_exists()
    icp(path, warn_only)

    # --- build the merged spec in layers ---
    spec: ConfigSpec = {}
    ispec: IntConfigSpec = {}

    # layer 1: production base
    check_kernel_config_nfs(spec=spec, warn_only=warn_only)

    # BPF, required for CONFIG_FUNCTION_TRACER
    _spec_add(
        spec,
        "CONFIG_FTRACE",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # BPF, required for CONFIG_FUNCTION_TRACER (to enable it dynamically, otherwise major slowdown)
    _spec_add(
        spec,
        "CONFIG_DYNAMIC_FTRACE",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # BPF
    _spec_add(
        spec,
        "CONFIG_FUNCTION_TRACER",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )

    _spec_add(
        spec,
        "CONFIG_HAVE_FENTRY",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # to see options like CONFIG_TRIM_UNUSED_KSYMS
    _spec_add(
        spec,
        "CONFIG_EXPERT",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # warnings as errors
    _spec_add(
        spec,
        "CONFIG_WERROR",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # fs
    _spec_add(
        spec,
        "CONFIG_EXT2_FS",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # fs
    _spec_add(
        spec,
        "CONFIG_EXT3_FS",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # fs
    _spec_add(
        spec,
        "CONFIG_EXFAT_FS",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # fs
    _spec_add(
        spec,
        "CONFIG_NTFS_FS",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # sec
    _spec_add(
        spec,
        "CONFIG_FORTIFY_SOURCE",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # sec
    _spec_add(
        spec,
        "CONFIG_HARDENED_USERCOPY",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )

    # legacy old
    _spec_add(
        spec,
        "CONFIG_UID16",
        required_state=False,
        module=False,
        warn=warn_only,
        url="",
    )
    # not a paravirt kernel
    _spec_add(
        spec,
        "CONFIG_PARAVIRT",
        required_state=False,
        module=False,
        warn=warn_only,
        url="",
    )
    # kvm
    _spec_add(
        spec,
        "CONFIG_KVM",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # kvm
    _spec_add(
        spec,
        "CONFIG_KVM_AMD",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # kvm
    _spec_add(
        spec,
        "CONFIG_VIRTIO_BALLOON",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # pcie
    _spec_add(
        spec,
        "CONFIG_HOTPLUG_PCI_PCIE",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # intel low power support
    _spec_add(
        spec,
        "CONFIG_X86_INTEL_LPSS",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # boot VESA
    _spec_add(
        spec,
        "CONFIG_FB",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # boot VESA
    _spec_add(
        spec,
        "CONFIG_FRAMEBUFFER_CONSOLE",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # boot VESA
    _spec_add(
        spec,
        "CONFIG_FB_MODE_HELPERS",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # boot VESA
    _spec_add(
        spec,
        "CONFIG_FB_RADEON",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # boot VESA
    _spec_add(
        spec,
        "CONFIG_FB_NVIDIA",
        required_state=False,
        module=False,
        warn=warn_only,
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
    _spec_add(
        spec,
        "CONFIG_SYSFB_SIMPLEFB",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # boot VESA
    _spec_add(
        spec,
        "CONFIG_BOOT_VESA_SUPPORT",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # boot VESA
    _spec_add(
        spec,
        "CONFIG_DRM_LOAD_EDID_FIRMWARE",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # power managment debug
    _spec_add(
        spec,
        "CONFIG_PM_DEBUG",
        required_state=False,
        module=False,
        warn=warn_only,
        url="",
    )

    # required for CONFIG_MEDIA_USB_SUPPORT below
    _spec_add(
        spec,
        "CONFIG_MEDIA_SUPPORT",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # unknown if necessary
    _spec_add(
        spec,
        "CONFIG_MEDIA_USB_SUPPORT",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )

    _spec_add(
        spec,
        "CONFIG_FB_EFI",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )

    _spec_add(
        spec,
        "CONFIG_TRIM_UNUSED_KSYMS",
        required_state=False,
        module=False,
        warn=warn_only,
        url="",
    )

    _spec_add(
        spec,
        "CONFIG_INTEL_IOMMU_DEFAULT_ON",
        required_state=False,
        module=False,
        warn=True,
        url="http://forums.debian.net/viewtopic.php?t=126397",
    )

    _spec_add(
        spec,
        "CONFIG_IKCONFIG_PROC",
        required_state=True,
        module=False,
        warn=warn_only,
        url=None,
    )

    _spec_add(
        spec,
        "CONFIG_IKCONFIG",
        required_state=True,
        module=False,
        warn=warn_only,
        url=None,
    )

    _spec_add(
        spec,
        "CONFIG_SUNRPC_DEBUG",
        required_state=True,
        module=False,
        warn=warn_only,
        url=None,
    )

    # symbol table + stack trace — needed for any meaningful oops/trace
    _spec_add(
        spec,
        "CONFIG_STACKTRACE",
        required_state=True,
        module=False,
        warn=warn_only,
        url=None,
    )
    _spec_add(
        spec,
        "CONFIG_KALLSYMS",
        required_state=True,
        module=False,
        warn=warn_only,
        url=None,
    )
    _spec_add(
        spec,
        "CONFIG_KALLSYMS_ALL",
        required_state=True,
        module=False,
        warn=warn_only,
        url=None,
    )
    _spec_add(
        spec,
        "CONFIG_DEBUG_BUGVERBOSE",
        required_state=True,
        module=False,
        warn=warn_only,
        url=None,
    )
    _spec_add(
        spec,
        "CONFIG_STACKPROTECTOR_STRONG",
        required_state=True,
        module=False,
        warn=warn_only,
        url=None,
    )

    # required by sys-fs/zfs-9999
    _spec_add(
        spec,
        "CONFIG_DEBUG_INFO_DWARF5",
        required_state=True,
        module=False,
        warn=warn_only,
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
    # ORC unwinder by default (kernel's x86_64 default; smaller, faster).
    # When sys-fs/zfs is configured with USE=debug, check_kernel_config_zfs_debug
    # (Layer 2) flips this to UNWINDER_FRAME_POINTER. The two are mutually
    # exclusive, so we assert ORC explicitly here and let Layer 2 override.
    _spec_add(
        spec,
        "CONFIG_UNWINDER_ORC",
        required_state=True,
        module=False,
        warn=warn_only,
        url=None,
    )
    _spec_add(
        spec,
        "CONFIG_UNWINDER_FRAME_POINTER",
        required_state=False,
        module=False,
        warn=warn_only,
        url=None,
    )

    # Debug-group symbol baseline.
    # Every CONFIG symbol referenced by any check_kernel_config_<group> function
    # gets an explicit baseline here matching that group's default-disabled state.
    # Layer 2 group functions override these when their --flag is passed.
    # Without this baseline, an existing .config carrying a previously-enabled
    # group's symbols would silently ride forward into the next build.
    #
    # Notes:
    #  - CONFIG_DEBUG_FS is intentionally NOT baselined off — it's referenced by
    #    --gcov and --fault-inject but is also broadly required elsewhere
    #    (eBPF, drm-debug, etc).
    #  - --netconsole defaults ON, so its symbols baseline ON below.
    _DEBUG_GROUP_BASELINE_OFF = (
        # --kasan
        "CONFIG_KASAN",
        "CONFIG_KASAN_INLINE",
        "CONFIG_KASAN_OUTLINE",
        "CONFIG_KASAN_STACK",
        "CONFIG_KASAN_VMALLOC",
        "CONFIG_PANIC_ON_OOPS",
        # --kmemleak
        "CONFIG_DEBUG_KMEMLEAK",
        # --slub-debug
        "CONFIG_SLUB_DEBUG",
        "CONFIG_SLUB_DEBUG_ON",
        # --lockdep family
        "CONFIG_LOCKDEP",
        "CONFIG_PROVE_LOCKING",
        "CONFIG_PROVE_RCU",
        "CONFIG_DEBUG_LOCK_ALLOC",
        "CONFIG_DEBUG_SPINLOCK",
        "CONFIG_DEBUG_MUTEXES",
        "CONFIG_DEBUG_RWSEMS",
        "CONFIG_DEBUG_ATOMIC_SLEEP",
        "CONFIG_DEBUG_WW_MUTEX_SLOWPATH",
        "CONFIG_LOCK_STAT",
        # --debug-objects
        "CONFIG_DEBUG_OBJECTS",
        "CONFIG_DEBUG_OBJECTS_FREE",
        "CONFIG_DEBUG_OBJECTS_TIMERS",
        # --gcov (DEBUG_FS deliberately omitted — see note above)
        "CONFIG_GCOV_KERNEL",
        "CONFIG_GCOV_FORMAT_AUTODETECT",
        # --zbtree-debug
        "CONFIG_KFENCE",
        # --ubsan
        "CONFIG_UBSAN",
        "CONFIG_UBSAN_BOOL",
        "CONFIG_UBSAN_BOUNDS",
        "CONFIG_UBSAN_ENUM",
        "CONFIG_UBSAN_SHIFT",
        # --kcsan
        "CONFIG_KCSAN",
        "CONFIG_KCSAN_ASSUME_PLAIN_WRITES_ATOMIC",
        # --watchdog
        "CONFIG_LOCKUP_DETECTOR",
        "CONFIG_SOFTLOCKUP_DETECTOR",
        "CONFIG_HARDLOCKUP_DETECTOR",
        "CONFIG_HARDLOCKUP_DETECTOR_PERF",
        "CONFIG_DETECT_HUNG_TASK",
        "CONFIG_WQ_WATCHDOG",
        # --fault-inject (DEBUG_FS deliberately omitted)
        "CONFIG_FAULT_INJECTION",
        "CONFIG_FAULT_INJECTION_DEBUG_FS",
        "CONFIG_FAILSLAB",
        "CONFIG_FAIL_PAGE_ALLOC",
        # --mem-init
        "CONFIG_INIT_ON_ALLOC_DEFAULT_ON",
        "CONFIG_INIT_ON_FREE_DEFAULT_ON",
        "CONFIG_PAGE_POISONING",
        # --dma-debug
        "CONFIG_DMA_API_DEBUG",
        "CONFIG_DMA_API_DEBUG_SG",
        # --data-struct-debug
        "CONFIG_DEBUG_LIST",
        "CONFIG_DEBUG_PLIST",
        "CONFIG_DEBUG_SG",
        "CONFIG_DEBUG_NOTIFIERS",
        "CONFIG_DEBUG_IRQFLAGS",
        # --bpftrace (BTF + syscall tracepoints; build-time cost only)
        "CONFIG_DEBUG_INFO_BTF",
        "CONFIG_DEBUG_INFO_BTF_MODULES",
        "CONFIG_FTRACE_SYSCALLS",
    )
    for _sym in _DEBUG_GROUP_BASELINE_OFF:
        _spec_add(
            spec,
            _sym,
            required_state=False,
            module=False,
            warn=warn_only,
            url=None,
        )

    # --netconsole defaults ON; baseline its symbols accordingly.
    # NETCONSOLE is module-loadable; NETCONSOLE_DYNAMIC is built-in (configfs
    # interface for runtime reconfiguration).
    _spec_add(
        spec,
        "CONFIG_NETCONSOLE",
        required_state=True,
        module=True,
        warn=warn_only,
        url=None,
    )
    _spec_add(
        spec,
        "CONFIG_NETCONSOLE_DYNAMIC",
        required_state=True,
        module=False,
        warn=warn_only,
        url=None,
    )

    # ──────────────────────────────────────────────────────────────────────
    # Performance defaults — picked to make `check-config-perf` pass with no
    # CLI flags. Each block is a coherent group; flip the corresponding
    # --harden / --ia32 / etc. flag to deviate.
    # ──────────────────────────────────────────────────────────────────────

    # CPU march — pick the most specific symbol we can detect, disable the
    # rest of the choice. Without this every kernel build would default to
    # CONFIG_GENERIC_CPU and we'd lose 5–15% on hot paths.
    _picked_march = _detect_cpu_march_symbol()
    eprint(f"production base: detected CPU march → {_picked_march}")
    for _march_sym in _X86_MARCH_CHOICE_SYMBOLS:
        _spec_add(
            spec, _march_sym,
            required_state=(_march_sym == _picked_march),
            module=False, warn=warn_only, url=None,
        )

    # CPU mitigations — OFF by default; --harden flips them on.
    for _sym in (
        "CONFIG_PAGE_TABLE_ISOLATION",
        "CONFIG_MITIGATION_RETPOLINE",
        "CONFIG_MITIGATION_RETHUNK",
        "CONFIG_MITIGATION_SLS",
        "CONFIG_MITIGATION_SRSO",
        "CONFIG_MITIGATION_GDS_FORCE",
        "CONFIG_RANDOMIZE_BASE",
        "CONFIG_RANDOMIZE_MEMORY",
        "CONFIG_CPU_MITIGATIONS",
    ):
        _spec_add(spec, _sym, required_state=False, module=False, warn=warn_only, url=None)
    # STACKPROTECTOR_STRONG: cheap, leave on regardless of --harden.
    _spec_add(spec, "CONFIG_STACKPROTECTOR_STRONG", required_state=True,
              module=False, warn=warn_only, url=None)

    # 32-bit emulation — OFF by default; --ia32 flips on.
    _spec_add(spec, "CONFIG_COMPAT", required_state=False, module=False,
              warn=warn_only, url=None)
    _spec_add(spec, "CONFIG_IA32_EMULATION", required_state=False, module=False,
              warn=warn_only, url=None)

    # Preemption — DYNAMIC with VOLUNTARY default for best interactive feel
    # without rebuild. Mutually exclusive with NONE/PREEMPT/RT.
    _spec_add(spec, "CONFIG_PREEMPT_DYNAMIC", required_state=True, module=False,
              warn=warn_only, url=None)
    _spec_add(spec, "CONFIG_PREEMPT_VOLUNTARY", required_state=True, module=False,
              warn=warn_only, url=None)
    _spec_add(spec, "CONFIG_PREEMPT_NONE", required_state=False, module=False,
              warn=warn_only, url=None)
    _spec_add(spec, "CONFIG_PREEMPT", required_state=False, module=False,
              warn=warn_only, url=None)
    _spec_add(spec, "CONFIG_PREEMPT_RT", required_state=False, module=False,
              warn=warn_only, url=None)

    # Timer — 1000Hz, NO_HZ_IDLE (tickless when idle).  NO_HZ_FULL is
    # specialist territory and explicitly off.
    _spec_add(spec, "CONFIG_HZ_1000", required_state=True, module=False,
              warn=warn_only, url=None)
    for _sym in ("CONFIG_HZ_100", "CONFIG_HZ_250", "CONFIG_HZ_300"):
        _spec_add(spec, _sym, required_state=False, module=False, warn=warn_only, url=None)
    _spec_add(spec, "CONFIG_NO_HZ_IDLE", required_state=True, module=False,
              warn=warn_only, url=None)
    _spec_add(spec, "CONFIG_NO_HZ_FULL", required_state=False, module=False,
              warn=warn_only, url=None)
    _spec_add(spec, "CONFIG_HIGH_RES_TIMERS", required_state=True, module=False,
              warn=warn_only, url=None)
    _spec_add(spec, "CONFIG_RCU_NOCB_CPU", required_state=True, module=False,
              warn=warn_only, url=None)
    # RCU_NOCB_CPU depends on (TREE_RCU) && (RCU_EXPERT || NO_HZ_FULL).
    # NO_HZ_FULL is off, so RCU_EXPERT must be on to make RCU_NOCB_CPU
    # reachable in the menu.
    _spec_add(spec, "CONFIG_RCU_EXPERT", required_state=True, module=False,
              warn=warn_only, url=None)

    # Scheduler.
    for _sym in (
        "CONFIG_SCHED_AUTOGROUP",
        "CONFIG_SCHED_MC",
        "CONFIG_SCHED_SMT",
        "CONFIG_SCHED_CLUSTER",
        "CONFIG_FAIR_GROUP_SCHED",
    ):
        _spec_add(spec, _sym, required_state=True, module=False, warn=warn_only, url=None)

    # CPU freq + idle.
    for _sym in (
        "CONFIG_X86_INTEL_PSTATE",
        "CONFIG_X86_AMD_PSTATE",
        "CONFIG_CPU_FREQ_DEFAULT_GOV_SCHEDUTIL",
        "CONFIG_CPU_IDLE_GOV_TEO",
    ):
        _spec_add(spec, _sym, required_state=True, module=False, warn=warn_only, url=None)
    _spec_add(spec, "CONFIG_CPU_IDLE_GOV_MENU", required_state=False, module=False,
              warn=warn_only, url=None)

    # Memory management.
    _spec_add(spec, "CONFIG_TRANSPARENT_HUGEPAGE", required_state=True,
              module=False, warn=warn_only, url=None)
    _spec_add(spec, "CONFIG_TRANSPARENT_HUGEPAGE_MADVISE", required_state=True,
              module=False, warn=warn_only, url=None)
    _spec_add(spec, "CONFIG_TRANSPARENT_HUGEPAGE_ALWAYS", required_state=False,
              module=False, warn=warn_only, url=None)
    _spec_add(spec, "CONFIG_COMPACTION", required_state=True, module=False,
              warn=warn_only, url=None)
    _spec_add(spec, "CONFIG_NUMA_BALANCING", required_state=True, module=False,
              warn=warn_only, url=None)
    _spec_add(spec, "CONFIG_ZSWAP", required_state=True, module=False,
              warn=warn_only, url=None)
    _spec_add(spec, "CONFIG_ZSWAP_DEFAULT_ON", required_state=True, module=False,
              warn=warn_only, url=None)
    # KSM — workstation default off (CPU cost vs RAM savings; only worth it
    # on VM hosts running many similar guests).
    _spec_add(spec, "CONFIG_KSM", required_state=False, module=False,
              warn=warn_only, url=None)

    # I/O.
    # BLK_WBT_MQ depends on BLK_WBT — assert the parent first.
    _spec_add(spec, "CONFIG_BLK_WBT", required_state=True, module=False,
              warn=warn_only, url=None)
    _spec_add(spec, "CONFIG_BLK_WBT_MQ", required_state=True, module=False,
              warn=warn_only, url=None)
    _spec_add(spec, "CONFIG_IOSCHED_BFQ", required_state=True, module=False,
              warn=warn_only, url=None)
    _spec_add(spec, "CONFIG_MQ_IOSCHED_KYBER", required_state=True, module=False,
              warn=warn_only, url=None)
    _spec_add(spec, "CONFIG_IO_URING", required_state=True, module=False,
              warn=warn_only, url=None)

    # Networking — BBR is built-in (no module) so it's always available; some
    # distros ship it as a module but built-in is one less rmod risk.
    _spec_add(spec, "CONFIG_NET_RX_BUSY_POLL", required_state=True, module=False,
              warn=warn_only, url=None)
    _spec_add(spec, "CONFIG_TCP_CONG_BBR", required_state=True, module=False,
              warn=warn_only, url=None)
    _spec_add(spec, "CONFIG_BPF_JIT", required_state=True, module=False,
              warn=warn_only, url=None)
    _spec_add(spec, "CONFIG_BPF_JIT_ALWAYS_ON", required_state=True, module=False,
              warn=warn_only, url=None)
    _spec_add(spec, "CONFIG_XDP_SOCKETS", required_state=True, module=False,
              warn=warn_only, url=None)

    # x86 features.
    _spec_add(spec, "CONFIG_X86_X2APIC", required_state=True, module=False,
              warn=warn_only, url=None)
    _spec_add(spec, "CONFIG_X86_FRED", required_state=True, module=False,
              warn=warn_only, url=None)

    ## not sure what this was for
    # verify_kernel_config_setting(
    #    path=path,
    #    define="CONFIG_CRYPTO_USER",
    #    required_state=True,
    #    module=True,
    #    warn=warn_only, fix=fix,
    #    url=None,
    # )

    _spec_add(
        spec,
        "CONFIG_DRM",
        required_state=True,
        module=True,
        warn=warn_only,
        url="https://wiki.gentoo.org/wiki/Nouveau",
    )

    _spec_add(
        spec,
        "CONFIG_DRM_FBDEV_EMULATION",
        required_state=True,
        module=False,
        warn=warn_only,
        url="https://wiki.gentoo.org/wiki/Nouveau",
    )
    _spec_add(
        spec,
        "CONFIG_DRM_AMDGPU",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_DRM_UDL",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_FIRMWARE_EDID",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_FB_VESA",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_MTRR_SANITIZER",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # ACPI
    _spec_add(
        spec,
        "CONFIG_ACPI_FPDT",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # ACPI
    _spec_add(
        spec,
        "CONFIG_ACPI_TAD",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # ACPI
    _spec_add(
        spec,
        "CONFIG_ACPI_PCI_SLOT",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # ACPI
    _spec_add(
        spec,
        "CONFIG_ACPI_SBS",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # ACPI
    _spec_add(
        spec,
        "CONFIG_ACPI_HED",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # ACPI
    _spec_add(
        spec,
        "CONFIG_ACPI_APEI",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # ACPI
    _spec_add(
        spec,
        "CONFIG_ACPI_DPTF",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # ACPI
    _spec_add(
        spec,
        "CONFIG_ACPI_CONFIGFS",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # ACPI
    _spec_add(
        spec,
        "CONFIG_ACPI_APEI_GHES",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # ACPI
    _spec_add(
        spec,
        "CONFIG_ACPI_APEI_PCIEAER",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # ACPI
    _spec_add(
        spec,
        "CONFIG_ACPI_NFIT",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # ACPI
    _spec_add(
        spec,
        "CONFIG_ACPI_PROCESSOR_AGGREGATOR",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # ACPI
    _spec_add(
        spec,
        "CONFIG_HIBERNATION",
        required_state=False,
        module=False,
        warn=warn_only,
        url="",
    )
    # cpu frequency
    _spec_add(
        spec,
        "CONFIG_CPU_FREQ_STAT",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # module versioning
    _spec_add(
        spec,
        "CONFIG_MODVERSIONS",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # block layer SG
    _spec_add(
        spec,
        "CONFIG_BLK_DEV_BSGLIB",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # ECC
    _spec_add(
        spec,
        "CONFIG_MEMORY_FAILURE",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # ECC
    _spec_add(
        spec,
        "CONFIG_MTD_NAND_ECC_SW_BCH",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # ECC
    _spec_add(
        spec,
        "CONFIG_RAS_CEC",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # mem
    _spec_add(
        spec,
        "CONFIG_PAGE_REPORTING",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # mem
    _spec_add(
        spec,
        "CONFIG_TRANSPARENT_HUGEPAGE",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # mem
    _spec_add(
        spec,
        "CONFIG_PER_VMA_LOCK",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # chipset
    _spec_add(
        spec,
        "CONFIG_LPC_ICH",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # chipset
    _spec_add(
        spec,
        "CONFIG_LPC_SCH",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # pcie
    _spec_add(
        spec,
        "CONFIG_PCIEAER",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # pcie
    _spec_add(
        spec,
        "CONFIG_PCIE_DPC",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # pcie
    _spec_add(
        spec,
        "CONFIG_PCI_IOV",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # old interface
    _spec_add(
        spec,
        "CONFIG_UEVENT_HELPER",
        required_state=False,
        module=False,
        warn=warn_only,
        url="",
    )
    # dmi
    _spec_add(
        spec,
        "CONFIG_DMI_SYSFS",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # mtd
    _spec_add(
        spec,
        "CONFIG_MTD",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # i386
    # I forget why... maybe virtualbox?
    _spec_add(
        spec,
        "CONFIG_IA32_EMULATION",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # usb speakers
    _spec_add(
        spec,
        "CONFIG_SND_USB_AUDIO",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # alsa required for the rest
    _spec_add(
        spec,
        "CONFIG_SND",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # alsa required for the rest
    _spec_add(
        spec,
        "CONFIG_SND_SOC",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # alsa
    _spec_add(
        spec,
        "CONFIG_SND_SOC_AMD_ACP",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # alsa
    _spec_add(
        spec,
        "CONFIG_SND_OSSEMUL",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # alsa
    _spec_add(
        spec,
        "CONFIG_SND_MIXER_OSS",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # alsa
    _spec_add(
        spec,
        "CONFIG_SND_PCM_OSS",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # alsa
    _spec_add(
        spec,
        "CONFIG_SND_INTEL8X0",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # alsa
    _spec_add(
        spec,
        "CONFIG_SND_INTEL8X0M",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # alsa
    _spec_add(
        spec,
        "CONFIG_SND_HDA_GENERIC",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # alsa audio
    _spec_add(
        spec,
        "CONFIG_SND_AC97_CODEC",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # alsa
    _spec_add(
        spec,
        "CONFIG_USB_GADGET",
        required_state=True,
        module=True,
        warn=warn_only,
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
    _spec_add(
        spec,
        "CONFIG_SND_SUPPORT_OLD_API",
        required_state=False,
        module=False,
        warn=warn_only,
        url="",
    )
    # alsa
    _spec_add(
        spec,
        "CONFIG_SOUNDWIRE",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # usb otg
    _spec_add(
        spec,
        "CONFIG_USB_OTG",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )

    _spec_add(
        spec,
        "CONFIG_DRM_NOUVEAU",
        required_state=True,
        module=True,
        warn=warn_only,
        url="https://wiki.gentoo.org/wiki/Nouveau",
    )
    _spec_add(
        spec,
        "CONFIG_VT_HW_CONSOLE_BINDING",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_VGA_SWITCHEROO",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )

    _spec_add(
        spec,
        "CONFIG_DRM_RADEON",
        required_state=True,
        module=True,
        warn=warn_only,
        url="https://wiki.gentoo.org/wiki/Nouveau",
    )

    _spec_add(
        spec,
        "CONFIG_BINFMT_MISC",
        required_state=True,
        module=True,
        warn=warn_only,
        url="https://pypi.org/project/fchroot",
    )

    _spec_add(
        spec,
        "CONFIG_HID_WACOM",
        required_state=True,
        module=True,
        warn=warn_only,
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

    _spec_add(
        spec,
        "CONFIG_NET_CORE",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )

    _spec_add(
        spec,
        "CONFIG_TUN",
        required_state=True,
        module=True,
        warn=warn_only,
        url="https://www.kernel.org/doc/html/latest/networking/tuntap.html",
    )

    _spec_add(
        spec,
        "CONFIG_VIRTIO_NET",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )

    _spec_add(
        spec,
        "CONFIG_APPLE_PROPERTIES",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_SPI",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_KEYBOARD_APPLESPI",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_MOUSE_APPLETOUCH",
        required_state=True,
        module=True,
        warn=warn_only,
        url="https://www.kernel.org/doc/html/v6.1-rc4/input/devices/appletouch.html",
    )
    _spec_add(
        spec,
        "CONFIG_BACKLIGHT_APPLE",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_HID_APPLE",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_HID_APPLEIR",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_USB_APPLEDISPLAY",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_APPLE_MFI_FASTCHARGE",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_APPLE_GMUX",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # for GPM
    _spec_add(
        spec,
        "CONFIG_INPUT_MOUSEDEV",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_ZRAM",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_ZRAM_MEMORY_TRACKING",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_BLK_DEV_FD",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_EARLY_PRINTK",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )

    _spec_add(
        spec,
        "CONFIG_NF_TABLES",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # sshuttle
    _spec_add(
        spec,
        "CONFIG_NF_NAT",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # sshuttle
    _spec_add(
        spec,
        "CONFIG_NETFILTER_ADVANCED",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # sshuttle — iptables framework (parent menu of IP_NF_*)
    _spec_add(
        spec,
        "CONFIG_IP_NF_IPTABLES",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # sshuttle — legacy iptables backend (gates IP_NF_NAT and IP_NF_MANGLE)
    _spec_add(
        spec,
        "CONFIG_IP_NF_IPTABLES_LEGACY",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # sshuttle — connection tracking (required by IP_NF_NAT)
    _spec_add(
        spec,
        "CONFIG_NF_CONNTRACK",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # sshuttle — xtables framework (selected =y by many built-in features; cannot be =m)
    _spec_add(
        spec,
        "CONFIG_NETFILTER_XTABLES",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # sshuttle — legacy xtables backend (gates IP_NF_IPTABLES_LEGACY)
    _spec_add(
        spec,
        "CONFIG_NETFILTER_XTABLES_LEGACY",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # sshuttle — gates the `if IP_NF_NAT` block that contains IP_NF_TARGET_REDIRECT
    _spec_add(
        spec,
        "CONFIG_IP_NF_NAT",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # sshuttle — required by NETFILTER_XT_TARGET_HL (IP_NF_MANGLE || IP6_NF_MANGLE || NFT_COMPAT)
    _spec_add(
        spec,
        "CONFIG_IP_NF_MANGLE",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # sshuttle
    _spec_add(
        spec,
        "CONFIG_IP_NF_MATCH_TTL",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # sshuttle
    _spec_add(
        spec,
        "CONFIG_IP_NF_TARGET_REDIRECT",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # sshuttle
    _spec_add(
        spec,
        "CONFIG_NETFILTER_XT_TARGET_HL",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # old outdated option
    _spec_add(
        spec,
        "CONFIG_NO_HZ",
        required_state=False,
        module=False,
        warn=warn_only,
        url="",
    )

    # speed
    _spec_add(
        spec,
        "CONFIG_PREEMPT_NONE",
        required_state=False,
        module=False,
        warn=warn_only,
        url="",
    )
    # speed
    _spec_add(
        spec,
        "CONFIG_PREEMPT_VOLUNTARY",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # speed
    _spec_add(
        spec,
        "CONFIG_PREEMPT",
        required_state=False,
        module=False,
        warn=warn_only,
        url="",
    )
    # new process accounting
    _spec_add(
        spec,
        "CONFIG_BSD_PROCESS_ACCT_V3",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # memory cgrroup
    _spec_add(
        spec,
        "CONFIG_MEMCG",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # cgroup debugging
    _spec_add(
        spec,
        "CONFIG_CGROUP_DEBUG",
        required_state=False,
        module=False,
        warn=warn_only,
        url="",
    )
    # cgroup
    _spec_add(
        spec,
        "CONFIG_CGROUP_FAVOR_DYNMODS",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    #
    _spec_add(
        spec,
        "CONFIG_CHECKPOINT_RESTORE",
        required_state=False,
        module=False,
        warn=warn_only,
        url="",
    )

    # required for CONFIG_X86_SGX below
    _spec_add(
        spec,
        "CONFIG_X86_X2APIC",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    #
    _spec_add(
        spec,
        "CONFIG_X86_SGX",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )

    # auto cgroups... might contradict PREEMPT_NONE
    _spec_add(
        spec,
        "CONFIG_SCHED_AUTOGROUP",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )

    # zswap
    _spec_add(
        spec,
        "CONFIG_ZSWAP",
        required_state=True,
        module=False,
        warn=warn_only,
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
    _spec_add(
        spec,
        "CONFIG_KSM",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # nvme
    _spec_add(
        spec,
        "CONFIG_BLK_DEV_NVME",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # nvme
    _spec_add(
        spec,
        "CONFIG_NVME_VERBOSE_ERRORS",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # nvme
    _spec_add(
        spec,
        "CONFIG_NVME_HWMON",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # nvme
    _spec_add(
        spec,
        "CONFIG_NVME_MULTIPATH",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # nvme
    _spec_add(
        spec,
        "CONFIG_NVME_TARGET",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    #
    _spec_add(
        spec,
        "CONFIG_X86_CPU_RESCTRL",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    #
    _spec_add(
        spec,
        "CONFIG_BCACHE",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    #
    _spec_add(
        spec,
        "CONFIG_THERMAL_STATISTICS",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # audio
    _spec_add(
        spec,
        "CONFIG_SND_SEQUENCER_OSS",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # audio
    _spec_add(
        spec,
        "CONFIG_SND_HDA_CODEC_HDMI",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # audio pc-speaker
    _spec_add(
        spec,
        "CONFIG_INPUT_PCSPKR",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # pcie pc-card reader
    _spec_add(
        spec,
        "CONFIG_MISC_RTSX_PCI",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_BPF_SYSCALL",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_NET_CLS_BPF",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_NET_ACT_BPF",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_BPF_EVENTS",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # kvm
    _spec_add(
        spec,
        "CONFIG_KVM_INTEL",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # kvm
    _spec_add(
        spec,
        "CONFIG_VHOST_NET",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )

    # mmc
    # required for CONFIG_MMC_BLOCK below
    _spec_add(
        spec,
        "CONFIG_MMC",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # mmc
    _spec_add(
        spec,
        "CONFIG_MMC_BLOCK",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )

    # FUSE
    _spec_add(
        spec,
        "CONFIG_FUSE_FS",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # vlan
    _spec_add(
        spec,
        "CONFIG_VLAN_8021Q",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # NUMA
    _spec_add(
        spec,
        "CONFIG_NUMA",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # udev
    _spec_add(
        spec,
        "CONFIG_DEVTMPFS",
        required_state=True,
        module=False,
        warn=warn_only,
        url="https://wiki.gentoo.org/wiki/Udev",
    )
    # wireguard
    _spec_add(
        spec,
        "CONFIG_WIREGUARD",
        required_state=True,
        module=True,
        warn=warn_only,
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
    _spec_add(
        spec,
        "CONFIG_USB_SERIAL",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_USB_SERIAL_PL2303",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_USB_SERIAL_CH341",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_USB_SERIAL_FTDI_SIO",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_USB_PEGASUS",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_USB_USBNET",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_USB_SERIAL_CYPRESS_M8",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_USB_ACM",
        required_state=True,
        module=True,
        warn=warn_only,
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
    _spec_add(
        spec,
        "CONFIG_BRIDGE",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_BLK_DEV_NBD",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_USB4",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    ## performance
    ## nope, zfs REQUIRES this
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
    _spec_add(
        spec,
        "CONFIG_DEBUG_STACK_USAGE",
        required_state=False,
        module=False,
        warn=warn_only,
        url="",
    )
    # performance
    _spec_add(
        spec,
        "CONFIG_DEBUG_WX",
        required_state=False,
        module=False,
        warn=warn_only,
        url="",
    )
    # performance
    _spec_add(
        spec,
        "CONFIG_DEBUG_KERNEL",
        required_state=False,
        module=False,
        warn=warn_only,
        url="",
    )
    # performance
    _spec_add(
        spec,
        "CONFIG_DEBUG_MISC",
        required_state=False,
        module=False,
        warn=warn_only,
        url="",
    )

    # performance
    _spec_add(
        spec,
        "CONFIG_DEBUG_MEMORY_INIT",
        required_state=False,
        module=False,
        warn=warn_only,
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
    _spec_add(
        spec,
        "CONFIG_RCU_TRACE",
        required_state=False,
        module=False,
        warn=warn_only,
        url="",
    )
    # performance
    _spec_add(
        spec,
        "CONFIG_SCHEDSTATS",
        required_state=False,
        module=False,
        warn=warn_only,
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
    _spec_add(
        spec,
        "CONFIG_TRANSPARENT_HUGEPAGE_MADVISE",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # performance
    _spec_add(
        spec,
        "CONFIG_CPU_FREQ_DEFAULT_GOV_USERSPACE",
        required_state=False,
        module=False,
        warn=warn_only,
        url="",
    )
    # performance: SCHEDUTIL is the chosen default (set elsewhere); explicitly
    # disable the other choice members so olddefconfig doesn't pick one.
    _spec_add(
        spec,
        "CONFIG_CPU_FREQ_DEFAULT_GOV_PERFORMANCE",
        required_state=False,
        module=False,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_CPU_FREQ_DEFAULT_GOV_POWERSAVE",
        required_state=False,
        module=False,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_CPU_FREQ_DEFAULT_GOV_ONDEMAND",
        required_state=False,
        module=False,
        warn=warn_only,
        url="",
    )
    _spec_add(
        spec,
        "CONFIG_CPU_FREQ_DEFAULT_GOV_CONSERVATIVE",
        required_state=False,
        module=False,
        warn=warn_only,
        url="",
    )
    # performance
    _spec_add(
        spec,
        "CONFIG_X86_INTEL_PSTATE",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # performance
    _spec_add(
        spec,
        "CONFIG_X86_AMD_PSTATE",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # performance
    _spec_add(
        spec,
        "CONFIG_SECURITY_SELINUX",
        required_state=False,
        module=False,
        warn=warn_only,
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
    _spec_add(
        spec,
        "CONFIG_SCSI_MPT3SAS",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # security, like pledge
    _spec_add(
        spec,
        "CONFIG_SECURITY_LANDLOCK",
        required_state=True,
        module=False,
        warn=warn_only,
        url="",
    )
    # 10G Ethernet
    _spec_add(
        spec,
        "CONFIG_NET_VENDOR_AQUANTIA",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # 10G Ethernet
    _spec_add(
        spec,
        "CONFIG_AQTION",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )
    # zbook g5 sd card reader
    _spec_add(
        spec,
        "CONFIG_MMC_REALTEK_PCI",
        required_state=True,
        module=True,
        warn=warn_only,
        url="",
    )

    # --- layer 2: debug group overrides (last-writer-wins over production base) ---
    # Auto-enable zfs_debug if sys-fs/zfs currently has USE=debug configured,
    # regardless of whether --zfs-debug was passed on the command line. The
    # debug-build of ZFS requires CONFIG_FRAME_POINTER, which is selected by
    # the FP unwinder.
    zfs_debug = zfs_debug or _zfs_debug_use_enabled()
    eprint(f"layer 2: zfs_debug={zfs_debug}")
    check_kernel_config_kasan(spec=spec, enable=kasan)
    check_kernel_config_kmemleak(spec=spec, enable=kmemleak)
    check_kernel_config_slub_debug(spec=spec, enable=slub_debug)
    check_kernel_config_lockdep(spec=spec, enable=lockdep)
    check_kernel_config_debug_objects(spec=spec, enable=debug_objects)
    check_kernel_config_gcov(spec=spec, enable=gcov)
    check_kernel_config_zbtree_debug(spec=spec, enable=zbtree_debug)
    check_kernel_config_zfs_debug(spec=spec, enable=zfs_debug)
    check_kernel_config_ubsan(spec=spec, enable=ubsan)
    check_kernel_config_kcsan(spec=spec, enable=kcsan)
    check_kernel_config_watchdog(spec=spec, enable=watchdog)
    check_kernel_config_fault_inject(spec=spec, enable=fault_inject)
    check_kernel_config_mem_init(spec=spec, enable=mem_init)
    check_kernel_config_dma_debug(spec=spec, enable=dma_debug)
    check_kernel_config_data_struct_debug(spec=spec, enable=data_struct_debug)
    check_kernel_config_netconsole(spec=spec, enable=netconsole)
    check_kernel_config_harden(spec=spec, enable=harden)
    check_kernel_config_ia32(spec=spec, enable=ia32)
    check_kernel_config_bpftrace(spec=spec, enable=bpftrace)

    # --- layer 3: compat overrides (win over everything) ---
    if zfs_compat_lockdep:
        check_kernel_config_zfs_compat_lockdep(spec=spec)
    if nvidia_compat:
        check_kernel_config_nvidia_compat(spec=spec)

    # --- integer config values (last-writer-wins, same layer logic) ---
    _int_spec_add(
        ispec,
        "CONFIG_STACK_DEPOT_MAX_ENTRIES",
        24,
    )
    _int_spec_add(
        ispec,
        "CONFIG_HZ",
        1000,
    )

    # --- pre-filter spec against the live kernel's Kconfig (when relevant) ---
    if path.resolve() == Path("/usr/src/linux/.config").resolve():
        spec = _filter_spec_for_kernel(spec, Path("/usr/src/linux"))

    # --- apply merged spec — each symbol written exactly once ---
    _spec_apply(
        spec=spec,
        path=path,
        fix=fix,
    )
    _int_spec_apply(
        ispec=ispec,
        path=path,
        fix=fix,
    )
    if _tmp_config is not None:
        Path(_tmp_config.name).unlink(missing_ok=True)

    # --- propagate Kconfig selects and validate (only when fixing the live tree) ---
    if fix and path.resolve() == Path("/usr/src/linux/.config").resolve():
        _resolve_and_verify_config(spec=spec, ispec=ispec, path=path)


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
            hs.Command("ln")(
                "-s",
                _kernel_config,
                dot_config,
            )


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
    _gcc_version_string_command = hs.Command("gcc")
    _gcc_version_string = _gcc_version_string_command("--version").splitlines()[0]
    icp(_gcc_version_string)
    _current_gcc_major_version = _gcc_version_string.split(" ")[-2][:2]
    icp(_current_gcc_major_version)
    # assert _current_gcc_major_version == "14"
    _config_gcc_version = (
        hs.Command("grep")(["CONFIG_GCC_VERSION", "/usr/src/linux/.config"])
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
        hs.Command("make")("clean")


def gcc_check_old():
    test_path = Path("/usr/src/linux/init/.init_task.o.cmd")
    if test_path.exists():
        icp(
            "found previously compiled kernel tree, checking is the current gcc version was used"
        )
        gcc_version = hs.Command("gcc-config")("-l")
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
            hs.Command("make")("clean")


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


def _active_debug_flags(
    *,
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
    netconsole: bool,
    harden: bool,
    ia32: bool,
    bpftrace: bool,
) -> list[str]:
    flags = [
        ("kasan", kasan),
        ("kmemleak", kmemleak),
        ("slub-debug", slub_debug),
        ("lockdep", lockdep),
        ("debug-objects", debug_objects),
        ("gcov", gcov),
        ("zbtree-debug", zbtree_debug),
        ("zfs-debug", zfs_debug),
        ("ubsan", ubsan),
        ("kcsan", kcsan),
        ("watchdog", watchdog),
        ("fault-inject", fault_inject),
        ("mem-init", mem_init),
        ("dma-debug", dma_debug),
        ("data-struct-debug", data_struct_debug),
        ("netconsole", netconsole),
        ("harden", harden),
        ("ia32", ia32),
        ("bpftrace", bpftrace),
    ]
    return [name for name, enabled in flags if enabled]


KERNEL_FLAGS_DIR = Path("/boot/compile-kernel-flags")


def _write_kernel_flags(kver: str, flags: list[str]) -> None:
    """Write active debug flags for kver to /boot/compile-kernel-flags/{kver}.
    Empty flags list writes an empty file (clean kernel — no flags).
    """
    KERNEL_FLAGS_DIR.mkdir(parents=True, exist_ok=True)
    flag_file = KERNEL_FLAGS_DIR / kver
    flag_file.write_text(" ".join(flags) + "\n" if flags else "\n", encoding="utf8")
    eprint(f"wrote kernel flags for {kver}: {flags}")


def _read_kernel_flags(kver: str) -> list[str] | None:
    """Return flags list for kver, or None if no record exists."""
    flag_file = KERNEL_FLAGS_DIR / kver
    if not flag_file.exists():
        return None
    content = flag_file.read_text(encoding="utf8").strip()
    return content.split() if content else []


_BOOT_FILE_PREFIXES = ("vmlinuz", "System.map", "config", "initramfs")
_SNAPSHOT_MANIFEST_DIR = Path("/boot/compile-kernel-snapshots")


def _snapshot_existing_kernel_files(kver: str) -> None:
    """Rename all existing /boot files for kver to {basename}.{mtime_ts}.

    Captures vmlinuz-{kver}, System.map-{kver}, config-{kver}, and
    initramfs-{kver}.img — including any existing .old variants — so that
    every previous compile is preserved with a unique timestamp suffix
    derived from each file's mtime. After snapshotting, /boot has no
    {basename}-{kver} or {basename}-{kver}.old entries left, so subsequent
    `make install` and genkernel will install fresh files cleanly without
    triggering installkernel's default rename-to-.old behaviour.

    Writes a manifest at /boot/compile-kernel-snapshots/{vmlinuz-snapshot-name}
    that records the snapshotted name of every companion file (initramfs,
    System.map, config) for that vmlinuz, so _postprocess_grub_cfg can
    rewrite GRUB initrd lines correctly. Without this manifest, GRUB's
    autogenerated `initrd /boot/initramfs-{kver}.img` line for a
    snapshotted vmlinuz would point at the NEW initramfs (built later by
    genkernel), causing kernel/initramfs version skew at boot.
    """
    boot = Path("/boot")
    if not boot.is_dir():
        return

    candidates: list[Path] = []
    for prefix in _BOOT_FILE_PREFIXES:
        if prefix == "initramfs":
            base = f"{prefix}-{kver}.img"
        else:
            base = f"{prefix}-{kver}"
        for name in (base, f"{base}.old"):
            p = boot / name
            if p.exists():
                candidates.append(p)

    # Track the snapshotted name of each kind so we can write the manifest.
    snapshotted: dict[str, str] = {}  # prefix -> snapshotted basename

    for src in candidates:
        ts = int(src.stat().st_mtime)
        # strip a trailing ".old" so the timestamped name is canonical
        base_name = src.name[:-4] if src.name.endswith(".old") else src.name
        target = boot / f"{base_name}.{ts}"
        # rebuilds within the same second — bump until unique
        while target.exists():
            ts += 1
            target = boot / f"{base_name}.{ts}"
        eprint(f"snapshot: {src} -> {target}")
        src.rename(target)
        # record by prefix (vmlinuz, initramfs, System.map, config)
        for prefix in _BOOT_FILE_PREFIXES:
            if base_name.startswith(prefix + "-") or base_name.startswith(prefix):
                # only the FIRST matching prefix wins (vmlinuz, initramfs, etc. are
                # all distinct prefixes; but base_name like "vmlinuz-..." matches
                # only "vmlinuz" not "config")
                snapshotted[prefix] = target.name
                break
        # Re-point any /boot symlinks (e.g. genkernel --symlink's /boot/kernel,
        # /boot/initramfs) that pointed at the just-renamed file. Without this
        # fix the symlinks dangle, breaking later boot checks and bootability
        # of the snapshotted kernel.
        for entry in boot.iterdir():
            if not entry.is_symlink():
                continue
            link_str = os.readlink(entry)
            link_basename = Path(link_str).name
            if link_basename != src.name:
                continue
            entry.unlink()
            # preserve absolute vs relative form of the original link
            new_target = (
                target.as_posix() if link_str.startswith("/") else target.name
            )
            entry.symlink_to(new_target)
            eprint(f"snapshot: repointed symlink {entry} -> {new_target}")

    # Write the manifest keyed by the snapshotted vmlinuz basename.
    # Schema: one line per companion, "prefix=snapshotted_basename".
    if "vmlinuz" in snapshotted:
        # Fill in any companion that wasn't snapshotted in THIS call by finding
        # the most recent prior snapshot for the same kver. This handles the
        # very common case where vmlinuz and initramfs were renamed in
        # different compile-and-install runs (initramfs gets snapshotted by
        # the second `make install` while vmlinuz gets snapshotted by the
        # third — they end up with different timestamp suffixes).
        for prefix in _BOOT_FILE_PREFIXES:
            if prefix in snapshotted:
                continue
            if prefix == "initramfs":
                base = f"{prefix}-{kver}.img"
            else:
                base = f"{prefix}-{kver}"
            # 1) canonical name still present (rare: this would mean we didn't
            #    snapshot it for some reason — e.g. it was newly written between
            #    candidate-collection and now). Prefer it if so.
            canonical = boot / base
            if canonical.exists():
                snapshotted[prefix] = canonical.name
                continue
            # 2) most recent prior snapshot {base}.{ts} that exists on disk
            siblings = sorted(
                boot.glob(f"{base}.[0-9]*"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if siblings:
                snapshotted[prefix] = siblings[0].name

        _SNAPSHOT_MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
        manifest = _SNAPSHOT_MANIFEST_DIR / snapshotted["vmlinuz"]
        manifest.write_text(
            "\n".join(f"{k}={v}" for k, v in sorted(snapshotted.items())) + "\n",
            encoding="utf8",
        )
        eprint(f"snapshot manifest: {manifest}")


def _read_snapshot_manifest(vmlinuz_basename: str) -> dict[str, str] | None:
    """Return manifest dict for a snapshotted vmlinuz, or None if no manifest.
    The manifest pairs each vmlinuz snapshot with its companion files so that
    GRUB postprocess can rewrite initrd lines to the correct snapshotted
    initramfs (and not the freshly-built one with the canonical name).

    For legacy manifests (pre-mtime-pairing fix) that lack the initramfs
    entry, transparently fill it in by picking the snapshotted initramfs
    with mtime closest to (but not exceeding) the vmlinuz snapshot mtime.
    The recovered manifest is rewritten to disk so the fix is sticky.
    """
    manifest = _SNAPSHOT_MANIFEST_DIR / vmlinuz_basename
    if not manifest.exists():
        return None
    out: dict[str, str] = {}
    for line in manifest.read_text(encoding="utf8").splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k] = v

    # Legacy manifest recovery: missing initramfs → guess by mtime pairing.
    if "vmlinuz" in out and "initramfs" not in out:
        boot = Path("/boot")
        vmlinuz_path = boot / out["vmlinuz"]
        if vmlinuz_path.exists():
            kver = _kver_from_vmlinuz(out["vmlinuz"])
            target_mtime = vmlinuz_path.stat().st_mtime
            # collect every initramfs snapshot for this kver that's older than
            # or close to the vmlinuz mtime (within 1 day either way), pick
            # the closest match. If none qualify, use the canonical initramfs
            # if it exists.
            candidates: list[tuple[float, Path]] = []
            for cand in boot.glob(f"initramfs-{kver}.img.[0-9]*"):
                if not cand.is_file():
                    continue
                candidates.append((abs(cand.stat().st_mtime - target_mtime), cand))
            canonical = boot / f"initramfs-{kver}.img"
            if canonical.exists():
                candidates.append(
                    (abs(canonical.stat().st_mtime - target_mtime), canonical)
                )
            if candidates:
                candidates.sort(key=lambda t: t[0])
                out["initramfs"] = candidates[0][1].name
                # persist the repair
                manifest.write_text(
                    "\n".join(f"{k}={v}" for k, v in sorted(out.items())) + "\n",
                    encoding="utf8",
                )
                eprint(
                    f"snapshot manifest repaired: {manifest.name} initramfs={out['initramfs']}"
                )
    return out


def _snapshot_for_current_source() -> None:
    """Read kver from /usr/src/linux and snapshot existing /boot files for it.
    No-op if kernel.release is not yet generated (kernel not configured).
    """
    kver_file = Path("/usr/src/linux/include/config/kernel.release")
    if not kver_file.exists():
        icp("snapshot skipped: include/config/kernel.release not present")
        return
    kver = kver_file.read_text(encoding="utf8").strip()
    _snapshot_existing_kernel_files(kver)


def _kver_from_vmlinuz(vmlinuz_path: str) -> str:
    """Extract kver from a vmlinuz path like /boot/vmlinuz-6.19.6-gentoo-x86_64."""
    name = Path(vmlinuz_path).name
    # strip vmlinuz- prefix
    if name.startswith("vmlinuz-"):
        name = name[len("vmlinuz-"):]
    # strip trailing .{integer_ts} (snapshot suffix from _snapshot_existing_kernel_files)
    # so e.g. vmlinuz-6.19.6-gentoo-x86_64.1773443816 maps to 6.19.6-gentoo-x86_64
    import re as _re
    name = _re.sub(r"\.\d{9,}$", "", name)
    # also strip legacy .old suffix for pre-snapshot kernels
    if name.endswith(".old"):
        name = name[:-4]
    return name


def _postprocess_grub_cfg(cfg_path: Path) -> None:
    """Rewrite menuentry titles AND initrd lines in grub.cfg.

    Two rewrites per menuentry:

    1. Title: extract kver from the linux line, look up its flags file, strip
       any existing [...] bracket from the title, append the correct one (or
       nothing for clean kernels).

    2. Initrd: if the linux line points at a SNAPSHOTTED vmlinuz (e.g.
       /boot/vmlinuz-{kver}.{ts}), replace any
       /boot/initramfs-{kver}.img reference on the initrd line with the
       SNAPSHOTTED initramfs from the manifest written by
       _snapshot_existing_kernel_files. Without this rewrite, GRUB pairs
       the snapshotted kernel with the freshly-built initramfs, causing
       /lib/modules/{uname -r} mismatch and an early-init hang at boot.
       Microcode initrds (intel-uc.img, amd-uc.img) are kver-agnostic and
       left alone.

    Operates on cfg_path in place.
    """
    import re
    text = cfg_path.read_text(encoding="utf8")
    lines = text.splitlines(keepends=True)
    result: list[str] = []

    # Pass 1: build vmlinuz → flags label mapping
    flags_map: dict[str, str] = {}  # vmlinuz path → "[flag flag]" or "" or None
    for line in lines:
        m = re.match(r'\s+linux\s+(\S+)', line)
        if m:
            vmlinuz = m.group(1)
            kver = _kver_from_vmlinuz(vmlinuz)
            flags = _read_kernel_flags(kver)
            if flags is None:
                flags_map[vmlinuz] = None  # type: ignore[assignment]
            elif flags:
                flags_map[vmlinuz] = "[" + " ".join(flags) + "]"
            else:
                flags_map[vmlinuz] = ""

    # Helper: detect a snapshotted vmlinuz path and look up its companions.
    snapshot_re = re.compile(r"\.\d{9,}$")

    def snapshot_initramfs_for(vmlinuz_path: str) -> str | None:
        """If vmlinuz_path is a snapshot, return the matching snapshotted
        initramfs basename (or None if no manifest / not a snapshot).
        """
        basename = Path(vmlinuz_path).name
        if not snapshot_re.search(basename):
            return None  # not a snapshot — leave initrd as-is
        manifest = _read_snapshot_manifest(basename)
        if manifest is None:
            return None
        return manifest.get("initramfs")

    # Pass 2: rewrite titles + initrd lines.
    i = 0
    while i < len(lines):
        line = lines[i]

        # menuentry / submenu title rewrite
        if re.match(r"\s*(menuentry|submenu)\s+['\"]", line):
            vmlinuz = None
            depth = 0
            for j in range(i, min(i + 60, len(lines))):
                lj = lines[j]
                depth += lj.count("{") - lj.count("}")
                m = re.match(r"\s+linux\s+(\S+)", lj)
                if m:
                    vmlinuz = m.group(1)
                    break
                if depth < 0:
                    break

            if vmlinuz and vmlinuz in flags_map and flags_map[vmlinuz] is not None:
                label = flags_map[vmlinuz]
                def replace_title(m2: re.Match) -> str:
                    q = m2.group(1)
                    title = m2.group(2)
                    title = re.sub(r"\s*\[.*?\]", "", title).strip()
                    if label:
                        title = title + " " + label
                    return q + title + q
                line = re.sub(r"(['\"])(.+?)\1", replace_title, line, count=1)
            result.append(line)
            i += 1
            continue

        # initrd line rewrite — only if the corresponding linux line in the
        # SAME menuentry block points at a snapshotted vmlinuz
        m_initrd = re.match(r"(\s+initrd\s+)(.+?)(\s*)$", line)
        if m_initrd:
            # Find the linux line for this block by scanning backwards
            vmlinuz = None
            for k in range(i - 1, max(-1, i - 60), -1):
                lk = lines[k]
                if re.match(r"\s*(menuentry|submenu)\s+['\"]", lk):
                    break
                m_lx = re.match(r"\s+linux\s+(\S+)", lk)
                if m_lx:
                    vmlinuz = m_lx.group(1)
                    break

            if vmlinuz:
                snap_initramfs = snapshot_initramfs_for(vmlinuz)
                if snap_initramfs:
                    kver = _kver_from_vmlinuz(vmlinuz)
                    canonical = f"/boot/initramfs-{kver}.img"
                    snap_path = f"/boot/{snap_initramfs}"
                    body = m_initrd.group(2)
                    parts = body.split()
                    # Replace canonical reference with snapshot path.
                    new_parts = [
                        snap_path if p == canonical else p
                        for p in parts
                    ]
                    # Append snapshot path if no initramfs reference exists.
                    # (grub-mkconfig sometimes emits only microcode initrds
                    # for snapshotted kernels because the canonical
                    # /boot/initramfs-{kver}.img is the NEW build, not the
                    # snapshot's matching initramfs.)
                    has_initramfs_ref = any(
                        Path(p).name.startswith(f"initramfs-{kver}")
                        for p in new_parts
                    )
                    if not has_initramfs_ref:
                        new_parts.append(snap_path)
                    if new_parts != parts:
                        line = (
                            m_initrd.group(1)
                            + " ".join(new_parts)
                            + m_initrd.group(3)
                        )
                        eprint(f"postprocess: rewrote initrd for {Path(vmlinuz).name} → {snap_initramfs}")
        result.append(line)
        i += 1

    cfg_path.write_text("".join(result), encoding="utf8")
    eprint("postprocessed grub.cfg with per-kernel flag labels and snapshot initrds")


def _set_grub_distributor() -> None:
    """Set GRUB_DISTRIBUTOR to plain 'Gentoo' — flags are handled per-kernel
    by _postprocess_grub_cfg after grub-mkconfig runs."""
    grub_defaults = Path("/etc/default/grub")
    if not grub_defaults.exists():
        return
    file_lines = grub_defaults.read_text(encoding="utf8").splitlines()
    new_lines: list[str] = []
    found = False
    for line in file_lines:
        stripped = line.strip()
        if stripped.startswith("GRUB_DISTRIBUTOR=") and not stripped.startswith("#"):
            found = True
            continue
        new_lines.append(line)
    if found:
        new_lines.append('GRUB_DISTRIBUTOR="Gentoo"')
    else:
        new_lines.append("")
        new_lines.append('GRUB_DISTRIBUTOR="Gentoo"')
    grub_defaults.write_text("\n".join(new_lines) + "\n", encoding="utf8")


def set_grub_font(size: int = 12) -> None:
    """Generate a compact GRUB font at the given pixel size and configure GRUB to use it.

    Uses grub-mkfont to convert the first available monospace TTF on the system
    to a .pf2 bitmap font, then sets GRUB_FONT in /etc/default/grub.
    At 12px on 1080p the menu fits ~160 chars wide vs ~96 at the default 16px.

    Args:
        size: font size in pixels (default 12; stock GRUB unicode.pf2 is 16).
    """
    import glob as _glob

    # Candidate monospace TTFs in preference order
    candidates = [
        "/usr/share/fonts/liberation/LiberationMono-Regular.ttf",
        "/usr/share/fonts/liberation-fonts/LiberationMono-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/dejavu-sans-mono/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    ]
    # Also try any glob matches for common patterns
    candidates += _glob.glob("/usr/share/fonts/**/LiberationMono-Regular.ttf", recursive=True)
    candidates += _glob.glob("/usr/share/fonts/**/DejaVuSansMono.ttf", recursive=True)

    ttf_path: Path | None = None
    for candidate in candidates:
        p = Path(candidate)
        if p.exists():
            ttf_path = p
            break

    if ttf_path is None:
        raise FileNotFoundError(
            "No suitable TTF found for grub-mkfont. "
            "Install media-fonts/liberation-fonts or media-fonts/dejavu."
        )

    out_dir = Path("/boot/grub/fonts")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_pf2 = out_dir / f"compile-kernel-{size}.pf2"

    eprint(f"generating {out_pf2} from {ttf_path} at {size}px")
    import shutil as _shutil
    if _shutil.which("grub-mkfont") is None:
        raise FileNotFoundError(
            "grub-mkfont not found in PATH.\n"
            "On Gentoo this is provided by sys-boot/grub built with USE=truetype.\n"
            "Fix: add 'sys-boot/grub truetype' to /etc/portage/package.use, then\n"
            "     emerge -1 sys-boot/grub"
        )
    hs.Command("grub-mkfont")(
        "--size", str(size),
        "--output", str(out_pf2),
        str(ttf_path),
    )

    # Write GRUB_FONT into /etc/default/grub
    grub_defaults = Path("/etc/default/grub")
    if grub_defaults.exists():
        file_lines = grub_defaults.read_text(encoding="utf8").splitlines()
        new_lines = [l for l in file_lines
                     if not (l.strip().startswith("GRUB_FONT=") and not l.strip().startswith("#"))]
        new_lines.append(f'GRUB_FONT="{out_pf2}"')
        grub_defaults.write_text("\n".join(new_lines) + "\n", encoding="utf8")
        eprint(f"GRUB_FONT set to {out_pf2}")

    icp("run grub-mkconfig to apply the new font")


def install_compiled_kernel(
    kasan: bool = False,
    kmemleak: bool = False,
    slub_debug: bool = False,
    lockdep: bool = False,
    debug_objects: bool = False,
    gcov: bool = False,
    zbtree_debug: bool = False,
    zfs_debug: bool = False,
    ubsan: bool = False,
    kcsan: bool = False,
    watchdog: bool = False,
    fault_inject: bool = False,
    mem_init: bool = False,
    dma_debug: bool = False,
    data_struct_debug: bool = False,
    netconsole: bool = True,
    harden: bool = False,
    ia32: bool = False,
    bpftrace: bool = False,
):
    _snapshot_for_current_source()
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
    _kver = Path("/usr/src/linux/include/config/kernel.release").read_text(encoding="utf8").strip()
    _write_kernel_flags(
        _kver,
        _active_debug_flags(
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
            netconsole=netconsole,
            harden=harden,
            ia32=ia32,
            bpftrace=bpftrace,
        ),
    )
    _set_grub_distributor()
    hs.Command("grub-mkconfig")("-o", "/boot/grub/grub.cfg")
    _postprocess_grub_cfg(Path("/boot/grub/grub.cfg"))


def configure_kernel(
    fix: bool,
    warn_only: bool,
    interactive: bool,
    kasan: bool = False,
    kmemleak: bool = False,
    slub_debug: bool = False,
    lockdep: bool = False,
    debug_objects: bool = False,
    gcov: bool = False,
    zbtree_debug: bool = False,
    zfs_debug: bool = False,
    ubsan: bool = False,
    kcsan: bool = False,
    watchdog: bool = False,
    fault_inject: bool = False,
    mem_init: bool = False,
    dma_debug: bool = False,
    data_struct_debug: bool = False,
    netconsole: bool = True,
    harden: bool = False,
    ia32: bool = False,
    bpftrace: bool = False,
    zfs_compat_lockdep: bool = False,
    nvidia_compat: bool = False,
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
        netconsole=netconsole,
        harden=harden,
        ia32=ia32,
        bpftrace=bpftrace,
        zfs_compat_lockdep=zfs_compat_lockdep,
        nvidia_compat=nvidia_compat,
    )  # must be done after nconfig


def compile_and_install_kernel(
    *,
    configure: bool,
    force: bool,
    fix: bool,
    warn_only: bool,
    no_check_boot: bool,
    symlink_config: bool,
    pre_module_rebuild: bool,
    kasan: bool = False,
    kmemleak: bool = False,
    slub_debug: bool = False,
    lockdep: bool = False,
    debug_objects: bool = False,
    gcov: bool = False,
    zbtree_debug: bool = False,
    zfs_debug: bool = False,
    ubsan: bool = False,
    kcsan: bool = False,
    watchdog: bool = False,
    fault_inject: bool = False,
    mem_init: bool = False,
    dma_debug: bool = False,
    data_struct_debug: bool = False,
    netconsole: bool = True,
    harden: bool = False,
    ia32: bool = False,
    bpftrace: bool = False,
    zfs_compat_lockdep: bool = False,
    nvidia_compat: bool = False,
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
            netconsole=netconsole,
            harden=harden,
            ia32=ia32,
            bpftrace=bpftrace,
            zfs_compat_lockdep=zfs_compat_lockdep,
            nvidia_compat=nvidia_compat,
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
        netconsole=netconsole,
        harden=harden,
        ia32=ia32,
        bpftrace=bpftrace,
        zfs_compat_lockdep=zfs_compat_lockdep,
        nvidia_compat=nvidia_compat,
    )
    # handle a downgrade from -9999 before genkernel calls @module-rebuild
    icp("attempting to upgrade zfs")
    try:
        hs.Command("emerge")(
            "sys-fs/zfs",
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
            "NOTE: kernel is unconfigured, skipping `emerge sys-fs/zfs` before kernel compile"
        )

    if not unconfigured_kernel:
        if pre_module_rebuild:
            icp("attempting emerge @module-rebuild")
            try:
                hs.Command("emerge")(
                    "@module-rebuild",
                    _out=sys.stdout,
                    _err=sys.stderr,
                )
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
            kasan=kasan,
            kmemleak=kmemleak,
            slub_debug=slub_debug,
            lockdep=lockdep,
            debug_objects=debug_objects,
        )

    check_kernel_config(
        path=Path("/usr/src/linux/.config"),
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
        ubsan=ubsan,
        kcsan=kcsan,
        watchdog=watchdog,
        fault_inject=fault_inject,
        mem_init=mem_init,
        dma_debug=dma_debug,
        data_struct_debug=data_struct_debug,
        netconsole=netconsole,
        harden=harden,
        ia32=ia32,
        bpftrace=bpftrace,
        zfs_compat_lockdep=zfs_compat_lockdep,
        nvidia_compat=nvidia_compat,
    )  # must be done after nconfig
    _snapshot_for_current_source()
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
    genkernel_command.bake("--callback=/usr/bin/emerge zfs @module-rebuild")
    # --callback="/usr/bin/emerge zfs sci-libs/linux-gpib sci-libs/linux-gpib-modules @module-rebuild"
    # --zfs
    icp(genkernel_command)
    genkernel_command(_fg=True)

    hs.Command("rc-update")(
        "add",
        "zfs-import",
        "boot",
    )
    hs.Command("rc-update")(
        "add",
        "zfs-share",
        "default",
    )
    hs.Command("rc-update")(
        "add",
        "zfs-zed",
        "default",
    )

    if Path("/boot/grub").is_dir():
        _kver = Path("/usr/src/linux/include/config/kernel.release").read_text(encoding="utf8").strip()
        _write_kernel_flags(
            _kver,
            _active_debug_flags(
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
                netconsole=netconsole,
                harden=harden,
                ia32=ia32,
                bpftrace=bpftrace,
            ),
        )
        _set_grub_distributor()
        hs.Command("grub-mkconfig")("-o", "/boot/grub/grub.cfg")
        _postprocess_grub_cfg(Path("/boot/grub/grub.cfg"))

    hs.Command("emerge")(
        "sys-kernel/linux-firmware",
        "-u",
        _out=sys.stdout,
        _err=sys.stderr,
    )

    if Path("/boot/grub").is_dir():
        os.makedirs("/boot_backup", exist_ok=True)
        with chdir(
            "/boot_backup",
        ):
            if not Path("/boot_backup/.git").is_dir():
                hs.Command("git")("init")

            hs.Command("git")(
                "config",
                "user.email",
                "user@example.com",
            )
            hs.Command("git")(
                "config",
                "user.name",
                "user",
            )

            timestamp = str(time.time())
            os.makedirs(timestamp)
            hs.Command("cp")(
                "-ar",
                "/boot",
                timestamp + "/",
            )
            hs.Command("git")(
                "add",
                timestamp,
                "--force",
            )
            hs.Command("git")(
                "commit",
                "-m",
                timestamp,
            )

    icp("kernel compile and install completed OK")
