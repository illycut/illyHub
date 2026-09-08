# Sync engine

How the hub plays the same Tidal content on a HEOS side and a Sonos side "at the same moment"
and keeps them together. API surface: `docs/api.md` → Sync Play. Code: `hub/src/illyhub_hub/sync.py`.

## Constraints that shape the design

1. **HEOS cannot seek.** The HEOS CLI protocol has no seek command (PRD review §6a). Any
   correction therefore happens on Sonos. HEOS is the **clock master**; Sonos is the **follower**.
   The engine never touches the master after it starts playing.
2. **Whole-second position reports.** Both ecosystems report position at 1 s resolution. The
   `PositionTracker` (positions.py) turns tick edges into sub-second estimates with a confidence
   value; the engine extrapolates from the last report using the hub clock.
3. **Seek is slow and imprecise.** A Sonos seek re-buffers (hundreds of ms). The engine seeks the
   follower to `master_position + lookahead`. Lookahead starts at 400 ms; after each correction
   lands, the residual drift reveals the true seek latency (`latency = lookahead − residual`) and
   becomes the next lookahead, and one gap-exempt follow-up correction settles a residual that is
   still over threshold. Against the fakes (instant seeks) the first landing therefore drives the
   lookahead to 0 and the settle correction re-locks within a sample.
4. **Two sides only.** N players per vendor are grouped natively first; the engine syncs the two
   coordinators. Sonos-to-Sonos sync is sample-accurate on its own; the hub only bridges vendors.

## Phases

```
resolving → priming → verifying → starting → locked ⇄ drifting → correcting → locked
                                                  ↘ lost (retry re-primes the follower)
                                                  ↘ stopped (user stop or playback ended)
```

- **resolving**: Tidal ref → track list via the service layer; availability per side; refuse
  non-Tidal content (`unsupported_content`), unlinked hub (`needs_link`), a side whose vendor app
  lacks Tidal (`not_available_on_side`).
- **priming**: `PlaybackAdapter.prime_content()` on both coordinators, in parallel. Sonos: clear
  queue, add the tracks, `play_from_queue(index, start=False)` (queue selected, positioned, not
  playing). HEOS: `player/clear_queue`, `browse/add_to_queue` with *add to end* (aid 3, does not
  start playback), wait until `get_queue` shows the start position. Both return what they loaded
  at the start position (track id and/or title).
- **verifying**: each side's primed description is checked against *its own* expectation
  (`tracks[index]`): HEOS media id or Sonos URI mapped to the canonical Tidal id when the
  namespace allows, else the title. Vendor ids are never compared with each other. Mismatch →
  `sync_mismatch`, nothing starts. Priming runs in parallel; if one side fails after the other
  rebuilt its queue, the error says so, and a Sonos queue is put back from a SoCo `Snapshot`.
- **starting**: per-vendor command round-trip is a rolling average of the last 10 adapter calls
  the router or the engine made (`LatencyTracker`). Play goes to the slower side first, then
  after `slow − fast` ms to the faster one. HEOS starts via `play_queue(index+1)`, Sonos via
  `play()`. The first position report from each side gives its start instant
  (`reported_at − position`); the difference is `start_delta_ms`.
- **locked / drifting / correcting**: every `monitor_s` the engine samples both extrapolated
  positions, skipping any sample where either side's report is only an estimate (confidence
  0.5 right after a seek or track change). `drift = follower − master`. After `samples`
  consecutive samples over `drift_ms`, and at most once per `correction_gap_s`, it seeks the
  follower to `master + lookahead` (`correcting`) and waits for the first follower report after
  the seek. The residual then reveals the true seek latency (`latency = lookahead − residual`),
  which becomes the next lookahead, and arms **one** gap-exempt settle correction per
  over-threshold episode (armed on landing, spent by the next correction, re-armed only after
  drift has dropped back under threshold). A correction that is never confirmed is time-boxed
  at twice the gap; judgement resumes and the lookahead is left alone.
- **track boundary**: each side's index in the track list comes from its own identity
  (`content_ref`, else title + artist). Master first: the follower must show the same track within
  `track_grace_s`, else it is re-primed there. Follower first (it is on `index + 1`): the engine
  waits for the master and never seeks the follower backwards into the previous track; if the
  master does not follow within the grace, the follower is re-primed at the master's track. Any
  other divergence is treated the same way after the grace. Drift is not judged during a
  boundary window.
- **lost**: master or follower paused/stopped outside the hub, or its adapter left `connected`.
  `reason` says which. `POST /api/sync/retry` re-primes the follower from the master's current
  track; if the master is not playing, both restart from that track.
- **stopped**: `POST /api/sync/stop` releases both sides without stopping playback. It works in
  every phase: mid-start it cancels the start task and leaves the sides as they are. A session
  also ends `stopped` when the master finishes the last track ("playback ended"), when a hub
  `play` lands on either synced side ("playback changed"), or when a new Sync Play supersedes it.
  Retry is only for `lost` sessions.
- **transport during a session**: the client addresses the master side; the engine mirrors
  play/pause/next/prev/stop to the follower from the router's acks (`docs/api.md` → Transport
  during Sync Play). Paused-through-the-hub is not "lost"; resume waives the correction gap once.

## Tuning

`SyncConfig` is hot-reloadable (`POST /api/sync/config`). Every session writes
`HUB_DATA_DIR/sync/{id}.csv` (`t,master_pos,follower_pos,drift,action`); `GET /api/sync/sessions/{id}/report`
returns mean / p95 / max |drift|, corrections, start delta and duration. The PRD targets are start
delta under 200 ms and drift held under 300 ms on Tidal content.

## What only hardware can tell us

Everything here runs against the fakes (independent clocks with a configurable drift rate and
seek latency). Unverified until the hub Mac runs it: the real HEOS queue-fill latency after
`add_to_queue`, Sonos `play_from_queue(start=False)` leaving the position at 0, actual seek
latency, and whether Tidal allows two concurrent streams on one account (ai-dev #9 — if it does
not, Sync Play needs two accounts, one per ecosystem).
