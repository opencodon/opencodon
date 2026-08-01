# nix/web.nix — opencodon browser UI (Vite/React) frontend build
{ pkgs, opencodonNpmLib, ... }:
let
  # @opencodon/client and @opencodon/shared ship as file: workspace deps of
  # apps/web, so their source must be in the filtered src tree too.
  npm = opencodonNpmLib.mkNpmPassthru {
    dirs = [
      "apps/web"
      "apps/client"
      "apps/shared"
    ];
  };

  packageJson = builtins.fromJSON (builtins.readFile (npm.src + "/apps/web/package.json"));
  version = packageJson.version;
in
pkgs.buildNpmPackage (npm // {
  pname = "opencodon-web";
  inherit version;

  doCheck = false;

  buildPhase = ''
    # Build from apps/web so vite.config.ts and tsconfig resolve correctly.
    # The workspace root's node_modules/ is at ../../node_modules/.
    cd apps/web
    # outDir in vite.config.ts points to ../../opencodon_cli/web_dist for the
    # monorepo layout.  Override with --outDir dist for the nix build.
    node ../../node_modules/vite/bin/vite.js build --outDir dist

    # Return to source root so installPhase paths are correct.
    cd ../..
  '';

  installPhase = ''
    runHook preInstall
    # vite writes to apps/web/dist/ (we cd'd there, overrode outDir, then cd'd back).
    cp -r apps/web/dist $out
    runHook postInstall
  '';
})
