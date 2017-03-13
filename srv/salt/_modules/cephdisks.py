#!/usr/bin/python

import os
import re
import xml.etree.ElementTree as et
from glob import glob
from subprocess import Popen, PIPE
import logging

log = logging.getLogger(__name__)

VERSION=0.2

class HardwareDetections(object):
    """
    Custom Salt module to detect suitable disks for the ceph deployment
    """

    def __init__(self, **kwargs):
        """
        kwargs adds the possibility to pass custom raidcontroller
        names to the class.
        args:
            hw_raid(bool): Manually set hw_raid True if this class can't detect it
            hw_raid_name(str): Manually set the hw_raid_ctrls name
            software_raid(bool): Manually set if you have sw raid and the class
                                            fails to detect it.
        REQUIREMENTS FOR THE PROGRAMM TO WORK:
        gptfdisk, pciutils, smartmontools
        """
        self.detection_method = self._find_detection_tool(kwargs.get('detection_method', None))
        self.hw_raid = kwargs.get('hw_raid', None)
        self.hw_raid_name = kwargs.get('raid_controller_name', None)
        self.software_raid = kwargs.get('sw_raid', None)

    def _is_removable(self, base):
        """
        Ask the kernel if device is a removable

        args:
            base (str): base sys path of device
        returns:
            bool: True if is removable
        """
        filename = base + "/removable"
        with open(filename, 'r') as fd:
            removable = fd.readline().rstrip('\n')
        if (removable == "1"):
            return True

    def _is_rotational(self, base):
        """
        Ask the kernel for the disk's rotational value

        args:
            base (str): base sys path of device
        return:
            str: 1 if rotational 0 if nonrotational
        """
        filename = base + "/queue/rotational"
        with open(filename, 'r') as fd:
            rotational = fd.readline().rstrip('\n')
        if (rotational == "1"):
            return rotational
        else:
            return "0"

    def _return_device_bus_id(self, device):
        """
        Tries to get the BUS_ID for a device. Used to query
        S.M.A.R.T with -d <raidctrl>,<busid>
        args:
            device(str): shortname for device(sda, sdb)
        return:
            str: bus_id of device
        """
        lsscsi_path = self._which('lsscsi')
        cmd = lsscsi_path
        proc = Popen(cmd, stdout=PIPE, stderr=PIPE, shell=True)
        for line in proc.stdout:
            if device in line:
                m = re.match('\[(.*?)\]', line)
                if len(m.group(1).split(":")) >= 2:
                    # try to be less stupid here
                    return m.group(1).split(":")[-2]
                    """
                    is [0:0:ID:0] a fixed format?
                    check on other machines
                    """
                else:
                    log.warning("Could not retrieve bus_id for {}").format(device)
                    return None

    def _query_disktype(self, device, raid_ctrl, base):
        """
        Query smartctl for a more concise information on it's type.

        args:
            device (str): short form of device (sda, sdb)
            raid_ctrl (dict): dict with raidctrl info
            id (str): position in disk array? don't know how to fix that TODO:
        return:
            bool: 0 if SSD else 1
        """
        smartctl_path = self._which('smartctl')
        bus_id = self._return_device_bus_id(device)
        if not bus_id:
            log.warning('Could not find bus_id for {}. Falling back to legacy detection mode'.format(device))
            return self._is_rotational(base)
        try:
            cmd = "{} -i /dev/{} -d {},{}".format(smartctl_path,
                                                  device,
                                                  raid_ctrl['controller_name'],
                                                  bus_id)
            proc = Popen(cmd, stdout=PIPE, stderr=PIPE, shell=True)
            for line in proc.stdout:
                # ADD PARSING HERE TO DETECT FAILURE
                if "A mandatory SMART command failed" in line:
                    log.warn("Something went wrong during smartctl query")
                m = re.match("([^:]+): (.*)", line)
                if m:
                    if (m.group(1) == "Rotation Rate"):
                        c = re.match("^\s+ Solid State Device", m.group(2))
                        if c:
                            return '0'
            return '1'
        except:
            """ If something fails, fall back to the default detection mode"""
            log.warning('Something went wrong during smartctl query for device {}. Falling back to legacy detection mode'.format(device))
            return self._is_rotational(base)

    def _detect_raidctrl(self):
        """
        Detect raidcontroller type and name

        return:
            (dict): Information about raid
        """

        info = {}

        if self.hw_raid:
            info['raidtype'] = 'hardware'
            if self.hw_raid_name:
                info['controller_name'] = self.hw_raid_name
                log.info("Using user-provided options for raidname and raidtype")
                return info
            else:
                return self._hw_raid_ctrl_detection()
        elif self.software_raid:
            log.info('Found a software raid setup')
            info['raidtype'] = 'software'
            return info
        else:
            return self._hw_raid_ctrl_detection()

    def _hw_raid_ctrl_detection(self):
        """
        Calls out for lspci to retrieve information
        about the underlying RAID-Controller
        return:
           (dict): Information about RAID
        """
        info = {}
        info['controller_name'] = None
        lspci_path = self._which('lspci')
        cmd = "{} -vv | grep -i raid".format(lspci_path)
        # Verify that proc.stdout actually gives something
        # Or set default to None.
        # TODO: See if other places are also infected
        proc = Popen(cmd, stdout=PIPE, stderr=PIPE, shell=True)
        for line in proc.stdout:
            """
            match one of the available raid ctrls
            areca, megaraid, 3ware, hprr
            """
            if 'megaraid' in line.lower():
                info['controller_name'] = 'megaraid'
            elif 'areca' in line.lower() or 'arcmsr' in line.lower():
                info['controller_name'] = 'areca'
            elif '3ware' in line.lower():
                info['controller_name'] = '3ware'
            elif 'hprr' in line.lower():
                info['controller_name'] = 'hprr'
            elif 'hpt' in line.lower():
                info['controller_name'] = 'hpt'
            elif 'cciss' in line.lower():
                info['controller_name'] = 'cciss'
            elif 'aacraid' in line.lower():
                info['controller_name'] = 'aacraid'
            else:
                info['controller_name'] = None

            if info['controller_name']:
                info['raidtype'] = 'hardware'
                msg = 'Found raidctrl: {}'.format(info['controller_name'])
                log.info(msg)
                return info
        if not info['controller_name']:
            info['raidtype'] = None
            log.info("No raidctrl found")
            return info

    def _which(self, program, failhard=True):
        """
        Instead of using python's built-in _platform_ we rely
        on the tools presence.

        Sidenote for testing: smartctl, hwinfo and lshw resides in /sbin/
        Detection will only work in privileged environments
        args:
            programm(str): name of programm you want to retrieve the path from
        return:
            str: the full path of the programm
        """
        def _is_exe(fpath):
            return os.path.isfile(fpath) and os.access(fpath, os.X_OK)

        fpath, fname = os.path.split(program)
        if fpath:
            if is_exe(program):
                return program
        else:
            for path in os.environ["PATH"].split(os.pathsep):
                path = path.strip('"')
                exe_file = os.path.join(path, program)
                if _is_exe(exe_file):
                    return exe_file
        if failhard is False:
            return None
        elif failhard is True:
            msg = "Can't find the tool: {}. Please Install it in order to resume.".format(program)
            log.info(msg)
            raise StandardError(msg)
        else:
            msg = "Parameter <failhard> needs to be bool(True) or bool(False) but was: {}".format(str(failhard))
            log.info(msg)
            raise StandardError(msg)

    def _find_detection_tool(self, overwrite_method=None):
        """
        Finds the right tool to identify the underlying hardware.
        return:
            fnc: the corresponding function based on the presence of tools
        """
        # add checking for sudo
        if overwrite_method == 'lshw':
            return self._lshw
        if overwrite_method == 'hwinfo':
            return self._hwinfo
        if overwrite_method:
            err_msg = """ The tool: {} you specified for hardware detection
            is not implemented in cephdisks. Use lshw or hwinfo, please.""".format(overwrite_method)
            log.error(err_msg)
            raise Exception(err_msg)

        if self._which('hwinfo', failhard=False):
            """ SUSE, openSUSE """
            return self._hwinfo
        elif self._which('lshw', failhard=False):
            """ Ubuntu, Fedora, CentOS """
            return self._lshw
        else:
            err_msg = """Can not find a proper hardware detection tool.
            Install lshw or hwinfo in order to retrive hardware information"""
            log.error(err_msg)
            raise Exception(err_msg)

    def _hwinfo(self, device=None):
        """
        Parse hwinfo output into dictionary

        args:
            device (str): short name of device(sda, sdb..)
        return:
            dict: hwinfo output as dict
        """
        results = {}
        hwinfo_path = self._which('hwinfo')
        cmd = "{} --disk --only /dev/{}".format(hwinfo_path, device)
        proc = Popen(cmd, stdout=PIPE, stderr=PIPE, shell=True)
        for line in proc.stdout:
            m = re.match("  ([^:]+): (.*)", line)
            if m:
                if (m.group(1) == "Capacity"):
                    c = re.match("(\d+ \w+) \((\d+) bytes\)", m.group(2))
                    if c:
                        results[m.group(1)] = c.group(1)
                        results['Bytes'] = c.group(2)
                elif (m.group(1) == 'Device File'):
                    if ' ' in m.group(2):
                        results[m.group(1)] = re.sub(r'"', '', m.group(2).split(' ')[0])
                    else:
                        results[m.group(1)] = re.sub(r'"', '', m.group(2))
                else:
                    results[m.group(1)] = re.sub(r'"', '', m.group(2))
        return results

    def _udevadm(self, device):
        """
        """
        cmd = "udevadm info {}".format(device)
        proc = Popen(cmd, stdout=PIPE, stderr=PIPE, shell=True)
        stdout, stderr = proc.communicate()
        if stdout:
            for line in stdout:
                if 'by-id' in line:
                    return "/dev/" + line.split()[1]
        elif stderr:
            err_msg = "Something went wrong during 'udevadm' execution"
            log.info(err_msg)
            raise Exception(err_msg)
        return device

    def _osd(self, device, ids):
        """
        Search for Ceph Data and Journal partitions
        """
        # TODO: Search for all possible codes:
        data = "Partition GUID code: 45B0969E-9B03-4F30-B4C6-B4B80CEFF106"
        journal = "Partition GUID code: 4FBD7E29-9D25-41B8-AFD0-062C0CEFF05D"
        sgdisk_path = self._which('sgdisk')
        for partition_id in ids:
            cmd = "{} -i {} {}".format(sgdisk_path, partition_id, device)
            proc = Popen(cmd, stdout=PIPE, stderr=PIPE, shell=True)
            for line in proc.stdout:
                if (line.startswith(data) or line.startswith(journal)):
                    return True
            for line in proc.stderr:
                log.error(line)
        return False

    def _lshw(self, device=None):
        """
        Parse lshw output into dictionary

        args:
            device (str): short name of device(sda, sdb..)
        return:
            list: lshw output as list of dicts
        """

        results = {}
        lshw_path = self._which('lshw')
        cmd = "{} -class disk -xml".format(lshw_path)
        proc = Popen(cmd, stdout=PIPE, stderr=PIPE, shell=True)
        stdout, stderr = proc.communicate()
        if stdout:
            data = et.fromstring(stdout)
        elif stderr:
            err_msg = "Something went wrong during 'lshw' execution"
            log.info(err_msg)
            raise Exception(err_msg)
        attributes = {
                      'size': 'Capacity',
                      'product': 'Model',
                      'serial': 'Serial ID'
                     }
        # If find('node')? could this potentially go wrong?
        for node in data.findall('node'):
            disk_description = {}
            if node.find('logicalname') is not None:
                if node.find('logicalname').text == '/dev/cdrom':
                    continue
                ident = node.find('logicalname').text
                if type(ident) is list:
                    ident = ident[0]
                results[ident] = {}
                for key, attr in attributes.iteritems():
                    if node.find(key) is not None:
                        if key == 'size':
                            # Is the MB/GB/TB suffix important enough to add checking for it?
                            disk_description[attr] = str(int(node.find(key).text) / 1000000000)
                        else:
                            disk_description[attr] = node.find(key).text
                disk_description['Device File'] = self._udevadm(ident)
                disk_description['Driver'] = self._find_driver(ident)
                results[ident].update(disk_description)
            else:
                log.info('No logicalname found. Cannot identiy that disk.')
        return results

    def _find_driver(self, ident):
        """
        lshw can't detect the driver used. proposal.py relies on
        this information to determine the journal distribution.
        #TODO#
        """
        return 'None'

    def _preflight_check(self, hardware_dict):
        """
        Check if lshw or hwinfo actually returned the 
        needed fields of 'Capacity', 'Model', 'Device File',
        'device', 'rotational' and 'Driver'.
        If they don't exist and hwdict gets passed to populate.py
        it will fail silently and cause havoc.
        """
        required_fields = ['Driver', 'Model', 'Device File', 'Capacity', 'device', 'rotational']
        for rf in required_fields:
            if rf not in hardware_dict or not hardware_dict[rf]:
                raise ValueError("{} is not included in the hardware dict.".format(rf))

    def assemble_device_list(self):
        """
        Find all unpartitioned and allocated osds.  Return unified dict.

        If there is the indication for a involved RAIDController, rely on
        smartctl rather than on lshw/hwinfo. The kernel might have wrong
        information here.

        return:
            (list): list of dicts containing information about usable devices
        """

        drives = []
        raid_ctrl = self._detect_raidctrl()
        hw = self.detection_method()
        for path in glob('/sys/block/*/device'):
            base = os.path.dirname(path)
            device = os.path.basename(base)
            # Check this on a per disk basis
            # Skip partitioned, non-osd drives
            partitions = glob(base + "/" + device + "*")
            if partitions:
                for p in partitions:
                    ids = [re.sub('\D+', '', p) for p in partitions]
                if not self._osd("/dev/" + device, ids):
                    continue

            if (self._is_removable(base)):
                continue

            if hw:
                hardware = hw['/dev/'+device]
            else:
                hardware = self.detection_method(device)

            if raid_ctrl['raidtype'] and self._which('smartctl'):
                """ Trying to correct the kernel's assumption here """
                log.info("Requirements met to utilize S.M.A.R.T on {}".format(device))
                rotational = self._query_disktype(device, raid_ctrl, base)
                hardware['rotational'] = rotational
            else:
                hardware['rotational'] = self._is_rotational(base)

            hardware['device'] = device
            self._preflight_check(hardware)
            drives.append(hardware)
        return drives


def list_(**kwargs):
    hwd = HardwareDetections(**kwargs)
    return hwd.assemble_device_list()

def version():
    print VERSION

__func_alias__ = {
                'list_': 'list',
                }
