{
  description = "Develop of this shit lol";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      fastcdc = pkgs.python3Packages.buildPythonPackage rec {
        pname = "fastcdc";
        version = "1.7.0";

        pyproject = true;

        src = pkgs.python3Packages.fetchPypi {
          inherit pname version;
          hash = "sha256-Y0tPvqhSlkhOiWtv9w5DvNlHJJiVMMhjmm5bJTEF7tI=";
        };

        build-system = [
          pkgs.python3Packages.setuptools
          pkgs.python3Packages.poetry-core
          pkgs.python3Packages.cython
        ];

        doCheck = false;
        dontCheckRuntimeDeps = true;
      };
    in
    {
      devShells.${system}.default = pkgs.mkShell {
        nativeBuildInputs = with pkgs; [
          yt-dlp
          (python3.withPackages (ps: with ps; [
            fastapi
            uvicorn
            aiofiles
            mutagen
            python-dotenv
            spotipy
            pillow
            pyjwt
            passlib
            bcrypt
            python-multipart
            sqlalchemy
            redis
            requests
            unidecode
            httpx
            blake3
            fastcdc
          ]))
          ytdl-sub
          ffmpeg
          sqlite

          (writeShellScriptBin "run" ''
            exec python src/main.py "$@"
          '')
        ];

        shellHook = ''
          export PYTHONPYCACHEPREFIX="''$PWD/.cache/omnitune"
          echo "Hello World!"
        '';
      };
    };
}