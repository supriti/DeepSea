#!/usr/bin/python

import salt.client
import salt.key
import salt.config
import salt.utils
import salt.utils.minions

import re
import pprint
import string
import random
from subprocess import call, Popen, PIPE
import yaml
import json
from os.path import dirname, basename, isdir
import os
import struct
import time
import base64
import errno
import uuid
import ipaddress
import logging

from config import ClusterConfig


"""
WHY THIS RUNNER EXISTS:

For a set of servers, multiple Ceph configurations are possible.  Enumerating
all of them would generate so many that the useful would be lost in the noise.
Rather than following a template of a contrived example, this utility creates
all the possible configuration files for each server of the existing equipment.  This should help those that can never seem to get their YAML indentation correct.

Second, all the complexity of combining these files is kept in a policy.cfg at
the root of /srv/pillar/ceph/proposals.  Assigning multiple roles to the same
server or keeping them separate is controlled by specifying which files to 
include in the policy.cfg.  Preinstalling a policy.cfg will allow the automatic
creation of a Ceph cluster.

See the partner runner push.proposal for details.

"""

log = logging.getLogger(__name__)


class NetworkDiscovery(object):
    def __init__(self, settings):
        self.settings = settings
        self.minions = settings.minions
        self.networks = self._networks()
        self.public_network, self.cluster_network = self._public_cluster()

    def public_interface(self, host):
        """
        Find the public interface for a server
        """
        public_net = ipaddress.ip_network(u'{}'.format(self.public_network))
        for entry in self.networks[public_net]:
            if entry[0] == host:
                log.debug("Public interface for {}: {}".format(host, entry[2]))
                return entry[2]
        return ""

    def _networks(self):
        """
        Create a dictionary of networks with tuples of minion name, network
        interface and current address.  (The network interface is not
        currently used.)
        """
        networks = {}
        local = salt.client.LocalClient()
        interfaces = local.cmd('*' , 'network.interfaces')

        for minion in interfaces.keys():
            for nic in interfaces[minion]:
                if 'inet' in interfaces[minion][nic]:
                    for addr in interfaces[minion][nic]['inet']:
                        if addr['address'].startswith('127'):
                            # Skip loopbacks
                            continue
                        cidr = self._network(addr['address'], addr['netmask'])
                        if cidr in networks:
                            networks[cidr].append((minion, nic, addr['address']))
                        else:
                            networks[cidr] = [ (minion, nic, addr['address']) ]
        return networks


    def _network(self, address, netmask):
        """
        Return CIDR network
        """
        return ipaddress.ip_interface(u'{}/{}'.format(address, netmask)).network

    def _public_cluster(self):
        """
        Guess which network is public and which network is cluster. The
        public network should have the greatest quantity since the cluster
        network is not required for some roles.  If those are equal, pick
        the lowest numeric address.

        Other strategies could include prioritising private addresses or
        interface speeds.  However, this will be wrong for somebody.
        """
        priorities = []
        for network in self.networks:
            quantity = len(self.networks[network])
            # Minimum number of nodes, ignore other networks
            if quantity > 3:
                priorities.append( (len(self.networks[network]), network) )
                log.debug("Including network {}".format(network))
            else:
                log.warn("Ignoring network {}".format(network))

        if not priorities:
            raise ValueError("No network exists on at least 4 nodes")

        priorities = sorted(priorities, cmp=NetworkDiscovery.network_sort)
        if len(priorities) == 1:
            return str(priorities[0][1]), str(priorities[0][1])
        else:
            return str(priorities[0][1]), str(priorities[1][1])

    @staticmethod
    def network_sort(a, b):
        """
        Sort quantity descending and network ascending.
        """
        if a[0] < b[0]:
            return 1
        elif a[0] > b[0]:
            return -1
        else:
            return cmp(a[1], b[1])


