{
  description = "argit dev shell — per-user agent backup/restore";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        py = pkgs.python312;
      in
      {
        devShells.default = pkgs.mkShell {
          buildInputs = [
            py
            pkgs.uv
            pkgs.ruff
            pkgs.gnupg
            pkgs.pass
            pkgs.sqlite
            pkgs.git
            pkgs.git-lfs
            (py.withPackages (ps: [ ps.pip ps.click ps.pytest ]))
          ];

          shellHook = ''
            # Workaround for nixpkgs xcrun warnings on Darwin
            # See: https://github.com/NixOS/nixpkgs/issues/376958
            unset DEVELOPER_DIR
            export PYTHONPATH="$PWD/src:$PYTHONPATH"
          '';
        };
      });
}
