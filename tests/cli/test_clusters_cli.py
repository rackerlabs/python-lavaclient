import socks
import subprocess
import pytest
from mock import patch, call, MagicMock
from datetime import datetime
from copy import deepcopy
from six.moves import StringIO
from contextlib import contextmanager

from lavaclient.cli import main
from lavaclient.api.response import Cluster, ClusterDetail, NodeGroup, Node
from lavaclient.api.clusters import DEFAULT_SSH_KEY, parse_node_group
from lavaclient.error import RequestError


def check_cluster_detail(print_single_table, print_table):
    assert print_single_table.call_count == 1
    (data, header), kwargs = print_single_table.call_args
    assert data == ['cluster_id', 'cluster_name', 'ACTIVE', 'stack_id',
                    datetime(2014, 1, 1), 1, 'username', 1.0]
    assert header == ClusterDetail.table_header
    assert kwargs['title'] == 'Cluster'

    assert print_table.call_count == 3

    (data, header), kwargs = print_table.call_args_list[0]
    assert list(data) == [('SSH Key', 'mykey'),
                          ('Cloud Files', 'username'),
                          ('Amazon S3', 'accesskey'),
                          ('Ambari', 'username')]
    assert header == ('Type', 'Name')
    assert kwargs['title'] == 'Credentials'

    (data, header), kwargs = print_table.call_args_list[1]
    assert list(data) == [('id', 'hadoop1-60', 1, 'component')]
    assert header == NodeGroup.table_header
    assert kwargs['title'] == 'Node Groups'

    (data, header), kwargs = print_table.call_args_list[2]
    assert list(data) == [['script_id', 'name', 'node_id', 'status']]
    assert header == ('ID', 'Name', 'Node ID', 'Status')
    assert kwargs['title'] == 'Scripts'


@pytest.mark.parametrize('group', [
    'foo(count=1)',
    'foo123(count=1)',
    'foo-bar(count=1)',
    'foo-1(count=1)',
    'foo_bar(count=1)',
    '190efgkln235u90erty901234124(count=1)',
])
def test_parse_node_group(group):
    result = parse_node_group(group)
    assert result == {'id': group.split('(', 1)[0], 'count': '1'}


@patch('sys.argv', ['lava', 'clusters', 'list'])
def test_list(print_table, mock_client, clusters_response):
    mock_client._request.return_value = clusters_response
    main()

    (data, header), kwargs = print_table.call_args
    assert list(data) == [['cluster_id', 'cluster_name', 'ACTIVE',
                           'stack_id', datetime(2014, 1, 1)]]
    assert header == Cluster.table_header
    assert kwargs['title'] is None


@patch('sys.argv', ['lava', 'clusters', 'get', 'cluster_id'])
def test_get(print_table, print_single_table, mock_client, cluster_response):
    mock_client._request.return_value = cluster_response
    main()

    check_cluster_detail(print_single_table, print_table)


@pytest.mark.parametrize('args,node_groups', [
    ([], None),
    (['--node-group', 'id'], [{'id': 'id'}]),
    (['--node-group', 'id(count=10)'], [{'id': 'id', 'count': 10}]),
    (['--node-group', 'id(flavor_id=flavor)'],
     [{'id': 'id', 'flavor_id': 'flavor'}]),
    (['--node-group', 'id(count=10, flavor_id=flavor)'],
     [{'id': 'id', 'flavor_id': 'flavor', 'count': 10}]),
])
def test_create_node_groups(args, node_groups, print_table,
                            print_single_table, mock_client,
                            cluster_response):
    mock_client._request.return_value = cluster_response

    base_args = ['lava', 'clusters', 'create', 'name', 'stack_id']
    with patch('sys.argv', base_args + args):
        main()

    kwargs = mock_client._request.call_args[1]
    assert kwargs['json']['cluster'].get('node_groups') == node_groups

    check_cluster_detail(print_single_table, print_table)


