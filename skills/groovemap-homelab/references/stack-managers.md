# Fitting GrooveMap into the user's stack manager

GrooveMap's lifecycle runs from the checkout with `just deploy`. Deploy merges three Compose
files, runs the import gate before MusicBrainz, writes `.env` and `secrets/`, and checks the
override. A stack manager that runs a single Compose file, or that starts the stack itself,
bypasses all of that. A manager that only displays the stack is fine.

The common thread for every manager below: **let it observe, don't let it start or update
GrooveMap**. Where a manager can run multiple files, use it only after the first load has
finished, and always with all three files. Claims about each tool's UI are **(unverified)**
against the version the user runs.

## Plain Compose

This is the default and the best fit. Keep the checkout where the user keeps other stacks.
Run `just deploy` to install and upgrade. Anything run by hand names
`-f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.override.yml`. A shell
alias saves typing:

```bash
alias gm='docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.override.yml'
```

## Portainer

A stack started from the CLI appears in Portainer as an external stack with limited control.
That is the right state: Portainer can show containers, logs and stats. Don't recreate
GrooveMap as a Portainer stack, from the web editor or from Git. The editor runs one file, and
a Git stack has no `secrets/` and no gate.

## Dockge

Dockge manages each stack as a directory under its stacks folder (default `/opt/stacks`), and
it starts and updates stacks from a single Compose file. Don't put the GrooveMap checkout
inside Dockge's stacks folder: Dockge would offer to start `docker-compose.yml` alone, which is
the development stack with public credentials (hard rule 3). Keep the checkout elsewhere.
GrooveMap's containers still show up in `docker ps` and in other tools.

## Komodo

Komodo can deploy a Stack from files on the server with several file paths. Recommend
`just deploy` for the first load and for upgrades, and Komodo for monitoring. If the user
insists on Komodo managing GrooveMap after the first load:

- point it at the checkout on the server ("files on server") with the file paths
  `docker-compose.yml`, `docker-compose.prod.yml` and `docker-compose.override.yml`, in that
  order;
- make sure it uses the checkout's own `.env`, and that `secrets/` stays in place;
- confirm `COMPOSE_PROFILES=musicbrainz` is already in `.env` (written by deploy after the
  gate) before Komodo ever runs `up`;
- turn off any automatic image updates for this stack. Images change only through
  `just env-init`.

## Auto-updaters (Watchtower, Diun, Ouroboros)

Watchtower would replace digest-pinned images with whatever their tags point to, which
violates hard rule 2. Exclude GrooveMap's containers. Watchtower honors the label
`com.centurylinklabs.watchtower.enable: "false"` on each service, or run Watchtower with
`--label-enable` so it updates only opted-in containers **(unverified)**. Diun only notifies,
so it is harmless.

## systemd

There's no need for a unit: the production overlay's `restart: always` makes Docker start the
stack after a reboot. If the user's convention is a unit per stack, ask first (it lives outside
the checkout), then use one that only runs `up`. The profile decision lives in `.env`, so `up`
stays safe before and after the gate:

```ini
# /etc/systemd/system/groovemap.service
[Unit]
Description=GrooveMap
Requires=docker.service
After=docker.service network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/srv/groovemap
ExecStart=/usr/bin/docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.override.yml up -d
ExecStop=/usr/bin/docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.override.yml stop

[Install]
WantedBy=multi-user.target
```

Leave out `-f docker-compose.override.yml` if there is no override. Never use `down -v` in a
unit.

## Ansible (or other configuration management)

Have the playbook own what the skill would otherwise write, and call the scripts for
everything else:

- clone or update the checkout (`ansible.builtin.git`);
- template `docker-compose.override.yml`, and set the host's `.env` keys with
  `ansible.builtin.lineinfile`, leaving every `*_IMAGE` line alone;
- run `just env-init` only when `.env` is absent, and `just deploy --no-wait` with
  `ansible.builtin.command` and `chdir` set to the checkout. Deploy is idempotent, so a re-run
  resumes or reconciles.

Don't model GrooveMap with `community.docker.docker_compose_v2` pointed at the base file. It
would skip the production overlay, the gate and the override check.
