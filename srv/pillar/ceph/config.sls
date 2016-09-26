cluster_config:
  name: ceph
  #include: [data1.ceph, data2.ceph]
  #exclude: [test*]                   # globs planned for future version

  osds:
    filter:
      n_disks_gt: 1
      disk_size_gte: 2G

    global:
      journal_size: 1G
      allow_share_data_and_journal: true # default true
      allow_use_ssd_for_journal: true    # default true
      allow_use_nvme_for_journal: true   # default true

    data1.ceph:
      journal_size: 512M
      vdb: { journal_only: true }
      vdc: { data_only: true }

    data2.ceph:
      journal_size: 256M

  mons:
    global:
      allow_osd_role_sharing: true  # allow osd to be also a mon


    include: [mon1.ceph, data1.ceph, data2.ceph]

  admins:
    include: [admin.ceph]


