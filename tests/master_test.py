"""
Unit test for treadmill master.
"""

# Disable C0302: Too many lines in the module
# pylint: disable=C0302

import os
import shutil
import tempfile
import time
import unittest

# Disable W0611: Unused import
import tests.treadmill_test_deps  # pylint: disable=W0611

import kazoo
import mock
import numpy as np

import treadmill
import treadmill.exc
from treadmill import master
from treadmill import scheduler
from treadmill.test import mockzk


class MasterTest(mockzk.MockZookeeperTestCase):
    """Mock test for treadmill.master."""

    def setUp(self):
        super(MasterTest, self).setUp()

        scheduler.DIMENSION_COUNT = 3

        self.root = tempfile.mkdtemp()
        os.environ['TREADMILL_MASTER_ROOT'] = self.root
        self.master = master.Master(kazoo.client.KazooClient(), 'test-cell')
        # Use 111 to assert on zkhandle value.
        # Disable the exit on exception hack for tests
        self.old_exit_on_unhandled = treadmill.exc.exit_on_unhandled
        treadmill.exc.exit_on_unhandled = mock.Mock(side_effect=lambda x: x)

    def tearDown(self):
        if self.root and os.path.isdir(self.root):
            shutil.rmtree(self.root)
        # Restore the exit on exception hack for tests
        treadmill.exc.exit_on_unhandled = self.old_exit_on_unhandled
        super(MasterTest, self).tearDown()

    def test_resource_parsing(self):
        """Tests parsing resources."""
        self.assertEquals([0, 0, 0], master.resources({}))
        self.assertEquals([1, 0, 0], master.resources({'memory': '1M'}))
        self.assertEquals([1, 10, 1024], master.resources({'memory': '1M',
                                                           'cpu': '10%',
                                                           'disk': '1G'}))

    @mock.patch('kazoo.client.KazooClient.get', mock.Mock())
    @mock.patch('kazoo.client.KazooClient.exists', mock.Mock())
    @mock.patch('kazoo.client.KazooClient.set', mock.Mock())
    @mock.patch('kazoo.client.KazooClient.create', mock.Mock())
    @mock.patch('kazoo.client.KazooClient.get_children', mock.Mock())
    def test_load_servers(self):
        """Tests load of server and bucket data."""
        zk_content = {
            'placement': {},
            'server.presence': {},
            'cell': {
                'pod:pod1': {},
                'pod:pod2': {},
            },
            'buckets': {
                'pod:pod1': {
                    'traits': None,
                },
                'pod:pod2': {
                    'traits': None,
                },
                'rack:1234': {
                    'traits': None,
                    'parent': 'pod:pod1',
                },
                'rack:2345': {
                    'traits': None,
                    'parent': 'pod:pod1',
                },
            },
            'servers': {
                'test.xx.com': {
                    'memory': '16G',
                    'disk': '128G',
                    'cpu': '400%',
                    'parent': 'rack:1234',
                },
            },
        }
        self.make_mock_zk(zk_content)
        self.master.load_buckets()
        self.master.load_cell()
        self.assertIn('pod:pod1',
                      self.master.cell.children)
        self.assertIn('rack:1234',
                      self.master.cell.children['pod:pod1'].children)

        self.master.load_servers()
        rack_1234 = self.master.cell.children['pod:pod1'].children['rack:1234']
        self.assertIn('test.xx.com', rack_1234.children)
        self.assertIn('test.xx.com', self.master.servers)
        # Check capacity - (memory, cpu, disk) vector.
        self.assertTrue(
            np.all(np.isclose(
                [16. * 1024, 400, 128. * 1024],
                rack_1234.children['test.xx.com'].init_capacity)))

        # Modify server parent, make sure it is reloaded.
        zk_content['servers']['test.xx.com']['parent'] = 'rack:2345'
        self.master.reload_servers(['test.xx.com'])

        rack_2345 = self.master.cell.children['pod:pod1'].children['rack:2345']
        self.assertNotIn('test.xx.com', rack_1234.children)
        self.assertIn('test.xx.com', rack_2345.children)

        # Modify server capacity, make sure it is refreshed.
        server_obj1 = self.master.servers['test.xx.com']
        zk_content['servers']['test.xx.com']['memory'] = '32G'
        self.master.reload_servers(['test.xx.com'])
        server_obj2 = self.master.servers['test.xx.com']

        self.assertIn('test.xx.com', rack_2345.children)
        self.assertNotEquals(id(server_obj1), id(server_obj2))

        # If server is removed, make sure it is remove from the model.
        del zk_content['servers']['test.xx.com']
        self.master.reload_servers(['test.xx.com'])
        self.assertNotIn('test.xx.com', rack_2345.children)
        self.assertNotIn('test.xx.com', self.master.servers)

    @mock.patch('kazoo.client.KazooClient.get', mock.Mock())
    @mock.patch('kazoo.client.KazooClient.exists', mock.Mock())
    @mock.patch('kazoo.client.KazooClient.get_children', mock.Mock())
    @mock.patch('kazoo.client.KazooClient.set', mock.Mock())
    @mock.patch('kazoo.client.KazooClient.create', mock.Mock())
    @mock.patch('kazoo.client.KazooClient.exists', mock.Mock())
    @mock.patch('time.time', mock.Mock(return_value=0))
    def test_adjust_server_state(self):
        """Tests load of server and bucket data."""
        zk_content = {
            'placement': {},
            'server.presence': {},
            'buckets': {
                'pod:pod1': {
                    'traits': None,
                },
                'pod:pod2': {
                    'traits': None,
                },
                'rack:1234': {
                    'traits': None,
                    'parent': 'pod:pod1',
                },
            },
            'servers': {
                'test.xx.com': {
                    'memory': '16G',
                    'disk': '128G',
                    'cpu': '400%',
                    'parent': 'rack:1234',
                },
            },
        }

        time.time.return_value = 100
        self.make_mock_zk(zk_content)
        self.master.load_buckets()
        self.master.load_servers()

        self.assertEquals((scheduler.State.down, 100),
                          self.master.servers['test.xx.com'].get_state())

        zk_content['server.presence']['test.xx.com'] = {}

        time.time.return_value = 200
        self.master.adjust_server_state('test.xx.com')
        self.assertEquals((scheduler.State.up, 200),
                          self.master.servers['test.xx.com'].get_state())

    @mock.patch('kazoo.client.KazooClient.get', mock.Mock())
    def test_load_allocations(self):
        """Tests loading allocation from serialized db data."""
        kazoo.client.KazooClient.get.return_value = ("""
---
- name: treadmill/dev
  assignments:
  - pattern: treadmlx.*
    priority: 10
  - pattern: treadmlp.test
    priority: 42
  rank: 100
  cpu: 100%
  disk: 1G
  memory: 1G
""", None)

        self.master.load_allocations()
        root = self.master.cell.allocations[None]
        self.assertIn('treadmill', root.sub_allocations)
        leaf_alloc = root.get_sub_alloc('treadmill').get_sub_alloc('dev')
        self.assertEquals(100, leaf_alloc.rank)
        self.assertEquals(1024, leaf_alloc.reserved[0])
        self.assertEquals(100, leaf_alloc.reserved[1])
        self.assertEquals(1024, leaf_alloc.reserved[2])

        assignments = self.master.assignments
        self.assertEquals(
            (10, leaf_alloc),
            assignments['treadmlx.*[#]' + '[0-9]' * 10]
        )

    @mock.patch('kazoo.client.KazooClient.get', mock.Mock())
    @mock.patch('kazoo.client.KazooClient.get_children', mock.Mock())
    @mock.patch('treadmill.master.Master._create_task', mock.Mock())
    def test_load_apps(self):
        """Tests loading application data."""
        zk_content = {
            'scheduled': {
                'foo.bar#1234': {
                    'memory': '16G',
                    'disk': '128G',
                    'cpu': '400%',
                    'affinity': 'foo.bar',
                },
            },
        }
        self.make_mock_zk(zk_content)
        self.master.load_apps()

        self.assertIn('foo.bar#1234', self.master.cell.apps)
        self.assertEquals(self.master.cell.apps['foo.bar#1234'].priority, 1)

        zk_content['scheduled']['foo.bar#1234']['priority'] = 5
        self.master.load_apps()
        self.assertEquals(len(self.master.cell.apps), 1)
        self.assertEquals(self.master.cell.apps['foo.bar#1234'].priority, 5)

    @mock.patch('kazoo.client.KazooClient.get', mock.Mock())
    @mock.patch('kazoo.client.KazooClient.get_children', mock.Mock())
    @mock.patch('treadmill.zkutils.ensure_deleted', mock.Mock())
    @mock.patch('treadmill.zkutils.put', mock.Mock())
    @mock.patch('treadmill.zkutils.update', mock.Mock())
    @mock.patch('time.time', mock.Mock(return_value=500))
    def test_reschedule(self):
        """Tests application placement."""
        srv_1 = scheduler.Server('1', [10, 10, 10],
                                 valid_until=1000, traits=0)
        srv_2 = scheduler.Server('2', [10, 10, 10],
                                 valid_until=1000, traits=0)
        srv_3 = scheduler.Server('3', [10, 10, 10],
                                 valid_until=1000, traits=0)
        srv_4 = scheduler.Server('4', [10, 10, 10],
                                 valid_until=1000, traits=0)
        cell = self.master.cell
        cell.add_node(srv_1)
        cell.add_node(srv_2)
        cell.add_node(srv_3)
        cell.add_node(srv_4)

        app1 = scheduler.Application('app1', 4, [1, 1, 1], 'app')
        app2 = scheduler.Application('app2', 3, [2, 2, 2], 'app')

        cell.add_app(cell.allocations[None], app1)
        cell.add_app(cell.allocations[None], app2)

        # At this point app1 is on server 1, app2 on server 2.
        self.master.reschedule()
        treadmill.zkutils.put.assert_has_calls([
            mock.call(mock.ANY, '/placement/1/app1', None, acl=mock.ANY),
            mock.call(mock.ANY, '/placement/2/app2', None, acl=mock.ANY),
        ])

        srv_1.state = scheduler.State.down
        self.master.reschedule()

        treadmill.zkutils.ensure_deleted.assert_has_calls([
            mock.call(mock.ANY, '/placement/1/app1'),
        ])
        treadmill.zkutils.put.assert_has_calls([
            mock.call(mock.ANY, '/placement/3/app1', None, acl=mock.ANY),
            mock.call(mock.ANY, '/placement', mock.ANY),
        ])

    @mock.patch('kazoo.client.KazooClient.get', mock.Mock())
    @mock.patch('kazoo.client.KazooClient.get_children', mock.Mock())
    @mock.patch('treadmill.zkutils.ensure_deleted', mock.Mock())
    @mock.patch('treadmill.zkutils.put', mock.Mock())
    @mock.patch('treadmill.zkutils.update', mock.Mock())
    @mock.patch('time.time', mock.Mock(return_value=500))
    def test_reschedule_once(self):
        """Tests application placement."""
        srv_1 = scheduler.Server('1', [10, 10, 10],
                                 valid_until=1000, traits=0)
        srv_2 = scheduler.Server('2', [10, 10, 10],
                                 valid_until=1000, traits=0)
        srv_3 = scheduler.Server('3', [10, 10, 10],
                                 valid_until=1000, traits=0)
        srv_4 = scheduler.Server('4', [10, 10, 10],
                                 valid_until=1000, traits=0)
        cell = self.master.cell
        cell.add_node(srv_1)
        cell.add_node(srv_2)
        cell.add_node(srv_3)
        cell.add_node(srv_4)

        app1 = scheduler.Application('app1', 4, [1, 1, 1], 'app',
                                     schedule_once=True)
        app2 = scheduler.Application('app2', 3, [2, 2, 2], 'app')

        cell.add_app(cell.allocations[None], app1)
        cell.add_app(cell.allocations[None], app2)

        # At this point app1 is on server 1, app2 on server 2.
        self.master.reschedule()
        treadmill.zkutils.put.assert_has_calls([
            mock.call(mock.ANY, '/placement/1/app1', None, acl=mock.ANY),
            mock.call(mock.ANY, '/placement/2/app2', None, acl=mock.ANY),
        ])

        srv_1.state = scheduler.State.down
        self.master.reschedule()

        treadmill.zkutils.ensure_deleted.assert_has_calls([
            mock.call(mock.ANY, '/placement/1/app1'),
            mock.call(mock.ANY, '/scheduled/app1'),
        ])

    @mock.patch('kazoo.client.KazooClient.get', mock.Mock())
    @mock.patch('kazoo.client.KazooClient.exists', mock.Mock())
    @mock.patch('kazoo.client.KazooClient.get_children', mock.Mock())
    @mock.patch('treadmill.zkutils.ensure_exists', mock.Mock())
    @mock.patch('treadmill.zkutils.ensure_deleted', mock.Mock())
    @mock.patch('treadmill.zkutils.put', mock.Mock())
    @mock.patch('treadmill.master.Master._create_task', mock.Mock())
    def test_restore_placement(self):
        """Tests application placement."""
        zk_content = {
            'placement': {
                'test.xx.com': {
                    '.data': """
                        state: up
                        since: 100
                    """,
                    'xxx.app1#1234': {
                        '.data': '{identity: 1}\n',
                    },
                    'xxx.app2#2345': '',
                }
            },
            'server.presence': {
                'test.xx.com': {},
            },
            'cell': {
                'pod:pod1': {},
                'pod:pod2': {},
            },
            'buckets': {
                'pod:pod1': {
                    'traits': None,
                },
                'pod:pod2': {
                    'traits': None,
                },
                'rack:1234': {
                    'traits': None,
                    'parent': 'pod:pod1',
                },
            },
            'servers': {
                'test.xx.com': {
                    'memory': '16G',
                    'disk': '128G',
                    'cpu': '400%',
                    'parent': 'rack:1234',
                },
            },
            'scheduled': {
                'xxx.app1#1234': {
                    'affinity': 'app1',
                    'memory': '1G',
                    'disk': '1G',
                    'cpu': '100%',
                    'identity_group': 'xxx.app1',
                },
                'xxx.app2#2345': {
                    'affinity': 'app2',
                    'memory': '1G',
                    'disk': '1G',
                    'cpu': '100%',
                },
            },
            'identity-groups': {
                'xxx.app1': {
                    'count': 5,
                }
            }
        }

        self.make_mock_zk(zk_content)
        self.master.load_buckets()
        self.master.load_cell()
        self.master.load_servers()
        self.master.load_apps()
        self.master.load_identity_groups(restore=True)

        self.assertTrue(
            self.master.servers['test.xx.com'].state is scheduler.State.up)

        # Reschedule should produce no events.
        treadmill.zkutils.ensure_deleted.reset_mock()
        treadmill.zkutils.ensure_exists.reset_mock()
        self.master.reschedule()
        self.assertFalse(treadmill.zkutils.ensure_deleted.called)
        self.assertFalse(treadmill.zkutils.ensure_exists.called)

        self.assertEquals(self.master.cell.apps['xxx.app1#1234'].identity, 1)
        self.assertEquals(
            self.master.cell.apps['xxx.app1#1234'].identity_group, 'xxx.app1')
        self.assertEquals(
            self.master.cell.identity_groups['xxx.app1'].available,
            set([0, 2, 3, 4]))

    @mock.patch('kazoo.client.KazooClient.get', mock.Mock())
    @mock.patch('kazoo.client.KazooClient.exists', mock.Mock())
    @mock.patch('kazoo.client.KazooClient.get_children', mock.Mock())
    @mock.patch('treadmill.zkutils.ensure_exists', mock.Mock())
    @mock.patch('treadmill.zkutils.ensure_deleted', mock.Mock())
    @mock.patch('treadmill.zkutils.put', mock.Mock())
    @mock.patch('treadmill.master.Master._create_task', mock.Mock())
    def test_restore_with_integrity_err(self):
        """Tests application placement."""
        zk_content = {
            'placement': {
                'test1.xx.com': {
                    '.data': """
                        state: up
                        since: 100
                    """,
                    'xxx.app1#1234': '',
                    'xxx.app2#2345': '',
                },
                'test2.xx.com': {
                    '.data': """
                        state: up
                        since: 100
                    """,
                    'xxx.app1#1234': '',
                }
            },
            'server.presence': {
                'test1.xx.com': {},
                'test2.xx.com': {},
            },
            'cell': {
                'pod:pod1': {},
                'pod:pod2': {},
            },
            'buckets': {
                'pod:pod1': {
                    'traits': None,
                },
                'pod:pod2': {
                    'traits': None,
                },
                'rack:1234': {
                    'traits': None,
                    'parent': 'pod:pod1',
                },
            },
            'servers': {
                'test1.xx.com': {
                    'memory': '16G',
                    'disk': '128G',
                    'cpu': '400%',
                    'parent': 'rack:1234',
                },
                'test2.xx.com': {
                    'memory': '16G',
                    'disk': '128G',
                    'cpu': '400%',
                    'parent': 'rack:1234',
                },
            },
            'scheduled': {
                'xxx.app1#1234': {
                    'affinity': 'app1',
                    'memory': '1G',
                    'disk': '1G',
                    'cpu': '100%',
                },
                'xxx.app2#2345': {
                    'affinity': 'app2',
                    'memory': '1G',
                    'disk': '1G',
                    'cpu': '100%',
                },
            }
        }

        self.make_mock_zk(zk_content)
        self.master.load_buckets()
        self.master.load_cell()
        self.master.load_servers()
        self.master.load_apps()

        self.assertIn('xxx.app2#2345',
                      self.master.servers['test1.xx.com'].apps)
        self.assertIsNone(self.master.cell.apps['xxx.app1#1234'].server)

    @mock.patch('kazoo.client.KazooClient.get', mock.Mock())
    @mock.patch('kazoo.client.KazooClient.get_children', mock.Mock())
    @mock.patch('treadmill.zkutils.ensure_exists', mock.Mock())
    @mock.patch('treadmill.zkutils.ensure_deleted', mock.Mock())
    @mock.patch('treadmill.zkutils.put', mock.Mock())
    @mock.patch('treadmill.master.Master.load_allocations', mock.Mock())
    @mock.patch('treadmill.master.Master.load_apps', mock.Mock())
    @mock.patch('treadmill.master.Master.load_app', mock.Mock())
    def test_process_events(self):
        """Tests application placement."""
        zk_content = {
            'events': {
                '001-allocations-12345': {},
                '000-apps-12346': {
                    '.data': """
                        - xxx.app1#1234
                        - xxx.app2#2345
                    """
                },
            },
        }

        self.make_mock_zk(zk_content)
        self.master.watch('/events')
        while True:
            try:
                event = self.master.queue.popleft()
                self.master.process(event)
            except IndexError:
                break

        self.assertTrue(treadmill.master.Master.load_allocations.called)
        self.assertTrue(treadmill.master.Master.load_apps.called)
        treadmill.master.Master.load_app.assert_has_calls([
            mock.call('xxx.app1#1234'),
            mock.call('xxx.app2#2345'),
        ])

    @mock.patch('kazoo.client.KazooClient.get', mock.Mock())
    @mock.patch('kazoo.client.KazooClient.create', mock.Mock())
    def test_create_apps(self):
        """Tests app api."""
        zkclient = kazoo.client.KazooClient()
        kazoo.client.KazooClient.create.return_value = '/scheduled/foo.bar#12'

        master.create_apps(zkclient, 'foo.bar', {}, 3)
        kazoo.client.KazooClient.create.assert_has_calls(
            [mock.call('/scheduled/foo.bar#',
                       '{}\n',
                       makepath=True,
                       sequence=True,
                       ephemeral=False,
                       acl=mock.ANY)] * 3)

    @mock.patch('kazoo.client.KazooClient.get', mock.Mock(
        return_value=('{}', None)))
    @mock.patch('kazoo.client.KazooClient.set', mock.Mock())
    @mock.patch('kazoo.client.KazooClient.create', mock.Mock())
    def test_update_app_priority(self):
        """Tests app api."""
        zkclient = kazoo.client.KazooClient()

        kazoo.client.KazooClient.create.return_value = '/events/001-apps-1'
        master.update_app_priorities(zkclient, {'foo.bar#1': 10,
                                                'foo.bar#2': 20})
        kazoo.client.KazooClient.set.assert_has_calls(
            [mock.call('/scheduled/foo.bar#1', '{priority: 10}\n'),
             mock.call('/scheduled/foo.bar#2', '{priority: 20}\n')])

        # Verify that event is placed correctly.
        kazoo.client.KazooClient.create.assert_called_with(
            '/events/001-apps-', '[foo.bar#1, foo.bar#2]\n',
            makepath=True, acl=mock.ANY, sequence=True, ephemeral=False)

    @mock.patch('kazoo.client.KazooClient.get', mock.Mock(
        return_value=('{}', None)))
    @mock.patch('kazoo.client.KazooClient.set', mock.Mock())
    @mock.patch('kazoo.client.KazooClient.create', mock.Mock())
    @mock.patch('kazoo.client.KazooClient.exists',
                mock.Mock(return_value=False))
    def test_cell_insert_bucket(self):
        """Tests inserting bucket into cell."""
        zkclient = kazoo.client.KazooClient()
        kazoo.client.KazooClient.create.return_value = '/events/000-cell-1'
        master.cell_insert_bucket(zkclient, 'pod:pod1')

        kazoo.client.KazooClient.create.assert_has_calls([
            mock.call('/cell/pod:pod1', '',
                      makepath=True, acl=mock.ANY,
                      sequence=False),
            mock.call('/events/000-cell-', '',
                      makepath=True, acl=mock.ANY,
                      sequence=True, ephemeral=False)
        ])


if __name__ == '__main__':
    unittest.main()
