# nixos-status: static NixOS system status page, regenerated on every switch.
# Copyright (C) 2026 Shashin Halalingaiah
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# How it works
# ------------
# 1. At evaluation time this module writes a small JSON manifest into the
#    system closure: versions, the flake inputs from flake.lock, and every
#    package in environment.systemPackages and in each home-manager user's
#    home.packages, each tagged with the flake input its definition came
#    from. Store paths are recorded as plain strings (string context is
#    dropped) so the manifest never pulls source trees into the closure.
# 2. An activation script runs `nixos-status generate` on every switch and
#    on boot. It adds what is only known at that moment (generation number,
#    closure sizes, disk usage, booted kernel, flatpak apps), appends a
#    record to the history under `stateDir`, and renders `index.html`.
# 3. The page is static. Opening it computes nothing.
{
  config,
  lib,
  pkgs,
  options,
  ...
}:
let
  cfg = config.services.nixos-status;
  inherit (lib) types;

  hasHomeManager = options ? home-manager && config.home-manager ? users;

  safe =
    default: expr:
    let
      r = builtins.tryEval expr;
    in
    if r.success then r.value else default;

  stripContext = s: builtins.unsafeDiscardStringContext (toString s);

  # Flake inputs the consumer handed over, as { name, path } pairs, followed
  # by nixpkgs itself as the fallback. Consumer names win on equal paths so
  # a nixpkgs input called e.g. `nixpkgs-stable` keeps its own name.
  sources =
    lib.mapAttrsToList (name: input: {
      inherit name;
      path = stripContext input.outPath;
    }) (lib.filterAttrs (_: i: builtins.isAttrs i && i ? outPath) cfg.inputs)
    ++ lib.optional (cfg.flake != null && cfg.flake ? outPath) {
      name = "self";
      path = stripContext cfg.flake.outPath;
    }
    ++ [
      {
        name = "nixpkgs";
        path = stripContext pkgs.path;
      }
    ];

  # The flake input whose source tree contains the file that defines `pos`
  # (a `meta.position` value such as /nix/store/…-source/pkgs/foo.nix:12).
  # Longest matching prefix wins; ties go to the earliest entry in `sources`.
  sourceOf =
    pos:
    lib.foldl' (
      best: s:
      if
        s.path != ""
        && lib.hasPrefix (s.path + "/") pos
        && (best == null || builtins.stringLength s.path > builtins.stringLength best.path)
      then
        s
      else
        best
    ) null sources;

  packageInfo =
    p:
    let
      pos = stripContext (safe "" (p.meta.position or ""));
      src = sourceOf pos;
      outputs = safe [ "out" ] (p.outputs or [ "out" ]);
      outPaths = map (o: stripContext p.${o}.outPath) (builtins.filter (o: p ? ${o}) outputs);
      description = safe "" (p.meta.description or "");
      homepage = safe null (p.meta.homepage or null);
    in
    {
      name = safe "" p.name;
      pname = lib.getName p;
      version = safe "" (p.version or (lib.getVersion p));
      source =
        if src != null then
          src.name
        else if pos == "" then
          "unknown"
        else
          "other";
      position = if src == null then pos else lib.removePrefix (src.path + "/") pos;
      inherit outPaths;
      description = if builtins.isString description then description else "";
      homepage =
        if builtins.isString homepage then
          homepage
        else if builtins.isList homepage && homepage != [ ] then
          builtins.head homepage
        else
          null;
    };

  # One entry per distinct `name` (pname-version); outputs of the same
  # package collapse into one row. Sorted by lower-case pname, then version.
  packageList =
    ps:
    let
      infos = map packageInfo (builtins.filter lib.isDerivation ps);
      byName = lib.listToAttrs (map (i: lib.nameValuePair i.name i) infos);
      key = i: "${lib.toLower i.pname}\n${i.version}";
    in
    lib.sort (a: b: key a < key b) (lib.attrValues byName);

  groups =
    [
      {
        id = "system";
        title = "System packages";
        packages = packageList config.environment.systemPackages;
      }
    ]
    ++ lib.optionals hasHomeManager (
      map (u: {
        id = "user-${u}";
        title = "User packages: ${u}";
        packages = packageList config.home-manager.users.${u}.home.packages;
      }) (builtins.filter (u: config.home-manager.users ? ${u}) cfg.users)
    )
    ++ lib.mapAttrsToList (id: ps: {
      id = "extra-${id}";
      title = id;
      packages = packageList ps;
    }) cfg.extraGroups;

  # flake.lock of the consumer flake, reduced to its root-level inputs.
  lockFile = if cfg.flake != null then "${cfg.flake}/flake.lock" else null;
  lock =
    if lockFile != null && builtins.pathExists lockFile then
      builtins.fromJSON (builtins.readFile lockFile)
    else
      null;
  resolveRef =
    ref:
    if builtins.isString ref then
      ref
    else
      lib.foldl' (nodeName: el: resolveRef (lock.nodes.${nodeName}.inputs.${el})) lock.root ref;
  inputsInfo =
    if lock == null then
      [ ]
    else
      lib.mapAttrsToList (
        name: ref:
        let
          node = lock.nodes.${resolveRef ref} or { };
          locked = node.locked or { };
          original = node.original or { };
        in
        {
          inherit name;
          follows = !(builtins.isString ref);
          type = locked.type or original.type or null;
          owner = original.owner or locked.owner or null;
          repo = original.repo or locked.repo or null;
          ref = original.ref or null;
          url = original.url or locked.url or null;
          rev = locked.rev or null;
          lastModified = locked.lastModified or null;
        }
      ) (lock.nodes.${lock.root}.inputs or { });

  flakeInfo =
    if cfg.flake == null then
      null
    else
      {
        rev = cfg.flake.rev or cfg.flake.dirtyRev or null;
        shortRev = cfg.flake.shortRev or cfg.flake.dirtyShortRev or null;
        dirty = !(cfg.flake ? rev);
        lastModified = cfg.flake.lastModified or null;
      };

  manifest = pkgs.writeText "nixos-status-manifest.json" (
    builtins.toJSON {
      format = 1;
      system = {
        hostname = config.networking.hostName;
        nixos = {
          version = config.system.nixos.version;
          release = config.system.nixos.release;
          codeName = config.system.nixos.codeName or null;
          nixpkgsRevision = config.system.nixos.revision or null;
        };
        kernel = {
          version = config.boot.kernelPackages.kernel.version;
          modDirVersion = config.boot.kernelPackages.kernel.modDirVersion or null;
        };
        systemd = config.systemd.package.version;
        nix = config.nix.package.version;
        stateVersion = config.system.stateVersion;
        flake = flakeInfo;
      };
      inputs = inputsInfo;
      inherit groups;
      users = cfg.users;
      flatpak = config.services.flatpak.enable or false;
      stateDir = cfg.stateDir;
      historyKeep = cfg.historyKeep;
    }
  );

  desktopItem = pkgs.makeDesktopItem {
    name = "nixos-status";
    desktopName = cfg.desktopEntry.name;
    genericName = "System status";
    comment = "Packages, versions, sizes and changes of the active NixOS generation";
    exec = "xdg-open file://${cfg.stateDir}/index.html";
    icon = cfg.desktopEntry.icon;
    categories = [
      "System"
      "Monitor"
    ];
    terminal = false;
    startupNotify = false;
  };