@pytest.mark.parametrize('region', ['region-0', 'region-1'])
def test_create_regions(mock_client, cluster_response, region):
    mock_client._request.return_value = cluster_response
    args = ['lava', 'clusters', 'create', 'name', 'stack_id',
            '--cluster-region', region]
    with patch('sys.argv', args):
        main()

    kwargs = mock_client._request.call_args[1]
    assert kwargs['json']['cluster']['region'] == region


@pytest.mark.parametrize('args,keys', [
    ([], [DEFAULT_SSH_KEY]),
    (['--ssh-key', 'key1'], ['key1']),
    (['--ssh-key', 'key1', '--ssh-key', 'key2'], ['key1', 'key2']),
])
def test_create_ssh_keys(args, keys, mock_client, print_table,
                         print_single_table, cluster_response):
    mock_client._request.return_value = cluster_response

    base_args = ['lava', 'clusters', 'create', 'name', 'stack_id']
    with patch('sys.argv', base_args + args):
        main()

        kwargs = mock_client._request.call_args[1]
        assert kwargs['json']['cluster'].get('ssh_keys') == keys


@contextmanager
def mock_keyfile(*args, **kwargs):
    yield StringIO('a' * 50)


@pytest.mark.usefixtures('print_table', 'print_single_table')
@patch('sys.argv', ['lava', 'clusters', 'create', 'name', 'stack_id'])
@patch('lavaclient.api.clusters.open', mock_keyfile, create=True)
@patch('lavaclient.api.clusters.confirm')
def test_create_no_ssh_key(confirm, mock_client, cluster_response,
                           ssh_key_response):
    mock_client._request.side_effect = [
        RequestError('Cannot find requested ssh_keys: {0}'.format(
            [DEFAULT_SSH_KEY])),
        ssh_key_response,
        cluster_response,
    ]
    confirm.return_value = True

    with patch.object(mock_client.clusters._args, 'headless', False):
        main()

    assert confirm.called
    assert mock_client._request.call_count == 3


@pytest.mark.usefixtures('print_table', 'print_single_table')
@patch('sys.argv', ['lava', 'clusters', 'create', 'name', 'stack_id'])
@patch('lavaclient.api.clusters.open', mock_keyfile, create=True)
def test_create_no_ssh_key_headless(mock_client, cluster_response,
                                    ssh_key_response):
    mock_client._request.side_effect = [
        RequestError('Cannot find requested ssh_keys: {0}'.format(
            [DEFAULT_SSH_KEY])),
    ]

    pytest.raises(Exception, main)

    assert mock_client._request.call_count == 1


@pytest.mark.usefixtures('print_table', 'print_single_table')
@patch('sys.argv', ['lava', 'clusters', 'create', 'name', 'stack_id',
                    '--ssh-key', 'mykey'])
def test_create_missing_ssh_key(mock_client, cluster_response):
    mock_client._request.side_effect = RequestError(
        'Cannot find requested ssh_keys: mykey')

    pytest.raises(Exception, main)
    assert mock_client._request.call_count == 1


def test_create_with_scripts(print_table, print_single_table, mock_client,
                             cluster_response):
    mock_client._request.return_value = cluster_response

    with patch('sys.argv', ['lava', 'clusters', 'create', 'name', 'stack_id',
                            '--user-script', 'id1', '--user-script', 'id2']):
        main()
        kwargs = mock_client._request.call_args[1]
        assert kwargs['json']['cluster'].get('scripts') == [{'id': 'id1'},
                                                            {'id': 'id2'}]


@pytest.mark.parametrize('args,node_groups', [
    (['--node-group', 'id(count=10)'], [{'id': 'id', 'count': 10}]),
    (['--node-group', 'id1(count=10)', '--node-group', 'id2(count=3)'],
     [{'id': 'id1', 'count': 10}, {'id': 'id2', 'count': 3}]),
])
def test_resize(args, node_groups, print_table, print_single_table,
                mock_client, cluster_response):
    mock_client._request.return_value = cluster_response

    base_args = ['lava', 'clusters', 'resize', 'cluster_id']

    with patch('sys.argv', base_args + args):
        main()

        kwargs = mock_client._request.call_args[1]
        assert kwargs['json']['cluster'].get('node_groups') == node_groups


