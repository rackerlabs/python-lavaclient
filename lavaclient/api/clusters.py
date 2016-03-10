#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Create, destroy, and otherwise interact with Rackspace CloudBigData clusters
"""

import itertools
import argparse
import re
import six
import logging
import time
import sys
import socket
import os.path
import functools
from getpass import getuser
from collections import defaultdict
from datetime import datetime, timedelta
from figgis import Config, ListField, Field, PropertyError, ValidationError

from lavaclient.api import resource
from lavaclient.api.response import Cluster, ClusterDetail, Node, ReprMixin
from lavaclient import error
from lavaclient.validators import Length, Range, List
from lavaclient.util import (CommandLine, argument, command, display_table,
                             coroutine, create_socks_proxy, expand, confirm,
                             display, create_ssh_tunnel, display_result,
                             deprecation)
from lavaclient.log import NullHandler


LOG = logging.getLogger(__name__)
LOG.addHandler(NullHandler())


WAIT_INTERVAL = 30
MIN_INTERVAL = 10

IN_PROGRESS_STATES = frozenset([
    'BUILDING', 'BUILD', 'CONFIGURING', 'CONFIGURED', 'UPDATING', 'REBOOTING',
    'RESIZING', 'WAITING'])
FINAL_STATES = frozenset(['ACTIVE', 'ERROR', 'IMPAIRED'])
INVALID_USERNAMES = frozenset(['root'])
USERNAME_REGEX = re.compile(r'^[a-zA-Z_][\w-]{0,31}$')

DEFAULT_SSH_KEY = '{0}@{1}'.format(getuser(), socket.gethostname())
DEFAULT_SSH_PUBKEY = os.path.join('$HOME', '.ssh', 'id_rsa.pub')


def natural_number(value):
    """Argparse type to force a non-negative integer value"""
    intval = int(value)
    if intval < 0:
        raise argparse.ArgumentTypeError('Must be a non-negative integer')

    return intval


def id_or_name(func):
    """Decorate a function to either accept a cluster ID or cluster name as
    the first argument"""
    @functools.wraps(func)
    def wrapped(self, maybe_cluster_id):
        try:
            return func(self, maybe_cluster_id)
        except error.RequestError as exc:
            if exc.code != 404:
                raise

            cluster = next(
                (cluster for cluster in self._client.clusters.list()
                 if cluster.name == maybe_cluster_id),
                None
            )

            if cluster is None:
                raise

            return func(self, cluster.id)

    return wrapped


######################################################################
# API Responses
######################################################################

class ClustersResponse(Config, ReprMixin):

    """Response from /clusters"""

    clusters = ListField(Cluster, required=True)


class ClusterResponse(Config, ReprMixin):

    """Response from /clusters/<cluster_id>"""

    cluster = Field(ClusterDetail, required=True)


######################################################################
# API Request Data
######################################################################

class ClusterCreateScript(Config):

    id = Field(six.text_type, required=True)


class ClusterCreateNodeGroups(Config):

    __allow_extra__ = False

    id = Field(six.text_type, required=True,
               validator=Length(min=1, max=255))
    count = Field(int, validator=Range(min=1, max=100))
    flavor_id = Field(six.text_type)


class ClusterUpdateCredential(Config):

    __allow_extra__ = False

    name = Field(six.text_type, required=True)
    type = Field(six.text_type, required=True)


class ClusterCreateCredential(Config):

    name = Field(six.text_type, required=True,
                 validator=Length(min=2, max=255))


class ClusterCreateCredential(Config):

    type = Field(six.text_type, required=True)
    credential = Field(ClusterCreateCredential, required=True)


def valid_username(value):
    if value.lower() in INVALID_USERNAMES:
        raise ValidationError('Reserved username: {0}'.format(value))
    elif not USERNAME_REGEX.match(value):
        raise ValidationError(
            'Invalid username; must be 32 characters or less, only include '
            'letters, numbers, hyphens, and underscores, and must start with '
            'a letter or underscore')

    return True


class ClusterCreateRequest(Config):

    """POST data to create cluster"""

    name = Field(six.text_type, required=True,
                 validator=Length(min=1, max=255))
    username = Field(six.text_type, required=True,
                     validator=[Length(min=2, max=255), valid_username])
    ssh_keys = ListField(six.text_type, required=True,
                         validator=List(Length(min=1, max=255)))
    stack_id = Field(six.text_type, required=True)
    node_groups = ListField(ClusterCreateNodeGroups)
    scripts = ListField(ClusterCreateScript)
    connectors = ListField(ClusterCreateCredential)
    credentials = ListField(ClusterCreateCredential)
    region = Field(six.text_type, required=False)


class ClusterResizeRequest(Config):

    """PUT data to resize cluster"""

    node_groups = ListField(ClusterCreateNodeGroups)


class ClusterUpdateRequest(Config):

    """Update a cluster resource"""

    cluster = Field(ClusterResizeRequest)


class ClusterCredentialsRequest(Config):

    credentials = ListField(ClusterUpdateCredential)


class ClusterCredentialsRemovalRequest(Config):

    remove_credentials = ListField(ClusterUpdateCredential)


######################################################################
# API Resource
######################################################################

def parse_node_group(value):
    """Parse command-line node group string, e.g.
    `slave(count=1, flavor_id=hadoop1-7)`"""
    var_rgx = r'(?:[a-zA-Z_]\w*)'
    expr_rgx = r'(?:{var}\s*=\s*.*?)'.format(var=var_rgx)
    node_group_rgx = r'([^()]+)(?:\(({expr}?(?:\s*,\s*{expr})*)\))?$'.format(
        expr=expr_rgx)

    match = re.match(node_group_rgx, value)
    if match is None:
        raise argparse.ArgumentTypeError(
            'Invalid node group: {0}'.format(value))

    node_group, argstr = match.groups()

    if argstr:
        exprs = [item.split('=', 1) for item in argstr.split(',')]
        keywords = dict((key.strip(), value.strip()) for key, value in exprs)
    else:
        keywords = {}

    data = {'id': node_group}
    data.update(keywords)

    # Make sure that node group is valid
    try:
        ClusterCreateNodeGroups(data)
    except PropertyError as exc:
        LOG.error('Could not parse node group: %s', value, exc_info=exc)
        raise argparse.ArgumentTypeError(
            'Invalid node group: {0}'.format(value))

    return data


def elapsed_minutes(start):
    return (datetime.now() - start).total_seconds() / 60


def parse_credential(value):
    """Parse command-line credential string, e.g. `cloud_files=my_files`"""
    match = re.match(r'([A-Za-z]\w*)=(.+)$', value)
    if not match:
        raise argparse.ArgumentTypeError('Must be in the form of type=name')

    return {match.group(1): match.group(2)}


@six.add_metaclass(CommandLine)
class Resource(resource.Resource):
    """
    Clusters API methods
    """

    @command(
        parser_options=dict(
            description='List all existing clusters',
        ),
    )
    @display_table(Cluster)
    def list(self):
        """
        List clusters that belong to the tenant specified in the client

        :returns: List of :class:`~lavaclient.api.response.Cluster` objects
        """
        return self._parse_response(
            self._client._get('clusters'),
            ClustersResponse,
            wrapper='clusters')

    @command(
        parser_options=dict(
            description='Display an existing cluster in detail',
        ),
    )
    @display_table(ClusterDetail)
    def get(self, cluster_id):
        """
        Get the cluster corresponding to the cluster ID

        :param cluster_id: Cluster ID
        :returns: :class:`~lavaclient.api.response.ClusterDetail`
        """
        @id_or_name
        def _func(self, cluster_id):
            return self._parse_response(
                self._client._get('clusters/' + six.text_type(cluster_id)),
                ClusterResponse,
                wrapper='cluster')

        return _func(self, cluster_id)

    def _gather_node_groups(self, node_groups):
        """Transform node_groups into a list of dicts"""
        if isinstance(node_groups, dict):
            node_group_list = []
            for key, node_group in six.iteritems(node_groups):
                node_group.update(id=key)
                node_group_list.append(node_group)

            return node_group_list
        else:
            return node_groups

    def create(self, name, stack_id, username=None, ssh_keys=None,
               user_scripts=None, node_groups=None, connectors=None,
               wait=False, credentials=None, region=None):
        """
        Create a cluster

        :param name: Cluster name
        :param stack_id: Valid stack identifier
        :param username: User to create on the cluster; defaults to local user
        :param ssh_keys: List of SSH keys; if none is specified, it will use
                         the key `user@hostname`, creating the key from
                         `$HOME/.ssh/id_rsa.pub` if it doesn't exist.
        :param node_groups: `dict` of `(node_group_id, attrs)` pairs, in which
                            `attrs` is a `dict` of node group attributes.
                            Instead of a `dict`, you may give a `list` of
                            `dicts`, each containing the `id` key. Currently
                            supported attributes are `flavor_id` and `count`
        :param user_scripts: List of user script ID's; See
                             :meth:`lavaclient.api.scripts.Resource.create`
        :param credentials: List of credentials to use. Each item must be a
                            dictionary of `(type, name)` pairs
        :param connectors: List of connector credentials to use. Each item
                           must be a dictionary of `(type, name)` pairs.
                           Deprecated in favor of `credentials`
        :param wait: If `True`, wait for the cluster to become active before
                     returning
        :param region: The region to create the cluster in
        :returns: :class:`~lavaclient.api.response.ClusterDetail`
        """
        if ssh_keys is None:
            ssh_keys = [DEFAULT_SSH_KEY]
        if username is None:
            username = getuser()
        if connectors is None:
            connectors = []
        if credentials is None:
            credentials = []

        if connectors:
            deprecation('connectors are deprecated; use credentials')

        data = dict(
            name=name,
            username=username,
            ssh_keys=ssh_keys,
            stack_id=stack_id
        )

        if region:
            data['region'] = region

        if node_groups:
            data.update(node_groups=self._gather_node_groups(node_groups))

        if user_scripts:
            data.update(scripts=[{'id': script} for script in user_scripts])

        if connectors or credentials:
            cdata = []
            for credential in connectors + credentials:
                ctype, name = six.next(six.iteritems(credential))
                cdata.append({'type': ctype, 'credential': {'name': name}})
            data.update(credentials=cdata)

        request_data = self._marshal_request(
            data, ClusterCreateRequest, wrapper='cluster')

        cluster = self._parse_response(
            self._client._post('clusters', json=request_data),
            ClusterResponse,
            wrapper='cluster')

        if wait:
            return self.wait(cluster.id)

        return cluster

    @command(
        parser_options=dict(
            description='Resize an existing Lava cluster',
        ),
        node_groups=argument(
            type=parse_node_group, action='append',
            help='Node group options; may be used multiple times to resize '
                 'multiple node groups. Each option should be in the form '
                 '\'<id>(count=<value>)\', where <id> is a valid node '
                 'group ID for the cluster and the count is the '
                 'option to specify new count for that node group. '),
        wait=argument(
            action='store_true',
            help='Wait for the cluster to become active'
        ),
    )
    @display_table(ClusterDetail)
    def resize(self, cluster_id, node_groups=None, wait=False):
        """
        Resize a cluster

        :param cluster_id: Cluster ID
        :param node_groups: `dict` of `(node_group_id, attrs)` pairs, in which
                            `attrs` is a `dict` of node group attributes.
                            Instead of a `dict`, you may give a `list` of
                            `dicts`, each containing the `id` key. Currently
                            supported attributes are `flavor_id` and `count`
        :returns: :class:`~lavaclient.api.response.ClusterDetail`
        """
        if not node_groups:
            raise error.RequestError("Must specify atleast one node_group "
                                     "to resize")

        gathered = self._gather_node_groups(node_groups)
        if not all('count' in node_group and 'id' in node_group
                   for node_group in gathered):
            raise error.RequestError("Invalid or missing option "
                                     "in the node groups")

        data = dict(
            cluster=dict(
                node_groups=gathered
            )
        )

        request_data = self._marshal_request(data, ClusterUpdateRequest)

        @id_or_name
        def _func(self, cluster_id):
            return self._parse_response(
                self._client._put('clusters/{0}'.format(cluster_id),
                                  json=request_data),
                ClusterResponse,
                wrapper='cluster')

        cluster = _func(self, cluster_id)

        if wait:
            return self.wait(cluster.id)

        return cluster

    @command(
        parser_options=dict(
            description='Update the connector credentials for a cluster',
        ),
        credentials=argument(
            '--credential',
            type=parse_credential, action='append',
            help='Credentials to use in the cluster. Only the credentials you '
            'specify are modified. No credentials are removed. Each must be'
            ' in the form of `type=name`. See `lava credentials`'
        ),
        wait=argument(
            action='store_true',
            help='Wait for the cluster to become active')
    )
    @display_table(ClusterDetail)
    def _update_credentials(self, cluster_id, credentials=None, wait=False):
        if not credentials:
            raise error.RequestError("Must specify at least one credential")

        creds = defaultdict(list)
        for cred in credentials:
            ctype, name = six.next(c for c in six.iteritems(cred))
            creds[ctype].append(name)
        return self.update_credentials(cluster_id, creds, wait)

    def update_credentials(self, cluster_id, credentials=None, wait=False):
        """
        Update the credentials on a cluster

        :param cluster_id: Cluster ID
        :param credentials: The credential name to on update the cluster.
                            Credentials must be a dict of {type: [name]} values
        :param wait: If `True`, wait for the cluster to become active before
                     returning
        :returns: :class:`~lavaclient.api.response.ClusterDetail`
        """
        new_creds = []
        for ctype in credentials:
            names = credentials[ctype]
            for name in names:
                new_creds.append({'name': name, 'type': ctype})

        request_data = self._marshal_request({'credentials': new_creds},
                                             ClusterCredentialsRequest,
                                             wrapper='cluster')

        @id_or_name
        def _func(self, cluster_id):
            return self._client._put('clusters/{0}'.format(cluster_id),
                                     json=request_data)

        response = _func(self, cluster_id)
        cluster = self._parse_response(
            response,
            ClusterResponse,
            wrapper='cluster')

        if wait:
            return self.wait(cluster.id)

        return cluster

    @command(
        parser_options=dict(
            description='Remove the specified ssh keys from a cluster cluster',
        ),
        keynames=argument(
            metavar='keyname',
            nargs='+',
            help='Name of ssh credential to remove from the cluster'),
        wait=argument(
            action='store_true',
            help='Wait for the cluster to become active'
        ),
        force=argument(
            action='store_true',
            help="Suppress delete confirmation dialog")
    )
    @display_table(ClusterDetail)
    def _delete_ssh_credentials(self, cluster_id, keynames, wait=False,
                                force=False):
        if not force:
            if not confirm('Remove these keys from this cluster?'):
                return

        return self.delete_ssh_credentials(cluster_id, keynames, wait)

    def delete_ssh_credentials(self, cluster_id, keynames, wait):
        """
        Remove the specified SSH credentials from a cluster.

        :param cluster_id: Cluster ID
        :param keys: The ssh key names to remove from the cluster
        :param wait: If `True`, wait for the cluster to become active before
                     returning
        :returns: :class:`~lavaclient.api.response.ClusterDetail`
        """

        creds = []

        for name in keynames:
            creds.append({'name': name, 'type': 'ssh_keys'})

        request_data = self._marshal_request({'remove_credentials': creds},
                                             ClusterCredentialsRemovalRequest,
                                             wrapper='cluster')

        @id_or_name
        def _func(self, cluster_id):
            return self._client._put('clusters/{0}'.format(cluster_id),
                                     json=request_data)

        response = _func(self, cluster_id)
        cluster = self._parse_response(
            response,
            ClusterResponse,
            wrapper='cluster')

        if wait:
            return self.wait(cluster.id)

        return cluster

    def _create_default_ssh_credential(self):
        if not confirm('You have not uploaded any SSH key credentials; do '
                       'you want to upload {0} now?'.format(
                           DEFAULT_SSH_PUBKEY)):
            sys.exit(1)

        try:
            with open(expand(DEFAULT_SSH_PUBKEY)) as f:
                six.print_('SSH key does not exist; creating...')
                self._client.credentials.create_ssh_key(
                    DEFAULT_SSH_KEY,
                    f.read().strip())
        except IOError:
            six.print_('No SSH keypair found; to generate a keypair, run '
                       '`ssh-keygen`')
            sys.exit(1)

        return DEFAULT_SSH_KEY

    @command(
        parser_options=dict(
            description='Create a new Lava cluster',
        ),
        name=argument(help='Cluster name'),
        username=argument(
            help='Login name of the user to install onto the created nodes; '
                 'defaults to {0}'.format(getuser()),
            default=getuser()),
        ssh_keys=argument(
            '--ssh-key', action='append',
            help="SSH key name; may be used multiple times. If not "
                 "specified, the client will attempt to use the key "
                 "'{key}', creating it from ~/.ssh/id_rsa.pub if it "
                 "doesn't exist. See `lava credentials`".format(
                     key=DEFAULT_SSH_KEY)),
        stack_id=argument(
            help='Valid Lava stack ID. For a list of stacks, use the '
                 '`lava stacks list` command'),
        user_scripts=argument(
            '--user-script', action='append',
            help='User script ID: See `lava scripts --help` for more '
                 'information; may be used multiple times to provide multiple '
                 'script IDs'),
        node_groups=argument(
            type=parse_node_group, action='append',
            help='Node group options; may be used multiple times to '
                 'configure multiple node groups. Each option should be in '
                 'the form <id>(<key>=<value>, ...), where <id> is a valid '
                 'node group ID for the stack and the key-value pairs are '
                 'options to specify for that node group. Current valid '
                 'options are `count` and `flavor_id`'),
        credentials=argument(
            '--credential', action='append', type=parse_credential,
            help='Credentials to use in the cluster. Each must be in the '
                 'form of `type=name`. See `lava credentials`'),
        connectors=argument(
            '--connector', action='append', type=parse_credential,
            help='Deprecated'),
        cluster_region=argument(
            '--cluster-region',
            help='The region to create the cluster in'
        ),
        wait=argument(
            action='store_true',
            help='Wait for the cluster to become active'
        ),
    )
    @display_table(ClusterDetail)
    def _create(self, name, stack_id, username=None, ssh_keys=None,
                user_scripts=None, node_groups=None, connectors=None,
                wait=False, credentials=None, cluster_region=None):
        """
        CLI-only; cluster create command
        """
        if ssh_keys is None:
            ssh_keys = [DEFAULT_SSH_KEY]

        try:
            return self.create(name,
                               stack_id,
                               username=username,
                               ssh_keys=ssh_keys,
                               user_scripts=user_scripts,
                               node_groups=node_groups,
                               connectors=connectors,
                               wait=wait,
                               credentials=credentials,
                               region=cluster_region)
        except error.RequestError as exc:
            if self._args.headless or not (
                    ssh_keys == [DEFAULT_SSH_KEY] and (
                        'Cannot find requested ssh_keys' in str(exc) or
                        'One or more ssh_keys are invalid' in str(exc)
                    )):
                raise

        # Create the SSH key for the user and then attempt to create the
        # cluster again
        self._create_default_ssh_credential()

        return self.create(name,
                           stack_id,
                           username=username,
                           ssh_keys=ssh_keys,
                           user_scripts=user_scripts,
                           node_groups=node_groups,
                           connectors=connectors,
                           wait=wait,
                           credentials=credentials,
                           region=cluster_region)

    @command(
        parser_options=dict(
            description='Delete one or more clusters',
        ),
        cluster_ids=argument(nargs='+', metavar='<cluster ID>',
                             help='Cluster ID'),
        force=argument(action='store_true',
                       help="Suppress delete confirmation dialog"),
    )
    def _delete(self, cluster_ids, force=False):
        for cluster_id in cluster_ids:
            if not force:
                cluster = self._get_by_name_or_id(cluster_id)
                cluster_id = cluster.id

                display_result(cluster, ClusterDetail)

                if not confirm('Delete this cluster?'):
                    continue

            self.delete(cluster_id)

    def delete(self, cluster_id):
        """
        Delete a cluster

        :param cluster_id: Cluster ID
        :returns: :class:`~lavaclient.api.response.ClusterDetail`
        """
        @id_or_name
        def _func(self, cluster_id):
            self._client._delete('clusters/' + six.text_type(cluster_id))

        _func(self, cluster_id)

    @coroutine
    def _cli_wait_printer(self, start):
        """Coroutine that runs during the wait command. Prints status to stdout
        if the command line is running; otherwise, just log the status."""

        started = False

        while True:
            cluster = yield
            LOG.debug('Cluster {0}: {1}'.format(cluster.id, cluster.status))

            if self._command_line:
                if not started:
                    started = True
                    cli_msg_length = 0
                    six.print_('Waiting for cluster {0}'.format(cluster.id))

                msg = 'Status: {0} (Elapsed time: {1:.1f} minutes)'.format(
                    cluster.status, elapsed_minutes(start))
                sys.stdout.write('\b' * cli_msg_length + msg)
                sys.stdout.flush()
                cli_msg_length = len(msg)

                if cluster.status == 'ACTIVE':
                    six.print_('\n')

    @command(
        parser_options=dict(
            description='Poll a cluster until it becomes active'
        ),
        timeout=argument(type=natural_number,
                         help='Poll timeout (in minutes)'),
        interval=argument(type=natural_number,
                          help='Poll interval (in seconds)'),
    )
    @display_table(ClusterDetail)
    def wait(self, cluster_id, timeout=None, interval=None):
        """
        Wait (blocking) for a cluster to either become active or fail.

        :param cluster_id: Cluster ID
        :param timeout: Wait timeout in minutes (default: no timeout)
        :returns: :class:`~lavaclient.api.response.ClusterDetail`
        """
        if interval is None:
            interval = WAIT_INTERVAL

        interval = max(MIN_INTERVAL, interval)

        delta = timedelta(minutes=timeout) if timeout else timedelta(days=365)

        start = datetime.now()
        timeout_date = start + delta

        printer = self._cli_wait_printer(start)
        cluster_id = self._get_by_name_or_id(cluster_id).id

        while datetime.now() < timeout_date:
            cluster = self.get(cluster_id)
            printer.send(cluster)

            if cluster.status == 'ACTIVE':
                return cluster
            elif cluster.status not in IN_PROGRESS_STATES:
                raise error.FailedError(
                    'Cluster status is {0}'.format(cluster.status))

            if datetime.now() + timedelta(seconds=interval) >= timeout_date:
                break

            time.sleep(interval)

        raise error.TimeoutError(
            'Cluster did not become active before timeout')

    @command(parser_options=dict(
        description='List all nodes in the cluster'
    ))
    @display(Node.display_nodes)
    def nodes(self, cluster_id):
        """
        Get the cluster nodes

        :param cluster_id: Cluster ID
        :returns: List of :class:`~lavaclient.api.response.Node` objects
        """
        return self._client.nodes.list(cluster_id)

    def _get_named_node(self, nodes, node_name=None, wait=False):
        if node_name is None:
            return nodes[0]

        try:
            return six.next(node for node in nodes
                            if node.name.lower() == node_name.lower())
        except StopIteration:
            raise error.InvalidError(
                'Invalid node: {0}; available nodes are {1}'.format(
                    node_name, ', '.join(node.name for node in nodes)))

    def _get_component_node(self, nodes, component, wait=False):
        components_by_node = (
            (node, (comp['name'].lower() for comp in node.components))
            for node in nodes)

        try:
            return six.next(node for node, components in components_by_node
                            if component.lower() in components)
        except StopIteration:
            raise error.InvalidError(
                'Component {0} not found in cluster'.format(component))

    def _get_by_name_or_id(self, maybe_id):
        try:
            return self.get(maybe_id)
        except error.RequestError as exc:
            if exc.code != 404:
                raise

            cluster = next((cluster for cluster in self.list()
                            if cluster.name == maybe_id), None)
            if not cluster:
                raise

            return cluster

    def _ssh_cluster_nodes(self, cluster_id, wait=False):
        """
        Return `(cluster, nodes)`, where `nodes` is a list of all non-Ambari
        nodes in the cluster. If the cluster is not ACTIVE/ERROR/IMPAIRED and
        wait is `True`, the function will block until it becomes active;
        otherwise, an exception is thrown.
        """
        cluster = self._get_by_name_or_id(cluster_id)
        status = cluster.status.upper()
        if status not in FINAL_STATES:
            LOG.debug('Cluster status: %s', status)

            if status not in IN_PROGRESS_STATES:
                raise error.InvalidError(
                    'Cluster is in state {0}'.format(status))
            elif not wait:
                raise error.InvalidError('Cluster is not yet active')

            self.wait(cluster_id)

        if cluster.status.upper() == 'ERROR':
            raise error.InvalidError('Cluster is in state ERROR')

        nodes = [node for node in self.nodes(cluster_id)
                 if node.name.lower() != 'ambari']
        return cluster, nodes

    @command(
        parser_options=dict(
            description='Create a SOCKS5 proxy over SSH to a node in the '
                        'cluster'
        ),
        port=argument(type=int, help='Port on localhost on which to create '
                                     'the proxy'),
        node_name=argument(help='Name of node on which to make the SSH '
                                'connection, e.g. gateway-1. By default, use '
                                'first available node.'),
        ssh_command=argument(help="SSH command (default: 'ssh')"),
        wait=argument(action='store_true',
                      help="Wait for cluster to become active (if it isn't "
                           "already)"),
    )
    def ssh_proxy(self, cluster_id, port=None, node_name=None,
                  ssh_command=None, wait=False):
        """
        Set up a SOCKS5 proxy over SSH to a node in the cluster. Returns the
        SSH process (via :py:class:`~subprocess.Popen`), which can be
        stopped via the :func:`kill` method.

        :param cluster_id: Cluster ID
        :param port: Local port on which to create the proxy (default: 12345)
        :param node_name: Name of node on which to make the SSH connection. By
                          default, use first available node.
        :param wait: If `True`, wait for the cluster to become active before
                     creating the proxy
        :returns: :py:class:`~subprocess.Popen` object representing the SSH
                  connection.
        """
        if port is None:
            port = 12345

        cluster, nodes = self._ssh_cluster_nodes(cluster_id, wait=wait)
        ssh_node = self._get_named_node(nodes, node_name=node_name)

        # Get a URL to test the proxy against
        all_urls = itertools.chain.from_iterable(
            [component['uri'] for component in node.components
             if 'uri' in component and component['uri'].startswith('http')]
            for node in nodes)
        test_url = six.next(all_urls, None)

        printer = self._cli_printer(LOG)
        printer('Starting SOCKS proxy via node {0} ({1})'.format(
            ssh_node.name, ssh_node.public_ip))

        process = create_socks_proxy(cluster.username, ssh_node.public_ip,
                                     port, ssh_command=ssh_command,
                                     test_url=test_url)

        printer('Successfully created SOCKS proxy on localhost:{0}'.format(
            port), logging.INFO)

        if not self._command_line:
            return process

        try:
            printer('Use Ctrl-C to stop proxy', logging.NOTSET)
            process.communicate()
        except KeyboardInterrupt:
            printer('SOCKS proxy closed')

    @command(
        parser_options=dict(
            description='SSH to a node in the cluster and optionally execute '
                        'a command'
        ),
        node_name=argument(help='Name of node on which to make the SSH '
                                'connection, e.g. gateway-1. By default, use '
                                'first available node.'),
        ssh_command=argument(help="SSH command (default: 'ssh')"),
        wait=argument(action='store_true',
                      help="Wait for cluster to become active (if it isn't "
                           "already)"),
        command=argument(help='Command to execute over SSH')
    )
    def _ssh(self, cluster_id, node_name=None, ssh_command=None, wait=False,
             command=None):
        """
        Command-line only. SSH to the desired cluster node.
        """
        output = self._execute_ssh(cluster_id, node_name=node_name,
                                   ssh_command=ssh_command, wait=wait,
                                   command=command)
        if command:
            six.print_(output)

    def _execute_ssh(self, cluster_id, node_name=None, ssh_command=None,
                     wait=False, command=None):
        cluster, nodes = self._ssh_cluster_nodes(cluster_id, wait=wait)
        node = self._get_named_node(nodes, node_name=node_name)

        return node._ssh(cluster.username, command=command,
                         ssh_command=ssh_command)

    def ssh_execute(self, cluster_id, node_name, command, ssh_command=None,
                    wait=False):
        """
        Execute a command over SSH to the specified node in the cluster.

        :param cluster_id: Cluster ID
        :param node_name: Name of node on which to make the SSH connection. By
                          default, use first available node.
        :param command: Shell command to execute remotely
        :param ssh_command: SSH shell command to execute locally
                            (default: `'ssh'`)
        :param wait: If `True`, wait for the cluster to become active before
                     creating the proxy
        :returns: The output of running the command
        """
        return self._execute_ssh(cluster_id, node_name=node_name,
                                 ssh_command=ssh_command, command=command,
                                 wait=wait)

    @command(
        parser_options=dict(description='Create SSH tunnel'),
        local_port=argument(type=int,
                            help='Port to which to bind on localhost'),
        remote_port=argument(type=int,
                             help='Port to which to bind on cluster node'),
        node_name=argument(help='Name of node on which to make the SSH '
                                'connection, e.g. gateway-1. Mutually '
                                'exclusive with --component'),
        component=argument(help='Find and connect to node containing a '
                                'particular component, e.g. HiveServer2. '
                                'Mutually exclusive with --node-name'),
        ssh_command=argument(help="SSH command (default: 'ssh')"),
        wait=argument(action='store_true',
                      help="Wait for cluster to become active (if it isn't "
                           "already)"),
    )
    def ssh_tunnel(self, cluster_id, local_port, remote_port, node_name=None,
                   component=None, ssh_command=None, wait=False):
        """
        Create a SSH tunnel from the local host to a particular port on a
        cluster node.  Returns the SSH process (via
        :py:class:`~subprocess.Popen`), which can be stopped via the
        :func:`kill` method.

        :param cluster_id: Cluster ID
        :param local_port: Port on which to bind on localhost
        :param remote_port: Port on which to bind on the cluster node
        :param node_name: Name of node on which to make the SSH connection.
                          Mutually exclusive with `component`
        :param component: Name of a component installed on a node in the
                          cluster, e.g. `HiveServer2`. SSH tunnel will be set
                          up on the first node containing the component.
                          Mutually exclusive with `node_name`
        :param wait: If `True`, wait for the cluster to become active before
                     creating the proxy
        :returns: :py:class:`~subprocess.Popen` object representing the SSH
                  connection.
        """
        if node_name and component:
            raise error.InvalidError(
                'node_name and component are mutually exclusive')
        elif not (node_name or component):
            raise error.InvalidError(
                'One of node_name or component is required')

        cluster, nodes = self._ssh_cluster_nodes(cluster_id, wait=wait)

        if component:
            ssh_node = self._get_component_node(nodes, component)
        else:
            ssh_node = self._get_named_node(nodes, node_name=node_name)

        printer = self._cli_printer(LOG)
        printer('Starting SSH tunnel from localhost:{0} to {1}:{2} '
                '({3})'.format(local_port, ssh_node.name, remote_port,
                               ssh_node.public_ip))

        process = create_ssh_tunnel(cluster.username, ssh_node, local_port,
                                    remote_port, ssh_command=ssh_command)

        printer('Successfully created SSH tunnel to localhost:{0}'.format(
            local_port), logging.INFO)

        if not self._command_line:
            return process

        try:
            printer('Use Ctrl-C to stop tunnel', logging.NOTSET)
            process.communicate()
        except KeyboardInterrupt:
            printer('SSH tunnel closed')

    def services(self, action, cluster_id, service_names):
        data = {
            'cluster': {
                'control_services': {
                    'action': action,
                    'services': [{'name': name} for name in service_names],
                }
            }
        }

        @id_or_name
        def _service(self, cluster_id):
            return self._parse_response(
                self._client._put('clusters/{0}'.format(cluster_id),
                                  json=data),
                ClusterResponse,
                wrapper='cluster')

        return _service(self, cluster_id)

    @command(
        parser_options=dict(
            description='Start/stop/restart services on a cluster'
        ),
        action=argument(choices=['start', 'stop', 'restart']),
        service_names=argument(metavar='<service>', nargs='*',
                               help='Cluster service name, e.g. HDFS'),
        force=argument(action='store_true',
                       help="Don't display confirmation dialog"),
    )
    @display_table(ClusterDetail)
    def _services(self, action, cluster_id, service_names, force=False):
        if not force:
            if service_names:
                services_str = 'service{0} {1}'.format(
                    's' if len(service_names) > 1 else '',
                    ', '.join(service_names))
            else:
                services_str = 'all services'

            dialog = '{0} {1} on cluster {2}?'.format(
                action.capitalize(),
                services_str,
                cluster_id)

            if not confirm(dialog):
                return

        return self.services(action, cluster_id, service_names)
