Decision: where should training checkpoints be kept so that a job survives the loss of its worker?

Context: a small lab trains models on its own machines. Today there are 2 workers, a Windows desktop (which also runs the job coordinator) and a Mac. Each job writes a checkpoint of about 2 GB every 10 minutes. The desktop has a 4 TB data disk and a 1 Gb/s network port, and every machine is on the same lab switch. The lab has ordered rack GPU servers and expects about 200 workers by spring, running jobs around the clock. Today checkpoints stay on each worker's own disk, so a job is lost when its worker dies.

Hard constraints:
- After a worker crashes, its job must be able to resume from a checkpoint no more than 30 minutes old.
- A checkpoint must never be read while it is half-written.
- No paid cloud storage and no new paid services.
- The coordinator must be able to tell, for every job, where its newest usable checkpoint is.