class OSDDiscovery(object):
    def __init__(self, settings):
        self.settings = settings
        self.osds = {}
        self._discover()
        self._assign_osds()

    def _discover(self):
        config = self.settings.cluster_config
        for host in config.osds.potential_osd_members():
            disk_info = salt.utils.minions.mine_get(host, 'cephdisks.list',
                                                    'glob',
                                                    self.settings.__opts__)
            disk_list = disk_info[host] if host in disk_info else []

            valid, disk_list = config.osds.check_osd_policy(host, disk_list)
            if valid:
                self.osds[host] = { 'disk_list': disk_list }

    def _assign_osds(self):
        for host in self.osds.keys():
            self._assign_host_osds(host)

    def _assign_host_osds(self, host):
        config = self.settings.cluster_config.osds.host(host)
        journals = list()
        host_map = self.osds[host]
        host_map['osd_map'] = list()

        for disk in host_map['disk_list']:
            dev_file = OSDDiscovery._dev_short_id(disk)
            if config.device(dev_file).is_disk_eligible_for_journal(disk):
                journals.append(disk)

        if not journals and \
           not config.globals.allow_share_data_and_journal():
            log.warning(("[{}] Host {}: There are no disks available for "
                       "journals according to the configuration policies, and "
                       "sharing data and journal in the same device is not "
                       "allowed").format(OSDDiscovery.__name__, host))
            return

        if not journals:
            # TODO: check if journal + data size fits in device capacity
            for disk in self.osds[host]['disk_list']:
                j_size = config.device(
                               OSDDiscovery._dev_short_id(disk)).journal_size()
                size = int(disk['Bytes'])
                if size / (j_size * 1.0) >= 2:
                    host_map['osd_map'].append(OSDDiscovery._device_id(disk))
                else:
                    log.warn("[{}] Host: {} device {} does not have enough "
                             "space to hold the data and journal. device_size={} "
                             " journal_size={} device_size/journal_size < 2"
                             .format(OSDDiscovery.__name__, host,
                                     OSDDiscovery._device_id(disk), size,
                                     j_size))

            return

        for journal in journals:
            host_map['disk_list'].remove(journal)

        sorted(journals, cmp=OSDDiscovery._journal_disk_cmp)
        sorted(host_map['disk_list'], cmp=self._disk_cmp(host))

        self._assign_host_osds_journals(host, journals, host_map['disk_list'])

        log.debug("[{}] Host: {} OSDMap: {}".format(OSDDiscovery.__name__,
                                                    host, host_map['osd_map']))

    def _assign_host_osds_journals(self, host, journals, data_disks):
        config = self.settings.cluster_config.osds.host(host)

        host_map = self.osds[host]
        host_map['osd_map'] = list()

        j_idx = 0
        j_part = 0
        j_size = int(journals[j_idx]['Bytes'])
        for disk in data_disks:
            dev = OSDDiscovery._dev_short_id(disk)
            size = config.device(dev).journal_size()
            if j_size >= size:
                j_size -= size
                j_part += 1
                host_map['osd_map'].append({
                    'data': OSDDiscovery._device_id(disk, 1),
                    'journal': OSDDiscovery._device_id(journals[j_idx], j_part)
                })
            else:
                if j_idx+1 < len(journals):
                    j_part = 1
                    j_idx += 1
                    j_size = int(journals[j_idx]['Bytes'])
                elif config.globals.allow_share_data_and_journal():
                    # no more space in journal device, assigning remaining
                    # disks to share data and journal
                    js = size
                    s = int(disk['Bytes'])
                    if s / (js * 1.0) >= 2:
                        host_map['osd_map'].append(OSDDiscovery._device_id(disk))
                    else:
                        log.warn("[{}] Host: {} device {} does not have enough "
                          "space to hold the data and journal. device_size={} "
                          " journal_size={} device_size/journal_size < 2"
                          .format(OSDDiscovery.__name__, host,
                                  OSDDiscovery._device_id(disk), size, j_size))

                else:
                    log.warn("[{}] Host: {} cannot allocate a journal device "
                             "to disk {}".format(OSDDiscovery.__name__,
                                        host, OSDDiscovery._device_id(disk)))

        if j_idx+1 < len(journals):
            # allocate journal devices to unused journal disks
            for i in range(j_idx+1, len(journals)):
                disk = journals[i]
                dev = OSDDiscovery._dev_short_id(disk)
                size = config.device(dev).journal_size()
                if j_size >= size:
                    j_size -= size
                    j_part += 1
                    host_map['osd_map'].append({
                      'data': OSDDiscovery._device_id(disk, 1),
                      'journal': OSDDiscovery._device_id(journals[j_idx], j_part)
                    })
                elif config.globals.allow_share_data_and_journal():
                    # no more space in journal device, assigning remaining
                    # disks to share data and journal
                    js = size
                    s = int(disk['Bytes'])
                    if s / (js * 1.0) >= 2:
                        host_map['osd_map'].append(OSDDiscovery._device_id(disk))
                    else:
                        log.warn("[{}] Host: {} device {} does not have enough "
                          "space to hold the data and journal. device_size={} "
                          " journal_size={} device_size/journal_size < 2"
                          .format(OSDDiscovery.__name__, host,
                                  OSDDiscovery._device_id(disk), size, j_size))
                else:
                    log.warn("[{}] Host: {} cannot allocate a journal device "
                             "to disk {}".format(OSDDiscovery.__name__,
                                        host, OSDDiscovery._device_id(disk)))

    def _disk_cmp(self, host):
        config = self.settings.cluster_config.osds.host(host)
        def _cmp_fun(d1, d2):
            dev1 = re.search('/dev/(.*)', d1['Device File']).group(1)
            dev2 = re.search('/dev/(.*)', d2['Device File']).group(1)
            return config.device(dev1).journal_size() - \
                   config.device(dev2).journal_size()
        return _cmp_fun

    @staticmethod
    def _journal_disk_cmp(jn1, jn2):
        res = jn1['rotational'] - jn2['rotational']
        if not res:
            if jn1['Driver'] == 'nvme' and jn2['Driver'] != 'nvme':
                res = -1
            elif jn1['Driver'] != 'nvme' and jn2['Driver'] == 'nvme':
                res = 1
            else:
                res = int(jn2['Bytes']) - int(jn1['Bytes'])

        return res

    @staticmethod
    def _device_id(disk, part=None):
        """
        Default to Device File value.  Use by-id if available.
        """
        device_id = disk['Device File']
        if part:
            device_id = device_id + str(part)
        if 'Device Files' in disk:
            for path in disk['Device Files'].split(', '):
                if 'by-id' in path:
                    device_id = path
                    if part:
                        device_id = device_id + "-part" + str(part)
                    break
        return device_id

    @staticmethod
    def _dev_short_id(disk):
        return re.search('/dev/(.*)', disk['Device File']).group(1)

    def print_osds(self):
        out_data = {'osds': {}}
        for osd in self.osds.keys():
            out_data['osds'][osd] = self.osds[osd]['osd_map']
        salt.output.display_output(out_data, '', self.settings.__opts__)

    def save_osds(self, writer):
        for host in self.osds.keys():
            host_opts = self.settings.cluster_config.osds.host(host)
            osd_map = self.osds[host]['osd_map']
            if osd_map:
                contents = {
                    'storage': {
                        'data+journals': list(),
                        'osds': list()
                    },
                    'osd_journal_size': host_opts.journal_size('M')
                }
                for entry in osd_map:
                    if isinstance(entry, dict):
                        contents['storage']['data+journals'].append({
                            entry['data']: entry['journal']
                        })
                    else:
                        contents['storage']['osds'].append(entry)
                model_dir = "{}/disks/stack/default/{}/minions".format(
                                          self.settings.root_dir,
                                          self.settings.cluster_config.name())
                if not os.path.isdir(model_dir):
                    create_dirs(model_dir, self.settings.root_dir)
                filename = model_dir + "/" + host + ".yml"
                writer.write(filename, contents)

                role_dir = "{}/disks/cluster".format(self.settings.root_dir)
                if not os.path.isdir(role_dir):
                    create_dirs(role_dir, self.settings.root_dir)
                filename = "{}/{}.sls".format(role_dir, host)
                writer.write(filename, { 'roles': ['storage'] })