@pytest.mark.parametrize('args,expected', [
    (['--credential', 's3=s3_cred_update'],
     [{'name': 's3_cred_update', 'type': 's3'}]),
    (['--credential', 'ssh_keys=ssh_cred_update',
      '--credential', 'cloud_files=cf_cred_update'],
     [{'name': 'ssh_cred_update', 'type': 'ssh_keys'},
      {'name': 'cf_cred_update', 'type': 'cloud_files'}])
])
def test_update_credentials(args, expected, print_table, print_single_table,
                            mock_client, cluster_response):
    mock_client._request.return_value = cluster_response

    base_args = ['lava', 'clusters', 'update_credentials', 'cluster_id']

    with patch('sys.argv', base_args + args):
        main()

        kwargs = mock_client._request.call_args[1]
        creds = kwargs['json']['cluster']['credentials']
        assert len(creds) == len(expected)
        for cred in creds:
            assert cred in expected


@patch('lavaclient.api.clusters.confirm', MagicMock(return_value=True))
def test_delete_ssh_credentials(print_table, print_single_table,
                                mock_client, cluster_response):
    mock_client._request.return_value = cluster_response

    args = ['lava', 'clusters', 'delete_ssh_credentials', 'cluster_id',
            'delete_this_key', 'delete_that_key']
    expected = {'remove_credentials':
                [{'name': 'delete_this_key',
                  'type': 'ssh_keys'},
                 {'name': 'delete_that_key',
                  'type': 'ssh_keys'}]}
    with patch('sys.argv', args):
        main()

        kwargs = mock_client._request.call_args[1]
        assert kwargs['json']['cluster'] == expected


@patch('sys.argv', ['lava', 'clusters', 'delete', 'cluster_id'])
@patch('lavaclient.api.clusters.confirm', MagicMock(return_value=True))
def test_delete(mock_client, cluster_response, print_single_table):
    mock_client._request.return_value = cluster_response

    main()
    assert mock_client._request.call_count == 2

    call1, call2 = mock_client._request.call_args_list

    assert call1[0] == ('GET', 'clusters/cluster_id')
    assert call2[0] == ('DELETE', 'clusters/cluster_id')


@patch('sys.argv', ['lava', 'clusters', 'wait', 'cluster_id'])
@patch('lavaclient.api.clusters.elapsed_minutes',
       MagicMock(side_effect=[0.0, 1.0, 2.0]))
@patch('time.sleep', MagicMock)
@patch('sys.stdout')
def test_wait(stdout, print_table, print_single_table, mock_client,
              cluster_response):
    building = deepcopy(cluster_response)
    building['cluster']['status'] = 'BUILDING'
    configuring = deepcopy(cluster_response)
    configuring['cluster']['status'] = 'CONFIGURING'
    active = deepcopy(cluster_response)
    active['cluster']['status'] = 'ACTIVE'

    mock_client._request.side_effect = [
        building, building, configuring, active
    ]

    with patch.object(mock_client.clusters, '_command_line', True):
        main()
        stdout.write.assert_has_calls([
            call('Waiting for cluster cluster_id'),
            call('\n'),
            call('Status: BUILDING (Elapsed time: 0.0 minutes)'),
            call('{0}Status: CONFIGURING (Elapsed time: 1.0 minutes)'.format(
                '\b' * 44)),
            call('{0}Status: ACTIVE (Elapsed time: 2.0 minutes)'.format(
                '\b' * 47)),
        ])

    check_cluster_detail(print_single_table, print_table)


@patch('sys.argv', ['lava', 'clusters', 'nodes', 'cluster_id'])
def test_nodes(print_table, mock_client, nodes_response):
    mock_client._request.return_value = nodes_response
    main()

    assert print_table.call_count == 2
    (data, header), kwargs = print_table.call_args_list[0]
    alldata = [entry for entry in list(data)[0]]
    assert alldata[:6] == ['node_id', 'NODENAME', 'node_group', 'ACTIVE',
                           '1.2.3.4', '5.6.7.8']
    assert header == Node.table_header
    assert kwargs['title'] == 'Nodes'

    (data, header), kwargs = print_table.call_args_list[1]
    alldata = [entry for entry in list(data)[0]]
    assert alldata[:6] == ['NODENAME', 'component_name', 'Component name',
                           'http://host']
    assert header == ('Node', 'ID', 'Name', 'URI')
    assert kwargs['title'] == 'Components'


