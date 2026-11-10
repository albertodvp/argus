{
  description = "Argus development environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-parts.url = "github:hercules-ci/flake-parts";
    flake-parts.inputs.nixpkgs-lib.follows = "nixpkgs";
  };

  outputs = inputs:
    inputs.flake-parts.lib.mkFlake { inherit inputs; } {
      systems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" ];
      perSystem = { pkgs, lib, ... }: {
        # Python 3.11 is the oldest interpreter Hermes supports: type-check and test against it.
        devShells.default = pkgs.mkShellNoCC {
          # nodejs: npm installs the pinned TypeScript that type-checks the dashboard JS (just typecheck-js).
          packages = with pkgs; [ python311 uv ruff just direnv nodejs ];
          UV_PYTHON = "${pkgs.python311}/bin/python3";
          UV_PYTHON_DOWNLOADS = "never";
          UV_LINK_MODE = "copy";
        };
        # `just validate`: `hermes plugins validate --install-deps` stages Hermes' own prebuilt uv
        # (generic Linux, dynamically linked), which NixOS cannot run outside an FHS environment.
        packages = lib.optionalAttrs pkgs.stdenv.hostPlatform.isLinux {
          catalog-env = pkgs.buildFHSEnv {
            name = "argus-catalog-env";
            targetPkgs = p: with p; [ bash coreutils git uv python314 stdenv.cc.cc.lib zlib openssl ];
            runScript = "bash";
          };
        };
      };
    };
}