in
{
  options.services.nixos-status = {
    enable = lib.mkEnableOption "the NixOS status page, regenerated on every switch";

    package = lib.mkOption {
      type = types.package;
      default = pkgs.callPackage ./package.nix { nix = config.nix.package; };
      defaultText = lib.literalExpression "pkgs.callPackage ./package.nix { nix = config.nix.package; }";
      description = "The nixos-status CLI used by the activation hook and installed system-wide.";
    };

    stateDir = lib.mkOption {
      type = types.str;
      default = "/var/lib/nixos-status";
      description = ''
        Directory holding `index.html`, `latest.json` and `history/`.
        Must survive reboots for the previous-version diff and the changelog
        to work; on an impermanent root, persist it.
      '';
    };

    users = lib.mkOption {
      type = types.listOf types.str;
      default =
        if hasHomeManager then
          lib.attrNames config.home-manager.users
        else
          lib.attrNames (lib.filterAttrs (_: u: u.isNormalUser) config.users.users);
      defaultText = lib.literalMD "home-manager users if present, otherwise all normal users";
      description = ''
        Users whose packages, profile size and flatpak installation are
        reported. Per-user package lists require the home-manager NixOS module.
      '';
    };

    flake = lib.mkOption {
      type = types.nullOr types.raw;
      default = null;
      example = lib.literalExpression "inputs.self";
      description = ''
        The flake this configuration is built from (`self`). Enables the
        revision in the page header and the flake inputs table, read from
        its `flake.lock`.
      '';
    };

    inputs = lib.mkOption {
      type = types.attrsOf types.raw;
      default = { };
      example = lib.literalExpression "inputs";
      description = ''
        The flake inputs of this configuration. Each package is tagged with
        the name of the input whose source tree defines it (`nixpkgs`,
        `nixpkgs-stable`, …). Without this, packages from the configured
        nixpkgs are tagged `nixpkgs` and everything else `other`.
      '';
    };

    extraGroups = lib.mkOption {
      type = types.attrsOf (types.listOf types.package);
      default = { };
      example = lib.literalExpression ''{ "Server stack" = [ pkgs.nginx pkgs.postgresql ]; }'';
      description = "Additional package groups to list, keyed by section title.";
    };

    historyKeep = lib.mkOption {
      type = types.ints.positive;
      default = 10;
      description = "Number of activation records kept for the changelog.";
    };

    desktopEntry = {
      enable = lib.mkOption {
        type = types.bool;
        default = true;
        description = "Install a launcher entry that opens the page in the default browser.";
      };
      name = lib.mkOption {
        type = types.str;
        default = "NixOS Status";
        description = "Launcher entry title.";
      };
      icon = lib.mkOption {
        type = types.str;
        default = "nix-snowflake";
        description = "Icon name for the launcher entry (nixos-icons ships `nix-snowflake`).";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    system.build.nixosStatusManifest = manifest;

    environment.systemPackages = [ cfg.package ] ++ lib.optional cfg.desktopEntry.enable desktopItem;

    # Runs after /etc and users are in place, before systemd units switch.
    # A failure here must never abort activation.
    system.activationScripts.nixos-status =
      lib.stringAfter
        [
          "etc"
          "users"
          "specialfs"
        ]
        ''
          ${cfg.package}/bin/nixos-status generate \
            --manifest ${manifest} \
            --system-config "$systemConfig" \
            --state ${lib.escapeShellArg cfg.stateDir} \
            || echo "nixos-status: status page generation failed (non-fatal)" >&2
        '';
  };
}
