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
# The CLI is a single stdlib-only Python program. `nix` is needed by the
# `generate` subcommand (closure sizes); coreutils and util-linux provide
# `df` and `runuser`. flatpak and xdg-open are looked up on PATH at runtime
# so that hosts without them pay no closure cost.
{
  lib,
  writeShellApplication,
  python3,
  nix,
  coreutils,
  util-linux,
}:
writeShellApplication {
  name = "nixos-status";
  runtimeInputs = [
    nix
    coreutils
    util-linux
  ];
  text = ''
    exec ${python3}/bin/python3 -I ${./render.py} "$@"
  '';
  meta = {
    description = "Static NixOS system status page, regenerated on every switch";
    homepage = "https://github.com/shashinh/nixos-status";
    license = lib.licenses.gpl3Plus;
    mainProgram = "nixos-status";
    platforms = lib.platforms.linux;
  };
}