class MONSDiscovery(object):
    def __init__(self, settings, osds, network):
        self.settings = settings
        self.osds = osds
        self.network = network
        self.mons = list()
        self._discover()

    def _discover(self):
        mons = self.settings.cluster_config.mons.mon_members()
        if not self.settings.cluster_config.mons.globals.allow_osd_role_sharing():
            for host in self.osds.keys():
                mons.remove(host)
        self.mons = mons

    def save_mons(self, writer):
        role_dir = "{}/role-mon/cluster".format(self.settings.root_dir)
        if not os.path.isdir(role_dir):
            create_dirs(role_dir, self.settings.root_dir)
        for host in self.mons:
            filename = "{}/{}.sls".format(role_dir, host)
            writer.write(filename, { 'roles': ['mon'] })

        network_dir = "{}/role-mon/stack/default/{}/minions".format(
                self.settings.root_dir, self.settings.cluster_config.name())
        if not os.path.isdir(network_dir):
            create_dirs(network_dir, self.settings.root_dir)
        for host in self.mons:
            filename = "{}/{}.yml".format(network_dir, host)
            writer.write(filename, {
                'public_address': self.network.public_interface(host)
            })

    def print_mons(self):
        salt.output.display_output({'mons': self.mons}, '',
                                   self.settings.__opts__)