@pytest.mark.parametrize('status', ['ACTIVE', 'IMPAIRED'])
@patch('subprocess.Popen')
def test_ssh_proxy(popen, status, mock_client, cluster_response,
                   nodes_response):
    del nodes_response['nodes'][0]['components'][0]['uri']

    cluster_response['cluster']['status'] = status

    popen.return_value = MagicMock(
        poll=MagicMock(return_value=None),
        communicate=MagicMock(return_value=('stdout', 'stderr'))
    )
    mock_client._request.side_effect = [cluster_response, nodes_response]

    with patch('sys.argv', ['lava', 'clusters', 'ssh_proxy', 'cluster_id',
                            '--node-name', 'NODENAME', '--port', '54321']):
        main()

    popen.assert_called_with(
        ['ssh', '-o', 'PasswordAuthentication=no', '-o', 'BatchMode=yes',
         '-N', '-D', '54321', 'username@1.2.3.4'],
        stderr=subprocess.STDOUT,
        stdout=subprocess.PIPE)


@pytest.mark.parametrize('status', ['ERROR', 'PENDING'])
@patch('subprocess.Popen')
def test_ssh_proxy_nonterminal(popen, status, mock_client, cluster_response,
                               nodes_response):
    del nodes_response['nodes'][0]['components'][0]['uri']

    cluster_response['cluster']['status'] = status

    popen.return_value = MagicMock(
        poll=MagicMock(return_value=None),
        communicate=MagicMock(return_value=('stdout', 'stderr'))
    )
    mock_client._request.side_effect = [cluster_response, nodes_response]

    with patch('sys.argv', ['lava', 'clusters', 'ssh_proxy', 'cluster_id',
                            '--node-name', 'NODENAME', '--port', '54321']):
        pytest.raises(Exception, main)


@pytest.mark.parametrize('failure', [None,
                                     socks.ProxyConnectionError,
                                     socks.GeneralProxyError])
@patch('subprocess.Popen')
@patch('lavaclient.util.test_socks_connection')
def test_ssh_proxy_errors(test_connection, popen, failure, mock_client,
                          cluster_response, nodes_response):
    del nodes_response['nodes'][0]['components'][0]['uri']

    popen.return_value = MagicMock(
        poll=MagicMock(return_value=None),
        communicate=MagicMock(return_value=('stdout', 'stderr'))
    )
    if failure:
        test_connection.side_effect = [failure, 200]
    else:
        test_connection.return_value = 200

    mock_client._request.side_effect = [cluster_response, nodes_response]

    with patch('sys.argv', ['lava', 'clusters', 'ssh_proxy', 'cluster_id',
                            '--node-name', 'NODENAME', '--port',
                            '54321']):
        main()

    popen.assert_called_with(
        ['ssh', '-o', 'PasswordAuthentication=no', '-o', 'BatchMode=yes',
         '-N', '-D', '54321', 'username@1.2.3.4'],
        stderr=subprocess.STDOUT,
        stdout=subprocess.PIPE)


@patch('subprocess.Popen')
def test_ssh_proxy_cmd_fail(popen, mock_client, cluster_response,
                            nodes_response):
    popen.return_value = MagicMock(
        poll=MagicMock(return_value=1),
        communicate=MagicMock(return_value=('stdout', 'stderr'))
    )
    mock_client._request.side_effect = [cluster_response, nodes_response]

    with patch('sys.argv', ['lava', 'clusters', 'ssh_proxy', 'cluster_id',
                            '--node-name', 'NODENAME', '--port',
                            '54321']):
        pytest.raises(Exception, main)


