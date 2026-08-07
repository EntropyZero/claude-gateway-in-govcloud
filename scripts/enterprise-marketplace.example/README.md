# Enterprise plugin marketplace — starter

Copy this directory into its **own git repository** on a host your developer
laptops can reach (a github.com org repo, or an internal GitLab/Gitea/etc.
over https/ssh), then point `deploy.env` at it (see the
`PLUGIN_MARKETPLACE_*` / `MANAGED_PLUGINS` block in `deploy.env.example`)
and re-run `deploy-gateway.sh`. Every client then registers the marketplace
(`extraKnownMarketplaces`) and force-installs the listed plugins
(`enabledPlugins`) at managed scope — users cannot disable them, and the
skills inside become available in every session.

Clients fetch this repository **directly** — the gateway and the offline
build host never touch it, so it is deliberately outside the mirror layer.
Two prerequisites to verify before enabling the push:

- **Reachable from developer laptops** (Zscaler policy, internal DNS).
- **Readable without interactive git authentication.** The client fetches
  with an internal git implementation that ignores git credential helpers,
  `gh` auth, and OS keychains — a private github.com repo fails outright
  (anthropics/claude-code#17201). Host it anonymously readable from the
  corporate network (an internal git server is usually the right answer).

Client-side auto-install from managed settings **needs live confirmation on
your deployed client version** (see `client-config.md` §6i): after first
enabling, run `/plugin` on a pilot client and confirm the plugin shows with
managed scope. If it does not auto-install, the one-time per-user fallback
is `/plugin install <plugin>@<marketplace>`.

## Layout

```
.claude-plugin/marketplace.json          the marketplace index
plugins/org-skills/                      one plugin (add more side by side)
  .claude-plugin/plugin.json             plugin manifest
  skills/example-skill/SKILL.md          one skill (add more side by side)
```

Rename `org-skills` / `example-skill` to real names (kebab-case, no spaces)
and keep `marketplace.json` + `plugin.json` in sync. `PLUGIN_MARKETPLACE_NAME`
in `deploy.env` **must equal** the `name` in `marketplace.json` (`org-plugins`
here) — a mismatch is untested client behavior; keep them identical. A skill
is invoked as `/<plugin>:<skill>` — the folder name under `skills/` is the
invocation name.

## Shipping updates

With `PLUGIN_MARKETPLACE_AUTO_UPDATE=true` (the deploy.env default), clients
refresh the marketplace and its installed plugins at startup — pushing to
this repository is the whole release process. Bump `version` in
`plugin.json` when you change a plugin (per the plugin docs, clients use
that field to detect updates — like all client-side behavior here, confirm
on a pilot client the first time; see `client-config.md` §6i).
