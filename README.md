# autopatchelf

A tool to patch ELF files so that they refer to specific required libraries. Required libraries are looked up automatically in pre-specified directories/paths.

Uses [`patchelf`](https://github.com/NixOS/patchelf) to do the actual patching.

Based on the [`autopatchelf` hook](https://github.com/NixOS/nixpkgs/blob/e42a5c78e75aba56b546cbcb8efdf46587fea276/doc/hooks/autopatchelf.section.md), specifically [auto-patchelf.py](https://github.com/NixOS/nixpkgs/blob/e42a5c78e75aba56b546cbcb8efdf46587fea276/pkgs/build-support/setup-hooks/auto-patchelf.py).

## Usage

```console
$ export NIX_BINTOOLS="$(nix-build -A bintools '<nixpkgs>')"
$ autopatchelf --libs $(nix-build -A gcc-unwrapped.lib '<nixpkgs>')/lib $(nix-build -A zlib '<nixpkgs>')/lib --path directory_containing_so_libraries_and_elf_executables
```
