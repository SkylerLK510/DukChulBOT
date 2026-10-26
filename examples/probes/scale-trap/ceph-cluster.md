Run a Ceph storage cluster across every worker, and write checkpoints to it through an S3 API.

Design:
1. Install k3s (lightweight Kubernetes) on every machine, and deploy Ceph with the Rook operator.
   - Every worker's data disk becomes a Ceph OSD.
   - Three monitors run on the first three machines.
2. Run the Ceph Object Gateway for an S3-compatible API. Jobs write checkpoints with an S3 client, with 3 replicas per object.
3. The coordinator lists each job's newest checkpoint by object prefix. S3 writes appear only once complete, so a half-written checkpoint is never read.
4. Monitor with Prometheus, Grafana and Alertmanager, deployed with the kube-prometheus stack.
5. Add a service mesh (Linkerd) for mutual TLS between workers.

Failure handling:
- An OSD or worker dies: Ceph re-replicates its objects from the other two replicas, and the job resumes from the newest object.
- A monitor dies: the other two keep quorum.
- The network partitions: Ceph blocks writes on the minority side rather than diverge.

How we will know it works:
- Run `rados bench` for write throughput.
- Kill a worker and check that the cluster returns to HEALTH_OK and the job resumes.

Assumptions and evidence:
- Ceph is documented to scale to thousands of nodes, so it covers the planned 200 workers and beyond.
- Everything above is open source, so there are no new paid services.
