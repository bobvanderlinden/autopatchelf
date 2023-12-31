#!/usr/bin/env python3

import argparse
import os
import pprint
import subprocess
import sys
from fnmatch import fnmatch
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import DefaultDict, Iterator, List, Optional, Set, Tuple

from elftools.common.exceptions import ELFError  # type: ignore
from elftools.elf.dynamic import DynamicSection  # type: ignore
from elftools.elf.elffile import ELFFile  # type: ignore
from elftools.elf.enums import ENUM_E_TYPE, ENUM_EI_OSABI  # type: ignore

import logging

logger = logging.getLogger(__name__)

interpreter_path: Path = None  # type: ignore
interpreter_osabi: str = None  # type: ignore
interpreter_arch: str = None  # type: ignore
libc_lib: Path = None  # type: ignore
patchelf: str = None  # type: ignore


@contextmanager
def open_elf(path: Path) -> Iterator[ELFFile]:
    with path.open("rb") as stream:
        yield ELFFile(stream)


def is_static_executable(elf: ELFFile) -> bool:
    # Statically linked executables have an ELF type of EXEC but no INTERP.
    return elf.header["e_type"] == "ET_EXEC" and not elf.get_section_by_name(".interp")


def is_dynamic_executable(elf: ELFFile) -> bool:
    # We do not require an ELF type of EXEC. This also catches
    # position-independent executables, as they typically have an INTERP
    # section but their ELF type is DYN.
    return bool(elf.get_section_by_name(".interp"))


def get_dependencies(elf: ELFFile) -> List[str]:
    dependencies = []
    # This convoluted code is here on purpose. For some reason, using
    # elf.get_section_by_name(".dynamic") does not always return an
    # instance of DynamicSection, but that is required to call iter_tags
    for section in elf.iter_sections():
        if isinstance(section, DynamicSection):
            for tag in section.iter_tags("DT_NEEDED"):
                dependencies.append(tag.needed)
            break  # There is only one dynamic section

    return dependencies


def get_rpath(elf: ELFFile) -> List[str]:
    # This convoluted code is here on purpose. For some reason, using
    # elf.get_section_by_name(".dynamic") does not always return an
    # instance of DynamicSection, but that is required to call iter_tags
    for section in elf.iter_sections():
        if isinstance(section, DynamicSection):
            for tag in section.iter_tags("DT_RUNPATH"):
                return tag.runpath.split(":")

            for tag in section.iter_tags("DT_RPATH"):
                return tag.rpath.split(":")

            break  # There is only one dynamic section

    return []


def get_arch(elf: ELFFile) -> str:
    return elf.get_machine_arch()


def get_osabi(elf: ELFFile) -> str:
    return elf.header["e_ident"]["EI_OSABI"]


def osabi_are_compatible(wanted: str, got: str) -> bool:
    """
    Tests whether two OS ABIs are compatible, taking into account the
    generally accepted compatibility of SVR4 ABI with other ABIs.
    """
    if not wanted or not got:
        # One of the types couldn't be detected, so as a fallback we'll
        # assume they're compatible.
        return True

    # Generally speaking, the base ABI (0x00), which is represented by
    # readelf(1) as "UNIX - System V", indicates broad compatibility
    # with other ABIs.
    #
    # TODO: This isn't always true. For example, some OSes embed ABI
    # compatibility into SHT_NOTE sections like .note.tag and
    # .note.ABI-tag.  It would be prudent to add these to the detection
    # logic to produce better ABI information.
    if wanted == "ELFOSABI_SYSV":
        return True

    # Similarly here, we should be able to link against a superset of
    # features, so even if the target has another ABI, this should be
    # fine.
    if got == "ELFOSABI_SYSV":
        return True

    # Otherwise, we simply return whether the ABIs are identical.
    return wanted == got


def glob(path: Path, pattern: str, recursive: bool) -> Iterator[Path]:
    if path.is_dir():
        return path.rglob(pattern) if recursive else path.glob(pattern)
    else:
        # path.glob won't return anything if the path is not a directory.
        # We extend that behavior by matching the file name against the pattern.
        # This allows to pass single files instead of dirs to auto_patchelf,
        # for greater control on the files to consider.
        return [path] if path.match(pattern) else []


cached_paths: Set[Path] = set()
soname_cache: DefaultDict[Tuple[str, str], List[Tuple[Path, str]]] = defaultdict(list)


