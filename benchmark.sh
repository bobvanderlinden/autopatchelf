#!/bin/sh
if ! [ -d test_venv_cached ]; then
  rm -rf test_venv
  python -m venv test_venv
  test_venv/bin/pip install pandas numpy scipy opencv-python scikit-learn
  cp -r test_venv test_venv_cached
fi

NIX_BINTOOLS="$(nix-build -A bintools '<nixpkgs>')"
PATH="$(nix-build -A patchelf '<nixpkgs>')/bin:$PATH"

LIBS="$(nix-build -A gcc-unwrapped.lib '<nixpkgs>')/lib $(nix-build -A zlib '<nixpkgs>')/lib $(nix-build -A qt6.qtbase '<nixpkgs>')/lib"

export NIX_BINTOOLS PATH

hyperfine \
  --prepare 'rm -rf test_venv; cp -r test_venv_cached test_venv' \
  "python autopatchelf.py --libs $LIBS --paths test_venv"
