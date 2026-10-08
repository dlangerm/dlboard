# Upgrading

## What is this

How to move a running dlboard to a newer version without losing data. What is guaranteed to keep working
is in [compatibility.md](compatibility.md); what's new in each version is in the
[changelog](../CHANGELOG.md).

## Steps

1. **Back up first.** See [backup.md](backup.md). A backup is the only way back from a version
   you decide you don't want, or from a migration that fails.
2. **Read the changelog** for every version between yours and the target.
3. **Install the new version**: `uv tool upgrade dlboard`, `pip install -U dlboard`, or pull the new
   image tag. Pin an exact version (`dlboard==0.3.0`, an `X.Y.Z` image tag) in production rather than
   following `latest`.
4. **Restart the server.** On startup, every store upgrades its database to the newest schema before it
   takes requests. There is no separate migration command to run.
5. **Check it came up**: `/readyz` answers 200 once the database is reachable and upgraded.
6. **Upgrade training environments** (`dlboard-client`) whenever convenient. Clients on the same REST
   API major keep working against the new server.

## Several workers or replicas

Workers starting at the same time take turns upgrading the schema (on Postgres, under an advisory lock),
so starting `--workers 4`, or several containers, against a database that needs upgrading is safe.

A **rolling** upgrade, where old and new servers run side by side for a while, works for servers that
are already running: migrations are additive, so an old process keeps working against the newly
upgraded schema. What it can't do is *start*. A server only checks the schema when it boots, and an
old version refuses to boot against a database a newer one has migrated. So an old instance that
restarts or is scaled up mid-rollout fails until it's replaced by the new version, which is what a
rolling deploy does anyway. A release that can't promise even this says so in the changelog and needs
all servers stopped together.

## Going back

Don't downgrade the software against an upgraded database: an older server can't open a database
that a newer version has migrated. To roll back, restore the backup from step 1 and run the old
version against it. Anything logged after the backup is lost, so decide quickly.

## Version skew

| Client | Server | Result |
|---|---|---|
| Older | Newer, same REST API major | Works. |
| Newer | Older, same REST API major | Works, but newer features may not be there. |
| Different API major | | The client refuses to start and says which versions disagree. |

## Plugins you wrote

The Python plugin APIs are not covered by the stability promise before 1.0. Read the changelog's
"Changed" entries before upgrading a deployment with custom plugins, and run your plugins' tests against
the new version in a staging copy first.
