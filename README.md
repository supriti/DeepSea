# DeepSea
A collection of Salt files for deploying, managing and automating Ceph.

The goal is to manage multiple Ceph clusters with a single salt master.  At this time, only a single Ceph cluster can be managed.

The [diagram](deepsea.png) should explain the intended flow for the orchestration runners and related salt states.

## Status
Automatic discovery, configuration and deployment of Ceph clusters works. RGW
deployment works for single site deployements. MDS deployment and CephFS creation works.

## Get Involved
To learn more about DeepSea, take a look at the [Wiki](https://github.com/SUSE/DeepSea/wiki).

There is also a dedicated mailing list [deepsea-users](http://lists.suse.com/mailman/listinfo/deepsea-users).
If you have any questions, suggestions for improvements or any other
feedback, please join us there! We look forward to your contribution.

If you think you've found a bug or would like to suggest an enhancement, please submit it via the [bug tracker](https://github.com/SUSE/DeepSea/issues/new) on GitHub.

For contributing to DeepSea, refer to the [contribution guidelines](https://github.com/suse/deepsea/blob/master/contributing.md)

## Usage
### Prepare Salt
- Install salt-master on one host
- Install salt-minion on all hosts including the master.
- Accept keys (e.g. `salt-key -A -y`)

### Install DeepSea
- Install [rpm](https://build.opensuse.org/package/show/home:swiftgist/deepsea)
- For non-RPM-distros, try `make install`.

### Configure
- Edit [/srv/pillar/ceph/master_minion.sls](srv/pillar/ceph/master_minion.sls)

### Steps
- Run `salt-run state.orch ceph.stage.0` or `salt-run state.orch ceph.stage.prep`
- Run `salt-run state.orch ceph.stage.1` or `salt-run state.orch ceph.stage.discovery`
- Create `/srv/pillar/ceph/proposals/policy.cfg`.  Examples are [here](doc/examples)
- Run `salt-run state.orch ceph.stage.2` or `salt-run state.orch ceph.stage.configure`
- Run `salt-run state.orch ceph.stage.3` or `salt-run state.orch ceph.stage.deploy`
- Run `salt-run state.orch ceph.stage.4` or `salt-run state.orch ceph.stage.services`

### Details on policy.cfg
The discovery stage (or stage 1) creates many configuration proposals under
`/srv/pillar/ceph/proposals`. The files contain configuration options for Ceph
clusters, potential storage layouts and role assignments for the cluster
minions. The policy.cfg specifies which of these files and options are to be
used for the deployment.

Please refer to the [Policy wiki page](https://github.com/SUSE/DeepSea/wiki/policy)
for more detailed information.

## Test intial deployment and generating load
Once a cluster is deployed one might want to verify functionality or run
benchmarks to verify the cluster works as expected.
- In order to gain some confidence in your cluster after the inital deployment
  (stage 3) run `salt-run state.orch ceph.benchmarks.baseline`. This runs an osd
  benchmark on each OSD and aggregates the results. It reports your average OSD
  performance and points out OSDs that deviate from the average. *Please note
  that for now the baseline benchmark assumes all uniform OSDs.*
- To load test CephFS run `salt-run state.orch ceph.benchmarks.cephfs`.
  This requires a running MDS (deploy in stage 4) and at least on minion with
  the mds-client role. The cephfs_benchmark stage will then mount the CephFS
  instance on the mds-client and run a bunch of fio tests. See the [benchmark
  readme](srv/pillar/ceph/benchmark/README.md) for futher details.
- *more to come*
