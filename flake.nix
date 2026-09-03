{
  description = "Blackmagic RAW gyro/IMU inject & extract tools";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.05";
  };

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAllSystems (pkgs: {
        default =
        pkgs.stdenvNoCC.mkDerivation {
          pname = "blackmagic-utils";
          version = "0.2.0";
          src = self;
          # bash for the generated shebang; python3 is the interpreter the
          # dispatcher invokes for the inject/extract subcommands.
          nativeBuildInputs = [ pkgs.bash pkgs.python3 ];
          installPhase = ''
            mkdir -p $out/bin $out/lib/bm_utils $out/share/bm_utils/scripts $out/share/bash-completion/completions

            # Python logic (invoked explicitly, so no shebang fixup needed).
            cp $src/inject_gyro_into_braw.py $src/extract_gyro_from_braw.py $out/lib/bm_utils/

            # Fusion post-render scripts, so `bm_utils install` is self-contained.
            cp -R $src/scripts/. $out/share/bm_utils/scripts/

            # Bash completion (loaded automatically if the user has bash-completion).
            cp $src/completions/bm_utils $out/share/bash-completion/completions/bm_utils

            # The single dispatcher. Bake in an absolute interpreter (the nix
            # bash for the shebang, the nix python for the BM_UTILS_PYTHON
            # default) so it runs straight out of the store where no python3
            # is on the PATH.
            sed -e "1s|^#!.*|#!${pkgs.bash}/bin/bash|" -e "s|:-python3}|:-${pkgs.python3}/bin/python3}|" $src/bm_utils > $out/bin/bm_utils
            chmod +x $out/bin/bm_utils
          '';
        };
      });

      devShells = forAllSystems (pkgs:
        pkgs.mkShell {
          packages = [ pkgs.python3 pkgs.bash-completion ];
          shellHook = ''
            # Make the repo's dispatcher + completion available in this shell.
            export PATH="$PWD:$PATH"
            . "$PWD/completions/bm_utils"
          '';
        });
    };
}
