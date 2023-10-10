{
  description = "autopatchelf";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    poetry2nix.url = "github:nix-community/poetry2nix";
  };

  outputs = { self, nixpkgs, poetry2nix, flake-utils }:
    let
      systems = [ "x86_64-linux" "i686-linux" "x86_64-darwin" "aarch64-linux" "aarch64-darwin" ];
      forAllSystems = f: builtins.listToAttrs (map (name: { inherit name; value = f name; }) systems);
    in
    {
      packages = forAllSystems (system: {
        default = poetry2nix.legacyPackages.${system}.mkPoetryApplication {
          projectDir = ./.;
          extras = [ ];
        };
      });
    };
}
