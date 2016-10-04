#!/usr/bin/python
# -*- coding: utf-8 -*-

from fnmatch import fnmatch
import re
import logging


#### Config Utils ####

log = logging.getLogger(__name__)


class Util(object):
    @staticmethod
    def _process_include_exclude(initial_set, include_set, exclude_set,
                                 not_in_initial_set_msg, not_in_result_set_msg,
                                 empty_set_msg):
        if not include_set:
            result_set = list(initial_set)
        else:
            result_set = list()
            for host in include_set:
                if host in initial_set:
                    result_set.append(host)
                else:
                    log.warn(("[{}] " + not_in_initial_set_msg)
                             .format(ClusterConfig.__name__, host))

        for host in exclude_set if exclude_set else []:
            if host in initial_set and host in result_set:
                result_set.remove(host)
            elif host not in initial_set:
                log.warn(("[{}] " + not_in_initial_set_msg)
                         .format(ClusterConfig.__name__, host))
            else:
                log.warn(("[{}] " + not_in_result_set_msg)
                         .format(ClusterConfig.__name__, host))

        if not result_set:
            log.error(("[{}] " + empty_set_msg).format(ClusterConfig.__name__))

        return result_set

    @staticmethod
    def _parse_bool_val(val, default=False):
        if val is None:
            return default

        s_val = str(val).lower()
        return not (s_val == 'false' or s_val == '0' or s_val == '')

    @staticmethod
    def _parse_int_val(val, default=0):
        if val is None:
            return default

        return int(val)

    _bin_prefix_table = {
        'b': 0,
        'K': 1,
        'M': 2,
        'G': 3,
        'T': 4,
        '' : 2
    }

    @staticmethod
    def _parse_bin_size_val(val, res_prefix='b', default=0):
        if val is None:
            return default / (1024 ** Util._bin_prefix_table[res_prefix])

        # TODO: handle invalid vals
        match = re.match(r'(\d+)([MGTkb]$|$)', str(val))
        i_val = int(match.group(1))
        bin_prefix = match.group(2)
        return (i_val * 1024**(Util._bin_prefix_table[bin_prefix]) / \
                (1024 ** Util._bin_prefix_table[res_prefix]))


#### Config Sections Helpers ####

class ConfigSection(object):
    def __init__(self, key, parent, *args, **kwargs):
        super(ConfigSection, self).__init__(*args, **kwargs)
        self.key = key
        self.parent = parent
        self.minions = parent.minions
        if key in parent.dict:
            self.dict = parent.dict[key]
        else:
            self.dict = {}

    def get_section_key(self):
        return self.key


class IncludeExcludeSection(object):
    def __init__(self, *args, **kwargs):
        super(IncludeExcludeSection, self).__init__(*args, **kwargs)
        self._expand_globs('include')
        self._expand_globs('exclude')

    def include_set(self):
        return self.dict['include']

    def exclude_set(self):
        return self.dict['exclude']

    def _expand_globs(self, set_key):
        new_set = list()
        if set_key in self.dict:
            for host in self.minions:
                for pattern in self.dict[set_key]:
                    if fnmatch(host, pattern):
                        new_set.append(host)
        self.dict[set_key] = new_set


class SectionOpts(ConfigSection):
    def __init__(self, opts_key, parent):
        super(SectionOpts, self).__init__(opts_key, parent)

    def _get_opt_int_val(self, key, default=0):
        if key in self.dict:
            return Util._parse_int_val(self.dict[key])
        return default

    def _get_opt_bool_val(self, key, default=False):
        if key in self.dict:
            return Util._parse_bool_val(self.dict[key])
        return default

    def _get_opt_bin_size_val(self, key, default=0, res_prefix='b'):
        if key in self.dict:
            return Util._parse_bin_size_val(self.dict[key], res_prefix)
        return default


#### OSDs Section ####

class OSDFilterOpts(SectionOpts):
    def __init__(self, parent):
        super(OSDFilterOpts, self).__init__('filter', parent)

    def n_disks_gt(self):
        return self._get_opt_int_val('n_disks_gt', 1)

    def disk_size_gte(self):
        return self._get_opt_bin_size_val('disk_size_gte', 0)