@pytest.mark.parametrize('error_code', [400, 500])
@patch('subprocess.Popen')
@patch('lavaclient.util.test_socks_connection')
def test_ssh_proxy_http_fail(test_connection, popen, error_code, mock_client,
                             cluster_response, nodes_response):
    popen.return_value = MagicMock(
        poll=MagicMock(return_value=None),
        communicate=MagicMock(return_value=('stdout', 'stderr'))
    )
    test_connection.return_value = error_code
    mock_client._request.side_effect = [cluster_response, nodes_response]

    with patch('sys.argv', ['lava', 'clusters', 'ssh_proxy', 'cluster_id',
                            '--node-name', 'NODENAME', '--port',
                            '54321']):
        pytest.raises(Exception, main)


@patch('subprocess.Popen')
def test_ssh_tunnel_nodename(popen, mock_client, cluster_response,
                             nodes_response):
    popen.return_value = MagicMock(
        poll=MagicMock(return_value=None),
        communicate=MagicMock(return_value=('stdout', 'stderr'))
    )
    mock_client._request.side_effect = [cluster_response, nodes_response]

    with patch('sys.argv', ['lava', 'clusters', 'ssh_tunnel', 'cluster_id',
                            '123', '456', '--node-name', 'NODENAME']):
        main()

    popen.assert_called_with(
        ['ssh', '-o', 'PasswordAuthentication=no', '-o', 'BatchMode=yes',
         '-N', '-L', '123:NODENAME:456', 'username@1.2.3.4'],
        stderr=subprocess.STDOUT,
        stdout=subprocess.PIPE)


@patch('subprocess.Popen')
def test_ssh_tunnel_component(popen, mock_client, cluster_response,
                              nodes_response):
    popen.return_value = MagicMock(
        poll=MagicMock(return_value=None),
        communicate=MagicMock(return_value=('stdout', 'stderr'))
    )
    mock_client._request.side_effect = [cluster_response, nodes_response]

    with patch('sys.argv', ['lava', 'clusters', 'ssh_tunnel', 'cluster_id',
                            '123', '456', '--component', 'component_name']):
        main()

    popen.assert_called_with(
        ['ssh', '-o', 'PasswordAuthentication=no', '-o', 'BatchMode=yes',
         '-N', '-L', '123:NODENAME:456', 'username@1.2.3.4'],
        stderr=subprocess.STDOUT,
        stdout=subprocess.PIPE)


def test_ssh_tunnel_arg_failure(mock_client):
    with patch('sys.argv', ['lava', 'clusters', 'ssh_tunnel', 'cluster_id',
                            '123', '456', '--component', 'component_name',
                            '--node-name', 'NODENAME']):
        pytest.raises(Exception, main)


@pytest.mark.parametrize('force,action,services', [
    (False, 'start', []),
    (False, 'start', ['svc1']),
    (False, 'start', ['svc1', 'svc2']),
    (False, 'stop', []),
    (False, 'stop', ['svc1']),
    (False, 'stop', ['svc1', 'svc2']),
    (False, 'restart', []),
    (False, 'restart', ['svc1']),
    (False, 'restart', ['svc1', 'svc2']),
    (True, 'start', []),
    (True, 'start', ['svc1']),
    (True, 'start', ['svc1', 'svc2']),
    (True, 'stop', []),
    (True, 'stop', ['svc1']),
    (True, 'stop', ['svc1', 'svc2']),
    (True, 'restart', []),
    (True, 'restart', ['svc1']),
    (True, 'restart', ['svc1', 'svc2']),
])
@patch('lavaclient.api.clusters.confirm')
def test_services(confirm, force, action, services, mock_client,
                  cluster_response, print_table, print_single_table):
    mock_client._request.return_value = cluster_response

    args = ['lava', 'clusters',  'services', action, 'cluster_id'] + services
    if force:
        args.append('--force')

    with patch('sys.argv', args):
        main()

    confirm.assert_not_called() if force else confirm.assert_called()

    mock_client._request.assert_called_with(
        'PUT', 'clusters/cluster_id',
        json={
            'cluster': {
                'control_services': {
                    'action': action,
                    'services': [{'name': name} for name in services],
                }
            }
        }
    )

    check_cluster_detail(print_single_table, print_table)