class AdminsDiscovery(object):
    def __init__(self, settings, osds, mons):
        self.settings = settings
        self.osds = osds
        self.mons = mons
        self._discover()

    def _discover(self):
        config = self.settings.cluster_config
        admins = config.admins.admin_members()

        if not config.admins.globals.allow_osd_role_sharing():
            for host in self.osds.keys():
                admins.remove(host)

        # by convention all mons are admins
        for host in self.mons:
            if host not in admins:
                admins.append(host)

        self.admins = admins

    def save_admins(self, writer):
        role_dir = "{}/role-admin/cluster".format(self.settings.root_dir)
        if not os.path.isdir(role_dir):
            create_dirs(role_dir, self.settings.root_dir)
        for host in self.admins:
            filename = "{}/{}.sls".format(role_dir, host)
            writer.write(filename, { 'roles': ['admin'] })

    def print_admins(self):
        salt.output.display_output({'admins': self.admins}, '',
                                   self.settings.__opts__)



class Settings(object):
    """
    Common settings
    """

    def __init__(self):
        """
        Assign root_dir, salt __opts__ and stack configuration.  (Stack
        configuration is not used currently.)
        """
        __opts__ = salt.config.client_config('/etc/salt/master')
        self.__opts__ = __opts__

        for ext in __opts__['ext_pillar']:
            if 'stack' in ext:
                self.stack = ext['stack']
        self.root_dir = "/srv/pillar/ceph/proposals"
        local = salt.client.LocalClient()
        self.pillar_data = local.cmd('*', 'pillar.items', [], expr_form="glob")
        self.pillar_data = self.pillar_data[self.pillar_data.keys()[0]]
        self.minions = local.cmd('*' , 'grains.get', [ 'id' ])
        self.cluster_config = ClusterConfig(
                self.pillar_data['cluster_config'], self.minions)


class SaltWriter(object):
    """
    All salt files are essentially yaml files in the pillar by default.  The
    pillar uses sls extensions and stack.py uses yml.
    """

    def __init__(self, **kwargs):
        """
        Keep yaml human readable/editable.  Disable yaml references.
        """
        self.dumper = yaml.SafeDumper
        self.dumper.ignore_aliases = lambda self, data: True

        if 'overwrite' in kwargs:
            self.overwrite = kwargs['overwrite']
        else:
            self.overwrite = False

    def write(self, filename, contents):
        """
        Write a yaml file in the conventional way
        """
        if self.overwrite or not os.path.isfile(filename):
            log.info("Writing {}".format(filename))
            with open(filename, "w") as yml:
                yml.write(yaml.dump(contents, Dumper=self.dumper,
                                              default_flow_style=False))


