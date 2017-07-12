#!/usr/bin/env python3
#
# This file is part of Script of Scripts (SoS), a workflow system
# for the execution of commands and scripts in different languages.
# Please visit https://github.com/vatlab/SOS for more information.
#
# Copyright (C) 2016 Bo Peng (bpeng@mdanderson.org)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#

import os
import unittest

from sos.utils import env
from sos.target import FileTarget
from sos.hosts import Host
import subprocess

has_docker = True
try:
    subprocess.check_output('docker ps | grep test_sos', shell=True).decode()
except subprocess.CalledProcessError:
    subprocess.call('sh build_test_docker.sh', shell=True)
    try:
        subprocess.check_output('docker ps | grep test_sos', shell=True).decode()
    except subprocess.CalledProcessError:
        print('Failed to set up a docker machine with sos')
        has_docker = False

#if sys.platform == 'win32':
#    with open('~/docker.yml', 'r') as d:
#        cfg = d.read()
#    with open('~/docker.yml', 'w') as d:
#        d.write(cfg.replace('/home/', 'c:\\Users\\'))

class TestRemote(unittest.TestCase):
    def setUp(self):
        env.reset()
        #self.resetDir('~/.sos')
        self.temp_files = []
        Host.reset()
        # remove .status file left by failed workflows.
        subprocess.call('sos purge', shell=True)

    def tearDown(self):
        for f in self.temp_files:
            FileTarget(f).remove('both')


    @unittest.skipIf(not has_docker, "Docker container not usable")
    def testRemoteExecute(self):
        if os.path.isfile('result_remote.txt'):
            os.remove('result_remote.txt')
        with open('test_remote.sos', 'w') as tr:
            tr.write('''
[10]
output: 'result_remote.txt'
task:

run:
  echo 'a' > 'result_remote.txt'

''')
        self.assertEqual(subprocess.call('sos run test_remote.sos -c ~/docker.yml -r docker -s force -w', shell=True), 0)
        self.assertFalse(FileTarget('result_remote.txt').exists())
        self.assertEqual(subprocess.call('sos preview result_remote.txt -c ~/docker.yml -r docker', shell=True), 0)
        self.assertNotEqual(subprocess.call('sos preview result_remote.txt', shell=True), 0)
        self.assertEqual(subprocess.call('sos pull result_remote.txt -c ~/docker.yml --from docker', shell=True), 0)
        self.assertTrue(FileTarget('result_remote.txt').exists())
        self.assertEqual(subprocess.call('sos preview result_remote.txt', shell=True), 0)

if __name__ == '__main__':
    unittest.main()
