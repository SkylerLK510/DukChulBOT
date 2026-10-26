Copy every checkpoint to the desktop now, state where that stops working, and switch before the lab gets there.

Design:
1. When a job finishes writing a checkpoint on its worker, the worker copies it to `D:\checkpoints\<job>\<step>.part` on the desktop. It checks the SHA-256 and renames the copy to `<step>.ckpt`. Readers open only `.ckpt` files.
2. The desktop keeps the newest 3 checkpoints per job. The coordinator's index points each job at its newest `.ckpt`.
3. When a worker dies, the coordinator restarts its job from the newest `.ckpt` on the desktop.

Ceiling: each worker produces 2 GB every 10 minutes, about 3.3 MB/s. The desktop's link measured 112 MB/s, so it can absorb about 30 workers while staying under 80% busy. Beyond that the copy backlog grows without limit and resume points go stale, breaking the 30-minute constraint. The planned 200 workers are 6 to 7 times past this ceiling, so this design is for now, not for spring.

Switch-over:
- The coordinator records each job's copy lag (how old its newest checkpoint on the desktop is) and how busy the desktop's port is.
- **Triggers.** Switch when any of these happens:
  - the lab passes 20 workers;
  - the port averages over 50% busy for a day;
  - any job's copy lag passes 15 minutes.
- **The new mode.** On a trigger, switch to buddy replication: each worker copies to one peer, and the desktop keeps only the index and final checkpoints. The index format is the same in both modes, so the code that resumes jobs does not change.
- **Timing.** Buddy replication is built and load-tested before the rack servers arrive, not after.

Failure handling:
- A worker dies mid-copy: the `.part` file is never renamed and is swept after an hour. The job resumes from the previous checkpoint, at most 20 minutes old.
- A checksum mismatch: the copy is retried up to 3 times, then an alert is raised and the local copy kept.
- Copy lag passes 15 minutes: an alert is raised, and it counts as a switch-over trigger.

How we will know it works:
- Kill a worker during a copy. Check that its job resumes from the previous checkpoint and no `.part` file is ever opened.
- Throttle the desktop's port to 10 MB/s with three workers. Check that the copy-lag alert fires within 15 minutes.
- Before spring, load-test buddy mode with 200 simulated workers for 2 hours. Pass: every job's newest checkpoint stays under 20 minutes old.

Assumptions and evidence:
- **Measured:** the link's 112 MB/s (iperf3, Mac to desktop).
- **Arithmetic:** the per-worker 3.3 MB/s.
- **Choices, not measurements:** the 80% headroom and the triggers.