class CephCluster(object):
    """
    Generate cluster assignment files
    """

    def __init__(self, settings, writer, **kwargs):
        """
        Track cluster names, set minions to actively responding minions

        Allow overriding of default cluster
        """
        self.root_dir = settings.root_dir
        if 'cluster' in kwargs:
            self.names = kwargs['cluster']
        else:
            self.names = [ settings.cluster_config.name() ]
        self.writer = writer

        self.settings = settings
        self.minions = settings.minions

        # Should we add a master_minion lookup and have two calls instead?
        local = salt.client.LocalClient()
        _rgws = local.cmd('*' , 'pillar.get', [ 'rgw_configurations' ])
        for node in _rgws.keys():
            self.rgw_configurations = _rgws[node]
            # Just need first
            break

    def generate(self):
        """
        Create cluster assignment for every cluster and unassigned
        """
        self._assignments()
        self._global()


    def _assignments(self):
        """
        Create cluster assignment for every cluster and unassigned
        """
        members = self.settings.cluster_config.members()

        for minion in members:
            self._write_assignment(minion, self.names[0])

        for minion in self.settings.minions:
            if minion not in members:
                self._write_assignment(minion, 'unassigned')

    def _write_assignment(self, host, cluster):
        cluster_dir = "{}/cluster-{}/cluster".format(self.root_dir,
                                                  cluster)
        if not os.path.isdir(cluster_dir):
            create_dirs(cluster_dir, self.root_dir)
        filename = "{}/{}.sls".format(cluster_dir, host)
        contents = {}
        contents['cluster'] = cluster
        self.writer.write(filename, contents)

    def _global(self):
        """
        Specify global options for all clusters
        """
        stack_dir = "{}/config/stack/default".format(self.root_dir)
        if not os.path.isdir(stack_dir):
             create_dirs(stack_dir, self.root_dir)
        filename = "{}/global.yml".format(stack_dir)
        contents = {}
        contents['time_server'] = '{{ pillar.get("master_minion") }}'
        contents['time_service'] = 'ntp'

        self.writer.write(filename, contents)

def create_dirs(path, root):
    try:
        os.makedirs(path)
    except OSError as err:
        if err.errno == errno.EACCES:
            print "ERROR: Cannot create dir {}".format(path)
            print "       Please make sure {} is owned by salt".format(root)
            raise err


def proposals(**kwargs):
    """
    Collect the hardware profiles, all possible role assignments and common
    configuration under /srv/pillar/ceph/proposals
    """
    settings = Settings()

    salt_writer = SaltWriter(**kwargs)

    ceph_cluster = CephCluster(settings, salt_writer, **kwargs)
    ceph_cluster.generate()

    osd_disc = OSDDiscovery(settings)
    osd_disc.print_osds()
    osd_disc.save_osds(salt_writer)

    network = NetworkDiscovery(settings)

    mon_disc = MONSDiscovery(settings, osd_disc.osds, network)
    mon_disc.print_mons()
    mon_disc.save_mons(salt_writer)

    admin_disc = AdminsDiscovery(settings, osd_disc.osds, mon_disc.mons)
    admin_disc.print_admins()
    admin_disc.save_admins(salt_writer)


    cluster_dir = "{}/config/stack/default/{}".format(settings.root_dir, settings.cluster_config.name())
    if not os.path.isdir(cluster_dir):
         create_dirs(cluster_dir, settings.root_dir)
    filename = "{}/cluster.yml".format(cluster_dir)
    contents = {}
    contents['fsid'] = str(uuid.uuid3(uuid.NAMESPACE_DNS, os.urandom(32)))
    contents['admin_method'] = "default"
    contents['configuration_method'] = "default"
    contents['mds_method'] = "default"
    contents['mon_method'] = "default"
    contents['osd_method'] = "default"
    contents['package_method'] = "default"
    contents['pool_method'] = "default"
    contents['repo_method'] = "default"
    contents['rgw_method'] = "default"
    contents['update_method'] = "default"
    contents['public_network'] = network.public_network
    contents['cluster_network'] = network.cluster_network
    salt_writer.write(filename, contents)

    name = settings.cluster_config.name()
    # generate config file policy.cfg
    filename = "{}/policy.cfg".format(settings.root_dir)
    f = open(filename, 'w')
    f.write("cluster-"+settings.cluster_config.name()+"/cluster/*.sls\n")
    f.write("config/stack/default/global.yml\n")
    f.write("config/stack/default/"+name+"/cluster.yml\n")
    f.write("role-admin/cluster/*.sls\n")
    f.write("disks/cluster/*.sls\n")
    f.write("disks/stack/default/"+name+"/minions/*.yml\n")
    f.write("role-mon/cluster/*.sls\n")
    f.write("role-mon/stack/default/"+name+"/minions/*.yml\n")
    f.close()

    return True


