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
          version = "0.1.0";
          src = self;
          nativeBuildInputs = [ pkgs.python312 ];
          installPhase = ''
            mkdir -p $out/bin
            cp inject_gyro_into_braw.py extract_gyro_from_braw.py $out/bin/
            chmod +x $out/bin/inject_gyro_into_braw.py $out/bin/extract_gyro_from_braw.py
            ln -s $out/bin/inject_gyro_into_braw.py  $out/bin/braw-inject-gyro
            ln -s $out/bin/extract_gyro_from_braw.py $out/bin/braw-extract-gyro
          '';
        };
      });

      devShells = forAllSystems (pkgs:
        pkgs.mkShell {
          packages = [ pkgs.python312 ];
          shellHook = ''
            braw-inject-gyro() { python3 ${self}/inject_gyro_into_braw.py "$@"; }
            braw-extract-gyro() { python3 ${self}/extract_gyro_from_braw.py "$@"; }
          '';
        });
    };
}