def populate_cache(initial: List[Path], recursive: bool = False) -> None:
    lib_dirs = list(initial)

    while lib_dirs:
        lib_dir = lib_dirs.pop(0)

        if lib_dir in cached_paths:
            continue

        cached_paths.add(lib_dir)

        for path in glob(lib_dir, "*.so*", recursive):
            if not path.is_file():
                continue

            # As an optimisation, resolve the symlinks here, as the target is unique
            # XXX: (layus, 2022-07-25) is this really an optimisation in all cases ?
            # It could make the rpath bigger or break the fragile precedence of $out.
            resolved = path.resolve()
            # Do not use resolved paths when names do not match
            if resolved.name != path.name:
                resolved = path

            try:
                with open_elf(path) as elf:
                    osabi = get_osabi(elf)
                    arch = get_arch(elf)
                    rpath = [
                        Path(p) for p in get_rpath(elf) if p and "$ORIGIN" not in p
                    ]
                    lib_dirs += rpath
                    soname_cache[(path.name, arch)].append((resolved.parent, osabi))

            except ELFError:
                # Not an ELF file in the right format
                pass


def find_dependency(soname: str, soarch: str, soabi: str) -> Optional[Path]:
    for lib, libabi in soname_cache[(soname, soarch)]:
        if osabi_are_compatible(soabi, libabi):
            return lib
    return None


@dataclass
class Dependency:
    file: Path  # The file that contains the dependency
    name: Path  # The name of the dependency
    found: bool = False  # Whether it was found somewhere


def auto_patchelf_file(
    path: Path, runtime_deps: list[Path], append_rpaths: List[Path] = []
) -> list[Dependency]:
    try:
        with open_elf(path) as elf:
            if is_static_executable(elf):
                # No point patching these
                logger.debug("skipping %s because it is statically linked", path)
                return []

            if elf.num_segments() == 0:
                # no segment (e.g. object file)
                logger.debug("skipping %s because it contains no segment", path)
                return []

            file_arch = get_arch(elf)
            if interpreter_arch != file_arch:
                # Our target architecture is different than this file's
                # architecture, so skip it.
                logger.debug(
                    "skipping %s because its architecture (%s)"
                    " differs from target (%s)",
                    path,
                    file_arch,
                    interpreter_arch,
                )
                return []

            file_osabi = get_osabi(elf)
            if not osabi_are_compatible(interpreter_osabi, file_osabi):
                logger.debug(
                    "skipping %s because its OS ABI (%s) is"
                    " not compatible with target (%s)",
                    path,
                    file_osabi,
                    interpreter_osabi,
                )
                return []

            file_is_dynamic_executable = is_dynamic_executable(elf)

            file_dependencies = map(Path, get_dependencies(elf))

    except ELFError:
        return []

    rpath = []

    patchelf_args = []

    if file_is_dynamic_executable:
        logger.debug("setting interpreter of %s", path)
        patchelf_args += ["--set-interpreter", interpreter_path.as_posix()]
        rpath += runtime_deps

    logger.debug("searching for dependencies of %s", path)
    dependencies = []
    # Be sure to get the output of all missing dependencies instead of
    # failing at the first one, because it's more useful when working
    # on a new package where you don't yet know the dependencies.
    for dep in file_dependencies:
        if dep.is_absolute() and dep.is_file():
            # This is an absolute path. If it exists, just use it.
            # Otherwise, we probably want this to produce an error when
            # checked (because just updating the rpath won't satisfy
            # it).
            continue
        elif (libc_lib / dep).is_file():
            # This library exists in libc, and will be correctly
            # resolved by the linker.
            continue

        if found_dependency := find_dependency(dep.name, file_arch, file_osabi):
            rpath.append(found_dependency)
            dependencies.append(Dependency(path, dep, True))
            logger.debug("    %s -> found: %s", dep, found_dependency)
        else:
            dependencies.append(Dependency(path, dep, False))
            logger.debug("    %s -> not found!", dep)

    rpath.extend(append_rpaths)

    if rpath:
        # Dedup the rpath
        rpath_str = ":".join(dict.fromkeys(map(Path.as_posix, rpath)))
        logger.debug("setting RPATH to %s", rpath_str)
        patchelf_args += ["--set-rpath", rpath_str]

    if patchelf_args:
        subprocess.run([patchelf, *patchelf_args, path.as_posix()], check=True)

    return dependencies


