import logging
import six
from figgis import Config, ListField

from lavaclient.api.clusters import id_or_name
from lavaclient.api import resource
from lavaclient import constants
from lavaclient.api.response import Node
from lavaclient.util import argument, command, confirm, display, CommandLine

LOG = logging.getLogger(constants.LOGGER_NAME)


######################################################################
# API Responses
######################################################################

class NodesResponse(Config):

    """Response from /clusters/<cluster_id>/nodes"""

    nodes = ListField(Node, required=True)


######################################################################
# API Resource
######################################################################

@six.add_metaclass(CommandLine)
class Resource(resource.Resource):

    """Nodes API methods"""
    @command(parser_options=dict(
        description='List all nodes in a cluster'
    ))
    @display(Node.display_nodes)
    def list(self, cluster_id):
        """
        List nodes belonging to the cluster.

        :returns: List of :class:`~lavaclient.api.response.Node` objects
        """
        @id_or_name
        def _func(self, cluster_id):
            return self._parse_response(
                self._client._get('clusters/{0}/nodes'.format(cluster_id)),
                NodesResponse,
                wrapper='nodes')

        return _func(self, cluster_id)

    @command(
        parser_options=dict(
            description='Delete the specified node in a cluster'
        ),
        force=argument(action='store_true',
                       help="Suppress delete confirmation dialog")
    )
    def _delete(self, cluster_id, node_id, force=False):
        if not force and not confirm('Delete this node?'):
            return

        self.delete(cluster_id, node_id)

    def delete(self, cluster_id, node_id):
        """
        Delete the node with node_id belonging to the cluster

        :param cluster_id:
        :param node_id:
        :return: None
        """
        @id_or_name
        def _func(self, cluster_id):
            self._client._delete('clusters/{0}/nodes/{1}'.format(
                cluster_id, node_id))

        _func(self, cluster_id)
