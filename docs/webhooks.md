# Webhooks

## What is this

When a run ends, dlboard can post it to a URL: a Slack or Mattermost channel, a CI pipeline, a bot.
Each project's owners set its webhooks on the project page's **Webhooks** tab, each with the ways a run ends it is for: finished, failed, crashed (given up as lost: its process killed, its node gone), or ended without saying how.

## When you'd need this

- You want to hear when a long run ends or fails without keeping its page open.
- Something else should start when a run ends: an evaluation job, a report, a deploy.

## How it works

- **Who:** a project's owners (and admins) add and delete its webhooks; nobody else sees them, or the tab.
- **The secret:** the server makes one for each webhook and shows it once, as the webhook is added.
  It is never shown again, so copy it then.
- **When:** the server looks for runs that ended every 30 seconds (`DLBOARD_WEBHOOK_INTERVAL_S`), and posts each to every webhook of its project that asks for how it ended.
  A run that reports the same end twice (Lightning does, after `fit` and after `test`) is posted once; a run that ended before a webhook was added is not posted to it.
  Posting is done by the [custodian](background-tasks.md), so training never waits on it.
- **Retries:** a post that fails (no answer within 5 seconds, or anything but a 2xx) is tried again a minute later, then two, four, and so on.
  After 10 tries, about eight and a half hours (`DLBOARD_WEBHOOK_ATTEMPTS`), the webhook is turned off, and its tab says why; **Turn on** turns it back on.

## What is posted

```json
{
  "event": "run.finished",
  "text": "lr-3e-4 finished after 2 hours 14 minutes, in vision / sweep: https://dlboard.example/experiment/12?run=345",
  "project": {"id": 3, "name": "vision"},
  "experiment": {"id": 12, "name": "sweep"},
  "run": {"id": 345, "name": "lr-3e-4", "status": "finished", "created_at": "...", "ended_at": "...", "duration_s": 8040.0},
  "url": "https://dlboard.example/experiment/12?run=345"
}
```

`event` is `run.finished`, `run.failed`, `run.crashed` or `run.ended`, and also sent as the `X-Dlboard-Event` header.
`text` says it all in a sentence, which is all a Slack incoming webhook reads, so a Slack webhook URL works as it is.
The links start with the server's address as the owner who added the webhook reached it.

**Checking a post is dlboard's.**
`X-Dlboard-Signature: t=<unix time>,v1=<hex>` is the HMAC-SHA256, keyed with the webhook's secret, of `<unix time>.` followed by the raw body.
A receiver recomputes it, compares in constant time, and refuses a time more than a few minutes old, so a captured post can't be replayed:

```python
import hashlib, hmac, time


def is_dlboard(secret: str, header: str, body: bytes) -> bool:
    fields = dict(part.split("=", 1) for part in header.split(","))
    expected = hmac.new(secret.encode(), f"{fields['t']}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(fields["v1"], expected) and abs(time.time() - int(fields["t"])) < 300
```

## Where the server may post

A webhook makes the server send a request to a URL someone typed in, so the server refuses any URL whose host resolves to an address that isn't public: this machine, a private network, link-local (a cloud's metadata service), carrier-grade NAT (a tailnet).
It resolves the host once per post and connects to that address, so a DNS answer that changes in between can't redirect it, and it follows no redirects.

To let webhooks reach an internal server (a self-hosted Mattermost, a tailnet bot), an admin lists its host name or address in `DLBOARD_WEBHOOK_ALLOWED_HOSTS`.
The rest of the `DLBOARD_WEBHOOK_*` settings are in [Configuration](configuration.md#webhooks).

Webhooks are part of every storage bundle (`LOCAL_STORAGE`, `POSTGRES_STORAGE`, `POSTGRES_S3_STORAGE`), beside retention; a deployment that composes its own list adds `dlboard.plugins.webhooks`.