def auto_patchelf(
    paths_to_patch: List[Path],
    lib_dirs: List[Path],
    runtime_deps: List[Path] = [],
    recursive: bool = True,
    ignore_missing: List[str] = [],
    append_rpaths: List[Path] = [],
) -> None:
    if not paths_to_patch:
        sys.exit("No paths to patch, stopping.")

    # Add all shared objects of the current output path to the cache,
    # before lib_dirs, so that they are chosen first in find_dependency.
    populate_cache(paths_to_patch, recursive)
    populate_cache(lib_dirs)

    dependencies = [
        dependency
        for directory_path in paths_to_patch
        for file_path in glob(directory_path, "*", recursive)
        if file_path.is_file()
        if not file_path.is_symlink()
        for dependency in auto_patchelf_file(file_path, runtime_deps, append_rpaths)
    ]

    missing = [dependency for dependency in dependencies if not dependency.found]

    # Print a summary of the missing dependencies at the end
    logger.debug("%s dependencies could not be satisfied", len(missing))
    failure = False
    for dep in missing:
        for pattern in ignore_missing:
            if fnmatch(dep.name.name, pattern):
                logger.warn("ignoring missing %s wanted by %s", dep.name, dep.file)
                break
        else:
            logger.error(
                "could not satisfy dependency %s wanted by %s", dep.name, dep.file
            )
            failure = True

    if failure:
        sys.exit(
            "auto-patchelf failed to find all the required dependencies.\n"
            "Add the missing dependencies to --libs or use "
            '`--ignore-missing="foo.so.1 bar.so etc.so"`.'
        )


def main() -> None:
    global interpreter_path
    global interpreter_osabi
    global interpreter_arch
    global libc_lib
    global patchelf

    parser = argparse.ArgumentParser(
        prog="auto-patchelf",
        description="auto-patchelf tries as hard as possible to patch the"
        " provided binary files by looking for compatible"
        "libraries in the provided paths.",
    )
    parser.add_argument(
        "--ignore-missing",
        nargs="*",
        type=str,
        default=[],
        help="Do not fail when some dependencies are not found.",
    )
    parser.add_argument(
        "--no-recurse",
        dest="recursive",
        action="store_false",
        help="Disable the recursive traversal of paths to patch.",
    )
    parser.add_argument(
        "--paths",
        nargs="*",
        type=Path,
        help="Paths whose content needs to be patched."
        " Single files and directories are accepted."
        " Directories are traversed recursively by default.",
    )
    parser.add_argument(
        "--libs",
        nargs="*",
        type=Path,
        help="Paths where libraries are searched for."
        " Single files and directories are accepted."
        " Directories are not searched recursively.",
    )
    parser.add_argument(
        "--runtime-dependencies",
        nargs="*",
        type=Path,
        default=[],
        help="Paths to prepend to the runtime path of executable binaries."
        " Subject to deduplication, which may imply some reordering.",
    )
    parser.add_argument(
        "--append-rpaths",
        nargs="*",
        type=Path,
        default=[],
        help="Paths to append to all runtime paths unconditionally",
    )
    parser.add_argument(
        "--patchelf",
        type=str,
        default="patchelf",
        help="Path to the patchelf binary. Defaults to patchelf.",
    )
    parser.add_argument(
        "--bintools",
        type=str,
        default=os.getenv("NIX_BINTOOLS"),
        help="Path to the bintools package. Defaults to $NIX_BINTOOLS.",
    )
    parser.add_argument(
        "-v", "--verbose", default=0, action="count", help="increase output verbosity"
    )

    args = parser.parse_args()

    patchelf = args.patchelf

    if not args.bintools:
        sys.exit("Failed to find bintools.")

    nix_support = Path(args.bintools) / "nix-support"
    dynamic_linker = nix_support / "dynamic-linker"
    interpreter_path = Path(dynamic_linker.read_text().strip())
    orig_libc = nix_support / "orig-libc"
    libc_lib = Path(orig_libc.read_text().strip()) / "lib"

    verbosity = [logging.WARN, logging.INFO, logging.DEBUG]
    logging.basicConfig(level=verbosity[min(args.verbose, len(verbosity) - 1)])

    with open_elf(interpreter_path) as interpreter:
        interpreter_osabi = get_osabi(interpreter)
        interpreter_arch = get_arch(interpreter)

    if not interpreter_osabi:
        sys.exit("Failed to determine osabi from interpreter!")

    if not interpreter_arch:
        sys.exit("Failed to determine arch from interpreter!")

    auto_patchelf(
        args.paths,
        args.libs,
        args.runtime_dependencies,
        args.recursive,
        args.ignore_missing,
        append_rpaths=args.append_rpaths,
    )


if __name__ == "__main__":
    main()
