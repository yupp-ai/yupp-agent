# Upgrade ahs-mono-prod VM: 4GB → 8GB RAM

**Date:** 2026-04-22
**Target:** `ahs-mono-prod` in `us-east5-a` (project `yupp-agent`)
**Expected downtime:** ~1–2 minutes
**Console:** https://console.cloud.google.com/compute/instancesDetail/zones/us-east5-a/instances/ahs-mono-prod?project=yupp-agent

## Pre-flight checks

1. Confirm current machine type (likely `e2-medium`):
   ```bash
   gcloud compute instances describe ahs-mono-prod \
     --zone=us-east5-a --project=yupp-agent \
     --format='value(machineType)'
   ```

2. Check whether the external IP is static or ephemeral:
   ```bash
   gcloud compute instances describe ahs-mono-prod \
     --zone=us-east5-a --project=yupp-agent \
     --format='value(networkInterfaces[0].accessConfigs[0].natIP,networkInterfaces[0].accessConfigs[0].name)'
   ```
   If ephemeral, the IP may change on start. Promote to static beforehand if anything external (Slack callback URLs, DNS, webhooks) depends on it.

3. Pick the target machine type:
   - **`e2-standard-2`** — 2 vCPU / 8GB, dedicated vCPUs. Recommended.
   - **`e2-custom-2-8192`** — keep shared-core pricing behavior closer to medium while doubling RAM.
   - **`n2-standard-2`** — 2 vCPU / 8GB, slightly better perf, higher cost.

4. Announce maintenance window (Slack bot will be offline during the stop/start).

## Execution

```bash
# 1. Stop the VM (required — set-machine-type fails on a running instance)
gcloud compute instances stop ahs-mono-prod \
  --zone=us-east5-a --project=yupp-agent

# 2. Resize
gcloud compute instances set-machine-type ahs-mono-prod \
  --zone=us-east5-a --project=yupp-agent \
  --machine-type=e2-standard-2

# 3. Start
gcloud compute instances start ahs-mono-prod \
  --zone=us-east5-a --project=yupp-agent
```

## Post-flight verification

1. Confirm the new machine type:
   ```bash
   gcloud compute instances describe ahs-mono-prod \
     --zone=us-east5-a --project=yupp-agent \
     --format='value(machineType)'
   ```

2. SSH in and check memory + services:
   ```bash
   gcloud compute ssh ahs-mono-prod --zone=us-east5-a --project=yupp-agent

   # On the VM:
   free -h                              # expect ~8GB total
   systemctl status ahs-mono            # monolith service (AHS + SAG + MCP)
   systemctl status postgresql redis    # data deps if running on-box
   journalctl -u ahs-mono -n 100 --no-pager
   ```

3. Smoke test from outside:
   - Hit the monolith health endpoint.
   - Send a test message to the Slack bot.
   - Confirm the war-room UI loads.

## Rollback

If something misbehaves, revert to the original machine type:
```bash
gcloud compute instances stop ahs-mono-prod --zone=us-east5-a --project=yupp-agent
gcloud compute instances set-machine-type ahs-mono-prod \
  --zone=us-east5-a --project=yupp-agent \
  --machine-type=e2-medium   # or whatever was returned by the pre-flight describe
gcloud compute instances start ahs-mono-prod --zone=us-east5-a --project=yupp-agent
```

## Notes

- Boot disk, data disks, internal IP, and metadata persist across the resize.
- Ephemeral external IPs can change on stop/start. Static IPs are preserved.
- VM cost roughly doubles for the compute line item (`e2-standard-2` ≈ 2× `e2-medium`).
- `ahs-mono` is wired as a systemd unit, so services should come up on boot automatically.
