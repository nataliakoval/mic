#!/usr/bin/python -tt
#
# Copyright (c) 2009, 2010, 2011 Intel, Inc.
# Copyright (c) 2007, 2008 Red Hat, Inc.
# Copyright (c) 2008 Daniel P. Berrange
# Copyright (c) 2008 David P. Huff
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation; version 2 of the License
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
# for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc., 59
# Temple Place - Suite 330, Boston, MA 02111-1307, USA.

import os

from mic import msger
from mic.utils import runner
from mic.utils.errors import MountError
from mic.utils.fs_related import *
from mic.utils.gpt_parser import GptParser

# Overhead of the MBR partitioning scheme (just one sector)
MBR_OVERHEAD = 1
# Overhead of the GPT partitioning scheme
GPT_OVERHEAD = 34

# Size of a sector in bytes
SECTOR_SIZE = 512

class PartitionedMount(Mount):
    def __init__(self, mountdir, skipformat = False):
        Mount.__init__(self, mountdir)
        self.disks = {}
        self.partitions = []
        self.subvolumes = []
        self.mapped = False
        self.mountOrder = []
        self.unmountOrder = []
        self.parted = find_binary_path("parted")
        self.kpartx = find_binary_path("kpartx")
        self.mkswap = find_binary_path("mkswap")
        self.btrfscmd=None
        self.mountcmd = find_binary_path("mount")
        self.umountcmd = find_binary_path("umount")
        self.skipformat = skipformat
        self.snapshot_created = self.skipformat
        # Size of a sector used in calculations
        self.sector_size = SECTOR_SIZE
        self._partitions_layed_out = False

    def __add_disk(self, disk_name):
        """ Add a disk 'disk_name' to the internal list of disks. Note,
        'disk_name' is the name of the disk in the target system
        (e.g., sdb). """

        if disk_name in self.disks:
            # We already have this disk
            return

        assert not self._partitions_layed_out

        self.disks[disk_name] = \
                { 'disk': None,     # Disk object
                  'mapped': False,  # True if kpartx mapping exists
                  'numpart': 0,     # Number of allocate partitions
                  'partitions': [], # Indexes to self.partitions
                  # Partitions with part num higher than 3 will
                  # be put to the extended partition.
                  'extended': 0,    # Size of extended partition
                  'offset': 0,      # Offset of next partition (in sectors)
                  # Minimum required disk size to fit all partitions (in bytes)
                  'min_size': 0,
                  'ptable_format': "msdos" } # Partition table format

    def add_disk(self, disk_name, disk_obj):
        """ Add a disk object which have to be partitioned. More than one disk
        can be added. In case of multiple disks, disk partitions have to be
        added for each disk separately with 'add_partition()". """

        self.__add_disk(disk_name)
        self.disks[disk_name]['disk'] = disk_obj

    def __add_partition(self, part):
        """ This is a helper function for 'add_partition()' which adds a
        partition to the internal list of partitions. """

        assert not self._partitions_layed_out

        self.partitions.append(part)
        self.__add_disk(part['disk_name'])

    def add_partition(self, size, disk_name, mountpoint, fstype = None,
                      label=None, fsopts = None, boot = False, align = None):
        """ Add the next partition. Prtitions have to be added in the
        first-to-last order. """

        # Converting MB to sectors for parted
        size = size * 1024 * 1024 / self.sector_size

        # We need to handle subvolumes for btrfs
        if fstype == "btrfs" and fsopts and fsopts.find("subvol=") != -1:
            self.btrfscmd=find_binary_path("btrfs")
            subvol = None
            opts = fsopts.split(",")
            for opt in opts:
                if opt.find("subvol=") != -1:
                    subvol = opt.replace("subvol=", "").strip()
                    break
            if not subvol:
                raise MountError("No subvolume: %s" % fsopts)
            self.subvolumes.append({'size': size, # In sectors
                                    'mountpoint': mountpoint, # Mount relative to chroot
                                    'fstype': fstype, # Filesystem type
                                    'fsopts': fsopts, # Filesystem mount options
                                    'disk_name': disk, # physical disk name holding partition
                                    'device': None, # kpartx device node for partition
                                    'mount': None, # Mount object
                                    'subvol': subvol, # Subvolume name
                                    'boot': boot, # Bootable flag
                                    'mounted': False # Mount flag
                                   })

        # We still need partition for "/" or non-subvolume
        if mountpoint == "/" or not fsopts or fsopts.find("subvol=") == -1:
            # Don't need subvolume for "/" because it will be set as default subvolume
            if fsopts and fsopts.find("subvol=") != -1:
                opts = fsopts.split(",")
                for opt in opts:
                    if opt.strip().startswith("subvol="):
                        opts.remove(opt)
                        break
                fsopts = ",".join(opts)

            part = { 'size': size, # In sectors
                     'mountpoint': mountpoint, # Mount relative to chroot
                     'fstype': fstype, # Filesystem type
                     'fsopts': fsopts, # Filesystem mount options
                     'label': label, # Partition label
                     'disk_name': disk_name, # physical disk name holding partition
                     'device': None, # kpartx device node for partition
                     'mount': None, # Mount object
                     'num': None, # Partition number
                     'boot': boot, # Bootable flag
                     'align': align, # Partition alignment
                     'partuuid': None } # Partition UUID (GPT-only)

            self.__add_partition(part)

    def layout_partitions(self, ptable_format = "msdos"):
        """ Layout the partitions, meaning calculate the position of every
        partition on the disk. The 'ptable_format' parameter defines the
        partition table format, and may be either "msdos" or "gpt". """

        msger.debug("Assigning %s partitions to disks" % ptable_format)

        if ptable_format not in ('msdos', 'gpt'):
            raise MountError("Unknown partition table format '%s', supported " \
                             "formats are: 'msdos' and 'gpt'" % ptable_format)

        if self._partitions_layed_out:
            return

        self._partitions_layed_out = True

        # Go through partitions in the order they are added in .ks file
        for n in range(len(self.partitions)):
            p = self.partitions[n]

            if not self.disks.has_key(p['disk_name']):
                raise MountError("No disk %s for partition %s" \
                                 % (p['disk_name'], p['mountpoint']))

            # Get the disk where the partition is located
            d = self.disks[p['disk_name']]
            d['numpart'] += 1
            d['ptable_format'] = ptable_format

            if d['numpart'] == 1:
                if ptable_format == "msdos":
                    overhead = MBR_OVERHEAD
                else:
                    overhead = GPT_OVERHEAD

                # Skip one sector required for the partitioning scheme overhead
                d['offset'] += overhead
                # Steal few sectors from the first partition to offset for the
                # partitioning overhead
                p['size'] -= overhead

            if p['align']:
                # If not first partition and we do have alignment set we need
                # to align the partition.
                # FIXME: This leaves a empty spaces to the disk. To fill the
                # gaps we could enlargea the previous partition?

                # Calc how much the alignment is off.
                align_sectors = d['offset'] % (p['align'] * 1024 / self.sector_size)
                # We need to move forward to the next alignment point
                align_sectors = (p['align'] * 1024 / self.sector_size) - align_sectors

                msger.debug("Realignment for %s%s with %s sectors, original"
                            " offset %s, target alignment is %sK." %
                            (p['disk_name'], d['numpart'], align_sectors,
                             d['offset'], p['align']))

                # p['size'] already converted in secctors
                if p['size'] <= align_sectors:
                    raise MountError("Partition for %s is too small to handle "
                                     "the alignment change." % p['mountpoint'])

                # increase the offset so we actually start the partition on right alignment
                d['offset'] += align_sectors

            if d['numpart'] > 3:
                # Increase allocation of extended partition to hold this partition
                d['extended'] += p['size']
                p['type'] = 'logical'
                p['num'] = d['numpart'] + 1
            else:
                p['type'] = 'primary'
                p['num'] = d['numpart']

            p['start'] = d['offset']
            d['offset'] += p['size']
            d['partitions'].append(n)
            msger.debug("Assigned %s to %s%d at Sector %d with size %d sectors "
                        "/ %d bytes." % (p['mountpoint'], p['disk_name'],
                                         p['num'], p['start'], p['size'],
                                         p['size'] * self.sector_size))

        # Once all the partitions have been layed out, we can calculate the
        # minumim disk sizes.
        for disk_name, disk in self.disks.items():
            last_partition = self.partitions[disk['partitions'][-1]]
            disk['min_size'] = last_partition['start'] + last_partition['size']

            if disk['ptable_format'] == 'gpt':
                # Account for the backup partition table at the end of the disk
                disk['min_size'] += GPT_OVERHEAD

            disk['min_size'] *= self.sector_size

    def __run_parted(self, args):
        """ Run parted with arguments specified in the 'args' list. """

        args.insert(0, self.parted)
        msger.debug(args)

        rc, out = runner.runtool(args, catch = 3)
        out = out.strip()
        if out:
            msger.debug('"parted" output: %s' % out)

        if rc != 0:
            # We don't throw exception when return code is not 0, because
            # parted always fails to reload part table with loop devices. This
            # prevents us from distinguishing real errors based on return
            # code.
            msger.debug("WARNING: parted returned '%s' instead of 0" % rc)

    def __create_partition(self, device, parttype, fstype, start, size):
        """ Create a partition on an image described by the 'device' object. """

        # Start is included to the size so we need to substract one from the end.
        end = start + size - 1
        msger.debug("Added '%s' part at Sector %d with size %d sectors" %
                    (parttype, start, end))

        args = ["-s", device, "unit", "s", "mkpart", parttype]
        if fstype:
            args.extend([fstype])
        args.extend(["%d" % start, "%d" % end])

        return self.__run_parted(args)

    def __format_disks(self):
        self.layout_partitions()

        if self.skipformat:
            msger.debug("Skipping disk format, because skipformat flag is set.")
            return

        for dev in self.disks.keys():
            d = self.disks[dev]
            msger.debug("Initializing partition table for %s" % \
                        (d['disk'].device))
            self.__run_parted(["-s", d['disk'].device, "mklabel",
                               d['ptable_format']])

        msger.debug("Creating partitions")

        for p in self.partitions:
            d = self.disks[p['disk_name']]
            if p['num'] == 5:
                self.__create_partition(d['disk'].device, "extended", None,
                                        p['start'], d['extended'])

            if p['fstype'] == "swap":
                parted_fs_type = "linux-swap"
            elif p['fstype'] == "vfat":
                parted_fs_type = "fat32"
            elif p['fstype'] == "msdos":
                parted_fs_type = "fat16"
            else:
                # Type for ext2/ext3/ext4/btrfs
                parted_fs_type = "ext2"

            # Boot ROM of OMAP boards require vfat boot partition to have an
            # even number of sectors.
            if p['mountpoint'] == "/boot" and p['fstype'] in ["vfat", "msdos"] \
               and p['size'] % 2:
                msger.debug("Substracting one sector from '%s' partition to " \
                            "get even number of sectors for the partition" % \
                            p['mountpoint'])
                p['size'] -= 1

            self.__create_partition(d['disk'].device, p['type'],
                                    parted_fs_type, p['start'], p['size'])

            if p['boot']:
                if d['ptable_format'] == 'gpt':
                    flag_name = "legacy_boot"
                else:
                    flag_name = "boot"
                msger.debug("Set '%s' flag for partition '%s' on disk '%s'" % \
                            (flag_name, p['num'], d['disk'].device))
                self.__run_parted(["-s", d['disk'].device, "set",
                                   "%d" % p['num'], flag_name, "on"])

        # If the partition table format is "gpt", find out PARTUUIDs for all
        # the partitions
        for disk_name, disk in self.disks.items():
            if disk['ptable_format'] != 'gpt':
                continue

            pnum = 0
            gpt_parser = GptParser(d['disk'].device, SECTOR_SIZE)
            # Iterate over all GPT partitions on this disk
            for entry in gpt_parser.get_partitions():
                pnum += 1
                # Find the matching partition in the 'self.partitions' list
                for n in d['partitions']:
                    p = self.partitions[n]
                    if p['num'] == pnum:
                        # Found, assign PARTUUID
                        p['partuuid'] = entry[1]
                        msger.debug("PARTUUID for partition %d of disk '%s' " \
                                    "(mount point '%s') is '%s'" % (pnum, \
                                    disk_name, p['mountpoint'], p['partuuid']))

            del gpt_parser

    def __map_partitions(self):
        """Load it if dm_snapshot isn't loaded. """
        load_module("dm_snapshot")

        for dev in self.disks.keys():
            d = self.disks[dev]
            if d['mapped']:
                continue

            msger.debug("Running kpartx on %s" % d['disk'].device )
            rc, kpartxOutput = runner.runtool([self.kpartx, "-l", "-v", d['disk'].device])
            kpartxOutput = kpartxOutput.splitlines()

            if rc != 0:
                raise MountError("Failed to query partition mapping for '%s'" %
                                 d['disk'].device)

            # Strip trailing blank and mask verbose output
            i = 0
            while i < len(kpartxOutput) and kpartxOutput[i][0:4] != "loop":
               i = i + 1
            kpartxOutput = kpartxOutput[i:]

            # Quick sanity check that the number of partitions matches
            # our expectation. If it doesn't, someone broke the code
            # further up
            if len(kpartxOutput) != d['numpart']:
                raise MountError("Unexpected number of partitions from kpartx: %d != %d" %
                                 (len(kpartxOutput), d['numpart']))

            for i in range(len(kpartxOutput)):
                line = kpartxOutput[i]
                newdev = line.split()[0]
                mapperdev = "/dev/mapper/" + newdev
                loopdev = d['disk'].device + newdev[-1]

                msger.debug("Dev %s: %s -> %s" % (newdev, loopdev, mapperdev))
                pnum = d['partitions'][i]
                self.partitions[pnum]['device'] = loopdev

                # grub's install wants partitions to be named
                # to match their parent device + partition num
                # kpartx doesn't work like this, so we add compat
                # symlinks to point to /dev/mapper
                if os.path.lexists(loopdev):
                    os.unlink(loopdev)
                os.symlink(mapperdev, loopdev)

            msger.debug("Adding partx mapping for %s" % d['disk'].device)
            rc = runner.show([self.kpartx, "-v", "-a", d['disk'].device])

            if rc != 0:
                # Make sure that the device maps are also removed on error case.
                # The d['mapped'] isn't set to True if the kpartx fails so
                # failed mapping will not be cleaned on cleanup either.
                runner.quiet([self.kpartx, "-d", d['disk'].device])
                raise MountError("Failed to map partitions for '%s'" %
                                 d['disk'].device)

            d['mapped'] = True

    def __unmap_partitions(self):
        for dev in self.disks.keys():
            d = self.disks[dev]
            if not d['mapped']:
                continue

            msger.debug("Removing compat symlinks")
            for pnum in d['partitions']:
                if self.partitions[pnum]['device'] != None:
                    os.unlink(self.partitions[pnum]['device'])
                    self.partitions[pnum]['device'] = None

            msger.debug("Unmapping %s" % d['disk'].device)
            rc = runner.quiet([self.kpartx, "-d", d['disk'].device])
            if rc != 0:
                raise MountError("Failed to unmap partitions for '%s'" %
                                 d['disk'].device)

            d['mapped'] = False

    def __calculate_mountorder(self):
        msger.debug("Calculating mount order")
        for p in self.partitions:
            self.mountOrder.append(p['mountpoint'])
            self.unmountOrder.append(p['mountpoint'])

        self.mountOrder.sort()
        self.unmountOrder.sort()
        self.unmountOrder.reverse()

    def cleanup(self):
        Mount.cleanup(self)
        if self.disks:
            self.__unmap_partitions()
            for dev in self.disks.keys():
                d = self.disks[dev]
                try:
                    d['disk'].cleanup()
                except:
                    pass

    def unmount(self):
        self.__unmount_subvolumes()
        for mp in self.unmountOrder:
            if mp == 'swap':
                continue
            p = None
            for p1 in self.partitions:
                if p1['mountpoint'] == mp:
                    p = p1
                    break

            if p['mount'] != None:
                try:
                    # Create subvolume snapshot here
                    if p['fstype'] == "btrfs" and p['mountpoint'] == "/" and not self.snapshot_created:
                        self.__create_subvolume_snapshots(p, p["mount"])
                    p['mount'].cleanup()
                except:
                    pass
                p['mount'] = None

    # Only for btrfs
    def __get_subvolume_id(self, rootpath, subvol):
        if not self.btrfscmd:
            self.btrfscmd=find_binary_path("btrfs")
        argv = [ self.btrfscmd, "subvolume", "list", rootpath ]

        rc, out = runner.runtool(argv)
        msger.debug(out)

        if rc != 0:
            raise MountError("Failed to get subvolume id from %s', return code: %d." % (rootpath, rc))

        subvolid = -1
        for line in out.splitlines():
            if line.endswith(" path %s" % subvol):
                subvolid = line.split()[1]
                if not subvolid.isdigit():
                    raise MountError("Invalid subvolume id: %s" % subvolid)
                subvolid = int(subvolid)
                break
        return subvolid

    def __create_subvolume_metadata(self, p, pdisk):
        if len(self.subvolumes) == 0:
            return

        argv = [ self.btrfscmd, "subvolume", "list", pdisk.mountdir ]
        rc, out = runner.runtool(argv)
        msger.debug(out)

        if rc != 0:
            raise MountError("Failed to get subvolume id from %s', return code: %d." % (pdisk.mountdir, rc))

        subvolid_items = out.splitlines()
        subvolume_metadata = ""
        for subvol in self.subvolumes:
            for line in subvolid_items:
                if line.endswith(" path %s" % subvol["subvol"]):
                    subvolid = line.split()[1]
                    if not subvolid.isdigit():
                        raise MountError("Invalid subvolume id: %s" % subvolid)

                    subvolid = int(subvolid)
                    opts = subvol["fsopts"].split(",")
                    for opt in opts:
                        if opt.strip().startswith("subvol="):
                            opts.remove(opt)
                            break
                    fsopts = ",".join(opts)
                    subvolume_metadata += "%d\t%s\t%s\t%s\n" % (subvolid, subvol["subvol"], subvol['mountpoint'], fsopts)

        if subvolume_metadata:
            fd = open("%s/.subvolume_metadata" % pdisk.mountdir, "w")
            fd.write(subvolume_metadata)
            fd.close()

    def __get_subvolume_metadata(self, p, pdisk):
        subvolume_metadata_file = "%s/.subvolume_metadata" % pdisk.mountdir
        if not os.path.exists(subvolume_metadata_file):
            return

        fd = open(subvolume_metadata_file, "r")
        content = fd.read()
        fd.close()

        for line in content.splitlines():
            items = line.split("\t")
            if items and len(items) == 4:
                self.subvolumes.append({'size': 0, # In sectors
                                        'mountpoint': items[2], # Mount relative to chroot
                                        'fstype': "btrfs", # Filesystem type
                                        'fsopts': items[3] + ",subvol=%s" %  items[1], # Filesystem mount options
                                        'disk_name': p['disk_name'], # physical disk name holding partition
                                        'device': None, # kpartx device node for partition
                                        'mount': None, # Mount object
                                        'subvol': items[1], # Subvolume name
                                        'boot': False, # Bootable flag
                                        'mounted': False # Mount flag
                                   })

    def __create_subvolumes(self, p, pdisk):
        """ Create all the subvolumes. """

        for subvol in self.subvolumes:
            argv = [ self.btrfscmd, "subvolume", "create", pdisk.mountdir + "/" + subvol["subvol"]]

            rc = runner.show(argv)
            if rc != 0:
                raise MountError("Failed to create subvolume '%s', return code: %d." % (subvol["subvol"], rc))

        # Set default subvolume, subvolume for "/" is default
        subvol = None
        for subvolume in self.subvolumes:
            if subvolume["mountpoint"] == "/" and p['disk_name'] == subvolume['disk_name']:
                subvol = subvolume
                break

        if subvol:
            # Get default subvolume id
            subvolid = self. __get_subvolume_id(pdisk.mountdir, subvol["subvol"])
            # Set default subvolume
            if subvolid != -1:
                rc = runner.show([ self.btrfscmd, "subvolume", "set-default", "%d" % subvolid, pdisk.mountdir])
                if rc != 0:
                    raise MountError("Failed to set default subvolume id: %d', return code: %d." % (subvolid, rc))

        self.__create_subvolume_metadata(p, pdisk)

    def __mount_subvolumes(self, p, pdisk):
        if self.skipformat:
            # Get subvolume info
            self.__get_subvolume_metadata(p, pdisk)
            # Set default mount options
            if len(self.subvolumes) != 0:
                for subvol in self.subvolumes:
                    if subvol["mountpoint"] == p["mountpoint"] == "/":
                        opts = subvol["fsopts"].split(",")
                        for opt in opts:
                            if opt.strip().startswith("subvol="):
                                opts.remove(opt)
                                break
                        pdisk.fsopts = ",".join(opts)
                        break

        if len(self.subvolumes) == 0:
            # Return directly if no subvolumes
            return

        # Remount to make default subvolume mounted
        rc = runner.show([self.umountcmd, pdisk.mountdir])
        if rc != 0:
            raise MountError("Failed to umount %s" % pdisk.mountdir)

        rc = runner.show([self.mountcmd, "-o", pdisk.fsopts, pdisk.disk.device, pdisk.mountdir])
        if rc != 0:
            raise MountError("Failed to umount %s" % pdisk.mountdir)

        for subvol in self.subvolumes:
            if subvol["mountpoint"] == "/":
                continue
            subvolid = self. __get_subvolume_id(pdisk.mountdir, subvol["subvol"])
            if subvolid == -1:
                msger.debug("WARNING: invalid subvolume %s" % subvol["subvol"])
                continue
            # Replace subvolume name with subvolume ID
            opts = subvol["fsopts"].split(",")
            for opt in opts:
                if opt.strip().startswith("subvol="):
                    opts.remove(opt)
                    break

            opts.extend(["subvolrootid=0", "subvol=%s" % subvol["subvol"]])
            fsopts = ",".join(opts)
            subvol['fsopts'] = fsopts
            mountpoint = self.mountdir + subvol['mountpoint']
            makedirs(mountpoint)
            rc = runner.show([self.mountcmd, "-o", fsopts, pdisk.disk.device, mountpoint])
            if rc != 0:
                raise MountError("Failed to mount subvolume %s to %s" % (subvol["subvol"], mountpoint))
            subvol["mounted"] = True

    def __unmount_subvolumes(self):
        """ It may be called multiple times, so we need to chekc if it is still mounted. """
        for subvol in self.subvolumes:
            if subvol["mountpoint"] == "/":
                continue
            if not subvol["mounted"]:
                continue
            mountpoint = self.mountdir + subvol['mountpoint']
            rc = runner.show([self.umountcmd, mountpoint])
            if rc != 0:
                raise MountError("Failed to unmount subvolume %s from %s" % (subvol["subvol"], mountpoint))
            subvol["mounted"] = False

    def __create_subvolume_snapshots(self, p, pdisk):
        import time

        if self.snapshot_created:
            return

        # Remount with subvolid=0
        rc = runner.show([self.umountcmd, pdisk.mountdir])
        if rc != 0:
            raise MountError("Failed to umount %s" % pdisk.mountdir)
        if pdisk.fsopts:
            mountopts = pdisk.fsopts + ",subvolid=0"
        else:
            mountopts = "subvolid=0"
        rc = runner.show([self.mountcmd, "-o", mountopts, pdisk.disk.device, pdisk.mountdir])
        if rc != 0:
            raise MountError("Failed to umount %s" % pdisk.mountdir)

        # Create all the subvolume snapshots
        snapshotts = time.strftime("%Y%m%d-%H%M")
        for subvol in self.subvolumes:
            subvolpath = pdisk.mountdir + "/" + subvol["subvol"]
            snapshotpath = subvolpath + "_%s-1" % snapshotts
            rc = runner.show([ self.btrfscmd, "subvolume", "snapshot", subvolpath, snapshotpath ])
            if rc != 0:
                raise MountError("Failed to create subvolume snapshot '%s' for '%s', return code: %d." % (snapshotpath, subvolpath, rc))

        self.snapshot_created = True

    def mount(self):
        for dev in self.disks.keys():
            d = self.disks[dev]
            d['disk'].create()

        self.__format_disks()
        self.__map_partitions()
        self.__calculate_mountorder()

        for mp in self.mountOrder:
            p = None
            for p1 in self.partitions:
                if p1['mountpoint'] == mp:
                    p = p1
                    break

            if not p['label']:
                if p['mountpoint'] == "/":
                    p['label'] = 'platform'
                else:
                    p['label'] = mp.split('/')[-1]

            if mp == 'swap':
                import uuid
                p['uuid'] = str(uuid.uuid1())
                runner.show([self.mkswap,
                             '-L', p['label'],
                             '-U', p['uuid'],
                             p['device']])
                continue

            rmmountdir = False
            if p['mountpoint'] == "/":
                rmmountdir = True
            if p['fstype'] == "vfat" or p['fstype'] == "msdos":
                myDiskMount = VfatDiskMount
            elif p['fstype'] in ("ext2", "ext3", "ext4"):
                myDiskMount = ExtDiskMount
            elif p['fstype'] == "btrfs":
                myDiskMount = BtrfsDiskMount
            else:
                raise MountError("Fail to support file system " + p['fstype'])

            if p['fstype'] == "btrfs" and not p['fsopts']:
                p['fsopts'] = "subvolid=0"

            pdisk = myDiskMount(RawDisk(p['size'] * self.sector_size, p['device']),
                                 self.mountdir + p['mountpoint'],
                                 p['fstype'],
                                 4096,
                                 p['label'],
                                 rmmountdir,
                                 self.skipformat,
                                 fsopts = p['fsopts'])
            pdisk.mount(pdisk.fsopts)
            if p['fstype'] == "btrfs" and p['mountpoint'] == "/":
                if not self.skipformat:
                    self.__create_subvolumes(p, pdisk)
                self.__mount_subvolumes(p, pdisk)
            p['mount'] = pdisk
            p['uuid'] = pdisk.uuid

    def resparse(self, size = None):
        # Can't re-sparse a disk image - too hard
        pass
