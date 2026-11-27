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
  Each end is queued as it is saved, so none is passed over, and a post comes within a minute of the run's end.
  A run that reports the same end twice (Lightning does, after `fit` and after `test`) is posted once; a run that ended before a webhook was added is not posted to it.
  Posting is done by the [custodian](background-tasks.md), so training never waits on it.
- **Retries:** a post that may land later (no answer within 5 seconds, a 408, a 429 or a 5xx) is tried again a minute later, then two, four, and so on (`DLBOARD_CUSTODIAN_RETRY_FIRST_S`, up to `DLBOARD_CUSTODIAN_RETRY_MAX_S`).
  After 10 tries (`DLBOARD_WEBHOOK_ATTEMPTS`), with the defaults about eight and a half hours, the webhook is turned off, and its tab says why; **Turn on** turns it back on.
  A 404 or 410 says the endpoint is gone (a deleted Slack webhook answers so), and turns the webhook off at once.
  Any other answer that isn't a 2xx (a 400, a 403, a redirect) refuses that one post, which would be refused again: it is dropped, and the webhook stays on.
  Runs that end while a webhook is off are not sent to it, then or once it is turned on again.
- **Never late:** a run's end is posted within two days of when it ended (`DLBOARD_WEBHOOK_EXPIRE_H`), or not at all.
  One the server couldn't post by then, because it was down that long, is dropped, and the server logs how many.
  The server refuses to start with an expiry shorter than a failing webhook's tries take, so a webhook that keeps failing is still turned off rather than going quiet.
- **Repeats:** every try of a post has the same body, the run as it was when it ended, and the same `X-Dlboard-Delivery` id, so a receiver that got a post whose answer was lost can drop the repeat.

## What is posted

```json
{
  "event": "run.finished",
  "text": "lr-3e-4 finished after 2 hours 14 minutes, in vision / sweep: https://dlboard.example/experiment/12?run=345",
  "project": {"id": 3, "name": "vision"},
  "experiment": {"id": 12, "name": "sweep"},
  "run": {"id": 345, "name": "lr-3e-4", "status": "finished", "created_at": "...", "ended_at": "...", "done_at": "...", "duration_s": 8040.0},
  "url": "https://dlboard.example/experiment/12?run=345"
}
```

`event` is `run.finished`, `run.failed`, `run.crashed` or `run.ended`, and also sent as the `X-Dlboard-Event` header.
`run.ended_at` is when the run ended by its own word (for a crashed run, when it was last heard from); `run.done_at` is when the server recorded the end.
`text` says it all in a sentence, which is all a Slack incoming webhook reads, so a Slack webhook URL works as it is.
The links start with `DLBOARD_WEBHOOK_PUBLIC_URL` if an admin set it, and otherwise with the server's address as the owner who added the webhook reached it.

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
For the same reason a post never goes through an HTTP proxy (`HTTPS_PROXY` is ignored), which would connect to the host by its name.
It trusts the system's CAs and the bundle `requests` ships, so an internal server with a private CA works once that CA is in the system's trust store.

To let webhooks reach an internal server (a self-hosted Mattermost, a tailnet bot), an admin lists its host name or address in `DLBOARD_WEBHOOK_ALLOWED_HOSTS`.
The rest of the `DLBOARD_WEBHOOK_*` settings are in [Configuration](configuration.md#webhooks).

Webhooks are part of every storage bundle (`LOCAL_STORAGE`, `POSTGRES_STORAGE`, `POSTGRES_S3_STORAGE`), beside retention; a deployment that composes its own list adds `dlboard.plugins.webhooks`.

## Taking webhooks off a deployment

Delete each project's webhooks on its tab first, then take `dlboard.plugins.webhooks` off the plugin list.
A webhook is kept in the database, its secret with it, until it is deleted, and without the plugin nothing can show or delete it.
Taken off with webhooks still set, they stay, unseen, and work again as they were once the plugin is listed again.
What the server had queued to post is dropped then, not posted, if its run ended more than `DLBOARD_WEBHOOK_EXPIRE_H` before.
A general way to find and clear what a removed plugin leaves behind is [#813](https://github.com/dlangerm/dlboard/issues/813).
