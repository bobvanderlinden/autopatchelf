{ pkgs, ... }:

{
  languages.python = {
    enable = true;
    poetry.enable = true;
    poetry.install.installRootPackage = true;
    package = pkgs.python312;
  };
  packages = [
    pkgs.hyperfine
  ];
  pre-commit.hooks = {
    black.enable = true;
    nixpkgs-fmt.enable = true;
  };
}
