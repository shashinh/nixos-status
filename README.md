# nixos-status

A static status page for a NixOS machine, regenerated on every
`nixos-rebuild switch`. It answers the questions that usually need a handful
of `nix` commands after an update: what is installed, which version, where it
came from, what changed since the last generation, how big the system is, and
whether a reboot is pending.

The page is a single self-contained HTML file. It is rendered once, by an
activation hook, and the browser only displays it: nothing is computed when it
is opened, no daemon runs, nothing is fetched.

What the page shows:

- **Versions**: NixOS release, nixpkgs revision, kernel, systemd, Nix, the
  flake revision the system was built from, and the previous value of each
  when it changed.
- **Size**: closure size of the whole system, of the system packages, of each
  user's profile and of kernel + initrd + firmware, each with its delta
  against the previous generation; disk usage of the filesystem holding
  `/nix/store` and of `/boot`; number of generations kept.
- **Flake inputs**: every input from `flake.lock` with its revision and date;
  inputs that moved since the previous generation are marked and link to the
  upstream compare view.
- **Packages**, grouped into system packages, one group per home-manager
  user, Flatpak applications, and any extra groups you declare. Each row has
  the version, the flake input the package definition came from (`nixpkgs`,
  `nixpkgs-stable`, another input, or the configuration flake itself), its
  closure size, and its description. Toggles show the previous version
  (`1.2.3 → 1.3.0`), restrict the list to what changed, or filter by name.
- **Changelog**: one entry per recorded switch (ten by default) listing the
  packages added, removed and upgraded, the inputs that moved, and the size
  delta.
- A **reboot-needed** banner when the booted kernel or initrd differs from
  the active generation's.
- A light/dark switch. The default follows the system preference.

A `nixos-status` command prints the same summary as plain text, and a
launcher entry ("NixOS Status") opens the page in the default browser.

## Quick start

Add the flake as an input and enable the module:

```nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    nixos-status = {
      url = "github:shashinh/nixos-status";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, nixos-status, ... }@inputs: {
    nixosConfigurations.myhost = nixpkgs.lib.nixosSystem {
      modules = [
        nixos-status.nixosModules.default
        {
          services.nixos-status = {
            enable = true;
            # Optional, recommended: lets the page name the flake input each
            # package came from, show the configuration's revision, and list
            # the inputs table from flake.lock.
            flake = self;
            inherit inputs;
          };
        }
        ./configuration.nix
      ];
    };
  };
}
```

Switch once. The page is written to `/var/lib/nixos-status/index.html`; the
previous-version diff and the changelog fill in from the second switch on.

```sh
nixos-status          # plain-text summary in the terminal
nixos-status open     # open the page in the default browser
nixos-status path     # print the path of the page
```

Without the module, `nix run github:shashinh/nixos-status` prints the summary
of an existing state directory.

## Options

All options live under `services.nixos-status`.

| Option | Default | Description |
|---|---|---|
| `enable` | `false` | Install the CLI, the launcher entry and the activation hook. |
| `flake` | `null` | The configuration flake (`self`). Enables the flake revision in the header and the inputs table. |
| `inputs` | `{ }` | The configuration's flake inputs. Packages are tagged with the name of the input whose source tree defines them. Without it, packages from the configured nixpkgs are tagged `nixpkgs`, everything else `other`. |
| `users` | home-manager users, else all normal users | Users whose packages, profile size and Flatpak installation are reported. Per-user package lists need the home-manager NixOS module. |
| `extraGroups` | `{ }` | Additional package groups, keyed by section title, e.g. `{ "Server stack" = [ pkgs.nginx pkgs.postgresql ]; }`. |
| `historyKeep` | `10` | Number of activation records kept, which bounds the changelog. |
| `stateDir` | `/var/lib/nixos-status` | Where `index.html`, `latest.json` and `history/` are written. |
| `desktopEntry.enable` | `true` | Install the launcher entry. |
| `desktopEntry.name` | `"NixOS Status"` | Launcher entry title. |
| `desktopEntry.icon` | `"nix-snowflake"` | Icon name for the launcher entry. |
| `package` | the flake's package, built with `config.nix.package` | The CLI used by the hook and installed system-wide. |

## Where the data lives

```
/var/lib/nixos-status/
├── index.html          the page
├── latest.json         symlink to the newest record
└── history/            one JSON record per activated generation
    └── <generation>-<unix time>.json
```

Each record is self-contained: the evaluation-time manifest plus everything
gathered at activation (generation number, closure sizes, disk usage, booted
kernel, Flatpak apps). The directory and files are world-readable; a package
list is not secret, and the launcher entry opens the page as a regular user.

Activating the same generation again (running `switch` twice, or the boot
after `nixos-rebuild boot`) replaces the newest record rather than adding a
duplicate, so the changelog only has real transitions.

The state must survive reboots for the diff and the changelog to work. On a
system with an impermanent root, persist `/var/lib/nixos-status`.

## How it works

1. **Evaluation time.** The module writes a small JSON manifest into the
   system closure: versions, the root inputs of `flake.lock`, and every
   package in `environment.systemPackages` and in each home-manager user's
   `home.packages`. Each package is tagged with the flake input whose source
   tree contains the file that defines it, by matching `meta.position`
   against the inputs' store paths. Store paths are recorded as plain strings
   so the manifest never pulls source trees into the closure.
2. **Activation.** A `system.activationScripts` entry runs
   `nixos-status generate` after `/etc` and users are set up. It reads the
   generation number from the system profile, asks `nix path-info` for the
   closure sizes in one call, reads `df`, compares the booted kernel and
   initrd with the activated ones, lists Flatpak apps if Flatpak is enabled,
   writes the record and renders the page. It runs as root and finishes well
   under a second; a failure prints a warning and never aborts activation.
3. **The page** is plain HTML with inline CSS and a few lines of inline
   JavaScript for the toggles, the filter box and the theme switch. The
   previous versions are already in the markup; the toggles only change what
   is displayed.

## Launcher entry

The module installs a standard XDG desktop entry that runs
`xdg-open file:///var/lib/nixos-status/index.html`, so any launcher that reads
desktop entries shows it. Some launchers cache the entry list and need a
restart to pick up a new entry. The entry opens the page with whatever
handles `text/html` for the user, so make sure a browser is the default
handler if it is not already.

## Caveats

- `nixos-rebuild test` activates a system that is not in the profile; the
  record is labelled generation `test`. `nixos-rebuild boot` renders the page
  on the next boot, not at `boot` time.
- Provenance uses `meta.position`, the file that called `mkDerivation`. A
  package defined in nixpkgs but overridden from your flake still reports
  `nixpkgs`; a wrapper you build with `symlinkJoin` or `writeShellApplication`
  reports your flake (`self`). Packages generated by NixOS or home-manager
  modules report the input the module lives in. Packages without a position
  report `unknown`.
- Package sizes are closure sizes (the package and everything it references),
  so they overlap and do not add up to the total.
- Flatpak versions are whatever `flatpak list` reports.
- The size of the filesystem holding `/nix/store` is for the whole
  filesystem. On a single btrfs volume with subvolumes it includes everything
  else on that volume.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
