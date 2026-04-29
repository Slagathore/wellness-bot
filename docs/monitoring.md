# Monitoring and Maintenance

## systemd Services
```bash
journalctl -u telegram-webhook -f
journalctl -u telegram-outbox -f
journalctl -u telegram-embeddings -f
journalctl -u telegram-sentiments -f
journalctl -u telegram-nightly -f
journalctl -u telegram-runtime -f
journalctl -u telegram-admin -f
```

## Redis
```bash
redis-cli monitor
```

## Nightly Job Outputs
- Summaries: `DATA_ROOT/users/<telegram_id>/derived/analytics/`
- Emotional summaries: `DATA_ROOT/users/<telegram_id>/derived/analytics/emotional_summary_*.json`
- Backups: `DATA_ROOT/backups/`

## Log Rotation
1. Copy `/etc/logrotate.d` template for `journalctl` or set `SystemMaxUse=` in `journal.conf`.
2. Ensure `DATA_ROOT/backups/` is pruned automatically (already handled in nightly script).

## Alerting Ideas
- Hook moderation events to Slack/Email.
- Track nightly pipeline success/failure (systemd unit + `OnFailure=` handler).
