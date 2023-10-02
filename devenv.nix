{ pkgs, ... }:

{
  languages.python = {
    enable = true;
    poetry.enable = true;
    package = pkgs.python312;
  };
  pre-commit.hooks = {
    black.enable = true;
    nixpkgs-fmt.enable = true;
  };
}
