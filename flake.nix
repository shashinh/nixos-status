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
{
  description = "Static NixOS system status page, regenerated on every switch";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      # The CLI on its own. `nix run github:shashinh/nixos-status` prints the
      # text summary of the local state directory; the NixOS module is what
      # keeps that state up to date.
      packages = forAllSystems (pkgs: rec {
        nixos-status = pkgs.callPackage ./package.nix { };
        default = nixos-status;
      });

      apps = forAllSystems (pkgs: {
        default = {
          type = "app";
          program = "${self.packages.${pkgs.stdenv.hostPlatform.system}.nixos-status}/bin/nixos-status";
          meta.description = "Print the status summary of the active NixOS generation";
        };
      });

      overlays.default = final: _prev: { nixos-status = final.callPackage ./package.nix { }; };

      nixosModules = {
        nixos-status = import ./module.nix;
        default = self.nixosModules.nixos-status;
      };
    };
}
