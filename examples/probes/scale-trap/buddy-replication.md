Copy each checkpoint to one buddy worker. The desktop keeps only the index and each job's final checkpoint.

Sizing first: at the planned 200 workers, each writing 2 GB every 10 minutes, checkpoints add up to 200 × 2 GB / 600 s ≈ 667 MB/s. The desktop's port carries at most about 112 to 125 MB/s, and each worker produces about 3.3 MB/s. So any design that sends every checkpoint through the desktop stops keeping up at about 35 workers, after which its backlog grows without limit and resume points go stale. This design therefore moves checkpoint bytes between workers and keeps only metadata on the desktop.

Design:
1. Each worker keeps its own newest 2 checkpoints on its disk.
2. Each worker has a buddy, assigned by the coordinator (worker i copies to worker i+1). After writing a checkpoint, a worker copies it to its buddy as `<job>/<step>.part`, checks the SHA-256, and renames it to `<step>.ckpt`. Readers open only `.ckpt` files.
   - Each worker sends and receives about 3.3 MB/s.
   - The switch carries all pairs in parallel.
3. The coordinator's index records, per job, the newest verified checkpoint and the two machines that hold it. That is a few hundred bytes per checkpoint.
4. When a worker dies, the coordinator restarts its job on the buddy from the newest `.ckpt` there.
5. When a job ends, its final checkpoint is copied to the desktop once.

Failure handling:
- A worker dies mid-copy: the buddy's `.part` is never renamed and is swept after an hour. The job resumes from the previous checkpoint on the buddy, which is at most 20 minutes old.
- A buddy is down or full: the coordinator assigns the next worker as buddy. Until a copy completes there, the dashboard marks the job "unprotected".
- A worker and its buddy die together: the job cannot resume. The coordinator reports it as lost rather than resuming from an older copy. Buddies are assigned on different power strips to make this rare.

How we will know it works:
- Before the servers arrive, run a scaled load test: 200 simulated workers, as processes on the existing machines, each writing a 2 GB dummy checkpoint every 10 minutes to its buddy.
  - Run it for 2 hours.
  - Pass: every job's newest replicated checkpoint stays at most 20 minutes old throughout, and the desktop's port stays under 10% busy.
- Kill a worker during a copy. Check that its job resumes on the buddy from the previous checkpoint.
- Kill a worker and its buddy together. Check that the job is reported as lost, not resumed from a stale copy.

Assumptions and evidence:
- **Measured:** the 112 MB/s link figure (iperf3, Mac to desktop).
- **Arithmetic:** the 3.3 MB/s per worker comes from the stated checkpoint size and interval.
- **Assumed:** the lab switch can carry all 200 ports at once. This is to be checked against its spec sheet before the servers are racked; if it can't, buddies are paired on the same switch.
- **What would change this:** if checkpoints shrink below about 60 MB, or the desktop gets a 10 Gb/s port, sending everything to the desktop becomes viable again.