class OSDGlobalOpts(SectionOpts):
    def __init__(self, parent):
        super(OSDGlobalOpts, self).__init__('global', parent)

    def allow_use_ssd_for_journal(self):
        return self._get_opt_bool_val('allow_use_ssd_for_journal', True)

    def allow_use_nvme_for_journal(self):
        return self._get_opt_bool_val('allow_use_nvme_for_journal', True)

    def allow_share_data_and_journal(self):
        return self._get_opt_bool_val('allow_share_data_and_journal', True)

    def journal_size(self, res_prefix='b'):
        # 5368709120 = 5G the default osd_journal_size defined in
        # ceph/src/common/config_opts.h
        return self._get_opt_bin_size_val('journal_size', 5368709120,
                                          res_prefix)


class DeviceOpts(SectionOpts):
    def __init__(self, device, parent):
        super(DeviceOpts, self).__init__(device, parent)
        self.device = device
        self.globals = parent.globals

    def journal_only(self, is_ssd=False):
        return self._get_opt_bool_val('journal_only', is_ssd)

    def data_only(self):
        return self._get_opt_bool_val('data_only', False)

    def journal_size(self, res_prefix='b'):
        return self._get_opt_bin_size_val('journal_size',
                                         self.globals.journal_size(res_prefix),
                                         res_prefix)

    def is_disk_eligible_for_journal(self, disk_opts):
        ssd_journal_opt = self.globals.allow_use_ssd_for_journal()
        nvme_journal_opt = self.globals.allow_use_nvme_for_journal()

        if (ssd_journal_opt and disk_opts['rotational'] == '0') or \
           (nvme_journal_opt and disk_opts['Driver'] == 'nvme'):
            return not self.data_only()

        return self.journal_only()


class HostOpts(IncludeExcludeSection, SectionOpts):
    def __init__(self, host, parent):
        super(HostOpts, self).__init__(host, parent)
        self.host = host
        self.globals = parent.globals

    def device(self, device):
        return DeviceOpts(device, self)

    def journal_size(self, res_prefix='b'):
        return self._get_opt_bin_size_val('journal_size',
                                         self.globals.journal_size(res_prefix),
                                         res_prefix)


class OSDSSection(IncludeExcludeSection, ConfigSection):
    def __init__(self, config):
        super(OSDSSection, self).__init__('osds', config)
        self.osd_members_initialized = False
        self.osd_members = None
        self.filters = OSDFilterOpts(self)
        self.globals = OSDGlobalOpts(self)

    def potential_osd_members(self):
        if not self.osd_members_initialized:
            self.osd_members = self._potential_osd_members()
            self.osd_members_initialized = True
        return self.osd_members

    def _potential_osd_members(self):
        """
        Returns:
            list(string): the osds members according to the include/exclude
                          rules specified in the config file.
                          This list only specifies a superset of hosts that
                          are considered for storage nodes. It might not
                          correspond to the concrete set of osd hosts

        TODO:
            allow glob patterns in the host specification, see fnmatch
        """
        return Util._process_include_exclude(
                                   self.parent.members(), self.include_set(),
                                   self.exclude_set(),
                                   "{} is not a member of the cluster",
                                   "{} is not in the osd member list",
                                   "no osd members defined by the config file")

    def check_osd_policy(self, host, disk_list):
        """
        Checks for filter conditions on the list of disks
        Args:
            param1 (string): host identifier
            param2 (list(dict)): list of disk structures as returned by
                                 the cephdisks module
        Returns:
            bool, list(disk_dict):
                the boolean value indicates that this host can be used to
                hold OSDs, and the list represents the disks that can be
                used as OSDs
        """
        host_opts = HostOpts(host, self)
        dev_exclude_set = host_opts.exclude_set()

        # filter excluded disks
        disk_list_filtered = list()
        for disk in disk_list:
            device_file = re.search('/dev/(.*)', disk['Device File']).group(1)
            if device_file not in dev_exclude_set:
                if int(disk['Bytes']) >= self.filters.disk_size_gte():
                    disk_list_filtered.append(disk)
        disk_list = disk_list_filtered

        # filter by the n_disks_gt condition
        if len(disk_list) <= self.filters.n_disks_gt():
            return (False, None)

        return (True, disk_list_filtered)

    def host(self, host):
        return HostOpts(host, self)


#### MONs Section ####

class MONGlobalOpts(SectionOpts):
    def __init__(self, parent):
        super(MONGlobalOpts, self).__init__('global', parent)

    def allow_osd_role_sharing(self):
        return self._get_opt_bool_val('allow_osd_role_sharing', True)


