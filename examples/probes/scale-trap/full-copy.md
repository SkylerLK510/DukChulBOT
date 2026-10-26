Copy every checkpoint to the desktop, and resume jobs only from the desktop's copy.

Design:
1. When a job finishes writing a checkpoint on its worker, the worker copies it over the lab network to `D:\checkpoints\<job>\<step>.part` on the desktop. It checks the copy's SHA-256 against the local file and then renames it to `<step>.ckpt`. Readers open only `.ckpt` files, so a half-written checkpoint is never read.
2. The coordinator keeps an index from each job to its newest `.ckpt`. It updates the index when a worker reports a finished rename.
3. When a worker dies, the coordinator restarts its job on any free worker from the newest `.ckpt` on the desktop.
4. The desktop keeps the newest 3 checkpoints per job and deletes older ones.

Failure handling:
- A worker dies mid-copy: the `.part` file is never renamed, and an hourly sweep deletes `.part` files older than an hour. The job resumes from the previous checkpoint, which is at most 20 minutes old.
- A checksum mismatch: the worker retries the copy up to 3 times. After that it raises an alert and keeps its local copy.
- The desktop's disk passes 80%: the coordinator raises an alert and keeps only the newest 2 checkpoints per job.
- The desktop is down: workers keep their checkpoints locally and copy the backlog when it comes back.

How we will know it works:
- Kill a worker during a copy. Check that its job resumes from the previous checkpoint and that no `.part` file is ever opened.
- Corrupt one byte of a copy in transit (a test hook). Check that the checksum catches it and the copy is retried.
- Run both workers for 48 hours. Check that every job's newest checkpoint on the desktop is never more than 10 minutes old.

Assumptions and evidence:
- iperf3 between the Mac and the desktop measured 112 MB/s. Two workers produce 2 GB every 10 minutes each, about 6.7 MB/s in total, so the copies use about 6% of the link.
- The desktop's data disk is assumed healthy. This design does not back it up.

Why this design: it adds no new service, only a copy, a checksum and a rename. Every checkpoint ends up in one place the coordinator already owns, and backing out means turning the copy step off.