class MONSSection(IncludeExcludeSection, ConfigSection):
    def __init__(self, config):
        super(MONSSection, self).__init__('mons', config)
        self.mon_members_initialized = False
        self.mon_members_set = None
        self.globals = MONGlobalOpts(self)

    def mon_members(self):
        if not self.mon_members_initialized:
            self.mon_members_set = self._mon_members()
            self.mon_members_initialized = True
        return self.mon_members_set

    def _mon_members(self):
        """
        Returns:
            list(string): the mon members according to the include/exclude
                          rules specified in the config file.

        TODO:
            allow glob patterns in the host specification, see fnmatch
        """
        return Util._process_include_exclude(
                                   self.parent.members(), self.include_set(),
                                   self.exclude_set(),
                                   "{} is not a member of the cluster",
                                   "{} is not in the mon member list",
                                   "no mon members defined by the config file")

#### Admin Section ####

class AdminsGlobalOpts(SectionOpts):
    def __init__(self, parent):
        super(AdminsGlobalOpts, self).__init__('global', parent)

    def allow_osd_role_sharing(self):
        return self._get_opt_bool_val('allow_osd_role_sharing', True)


class AdminsSection(IncludeExcludeSection, ConfigSection):
    def __init__(self, config):
        super(AdminsSection, self).__init__('admins', config)
        self.admin_members_initialized = False
        self.admin_members_set = None
        self.globals = AdminsGlobalOpts(self)

    def admin_members(self):
        if not self.admin_members_initialized:
            self.admin_members_set = self._admin_members()
            self.admin_members_initialized = True
        return self.admin_members_set

    def _admin_members(self):
        """
        Returns:
            list(string): the admin members according to the include/exclude
                          rules specified in the config file.

        TODO:
            allow glob patterns in the host specification, see fnmatch
        """
        return Util._process_include_exclude(
                                   self.parent.members(), self.include_set(),
                                   self.exclude_set(),
                                   "{} is not a member of the cluster",
                                   "{} is not in the mon member list",
                                   "no mon members defined by the config file")


#### Cluster Configuration Helper ####

class ClusterConfig(IncludeExcludeSection):
    def __init__(self, config_dict, minion_set):
        self.dict = config_dict
        self.minions = minion_set
        super(ClusterConfig, self).__init__()
        self.members_initialized = False
        self.member_set = None
        self.osds = OSDSSection(self)
        self.mons = MONSSection(self)
        self.admins = AdminsSection(self)

    def validate(self):
        """
        TODO: validate the config file
            - presence of mandatory keys
            - etc...
        """
        return True

    def name(self):
        """
        Returns:
            string: the Ceph cluster name
        """
        if 'name' in self.dict:
            return self.dict['name']
        else:
            return 'ceph'

    def members(self):
        if not self.members_initialized:
            self.member_set = self._members()
            self.members_initialized = True
        return self.member_set

    def _members(self):
        """
        Returns:
            list(string): the cluster members according to the include/exclude
                          rules specified in the config file

        TODO:
            allow glob patterns in the host specification, see fnmatch
        """
        return Util._process_include_exclude(
                               self.minions, self.include_set(),
                               self.exclude_set(),
                               "{} is not a valid salt minion",
                               "{} is not in the members list",
                               "no cluster members defined by the config file")

    def osds(self):
        return self.osds

    def mons(self):
        return self.mons

    def admins(self):
        return self.admins




## ##For testing purposes
##
## config_test = {
##     'name': 'cluster1',
##     'include': ['*'],
##     'exclude': ['mds*'],
##     'osds': {
##         'filter': {
##             'n_disks_gt': '1'
##         },
##         'global': {
##             'journal_size': '512M',
##             'allow_share_data_and_journal': 'true',
##             'allow_use_ssd_for_journal': 'true',
##             'allow_use_nvme_for_journal': 'true',
##         },
##         'data1.ceph': {
##             'sda': {
##                 'journal_only': 'true'
##             },
##             'sdb': {
##                 'data_only': 'true'
##             },
##             'vdc': {
##                 'data_only': 'true',
##                 'journal_size': '256M'
##             }
##         },
##         'data2.ceph': {
##             'exclude': [ 'vdc' ]
##         },
##         'exclude': ['data[34]*', 'mon1.ceph']
##     },
##     'mons': {
##         'global': {
##             'allow_osd_role_sharing': 'false'
##         },
##         'include': ['mon*']
##     }
## }
## c = ClusterConfig(config_test, ['admin.ceph', 'mon1.ceph', 'data1.ceph', 'data2.ceph', 'data3.ceph', 'mds1.ceph'])
## o = c.osds
## h = HostOpts('data1.ceph', o)
## print(o.globals.journal_size())
## print(h.device('vdc').journal_size())
## m = c.mons

